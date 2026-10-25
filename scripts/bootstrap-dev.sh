#!/usr/bin/env bash

set -Eeuo pipefail
shopt -s nullglob

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The single-node k3s cluster installed by ./scripts/bootstrap-host.sh. Its
# kubeconfig context is renamed from k3s's "default" to homelab-dev.
cluster_context="homelab-dev"
gateway_url="http://127.0.0.1:8080"
models_dir="${MODELS_DIR:-${HOME}/models}"
# Home-network access is opt-in: without LAN_ADDRESS the lan listener stays on
# loopback. The address lives in the flux-system/homelab-host ConfigMap, so
# later runs keep it; LAN_ADDRESS=off turns it off again. The root
# certificate authority lives on the host, outside Git and the cluster, so
# devices that trust it survive rebuilds.
lan_request="${LAN_ADDRESS:-}"
lan_address=""
lab_domain="lab.internal"
lab_ca_dir="${LAB_CA_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/homelab/lab-ca}"
# Garage's RPC secret and admin token are generated once and kept on the host,
# like the lab CA, so they never enter Git. S3 access keys are not kept here:
# they live in Garage's own database (see scripts/ml-credentials.py).
ml_storage_dir="${ML_STORAGE_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/homelab/ml-storage}"
# The Kind cluster this replaces; it must not run beside k3s.
kind_control_plane="homelab-dev-control-plane"

validate_gpu_directory() {
  local gpu_directory="${1:-/dev/dri}"
  local kfd_device="${2:-/dev/kfd}"
  local render_device

  if [[ ! -c "${kfd_device}" ]]; then
    echo "${kfd_device} is missing. AMD's device plugin needs it to advertise the GPU;" >&2
    echo "./scripts/bootstrap-host.sh checks the amdgpu driver." >&2
    return 1
  fi
  for render_device in "${gpu_directory}"/renderD*; do
    if [[ -c "${render_device}" ]]; then
      return
    fi
  done

  echo "No GPU render device found in ${gpu_directory}." >&2
  echo "The Vulkan LLM requires a host GPU with a working /dev/dri/renderD* device." >&2
  return 1
}

# MODELS_DIR is substituted by Flux into a quoted YAML string in the LLM's
# hostPath volume, so it must be an absolute path without characters that
# would end or escape that string.
validate_models_dir() {
  python3 - "${models_dir}" <<'PY'
import sys

path = sys.argv[1]
if not path.startswith("/"):
    raise SystemExit(f"MODELS_DIR must be an absolute path; got {path!r}.")
if any(character in path for character in '"\\$') or any(ord(c) < 32 or ord(c) == 127 for c in path):
    raise SystemExit(f"MODELS_DIR {path!r} contains a quote, backslash, dollar sign or control "
                     "character, which cannot be passed safely to the cluster. Move the models.")
PY
}

# The LLM runs as the owner of the model directory (never root) and needs
# the host's render and video group IDs for the GPU device nodes.
host_settings() {
  local models_uid models_gid render_gid video_gid

  models_uid="$(stat -c %u "${models_dir}")"
  models_gid="$(stat -c %g "${models_dir}")"
  if [[ "${models_uid}" == "0" || "${models_gid}" == "0" ]]; then
    echo "${models_dir} is owned by root; the LLM server runs as the directory's owner and never as root." >&2
    echo "Give it to your user: sudo chown -R \"\$USER:\$USER\" ${models_dir}" >&2
    return 1
  fi
  render_gid="$(getent group render | cut -d: -f3)"
  video_gid="$(getent group video | cut -d: -f3)"
  if [[ -z "${render_gid}" || -z "${video_gid}" ]]; then
    echo "The host has no render or video group; GPU device permissions depend on them." >&2
    return 1
  fi
  printf 'MODELS_UID=%s\nMODELS_GID=%s\nRENDER_GID=%s\nVIDEO_GID=%s\n' \
    "${models_uid}" "${models_gid}" "${render_gid}" "${video_gid}"
}

