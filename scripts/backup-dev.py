#!/usr/bin/env python3
"""Cold archive and empty-volume recovery for this repository's single-node k3s cluster.

Volumes are k3s local-path directories on this host. While they are copied,
every pod and k3s itself are frozen with the cgroup v2 freezer, the k3s
equivalent of pausing the old Kind node containers: nothing runs, nothing is
killed. Copies use sudo; the password is asked for before anything is frozen.
"""

import argparse
from contextlib import contextmanager
from collections import deque
from datetime import datetime, timezone
import hashlib
import fcntl
from decimal import Decimal
import re
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile

CLUSTER = "homelab-dev"
CONTEXT = CLUSTER  # The k3s kubeconfig context (ansible/roles/k3s).
STORAGE_CLASS = "local-path"
# kubernetes/infrastructure/storage keeps Kind's class name as an alias of
# local-path, for application charts written for Kind.
STORAGE_CLASSES = (STORAGE_CLASS, "standard")
# k3s's local-path provisioner keeps every volume under this directory.
STORAGE_ROOT = "/var/lib/rancher/k3s/storage/"
# Copies and freezing need root; -n never prompts once the cluster is frozen
# (credentials are refreshed with `sudo -v` before freezing).
SUDO = ("sudo", "-n")
CGROUP_ROOT = Path("/sys/fs/cgroup")
# Pods live under kubepods.slice (systemd cgroup driver) or kubepods
# (cgroupfs driver). k3s's own processes, including the kubelet and
# containerd, are in its service cgroup; freezing it too keeps the kubelet
# from failing probes and restarting frozen containers.
POD_CGROUPS = ("kubepods.slice", "kubepods")
K3S_CGROUP = "system.slice/k3s.service"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE, text=True, timeout=45).stdout


def get(resource, namespace=None):
    args = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", "get", resource]
    args += ["-n", namespace] if namespace else ["-A"]
    return json.loads(output(*args, "-o", "json"))["items"]


def nodes():
    names = [node["metadata"]["name"] for node in get("nodes")]
    if len(names) != 1:
        # Volumes are read from this host's disk, so they must all be local.
        raise ValueError(f"Expected the single-node k3s cluster; found {len(names)} node(s)")
    return names


def freezer_cgroups(root=CGROUP_ROOT):
    """The cgroups to freeze: the pods' parent cgroup, then k3s itself."""
    pods = [root / name for name in POD_CGROUPS if (root / name / "cgroup.freeze").exists()]
    k3s = root / K3S_CGROUP
    if len(pods) != 1 or not (k3s / "cgroup.freeze").exists():
        raise ValueError("Could not find the k3s pod and service cgroups to freeze "
                         f"(cgroup v2 with {' or '.join(POD_CGROUPS)} and {K3S_CGROUP})")
    return [pods[0], k3s]


def privileged(*args, **kwargs):
    return run(*SUDO, *args, **kwargs)


def set_frozen(cgroup, frozen):
    privileged("sh", "-c", f'echo {int(frozen)} > {shlex.quote(str(cgroup / "cgroup.freeze"))}',
               stdout=subprocess.DEVNULL, timeout=30)


def is_frozen(cgroup):
    return (cgroup / "cgroup.freeze").read_text().strip() == "1"


@contextmanager
def operation_lock():
    lock_dir = Path.home() / ".cache" / "homelab"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (lock_dir / "backup-dev.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another backup or restore is already running") from error
        yield


@contextmanager
def paused(cgroups):
    # Record before writing: an interrupted write may still have frozen it.
    attempted = []
    try:
        for cgroup in cgroups:
            attempted.append(cgroup)
            set_frozen(cgroup, True)
        yield
    finally:
        # A second Ctrl-C must not leave the cluster frozen.
        handlers = {sig: signal.signal(sig, signal.SIG_IGN)
                    for sig in (signal.SIGINT, signal.SIGTERM)}
        failures = []
        for cgroup in reversed(attempted):
            try:
                if is_frozen(cgroup):
                    set_frozen(cgroup, False)
            except (subprocess.SubprocessError, OSError, ValueError):
                failures.append(cgroup)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if failures:
            raise RuntimeError("Could not resume the cluster; run: "
                               + "; ".join(f"echo 0 | sudo tee {c / 'cgroup.freeze'}" for c in failures))


