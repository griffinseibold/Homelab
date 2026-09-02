# Homelab

A reproducible Linux and Kubernetes homelab built to practice infrastructure engineering, container orchestration, GitOps, observability, and distributed systems.

The goal of this project is to keep as much of the environment as possible **declarative and source controlled**. Rather than manually configuring the host and Kubernetes workloads, the setup should be reproducible from this repository.

The long-term target is simple:

> Install Ubuntu, clone this repository, run the bootstrap process, and recreate the environment.

## Long-Term Learning Goals

Beyond the currently implemented platform, this homelab may eventually provide
hands-on experience with:

* Linux administration
* Ansible configuration management
* Kubernetes
* Multi-node and multi-cluster environments
* Docker and container runtimes
* Helm
* GitOps
* Infrastructure as Code
* Prometheus and Grafana
* Logging and observability
* Kafka and event-driven systems
* Apache Flink and stream processing
* Networking and ingress
* Secrets management
* Failure testing and recovery
* CI/CD

The environment will evolve over time as new infrastructure and distributed-systems concepts are added.

## Hardware

Current host:

* AMD Ryzen 9 3900X
* 48 GB DDR4 RAM
* AMD Radeon RX 6900 XT
* Dedicated Ubuntu SSD
* Additional local SSD storage

The machine is intended to run multiple Kubernetes nodes and potentially multiple Kubernetes clusters without requiring a large number of traditional virtual machines.

## Current Architecture

The repository currently builds and manages this environment:

```text
Ubuntu Host
│
├── Ansible
│   └── Installs Docker, kubectl, Helm, Kind, Flux, and the GitHub CLI
│
└── Docker
    └── Kind cluster: homelab-dev
        ├── Control plane
        ├── Two worker nodes
        ├── Flux controllers
        │   └── Reconcile kubernetes/clusters/dev from Git
        ├── Envoy Gateway controller and data plane
        │   └── Shared HTTP Gateway exposed on localhost:8080
        ├── kube-prometheus-stack
        │   ├── Prometheus metrics with 7-day persistent retention
        │   └── Grafana dashboards on grafana.localhost:8080
        ├── Loki and Alloy
        │   └── Centralized pod logs with 7-day persistent retention
        ├── llama.cpp server on llm.localhost:8080
        │   └── Serves Qwen3-8B on the host GPU via Vulkan
        ├── Open WebUI on chat.localhost:8080
        │   └── Chat interface backed by the local LLM API
        ├── Argo CD on argocd.localhost:8080
        │   └── Deploys applications registered from their own repositories
        └── Applications (Argo-managed, not defined in this repository)
            └── hello-crud from github.com/griffinseibold/hello-crud
```

The `homelab-lab` Kind configuration exists, but that cluster is not currently
bootstrapped with Flux or included in the active workflow.

## Access

The web UIs are served through the shared Gateway on `127.0.0.1:8080`, except
Prometheus, which is reached with a port-forward. Names ending in
`.localhost` resolve to `127.0.0.1` on Ubuntu, so no hosts-file entries are
needed.

| Service | URL | Login |
| --- | --- | --- |
| Argo CD | <http://argocd.localhost:8080> | `admin`; password from the command below |
| Open WebUI chat | <http://chat.localhost:8080> | First account created becomes the admin |
| LLM API | <http://llm.localhost:8080/v1> (OpenAI-compatible) | None |
| Grafana | <http://grafana.localhost:8080> | Generated credentials; command in [Monitoring](#monitoring) |
| Registered applications | Their claimed hostname, or <http://localhost:8080> for the catch-all route | Application-specific |
| Prometheus | <http://localhost:9090> while port forwarding; command in [Monitoring](#monitoring) | None |

```bash
kubectl --context kind-homelab-dev -n argocd \
  get secret argocd-initial-admin-secret \
  -o go-template='{{ index .data "password" | base64decode }}
'
```

## Repository Structure

```text
homelab/
├── README.md
├── .gitignore
│
├── ansible/
│   ├── ansible.cfg
│   ├── inventory.ini
│   ├── playbooks/
│   └── roles/
│
├── scripts/
│   ├── bootstrap-host.sh
│   ├── bootstrap-dev.sh
│   └── download-models.sh
│
└── kubernetes/
    ├── clusters/
    │   └── dev/
    │       ├── infrastructure/
    │       └── flux-system/
    ├── infrastructure/
    │   ├── argocd/
    │   ├── chat/
    │   ├── gateway-api/
    │   ├── llm/
    │   ├── logging/
    │   └── monitoring/
    └── kind/
        ├── dev.yaml
        └── lab.yaml
```

Only paths that currently exist are shown.