validate_lan_address() {
  local address="${1:-${lan_request}}"

  if [[ -z "${address}" || "${address}" == "off" ]]; then
    return
  fi

  python3 - "${address}" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit(f"LAN_ADDRESS must be an IPv4 address or off; got {sys.argv[1]!r}.")
private_networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
if not any(address in ipaddress.ip_network(network) for network in private_networks):
    raise SystemExit(
        f"LAN_ADDRESS {address} is not a private home-network address "
        "(10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16)."
    )
PY
  if ! lan_address_is_assigned "${address}"; then
    echo "LAN_ADDRESS ${address} is not assigned to this host." >&2
    echo "Check it with: ip -4 address" >&2
    return 1
  fi
}

lan_address_is_assigned() {
  python3 -c 'import socket, sys; socket.socket().bind((sys.argv[1], 0))' "$1" 2>/dev/null
}

# The address the cluster currently publishes the lan listener on, if any.
published_lan_address() {
  kubectl --context "${cluster_context}" --namespace flux-system \
    get configmap homelab-host --ignore-not-found -o jsonpath='{.data.LAN_ADDRESS}'
}

resolve_lan_address() {
  local published

  published="$(published_lan_address)"
  case "${lan_request}" in
    "")
      lan_address="${published}"
      if [[ -n "${lan_address}" ]] && ! lan_address_is_assigned "${lan_address}"; then
        echo "The cluster publishes home-network HTTPS on ${lan_address}, which is no longer assigned to this host." >&2
        echo "Restore the router's DHCP reservation, or rerun with LAN_ADDRESS=<new address> or LAN_ADDRESS=off." >&2
        return 1
      fi
      ;;
    off) lan_address="" ;;
    *) lan_address="${lan_request}" ;;
  esac
  if [[ "${lan_address}" != "${published}" ]]; then
    echo "Home-network HTTPS: ${published:-off} -> ${lan_address:-off}."
  fi
}

# Host values the cluster's manifests need, substituted by Flux: the lan
# listener address (envoy-proxy.yaml) and the LLM's model directory, user
# and GPU groups (llm/deployment.yaml).
write_host_configmap() {
  local settings="$1"
  local arguments=(--from-literal="MODELS_DIR=${models_dir}")
  local line

  while IFS= read -r line; do
    arguments+=(--from-literal="${line}")
  done <<<"${settings}"
  if [[ -n "${lan_address}" ]]; then
    arguments+=(--from-literal="LAN_ADDRESS=${lan_address}")
  fi
  kubectl --context "${cluster_context}" --namespace flux-system \
    create configmap homelab-host "${arguments[@]}" \
    --dry-run=client -o yaml \
    | kubectl --context "${cluster_context}" apply \
      --server-side --force-conflicts --field-manager=homelab-bootstrap -f -
}

ensure_lab_ca() {
  local certificate="${lab_ca_dir}/ca.crt"
  local key="${lab_ca_dir}/ca.key"

  if [[ -f "${certificate}" && -f "${key}" ]]; then
    if [[ "$(openssl x509 -in "${certificate}" -noout -pubkey)" \
      != "$(openssl pkey -in "${key}" -pubout)" ]]; then
      echo "${key} does not belong to ${certificate}." >&2
      return 1
    fi
    return
  fi
  if [[ -e "${certificate}" || -e "${key}" ]]; then
    echo "Only part of the lab certificate authority exists in ${lab_ca_dir}." >&2
    echo "Restore the missing file from your copy; bootstrap will not replace a root that devices may trust." >&2
    return 1
  fi

  echo "Creating the lab certificate authority in ${lab_ca_dir}..."
  mkdir -p "${lab_ca_dir}"
  chmod 700 "${lab_ca_dir}"
  # Name constraints limit the root to lab.internal names and forbid IP
  # addresses, so a leaked key cannot impersonate any other site.
  (
    umask 077
    openssl req -x509 -new -noenc -days 3650 \
      -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
      -subj "/CN=Homelab ${lab_domain} CA" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "nameConstraints=critical,permitted;DNS:${lab_domain},excluded;IP:0.0.0.0/0.0.0.0,excluded;IP:0:0:0:0:0:0:0:0/0:0:0:0:0:0:0:0" \
      -keyout "${key}.new" -out "${certificate}.new"
    mv "${key}.new" "${key}"
    mv "${certificate}.new" "${certificate}"
  )
}

