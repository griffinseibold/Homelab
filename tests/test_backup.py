"""Recovery regression tests run entirely on temporary data and fake containers."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup", ROOT / "scripts/backup-dev.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def make_tar(path, content=None, name="data.db"):
    with tarfile.open(path, "w") as archive:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        if content is not None:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "snapshot"
        self.volume = {"namespace": "chat", "claim": "open-webui", "node": "worker",
                       "path": "/var/local-path-provisioner/pvc-test", "capacity": "2Gi", "archive": "chat.tar"}

    def snapshot(self):
        def archive(_volume, destination):
            make_tar(destination, b"database and WAL fixture")
        with patch.object(backup, "nodes", return_value=["worker"]), \
             patch.object(backup, "volume_inventory", return_value=[self.volume]), \
             patch.object(backup, "registrations", return_value={"items": []}), \
             patch.object(backup, "paused"), patch.object(backup, "archive_volume", side_effect=archive):
            backup.create(self.directory)
        return self.directory

    def test_kubectl_get_keeps_subcommand_flags_after_get(self):
        with patch.object(backup, "output", return_value='{"items": []}') as output:
            self.assertEqual(backup.get("pv"), [])
        args = output.call_args.args
        self.assertLess(args.index("get"), args.index("-A"))
        self.assertEqual(args[args.index("--context") + 1], "kind-homelab-dev")

    def test_create_verify_and_restore_preserve_bytes(self):
        self.snapshot()
        calls = []
        def invoke(*args, **kwargs):
            calls.append((args, kwargs["stdin"].read()))
        with patch.object(backup, "nodes", return_value=["worker"]), \
             patch.object(backup, "volume_inventory", return_value=[self.volume]), \
             patch.object(backup, "paused"), patch.object(backup, "require_unused_volume"), \
             patch.object(backup, "archive_volume", side_effect=lambda v, p: make_tar(p)), \
             patch.object(backup, "run", side_effect=invoke):
            backup.restore(self.directory, "chat/open-webui", None)
        self.assertEqual(calls[0][0], ("docker", "cp", "-a", "-", "worker:/var/local-path-provisioner/pvc-test"))
        self.assertEqual(calls[0][1], (self.directory / "chat.tar").read_bytes())

    def test_restore_refuses_occupied_destination_without_writes(self):
        self.snapshot()
        with patch.object(backup, "nodes", return_value=["worker"]), \
             patch.object(backup, "volume_inventory", return_value=[self.volume]), \
             patch.object(backup, "paused"), patch.object(backup, "require_unused_volume"), \
             patch.object(backup, "archive_volume", side_effect=lambda v, p: make_tar(p, b"existing")), \
             patch.object(backup, "run") as run:
            with self.assertRaisesRegex(ValueError, "not empty"):
                backup.restore(self.directory, "chat/open-webui", None)
            run.assert_not_called()

    def test_corruption_and_incomplete_backups_rejected_before_cluster_access(self):
        self.snapshot()
        (self.directory / "chat.tar").write_bytes(b"corrupt")
        with patch.object(backup, "nodes") as nodes:
            with self.assertRaisesRegex(ValueError, "Checksum"):
                backup.restore(self.directory, "chat/open-webui", None)
            nodes.assert_not_called()
        (self.directory / ".incomplete").touch()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            backup.verified_snapshot(self.directory)

    def test_failed_copy_leaves_incomplete_backup(self):
        with patch.object(backup, "nodes", return_value=["worker"]), \
             patch.object(backup, "volume_inventory", return_value=[self.volume]), \
             patch.object(backup, "registrations", return_value={"items": []}), \
             patch.object(backup, "paused"), patch.object(backup, "require_unused_volume"), \
             patch.object(backup, "archive_volume", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backup.create(self.directory)
        self.assertTrue((self.directory / ".incomplete").exists())

    def test_archive_member_and_link_traversal_rejected(self):
        for name in ("../outside", "/absolute"):
            with self.subTest(name=name):
                path = Path(self.temp.name) / "bad.tar"
                make_tar(path, b"bad", name=name)
                with self.assertRaisesRegex(ValueError, "Unsafe archive"):
                    backup.check_tar(path)
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../etc"
            archive.addfile(info)
        with self.assertRaisesRegex(ValueError, "Unsafe archive link"):
            backup.check_tar(path)

    def test_safe_cache_symlink_and_hardlink_are_accepted(self):
        path = Path(self.temp.name) / "links.tar"
        make_tar(path, b"weights", name="cache/blobs/model")
        with tarfile.open(path, "a") as archive:
            link = tarfile.TarInfo("cache/snapshots/revision/model.bin")
            link.type, link.linkname = tarfile.SYMTYPE, "../../blobs/model"
            archive.addfile(link)
            hardlink = tarfile.TarInfo("duplicate-model")
            hardlink.type, hardlink.linkname = tarfile.LNKTYPE, "cache/blobs/model"
            archive.addfile(hardlink)
        backup.check_tar(path)

    def test_chained_links_cannot_hide_an_escape(self):
        path = Path(self.temp.name) / "links.tar"
        with tarfile.open(path, "w") as archive:
            for name, target in [("a", "."), ("d/link", "../a/../outside")]:
                link = tarfile.TarInfo(name)
                link.type, link.linkname = tarfile.SYMTYPE, target
                archive.addfile(link)
        with self.assertRaisesRegex(ValueError, "escapes"):
            backup.check_tar(path)

    def test_link_cycles_and_link_directory_children_are_rejected(self):
        path = Path(self.temp.name) / "links.tar"
        with tarfile.open(path, "w") as archive:
            for name, target in [("a", "b"), ("b", "a")]:
                link = tarfile.TarInfo(name)
                link.type, link.linkname = tarfile.SYMTYPE, target
                archive.addfile(link)
        with self.assertRaisesRegex(ValueError, "cycle"):
            backup.check_tar(path)
        make_tar(path, b"bad", name="alias/child")
        with tarfile.open(path, "a") as archive:
            link = tarfile.TarInfo("alias")
            link.type, link.linkname = tarfile.SYMTYPE, "directory"
            archive.addfile(link)
        with self.assertRaisesRegex(ValueError, "nested beneath"):
            backup.check_tar(path)

    def test_duplicate_archives_rejected(self):
        self.snapshot()
        manifest = json.loads((self.directory / "manifest.json").read_text())
        second = dict(manifest["volumes"][0], namespace="other")
        manifest["volumes"].append(second)
        backup.write_json(self.directory / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            backup.verified_snapshot(self.directory)

    def test_pause_failure_resumes_every_node_it_may_have_paused(self):
        calls = []
        def invoke(*args, **kwargs):
            calls.append(args)
            if args == ("docker", "pause", "worker2"):
                raise subprocess.CalledProcessError(1, args)
        with patch.object(backup, "run", side_effect=invoke), \
             patch.object(backup, "output", return_value='[{"State":{"Paused":true}}]'):
            with self.assertRaises(subprocess.CalledProcessError):
                with backup.paused(["worker", "worker2"]):
                    self.fail("Should not enter after a failed pause")
        self.assertIn(("docker", "unpause", "worker"), calls)
        self.assertIn(("docker", "unpause", "worker2"), calls)

    def test_interrupt_during_copy_resumes_nodes(self):
        with patch.object(backup, "run") as run, \
             patch.object(backup, "output", return_value='[{"State":{"Paused":true}}]'):
            with self.assertRaises(KeyboardInterrupt):
                with backup.paused(["worker"]):
                    raise KeyboardInterrupt()
        self.assertEqual(run.call_args.args, ("docker", "unpause", "worker"))

    def test_restore_refuses_active_pod_before_pause(self):
        self.snapshot()
        pod = {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "open-webui"}}]},
               "status": {"phase": "Running"}}
        with patch.object(backup, "nodes", return_value=["worker"]), \
             patch.object(backup, "volume_inventory", return_value=[self.volume]), \
             patch.object(backup, "get", return_value=[pod]), patch.object(backup, "paused") as paused:
            with self.assertRaisesRegex(ValueError, "used by a pod"):
                backup.restore(self.directory, "chat/open-webui", None)
            paused.assert_not_called()

    def test_restore_refuses_smaller_pvc(self):
        self.snapshot()
        smaller = dict(self.volume, capacity="1Gi")
        with patch.object(backup, "nodes", return_value=["worker"]), \
             patch.object(backup, "volume_inventory", return_value=[smaller]), \
             patch.object(backup, "paused") as paused:
            with self.assertRaisesRegex(ValueError, "smaller"):
                backup.restore(self.directory, "chat/open-webui", None)
            paused.assert_not_called()

    def test_operation_lock_prevents_overlapping_pauses(self):
        with patch.object(Path, "home", return_value=Path(self.temp.name)):
            with backup.operation_lock():
                with self.assertRaisesRegex(ValueError, "already running"):
                    with backup.operation_lock():
                        self.fail("Concurrent operation admitted")

    def test_inventory_uses_unique_names_and_refuses_foreign_volumes(self):
        pv = {"metadata": {"name": "pv"}, "spec": {"storageClassName": "standard",
              "hostPath": {"path": "/var/local-path-provisioner/pvc-123"},
              "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                  {"key": "kubernetes.io/hostname", "operator": "In", "values": ["worker"]}]}]}}}}
        pvc = {"metadata": {"namespace": "a--b", "name": "c"}, "spec": {"volumeName": "pv"},
               "status": {"phase": "Bound", "capacity": {"storage": "2Gi"}}}
        other = copy.deepcopy(pvc)
        other["metadata"] = {"namespace": "a", "name": "b--c"}
        def get(resource):
            return [pv] if resource == "pv" else [pvc, other]
        with patch.object(backup, "get", side_effect=get):
            volumes = backup.volume_inventory(["worker"])
            self.assertNotEqual(volumes[0]["archive"], volumes[1]["archive"])
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                backup.volume_inventory(["different-cluster-worker"])


if __name__ == "__main__":
    unittest.main()
