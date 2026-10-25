# Backup and recovery

k3s keeps PVC data in `/var/lib/rancher/k3s/storage/` on the host. Deleting a
claim deletes its volume, and uninstalling k3s (`k3s-uninstall.sh`) deletes
them all. Models in `MODELS_DIR` are outside the cluster and survive; metrics,
logs, dashboards, chat history, application databases, S3 objects (datasets,
checkpoints, MLflow artifacts) and MLflow's tracking database need their own
copy. The backup includes every bound PVC, so Garage's `ml-storage/garage`,
MLflow's `mlflow/mlflow` and Kafka's `kafka/kafka` claims are archived like
the others. Garage's volume can hold up to 50 Gi and Kafka's 20 Gi; allow for
them on the backup disk.

Kafka's claim holds every topic, consumer group offsets and the broker's own
metadata, plus a cached copy of the JMX exporter agent. Kafka is frozen with
everything else, so the archive is crash-consistent like the rest: on restore
the broker checks its logs at start-up and recovers to the last write it
completed. Kafka is a buffer between services, not a system of record, so
messages produced during the backup may or may not be in it. Restore it
like any application-owned volume (below): suspend the `kafka` Flux
Kustomization and scale the broker to zero first. The restored data keeps
working because the broker's cluster ID is fixed in `kafka.yaml`; a volume
formatted with another ID is refused. Kueue keeps nothing on disk: its
queues are in Git, and Workloads are recreated from their Jobs.

## Create and verify a backup

Run from the host as your normal user, with Python 3, kubectl and sudo:

```bash
./scripts/backup-dev.py create
# Or choose a new directory on a separate disk:
./scripts/backup-dev.py create --output /path/to/backup-disk/homelab-2026-09-07
./scripts/backup-dev.py verify /path/to/backup-disk/homelab-2026-09-07
```

The default destination is a new timestamped directory in `~/homelab-backups`.
Allow host disk space for the actual contents of all PVCs; archives are
uncompressed. **The entire dev cluster is frozen during copying**, so choose a
maintenance window and avoid concurrent deployments or volume changes. The
script asks for your sudo password first, then freezes every pod and k3s
itself with the cgroup v2 freezer (the kubelet is frozen too, so nothing fails
a health check and restarts), and copies each volume with root `tar`,
preserving numeric ownership. Hashing runs after the cluster resumes. The
script uses an exclusive local lock, resumes the cluster on ordinary
errors/SIGINT/SIGTERM, and rejects existing output directories. An
`.incomplete` marker makes failed backups unrestorable.

Each backup contains a tar archive per bound PVC, a manifest with its original
namespace/name, capacity and SHA-256 checksums, and `argocd.json` with
Application, ApplicationSet and AppProject declarations. Generated Applications
are omitted when their ApplicationSet can recreate them. Names and file paths
are mapped in the manifest; hashed archive names avoid naming collisions.
Unknown storage backends and unbound claims fail explicitly.