load_lab_ca() {
  # Server-side apply keeps the private key out of a last-applied annotation.
  kubectl --context "${cluster_context}" create namespace cert-manager \
    --dry-run=client -o yaml \
    | kubectl --context "${cluster_context}" apply \
      --server-side --field-manager=homelab-bootstrap -f -
  kubectl --context "${cluster_context}" --namespace cert-manager \
    create secret tls lab-ca \
    --cert="${lab_ca_dir}/ca.crt" --key="${lab_ca_dir}/ca.key" \
    --dry-run=client -o yaml \
    | kubectl --context "${cluster_context}" apply \
      --server-side --field-manager=homelab-bootstrap -f -
}

ensure_ml_storage_secrets() {
  local rpc_secret="${ml_storage_dir}/rpc-secret"
  local admin_token="${ml_storage_dir}/admin-token"

  if [[ -f "${rpc_secret}" && -f "${admin_token}" ]]; then
    return
  fi
  if [[ -e "${rpc_secret}" || -e "${admin_token}" ]]; then
    echo "Only part of the Garage secrets exists in ${ml_storage_dir}." >&2
    echo "Restore the missing file from your copy, or delete both to generate new ones;" >&2
    echo "Garage's data does not depend on them (docs/operations.md)." >&2
    return 1
  fi

  echo "Creating Garage secrets in ${ml_storage_dir}..."
  mkdir -p "${ml_storage_dir}"
  chmod 700 "${ml_storage_dir}"
  (
    umask 077
    # Garage expects the RPC secret as 32 bytes of hex.
    openssl rand -hex 32 | tr -d '\n' > "${rpc_secret}.new"
    openssl rand -base64 32 | tr -d '\n' > "${admin_token}.new"
    mv "${rpc_secret}.new" "${rpc_secret}"
    mv "${admin_token}.new" "${admin_token}"
  )
}

load_ml_storage_secrets() {
  kubectl --context "${cluster_context}" create namespace ml-storage \
    --dry-run=client -o yaml \
    | kubectl --context "${cluster_context}" apply \
      --server-side --field-manager=homelab-bootstrap -f -
  # The values go through stdin, never the command line, and server-side
  # apply keeps them out of a last-applied annotation.
  python3 - "${ml_storage_dir}/rpc-secret" "${ml_storage_dir}/admin-token" <<'PY' \
    | kubectl --context "${cluster_context}" apply \
      --server-side --field-manager=homelab-bootstrap -f -
import base64
import json
import pathlib
import re
import sys

rpc_secret, admin_token = (pathlib.Path(path).read_text().strip() for path in sys.argv[1:])
if not re.fullmatch(r"[0-9a-f]{64}", rpc_secret):
    raise SystemExit(f"{sys.argv[1]} must hold 64 hexadecimal characters (openssl rand -hex 32).")
if len(admin_token) < 32:
    raise SystemExit(f"{sys.argv[2]} must hold at least 32 characters (openssl rand -base64 32).")
print(json.dumps({
    "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
    "metadata": {"name": "garage", "namespace": "ml-storage"},
    "data": {name: base64.b64encode(value.encode()).decode() for name, value in
             (("GARAGE_RPC_SECRET", rpc_secret), ("GARAGE_ADMIN_TOKEN", admin_token))},
}))
PY
}

# MLflow's artifact store credentials. scripts/ml-credentials.py creates the
# key in Garage (or reuses it) and writes mlflow/s3-credentials; it needs
# Garage running, so this runs after the ml-storage Kustomization is ready and
# before the mlflow one is waited for.
ensure_mlflow_credentials() {
  kubectl --context "${cluster_context}" create namespace mlflow \
    --dry-run=client -o yaml \
    | kubectl --context "${cluster_context}" apply \
      --server-side --field-manager=homelab-bootstrap -f -
  "${repository_root}/scripts/ml-credentials.py" mlflow --bucket mlflow-artifacts
}

require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    echo "Run ./scripts/bootstrap-host.sh first." >&2
    exit 1
  fi
}

# Kind published the same host ports through Docker. Both at once would
# compete for 127.0.0.1:8080 and LAN_ADDRESS:443.
require_kind_stopped() {
  if command -v docker >/dev/null 2>&1 \
    && [[ "$(docker inspect --format '{{.State.Running}}' "${kind_control_plane}" 2>/dev/null)" == "true" ]]; then
    echo "The old Kind cluster is still running (${kind_control_plane})." >&2
    echo "Back it up and stop it first; see docs/migrate-to-k3s.md." >&2
    return 1
  fi
}

