#!/usr/bin/env python3
"""Cold archive and empty-volume recovery for this repository's local Kind cluster."""

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
import signal
import subprocess
import sys
import tarfile
import tempfile

CLUSTER = "homelab-dev"
CONTEXT = f"kind-{CLUSTER}"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE, text=True, timeout=45).stdout


def get(resource, namespace=None):
    args = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", "get", resource]
    args += ["-n", namespace] if namespace else ["-A"]
    return json.loads(output(*args, "-o", "json"))["items"]


def nodes():
    names = output("kind", "get", "nodes", "--name", CLUSTER).split()
    if not names:
        raise ValueError(f"Kind cluster {CLUSTER} does not exist")
    states = json.loads(output("docker", "inspect", *names))
    for state in states:
        if state["Config"]["Labels"].get("io.x-k8s.kind.cluster") != CLUSTER:
            raise ValueError("Refusing a container outside the dev cluster")
        if not state["State"]["Running"] or state["State"]["Paused"]:
            raise ValueError("All dev nodes must be running and unpaused")
    return names


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
def paused(names):
    # Record before calling Docker: an interrupted client may have paused the
    # node even when it did not receive the successful daemon response.
    attempted = []
    try:
        for name in names:
            attempted.append(name)
            run("docker", "pause", name, stdout=subprocess.DEVNULL, timeout=30)
        yield
    finally:
        # A second Ctrl-C must not strand nodes during cleanup.
        handlers = {sig: signal.signal(sig, signal.SIG_IGN)
                    for sig in (signal.SIGINT, signal.SIGTERM)}
        failures = []
        for name in reversed(attempted):
            try:
                state = json.loads(output("docker", "inspect", name))[0]["State"]
                if state["Paused"]:
                    run("docker", "unpause", name, stdout=subprocess.DEVNULL, timeout=30)
            except (subprocess.SubprocessError, OSError, KeyError, ValueError, IndexError, TypeError):
                failures.append(name)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if failures:
            raise RuntimeError("Could not resume nodes; run: docker unpause " + " ".join(failures))


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
        if (spec.get("storageClassName") != "standard"
                or spec.get("volumeMode", "Filesystem") != "Filesystem"
                or len(hosts) != 1 or not hosts.issubset(names)
                or not path.startswith("/var/local-path-provisioner/")
                or ".." in PurePosixPath(path).parts):
            raise ValueError(f"Unsupported volume {ns}/{name}; expected a Kind local-path filesystem PVC")
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


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def archive_volume(volume, destination):
    with destination.open("wb") as handle:
        run("docker", "cp", f'{volume["node"]}:{volume["path"]}/.', "-", stdout=handle)


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
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    (directory / ".incomplete").touch()
    write_json(directory / "argocd.json", argo)
    print(f"Pausing {len(cluster_nodes)} nodes while archiving {len(volumes)} volumes...", flush=True)
    with paused(cluster_nodes):
        for volume in volumes:
            print(f'Archiving {volume["namespace"]}/{volume["claim"]}', flush=True)
            archive_volume(volume, directory / volume["archive"])
    # Hash after resuming to keep downtime limited to data copying.
    files = ["argocd.json"] + [v["archive"] for v in volumes]
    for volume in volumes:
        check_tar(directory / volume["archive"])
    write_json(directory / "manifest.json", {
        "version": 1, "cluster": CLUSTER,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
    # All processes are frozen before checking emptiness to prevent a writer
    # racing the check. The temporary tar remains private and is deleted.
    with tempfile.TemporaryDirectory(prefix="homelab-restore-") as temporary:
        with paused(cluster_nodes):
            current = Path(temporary) / "target.tar"
            archive_volume(target, current)
            check_tar(current, empty=True)
            with (directory / source["archive"]).open("rb") as handle:
                run("docker", "cp", "-a", "-", f'{target["node"]}:{target["path"]}', stdin=handle)
    print(f"Restored {claim} into {target_claim or claim}; verify the restored data before starting its workload.")


def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"Interrupted by signal {signum}")


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create_parser = sub.add_parser("create", help="Pause dev nodes and save all PVCs plus Argo registrations")
    create_parser.add_argument("--output", type=Path, default=Path.home() / "homelab-backups" /
                               datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
    verify_parser = sub.add_parser("verify", help="Check backup completeness, checksums and archive paths")
    verify_parser.add_argument("directory", type=Path)
    restore_parser = sub.add_parser("restore-volume", help="Restore one archive into an EMPTY, bound dev PVC")
    restore_parser.add_argument("directory", type=Path)
    restore_parser.add_argument("claim", help="Original namespace/PVC-name")
    restore_parser.add_argument("--target-claim", help="Different destination namespace/PVC-name")
    args = parser.parse_args()
    try:
        if args.command == "create":
            with operation_lock():
                create(args.output.expanduser().resolve())
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