def volume_inventory(names, only_claim=None):
    pvs = {pv["metadata"]["name"]: pv for pv in get("pv")}
    volumes = []
    for pvc in get("pvc"):
        ns, name = pvc["metadata"]["namespace"], pvc["metadata"]["name"]
        if only_claim and f"{ns}/{name}" != only_claim:
            continue
        if pvc.get("status", {}).get("phase") != "Bound":
            raise ValueError(f"{ns}/{name} is not Bound; cannot back up all claims")
        pv = pvs[pvc["spec"]["volumeName"]]
        spec = pv["spec"]
        terms = spec.get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", [])
        hosts = set()
        for term in terms:
            for expr in term.get("matchExpressions", []):
                if expr["key"] == "kubernetes.io/hostname" and expr["operator"] == "In":
                    hosts.update(expr["values"])
        path = spec.get("hostPath", spec.get("local", {})).get("path", "")
        if (spec.get("storageClassName") not in STORAGE_CLASSES
                or spec.get("volumeMode", "Filesystem") != "Filesystem"
                or len(hosts) != 1 or not hosts.issubset(names)
                or not path.startswith(STORAGE_ROOT) or len(PurePosixPath(path).parts) < 7
                or ".." in PurePosixPath(path).parts):
            raise ValueError(f"Unsupported volume {ns}/{name}; expected a k3s local-path filesystem PVC")
        volumes.append({"namespace": ns, "claim": name, "node": hosts.pop(),
                        "path": path, "capacity": pvc["status"]["capacity"]["storage"],
                        "archive": hashlib.sha256(f"{ns}/{name}".encode()).hexdigest() + ".tar"})
    return sorted(volumes, key=lambda v: (v["namespace"], v["claim"]))


def clean_resource(resource):
    meta = resource["metadata"]
    metadata = {k: meta[k] for k in ("name", "namespace", "labels") if k in meta}
    annotations = {key: value for key, value in meta.get("annotations", {}).items()
                   if key not in ("kubectl.kubernetes.io/last-applied-configuration",
                                  "argocd.argoproj.io/refresh", "argocd.argoproj.io/hydrate")}
    if annotations:
        metadata["annotations"] = annotations
    return {"apiVersion": resource["apiVersion"], "kind": resource["kind"],
            "metadata": metadata, "spec": resource["spec"]}


def registrations():
    items = []
    for resource in ("appprojects.argoproj.io", "applicationsets.argoproj.io", "applications.argoproj.io"):
        for item in get(resource, "argocd"):
            if any(owner.get("kind") == "ApplicationSet" for owner in item["metadata"].get("ownerReferences", [])):
                continue  # ApplicationSets recreate their generated Applications.
            items.append(clean_resource(item))
    return {"apiVersion": "v1", "kind": "List", "items": items}


def lab_mode():
    """The lab mode from scripts/lab-mode.py; cluster state recorded for reference."""
    configmap = output("kubectl", "--context", CONTEXT, "--request-timeout=30s", "get", "configmap",
                       "lab-mode", "-n", "llm", "--ignore-not-found", "-o", "json").strip()
    return json.loads(configmap).get("data", {}).get("mode", "apps") if configmap else "apps"


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def archive_volume(volume, destination):
    # Members are relative to the volume ("." and "./file"), the same layout
    # as the Kind-era `docker cp` archives, so both restore the same way.
    with destination.open("wb") as handle:
        privileged("tar", "--create", "--file=-", "--numeric-owner", "--xattrs", "--acls",
                   f'--directory={volume["path"]}', ".", stdout=handle)


def extract_volume(archive, volume):
    with archive.open("rb") as handle:
        privileged("tar", "--extract", "--file=-", "--numeric-owner", "--same-permissions",
                   "--same-owner", "--xattrs", "--acls", f'--directory={volume["path"]}', stdin=handle)