require_cluster() {
  if ! kubectl config get-contexts "${cluster_context}" >/dev/null 2>&1; then
    echo "No ${cluster_context} context in your kubeconfig." >&2
    echo "Run ./scripts/bootstrap-host.sh first; it installs k3s and adds the context." >&2
    return 1
  fi
  if ! kubectl --context "${cluster_context}" --request-timeout=10s get --raw /readyz >/dev/null 2>&1; then
    echo "The ${cluster_context} cluster is not answering. Check: systemctl status k3s" >&2
    return 1
  fi
}

bootstrap_flux() {
  local flux_system_path="${repository_root}/kubernetes/clusters/dev/flux-system"

  if flux check --context "${cluster_context}" >/dev/null 2>&1; then
    echo "Flux is already healthy; skipping install."
    return
  fi

  flux check --pre --context "${cluster_context}"

  echo "Installing Flux from the committed manifests..."
  kubectl --context "${cluster_context}" apply \
    -f "${flux_system_path}/gotk-components.yaml"
  kubectl --context "${cluster_context}" wait \
    --for=condition=Established \
    crd/gitrepositories.source.toolkit.fluxcd.io \
    crd/kustomizations.kustomize.toolkit.fluxcd.io \
    --timeout=2m
  kubectl --context "${cluster_context}" apply \
    -f "${flux_system_path}/gotk-sync.yaml"
}

# AMD's device plugin registers amd.com/gpu with the kubelet shortly after
# its pod starts. Without it the LLM stays Pending.
wait_for_gpu_resource() {
  local timeout_seconds="${1:-180}"
  local deadline=$((SECONDS + timeout_seconds))
  local allocatable

  until allocatable="$(
    kubectl --context "${cluster_context}" get nodes \
      -o jsonpath='{.items[*].status.allocatable.amd\.com/gpu}'
  )" && [[ "${allocatable}" =~ [1-9] ]]; do
    if (( SECONDS >= deadline )); then
      echo "The node does not advertise amd.com/gpu. Check the device plugin:" >&2
      echo "  kubectl --context ${cluster_context} -n kube-system logs daemonset/amdgpu-device-plugin" >&2
      return 1
    fi
    sleep 5
  done
}

wait_for_flux_kustomizations() {
  local infrastructure_manifests=(
    "${repository_root}/kubernetes/clusters/dev/infrastructure/gateway-api-controller.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/gateway-api-config.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/cert-manager.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/lab-ca.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/monitoring.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/logging.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/argocd.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/gpu.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/llm.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/chat.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/ml-storage.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/mlflow.yaml"
  )
  local manifests=("${infrastructure_manifests[@]}")
  local manifest_path
  local manifest_name
  local kustomization_name

  for manifest_path in "${manifests[@]}"; do
    manifest_name="$(basename "${manifest_path}")"

    if [[ "${manifest_name}" == "kustomization.yaml" ]]; then
      continue
    fi

    kustomization_name="$(
      awk '$1 == "name:" { print $2; exit }' "${manifest_path}"
    )"

    if [[ -z "${kustomization_name}" ]]; then
      echo "Could not determine the Flux Kustomization in ${manifest_path}." >&2
      exit 1
    fi

    if [[ "${kustomization_name}" == "mlflow" ]]; then
      ensure_mlflow_credentials
    fi

    flux reconcile kustomization "${kustomization_name}" \
      --context "${cluster_context}" \
      --timeout=15m

    if [[ "${kustomization_name}" == "gpu" ]]; then
      wait_for_gpu_resource
    fi
  done
}

