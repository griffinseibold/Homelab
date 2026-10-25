#!/usr/bin/env bash
# Merge ~/.kube/homelab-dev.yaml into ~/.kube/config, replacing any older
# homelab-dev entries (a reinstalled k3s has new certificates) and keeping
# every other cluster and the current context. Prints "changed" when it
# rewrote the file. Run as the user, not root. Managed by Ansible.

set -Eeuo pipefail

config="${HOME}/.kube/config"
cluster_file="${HOME}/.kube/homelab-dev.yaml"
merged="$(mktemp "${HOME}/.kube/config.XXXXXX")"
trap 'rm -f -- "${merged}"' EXIT

if [[ -f "${config}" ]]; then
  pruned="$(mktemp "${HOME}/.kube/config.XXXXXX")"
  trap 'rm -f -- "${merged}" "${pruned}"' EXIT
  cp -- "${config}" "${pruned}"
  for entry in context cluster user; do
    kubectl --kubeconfig "${pruned}" config "delete-${entry}" homelab-dev >/dev/null 2>&1 || true
  done
  # The first file's current-context wins, so the existing one is kept.
  KUBECONFIG="${pruned}:${cluster_file}" kubectl config view --flatten > "${merged}"
else
  KUBECONFIG="${cluster_file}" kubectl config view --flatten > "${merged}"
fi

if [[ -f "${config}" ]] && cmp -s -- "${merged}" "${config}"; then
  exit 0
fi
chmod 600 "${merged}"
mv -- "${merged}" "${config}"
echo changed
