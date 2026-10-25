# Operations

All cluster commands below explicitly target `kind-homelab-dev`.

## Logins and endpoints

Read generated credentials locally:

```bash
kubectl --context kind-homelab-dev -n argocd get secret argocd-initial-admin-secret \
  -o go-template='{{ index .data "password" | base64decode }}{{ "\n" }}'
kubectl --context kind-homelab-dev -n monitoring get secret kube-prometheus-stack-grafana \
  -o go-template='user: {{ index .data "admin-user" | base64decode }}{{ "\n" }}password: {{ index .data "admin-password" | base64decode }}{{ "\n" }}'
```

These are install-time credentials; they change on a fresh install. If the
Argo initial secret was removed after a password change, use the current login.
Open WebUI makes the first registered user its administrator.

Try the local OpenAI-compatible API. It answers only while the LLM is running,
which means in llm mode or while an application requests it (see
[below](#llm-on-demand-and-llm-mode)):

```bash
curl --noproxy '*' --fail http://llm.localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello!"}],"max_tokens":128}'
```

The server uses Qwen3-8B Q4_K_M with an 8,192-token context and at most one
replica.
It schedules on workers labeled `homelab.local/llm-capable=true`, where the
bootstrap has checked read-only model mounts and `/dev/dri` device mounts.
Both workers share the same physical GPU; they do not provide two GPUs.

## LLM on demand and LLM mode

The GPU server does not run all the time. A small controller, `llm-demand` in
the `llm` namespace, checks every 15 seconds what needs it and scales
llama-server between zero and one replica. It also starts and stops the chat.

### Two modes

| | Apps mode (default) | LLM mode |
| --- | --- | --- |
| Argo CD applications | Running | Stopped, with automated sync paused |
| LLM | Only while an application requests it | Always |
| Chat | Stopped | Running |

Switch with:

```bash
./scripts/lab-mode.py status        # What is running and why
./scripts/lab-mode.py llm --wait    # Stop apps, start the LLM and chat, wait until ready
./scripts/lab-mode.py apps          # Restore apps; chat stops, LLM stops when idle
```

`lab-mode.py llm` saves each Argo CD Application's sync policy in the
annotation `homelab.local/saved-sync-policy`, turns off automated sync, then
scales every Deployment and StatefulSet in the Applications' destination
namespaces to zero, saving each previous replica count in
`homelab.local/saved-replicas`. `lab-mode.py apps` restores the replicas first,
then the sync policies, and removes the annotations. Both are safe to rerun
after an interruption, and running `llm` twice never overwrites what was saved
the first time. Platform namespaces (`argocd`, `flux-system`, `monitoring`,
`llm`, `chat`, `ml-storage`, `mlflow`, `kafka`, `kueue-system` and the other
platform and Kubernetes system namespaces) are
never touched; an Application that deploys into one is left running with a
warning.

The mode lives in the ConfigMap `lab-mode` in namespace `llm`, key `mode`,
value `apps` or `llm`. It is cluster state, not Git, so it survives Flux syncs
but not a cluster rebuild; a missing ConfigMap means apps mode. Backups record
it as `lab_mode` in `manifest.json`. A backup taken in llm mode exports paused
Applications whose original sync policy is still in the annotation; after
importing them, run `./scripts/lab-mode.py apps` to resume them.

### The application contract

An application that needs the LLM adds this label to its Deployment or
StatefulSet (the workload's own `metadata.labels`, not the pod template):

```yaml
metadata:
  labels:
    homelab.local/llm: requested
```

It counts as a request while the workload has more than zero replicas (a
missing `replicas` field counts as one). The endpoint does not change:
`http://llama-server.llm.svc.cluster.local/v1`, with health at
`http://llama-server.llm.svc.cluster.local/health`.

- **Cold start.** The LLM may be unavailable for a couple of minutes after an
  application starts: the controller notices within 15 seconds, then the
  model loads onto the GPU. Expect refused connections, timeouts, or HTTP 503
  from `/health` (llama.cpp answers 200 once the model is ready) and retry.
- **Idle grace.** When the last request goes away the LLM keeps running for
  five minutes (`IDLE_GRACE_SECONDS` in
  [`demand/deployment.yaml`](../kubernetes/infrastructure/llm/demand/deployment.yaml))
  so a restart or rollout does not unload the model.
- **LLM mode.** Applications are scaled to zero, so they never need to handle
  it themselves.

### How it decides

The controller asks for one llama-server replica when the mode is `llm` or at
least one running workload carries the label, and zero once neither has been
true for the idle grace. It scales every Deployment and StatefulSet in `chat`
to one in llm mode and to zero in apps mode. (Open WebUI's chart creates a
StatefulSet for the app and a Deployment for its websocket Redis.) A chat
install less than three minutes old is not stopped, so its volume can bind
and Flux can finish the install. Every change is logged with its reason:

```bash
kubectl --context kind-homelab-dev -n llm logs deployment/llm-demand --tail=50
```

The controller only polls the Kubernetes API and resizes through the `scale`
subresource. It can list Deployments and StatefulSets cluster-wide, read the
`lab-mode` ConfigMap, resize llama-server, and resize workloads in `chat`;
nothing else. Flux no longer sets llama-server's replicas, and the Open WebUI
HelmRelease ignores replica drift, so neither undoes the controller. A fresh
install briefly starts llama-server at the Kubernetes default of one replica
until the controller's first loop stops it.

Its metrics (`llm_demand_mode`, `llm_demand_requests`,
`llm_demand_desired_replicas`, `llm_demand_last_success_timestamp_seconds`,
`llm_demand_errors_total`) are scraped by Prometheus. The
`LlmDemandControllerStalled` alert fires when it has not completed a loop for
15 minutes.

| Symptom | Next check |
| --- | --- |
| LLM API refuses connections or returns 503 | Normal while stopped or loading. `./scripts/lab-mode.py status` shows whether anything requests it; allow a couple of minutes after a request appears |
| LLM never starts for an application | The label is on the Deployment or StatefulSet itself, not the pod template, and its replicas are above zero; then the `llm-demand` logs |
| LLM keeps running with no requests | It stops five minutes after the last request. `status` lists requesters; check for a forgotten labeled workload or llm mode |
| Chat is not reachable | The chat runs only in llm mode: `./scripts/lab-mode.py llm --wait` |
| Applications are still stopped after `apps` | Rerun `./scripts/lab-mode.py apps`; `status` lists anything still paused |
| An application restarts during llm mode | An ApplicationSet or parent app turned its sync back on. `status` warns about it; rerun `./scripts/lab-mode.py llm` or pause the parent |
| Mode changes do nothing | `kubectl --context kind-homelab-dev -n llm get deployment llm-demand` and its logs; the controller does the LLM and chat scaling |

## ML storage and experiment tracking

The platform provides object storage and experiment tracking for machine
learning workloads, such as training jobs and inference services deployed
through Argo CD. Both are platform components managed by Flux, so llm mode
never stops them.

| | Inside the cluster | On this host |
| --- | --- | --- |
| S3 object storage (Garage) | `http://s3.ml-storage.svc.cluster.local:9000` | — |
| MLflow tracking server | `http://mlflow.mlflow.svc.cluster.local:5000` | <http://mlflow.localhost:8080> |

Buckets: `datasets`, `checkpoints` and `mlflow-artifacts`. Clients must use
**path-style addressing** and region `us-east-1` (boto3's default). MLflow
has no login, so it is not offered on the home network.

### S3 credentials for an application

Each namespace that needs S3 gets its own access key:

```bash
./scripts/ml-credentials.py crosswalk                       # datasets and checkpoints
./scripts/ml-credentials.py crosswalk --bucket datasets     # only the buckets named
```

The script creates the Garage key `k8s-crosswalk` (or reuses it), grants it
read and write on the buckets, and writes Secret `s3-credentials` in that
namespace with `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The namespace
must exist first. Rerunning it is safe: the key and its credentials stay the
same and permissions are only added. Use the Secret with `envFrom` and set
the endpoint yourself, for example in Python:

```python
import boto3
from botocore.config import Config

s3 = boto3.client("s3", endpoint_url="http://s3.ml-storage.svc.cluster.local:9000",
                  config=Config(s3={"addressing_style": "path"}))
```

The credentials live in Garage's own database and in the Secret, never in
Git. To revoke one, delete the key inside the Garage pod
(`kubectl --context kind-homelab-dev -n ml-storage exec deploy/garage -- /garage key delete --yes k8s-crosswalk`)
and the Secret.

### MLflow

Set `MLFLOW_TRACKING_URI=http://mlflow.mlflow.svc.cluster.local:5000`. The
server stores artifacts in the `mlflow-artifacts` bucket itself
(`--serve-artifacts`), so clients need no S3 credentials for MLflow
artifacts. Runs, parameters and metrics are in a SQLite database on a 2 Gi
volume; that suits one user. A PostgreSQL backend store is the upgrade path
for concurrent heavy writers. The server only accepts the Host headers it
expects (DNS rebinding protection); a client using another name for the
Service gets HTTP 403 and needs adding to `--allowed-hosts` in
[`deployment.yaml`](../kubernetes/infrastructure/mlflow/deployment.yaml).

### How it is set up

Garage (`dxflrs/garage:v2.4.1`) runs as one node with no replication, on a
50 Gi volume. Its RPC secret and admin token are generated once by
`bootstrap-dev.sh` in `~/.config/homelab/ml-storage/` (or `ML_STORAGE_DIR`)
and loaded as Secret `ml-storage/garage`; they are never in Git. They do not
protect any data at rest, so if they are lost, delete both files and rerun
bootstrap: it generates new ones, and Garage keeps its data. A `garage-setup`
Job creates any missing bucket through Garage's admin API; bootstrap then
runs `ml-credentials.py mlflow --bucket mlflow-artifacts` before it waits for
MLflow. Until bootstrap has run on a cluster, the `ml-storage` and `mlflow`
Flux Kustomizations stay not ready; nothing else depends on them.

Some admin API response shapes could not be checked against a live server
when this was written. The setup Job and `ml-credentials.py` check every
response and stop with the response text if it differs from what they
expect, rather than guessing.

| Symptom | Next check |
| --- | --- |
| `ml-storage` Kustomization not ready; Garage pod `CreateContainerConfigError` | Secret `ml-storage/garage` is missing: rerun `./scripts/bootstrap-dev.sh` |
| `garage-setup` Job failed | `kubectl --context kind-homelab-dev -n ml-storage logs job/garage-setup`. A rejected token means the Secret changed after Garage started: `kubectl ... -n ml-storage rollout restart deployment/garage`, then delete the Job so Flux recreates it |
| Setup waits for "no storage node in the layout" | `kubectl --context kind-homelab-dev -n ml-storage exec deploy/garage -- /garage status` and the Garage log |
| MLflow pod `CreateContainerConfigError` | Secret `mlflow/s3-credentials` is missing: `./scripts/ml-credentials.py mlflow --bucket mlflow-artifacts` |
| MLflow answers 403 Invalid Host header | Use `mlflow.localhost:8080` or the Service name with port 5000; see `--allowed-hosts` |
| Artifact upload fails | MLflow log; `./scripts/ml-credentials.py mlflow --bucket mlflow-artifacts` restores its key's permissions |
| S3 client gets `NoSuchBucket` or DNS errors for `BUCKET.s3...` | The client uses virtual-host addressing; switch it to path-style |

## Streaming and batch queueing

Two more platform services for applications such as GPU training and
inference projects. Flux manages both, and llm mode never stops them.

| | Inside the cluster | What it is for |
| --- | --- | --- |
| Kafka | `kafka.kafka.svc.cluster.local:9092` | Event streams between services |
| Kueue | ClusterQueue `gpu` | Queueing batch Jobs for the one GPU |

Neither is reachable from this host's browser or the home network. Kafka has
no login, so it stays inside the cluster.

### Kafka: the contract

- **Address.** Bootstrap with `kafka.kafka.svc.cluster.local:9092`, protocol
  `PLAINTEXT` (no TLS, no authentication). The broker tells clients to
  reconnect to that same name, so it only works from pods in the cluster.
- **Topics.** Automatic topic creation is **off**. An application creates
  the topics it needs when it starts, with **replication factor 1** (there is
  one broker), and treats "topic already exists" as success. Picking the
  partition count is up to the application; one is the default.
- **Retention.** Kafka keeps a partition's messages for **7 days** or until
  the partition holds **2 GiB**, whichever comes first, then deletes the
  oldest. The size limit is per partition, so a three-partition topic can
  hold up to 6 GiB. All topics share one 20 Gi volume, which keeps every
  topic's history plus Kafka's own metadata. A topic that needs less (or
  more) sets `retention.ms` or `retention.bytes` when it is created.
- **Consumer groups.** Commit offsets as usual; Kafka keeps them in its own
  internal topic. Lag per group and topic is on the Kafka dashboard.

For example, in Python with `confluent-kafka`:

```python
from confluent_kafka import KafkaError
from confluent_kafka.admin import AdminClient, NewTopic

admin = AdminClient({"bootstrap.servers": "kafka.kafka.svc.cluster.local:9092"})
for topic, future in admin.create_topics(
        [NewTopic("detections", num_partitions=1, replication_factor=1)]).items():
    try:
        future.result()
    except Exception as error:
        if error.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
            raise
```

To look inside, run Kafka's own tools in the broker pod:

```bash
kubectl --context kind-homelab-dev -n kafka exec deploy/kafka -- \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --describe
kubectl --context kind-homelab-dev -n kafka exec deploy/kafka -- \
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --all-groups
```

### Kueue: the contract

Kueue holds a batch Job back until the quota it asks for is free, then lets
it start. The ClusterQueue `gpu` has one `amd.com/gpu`, 16 CPUs and 32 Gi of
memory to hand out, so at most one admitted Job holds the GPU at a time and
the rest wait in order. An application opts in with three things:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: crosswalk
  labels:
    kueue.homelab/queues: "true"   # Kueue ignores namespaces without it
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: gpu
  namespace: crosswalk
spec:
  clusterQueue: gpu
---
apiVersion: batch/v1
kind: Job
metadata:
  name: train
  namespace: crosswalk
  labels:
    kueue.x-k8s.io/queue-name: gpu  # the LocalQueue's name
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: train
          image: example/train:1.0
          resources:
            requests: {cpu: "4", memory: 8Gi}
            limits: {amd.com/gpu: 1}
```

When Argo CD creates the namespace (`CreateNamespace=true`), set the label
with the Application's `managedNamespaceMetadata`. Kueue creates each Job
suspended, admits it when its requests fit, and records it as a `Workload`
in the same namespace. A Job counts only the resources the ClusterQueue
covers (`cpu`, `memory` and `amd.com/gpu`); anything else it requests is
not limited by Kueue.

Only **batch Jobs** go through Kueue. Deployments, StatefulSets and plain
pods never do, even in a labeled namespace, so a fault in Kueue cannot stop
ordinary workloads from starting.

### Kueue and the LLM share one GPU

Kueue only counts what it admitted. The LLM (llama-server) and inference
Deployments are not Kueue workloads, so when one of them is using the GPU,
Kueue still believes the GPU is free and admits a GPU Job. That Job's pod
then waits **Pending** in the Kubernetes scheduler until the GPU is
actually released: in apps mode, five minutes after the last application
stops requesting the LLM; in llm mode, not until you switch back to apps
mode. While it waits it keeps the queue's GPU quota, so the next GPU Job
waits behind it in Kueue. Measuring this interaction is the subject of the
Crosswalk project's Experiment C.

On this Kind cluster no node offers `amd.com/gpu` at all: the LLM reaches
the GPU through a privileged device mount instead of a device plugin. Kueue
installs and admits normally, Jobs that ask only for CPU and memory run, and
a Job asking for `amd.com/gpu` is admitted and then stays Pending. GPU Jobs
need the k3s cluster and AMD's device plugin.

### How it is set up

Kafka is the official `apache/kafka:4.3.1` image, one broker that is also
its own KRaft controller (no ZooKeeper), configured by
[`server.properties`](../kubernetes/infrastructure/kafka/server.properties)
on a 20 Gi volume. Plain manifests rather than the Strimzi operator: an
operator, its CRDs and its upgrade cycle buy nothing for a single broker.
Broker metrics come from the Prometheus JMX exporter 1.6.0 Java agent.
The project publishes the agent as a jar, not an image, so an init container
downloads it from GitHub on the first start, checks its SHA-256 against the
release's, and keeps it on Kafka's volume; later starts need no network.
Consumer group lag comes from `kafka-exporter` v1.11.0. Both are scraped by
Prometheus and drawn on the [Kafka dashboard](http://grafana.localhost:8080/d/homelab-kafka):
broker up, under-replicated and offline partitions, bytes in and out and
size on disk per topic, and lag per consumer group and topic. The
`KafkaBrokerUnavailable` alert fires after ten minutes without broker
metrics.

Kueue v0.20.3 is installed from its official chart. Its configuration
([`helm-release.yaml`](../kubernetes/infrastructure/kueue/controller/helm-release.yaml))
manages only batch Jobs in namespaces with the opt-in label; the
ResourceFlavor `amd-gpu` and ClusterQueue `gpu` are in
[`queues.yaml`](../kubernetes/infrastructure/kueue/queues/queues.yaml).
The controller's metrics (`kueue_pending_workloads`,
`kueue_admitted_active_workloads`, `kueue_admission_wait_time_seconds` and
others, per ClusterQueue) are scraped by Prometheus. Removing Kueue's
HelmRelease would also delete its CRDs and with them every LocalQueue and
Workload.

| Symptom | Next check |
| --- | --- |
| `kafka` Kustomization not ready; pod in `Init:Error` | `kubectl --context kind-homelab-dev -n kafka logs deploy/kafka -c fetch-jmx-agent`. The first start downloads the JMX agent from GitHub; a hash mismatch is refused on purpose |
| Producer fails with unknown topic or partition | Automatic topic creation is off: create the topic when the application starts |
| Topic creation fails with an invalid replication factor | There is one broker: use replication factor 1 |
| Client cannot resolve `kafka.kafka.svc.cluster.local` | It is running outside the cluster; Kafka is only for pods in the cluster |
| No lag for a consumer group | The group has not committed an offset yet, or `kafka-exporter` is restarting (it waits for the broker on a fresh install) |
| Topic history shorter than expected | Retention: 7 days or 2 GiB per partition; check the topic's own `retention.*` settings with `kafka-configs.sh --describe` |
| Job runs at once, ignoring the queue | The namespace lacks `kueue.homelab/queues: "true"`, or the Job lacks the `kueue.x-k8s.io/queue-name` label |
| Job stays suspended with no Workload | The LocalQueue named in the label exists in the Job's namespace: `kubectl --context kind-homelab-dev -n NAMESPACE get localqueues,workloads` |
| Workload waits with a quota message | Another admitted Job holds the GPU or the CPU and memory: `kubectl --context kind-homelab-dev describe clusterqueue gpu` |
| Admitted Job's pod Pending with `Insufficient amd.com/gpu` | Expected on Kind (no GPU device plugin). On k3s the LLM or an inference Deployment holds the GPU; see [above](#kueue-and-the-llm-share-one-gpu) |
| Applying a LocalQueue fails with "no matches for kind" | Kueue is not installed yet: `flux get kustomizations kueue kueue-queues --context kind-homelab-dev` |

## Home network access

Phones and laptops on your Wi-Fi can use chat, Grafana and opted-in
applications over HTTPS, for example `https://chat.lab.internal`. The LLM API
and Argo CD are never offered on the home network. Access is off unless the
cluster was created with `LAN_ADDRESS`, and it is never published to the
internet:

- Kind publishes the Gateway's `lan` listener on port 443 of one private IPv4
  address, `LAN_ADDRESS`. It is not published on `0.0.0.0` or on the host's
  public IPv6 address.
- Only namespaces labeled `gateway.homelab/lan: "true"` can attach routes to
  the listener. Other routes return 404 there, even with a forged `Host` header.
- Certificates come from your own root certificate authority. Its name
  constraints restrict it to `lab.internal` names and forbid IP addresses, so
  a leaked key cannot impersonate any other site.
- Your router's firewall remains the outer boundary. Do not add port forwards
  for this host.

### One-time setup

1. **Reserve the host's address.** In your router's DHCP settings, give this
   host a fixed address. Find its current address with `ip -4 address`.
2. **Add router DNS entries** for each name, such as `chat.lab.internal` and
   `grafana.lab.internal`, pointing to that address. Devices must use the router
   for DNS; a VPN or a custom Private DNS setting on a phone bypasses it.
3. **Apply the host configuration** with `./scripts/bootstrap-host.sh`. It sets
   `net.ipv4.ip_nonlocal_bind`, so Docker can publish the address at boot even
   if DHCP assigns it moments later. Without that setting, the control-plane
   node can fail to start after a reboot.
4. **Create the cluster with the address.** Kind fixes port mappings when a
   cluster is created, so an existing cluster must be backed up, deleted and
   restored ([recovery](recovery.md)):

   ```bash
   ./scripts/backup-dev.py create
   kind delete cluster --name homelab-dev
   LAN_ADDRESS=192.168.1.50 ./scripts/bootstrap-dev.sh   # this host's address
   ```

   Later runs reuse the address the cluster was created with, so `LAN_ADDRESS`
   can be omitted. A different address stops bootstrap without changing the
   cluster.
5. **Trust the root certificate on each device.** The first bootstrap creates
   it in `~/.config/homelab/lab-ca/` (or `LAB_CA_DIR`). Copy only `ca.crt` to
   the device, by email or a cloud drive for example. **Never copy `ca.key`.**
   - iPhone: open `ca.crt` and allow the profile download. Then go to Settings
     > General > VPN & Device Management and install it. Finally, go to Settings >
     General > About > Certificate Trust Settings and turn on full trust for
     "Homelab lab.internal CA".
   - Android: Settings > Security > Encryption & credentials > Install a
     certificate > CA certificate, then choose `ca.crt`. Menu names vary by
     manufacturer. Browsers trust it; many other apps ignore user certificates.
6. **Open the site with its scheme** the first time, for example
   `https://chat.lab.internal`. Browsers treat an unfamiliar ending such as
   `.internal` as a search unless `https://` is typed. Bookmark it afterwards.

Open WebUI lets the first account register and become administrator; later
sign-ups are disabled. An existing installation keeps the setting stored in
its database, so check Admin Panel > Settings > General and turn off
**Enable New Sign Ups** if it is still on.

### Adding an application

In the application's chart, label its namespace `gateway.homelab/lan: "true"`
and give its `HTTPRoute` a hostname such as `hello-crud.lab.internal` plus a
second parent reference to listener `lan`:

```yaml
parentRefs:
  - name: homelab
    namespace: gateway-system
    sectionName: http
  - name: homelab
    namespace: gateway-system
    sectionName: lan
```

Then add the hostname to your router's DNS. The wildcard certificate already
covers any `*.lab.internal` name. Anyone on your Wi-Fi can use an exposed
application, so prefer applications that have their own login.

### Turning it off

To withdraw one application, remove its namespace label and `lan` parent
reference. To close the listener for everything, delete the `lan` listener
from [`gateway.yaml`](../kubernetes/infrastructure/gateway-api/overlays/dev/gateway.yaml)
and push; Flux removes it within its sync interval. The port mapping itself
stays until the cluster is recreated without `LAN_ADDRESS`.

| Symptom | Next check |
| --- | --- |
| Name does not resolve on the phone | The router DNS entry; Wi-Fi rather than mobile data; no VPN or custom Private DNS |
| Connection refused or times out | `docker port homelab-dev-control-plane 30443/tcp` shows the LAN address; the host still has that address |
| Cluster down after a reboot; Docker logs `cannot assign requested address` | Run `./scripts/bootstrap-host.sh`, then `docker start homelab-dev-control-plane`. If the address changed, restore the DHCP reservation |
| Certificate warning | The device trusts `ca.crt` (iPhone also needs full trust enabled) |
| 404 on the home network only | The namespace label and `lan` parent reference; `kubectl --context kind-homelab-dev get httproutes -A` |
| `lab-ca` Flux Kustomization not ready | Rerun bootstrap so it loads the root into `cert-manager/lab-ca` |

## Applications

Register an Argo CD Application pointing to the application's repository and
Helm chart path, then select a destination namespace. Argo CD handles sync,
health, history and chart overrides such as `replicaCount`.

An application that uses the LLM labels its Deployment or StatefulSet
`homelab.local/llm: requested`, uses
`http://llama-server.llm.svc.cluster.local/v1`, and tolerates the LLM being
unavailable for a couple of minutes after it starts. The details are in
[the application contract](#the-application-contract). Keep each
application in its own namespace: llm mode stops every workload in an
Application's destination namespace.

For Gateway access, the chart must supply an `HTTPRoute` attached to
`gateway-system/homelab`, listener `http`, and label its namespace
`gateway.homelab/access: public`. Use a hostname such as
`hello-crud.localhost`. A route without hostnames is the catch-all.
To also reach it from phones on your Wi-Fi, see
[adding an application](#adding-an-application) to the home network.

Applications, ApplicationSets and AppProjects are exported by the backup
script. They can be registered manually again or imported after data recovery.
Application source and image-publishing CI remain in the application's repo.

## Metrics and logs

[Grafana](http://grafana.localhost:8080) includes Kubernetes dashboards, the
[LLM inference dashboard](http://grafana.localhost:8080/d/homelab-llm) and the
[Kafka dashboard](http://grafana.localhost:8080/d/homelab-kafka).
The LLM dashboard shows scrape health, token throughput, active and deferred
requests, and request history. Gaps while the LLM is scaled to zero are
normal. Idle throughput of zero is normal too; these are server
metrics, not GPU utilization measurements. Historical throughput claims need a
recorded benchmark with its model, prompt, concurrency and settings.

A ServiceMonitor scrapes the server every 30 seconds. Alerts fire after ten
minutes of unavailable metrics while the server is meant to be running (it
does not alert while scaled to zero), or a continuous request backlog. They are
visible in Prometheus/Alertmanager; no external notification receiver is
configured. The pinned server's [metrics reference](https://github.com/ggml-org/llama.cpp/blob/b10731/tools/server/README.md#get-metrics-prometheus-compatible-metrics-exporter)
describes what each measurement means.

Prometheus retains seven days on a 5 Gi PVC. Loki retains seven days on a
10 Gi PVC; Grafana's database uses 1 Gi and Open WebUI uses 2 Gi. Garage's
object storage uses 50 Gi, MLflow's database 2 Gi and Kafka 20 Gi. The `standard`
local-path storage class persists through pod restarts, but its data is inside
Kind nodes. [Back up before deleting the cluster](recovery.md).

In Grafana Explore, select Loki and try `{namespace="hello-crud"}` or
`{namespace="llm"} |= "error"`. Alloy collects pod logs on every node.
Prometheus discovers ServiceMonitors and PodMonitors across namespaces.
Custom PrometheusRules need `release: kube-prometheus-stack` to match its
rule selector. Unreachable Kind control-plane scrape targets are disabled.

To open Prometheus:

```bash
kubectl --context kind-homelab-dev -n monitoring port-forward \
  service/kube-prometheus-stack-prometheus 9090:9090
```

Visit <http://localhost:9090>; keep the command running while using it.

## Troubleshooting

```bash
flux get kustomizations --context kind-homelab-dev
flux get helmreleases -A --context kind-homelab-dev
kubectl --context kind-homelab-dev get pods,pvc -A
kubectl --context kind-homelab-dev get gateways,httproutes -A
kubectl --context kind-homelab-dev get events -A --sort-by=.lastTimestamp
kubectl --context kind-homelab-dev -n llm logs deployment/llama-server --tail=100
kubectl --context kind-homelab-dev -n llm logs deployment/llm-demand --tail=50
./scripts/lab-mode.py status
```

| Symptom | Next check |
| --- | --- |
| Docker permission denied | Log out/in after host bootstrap; check `docker info` |
| Missing or corrupt model | Run `download-models.sh` with the same `MODELS_DIR`, then `--check` |
| Interrupted download | Rerun; valid partials resume. A corrupt completed partial is removed, requiring a fresh retry |
| Existing mount mismatch | Use the original model directory, or back up before an intentional rebuild; mounts cannot change in place |
| LLM has no pod | Usually expected: it runs only in llm mode or while an application requests it. Check `./scripts/lab-mode.py status` |
| LLM Pending | Check worker labels, host mounts, memory, and the monitoring dependency |
| LLM startup failure | Check Vulkan render device and model permissions; inspect startup logs |
| GatewayClass not accepted | Bootstrap retries after one Envoy controller restart for the known first-install race |
| Data missing after rebuild | Restore from a verified backup; deleting Kind deletes its local PVC storage |

Older clusters may lack the loopback port mapping. Bootstrap probes their
control-plane address instead. Inspect it with:

```bash
kubectl --context kind-homelab-dev get node homelab-dev-control-plane \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}'
```

Use `http://NODE_IP:30080` with the intended hostname in the HTTP `Host` header.
For example, `curl --noproxy '*' -H 'Host: llm.localhost' http://NODE_IP:30080/health`
(the LLM answers only while it is running).
The normal `.localhost:8080` UI links require the current mapping.

## Tests

Bootstrap, recovery, LLM demand controller, lab mode, Garage setup, S3
credential, Kafka and Kueue regressions run with:

```bash
python3 -m unittest discover -s tests
```

For a real archive/restore round trip using synthetic SQLite data and a single
disposable Docker container (no Kind access):

```bash
RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p test_backup_docker.py -v
```

This opt-in test may download `busybox:1.37.0`; it removes its test container
and temporary data afterward.
