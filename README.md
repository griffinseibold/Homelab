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
│   └── Installs Docker, kubectl, Helm, Kind, and the Flux CLI
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
        └── hello-crud application
            ├── HTTPRoute attached to the shared Gateway
            └── SQLite database on a persistent volume
```

The `homelab-lab` Kind configuration exists, but that cluster is not currently
bootstrapped with Flux or included in the active workflow. Centralized
logging and local LLM serving remain roadmap items.

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
├── applications/
│   └── hello-crud/
│       ├── app.py
│       ├── tests/
│       └── Dockerfile
│
├── scripts/
│   ├── bootstrap-host.sh
│   └── bootstrap-dev.sh
│
└── kubernetes/
    ├── applications/
    │   └── hello-crud/
    │       ├── base/
    │       │   └── persistent-volume-claim.yaml
    │       └── overlays/dev/
    ├── clusters/
    │   └── dev/
    │       ├── applications/
    │       ├── infrastructure/
    │       └── flux-system/
    ├── infrastructure/
    │   ├── gateway-api/
    │   └── monitoring/
    └── kind/
        ├── dev.yaml
        └── lab.yaml
```

Only paths that currently exist are shown.

### `applications/`

Contains application source code, tests, dependency definitions, and container
build files. Each application is self-contained under its own directory.

### `ansible/`

Contains the inventory, playbook, and roles that configure the Ubuntu host and
install Docker, Kubernetes command-line tools, Helm, Kind, and Flux.

### `kubernetes/`

Contains Kind cluster definitions, Flux bootstrap resources, infrastructure,
and declarative application resources. Application manifests use a
base-and-overlay structure:

* `kubernetes/applications/<name>/base` contains reusable resources.
* `kubernetes/applications/<name>/overlays/<cluster>` contains cluster-specific
  changes such as image tags or replica counts.
* `kubernetes/clusters/<cluster>/applications` tells Flux which overlays that
  cluster should reconcile.
* `kubernetes/infrastructure` contains shared platform components and their
  cluster-specific configuration.

### `scripts/`

Contains the two entry points for recreating the current environment:

* `bootstrap-host.sh` installs Ansible when needed and configures the Ubuntu
  host.
* `bootstrap-dev.sh` creates or reuses the dev cluster, builds local application
  images, loads them into Kind, and bootstraps or verifies Flux.

## What Needs to Run

| Component | When it needs to run |
| --- | --- |
| `./scripts/bootstrap-host.sh` | Once on a new host, and again when host tooling changes |
| Docker daemon | Whenever the Kind cluster is running |
| `./scripts/bootstrap-dev.sh` | To create or verify the dev cluster and load local images |
| Flux controllers | Run automatically inside `homelab-dev` |
| Envoy Gateway | Installed and reconciled automatically by Flux |
| Shared `homelab` Gateway | Routes host port 8080 to attached application routes |
| kube-prometheus-stack | Installed and reconciled automatically by Flux |
| `kubectl port-forward` | Only while accessing the Prometheus UI from the host |
| `hello-crud-data` volume | Provisioned automatically and retained across pod restarts |

Flux polls Git and reconciles the cluster without a local process running in a
terminal. No interactive long-running process is required to reach
`hello-crud`.

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
* discovers dev application overlays with matching local Dockerfiles;
* builds each manifest's declared image and loads it into Kind;
* installs Flux from the committed manifests when it is not already healthy;
* waits until Flux, infrastructure, Gateway routes, and every declared dev
  application are ready.

Flux syncs this public repository anonymously over HTTPS, so no GitHub token
or deploy key is required to bootstrap or rebuild the cluster. Both scripts
are safe to rerun. They do not delete or recreate an existing cluster.

The Gateway's `127.0.0.1:8080` port mapping is created with the Kind node. If an
existing `homelab-dev` cluster predates that mapping, the bootstrap script keeps
the cluster and prints a temporary URL using the control-plane node address.
Recreating the cluster enables the stable localhost URL, but deleting a Kind
cluster also deletes its cluster-local persistent volumes; preserve any needed
data first.

## Updating the Application

Application source changes do not cause Flux to build a container. The current
workflow deliberately builds images locally, and each release requires a new
image tag.

After changing the Python application, update `newTag` in
`kubernetes/applications/hello-crud/overlays/dev/kustomization.yaml`, then build
and load the matching tag. For example:

```bash
docker build \
  -t ghcr.io/griffinseibold/hello-crud:0.2.1 \
  applications/hello-crud
kind load docker-image \
  ghcr.io/griffinseibold/hello-crud:0.2.1 \
  --name homelab-dev
```

Commit and push the source and manifest change. Flux will detect the commit and
roll out the new image. Reconciliation can be requested immediately instead of
waiting for the polling interval:

