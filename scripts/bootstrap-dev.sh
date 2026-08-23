#!/usr/bin/env bash

set -Eeuo pipefail
shopt -s nullglob

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cluster_name="homelab-dev"
cluster_context="kind-homelab-dev"
cluster_config="${repository_root}/kubernetes/kind/dev.yaml"

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
  local prompted_for_token="false"
  local github_token=""

  if flux check --context "${cluster_context}" >/dev/null 2>&1; then
    echo "Flux is already healthy; skipping bootstrap."
    return
  fi

  flux check --pre --context "${cluster_context}"

  if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    read -rsp "GitHub token: " github_token
    echo
    export GITHUB_TOKEN="${github_token}"
    prompted_for_token="true"
  fi

  flux bootstrap github \
    --owner=griffinseibold \
    --repository=Homelab \
    --branch=main \
    --path=kubernetes/clusters/dev \
    --personal \
    --context="${cluster_context}"

  if [[ "${prompted_for_token}" == "true" ]]; then
    unset GITHUB_TOKEN
  fi
}

wait_for_flux_applications() {
  local manifests=("${repository_root}"/kubernetes/clusters/dev/applications/*.yaml)
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

    kubectl --context "${cluster_context}" --namespace flux-system \
      wait "kustomization/${kustomization_name}" \
      --for=condition=Ready --timeout=5m
  done
}

for required_command in docker kind kubectl flux; do
  require_command "${required_command}"
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not available to the current user." >&2
  echo "Log out and back in after running ./scripts/bootstrap-host.sh." >&2
  exit 1
fi

if kind get clusters | grep -Fxq "${cluster_name}"; then
  echo "Kind cluster ${cluster_name} already exists."
else
  echo "Creating Kind cluster ${cluster_name}..."
  kind create cluster --config "${cluster_config}"
fi

echo "Waiting for Kubernetes nodes..."
kubectl --context "${cluster_context}" \
  wait --for=condition=Ready nodes --all --timeout=2m

build_and_load_local_images
bootstrap_flux

echo "Waiting for Flux reconciliation..."
kubectl --context "${cluster_context}" --namespace flux-system \
  wait kustomization/flux-system --for=condition=Ready --timeout=5m
wait_for_flux_applications

echo
echo "Dev environment is ready."
flux get all --all-namespaces --context "${cluster_context}"
kubectl --context "${cluster_context}" \
  get deployments,pods,services --all-namespaces
echo
if kubectl --context "${cluster_context}" --namespace hello-crud \
  get service/hello-crud >/dev/null 2>&1; then
  echo "Access hello-crud with:"
  echo "kubectl --context ${cluster_context} -n hello-crud port-forward service/hello-crud 8080:80"
fi
