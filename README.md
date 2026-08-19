# Homelab

A reproducible Linux and Kubernetes homelab built to practice infrastructure engineering, container orchestration, GitOps, observability, and distributed systems.

The goal of this project is to keep as much of the environment as possible **declarative and source controlled**. Rather than manually configuring the host and Kubernetes workloads, the setup should be reproducible from this repository.

The long-term target is simple:

> Install Ubuntu, clone this repository, run the bootstrap process, and recreate the environment.

## Goals

This homelab is intended to provide hands-on experience with:

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

## Architecture

The planned environment is roughly:

```text
Ubuntu Host
│
├── Ansible
│   └── Configures the host and installs required tooling
│
├── Container Runtime
│
├── Kubernetes
│   ├── Cluster: dev
│   │   ├── Control Plane
│   │   └── Worker Nodes
│   │
│   └── Cluster: lab
│       ├── Control Plane
│       └── Worker Nodes
│
└── GitOps
    └── Reconciles cluster state from this repository
        ├── Applications
        ├── Monitoring
        ├── Networking
        ├── Kafka
        └── Other infrastructure
```

Cluster topology and tooling may change as the project develops.

## Repository Structure

```text
homelab/
├── README.md
├── .gitignore
│
├── ansible/
│   ├── inventory/
│   ├── playbooks/
│   └── roles/
│
├── kubernetes/
│   ├── clusters/
│   ├── infrastructure/
│   └── applications/
│
├── helm/
│
├── scripts/
│
└── docs/
```

### `ansible/`

Contains host configuration.

Ansible will be responsible for tasks such as:

* Installing required packages
* Installing container tooling
* Installing Kubernetes tooling
* Installing Helm
* Installing GitOps tooling
* Configuring Linux settings
* Configuring storage and directories
* Performing repeatable host configuration

### `kubernetes/`

Contains declarative Kubernetes resources.

Examples include:

* Namespaces
* Deployments
* Services
* ConfigMaps
* Ingress resources
* Network policies
* Persistent storage
* Monitoring infrastructure
* Distributed-system workloads

### `helm/`

Contains Helm configuration and values files for applications installed through Helm.

### `scripts/`

Contains small bootstrap and utility scripts where appropriate.

Scripts should be kept minimal. Repeatable configuration should generally live in Ansible or Kubernetes manifests rather than large shell scripts.

### `docs/`

Contains architecture notes, design decisions, experiments, and lessons learned.

## Bootstrap

The eventual bootstrap workflow should look roughly like:

```bash
git clone git@github.com:<username>/homelab.git
cd homelab
```

Install the minimal dependencies required to run Ansible:

```bash
sudo apt update
sudo apt install -y git ansible
```

Run the host configuration:

```bash
ansible-playbook ansible/playbooks/bootstrap.yml
```

Create the Kubernetes environment:

```bash
./scripts/create-cluster.sh
```

Once GitOps is configured, the cluster should reconcile the remaining infrastructure directly from this repository.

> Bootstrap commands will change as the project evolves.

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

## Initial Roadmap

* [ ] Configure Ubuntu host
* [ ] Create Ansible bootstrap playbook
* [ ] Install container runtime
* [ ] Install `kubectl`
* [ ] Install Helm
* [ ] Create first Kubernetes cluster
* [ ] Create multi-node cluster
* [ ] Create second Kubernetes cluster
* [ ] Add GitOps
* [ ] Add ingress
* [ ] Add persistent storage
* [ ] Add Prometheus
* [ ] Add Grafana
* [ ] Add centralized logging
* [ ] Deploy Kafka
* [ ] Deploy Apache Flink
* [ ] Deploy sample applications
* [ ] Add CI validation for infrastructure changes
* [ ] Add automated cluster rebuild process
* [ ] Document architecture and design decisions
* [ ] Test failure and recovery scenarios

## Experiments

This environment will also be used for targeted experiments such as:

* Kubernetes pod scheduling
* Node affinity and anti-affinity
* Taints and tolerations
* Resource requests and limits
* Pod disruption
* Horizontal scaling
* Rolling deployments
* Cluster networking
* Service discovery
* Persistent volume behavior
* Kafka partitioning
* Consumer groups
* Kafka failure recovery
* Flink checkpointing
* Stream-processing failure recovery
* Observability and alerting
* Multi-cluster deployment strategies

Where useful, results and design decisions will be documented under `docs/`.

## Reproducibility

A major success criterion for this project is the ability to destroy and rebuild the environment.

The ideal test is:

```text
Fresh Ubuntu installation
        ↓
Clone repository
        ↓
Run bootstrap
        ↓
Create clusters
        ↓
GitOps reconciles infrastructure
        ↓
Environment restored
```

If rebuilding the environment requires undocumented manual configuration, that configuration should be considered technical debt and eventually moved into source control.

## Status

**Early development**

The host environment and repository structure are currently being established. Infrastructure, Kubernetes configuration, and distributed workloads will be added incrementally.