```bash
flux reconcile kustomization hello-crud \
  --with-source \
  --context kind-homelab-dev
```

Reach the application through its Gateway API `HTTPRoute`:

```bash
curl --noproxy '*' http://localhost:8080/healthz
curl --noproxy '*' http://localhost:8080/items
```

For a retained cluster that predates the localhost port mapping, use its Kind
node address with the fixed Gateway NodePort:

```bash
gateway_address="$(kubectl --context kind-homelab-dev \
  get node homelab-dev-control-plane \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')"
curl --noproxy '*' "http://${gateway_address}:30080/healthz"
```

Inspect the controller, shared Gateway, and application route with:

```bash
flux get kustomizations --context kind-homelab-dev
kubectl --context kind-homelab-dev get gatewayclasses
kubectl --context kind-homelab-dev get gateways,httproutes --all-namespaces
```

## Persistent Storage

`hello-crud` stores its items in a SQLite database at `/data/hello-crud.db`.
The `hello-crud-data` claim requests 1 Gi from Kind's default `standard`
local-path storage class and mounts it at `/data`.

The data survives application rollouts, pod deletion, and Kind node-container
restarts. It does not survive deleting the entire Kind cluster because the
volume resides inside a Kind node. Storage that survives complete cluster
rebuilds remains a separate requirement for model files and other important
data.

Inspect the claim and dynamically provisioned volume with:

```bash
kubectl --context kind-homelab-dev \
  -n hello-crud get persistentvolumeclaim
kubectl --context kind-homelab-dev get persistentvolume
```

## Monitoring

Flux installs the `kube-prometheus-stack` Helm chart into the `monitoring`
namespace. This provides the Prometheus Operator, a Prometheus instance with
seven days of retention on a 5 Gi persistent volume, Alertmanager,
node-exporter on every node, kube-state-metrics, and Grafana with its bundled
Kubernetes dashboards.

Grafana is reachable through the shared Gateway at
<http://grafana.localhost:8080>. Names ending in `.localhost` resolve to
`127.0.0.1` on Ubuntu, so no hosts-file entry is needed. The shared Gateway
routes by hostname: the Grafana `HTTPRoute` claims `grafana.localhost`, while
`hello-crud` remains the catch-all for other hostnames. Grafana uses the
chart's default `admin` / `prom-operator` login; replacing it belongs to the
secrets-management roadmap step, and the Gateway listens only on
`127.0.0.1`. User-created dashboards persist on a 1 Gi volume.

Prometheus discovers `ServiceMonitor` and `PodMonitor` resources in every
namespace, so future applications can expose metrics by shipping a monitor
resource alongside their manifests. The control-plane scrape targets that bind
only to localhost inside Kind nodes (controller manager, scheduler, etcd, and
kube-proxy) are disabled so the target list stays healthy.

The Prometheus UI is not routed through the shared Gateway yet. Reach it with
port forwarding:

```bash
kubectl --context kind-homelab-dev \
  -n monitoring port-forward \
  service/kube-prometheus-stack-prometheus 9090:9090
```

Then open <http://localhost:9090>.

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
* [X] Add Prometheus
* [X] Add Grafana
* [ ] Add centralized logging
* [X] Validate complete cluster rebuilds
* [ ] Validate AMD GPU acceleration
* [ ] Test `llama.cpp` with its Vulkan backend
* [ ] Test Ollama with ROCm/Vulkan
* [ ] Select a quantized 7B/8B instruct model
* [ ] Create persistent model storage
* [ ] Connect a simple client or chat UI

## Experiments

Experiments currently focus on building a reproducible Kubernetes platform and
proving that the host can run a useful local LLM.

### Platform Experiments

* GitOps reconciliation and recovery from configuration drift
* Application rollouts, rollbacks, health checks, and failure recovery
* Resource requests, limits, scheduling, and pod disruption
* Reusing application bases across cluster-specific Kustomize overlays
* Persistent storage behavior across application and cluster restarts
* Gateway API routing to services inside the cluster
* Metrics, dashboards, and centralized application logs
* Destroying and rebuilding the cluster entirely from source control

### Local LLM Experiments

* AMD GPU detection and acceleration on the Radeon RX 6900 XT
* Comparing Vulkan and ROCm compatibility on the host
* Comparing `llama.cpp` and Ollama for local inference
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
Build and load local application images
        ↓
Bootstrap Flux
        ↓
GitOps reconciles infrastructure
        ↓
Gateway routes become ready
        ↓
Environment restored
```

If rebuilding the environment requires undocumented manual configuration, that configuration should be considered technical debt and eventually moved into source control.

## Status

**Early development**

The host environment and repository structure are currently being established. Infrastructure, Kubernetes configuration, and distributed workloads will be added incrementally.
