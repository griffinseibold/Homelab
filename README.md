# Homelab

A reproducible Ubuntu development platform with Kubernetes, GitOps, monitoring,
and a GPU-backed local chat service. Ansible configures the host; Flux manages
the platform; Argo CD deploys applications from their own repositories.

```text
Ubuntu → Docker → Kind: homelab-dev (one control plane, two workers)
                    ├─ Flux + Envoy Gateway → localhost:8080
                    ├─ Prometheus, Grafana, Loki and Alloy
                    ├─ llama.cpp / Vulkan → Qwen3-8B → Open WebUI
                    └─ Argo CD → independently registered applications
```

The current host has a Ryzen 9 3900X, 48 GB RAM, and a Radeon RX 6900 XT
with 16 GB VRAM. This is a single-host development environment. The GPU server
uses privileged device access; the unauthenticated inference API and HTTP UIs
are exposed through a Gateway bound to host loopback.

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
and never deletes one. `kubernetes/kind/dev.yaml` is a template rendered by
this script; do not pass it directly to `kind create cluster`.

Flux reconciles the public repository configured in
[`gotk-sync.yaml`](kubernetes/clusters/dev/flux-system/gotk-sync.yaml), without
GitHub credentials. Local edits are not deployed until committed and pushed
to that configured source. When using a fork, update the source URL first.

For an existing cluster upgrading to the worker placement labels in this
revision, run the new `bootstrap-dev.sh` before pushing the LLM manifests.
It checks the mounts and labels compatible workers so the server can schedule.

## Access

| Service | URL | Login |
| --- | --- | --- |
| Chat | <http://chat.localhost:8080> | First registered account becomes admin |
| LLM API | <http://llm.localhost:8080/v1> | None |
| Grafana | <http://grafana.localhost:8080> | Generated credentials |
| Argo CD | <http://argocd.localhost:8080> | `admin`, generated password |

See [operations](docs/operations.md) for credential commands, API examples,
Prometheus access, logs, and troubleshooting. These `.localhost` addresses are
for the host itself; phone and remote access need separate networking and
authentication configuration.

## Day-to-day commands

```bash
./scripts/download-models.sh --check       # Verify weights without downloading
flux get kustomizations --context kind-homelab-dev
kubectl --context kind-homelab-dev get pods,pvc -A
./scripts/validate.sh                      # Same checks as CI; dependencies below
./scripts/backup-dev.py create             # Pauses dev nodes while copying PVCs
```

The [validation guide](docs/operations.md#validation) covers required tools.
Backups go to `~/homelab-backups` by default. Read the
[recovery guide](docs/recovery.md) before a cluster rebuild: ordinary PVC data
lives inside Kind nodes and is lost when the cluster is deleted. Host model
weights and host-side backups survive cluster deletion.

## Where things live

| Path | Purpose |
| --- | --- |
| `ansible/` | Ubuntu host configuration and tool installation |
| `scripts/` | Bootstrap, model download, validation, backup and recovery |
| `kubernetes/clusters/dev/` | Flux reconciliation and dependency ordering |
| `kubernetes/infrastructure/` | Gateway, monitoring, logging, Argo CD, LLM and chat |
| `kubernetes/kind/` | Dev template and an unused `homelab-lab` configuration |
| `tests/` | Bootstrap/recovery regressions and alert behavior checks |
| `docs/` | Operations, recovery, and researched business ideas |

Applications stay in their own repositories; register them through Argo CD.
The reference application is
[hello-crud](https://github.com/griffinseibold/hello-crud). Application
registrations are cluster state and are included in the backup export;
private repository credentials must be restored separately.

## Current priorities

This revision closes bootstrap portability and incomplete-download gaps, then
adds three capabilities:

1. **Automated validation:** shell checks, Ansible syntax, Kustomize builds,
   manifest checks, and behavioral tests run locally and in GitHub Actions.
2. **Recovery:** verified host-side PVC archives, Argo registration export,
   and restoration into an empty, unused volume.
3. **LLM observability:** metrics, an inference dashboard, and alerts for an
   unavailable server or sustained queue backlog.

The next operational gaps are off-host encrypted backups, an external alert
receiver, and managed secrets. Public access would also require authentication
and TLS. Keep plaintext credentials, model weights, and backups out of Git.

For commercial directions, see [business ideas worth testing](docs/business-ideas.md),
including proposed offers, current competitor prices, and paid validation steps.