### `ansible/`

Contains the inventory, playbook, and roles that configure the Ubuntu host and
install Docker, Kubernetes command-line tools, Helm, Kind, Flux, and the
GitHub CLI.

### `kubernetes/`

Contains Kind cluster definitions, Flux bootstrap resources, and the platform
infrastructure:

* `kubernetes/infrastructure` contains shared platform components and their
  cluster-specific configuration.
* `kubernetes/clusters/<cluster>` tells Flux what that cluster should
  reconcile.

Applications are deliberately absent: they live in their own repositories and
are registered through Argo CD at runtime.

### `scripts/`

Contains the entry points for recreating the current environment:

* `bootstrap-host.sh` installs Ansible when needed and configures the Ubuntu
  host.
* `bootstrap-dev.sh` creates or reuses the dev cluster and installs or
  verifies Flux and the platform.
* `download-models.sh` fetches and checksum-verifies the model weights the
  platform serves, into `~/models` (override with `MODELS_DIR`). Weights are
  multi-gigabyte artifacts that stay out of Git; the script is the
  declarative record of which models the environment uses. It skips files
  that are already present and valid, and resumes interrupted downloads.

## What Needs to Run

| Component | When it needs to run |
| --- | --- |
| `./scripts/bootstrap-host.sh` | Once on a new host, and again when host tooling changes |
| Docker daemon | Whenever the Kind cluster is running |
| `./scripts/bootstrap-dev.sh` | To create or verify the dev cluster and platform |
| Flux controllers | Run automatically inside `homelab-dev` |
| Envoy Gateway | Installed and reconciled automatically by Flux |
| Shared `homelab` Gateway | Routes host port 8080 to attached application routes |
| kube-prometheus-stack | Installed and reconciled automatically by Flux |
| Loki and Alloy | Installed and reconciled automatically by Flux |
| Argo CD | Installed by Flux; deploys registered applications |
| llama.cpp server and Open WebUI | Installed and reconciled automatically by Flux |
| `./scripts/download-models.sh` | Once per model; weights persist on the host across rebuilds |
| `kubectl port-forward` | Only while accessing the Prometheus UI from the host |

Flux polls Git and reconciles the platform without a local process running in
a terminal, and Argo CD does the same for registered applications.

## Bootstrap a Fresh Dev Environment

Bootstrap is split into two idempotent stages because new Docker group
membership normally requires one login refresh between them.

First, install Git if necessary and clone the repository:

```bash
sudo apt update && sudo apt install -y git
git clone git@github.com:griffinseibold/Homelab.git
cd Homelab
```

Configure the Ubuntu host:

```bash
./scripts/bootstrap-host.sh
```

Ansible prompts for the local user's sudo password. The password is used only
for that playbook run and is not stored by the script or in the repository. On
Ubuntu systems where `/usr/bin/sudo` is `sudo-rs`, the script automatically uses
the compatible classic-sudo executable at `/usr/bin/sudo.ws` for Ansible.

Log out and back in if the script asks you to activate Docker group membership.
Then create or verify the complete dev environment:

```bash
./scripts/bootstrap-dev.sh
```

The dev script:

* reuses `homelab-dev` if it already exists;
* installs Flux from the committed manifests when it is not already healthy;
* waits until Flux, infrastructure, and Gateway routes are ready.

Flux syncs this public repository anonymously over HTTPS, so no GitHub token
or deploy key is required to bootstrap or rebuild the cluster. Both scripts
are safe to rerun. They do not delete or recreate an existing cluster.

The Gateway's `127.0.0.1:8080` port mapping is created with the Kind node, so
it exists on any cluster the current configuration creates. For an older
cluster without the mapping, the script falls back to the control-plane node
address instead of recreating the cluster, because deleting a Kind cluster
also deletes its persistent volumes.

## Applications

Applications are not defined in this repository. Each application lives in its
own repository containing its source, tests, Dockerfile, a Helm chart, and a
CI workflow that publishes a public container image to GHCR on every version
tag. `hello-crud` is the reference example:
<https://github.com/griffinseibold/hello-crud>.

