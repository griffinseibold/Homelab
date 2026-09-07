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

main() {
  local existing_clusters
  local llm_nodes
  local node_name

  for required_command in curl docker kind kubectl flux python3 sha256sum; do
    require_command "${required_command}"
  done

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
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
