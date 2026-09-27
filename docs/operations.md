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

Try the local OpenAI-compatible API:

```bash
curl --noproxy '*' --fail http://llm.localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello!"}],"max_tokens":128}'
```

The server uses Qwen3-8B Q4_K_M with an 8,192-token context and one replica.
It schedules on workers labeled `homelab.local/llm-capable=true`, where the
bootstrap has checked read-only model mounts and `/dev/dri` device mounts.
Both workers share the same physical GPU; they do not provide two GPUs.

## Home network access

Phones and laptops on your Wi-Fi can use chat and Grafana over HTTPS at
`https://chat.lab.internal` and `https://grafana.lab.internal`. The LLM API and
Argo CD are never offered on the home network. Access is off until you enable
it, and it is never published to the internet:

- The Gateway's `lan` listener is published on one private IPv4 address,
  `LAN_ADDRESS`, not on `0.0.0.0` or the host's public IPv6 address.
- The forwarding container drops connections that do not come from a private
  network (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
- Only namespaces labeled `gateway.homelab/lan: "true"` can attach routes to
  the listener; other routes return 404 there, even with a forged `Host` header.
- Certificates come from your own root certificate authority. Its name
  constraints restrict it to `lab.internal` names and forbid IP addresses, so
  a leaked key cannot impersonate any other site.
- Your router's firewall remains the outer boundary. Do not add port forwards
  for this host.

### One-time setup

1. **Reserve the host's address.** In your router's DHCP settings, give this
   host a fixed address. Find its current address with `ip -4 address`.
2. **Add router DNS entries** for `chat.lab.internal` and
   `grafana.lab.internal` pointing to that address. Devices must use the
   router for DNS; a VPN or a custom Private DNS setting on a phone bypasses it.
3. **Run bootstrap with the address.** It reuses the existing cluster, loads
   the root certificate authority and starts the `homelab-dev-lan-gateway`
   container:

   ```bash
   LAN_ADDRESS=192.168.1.50 ./scripts/bootstrap-dev.sh   # this host's address
   ```

   Set `LAN_ADDRESS` on every run. Running bootstrap without it turns home
   network access off.
4. **Trust the root certificate on each device.** The first bootstrap creates
   it in `~/.config/homelab/lab-ca/` (or `LAB_CA_DIR`). Copy only `ca.crt` to
   the device, by email or a cloud drive for example. **Never copy `ca.key`.**
   - iPhone: open `ca.crt` and allow the profile download. Then go to Settings
     > General > VPN & Device Management and install it. Finally, go to Settings >
     General > About > Certificate Trust Settings and turn on full trust for
     "Homelab lab.internal CA".
   - Android: Settings > Security > Encryption & credentials > Install a
     certificate > CA certificate, then choose `ca.crt`. Menu names vary by
     manufacturer. Browsers trust it; many other apps ignore user certificates.
5. **Open the site with its scheme** the first time, for example
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
covers any `*.lab.internal` name. Only expose applications that have their
own login.

### Turning it off

`docker stop homelab-dev-lan-gateway` removes home network access immediately,
and it stays off after reboots. The next bootstrap run with `LAN_ADDRESS`
starts it again. To disable it through bootstrap, run it without `LAN_ADDRESS`.

| Symptom | Next check |
| --- | --- |
| Name does not resolve on the phone | The router DNS entry; Wi-Fi rather than mobile data; no VPN or custom Private DNS |
| Connection refused or times out | `docker ps --filter name=homelab-dev-lan-gateway`; the host still has `LAN_ADDRESS` |
| Certificate warning | The device trusts `ca.crt` (iPhone also needs full trust enabled) |
| 404 on the home network only | The namespace label and `lan` parent reference; `kubectl --context kind-homelab-dev get httproutes -A` |
| `lab-ca` Flux Kustomization not ready | Rerun bootstrap so it loads the root into `cert-manager/lab-ca` |

## Applications

Register an Argo CD Application pointing to the application's repository and
Helm chart path, then select a destination namespace. Argo CD handles sync,
health, history and chart overrides such as `replicaCount`.

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
requests, and request history. Idle throughput of zero is normal; these are server
metrics, not GPU utilization measurements. Historical throughput claims need a
recorded benchmark with its model, prompt, concurrency and settings.

A ServiceMonitor scrapes the server every 30 seconds. Alerts fire after ten
minutes of unavailable metrics or a continuous request backlog. They are
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
```

| Symptom | Next check |
| --- | --- |
| Docker permission denied | Log out/in after host bootstrap; check `docker info` |
| Missing or corrupt model | Run `download-models.sh` with the same `MODELS_DIR`, then `--check` |
| Interrupted download | Rerun; valid partials resume. A corrupt completed partial is removed, requiring a fresh retry |
| Existing mount mismatch | Use the original model directory, or back up before an intentional rebuild; mounts cannot change in place |
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
For example, `curl --noproxy '*' -H 'Host: llm.localhost' http://NODE_IP:30080/health`.
The normal `.localhost:8080` UI links require the current mapping.

## Tests

Bootstrap and recovery regressions run with:

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