wait_for_gateway_api() {
  # On a fresh cluster the Envoy Gateway controller can evaluate the
  # GatewayClass before its EnvoyProxy parameters exist and leave a stale
  # rejected status; a controller restart forces re-evaluation.
  if ! kubectl --context "${cluster_context}" \
    wait gatewayclass/homelab \
    --for=condition=Accepted --timeout=5m; then
    echo "GatewayClass not accepted yet; restarting the Envoy Gateway controller..."
    kubectl --context "${cluster_context}" --namespace envoy-gateway-system \
      rollout restart deployment/envoy-gateway
    kubectl --context "${cluster_context}" --namespace envoy-gateway-system \
      rollout status deployment/envoy-gateway --timeout=3m
    kubectl --context "${cluster_context}" \
      wait gatewayclass/homelab \
      --for=condition=Accepted --timeout=5m
  fi
  kubectl --context "${cluster_context}" --namespace gateway-system \
    wait gateway/homelab \
    --for=condition=Programmed --timeout=5m
  kubectl --context "${cluster_context}" --namespace gateway-system \
    wait certificate/lab-internal \
    --for=condition=Ready --timeout=5m
  kubectl --context "${cluster_context}" --namespace monitoring \
    wait httproute/grafana \
    --for="jsonpath={.status.parents[0].conditions[?(@.type=='Accepted')].status}=True" \
    --timeout=5m
  kubectl --context "${cluster_context}" --namespace argocd \
    wait httproute/argocd \
    --for="jsonpath={.status.parents[0].conditions[?(@.type=='Accepted')].status}=True" \
    --timeout=5m
  kubectl --context "${cluster_context}" --namespace llm \
    wait httproute/llm \
    --for="jsonpath={.status.parents[0].conditions[?(@.type=='Accepted')].status}=True" \
    --timeout=5m
  kubectl --context "${cluster_context}" --namespace chat \
    wait httproute/open-webui \
    --for="jsonpath={.status.parents[0].conditions[?(@.type=='Accepted')].status}=True" \
    --timeout=5m
  kubectl --context "${cluster_context}" --namespace mlflow \
    wait httproute/mlflow \
    --for="jsonpath={.status.parents[0].conditions[?(@.type=='Accepted')].status}=True" \
    --timeout=5m
}

wait_for_gateway_endpoint() {
  local host_header="$1"
  local path="$2"
  local timeout_seconds="${3:-120}"
  local endpoint
  local deadline

  endpoint="${gateway_url}${path}"
  deadline=$((SECONDS + timeout_seconds))

  until curl --noproxy '*' --fail --silent --max-time 2 \
    --header "Host: ${host_header}" "${endpoint}" >/dev/null; do
    if (( SECONDS >= deadline )); then
      echo "Gateway endpoint did not become ready: ${host_header} ${endpoint}" >&2
      return 1
    fi
    sleep 2
  done
}

wait_for_lan_endpoint() {
  local hostname="$1"
  local path="$2"
  local timeout_seconds="${3:-120}"
  local deadline

  deadline=$((SECONDS + timeout_seconds))

  until curl --noproxy '*' --fail --silent --max-time 2 \
    --cacert "${lab_ca_dir}/ca.crt" \
    --resolve "${hostname}:443:${lan_address}" \
    "https://${hostname}${path}" >/dev/null; do
    if (( SECONDS >= deadline )); then
      echo "LAN endpoint did not become ready: https://${hostname}${path} via ${lan_address}" >&2
      return 1
    fi
    sleep 2
  done
}

# The lab mode is cluster state written by scripts/lab-mode.py, not Git. A
# missing ConfigMap means apps mode.
current_lab_mode() {
  local mode

  mode="$(
    kubectl --context "${cluster_context}" --namespace llm \
      get configmap lab-mode --ignore-not-found -o jsonpath='{.data.mode}'
  )"
  if [[ "${mode}" == "llm" ]]; then
    echo llm
  else
    echo apps
  fi
}

wait_for_llm_demand() {
  # The controller decides whether the LLM and chat run, so it is what must
  # be healthy. Its readiness means it has completed a loop against the API.
  kubectl --context "${cluster_context}" --namespace llm \
    rollout status deployment/llm-demand --timeout=5m
}

