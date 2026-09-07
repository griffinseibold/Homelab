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

## Applications

Register an Argo CD Application pointing to the application's repository and
Helm chart path, then select a destination namespace. Argo CD handles sync,
health, history and chart overrides such as `replicaCount`.

For Gateway access, the chart must supply an `HTTPRoute` attached to
`gateway-system/homelab`, listener `http`, and label its namespace
`gateway.homelab/access: public`. Use a hostname such as
`hello-crud.localhost`. A route without hostnames is the catch-all.

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

## Validation

Run `./scripts/validate.sh` before committing. It needs Ansible, PyYAML,
kubectl, ShellCheck, kubeconform, and promtool. Host provisioning installs
all of them: `./scripts/bootstrap-host.sh` installs Ansible from apt, and
the `validation_tools` role covers the rest. The workflow in
`.github/workflows/validate.yml` records exact tool versions and download
checksums for CI, which provisions its own runner.

Validation checks scripts, strict YAML, reconciliation paths/dependencies,
Kustomize builds, native Kubernetes schemas, Ansible syntax and regression
behavior. Promtool checks alert firing/recovery and dashboard query syntax.
Unbundled custom-resource schemas are explicitly reported as skipped;
Helm charts and GPU runtime behavior still need integration checks.
No cluster credentials are required by CI, and it does not deploy changes.

For a real archive/restore round trip using synthetic SQLite data and a single
disposable Docker container (no Kind access):

```bash
RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p test_backup_docker.py -v
```

This opt-in test may download `busybox:1.37.0`; it removes its test container
and temporary data afterward.
