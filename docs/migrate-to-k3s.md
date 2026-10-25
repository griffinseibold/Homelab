# Migrating from Kind to k3s

This is a one-time cutover. It replaces the Kind cluster `homelab-dev` with
a single-node k3s cluster that has the same name, so AMD's device plugin can
give pods the real GPU. Data moves through the verified host-side backup.
Every step can be undone until the Kind cluster is deleted in
[step 10](#10-delete-the-kind-cluster), and even then a rollback can rebuild
Kind from the same backup.

Plan for an hour or two plus copying time. Applications, the LLM, the chat
and Grafana are down from step 3 until step 8.

## What changes and what does not

| | Kind (before) | k3s (after) |
| --- | --- | --- |
| kubectl context | `kind-homelab-dev` | `homelab-dev` |
| Nodes | three Docker containers | this host |
| Volumes | inside the node containers | `/var/lib/rancher/k3s/storage/` on the host |
| Storage class | `standard` | `local-path` (default); `standard` kept as an alias |
| GPU for the LLM | privileged pod with `/dev/dri` mounted | `amd.com/gpu: 1` from AMD's device plugin |
| Gateway on the host | Kind port mappings | Envoy hostPorts bound to `127.0.0.1:8080` and `LAN_ADDRESS:443` |
| Host software | Docker, Kind | k3s (Docker stays installed; nothing uses it) |

URLs, credentials stored in the cluster, the lab CA, `LAN_ADDRESS`, the
model weights in `MODELS_DIR`, and Flux's Git source do not change.
Application charts that name the `standard` storage class keep working.

## Before you start

- Run everything as your normal user from the repository root. Have sudo.
- Stay on the **current** checkout (the revision running Kind) for steps 1–3.
  The k3s revision lives on the `k3s` branch until step 4. Do not merge it
  into `main` before Kind is stopped: Kind's Flux syncs `main` and would
  try to run the k3s manifests on Kind.
- Free disk space for one copy of all volumes. Garage can hold up to 50 GiB,
  so check its use first:
  `kubectl --context kind-homelab-dev get pvc -A`
  and `docker exec homelab-dev-worker du -sh /var/local-path-provisioner`
  (repeat for each node).
- Write down your `LAN_ADDRESS` if you use home-network access:
  `docker port homelab-dev-control-plane 30443/tcp` prints it.
- Note which mode the lab is in: `./scripts/lab-mode.py status`.
- Note your Argo CD applications and any private repository credentials
  in Argo CD (Settings > Repositories). Credentials are not in the backup;
  you re-enter them in step 9.
- If any application uses S3, note its namespace; you rerun
  `ml-credentials.py` for it in step 9.

## 1. Switch to apps mode

So the backup holds Applications with their normal sync policies:

```bash
./scripts/lab-mode.py apps
```

## 2. Back up on Kind

```bash
./scripts/backup-dev.py create --output ~/homelab-backups/before-k3s
./scripts/backup-dev.py verify ~/homelab-backups/before-k3s
```

Both must succeed. Keep this directory until the migration has been
verified for a while; it is also your rollback. Also copy the host-kept
secrets somewhere private if you have not already:
`~/.config/homelab/lab-ca/` and `~/.config/homelab/ml-storage/`.

List what the backup holds; you restore these in step 7:

```bash
python3 -c 'import json,sys; [print(v["namespace"]+"/"+v["claim"], v["capacity"]) for v in json.load(open(sys.argv[1]))["volumes"]]' \
  ~/homelab-backups/before-k3s/manifest.json
```

## 3. Stop Kind

Stop it, but **do not delete it yet**: stopped node containers are the
fastest rollback.

```bash
docker stop homelab-dev-control-plane homelab-dev-worker homelab-dev-worker2
docker update --restart=no homelab-dev-control-plane homelab-dev-worker homelab-dev-worker2
```

The second command keeps Kind from starting again at the next reboot and
taking the Gateway's ports. Check nothing is left listening:
`ss -ltnp | grep -E ':(8080|443)\b'` prints nothing.

## 4. Publish the k3s revision and install k3s

Now that Kind is stopped, put the k3s revision on `main`, which the new
cluster's Flux will sync. Note the merge commit; a rollback reverts it.

```bash
git fetch origin
git checkout main
git pull --ff-only
git merge --no-ff k3s -m "Migrate the dev cluster from Kind to k3s"
git push origin main
git log -1 --format=%H     # the merge commit, for a rollback
./scripts/bootstrap-host.sh
```

This installs k3s (pinned), the port guard that keeps k3s's API server and
kubelet off the network, checks the GPU (`/dev/kfd` and a render node) and
adds the `homelab-dev` context to `~/.kube/config`. Check it:

```bash
kubectl --context homelab-dev get nodes                  # one node, Ready
kubectl --context homelab-dev get storageclass           # local-path (default)
sudo nft list table inet homelab_guard                   # the port guard
```

If the GPU check fails, fix it before continuing (or roll back): the LLM
cannot run without `/dev/kfd`.

## 5. Bootstrap the platform

```bash
./scripts/bootstrap-dev.sh                          # home-network access off
LAN_ADDRESS=192.168.1.50 ./scripts/bootstrap-dev.sh  # or with your address
```

It refuses to run while a Kind node is still running. It installs Flux, which
deploys everything from Git, loads the lab CA and Garage secrets, gives
MLflow its S3 key, and waits for every component, including the device
plugin advertising `amd.com/gpu`. Expect 10–20 minutes on first install.

The platform now runs with **empty** volumes. That is expected: you replace
them with the backed-up data next. Do not use the chat or Grafana yet; what
you do now is discarded in step 7.

## 6. Stop what writes to the volumes

A volume can only be restored while nothing uses it. Suspend Flux's
Kustomizations and Helm releases so they do not restart what you stop (Helm
drift detection would otherwise put Grafana and Loki back within minutes),
then stop the writers:

```bash
flux suspend kustomization --all --context homelab-dev
for namespace in chat monitoring; do
  flux suspend helmrelease --all --namespace "${namespace}" --context homelab-dev
done
kubectl --context homelab-dev -n llm scale deployment llm-demand --replicas=0
kubectl --context homelab-dev -n chat scale statefulset,deployment --all --replicas=0
kubectl --context homelab-dev -n monitoring scale deployment kube-prometheus-stack-grafana --replicas=0
kubectl --context homelab-dev -n monitoring scale statefulset loki --replicas=0
kubectl --context homelab-dev -n ml-storage scale deployment garage --replicas=0
kubectl --context homelab-dev -n mlflow scale deployment mlflow --replicas=0
kubectl --context homelab-dev -n kafka scale deployment kafka --replicas=0
```

Prometheus is managed by its operator, which would restart it. Pause it
only if you restore its metrics (optional; it keeps seven days):

```bash
kubectl --context homelab-dev -n monitoring patch prometheus kube-prometheus-stack-prometheus \
  --type merge -p '{"spec":{"paused":true,"replicas":0}}'
```

Wait until those pods are gone:

```bash
kubectl --context homelab-dev get pods -n chat -n monitoring
kubectl --context homelab-dev get pods -A | grep -E 'open-webui|grafana|loki-0|garage|mlflow|kafka-[a-z0-9]+-[a-z0-9]{5}\b|prometheus-kube'
```

The second command must print nothing (Alloy and node-exporter may keep
running; they hold no volumes).

## 7. Restore the volumes

For each platform claim: make it empty, then restore it. `empty-claim
--replace` deletes the fresh, empty claim from step 5 and recreates it
with the same size and Helm ownership; it refuses a claim a pod still uses.
Restore takes the claim names from the backup, so the names match.

```bash
backup=~/homelab-backups/before-k3s
for claim in \
  chat/open-webui \
  monitoring/kube-prometheus-stack-grafana \
  monitoring/storage-loki-0 \
  ml-storage/garage \
  mlflow/mlflow \
  kafka/kafka
do
  ./scripts/backup-dev.py empty-claim "${claim}" --replace
  ./scripts/backup-dev.py restore-volume "${backup}" "${claim}"
done
```

Each restore asks for sudo once, then freezes the cluster for the copy. A
claim that is not in the backup (for example `ml-storage/garage` or
`kafka/kafka` if Kind never ran them) fails with "Backup does not contain
exactly one volume"; skip it. Kafka's restored volume keeps working because
its cluster ID is fixed in `kafka.yaml`. Check the exact names in the list from step 2; Open
WebUI's claim may be named `open-webui` or `open-webui-open-webui-0`
depending on how its chart created it.

Optionally restore Prometheus too (only if you paused it in step 6):

```bash
claim=monitoring/prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0
./scripts/backup-dev.py empty-claim "${claim}" --replace
./scripts/backup-dev.py restore-volume "${backup}" "${claim}"
```

**Application volumes** (Flip Finder, hello-crud and any others) do not
exist yet: Argo CD creates them in step 9. Create each one empty first,
with at least the capacity listed for it, then restore it. The namespace
must exist; create it if the application's Argo CD Application would have:

```bash
kubectl --context homelab-dev create namespace hello-crud   # if missing
./scripts/backup-dev.py empty-claim hello-crud/data --size 1Gi
./scripts/backup-dev.py restore-volume "${backup}" hello-crud/data
```

Use the real namespace, claim name and size from the list. A claim created
this way uses the `standard` class, as on Kind, so the application's chart
adopts it unchanged. A StatefulSet adopts it if the name matches its volume
claim template (`TEMPLATE-STATEFULSET-0`), which the list shows.

## 8. Resume the platform

```bash
kubectl --context homelab-dev -n monitoring patch prometheus kube-prometheus-stack-prometheus \
  --type merge -p '{"spec":{"paused":false,"replicas":null}}'      # only if paused in step 6
for namespace in chat monitoring; do
  flux resume helmrelease --all --namespace "${namespace}" --context homelab-dev
done
flux resume kustomization --all --context homelab-dev
./scripts/bootstrap-dev.sh
```

Resuming Flux restores the Deployments and StatefulSets you scaled down:
Garage, MLflow, Kafka and llm-demand from their manifests, Grafana and Loki through
Helm drift correction. The chat stays stopped in apps mode; llm-demand
starts it in llm mode.
Bootstrap then rewrites MLflow's S3 credentials from the restored Garage key
and waits until everything is healthy.

## 9. Re-register applications

Argo CD itself was redeployed empty. Re-enter private repository
credentials in Argo CD first, then:

```bash
kubectl --context homelab-dev apply --dry-run=server -f "${backup}/argocd.json"
kubectl --context homelab-dev apply -f "${backup}/argocd.json"
```

Argo CD syncs each application into its namespace and adopts the claims you
restored. For an application that uses S3, write its credentials again
(the key itself was restored with Garage):

```bash
./scripts/ml-credentials.py crosswalk     # once per namespace that had s3-credentials
```

If you took the backup in llm mode, run `./scripts/lab-mode.py apps` now.

## Verify

Work through this list before deleting Kind.

- [ ] `flux get kustomizations --context homelab-dev`: all Ready.
- [ ] `kubectl --context homelab-dev get pods -A`: nothing crash-looping or Pending
      (llama-server has no pod in apps mode, which is normal).
- [ ] `kubectl --context homelab-dev get nodes -o jsonpath='{.items[0].status.allocatable.amd\.com/gpu}'` prints `1`.
- [ ] `./scripts/lab-mode.py llm --wait` succeeds; the chat at
      <http://chat.localhost:8080> logs in with your old account and shows
      your old conversations; a prompt gets an answer. Then
      `./scripts/lab-mode.py apps`.
- [ ] While the LLM runs, `kubectl --context homelab-dev -n llm logs deployment/llama-server | grep -i vulkan`
      names the RX 6900 XT, and the [AMD GPU dashboard](http://grafana.localhost:8080/d/homelab-amd-gpu)
      shows VRAM in use.
- [ ] Grafana at <http://grafana.localhost:8080> logs in with the old
      credentials and keeps any dashboards you created; Explore > Loki shows
      logs from before the migration.
- [ ] MLflow at <http://mlflow.localhost:8080> lists earlier experiments and
      opens an artifact.
- [ ] Kafka still has its topics and consumer group offsets:
      `kubectl --context homelab-dev -n kafka exec deploy/kafka -- /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --all-groups`,
      and the [Kafka dashboard](http://grafana.localhost:8080/d/homelab-kafka)
      shows the broker Up.
- [ ] `kubectl --context homelab-dev describe clusterqueue gpu` lists
      `amd.com/gpu` with a nominal quota of 1, and a GPU Job from an
      application's LocalQueue is admitted and runs while the LLM is stopped.
- [ ] Argo CD at <http://argocd.localhost:8080>: every application Synced and
      Healthy; each application shows its old data.
- [ ] Exposure on the host: `curl --noproxy '*' -sI http://grafana.localhost:8080`
      answers. `ss -ltn` shows no socket on 8080 or 443: the Gateway's
      hostPorts are forwarding rules for one address each
      (`sudo iptables -t nat -S | grep -E 'dport (8080|443)'` shows them with
      `-d 127.0.0.1/32` and your LAN address). k3s's own ports (6443, 10250)
      do listen on every address; the port guard drops them from outside.
- [ ] Exposure from **another device** on your network: `curl -k
      https://HOST_ADDRESS:6443`, `curl http://HOST_ADDRESS:8080` and
      `nc -vz HOST_ADDRESS 10250` must all fail (time out or be refused). With
      `LAN_ADDRESS` set, `https://grafana.lab.internal` works from your phone
      and `https://HOST_ADDRESS` serves only `*.lab.internal` names; without
      `LAN_ADDRESS`, port 443 does not answer.
- [ ] `./scripts/backup-dev.py create` succeeds on k3s and
      `./scripts/backup-dev.py verify` accepts the result.

## 10. Delete the Kind cluster

Once everything above checks out (waiting a few days costs only disk space):

```bash
kind delete cluster --name homelab-dev
```

Keep `~/homelab-backups/before-k3s` until you have a newer verified k3s
backup. Docker and the `kind` binary are no longer needed; remove them if
you like (`sudo apt remove docker-ce docker-ce-cli containerd.io`, `sudo rm
/usr/local/bin/kind`). The old `kind-homelab-dev` context can go with
`kubectl config delete-context kind-homelab-dev`.

## Rolling back to Kind

If something fails before step 10, return to Kind. Kind's Flux syncs `main`,
so first undo the merge from step 4 (skip this if you never pushed it):

```bash
git checkout main
git pull --ff-only
git revert -m 1 MERGE_COMMIT     # the commit noted in step 4
git push origin main
```

Then remove k3s and start Kind again:

```bash
/usr/local/bin/k3s-uninstall.sh           # removes k3s and its volumes
sudo systemctl disable --now homelab-guard
docker update --restart=unless-stopped homelab-dev-control-plane homelab-dev-worker homelab-dev-worker2
docker start homelab-dev-control-plane homelab-dev-worker homelab-dev-worker2
./scripts/bootstrap-dev.sh                # the reverted, Kind-era script
```

The uninstall also deletes everything restored into k3s, which is fine: the
Kind containers still hold the data as it was at step 3, and the backup is
untouched. `bootstrap-host.sh` from the Kind revision reinstalls Docker and
Kind if they were removed.

If Kind was already deleted, recreate it from the reverted revision with its
`bootstrap-dev.sh` and restore the same backup with its scripts, following
that revision's [recovery guide](recovery.md).

## Troubleshooting the cutover

| Symptom | Next check |
| --- | --- |
| `bootstrap-dev.sh`: Kind still running | `docker ps`; stop the containers (step 3) |
| Gateway pod Pending with "didn't have free ports" | Something holds 127.0.0.1:8080 or LAN_ADDRESS:443: `sudo ss -ltnp \| grep -E ':(8080\|443)\b'`; usually a Kind container |
| `amd.com/gpu` never appears | `kubectl --context homelab-dev -n kube-system logs daemonset/amdgpu-device-plugin`; `/dev/kfd` exists; `ls /sys/class/kfd/kfd/topology/nodes` |
| llama-server Pending: Insufficient amd.com/gpu | Another pod holds the GPU (a training job); `kubectl --context homelab-dev get pods -A -o wide` and the AMD GPU dashboard's "GPU allocated" panel |
| llama-server fails to open the GPU | Its log; `getent group render video` match the IDs in `kubectl --context homelab-dev -n flux-system get configmap homelab-host -o yaml` (rerun bootstrap after group changes) |
| `restore-volume`: target used by a pod | A writer is still running (step 6); `kubectl --context homelab-dev get pods -A` |
| `empty-claim --replace` hangs deleting | A pod still mounts it; the PVC protection finalizer waits for it to stop |
| Restored chat or Grafana shows no data | The claim name restored does not match the one the chart uses; compare `kubectl --context homelab-dev get pvc -A` with the backup list |
| Gateway answers on 127.0.0.1:8080 but not from the home network | Docker, still installed from the Kind setup, may set the iptables FORWARD policy to DROP. `sudo iptables -S FORWARD \| head -1`; if it is `-P FORWARD DROP`, stop Docker (`sudo systemctl disable --now docker docker.socket`) or remove it (step 10) and restart k3s |
| Backup on k3s: cgroups not found | `ls /sys/fs/cgroup/kubepods.slice /sys/fs/cgroup/system.slice/k3s.service`; the script needs cgroup v2 (Ubuntu's default) |
