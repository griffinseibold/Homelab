"""Bootstrap and download regressions without network or cluster access.

Run with: python3 -m unittest discover -s tests -v
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "Qwen3-8B-Q4_K_M.gguf"
MODEL_CHECKSUM = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
PAYLOAD = b"test model bytes\n"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "kubernetes/kind").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/bootstrap-dev.sh", self.repo / "scripts")
        shutil.copy2(ROOT / "scripts/download-models.sh", self.repo / "scripts")
        shutil.copy2(ROOT / "kubernetes/kind/dev.yaml", self.repo / "kubernetes/kind")
        shutil.copytree(
            ROOT / "kubernetes/clusters/dev/infrastructure",
            self.repo / "kubernetes/clusters/dev/infrastructure",
        )
        downloader = self.repo / "scripts/download-models.sh"
        downloader.write_text(
            downloader.read_text().replace(MODEL_CHECKSUM, hashlib.sha256(PAYLOAD).hexdigest())
        )
        self.models = self.directory / "model weights"
        self.models.mkdir()
        self.target = self.models / MODEL_NAME
        self.partial = self.models / (MODEL_NAME + ".partial")
        self.log = self.directory / "calls.jsonl"
        self.rendered = self.directory / "rendered.yaml"
        self.binary = self.directory / "bin"
        self.binary.mkdir()
        mock = self.binary / "mock"
        mock.write_text(
            "#!/usr/bin/env python3\n"
            + r'''
import json
import os
from pathlib import Path
import sys

tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["MOCK_LOG"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")
if tool == "kind":
    if args[:2] == ["get", "clusters"]:
        if os.environ.get("CLUSTER_LIST_FAILS") == "true":
            raise SystemExit(1)
        if os.environ.get("EXISTING_CLUSTER") == "true":
            print("homelab-dev")
    elif args[:2] == ["get", "nodes"]:
        print("homelab-dev-control-plane\nhomelab-dev-worker")
    elif args[:2] == ["create", "cluster"]:
        Path(os.environ["RENDERED_CONFIG"]).write_text(Path(args[args.index("--config") + 1]).read_text())
        if os.environ.get("CLUSTER_CREATE_FAILS") == "true":
            raise SystemExit(1)
elif tool == "docker":
    if args[0] == "inspect":
        print(os.environ["INSPECT_JSON"])
    elif args[0] == "port":
        print("127.0.0.1:8080")
elif tool == "curl" and "--output" in args:
    target = Path(args[args.index("--output") + 1])
    mode = os.environ.get("DOWNLOAD_MODE", "valid")
    if mode == "range":
        print("416", end="")
        raise SystemExit(22)
    if mode == "unsupported-range":
        print("200", end="")
        raise SystemExit(33)
    if mode == "interrupted":
        target.write_bytes(b"interrupted")
        print("200", end="")
        raise SystemExit(18)
    target.write_bytes(b"test model bytes\n" if mode == "valid" else b"corrupt")
    print("200", end="")
'''
        )
        mock.chmod(0o755)
        for tool in ("curl", "docker", "kind", "kubectl", "flux"):
            (self.binary / tool).symlink_to(mock)
        self.nodes = [
            {"Name": "/homelab-dev-control-plane", "Config": {"Labels": {"io.x-k8s.kind.role": "control-plane"}}},
            {
                "Name": "/homelab-dev-worker",
                "Config": {"Labels": {"io.x-k8s.kind.role": "worker"}},
                "Mounts": [
                    {"Type": "bind", "Source": str(self.models), "Destination": "/models", "RW": False},
                    {"Type": "bind", "Source": "/dev/dri", "Destination": "/dev/dri", "RW": True},
                ],
            },
        ]
        self.env = dict(
            os.environ,
            PATH=f"{self.binary}:{os.environ['PATH']}",
            MODELS_DIR=str(self.models),
            MOCK_LOG=str(self.log),
            RENDERED_CONFIG=str(self.rendered),
            INSPECT_JSON=json.dumps(self.nodes),
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_downloader(self, *args):
        return subprocess.run(
            [str(self.repo / "scripts/download-models.sh"), *args],
            env=self.env, text=True, capture_output=True, check=False,
        )

    def run_bootstrap(self, command="validate_gpu_directory() { return 0; }; main"):
        # GPU character-device validation is tested separately; a real GPU
        # must never be required by these bootstrap orchestration tests.
        return subprocess.run(
            ["bash", "-c", 'source "$1"; ' + command, "test", str(self.repo / "scripts/bootstrap-dev.sh")],
            env=self.env, text=True, capture_output=True, check=False,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_missing_model_does_not_create_directory_or_download(self):
        self.models.rmdir()
        result = self.run_downloader("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Download verified weights with:", result.stderr)
        self.assertFalse(self.models.exists())
        self.assertEqual(self.calls(), [])

    def test_check_valid_model_handles_special_path_characters(self):
        self.models = self.directory / 'weights "quoted" \\ slash\nnewline'
        self.models.mkdir()
        (self.models / MODEL_NAME).write_bytes(PAYLOAD)
        self.env["MODELS_DIR"] = str(self.models)
        self.assert_success(self.run_downloader("--check"))
        self.assertEqual(self.calls(), [])

    def test_check_corrupt_model_preserves_file_and_does_not_download(self):
        self.target.write_bytes(b"old corrupt file")
        result = self.run_downloader("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b"old corrupt file")
        self.assertEqual(self.calls(), [])

    def test_download_replaces_final_file_only_after_verification(self):
        self.target.write_bytes(b"old corrupt file")
        self.partial.write_bytes(b"previous transfer")
        self.assert_success(self.run_downloader())
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        self.assertFalse(self.partial.exists())
        self.assertIn("--continue-at", self.calls()[0])

    def test_corrupt_download_discards_partial_but_preserves_final_file(self):
        self.target.write_bytes(b"old corrupt file")
        self.env["DOWNLOAD_MODE"] = "corrupt"
        self.assertNotEqual(self.run_downloader().returncode, 0)
        self.assertEqual(self.target.read_bytes(), b"old corrupt file")
        self.assertFalse(self.partial.exists())

    def test_interrupted_download_keeps_resumable_partial(self):
        self.env["DOWNLOAD_MODE"] = "interrupted"
        self.assertNotEqual(self.run_downloader().returncode, 0)
        self.assertEqual(self.partial.read_bytes(), b"interrupted")
        self.assertFalse(self.target.exists())

    def test_rejected_resume_of_corrupt_partial_allows_fresh_retry(self):
        self.partial.write_bytes(b"corrupt completed file")
        self.env["DOWNLOAD_MODE"] = "range"
        self.assertNotEqual(self.run_downloader().returncode, 0)
        self.assertFalse(self.partial.exists())
        self.env["DOWNLOAD_MODE"] = "valid"
        self.assert_success(self.run_downloader())
        self.assertEqual(self.target.read_bytes(), PAYLOAD)

    def test_rejected_resume_of_valid_complete_partial_promotes_file(self):
        self.partial.write_bytes(PAYLOAD)
        self.env["DOWNLOAD_MODE"] = "range"
        self.assert_success(self.run_downloader())
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        self.assertFalse(self.partial.exists())

    def test_server_without_resume_support_allows_fresh_retry(self):
        self.partial.write_bytes(b"incomplete transfer")
        self.env["DOWNLOAD_MODE"] = "unsupported-range"
        self.assertNotEqual(self.run_downloader().returncode, 0)
        self.assertFalse(self.partial.exists())
        self.env["DOWNLOAD_MODE"] = "valid"
        self.assert_success(self.run_downloader())
        self.assertEqual(self.target.read_bytes(), PAYLOAD)

    def test_missing_model_stops_bootstrap_before_cluster_access(self):
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_missing_gpu_stops_bootstrap_before_cluster_access(self):
        self.target.write_bytes(PAYLOAD)
        result = self.run_bootstrap('validate_gpu_directory() { command false; }; main')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_gpu_validation_requires_a_character_device(self):
        gpu = self.directory / "dri"
        gpu.mkdir()
        (gpu / "renderD128").write_text("ordinary file")
        self.env["TEST_GPU_DIR"] = str(gpu)
        result = self.run_bootstrap('validate_gpu_directory "$TEST_GPU_DIR"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No GPU render device", result.stderr)
        (gpu / "renderD128").unlink()
        (gpu / "renderD128").symlink_to("/dev/null")
        self.assert_success(self.run_bootstrap('validate_gpu_directory "$TEST_GPU_DIR"'))

    def test_rendering_quotes_paths_without_evaluating_them(self):
        unusual = '/tmp/space "quotes" \\ backslash\nnewline $(touch nope)'
        self.env["MODELS_DIR"] = unusual
        result = self.run_bootstrap('render_cluster_config "$RENDERED_CONFIG"')
        self.assert_success(result)
        model_paths = [
            json.loads(line.split("hostPath:", 1)[1].strip())
            for line in self.rendered.read_text().splitlines()
            if "hostPath:" in line and '"' in line
        ]
        self.assertEqual(model_paths, [unusual, unusual])

    def test_new_cluster_uses_selected_models_directory_and_labels_worker(self):
        self.target.write_bytes(PAYLOAD)
        self.assert_success(self.run_bootstrap())
        config = self.rendered.read_text()
        self.assertIn(json.dumps(str(self.models)), config)
        self.assertNotIn("__MODELS_DIR__", config)
        self.assertIn(
            ["kubectl", "--context", "kind-homelab-dev", "label", "node", "homelab-dev-worker", "homelab.local/llm-capable=true", "--overwrite"],
            self.calls(),
        )

    def test_existing_compatible_cluster_is_preserved_and_labeled(self):
        self.target.write_bytes(PAYLOAD)
        self.env["EXISTING_CLUSTER"] = "true"
        self.assert_success(self.run_bootstrap())
        self.assertFalse(any(call[:3] == ["kind", "create", "cluster"] for call in self.calls()))
        self.assertTrue(any("homelab.local/llm-capable=true" in call for call in self.calls()))

    def test_cluster_listing_failure_does_not_attempt_creation(self):
        self.target.write_bytes(PAYLOAD)
        self.env["CLUSTER_LIST_FAILS"] = "true"
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertFalse(any(call[:2] == ["kind", "create"] for call in self.calls()))

    def test_failed_creation_removes_temporary_configuration(self):
        self.target.write_bytes(PAYLOAD)
        self.env["CLUSTER_CREATE_FAILS"] = "true"
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        creation = next(call for call in self.calls() if call[:2] == ["kind", "create"])
        self.assertFalse(Path(creation[creation.index("--config") + 1]).exists())

    def test_incompatible_existing_cluster_fails_without_mutations(self):
        self.target.write_bytes(PAYLOAD)
        self.env["EXISTING_CLUSTER"] = "true"
        self.nodes[1]["Mounts"][0]["Source"] = "/someone/elses/models"
        self.env["INSPECT_JSON"] = json.dumps(self.nodes)
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cluster has been preserved", result.stderr)
        self.assertFalse(any(call[0] in ("kubectl", "flux") for call in self.calls()))
        self.assertFalse(any(call[:2] in (["kind", "create"], ["kind", "delete"]) for call in self.calls()))

    def test_missing_gpu_mount_and_writable_model_mount_are_rejected(self):
        for mounts in (
            [self.nodes[1]["Mounts"][0]],
            [{**self.nodes[1]["Mounts"][0], "RW": True}, self.nodes[1]["Mounts"][1]],
        ):
            with self.subTest(mounts=mounts):
                nodes = [self.nodes[0], {**self.nodes[1], "Mounts": mounts}]
                self.env["INSPECT_JSON"] = json.dumps(nodes)
                result = self.run_bootstrap("validate_cluster_mounts")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("incompatible", result.stderr)

    def test_cluster_without_workers_is_rejected(self):
        self.env["INSPECT_JSON"] = json.dumps(self.nodes[:1])
        result = self.run_bootstrap("validate_cluster_mounts")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no worker nodes", result.stderr)


if __name__ == "__main__":
    unittest.main()
