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
`llm`, `chat` and the other platform and Kubernetes system namespaces) are
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

[Grafana](http://grafana.localhost:8080) includes Kubernetes dashboards and the
[LLM inference dashboard](http://grafana.localhost:8080/d/homelab-llm).
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
10 Gi PVC; Grafana's database uses 1 Gi and Open WebUI uses 2 Gi. The `standard`
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

Bootstrap, recovery, LLM demand controller and lab mode regressions run with:

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
