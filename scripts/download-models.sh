#!/usr/bin/env bash

# Downloads the model weights the platform serves. Weights are large binary
# artifacts that live outside Git; this script is the declarative record of
# which models the environment uses and where they come from.

set -Eeuo pipefail

models_dir="${MODELS_DIR:-${HOME}/models}"

# name  url  sha256
models=(
  "Qwen3-8B-Q4_K_M.gguf https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
)

mkdir -p "${models_dir}"

for entry in "${models[@]}"; do
  read -r file_name url checksum <<<"${entry}"
  target="${models_dir}/${file_name}"

  if [[ -f "${target}" ]]; then
    echo "${file_name} already exists; verifying checksum..."
    if echo "${checksum}  ${target}" | sha256sum --check --quiet; then
      echo "${file_name} is present and valid."
      continue
    fi
    echo "${file_name} failed verification; re-downloading." >&2
    rm -f "${target}"
  fi

  echo "Downloading ${file_name}..."
  curl --location --fail --continue-at - \
    --output "${target}.partial" "${url}"

  echo "Verifying ${file_name}..."
  echo "${checksum}  ${target}.partial" | sha256sum --check --quiet
  mv "${target}.partial" "${target}"
  echo "${file_name} downloaded and verified."
done

echo
echo "Models present in ${models_dir}:"
ls -lh "${models_dir}"
