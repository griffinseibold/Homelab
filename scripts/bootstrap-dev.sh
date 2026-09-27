#!/usr/bin/env bash

set -Eeuo pipefail
shopt -s nullglob

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cluster_name="homelab-dev"
cluster_context="kind-homelab-dev"
cluster_config="${repository_root}/kubernetes/kind/dev.yaml"
# Keep this outside main so the EXIT trap can read it after function unwinding.
rendered_cluster_config=""
gateway_node_port="30080"
gateway_host_port="8080"
models_dir="${MODELS_DIR:-${HOME}/models}"
llm_node_label="homelab.local/llm-capable"
# Home-network access is opt-in: without LAN_ADDRESS the Gateway stays on
# loopback. The root certificate authority lives on the host, outside Git and
# outside the cluster, so devices that trust it survive cluster rebuilds.
lan_address="${LAN_ADDRESS:-}"
lan_gateway_container="${cluster_name}-lan-gateway"
lan_gateway_config="${repository_root}/scripts/lan-gateway.conf"
lan_gateway_image="nginx:1.30.5-alpine"
lab_domain="lab.internal"
lab_ca_dir="${LAB_CA_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/homelab/lab-ca}"

validate_gpu_directory() {
  local gpu_directory="${1:-/dev/dri}"
  local render_device

  for render_device in "${gpu_directory}"/renderD*; do
    if [[ -c "${render_device}" ]]; then
      return
    fi
  done

  echo "No GPU render device found in ${gpu_directory}." >&2
  echo "The Vulkan LLM requires a host GPU with a working /dev/dri/renderD* device." >&2
  return 1
}

render_cluster_config() {
  local output_path="$1"

  # A JSON string is also a YAML quoted scalar. This preserves spaces,
  # quotes, backslashes and other characters without evaluating the path.
  python3 - "${cluster_config}" "${models_dir}" "${output_path}" <<'PY'
import json
import pathlib
import sys

source, models_dir, output = sys.argv[1:]
config = pathlib.Path(source).read_text()
marker = '"__MODELS_DIR__"'
if config.count(marker) != 2:
    raise SystemExit("Expected two model directory placeholders in Kind configuration")
pathlib.Path(output).write_text(config.replace(marker, json.dumps(models_dir)))
PY
}

validate_lan_address() {
  if [[ -z "${lan_address}" ]]; then
    return
  fi

  python3 - "${lan_address}" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit(f"LAN_ADDRESS must be an IPv4 address; got {sys.argv[1]!r}.")
private_networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
if not any(address in ipaddress.ip_network(network) for network in private_networks):
    raise SystemExit(
        f"LAN_ADDRESS {address} is not a private home-network address "
        "(10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16)."
    )
PY
  if ! lan_address_is_assigned "${lan_address}"; then
    echo "LAN_ADDRESS ${lan_address} is not assigned to this host." >&2
    echo "Check it with: ip -4 address" >&2
    return 1
  fi
}

lan_address_is_assigned() {
  python3 -c 'import socket, sys; socket.socket().bind((sys.argv[1], 0))' "$1" 2>/dev/null
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

configure_lan_gateway() {
  local config

  # Recreate on every run so configuration changes apply, and so running
  # without LAN_ADDRESS turns home-network access off again.
  docker rm --force "${lan_gateway_container}" >/dev/null 2>&1 || true
  if [[ -z "${lan_address}" ]]; then
    return
  fi

  config="$(sed "s/__GATEWAY_NODE__/${cluster_name}-control-plane/" "${lan_gateway_config}")"
  echo "Publishing the Gateway's lan listener on ${lan_address}:443..."
  # Binding the single private IPv4 address keeps the port off every other
  # interface, including the host's public IPv6 address. The container joins
  # Kind's network to reach the control-plane NodePort by name.
  docker run --detach --name "${lan_gateway_container}" \
    --network kind --restart unless-stopped \
    --publish "${lan_address}:443:443" \
    --env "NGINX_CONFIG=${config}" \
    "${lan_gateway_image}" \
    sh -c 'printf "%s\n" "${NGINX_CONFIG}" >/etc/nginx/nginx.conf && exec nginx -g "daemon off;"' \
    >/dev/null
}

validate_cluster_mounts() {
  local node_names
  local cluster_nodes

  node_names="$(kind get nodes --name "${cluster_name}")"
  if [[ -z "${node_names}" ]]; then
    echo "Could not find nodes in Kind cluster ${cluster_name}." >&2
    return 1
  fi
  mapfile -t cluster_nodes <<<"${node_names}"
  docker inspect "${cluster_nodes[@]}" | python3 -c '
import json
import os
import sys

expected_models_dir = os.path.realpath(sys.argv[1])
workers = []
errors = []
for node in json.load(sys.stdin):
    if node.get("Config", {}).get("Labels", {}).get("io.x-k8s.kind.role") != "worker":
        continue
    name = node["Name"].lstrip("/")
    workers.append(name)
    mounts = {mount["Destination"]: mount for mount in node.get("Mounts", [])}
    for destination, source in (("/models", expected_models_dir), ("/dev/dri", "/dev/dri")):
        mount = mounts.get(destination, {})
        actual_source = mount.get("Source", "")
        displayed_source = actual_source or "<missing>"
        if mount.get("Type") != "bind" or os.path.realpath(actual_source) != source:
            errors.append(f"{name}: {destination} must bind {source}; found {displayed_source}")
        elif destination == "/models" and mount.get("RW", True):
            errors.append(f"{name}: /models must be mounted read-only")
if not workers:
    errors.append("The cluster has no worker nodes for the GPU model server.")
if errors:
    print("Existing cluster mounts are incompatible:", file=sys.stderr)
    print("\n".join(errors), file=sys.stderr)
    print("The cluster has been preserved. Use its original MODELS_DIR, or back up persistent data and explicitly rebuild the cluster to change mounts.", file=sys.stderr)
    raise SystemExit(1)
print("\n".join(workers))
' "${models_dir}"
}

require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    echo "Run ./scripts/bootstrap-host.sh first." >&2
    exit 1
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

wait_for_flux_kustomizations() {
  local infrastructure_manifests=(
    "${repository_root}/kubernetes/clusters/dev/infrastructure/gateway-api-controller.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/gateway-api-config.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/cert-manager.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/lab-ca.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/monitoring.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/logging.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/argocd.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/llm.yaml"
    "${repository_root}/kubernetes/clusters/dev/infrastructure/chat.yaml"
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

    flux reconcile kustomization "${kustomization_name}" \
      --context "${cluster_context}" \
      --timeout=15m
  done
}

gateway_host_port_is_mapped() {
  docker port "${cluster_name}-control-plane" \
    "${gateway_node_port}/tcp" 2>/dev/null \
    | awk -F: -v expected_port="${gateway_host_port}" '
        $NF == expected_port { found = 1 }
        END { exit !found }
      '
}

gateway_url() {
  local node_address

  if gateway_host_port_is_mapped; then
    printf 'http://localhost:%s' "${gateway_host_port}"
    return
  fi

  node_address="$(
    kubectl --context "${cluster_context}" \
      get node "${cluster_name}-control-plane" \
      -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}'
  )"
  printf 'http://%s:%s' "${node_address}" "${gateway_node_port}"
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
}