Argo CD deploys applications. To register one, open the Argo CD UI (see
[Access](#access)), create an Application pointing at the application's
repository and chart path, and pick a destination namespace. Argo CD then
syncs the chart continuously, shows health and history, and allows scaling by
overriding the chart's `replicaCount` value from the UI.

For an application to be reachable through the shared Gateway, its chart must
provide an `HTTPRoute` attached to the `homelab` Gateway and label its
namespace with `gateway.homelab/access: public`. Routes claim a hostname such
as `hello-crud.localhost`, or attach with no hostname to act as the catch-all.

Applications registered through the UI are cluster state, not Git state: after
a full cluster rebuild they must be registered again. The platform accepts
this trade-off so that this repository never has to know which applications
exist.

Inspect the platform and every attached route with:

```bash
flux get kustomizations --context kind-homelab-dev
kubectl --context kind-homelab-dev get gatewayclasses
kubectl --context kind-homelab-dev get gateways,httproutes --all-namespaces
```

## Persistent Storage

Workloads claim storage from Kind's default `standard` local-path storage
class. Prometheus metrics, Grafana dashboards, and application data such as
hello-crud's SQLite database all persist this way.

The data survives rollouts, pod deletion, and Kind node-container restarts. It
does not survive deleting the entire Kind cluster because the volumes reside
inside Kind nodes. Storage that survives complete cluster rebuilds remains a
separate requirement for model files and other important data.

Inspect the claims and dynamically provisioned volumes with:

```bash
kubectl --context kind-homelab-dev \
  get persistentvolumeclaims --all-namespaces
kubectl --context kind-homelab-dev get persistentvolume
```

## Monitoring

Flux installs the `kube-prometheus-stack` Helm chart into the `monitoring`
namespace. This provides the Prometheus Operator, a Prometheus instance with
seven days of retention on a 5 Gi persistent volume, Alertmanager,
node-exporter on every node, kube-state-metrics, and Grafana with its bundled
Kubernetes dashboards.

Grafana is reachable through the shared Gateway at
<http://grafana.localhost:8080>. The Gateway routes by hostname: the Grafana
`HTTPRoute` claims `grafana.localhost`, and an application route with no
hostname acts as the catch-all. User-created dashboards persist on a 1 Gi
volume.

The chart generates the Grafana admin credentials into a cluster Secret, so
they change on every fresh install. Read them with:

```bash
kubectl --context kind-homelab-dev -n monitoring \
  get secret kube-prometheus-stack-grafana \
  -o go-template='user: {{ index .data "admin-user" | base64decode }}
password: {{ index .data "admin-password" | base64decode }}
'
```

Managing this credential declaratively belongs to the secrets-management
roadmap step. Until then the exposure is limited: the Gateway listens only on
`127.0.0.1`.

Prometheus discovers `ServiceMonitor` and `PodMonitor` resources in every
namespace, so future applications can expose metrics by shipping a monitor
resource alongside their manifests. The control-plane scrape targets that bind
only to localhost inside Kind nodes (controller manager, scheduler, etcd, and
kube-proxy) are disabled so the target list stays healthy.

Loki and an Alloy DaemonSet provide centralized logging in the same
namespace. Alloy tails every container on its node through the kubelet API and
pushes the lines to Loki, which keeps seven days of logs on a 10 Gi persistent
volume. Loki is preconfigured as a Grafana datasource: open Grafana's Explore
view, pick Loki, and query with LogQL, for example
`{namespace="hello-crud"}` or `{app="hello-crud"} |= "error"`. Logs survive
pod restarts and rescheduling, so crash investigations no longer depend on
`kubectl logs` reaching the right pod in time.

The Prometheus UI is not routed through the shared Gateway yet. Reach it with
port forwarding:

```bash
kubectl --context kind-homelab-dev \
  -n monitoring port-forward \
  service/kube-prometheus-stack-prometheus 9090:9090
```

Then open <http://localhost:9090>.

## LLM Serving

Flux runs a `llama.cpp` server in the `llm` namespace, serving Qwen3-8B
(Q4_K_M) fully offloaded to the host's RX 6900 XT through the Vulkan backend
(about 1,200 t/s prompt processing and 85 t/s generation). The server exposes
an OpenAI-compatible API through the shared Gateway at
`http://llm.localhost:8080/v1`, so any application or client library that
speaks the OpenAI protocol can use the local model.

Model weights live on the host in `~/models` (fetched and verified by
`./scripts/download-models.sh`) and are mounted read-only into the Kind
workers, so they survive complete cluster rebuilds. The GPU reaches the pod
through a privileged `/dev/dri` hostPath mount because Kind has no GPU device
plugin; a real multi-node cluster would use the AMD device plugin instead.
One GPU means one server replica; additional models would run as additional
deployments, loaded and unloaded by scaling their replicas.

Open WebUI in the `chat` namespace provides the chat interface at
<http://chat.localhost:8080>, backed by the same API. It is a progressive web
app: on a phone, "Add to Home Screen" installs it like a native app. The
first account registered becomes the administrator, and conversation history
persists on a 2 Gi volume.

Try the API directly:

```bash
curl --noproxy '*' http://llm.localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}'
```

## Infrastructure Philosophy

### Everything Possible Should Be Code

Manual changes should be minimized.

If a configuration can reasonably be represented in:

* Ansible
* Kubernetes YAML
* Helm values
* Kustomize
* GitOps configuration

it should be committed to this repository.

The host and clusters should be treated as replaceable infrastructure rather than environments that must be manually preserved.

### Git Is the Source of Truth

Git should describe the desired state of the environment.

A normal infrastructure change should eventually look like:

```text
Change configuration
        ↓
Commit
        ↓
Push
        ↓
GitOps reconciliation
        ↓
Cluster reaches desired state
```

Direct manual modification of cluster resources should primarily be used for troubleshooting and experimentation.

Any permanent change should eventually be represented in Git.

### Applications Are the Deliberate Exception

The platform — everything in this repository — follows the rule above
strictly. Applications do not: they are registered through the Argo CD UI at
runtime, and which applications run on the cluster is cluster state rather
than platform Git state. Each application is still GitOps-managed, but
against its own repository: Argo CD continuously syncs every registered
application from the chart in that application's repository. The platform
trades rebuild-time convenience (re-registering applications after a rebuild)
for a platform that never has to know which applications exist.

## Secrets

Plaintext secrets must **not** be committed to this repository.

Examples include:

* Passwords
* API keys
* SSH private keys
* Tokens
* Certificates containing private keys
* Kubernetes credentials
* Cloud credentials

Sensitive files should be ignored through `.gitignore`.

Long-term secrets management may use encrypted Git-managed secrets or an external secret-management solution.

## Roadmap

* [X] Configure Ubuntu host
* [X] Create Ansible bootstrap playbook
* [X] Install container runtime
* [X] Install `kubectl`
* [X] Install Helm
* [X] Create first Kubernetes cluster
* [X] Create multi-node cluster
* [X] Create second Kubernetes cluster
* [X] Add GitOps
* [X] Deploy a sample application
* [X] Add persistent application storage
* [X] Add Gateway API
* [X] Validate complete cluster rebuilds
* [X] Add Prometheus
* [X] Add Grafana
* [X] Add Argo CD for application delivery
* [X] Move applications into their own repositories with image-publishing CI
* [X] Add centralized logging
* [X] Validate AMD GPU acceleration
* [X] Test `llama.cpp` with its Vulkan backend
* [X] Select a quantized 7B/8B instruct model (Qwen3-8B Q4_K_M)
* [X] Create persistent model storage
* [X] Connect a simple client or chat UI (Open WebUI)

## Experiments

Experiments currently focus on building a reproducible Kubernetes platform and
proving that the host can run a useful local LLM.

### Platform Experiments

* GitOps reconciliation and recovery from configuration drift
* Application rollouts, rollbacks, health checks, and failure recovery
* Resource requests, limits, scheduling, and pod disruption
* Registering, scaling, and rolling back applications through Argo CD
* Persistent storage behavior across application and cluster restarts
* Gateway API routing to services inside the cluster
* Metrics, dashboards, and centralized application logs
* Destroying and rebuilding the cluster entirely from source control

### Local LLM Experiments

* AMD GPU detection and acceleration on the Radeon RX 6900 XT
* Comparing Vulkan and ROCm compatibility on the host
* Running quantized 7B and 8B instruct models within 16 GB of VRAM
* Measuring model load time, token throughput, memory use, and VRAM use
* Comparing CPU-only, GPU-accelerated, and hybrid inference
* Persisting model files independently from application containers
* Exposing a local inference API to Kubernetes applications
* Connecting a simple client or chat interface to the selected runtime

Kafka, Flink, distributed training, RAG, and fine-tuning are not current project
commitments and may be revisited after the local inference platform is stable.

Where useful, results and design decisions will be committed alongside the
configuration or application they describe.

## Reproducibility

A major success criterion for this project is the ability to destroy and rebuild the environment.

The ideal test is:

```text
Fresh Ubuntu installation
        ↓
Clone repository
        ↓
Run Ansible host bootstrap
        ↓
Create the dev Kind cluster
        ↓
Install Flux
        ↓
GitOps reconciles the platform
        ↓
Gateway routes become ready
        ↓
Register applications in Argo CD
        ↓
Environment restored
```

If rebuilding the environment requires undocumented manual configuration, that configuration should be considered technical debt and eventually moved into source control.

## Status

**Working platform, actively evolving**

The host bootstrap, GitOps-managed platform (Gateway, monitoring, Argo CD),
and the application-delivery workflow are in place, and a complete
destroy-and-rebuild of the cluster has been validated from this repository.
Current work is tracked in the [Roadmap](#roadmap); centralized logging and
the local LLM platform are next.
