# Homelab

A reproducible Ubuntu development platform with Kubernetes, GitOps, monitoring,
a GPU-backed local chat service, and S3 storage, experiment tracking, event
streaming and batch queueing for machine-learning work. Ansible configures the
host; Flux manages the platform; Argo CD deploys applications from their own
repositories.

```text
Ubuntu → k3s: homelab-dev (single node, the host's AMD GPU via AMD's device plugin)
                    ├─ Flux + Envoy Gateway → localhost:8080, optional home-network HTTPS
                    ├─ cert-manager → *.lab.internal from a host-kept root CA
                    ├─ Prometheus, Grafana, Loki and Alloy
                    ├─ llama.cpp / Vulkan → Qwen3-8B, started on demand
                    │    └─ llm-demand controller → scales the LLM and Open WebUI
                    ├─ Garage → in-cluster S3 (datasets, checkpoints, MLflow artifacts)
                    ├─ MLflow → experiment tracking
                    ├─ Kafka (one KRaft broker) → event streams between services
                    ├─ Kueue → queues batch Jobs for the GPU
                    └─ Argo CD → independently registered applications
```

The lab runs in one of two modes. **Apps mode** (the default) runs the Argo CD
applications; the LLM starts only while a running application asks for it and
stops five minutes after the last one goes away. **LLM mode** stops every
application and runs only the LLM and the Open WebUI chat, for personal use.

The current host has a Ryzen 9 3900X, 48 GB RAM, and a Radeon RX 6900 XT
with 16 GB VRAM. This is a single-host development environment. Pods get the
GPU by requesting `amd.com/gpu: 1`, so the LLM and GPU jobs take turns
through the scheduler; none of them run privileged. The unauthenticated
inference API and HTTP UIs are exposed through a Gateway bound to host
loopback; k3s's own API server and kubelet ports are firewalled from the
network. Chat and Grafana can also
be opted in to HTTPS on the home network; nothing is published to the internet.

## Start here

On Ubuntu with an AMD GPU on the in-kernel `amdgpu` driver (`/dev/kfd` and a
`/dev/dri/renderD*` device):

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/griffinseibold/Homelab.git
cd Homelab
./scripts/bootstrap-host.sh
./scripts/download-models.sh
./scripts/bootstrap-dev.sh
```

The host script prompts for sudo and installs a pinned k3s, kubectl, Helm,
Flux, and the GitHub CLI, and checks the GPU. It adds the `homelab-dev`
context to `~/.kube/config`. Models require several GB of download and disk space.
To put weights elsewhere, set `export MODELS_DIR=/absolute/path/to/models`
before **both** model download and dev bootstrap. The default is `~/models`.

Dev bootstrap verifies model checksums and GPU devices, installs Flux, and
records host values the manifests need (model directory, GPU groups, LAN
address) in the `flux-system/homelab-host` ConfigMap. It is safe to rerun. It
waits for the GPU to be advertised and for the LLM demand controller rather
than the LLM itself, and checks the LLM and chat endpoints only in llm mode,
because in apps mode they are stopped until needed.

**Coming from the Kind cluster?** Follow [the migration guide](docs/migrate-to-k3s.md);
it moves all data through a verified backup.

Flux reconciles the public repository configured in
[`gotk-sync.yaml`](kubernetes/clusters/dev/flux-system/gotk-sync.yaml), without
GitHub credentials. Local edits are not deployed until committed and pushed
to that configured source. When using a fork, update the source URL first.

When the S3 storage and MLflow first reach an existing cluster, rerun
`bootstrap-dev.sh` after pushing: it generates and loads Garage's secrets and
MLflow's S3 credentials. Until then those two Flux Kustomizations stay not
ready; the rest of the platform is unaffected.

## Access

| Service | On this host | Home network | Login |
| --- | --- | --- | --- |
| Chat (llm mode only) | <http://chat.localhost:8080> | `https://chat.lab.internal` | First registered account becomes admin; sign-ups are then disabled |
| LLM API (llm mode, or while an app requests it) | <http://llm.localhost:8080/v1> | — | None |
| Grafana | <http://grafana.localhost:8080> | `https://grafana.lab.internal` | Generated credentials |
| Argo CD | <http://argocd.localhost:8080> | — | `admin`, generated password |
| MLflow | <http://mlflow.localhost:8080> | — | None |