def check_tar(path, empty=False):
    with tarfile.open(path, "r:") as archive:
        members = {}
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"Unsafe archive member: {member.name}")
            if empty and not (name == PurePosixPath(".") and member.isdir()):
                raise ValueError("Destination volume is not empty; refusing to overwrite data")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError(f"Unsupported special file in archive: {member.name}")
            if name in members:
                raise ValueError(f"Duplicate archive member: {member.name}")
            members[name] = member

    links = {name: member for name, member in members.items() if member.issym() or member.islnk()}
    for name, member in links.items():
        if PurePosixPath(member.linkname).is_absolute():
            raise ValueError(f"Unsafe archive link: {name}")

    # Docker archives links themselves, never their directory descendants.
    # Reject aliases written through a link so extraction order cannot redefine
    # the tree used by the resolver below.
    for name in members:
        if any(parent in links for parent in name.parents):
            raise ValueError(f"Archive member is nested beneath a link: {name}")

    def resolve(parts):
        pending, resolved, expansions = deque(parts), [], 0
        while pending:
            part = pending.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    raise ValueError("Unsafe archive link escapes the volume")
                resolved.pop()
                continue
            resolved.append(part)
            link = links.get(PurePosixPath(*resolved))
            if link:
                expansions += 1
                if expansions > 40:
                    raise ValueError("Archive link cycle or excessive link depth")
                # Symlinks are relative to their parent; hardlinks are relative
                # to the archive root. Expand before handling any subsequent ..
                # so a chain cannot disguise an escape as a safe lexical path.
                resolved.pop()
                if link.islnk():
                    resolved.clear()
                pending.extendleft(reversed(PurePosixPath(link.linkname).parts))
        return resolved

    for name in links:
        resolve(name.parts)


def verified_snapshot(directory):
    if (directory / ".incomplete").exists():
        raise ValueError("Backup is incomplete; do not restore it")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("version") != 1 or manifest.get("cluster") != CLUSTER:
        raise ValueError("Unrecognized backup format or cluster")
    archives = [v["archive"] for v in manifest["volumes"]]
    claims = [(v["namespace"], v["claim"]) for v in manifest["volumes"]]
    if len(set(archives)) != len(archives) or len(set(claims)) != len(claims):
        raise ValueError("Backup contains duplicate volumes or archive names")
    expected = {"argocd.json"} | {v["archive"] for v in manifest["volumes"]}
    if set(manifest["sha256"]) != expected:
        raise ValueError("Backup manifest is missing file checksums")
    for name, digest in manifest["sha256"].items():
        if Path(name).name != name or (directory / name).is_symlink():
            raise ValueError("Backup files must be ordinary files in the backup directory")
        if checksum(directory / name) != digest:
            raise ValueError(f"Checksum mismatch: {name}")
        if name.endswith(".tar"):
            check_tar(directory / name)
    return manifest


def create(directory):
    if directory.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Store backups outside the repository; they can contain private data")
    cluster_nodes = nodes()
    volumes = volume_inventory(cluster_nodes)
    argo = registrations()
    mode = lab_mode()
    cgroups = freezer_cgroups()
    run("sudo", "-v")  # Ask for the password now, not while frozen.
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    (directory / ".incomplete").touch()
    write_json(directory / "argocd.json", argo)
    print(f"Freezing the cluster while archiving {len(volumes)} volumes...", flush=True)
    with paused(cgroups):
        for volume in volumes:
            print(f'Archiving {volume["namespace"]}/{volume["claim"]}', flush=True)
            archive_volume(volume, directory / volume["archive"])
    # Hash after resuming to keep downtime limited to data copying.
    files = ["argocd.json"] + [v["archive"] for v in volumes]
    for volume in volumes:
        check_tar(directory / volume["archive"])
    write_json(directory / "manifest.json", {
        "version": 1, "cluster": CLUSTER, "platform": "k3s",
        "created_at": datetime.now(timezone.utc).isoformat(),
        # In llm mode the exported Applications are paused; their saved sync
        # policy stays in an annotation for ./scripts/lab-mode.py apps.
        "lab_mode": mode,
        "volumes": volumes, "sha256": {name: checksum(directory / name) for name in files},
    })
    (directory / ".incomplete").unlink()
    verified_snapshot(directory)
    print(f"Verified backup: {directory}")


