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
from contextlib import redirect_stdout
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
        self.volume = {"namespace": "chat", "claim": "open-webui", "node": "host",
                       "path": "/var/lib/rancher/k3s/storage/pvc-test_chat_open-webui",
                       "capacity": "2Gi", "archive": "chat.tar"}
        self.cgroups = [Path("/sys/fs/cgroup/kubepods.slice"), Path("/sys/fs/cgroup/system.slice/k3s.service")]

    def snapshot(self):
        def archive(_volume, destination):
            make_tar(destination, b"database and WAL fixture")
        with patch.object(backup, "nodes", return_value=["host"]), \
             patch.object(backup, "volume_inventory", return_value=[self.volume]), \
             patch.object(backup, "registrations", return_value={"items": []}), \
             patch.object(backup, "lab_mode", return_value="llm"), \
             patch.object(backup, "freezer_cgroups", return_value=self.cgroups), \
             patch.object(backup, "run"), \
             patch.object(backup, "paused"), patch.object(backup, "archive_volume", side_effect=archive):
            backup.create(self.directory)
        return self.directory

    def test_kubectl_get_keeps_subcommand_flags_after_get(self):
        with patch.object(backup, "output", return_value='{"items": []}') as output:
            self.assertEqual(backup.get("pv"), [])
        args = output.call_args.args
        self.assertLess(args.index("get"), args.index("-A"))
        self.assertEqual(args[args.index("--context") + 1], "homelab-dev")

    def test_lab_mode_is_recorded_and_defaults_to_apps(self):
        self.snapshot()
        self.assertEqual(json.loads((self.directory / "manifest.json").read_text())["lab_mode"], "llm")
        with patch.object(backup, "output", return_value="") as output:
            self.assertEqual(backup.lab_mode(), "apps")
        args = output.call_args.args
        self.assertEqual(args[args.index("--context") + 1], "homelab-dev")
        self.assertIn("--ignore-not-found", args)
        with patch.object(backup, "output", return_value='{"data": {"mode": "llm"}}'):
            self.assertEqual(backup.lab_mode(), "llm")

    def restore_patches(self, existing=None):
        """Patches for restore(); returns the list of (args, stdin bytes) of run calls."""
        calls = []
        def invoke(*args, **kwargs):
            stdin = kwargs.get("stdin")
            calls.append((args, stdin.read() if stdin else None))
        return calls, [
            patch.object(backup, "nodes", return_value=["host"]),
            patch.object(backup, "volume_inventory", return_value=[self.volume]),
            patch.object(backup, "freezer_cgroups", return_value=self.cgroups),
            patch.object(backup, "paused"), patch.object(backup, "require_unused_volume"),
            patch.object(backup, "archive_volume", side_effect=lambda v, p: make_tar(p, existing)),
            patch.object(backup, "run", side_effect=invoke),
        ]

    def test_create_verify_and_restore_preserve_bytes(self):
        self.snapshot()
        calls, patches = self.restore_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            backup.restore(self.directory, "chat/open-webui", None)
        self.assertEqual(calls[0][0], ("sudo", "-v"))
        extract = calls[1]
        self.assertEqual(extract[0][:4], ("sudo", "-n", "tar", "--extract"))
        self.assertIn("--numeric-owner", extract[0])
        self.assertIn("--same-owner", extract[0])
        self.assertEqual(extract[0][-1], f'--directory={self.volume["path"]}')
        self.assertEqual(extract[1], (self.directory / "chat.tar").read_bytes())

    def test_restore_refuses_occupied_destination_without_writes(self):
        self.snapshot()
        calls, patches = self.restore_patches(existing=b"existing")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            with self.assertRaisesRegex(ValueError, "not empty"):
                backup.restore(self.directory, "chat/open-webui", None)
        self.assertFalse(any("--extract" in args for args, _ in calls))

    def test_archive_uses_root_tar_relative_to_the_volume(self):
        with patch.object(backup, "run") as run:
            backup.archive_volume(self.volume, Path(self.temp.name) / "out.tar")
        args = run.call_args.args
        self.assertEqual(args[:4], ("sudo", "-n", "tar", "--create"))
        self.assertIn("--numeric-owner", args)
        self.assertEqual(args[-2:], (f'--directory={self.volume["path"]}', "."))

    def test_kind_era_backups_still_verify_and_restore(self):
        # The Kind script wrote the same manifest without "platform"; its
        # docker cp archives also hold paths relative to the volume root.
        self.snapshot()
        manifest = json.loads((self.directory / "manifest.json").read_text())
        manifest.pop("platform")
        manifest["volumes"][0]["path"] = "/var/local-path-provisioner/pvc-test"
        backup.write_json(self.directory / "manifest.json", manifest)
        calls, patches = self.restore_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            backup.restore(self.directory, "chat/open-webui", None)
        self.assertTrue(any("--extract" in args for args, _ in calls))

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
        with patch.object(backup, "nodes", return_value=["host"]), \
             patch.object(backup, "freezer_cgroups", return_value=self.cgroups), \
             patch.object(backup, "run"), \
             patch.object(backup, "volume_inventory", return_value=[self.volume]), \
             patch.object(backup, "registrations", return_value={"items": []}), \
             patch.object(backup, "lab_mode", return_value="apps"), \
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

    def frozen_tree(self):
        root = Path(self.temp.name) / "cgroup"
        for name in ("kubepods.slice", "system.slice/k3s.service"):
            (root / name).mkdir(parents=True)
            (root / name / "cgroup.freeze").write_text("0\n")
        return root

    def fake_freezer(self, fail_on=None):
        """Stand-in for the sudo write: records calls and writes the file."""
        calls = []
        def set_frozen(cgroup, frozen):
            calls.append((cgroup.name, frozen))
            (cgroup / "cgroup.freeze").write_text(f"{int(frozen)}\n")
            if fail_on == (cgroup.name, frozen):
                raise subprocess.CalledProcessError(1, ["sudo"])
        return calls, set_frozen

    def test_freezer_finds_pod_and_k3s_cgroups_or_refuses(self):
        root = self.frozen_tree()
        self.assertEqual(backup.freezer_cgroups(root), [root / "kubepods.slice", root / "system.slice/k3s.service"])
        (root / "system.slice/k3s.service/cgroup.freeze").unlink()
        with self.assertRaisesRegex(ValueError, "cgroups to freeze"):
            backup.freezer_cgroups(root)

    def test_freeze_writes_through_sudo_without_a_prompt(self):
        with patch.object(backup, "run") as run:
            backup.set_frozen(Path("/sys/fs/cgroup/kubepods.slice"), True)
        args = run.call_args.args
        self.assertEqual(args[:2], ("sudo", "-n"))
        self.assertIn("echo 1 > /sys/fs/cgroup/kubepods.slice/cgroup.freeze", args[-1])

    def test_pause_failure_resumes_everything_it_may_have_frozen(self):
        root = self.frozen_tree()
        cgroups = backup.freezer_cgroups(root)
        calls, set_frozen = self.fake_freezer(fail_on=("k3s.service", True))
        with patch.object(backup, "set_frozen", side_effect=set_frozen):
            with self.assertRaises(subprocess.CalledProcessError):
                with backup.paused(cgroups):
                    self.fail("Should not enter after a failed freeze")
        self.assertIn(("kubepods.slice", False), calls)
        self.assertIn(("k3s.service", False), calls)
        self.assertTrue(all(not backup.is_frozen(c) for c in cgroups))

    def test_interrupt_during_copy_resumes_the_cluster(self):
        root = self.frozen_tree()
        cgroups = backup.freezer_cgroups(root)
        calls, set_frozen = self.fake_freezer()
        with patch.object(backup, "set_frozen", side_effect=set_frozen):
            with self.assertRaises(KeyboardInterrupt):
                with backup.paused(cgroups):
                    self.assertTrue(all(backup.is_frozen(c) for c in cgroups))
                    raise KeyboardInterrupt()
        # Pods resume after k3s, in reverse order of freezing.
        self.assertEqual(calls[-2:], [("k3s.service", False), ("kubepods.slice", False)])
        self.assertTrue(all(not backup.is_frozen(c) for c in cgroups))

    def test_failed_resume_names_the_command_to_run(self):
        root = self.frozen_tree()
        cgroups = backup.freezer_cgroups(root)
        calls, set_frozen = self.fake_freezer(fail_on=("kubepods.slice", False))
        with patch.object(backup, "set_frozen", side_effect=set_frozen):
            with self.assertRaisesRegex(RuntimeError, "sudo tee .*kubepods.slice/cgroup.freeze"):
                with backup.paused(cgroups):
                    pass

    def test_only_a_single_node_cluster_is_accepted(self):
        with patch.object(backup, "get", return_value=[{"metadata": {"name": "a"}}, {"metadata": {"name": "b"}}]):
            with self.assertRaisesRegex(ValueError, "single-node"):
                backup.nodes()
        with patch.object(backup, "get", return_value=[{"metadata": {"name": "host"}}]):
            self.assertEqual(backup.nodes(), ["host"])

    def test_restore_refuses_active_pod_before_pause(self):
        self.snapshot()
        pod = {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "open-webui"}}]},
               "status": {"phase": "Running"}}
        with patch.object(backup, "nodes", return_value=["host"]), \
             patch.object(backup, "volume_inventory", return_value=[self.volume]), \
             patch.object(backup, "get", return_value=[pod]), patch.object(backup, "paused") as paused:
            with self.assertRaisesRegex(ValueError, "used by a pod"):
                backup.restore(self.directory, "chat/open-webui", None)
            paused.assert_not_called()

    def test_restore_refuses_smaller_pvc(self):
        self.snapshot()
        smaller = dict(self.volume, capacity="1Gi")
        with patch.object(backup, "nodes", return_value=["host"]), \
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
        pv = {"metadata": {"name": "pv"}, "spec": {"storageClassName": "local-path",
              "hostPath": {"path": "/var/lib/rancher/k3s/storage/pvc-123_a--b_c"},
              "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                  {"key": "kubernetes.io/hostname", "operator": "In", "values": ["host"]}]}]}}}}
        pvc = {"metadata": {"namespace": "a--b", "name": "c"}, "spec": {"volumeName": "pv"},
               "status": {"phase": "Bound", "capacity": {"storage": "2Gi"}}}
        other = copy.deepcopy(pvc)
        other["metadata"] = {"namespace": "a", "name": "b--c"}
        def get(resource):
            return [pv] if resource == "pv" else [pvc, other]
        with patch.object(backup, "get", side_effect=get):
            volumes = backup.volume_inventory(["host"])
            self.assertNotEqual(volumes[0]["archive"], volumes[1]["archive"])
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                backup.volume_inventory(["different-cluster-node"])
            pv["spec"]["storageClassName"] = "standard"  # Kind's name, aliased on k3s.
            self.assertEqual(len(backup.volume_inventory(["host"])), 2)
            pv["spec"]["storageClassName"] = "local-path"
            for path in ("/var/lib/rancher/k3s/storage", "/var/local-path-provisioner/pvc-123",
                         "/var/lib/rancher/k3s/storage/../../../etc"):
                pv["spec"]["hostPath"]["path"] = path
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "Unsupported"):
                    backup.volume_inventory(["host"])

    def helm_claim(self):
        return {"metadata": {"name": "open-webui", "namespace": "chat", "uid": "123", "resourceVersion": "9",
                             "labels": {"app.kubernetes.io/managed-by": "Helm"},
                             "annotations": {"meta.helm.sh/release-name": "open-webui",
                                             "pv.kubernetes.io/bind-completed": "yes",
                                             "volume.kubernetes.io/selected-node": "host"}},
                "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "2Gi"}},
                         "storageClassName": "local-path", "volumeMode": "Filesystem", "volumeName": "pvc-1"},
                "status": {"phase": "Bound"}}

    def fake_kubectl(self, existing):
        calls = []
        def kubectl(*args, stdin=None):
            calls.append((args, json.loads(stdin) if stdin else None))
            if args[:2] == ("get", "pvc"):
                return json.dumps(existing) if existing else ""
            return ""
        return calls, kubectl

    def test_empty_claim_replaces_a_claim_keeping_helm_ownership(self):
        calls, kubectl = self.fake_kubectl(self.helm_claim())
        with patch.object(backup, "kubectl", side_effect=kubectl), \
             patch.object(backup, "require_unused_volume") as unused, redirect_stdout(io.StringIO()):
            backup.empty_claim("chat/open-webui", replace=True)
        unused.assert_called_once()
        verbs = [args[0] for args, _ in calls]
        self.assertEqual(verbs, ["get", "delete", "create", "create", "wait", "delete"])
        claim = calls[2][1]
        self.assertEqual(claim["metadata"], {"name": "open-webui", "namespace": "chat",
                                             "labels": {"app.kubernetes.io/managed-by": "Helm"},
                                             "annotations": {"meta.helm.sh/release-name": "open-webui"}})
        self.assertNotIn("volumeName", claim["spec"])
        self.assertEqual(claim["spec"]["resources"], {"requests": {"storage": "2Gi"}})
        pod = calls[3][1]
        self.assertTrue(pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"])
        self.assertEqual(calls[5][0][:3], ("delete", "pod", "bind-open-webui"))

    def test_empty_claim_never_deletes_without_replace(self):
        calls, kubectl = self.fake_kubectl(self.helm_claim())
        with patch.object(backup, "kubectl", side_effect=kubectl):
            with self.assertRaisesRegex(ValueError, "--replace"):
                backup.empty_claim("chat/open-webui")
        self.assertEqual([args[0] for args, _ in calls], ["get"])

    def test_empty_claim_refuses_a_claim_in_use(self):
        calls, kubectl = self.fake_kubectl(self.helm_claim())
        with patch.object(backup, "kubectl", side_effect=kubectl), \
             patch.object(backup, "require_unused_volume", side_effect=ValueError("used by a pod")):
            with self.assertRaisesRegex(ValueError, "used by a pod"):
                backup.empty_claim("chat/open-webui", replace=True)
        self.assertFalse(any(args[0] == "delete" for args, _ in calls))

    def test_empty_claim_creates_a_new_claim_of_the_given_size(self):
        calls, kubectl = self.fake_kubectl(None)
        with patch.object(backup, "kubectl", side_effect=kubectl), redirect_stdout(io.StringIO()):
            backup.empty_claim("hello-crud/data", size="1Gi")
        claim = next(body for args, body in calls if body and body["kind"] == "PersistentVolumeClaim")
        self.assertEqual(claim["spec"]["storageClassName"], "standard")
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "1Gi")
        for size, replace, message in ((None, False, "--size"), ("1Gi", True, "nothing to replace"),
                                       ("lots", False, "Unsupported storage quantity")):
            with self.subTest(size=size), patch.object(backup, "kubectl", side_effect=self.fake_kubectl(None)[1]):
                with self.assertRaisesRegex(ValueError, message):
                    backup.empty_claim("hello-crud/data", size=size, replace=replace)

    def test_empty_claim_removes_the_bind_pod_even_if_binding_fails(self):
        calls = []
        def kubectl(*args, stdin=None):
            calls.append(args)
            if args[0] == "wait":
                raise subprocess.CalledProcessError(1, ["kubectl", "wait"])
            return ""
        with patch.object(backup, "kubectl", side_effect=kubectl):
            with self.assertRaises(subprocess.CalledProcessError):
                backup.empty_claim("hello-crud/data", size="1Gi")
        self.assertEqual(calls[-1][:2], ("delete", "pod"))


if __name__ == "__main__":
    unittest.main()
