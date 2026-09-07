#!/usr/bin/env bash

set -Eeuo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

for required_command in python3 git shellcheck kubectl kubeconform ansible-playbook; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Missing validation dependency: ${required_command}" >&2
    echo "See the validation setup in README.md and .github/workflows/validate.yml." >&2
    exit 1
  fi
done
if ! command -v "${PROMTOOL:-promtool}" >/dev/null 2>&1; then
  echo "Install promtool or set PROMTOOL to its executable path." >&2
  exit 1
fi

echo "Checking shell scripts..."
while IFS= read -r -d '' script; do
  [[ "${script}" == *.sh && -f "${script}" ]] || continue
  bash -n "${script}"
  shellcheck "${script}"
done < <(git ls-files --cached --others --exclude-standard -z)

echo "Running Python regression tests..."
python3 -m unittest discover -s tests -v

echo "Checking Ansible syntax..."
(
  cd ansible
  ansible-playbook --syntax-check playbooks/bootstrap.yml
)

echo "Checking manifests..."
python3 scripts/validate-manifests.py

echo "Checking Prometheus rules and dashboard queries..."
python3 scripts/test-alerts.py

echo "Validation passed. No cluster access or changes were required."