def storage_bytes(quantity):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTPE]i|[kKMGTPE]|m)?", quantity)
    if not match:
        raise ValueError(f"Unsupported storage quantity: {quantity}")
    number, unit = match.groups()
    if unit == "m":
        return Decimal(number) / 1000
    if not unit:
        return Decimal(number)
    power = "KMGTPE".index(unit[0].upper()) + 1
    return Decimal(number) * ((1024 if unit.endswith("i") else 1000) ** power)


def require_unused_volume(volume):
    for pod in get("pods", volume["namespace"]):
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for item in pod["spec"].get("volumes", []):
            if item.get("persistentVolumeClaim", {}).get("claimName") == volume["claim"]:
                raise ValueError("Target PVC is used by a pod; suspend its GitOps reconciliation, "
                                 "stop its workloads and delete any binding pod before restoring")


def restore(directory, claim, target_claim):
    manifest = verified_snapshot(directory)  # Validate before any node is paused.
    matches = [v for v in manifest["volumes"] if f'{v["namespace"]}/{v["claim"]}' == claim]
    if len(matches) != 1:
        raise ValueError(f"Backup does not contain exactly one volume for {claim}")
    source = matches[0]
    cluster_nodes = nodes()
    targets = volume_inventory(cluster_nodes, only_claim=target_claim or claim)
    if len(targets) != 1:
        raise ValueError("Create and bind the target PVC before restoring")
    target = targets[0]
    if storage_bytes(target["capacity"]) < storage_bytes(source["capacity"]):
        raise ValueError("Target PVC capacity is smaller than the original PVC")
    require_unused_volume(target)
    cgroups = freezer_cgroups()
    run("sudo", "-v")  # Ask for the password now, not while frozen.
    # All processes are frozen before checking emptiness to prevent a writer
    # racing the check. The temporary tar remains private and is deleted.
    # Archives from the Kind-era script restore the same way: both hold the
    # volume's contents relative to its root.
    with tempfile.TemporaryDirectory(prefix="homelab-restore-") as temporary:
        with paused(cgroups):
            current = Path(temporary) / "target.tar"
            archive_volume(target, current)
            check_tar(current, empty=True)
            extract_volume(directory / source["archive"], target)
    print(f"Restored {claim} into {target_claim or claim}; verify the restored data before starting its workload.")


# Annotations the control plane sets while binding; a new claim must not carry them.
BINDING_ANNOTATION_PREFIXES = ("pv.kubernetes.io/", "volume.kubernetes.io/", "volume.beta.kubernetes.io/")
BIND_IMAGE = "busybox:1.37.0"


def kubectl(*args, stdin=None):
    command = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", *args]
    if stdin is None:
        return output(*command)
    return run(*command, input=stdin, stdout=subprocess.PIPE, text=True, timeout=180).stdout


def empty_claim_manifest(pvc):
    """A new claim with the same name, labels, annotations and request as pvc, but no volume."""
    meta = pvc["metadata"]
    metadata = {k: meta[k] for k in ("name", "namespace", "labels") if k in meta}
    annotations = {k: v for k, v in (meta.get("annotations") or {}).items()
                   if not k.startswith(BINDING_ANNOTATION_PREFIXES)
                   and k != "kubectl.kubernetes.io/last-applied-configuration"}
    if annotations:
        # Helm's ownership annotations survive, so a resumed HelmRelease
        # adopts the new claim instead of failing on it.
        metadata["annotations"] = annotations
    spec = {k: pvc["spec"][k] for k in ("accessModes", "resources", "storageClassName", "volumeMode")
            if k in pvc["spec"]}
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": metadata, "spec": spec}


def new_claim_manifest(namespace, name, size, storage_class):
    storage_bytes(size)  # Rejects a malformed quantity before anything changes.
    if storage_class not in STORAGE_CLASSES:
        raise ValueError(f"Storage class must be one of {', '.join(STORAGE_CLASSES)}")
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class,
                     "resources": {"requests": {"storage": size}}}}


