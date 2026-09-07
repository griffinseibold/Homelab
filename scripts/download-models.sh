#!/usr/bin/env bash

# Downloads the model weights the platform serves. Weights are large binary
# artifacts that live outside Git; this script is the declarative record of
# which models the environment uses and where they come from.

set -Eeuo pipefail

models_dir="${MODELS_DIR:-${HOME}/models}"
check_only=false

case "${1:-}" in
  "") ;;
  --check) check_only=true ;;
  *) echo "Usage: $0 [--check]" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
  echo "Usage: $0 [--check]" >&2
  exit 2
fi

# name  url  sha256
models=(
  "Qwen3-8B-Q4_K_M.gguf https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
)

checksum_is_valid() {
  local target="$1"
  local checksum="$2"
  local actual_checksum

  [[ -f "${target}" ]] || return 1
  actual_checksum="$(sha256sum < "${target}")"
  [[ "${actual_checksum%% *}" == "${checksum}" ]]
}

if ! "${check_only}"; then
  mkdir -p "${models_dir}"
fi

for entry in "${models[@]}"; do
  read -r file_name url checksum <<<"${entry}"
  target="${models_dir}/${file_name}"

  if checksum_is_valid "${target}" "${checksum}"; then
    echo "${file_name} is present and valid."
    continue
  fi

  if "${check_only}"; then
    echo "Model is missing or has an invalid checksum: ${target}" >&2
    printf 'Download verified weights with: MODELS_DIR=%q %q\n' \
      "${models_dir}" "${BASH_SOURCE[0]}" >&2
    exit 1
  fi

  echo "Downloading ${file_name}..."
  curl_status=0
  http_status="$(curl --location --fail --continue-at - \
    --write-out '%{http_code}' --output "${target}.partial" "${url}")" || curl_status=$?
  if (( curl_status != 0 )); then
    if [[ "${http_status}" == "416" ]] || (( curl_status == 33 )); then
      # A server can reject a range when a previous download already reached
      # EOF or when it does not support resuming. Accept a valid completed
      # partial; discard an invalid one so a fresh transfer can succeed.
      if checksum_is_valid "${target}.partial" "${checksum}"; then
        mv -f -- "${target}.partial" "${target}"
        echo "${file_name} downloaded and verified."
        continue
      fi
      rm -f -- "${target}.partial"
      echo "${file_name}: rejected resume range; invalid partial removed. Run this command again to retry." >&2
    else
      echo "${file_name}: transfer failed; partial retained for a later retry." >&2
    fi
    exit 1
  fi

  echo "Verifying ${file_name}..."
  if ! checksum_is_valid "${target}.partial" "${checksum}"; then
    # Resuming a completed corrupt partial forever cannot repair its bytes.
    # Keep interrupted transfers resumable, but discard a failed checksum so
    # the next invocation starts a fresh transfer. Preserve any final file
    # until its replacement has been fully verified.
    rm -f -- "${target}.partial"
    echo "${file_name} failed verification; corrupt partial removed. Run this command again to retry." >&2
    exit 1
  fi
  mv -f -- "${target}.partial" "${target}"
  echo "${file_name} downloaded and verified."
done

if ! "${check_only}"; then
  echo
  echo "Models present in ${models_dir}:"
  ls -lh -- "${models_dir}"
fi
