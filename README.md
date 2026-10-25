# Homelab

A reproducible Ubuntu development platform with Kubernetes, GitOps, monitoring,
and a GPU-backed local chat service. Ansible configures the host; Flux manages
the platform; Argo CD deploys applications from their own repositories.

```text
Ubuntu → Docker → Kind: homelab-dev (one control plane, two workers)
                    ├─ Flux + Envoy Gateway → localhost:8080, optional home-network HTTPS
                    ├─ cert-manager → *.lab.internal from a host-kept root CA
                    ├─ Prometheus, Grafana, Loki and Alloy
                    ├─ llama.cpp / Vulkan → Qwen3-8B, started on demand
                    │    └─ llm-demand controller → scales the LLM and Open WebUI
                    └─ Argo CD → independently registered applications
```

The lab runs in one of two modes. **Apps mode** (the default) runs the Argo CD
applications; the LLM starts only while a running application asks for it and
stops five minutes after the last one goes away. **LLM mode** stops every
application and runs only the LLM and the Open WebUI chat, for personal use.

The current host has a Ryzen 9 3900X, 48 GB RAM, and a Radeon RX 6900 XT
with 16 GB VRAM. This is a single-host development environment. The GPU server
uses privileged device access; the unauthenticated inference API and HTTP UIs
are exposed through a Gateway bound to host loopback. Chat and Grafana can also
be opted in to HTTPS on the home network; nothing is published to the internet.

## Start here

On Ubuntu with an available Vulkan-capable GPU and `/dev/dri/renderD*` device:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/griffinseibold/Homelab.git
cd Homelab
./scripts/bootstrap-host.sh
# Log out and back in if Docker group membership changed.
./scripts/download-models.sh
./scripts/bootstrap-dev.sh
```

The host script prompts for sudo and installs Docker, kubectl, Helm, Kind,
Flux, and the GitHub CLI. Models require several GB of download and disk space.
To put weights elsewhere, set `export MODELS_DIR=/absolute/path/to/models`
before **both** model download and dev bootstrap. The default is `~/models`.

Dev bootstrap verifies model checksums, GPU access prerequisites, and existing
worker mounts before configuring the cluster. It reuses compatible clusters
and never deletes one. It waits for the LLM demand controller rather than the
LLM itself, and checks the LLM and chat endpoints only in llm mode, because in
apps mode they are stopped until needed. `kubernetes/kind/dev.yaml` is a
template rendered by this script; do not pass it directly to `kind create cluster`.

Flux reconciles the public repository configured in
[`gotk-sync.yaml`](kubernetes/clusters/dev/flux-system/gotk-sync.yaml), without
GitHub credentials. Local edits are not deployed until committed and pushed
to that configured source. When using a fork, update the source URL first.

For an existing cluster upgrading to the worker placement labels in this
revision, run the new `bootstrap-dev.sh` before pushing the LLM manifests.
It checks the mounts and labels compatible workers so the server can schedule.

## Access

| Service | On this host | Home network | Login |
| --- | --- | --- | --- |
| Chat (llm mode only) | <http://chat.localhost:8080> | `https://chat.lab.internal` | First registered account becomes admin; sign-ups are then disabled |
| LLM API (llm mode, or while an app requests it) | <http://llm.localhost:8080/v1> | — | None |
| Grafana | <http://grafana.localhost:8080> | `https://grafana.lab.internal` | Generated credentials |
| Argo CD | <http://argocd.localhost:8080> | — | `admin`, generated password |

In apps mode the chat is stopped and the LLM API is usually off; switch with
`./scripts/lab-mode.py llm` to use them. See
[LLM on demand and LLM mode](docs/operations.md#llm-on-demand-and-llm-mode).
See [operations](docs/operations.md) for credential commands, API examples,
Prometheus access, logs, and troubleshooting. The `.localhost` addresses work
on the host itself. Home-network addresses need a cluster created with
`LAN_ADDRESS`, plus router and device setup; see
[home network access](docs/operations.md#home-network-access). The LLM API and
Argo CD stay on this host because they are unauthenticated or control the
cluster.

## Day-to-day commands

```bash
./scripts/lab-mode.py status               # Mode, LLM, who requests it, paused apps
./scripts/lab-mode.py llm --wait           # Stop apps; run only the LLM and chat
./scripts/lab-mode.py apps                 # Back to apps (the default)
./scripts/download-models.sh --check       # Verify weights without downloading
flux get kustomizations --context kind-homelab-dev
kubectl --context kind-homelab-dev get pods,pvc -A
./scripts/backup-dev.py create             # Pauses dev nodes while copying PVCs
```

Backups go to `~/homelab-backups` by default. Read the
[recovery guide](docs/recovery.md) before a cluster rebuild: ordinary PVC data
lives inside Kind nodes and is lost when the cluster is deleted. Host model
weights and host-side backups survive cluster deletion.

## Where things live

| Path | Purpose |
| --- | --- |
| `ansible/` | Ubuntu host configuration and tool installation |
| `scripts/` | Bootstrap, model download, lab mode switch, backup and recovery |
| `kubernetes/clusters/dev/` | Flux reconciliation and dependency ordering |
| `kubernetes/infrastructure/` | Gateway, cert-manager, monitoring, logging, Argo CD, LLM and chat |
| `kubernetes/kind/` | Dev template and an unused `homelab-lab` configuration |
| `tests/` | Bootstrap, recovery, LLM demand and lab mode regressions |
| `docs/` | Operations and recovery guides |

Applications stay in their own repositories; register them through Argo CD.
The reference application is
[hello-crud](https://github.com/griffinseibold/hello-crud). Application
registrations are cluster state and are included in the backup export;
private repository credentials must be restored separately. An application
that uses the LLM labels its Deployment or StatefulSet
`homelab.local/llm: requested`; see the
[application contract](docs/operations.md#applications).

## Current priorities

This revision closes bootstrap portability and incomplete-download gaps, then
adds three capabilities:

1. **Recovery:** verified host-side PVC archives, Argo registration export,
   and restoration into an empty, unused volume.
2. **LLM observability:** metrics, an inference dashboard, and alerts for an
   unavailable server or sustained queue backlog.
3. **LLM on demand:** the GPU server runs only while an application requests
   it, and an LLM mode pauses every application for personal chat.

The next operational gaps are off-host encrypted backups, an external alert
receiver, and managed secrets. Public access would also require authentication
and TLS. Keep plaintext credentials, model weights, and backups out of Git.