wait_for_gateway_endpoint() {
  local host_header="$1"
  local path="$2"
  local endpoint
  local deadline

  endpoint="$(gateway_url)${path}"
  deadline=$((SECONDS + 120))

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
  local deadline

  deadline=$((SECONDS + 120))

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

main() {
  local existing_clusters
  local llm_nodes
  local node_name

  for required_command in curl docker kind kubectl flux openssl python3 sha256sum; do
    require_command "${required_command}"
  done
  validate_lan_address

  # Check local prerequisites before creating or changing a cluster. The check
  # verifies the pinned hashes without starting a multi-gigabyte download.
  MODELS_DIR="${models_dir}" "${repository_root}/scripts/download-models.sh" --check
  models_dir="$(cd "${models_dir}" && pwd -P)"
  validate_gpu_directory

  if ! docker info >/dev/null 2>&1; then
    echo "Docker is not available to the current user." >&2
    echo "Log out and back in after running ./scripts/bootstrap-host.sh." >&2
    exit 1
  fi
  ensure_lab_ca

  existing_clusters="$(kind get clusters)"
  if grep -Fxq "${cluster_name}" <<<"${existing_clusters}"; then
    echo "Kind cluster ${cluster_name} already exists."
    llm_nodes="$(validate_cluster_mounts)"
    if ! gateway_host_port_is_mapped; then
      echo "This cluster predates the localhost Gateway port mapping."
      echo "Bootstrap will use its Kind node address without recreating the cluster."
    fi
  else
    echo "Creating Kind cluster ${cluster_name}..."
    rendered_cluster_config="$(mktemp --suffix=.yaml)"
    trap 'rm -f -- "${rendered_cluster_config}"' EXIT
    render_cluster_config "${rendered_cluster_config}"
    kind create cluster --config "${rendered_cluster_config}"
    rm -f -- "${rendered_cluster_config}"
    trap - EXIT
    llm_nodes="$(validate_cluster_mounts)"
  fi

  echo "Waiting for Kubernetes nodes..."
  kubectl --context "${cluster_context}" \
    wait --for=condition=Ready nodes --all --timeout=2m

  # Existing clusters also receive the placement label after their immutable
  # mounts have been checked, so an upgrade does not require cluster recreation.
  while IFS= read -r node_name; do
    kubectl --context "${cluster_context}" label node "${node_name}" \
      "${llm_node_label}=true" --overwrite
  done <<<"${llm_nodes}"

  bootstrap_flux
  load_lab_ca

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
  wait_for_gateway_endpoint llm.localhost /health
  wait_for_gateway_endpoint chat.localhost /health
  configure_lan_gateway
  if [[ -n "${lan_address}" ]]; then
    wait_for_lan_endpoint "chat.${lab_domain}" /health
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
  echo "Access the chat UI at http://chat.localhost:8080"
  echo "Access the LLM API at http://llm.localhost:8080/v1"
  echo "Access Grafana at http://grafana.localhost:8080"
  echo "Access Argo CD at http://argocd.localhost:8080 (user admin; password below)"
  printf '%s\n' "kubectl --context ${cluster_context} -n argocd get secret argocd-initial-admin-secret -o go-template='{{ index .data \"password\" | base64decode }}{{ \"\\n\" }}'"
  echo "Read the generated Grafana login with:"
  printf '%s\n' "kubectl --context ${cluster_context} -n monitoring get secret kube-prometheus-stack-grafana -o go-template='user: {{ index .data \"admin-user\" | base64decode }}{{ \"\\n\" }}password: {{ index .data \"admin-password\" | base64decode }}{{ \"\\n\" }}'"
  echo
  if [[ -n "${lan_address}" ]]; then
    echo "Home network: https://chat.${lab_domain} and https://grafana.${lab_domain} via ${lan_address}."
    echo "Devices need router DNS entries for these names pointing at ${lan_address},"
    echo "and must trust the root certificate ${lab_ca_dir}/ca.crt (docs/operations.md)."
  else
    echo "Home-network access is off. To enable it, rerun with LAN_ADDRESS set to"
    echo "this host's private IPv4 address (docs/operations.md)."
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
