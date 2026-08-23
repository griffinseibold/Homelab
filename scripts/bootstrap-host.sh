#!/usr/bin/env bash

set -Eeuo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
become_executable="$(command -v sudo 2>/dev/null || true)"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this script as your normal user, not as root." >&2
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required to bootstrap the host." >&2
  exit 1
fi

# Current Ubuntu releases may use sudo-rs as /usr/bin/sudo while providing
# classic sudo as sudo.ws. Ansible's sudo plugin expects the classic prompt
# protocol, so prefer the compatibility executable when it is installed.
if [[ -x /usr/bin/sudo.ws ]]; then
  become_executable="/usr/bin/sudo.ws"
fi

if ! command -v ansible-playbook >/dev/null 2>&1; then
  echo "Installing Git and Ansible..."
  sudo apt-get update
  sudo apt-get install -y git ansible
fi

echo "Configuring the host with Ansible..."
echo "Ansible will ask for your sudo password."
echo "Using ${become_executable} for privilege escalation."
(
  cd "${repository_root}/ansible"
  ansible-playbook \
    --ask-become-pass \
    --extra-vars "ansible_become_exe=${become_executable}" \
    playbooks/bootstrap.yml
)

echo
echo "Host bootstrap complete."

if docker info >/dev/null 2>&1; then
  echo "Docker is available in this session. Run ./scripts/bootstrap-dev.sh next."
else
  echo "Log out and back in once to activate Docker group membership."
  echo "Then return to ${repository_root} and run ./scripts/bootstrap-dev.sh."
fi
