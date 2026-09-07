# Backup and recovery

Kind's `standard` PVCs live inside node containers. Deleting the cluster deletes
those volumes. Models in `MODELS_DIR` are host-mounted and survive; metrics,
logs, dashboards, chat history, and application databases need their own copy.

## Create and verify a backup

Run from the host with Python 3, Docker, Kind, and kubectl available:

```bash
./scripts/backup-dev.py create
# Or choose a new directory on a separate disk:
./scripts/backup-dev.py create --output /path/to/backup-disk/homelab-2026-09-07
./scripts/backup-dev.py verify /path/to/backup-disk/homelab-2026-09-07
```

The default destination is a new timestamped directory in `~/homelab-backups`.
Allow host disk space for the actual contents of all PVCs; archives are
uncompressed. **The entire dev cluster is paused during copying**, so choose a
maintenance window and avoid concurrent deployments or volume changes.
Hashing runs after nodes resume. The script uses an exclusive local lock,
resumes nodes on ordinary errors/SIGINT/SIGTERM, and rejects existing output
directories. An `.incomplete` marker makes failed backups unrestorable.

Each backup contains a tar archive per bound PVC, a manifest with its original
namespace/name, capacity and SHA-256 checksums, and `argocd.json` with
Application, ApplicationSet and AppProject declarations. Generated Applications
are omitted when their ApplicationSet can recreate them. Names and file paths
are mapped in the manifest; hashed archive names avoid naming collisions.
Unknown storage backends and unbound claims fail explicitly.

[Docker pause](https://docs.docker.com/reference/cli/docker/container/pause/)
freezes processes without asking applications to flush or shut down. Treat
these as **crash-consistent archives**, not application-consistent database
backups or an atomic distributed snapshot. SQLite WAL and other recovery files
are copied with their databases. Use application-native backups for systems
that cannot recover from a crash, and validate data after every recovery drill.

The archive is private to your user by default but **is not encrypted**. It can
contain conversations, user accounts and application secrets held on disk or
in Argo Helm values. Kubernetes Secrets, private Git credentials, external
services, host configuration and model weights are not exported. A host-side
backup survives deleting Kind, but not losing that host/disk: copy it to a
separate protected location. Checksums detect corruption, not tampering; only
restore trusted backups.

If a forced kill or host failure prevents cleanup, inspect Docker's paused
containers and explicitly unpause the affected dev nodes before continuing.
Never remove an `.incomplete` marker to make a failed backup appear valid.

## Restore one volume without replacing existing data

`restore-volume` verifies the entire backup, checks destination capacity, and
requires an **empty, bound PVC with no active pod using it**. It pauses the dev
nodes for the copy and preserves numeric ownership and file modes. It never
clears or overwrites an occupied volume.

Start by bootstrapping a replacement cluster if needed. Keep the original
backup and any surviving volume untouched. The example below restores chat to
a separate PVC and can also be used as a drill. Change the size to at least the
original claim capacity listed in `manifest.json`.

Kind uses `WaitForFirstConsumer`, so a temporary pod binds the new PVC. It does
not write into the volume:

```bash
kubectl --context kind-homelab-dev apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: open-webui-restored
  namespace: chat
spec:
  storageClassName: standard
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: bind-chat-restore
  namespace: chat
spec:
  restartPolicy: Never
  containers:
    - name: bind
      image: busybox:1.37.0
      command: ["sleep", "3600"]
      volumeMounts:
        - name: data
          mountPath: /restore
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: open-webui-restored
YAML
kubectl --context kind-homelab-dev -n chat wait pvc/open-webui-restored \
  --for=jsonpath='{.status.phase}'=Bound --timeout=2m
kubectl --context kind-homelab-dev -n chat delete pod bind-chat-restore --wait=true
./scripts/backup-dev.py restore-volume /path/to/verified-backup chat/open-webui \
  --target-claim chat/open-webui-restored
```

The temporary pod has been removed before restoration. For an existing
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
original PVC (`helm --kube-context kind-homelab-dev -n chat get manifest open-webui`).
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
kubectl --context kind-homelab-dev apply --dry-run=server -f /path/to/verified-backup/argocd.json
kubectl --context kind-homelab-dev apply -f /path/to/verified-backup/argocd.json
```

These declarations point at application repositories and revisions; they do
not contain the source itself. Recreate private repository/cluster credentials
and required Kubernetes Secrets separately. A server dry run may need to be
repeated after restoring prerequisites such as namespaces and projects.

A successful drill means the backup verifies, data restores to a new volume,
the application starts with the intended credentials, and a known record can
be read. Record the backup timestamp, restore duration, application version,
and data check. Automated tests cover copy/failure behavior and archive safety;
a full destructive rebuild of the live cluster is not part of those tests.