def bind_pod_manifest(namespace, claim):
    # local-path binds a claim only once a pod uses it. This pod never writes.
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": f"bind-{claim}"[:63].rstrip("-."), "namespace": namespace},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 65534,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "bind", "image": BIND_IMAGE, "command": ["sleep", "3600"],
                                     "securityContext": {"allowPrivilegeEscalation": False,
                                                         "readOnlyRootFilesystem": True,
                                                         "capabilities": {"drop": ["ALL"]}},
                                     "volumeMounts": [{"name": "data", "mountPath": "/restore",
                                                       "readOnly": True}]}],
                     "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}


def empty_claim(claim, size=None, replace=False, storage_class="standard"):
    """Leave namespace/claim as an empty, bound claim for restore-volume."""
    namespace, _, name = claim.partition("/")
    if not namespace or not name:
        raise ValueError("Name the claim as namespace/name")
    found = kubectl("get", "pvc", name, "-n", namespace, "--ignore-not-found", "-o", "json").strip()
    if found:
        if not replace:
            raise ValueError(f"{claim} exists; pass --replace to delete it and its data first")
        require_unused_volume({"namespace": namespace, "claim": name})
        manifest = empty_claim_manifest(json.loads(found))
        print(f"Deleting {claim} and its data...", flush=True)
        kubectl("delete", "pvc", name, "-n", namespace, "--wait=true", "--timeout=2m")
    else:
        if not size:
            raise ValueError(f"{claim} does not exist; pass --size with at least the original capacity")
        if replace:
            raise ValueError(f"{claim} does not exist; there is nothing to replace")
        kubectl("get", "namespace", namespace)  # Fails clearly if the namespace is missing.
        manifest = new_claim_manifest(namespace, name, size, storage_class)
    kubectl("create", "-f", "-", stdin=json.dumps(manifest))
    pod = bind_pod_manifest(namespace, name)
    kubectl("create", "-f", "-", stdin=json.dumps(pod))
    try:
        kubectl("wait", f"pvc/{name}", "-n", namespace, "--for=jsonpath={.status.phase}=Bound", "--timeout=3m")
    finally:
        kubectl("delete", "pod", pod["metadata"]["name"], "-n", namespace, "--wait=true", "--timeout=2m")
    print(f"{claim} is empty and bound; restore into it with restore-volume.")


def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"Interrupted by signal {signum}")


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create_parser = sub.add_parser("create", help="Freeze the cluster and save all PVCs plus Argo registrations")
    create_parser.add_argument("--output", type=Path, default=Path.home() / "homelab-backups" /
                               datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
    verify_parser = sub.add_parser("verify", help="Check backup completeness, checksums and archive paths")
    verify_parser.add_argument("directory", type=Path)
    restore_parser = sub.add_parser("restore-volume", help="Restore one archive into an EMPTY, bound dev PVC")
    restore_parser.add_argument("directory", type=Path)
    restore_parser.add_argument("claim", help="Original namespace/PVC-name")
    restore_parser.add_argument("--target-claim", help="Different destination namespace/PVC-name")
    empty_parser = sub.add_parser("empty-claim", help="Create, or with --replace recreate, an EMPTY bound PVC "
                                  "to restore into; --replace deletes the claim's data")
    empty_parser.add_argument("claim", help="namespace/PVC-name")
    empty_parser.add_argument("--size", help="Capacity for a new claim, at least the original's (manifest.json)")
    empty_parser.add_argument("--replace", action="store_true",
                              help="Delete the existing claim and its data, then recreate it empty")
    # Every Kind volume was "standard", so a claim restored for an
    # application keeps the class its chart was written for.
    empty_parser.add_argument("--storage-class", default="standard", choices=STORAGE_CLASSES,
                              help="Class for a new claim (default: standard, Kind's class name)")
    args = parser.parse_args()
    try:
        if args.command == "create":
            with operation_lock():
                create(args.output.expanduser().resolve())
        elif args.command == "empty-claim":
            with operation_lock():
                empty_claim(args.claim, args.size, args.replace, args.storage_class)
        elif args.command == "verify":
            snapshot = verified_snapshot(args.directory)
            print(f'Backup verified: {len(snapshot["volumes"])} volume(s), plus Argo registrations')
        else:
            with operation_lock():
                restore(args.directory, args.claim, args.target_claim)
    except (ValueError, KeyError, OSError, RuntimeError, tarfile.TarError,
            subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"Backup/recovery failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
