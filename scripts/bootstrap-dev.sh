#!/usr/bin/env bash

set -Eeuo pipefail
shopt -s nullglob

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cluster_name="homelab-dev"
cluster_context="kind-homelab-dev"
cluster_config="${repository_root}/kubernetes/kind/dev.yaml"
gateway_node_port="30080"
gateway_host_port="8080"

require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    echo "Run ./scripts/bootstrap-host.sh first." >&2
    exit 1
  fi
}

build_and_load_local_images() {
  local overlays=("${repository_root}"/kubernetes/applications/*/overlays/dev)
  local overlay_path
  local application_root
  local application_name
  local source_path
  local image_reference

  for overlay_path in "${overlays[@]}"; do
    application_root="$(dirname "$(dirname "${overlay_path}")")"
    application_name="$(basename "${application_root}")"
    source_path="${repository_root}/applications/${application_name}"

    if [[ ! -f "${source_path}/Dockerfile" ]]; then
      echo "Skipping ${application_name}: no local Dockerfile."
      continue
    fi

    image_reference="$(
      kubectl kustomize "${overlay_path}" \
        | awk '
            $1 == "image:" { print $2; exit }
            $1 == "-" && $2 == "image:" { print $3; exit }
          '
    )"

    if [[ -z "${image_reference}" ]]; then
      echo "Could not determine the image for ${application_name}." >&2
      exit 1
    fi

    echo "Building ${image_reference}..."
    docker build --tag "${image_reference}" "${source_path}"

    echo "Loading ${image_reference} into ${cluster_name}..."
    kind load docker-image "${image_reference}" --name "${cluster_name}"
  done
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
  )
  local application_manifests=(
    "${repository_root}"/kubernetes/clusters/dev/applications/*.yaml
  )
  local manifests=(
    "${infrastructure_manifests[@]}"
    "${application_manifests[@]}"
  )
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
  kubectl --context "${cluster_context}" \
    wait gatewayclass/homelab \
    --for=condition=Accepted --timeout=5m
  kubectl --context "${cluster_context}" --namespace gateway-system \
    wait gateway/homelab \
    --for=condition=Programmed --timeout=5m
  kubectl --context "${cluster_context}" --namespace hello-crud \
    wait httproute/hello-crud \
    --for="jsonpath={.status.parents[0].conditions[?(@.type=='Accepted')].status}=True" \
    --timeout=5m
  kubectl --context "${cluster_context}" --namespace hello-crud \
    wait httproute/hello-crud \
    --for="jsonpath={.status.parents[0].conditions[?(@.type=='ResolvedRefs')].status}=True" \
    --timeout=5m
  kubectl --context "${cluster_context}" --namespace monitoring \
    wait httproute/grafana \
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

for required_command in curl docker kind kubectl flux; do
  require_command "${required_command}"
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not available to the current user." >&2
  echo "Log out and back in after running ./scripts/bootstrap-host.sh." >&2
  exit 1
fi

if kind get clusters | grep -Fxq "${cluster_name}"; then
  echo "Kind cluster ${cluster_name} already exists."
  if ! gateway_host_port_is_mapped; then
    echo "This cluster predates the localhost Gateway port mapping."
    echo "Bootstrap will use its Kind node address without recreating the cluster."
  fi
else
  echo "Creating Kind cluster ${cluster_name}..."
  kind create cluster --config "${cluster_config}"
fi

echo "Waiting for Kubernetes nodes..."
kubectl --context "${cluster_context}" \
  wait --for=condition=Ready nodes --all --timeout=2m

build_and_load_local_images
bootstrap_flux

echo "Reconciling the latest Git revision..."
flux reconcile kustomization flux-system \
  --with-source \
  --context "${cluster_context}" \
  --timeout=10m

echo "Waiting for infrastructure and applications..."
wait_for_flux_kustomizations
wait_for_gateway_api
wait_for_gateway_endpoint localhost /healthz
wait_for_gateway_endpoint grafana.localhost /api/health

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
echo "Access hello-crud through the Gateway:"
echo "curl --noproxy '*' $(gateway_url)/healthz"
echo "Access Grafana at http://grafana.localhost:8080 (admin / prom-operator)"