In apps mode the chat is stopped and the LLM API is usually off; switch with
`./scripts/lab-mode.py llm` to use them. See
[LLM on demand and LLM mode](docs/operations.md#llm-on-demand-and-llm-mode).
See [operations](docs/operations.md) for credential commands, API examples,
Prometheus access, logs, and troubleshooting. The `.localhost` addresses work
on the host itself. Home-network addresses need a cluster created with
`LAN_ADDRESS`, plus router and device setup; see
[home network access](docs/operations.md#home-network-access). The LLM API and
Argo CD stay on this host because they are unauthenticated or control the
cluster; so does MLflow, which has no login. Inside the cluster, applications
use S3 at `http://s3.ml-storage.svc.cluster.local:9000` and MLflow at
`http://mlflow.mlflow.svc.cluster.local:5000`; see
[ML storage and experiment tracking](docs/operations.md#ml-storage-and-experiment-tracking).
They stream events through Kafka at `kafka.kafka.svc.cluster.local:9092` and
queue GPU Jobs through Kueue's ClusterQueue `gpu`; see
[streaming and batch queueing](docs/operations.md#streaming-and-batch-queueing).

## Day-to-day commands

```bash
./scripts/lab-mode.py status               # Mode, LLM, who requests it, paused apps
./scripts/lab-mode.py llm --wait           # Stop apps; run only the LLM and chat
./scripts/lab-mode.py apps                 # Back to apps (the default)
./scripts/download-models.sh --check       # Verify weights without downloading
./scripts/ml-credentials.py crosswalk      # S3 credentials Secret for a namespace
flux get kustomizations --context homelab-dev
kubectl --context homelab-dev get pods,pvc -A
./scripts/backup-dev.py create             # Freezes the cluster while copying PVCs
```

Backups go to `~/homelab-backups` by default. Read the
[recovery guide](docs/recovery.md) before a cluster rebuild: PVC data lives in
`/var/lib/rancher/k3s/storage/` and is deleted with its claim or by
uninstalling k3s. Host model weights and host-side backups survive.

## Where things live

| Path | Purpose |
| --- | --- |
| `ansible/` | Ubuntu host configuration, k3s and GPU checks |
| `scripts/` | Bootstrap, model download, lab mode switch, S3 credentials, backup and recovery |
| `kubernetes/clusters/dev/` | Flux reconciliation and dependency ordering |
| `kubernetes/infrastructure/` | Gateway, cert-manager, monitoring, logging, Argo CD, GPU device plugin, LLM, chat, S3 storage, MLflow, Kafka and Kueue |
| `tests/` | Bootstrap, exposure, recovery, LLM demand, lab mode, S3 setup, credential, Kafka and Kueue regressions |
| `docs/` | Operations, recovery and Kind-to-k3s migration guides |

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
adds these capabilities:

1. **Recovery:** verified host-side PVC archives, Argo registration export,
   and restoration into an empty, unused volume.
2. **LLM observability:** metrics, an inference dashboard, and alerts for an
   unavailable server or sustained queue backlog.
3. **LLM on demand:** the GPU server runs only while an application requests
   it, and an LLM mode pauses every application for personal chat.
4. **ML platform:** S3-compatible object storage (Garage) and an MLflow
   tracking server for GPU training and inference projects.
5. **k3s with a real GPU resource:** a single-node k3s cluster replaces Kind,
   so AMD's device plugin schedules the GPU (`amd.com/gpu`) and GPU metrics
   come from the host.
6. **Streaming and queueing:** a single-broker Kafka for event streams and
   Kueue to queue batch Jobs for the one GPU.

The next operational gaps are off-host encrypted backups, an external alert
receiver, and managed secrets. Public access would also require authentication
and TLS. Keep plaintext credentials, model weights, and backups out of Git.