main() {
  local lab_mode
  local settings

  for required_command in curl kubectl flux openssl python3 sha256sum; do
    require_command "${required_command}"
  done
  validate_lan_address

  # Check local prerequisites before changing the cluster. The check
  # verifies the pinned hashes without starting a multi-gigabyte download.
  MODELS_DIR="${models_dir}" "${repository_root}/scripts/download-models.sh" --check
  models_dir="$(cd "${models_dir}" && pwd -P)"
  validate_models_dir
  validate_gpu_directory
  settings="$(host_settings)"
  require_kind_stopped
  ensure_lab_ca
  ensure_ml_storage_secrets

  require_cluster
  echo "Waiting for Kubernetes nodes..."
  kubectl --context "${cluster_context}" \
    wait --for=condition=Ready nodes --all --timeout=2m
  resolve_lan_address

  bootstrap_flux
  write_host_configmap "${settings}"
  load_lab_ca
  load_ml_storage_secrets

  echo "Reconciling the latest Git revision..."
  flux reconcile kustomization flux-system \
    --with-source \
    --context "${cluster_context}" \
    --timeout=10m

  echo "Waiting for infrastructure..."
  wait_for_flux_kustomizations
  wait_for_gateway_api
  wait_for_gateway_endpoint grafana.localhost /api/health
  wait_for_gateway_endpoint argocd.localhost /healthz
  wait_for_gateway_endpoint mlflow.localhost /health
  wait_for_llm_demand
  lab_mode="$(current_lab_mode)"
  # In apps mode the LLM runs only while an application requests it and the
  # chat is stopped, so their endpoints are expected to be down. Their routes
  # were still checked above. In llm mode the controller may have just
  # started them, so allow for the model to load.
  if [[ "${lab_mode}" == "llm" ]]; then
    wait_for_gateway_endpoint llm.localhost /health 600
    wait_for_gateway_endpoint chat.localhost /health 600
  fi
  if [[ -n "${lan_address}" ]]; then
    if [[ "${lab_mode}" == "llm" ]]; then
      wait_for_lan_endpoint "chat.${lab_domain}" /health 600
    fi
    wait_for_lan_endpoint "grafana.${lab_domain}" /api/health
  fi

  echo
  echo "Dev environment is ready."
  flux get all --all-namespaces --context "${cluster_context}"
  kubectl --context "${cluster_context}" \
    get deployments,pods,services --all-namespaces
  kubectl --context "${cluster_context}" \
    get gatewayclasses.gateway.networking.k8s.io
  kubectl --context "${cluster_context}" \
    get gateways.gateway.networking.k8s.io,httproutes.gateway.networking.k8s.io \
    --all-namespaces
  echo
  echo "Applications are registered through Argo CD and are not part of this"
  echo "repository; re-register them in the Argo CD UI after a cluster rebuild."
  if [[ "${lab_mode}" == "llm" ]]; then
    echo "The lab is in llm mode: applications are stopped; the LLM and chat are running."
    echo "Access the chat UI at http://chat.localhost:8080"
    echo "Access the LLM API at http://llm.localhost:8080/v1"
    echo "Return to apps mode with: ./scripts/lab-mode.py apps"
  else
    echo "The lab is in apps mode: the LLM runs only while an application requests it,"
    echo "and the chat is stopped. For personal chat, run: ./scripts/lab-mode.py llm"
  fi
  echo "Access Grafana at http://grafana.localhost:8080"
  echo "Access Argo CD at http://argocd.localhost:8080 (user admin; password below)"
  echo "Access MLflow at http://mlflow.localhost:8080 (this host only; no login)"
  echo "In-cluster: MLflow http://mlflow.mlflow.svc.cluster.local:5000,"
  echo "S3 http://s3.ml-storage.svc.cluster.local:9000 (credentials: ./scripts/ml-credentials.py NAMESPACE)"
  printf '%s\n' "kubectl --context ${cluster_context} -n argocd get secret argocd-initial-admin-secret -o go-template='{{ index .data \"password\" | base64decode }}{{ \"\\n\" }}'"
  echo "Read the generated Grafana login with:"
  printf '%s\n' "kubectl --context ${cluster_context} -n monitoring get secret kube-prometheus-stack-grafana -o go-template='user: {{ index .data \"admin-user\" | base64decode }}{{ \"\\n\" }}password: {{ index .data \"admin-password\" | base64decode }}{{ \"\\n\" }}'"
  echo
  if [[ -n "${lan_address}" ]]; then
    echo "Home network: https://chat.${lab_domain} (llm mode) and https://grafana.${lab_domain} via ${lan_address}."
    echo "Devices need router DNS entries for these names pointing at ${lan_address},"
    echo "and must trust the root certificate ${lab_ca_dir}/ca.crt (docs/operations.md)."
  else
    echo "Home-network access is off. Turn it on by rerunning with LAN_ADDRESS set to"
    echo "this host's private IPv4 address (docs/operations.md)."
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