The [cgroup freezer](https://docs.kernel.org/admin-guide/cgroup-v2.html#core-interface-files)
stops processes without asking applications to flush or shut down. Treat
these as **crash-consistent archives**, not application-consistent database
backups or an atomic distributed snapshot. SQLite WAL and other recovery files
are copied with their databases. Use application-native backups for systems
that cannot recover from a crash, and validate data after every recovery drill.

Backups taken with the Kind-era script verify and restore the same way:
their archives hold each volume's contents relative to its root, as k3s
archives do. [The migration guide](migrate-to-k3s.md) uses this.

The archive is private to your user by default but **is not encrypted**. It can
contain conversations, user accounts and application secrets held on disk or
in Argo Helm values. Kubernetes Secrets, private Git credentials, external
services, host configuration and model weights are not exported. A host-side
backup survives uninstalling k3s, but not losing that host/disk: copy it to a
separate protected location. Checksums detect corruption, not tampering; only
restore trusted backups.

The home-network root certificate authority is not in these archives either.
It lives on the host in `~/.config/homelab/lab-ca/` (or `LAB_CA_DIR`), outside
the cluster, and bootstrap reloads it into every rebuilt cluster, so trusting devices
keep working. Keep a copy of `ca.key` and `ca.crt` somewhere private, such as a
password manager. If they are lost, bootstrap creates a new root and every
device must trust the new `ca.crt`. Create the replacement cluster with the
same `LAN_ADDRESS` to keep home-network access.

Garage's RPC secret and admin token in `~/.config/homelab/ml-storage/` are not
in these archives and do not need to be: bootstrap generates new ones if they
are missing, and Garage's data does not depend on them. S3 access keys live in
Garage's volume, so after restoring it, rerun `./scripts/ml-credentials.py` for
each namespace to rewrite its `s3-credentials` Secret with the same key.

If a forced kill or host failure prevents cleanup, the cluster may stay
frozen. Thaw it before continuing (this is safe to run when nothing is frozen):

```bash
echo 0 | sudo tee /sys/fs/cgroup/system.slice/k3s.service/cgroup.freeze \
  /sys/fs/cgroup/kubepods.slice/cgroup.freeze
```

A reboot also thaws everything.
Never remove an `.incomplete` marker to make a failed backup appear valid.

## Restore one volume without replacing existing data

`restore-volume` verifies the entire backup, checks destination capacity, and
requires an **empty, bound PVC with no active pod using it**. It freezes the
cluster for the copy and preserves numeric ownership and file modes. It never
clears or overwrites an occupied volume.

Start by bootstrapping a replacement cluster if needed. Keep the original
backup and any surviving volume untouched. The example below restores chat to
a separate PVC and can also be used as a drill. Change the size to at least the
original claim capacity listed in `manifest.json`.

Local-path volumes are created only when a pod first uses them, so
`empty-claim` creates the claim and binds it with a temporary pod that mounts
it read-only, then removes the pod:

```bash
./scripts/backup-dev.py empty-claim chat/open-webui-restored --size 2Gi
./scripts/backup-dev.py restore-volume /path/to/verified-backup chat/open-webui \
  --target-claim chat/open-webui-restored
```

To restore into the original claim name instead, stop its writers (below),
then `./scripts/backup-dev.py empty-claim chat/open-webui --replace`
**deletes that claim and its data** and recreates it empty with the same size
and Helm ownership, so the chart adopts it. It refuses while a pod uses the
claim. New claims use the `standard` class unless `--storage-class
local-path` is given; both are the same k3s storage.

`empty-claim` removes its temporary pod before returning. For an existing
application-owned target, suspend both its Flux Kustomization and HelmRelease
(or its Argo application's sync), scale its writers to zero and wait for their
pods to disappear first. Prevent controllers from recreating writers until
recovery is complete; the script does not manage application lifecycles.

Before switching Open WebUI, first add the following under
`spec.values.persistence` in `kubernetes/infrastructure/chat/helm-release.yaml`
and reconcile that Git change while the original claim is still selected:

```yaml
annotations:
  helm.sh/resource-policy: keep
```

Confirm the successful Helm release manifest includes this annotation on the
original PVC (`helm --kube-context homelab-dev -n chat get manifest open-webui`).
Helm's [resource retention annotation](https://helm.sh/docs/howto/charts_tips_and_tricks/#tell-helm-not-to-uninstall-a-resource)
prevents the subsequent chart change from deleting its original PVC. The retained
claim becomes your responsibility to manage.

Then set `spec.values.persistence.existingClaim` to `open-webui-restored` in a
second Git change and deploy it when ready to switch. Use the chart's ordinary
rollout, then check that users can log in and expected conversation history is
present. The [pinned chart](https://github.com/open-webui/helm-charts/tree/open-webui-16.3.0/charts/open-webui)
supports this existing-claim setting. Preserve the old claim until verification
is complete; switching back provides a recovery path if the restored data is
not usable. Do not treat copied bytes as proof that the application recovered.

A failed extraction may leave the new destination partially populated. Keep
its consumers stopped, inspect the error and disk space, and retry into another
new empty PVC. The tool intentionally refuses to merge a retry into partial
data. Other applications need their own existing-volume configuration;
StatefulSet volume templates, credentials and external databases require
application-specific recovery steps.

## Recover registrations and finish a drill

After recovering application data and any required credentials, inspect
`argocd.json` locally. Applying it can start automatic application sync, so
complete storage configuration first:

```bash
kubectl --context homelab-dev apply --dry-run=server -f /path/to/verified-backup/argocd.json
kubectl --context homelab-dev apply -f /path/to/verified-backup/argocd.json
```

A backup taken in llm mode (`"lab_mode": "llm"` in `manifest.json`) holds
Applications with automated sync paused and their original policy saved in
the `homelab.local/saved-sync-policy` annotation. After applying them, run
`./scripts/lab-mode.py apps` to restore their sync. A rebuilt cluster starts in
apps mode.

These declarations point at application repositories and revisions; they do
not contain the source itself. Recreate private repository/cluster credentials
and required Kubernetes Secrets separately. A server dry run may need to be
repeated after restoring prerequisites such as namespaces and projects.

A successful drill means the backup verifies, data restores to a new volume,
the application starts with the intended credentials, and a known record can
be read. Record the backup timestamp, restore duration, application version,
and data check. Automated tests cover copy/failure behavior and archive safety;
a full destructive rebuild of the live cluster is not part of those tests.
