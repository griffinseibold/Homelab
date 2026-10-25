"""Bootstrap and download regressions without network or cluster access.

Run with: python3 -m unittest discover -s tests -v
"""

import base64
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
        shutil.copy2(ROOT / "scripts/bootstrap-dev.sh", self.repo / "scripts")
        shutil.copy2(ROOT / "scripts/download-models.sh", self.repo / "scripts")
        # ml-credentials.py has its own tests; here it only records that
        # bootstrap ran it, and with which arguments.
        credentials = self.repo / "scripts/ml-credentials.py"
        credentials.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['MOCK_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(['ml-credentials.py', *sys.argv[1:]]) + '\\n')\n"
        )
        credentials.chmod(0o755)
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
if tool == "docker":
    if args[:2] == ["inspect", "--format"]:
        if os.environ.get("KIND_RUNNING") != "true":
            raise SystemExit(1)
        print("true")
elif tool == "kubectl" and args[:2] == ["config", "get-contexts"]:
    if os.environ.get("NO_CONTEXT") == "true":
        raise SystemExit(1)
elif tool == "kubectl" and "--raw" in args:
    if os.environ.get("CLUSTER_DOWN") == "true":
        raise SystemExit(1)
elif tool == "kubectl" and "configmap" in args and "homelab-host" in args and "get" in args:
    print(os.environ.get("PUBLISHED_LAN", ""), end="")
elif tool == "kubectl" and "allocatable" in " ".join(args):
    print(os.environ.get("GPU_ALLOCATABLE", "1"), end="")
elif tool == "kubectl" and "apply" in args and "-" in args:
    # Record what was applied from standard input, to check that secrets
    # arrive through stdin rather than the command line.
    with open(os.environ["MOCK_LOG"] + ".stdin", "a") as applied:
        applied.write(sys.stdin.read() + "\n")
elif tool == "kubectl" and args[2:6] == ["--namespace", "llm", "get", "configmap"]:
    # The lab mode is cluster state; unset means the ConfigMap is missing.
    print(os.environ.get("LAB_MODE", ""), end="")
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
        for tool in ("curl", "docker", "kubectl", "flux"):
            (self.binary / tool).symlink_to(mock)
        self.env = dict(
            os.environ,
            PATH=f"{self.binary}:{os.environ['PATH']}",
            MODELS_DIR=str(self.models),
            MOCK_LOG=str(self.log),
            LAB_CA_DIR=str(self.directory / "lab-ca"),
            ML_STORAGE_DIR=str(self.directory / "ml-storage"),
        )
        self.env.pop("LAN_ADDRESS", None)
        self.env.pop("LAB_MODE", None)
        self.env.pop("KIND_RUNNING", None)
        self.lab_ca = self.directory / "lab-ca"
        self.ml_storage = self.directory / "ml-storage"

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_downloader(self, *args):
        return subprocess.run(
            [str(self.repo / "scripts/download-models.sh"), *args],
            env=self.env, text=True, capture_output=True, check=False,
        )

    HOST = ("validate_gpu_directory() { return 0; }; "
            "host_settings() { printf 'MODELS_UID=1000\\nMODELS_GID=1000\\nRENDER_GID=992\\nVIDEO_GID=44\\n'; }; ")

    def run_bootstrap(self, command=HOST + "main"):
        # GPU character-device validation and host IDs are tested separately;
        # a real GPU must never be required by these orchestration tests.
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

    def test_gpu_validation_requires_kfd_and_a_render_character_device(self):
        gpu = self.directory / "dri"
        gpu.mkdir()
        (gpu / "renderD128").write_text("ordinary file")
        self.env["TEST_GPU_DIR"] = str(gpu)
        self.env["TEST_KFD"] = "/dev/null"  # Any character device.
        check = 'validate_gpu_directory "$TEST_GPU_DIR" "$TEST_KFD"'
        result = self.run_bootstrap(check)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No GPU render device", result.stderr)
        (gpu / "renderD128").unlink()
        (gpu / "renderD128").symlink_to("/dev/null")
        self.assert_success(self.run_bootstrap(check))
        self.env["TEST_KFD"] = str(self.directory / "missing-kfd")
        result = self.run_bootstrap(check)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing-kfd is missing", result.stderr)

    def test_models_directory_must_be_safe_to_substitute(self):
        for path in ("relative/models", '/tmp/a"b', "/tmp/a$b", "/tmp/a\\b", "/tmp/a\nb"):
            with self.subTest(path=path):
                self.env["MODELS_DIR"] = path
                self.assertNotEqual(self.run_bootstrap("validate_models_dir").returncode, 0)
        self.env["MODELS_DIR"] = "/home/user/model weights"
        self.assert_success(self.run_bootstrap("validate_models_dir"))

    def test_host_settings_report_owner_and_gpu_groups(self):
        fake = ('stat() { [[ "$2" == %u ]] && echo 1000 || echo 1001; }; '
                'getent() { [[ "$2" == render ]] && echo render:x:992: || echo video:x:44:; }; ')
        result = self.run_bootstrap(fake + "host_settings")
        self.assert_success(result)
        self.assertEqual(result.stdout.split(),
                         ["MODELS_UID=1000", "MODELS_GID=1001", "RENDER_GID=992", "VIDEO_GID=44"])
        result = self.run_bootstrap('stat() { echo 1000; }; getent() { :; }; host_settings')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no render or video group", result.stderr)

    def test_root_owned_models_directory_is_refused(self):
        result = self.run_bootstrap('stat() { echo 0; }; host_settings')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owned by root", result.stderr)

    def test_bootstrap_writes_host_configmap_for_flux(self):
        self.target.write_bytes(PAYLOAD)
        self.assert_success(self.run_bootstrap())
        create = self.host_configmap()
        self.assertIn(f"--from-literal=MODELS_DIR={self.models}", create)
        self.assertIn("--from-literal=RENDER_GID=992", create)
        self.assertFalse(any(arg.startswith("--from-literal=LAN_ADDRESS") for arg in create))
        self.assertEqual(create[create.index("--namespace") + 1], "flux-system")

    def test_running_kind_cluster_stops_bootstrap_before_cluster_changes(self):
        self.target.write_bytes(PAYLOAD)
        self.env["KIND_RUNNING"] = "true"
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("migrate-to-k3s.md", result.stderr)
        self.assertFalse(any(call[0] in ("kubectl", "flux") for call in self.calls()))

    def test_missing_k3s_context_points_to_host_bootstrap(self):
        self.target.write_bytes(PAYLOAD)
        self.env["NO_CONTEXT"] = "true"
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap-host.sh", result.stderr)
        self.assertFalse(any(call[0] == "flux" for call in self.calls()))

    def test_unreachable_cluster_is_reported(self):
        self.target.write_bytes(PAYLOAD)
        self.env["CLUSTER_DOWN"] = "true"
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemctl status k3s", result.stderr)

    def test_bootstrap_waits_for_the_gpu_resource_after_the_gpu_kustomization(self):
        self.target.write_bytes(PAYLOAD)
        self.assert_success(self.run_bootstrap())
        calls = self.calls()
        gpu = next(i for i, c in enumerate(calls) if c[:4] == ["flux", "reconcile", "kustomization", "gpu"])
        llm = next(i for i, c in enumerate(calls) if c[:4] == ["flux", "reconcile", "kustomization", "llm"])
        allocatable = next(i for i, c in enumerate(calls) if "allocatable" in " ".join(c))
        self.assertLess(gpu, allocatable)
        self.assertLess(allocatable, llm)

    def test_missing_gpu_resource_times_out_with_a_hint(self):
        self.env["GPU_ALLOCATABLE"] = ""
        result = self.run_bootstrap("sleep() { :; }; wait_for_gpu_resource 0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("amdgpu-device-plugin", result.stderr)

    def test_lab_ca_is_created_once_and_constrained_to_lab_names(self):
        self.assert_success(self.run_bootstrap("ensure_lab_ca"))
        certificate = (self.lab_ca / "ca.crt").read_bytes()
        self.assertEqual(self.lab_ca.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.lab_ca / "ca.key").stat().st_mode & 0o777, 0o600)
        constraints = subprocess.run(
            ["openssl", "x509", "-in", str(self.lab_ca / "ca.crt"), "-noout", "-ext", "nameConstraints"],
            text=True, capture_output=True, check=True,
        ).stdout
        self.assertIn("critical", constraints)
        self.assertIn("DNS:lab.internal", constraints.split("Excluded:")[0])
        self.assertIn("IP:0.0.0.0/0.0.0.0", constraints.split("Excluded:")[1])
        self.assert_success(self.run_bootstrap("ensure_lab_ca"))
        self.assertEqual((self.lab_ca / "ca.crt").read_bytes(), certificate)
        self.assertEqual(self.calls(), [])

    def test_partial_or_mismatched_lab_ca_is_never_replaced(self):
        self.assert_success(self.run_bootstrap("ensure_lab_ca"))
        certificate = (self.lab_ca / "ca.crt").read_bytes()
        key = (self.lab_ca / "ca.key").read_bytes()
        (self.lab_ca / "ca.key").unlink()
        result = self.run_bootstrap("ensure_lab_ca")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("will not replace", result.stderr)
        self.assertEqual((self.lab_ca / "ca.crt").read_bytes(), certificate)
        self.assertFalse((self.lab_ca / "ca.key").exists())

        other = self.directory / "other-ca"
        self.env["LAB_CA_DIR"] = str(other)
        self.assert_success(self.run_bootstrap("ensure_lab_ca"))
        (self.lab_ca / "ca.key").write_bytes((other / "ca.key").read_bytes())
        self.env["LAB_CA_DIR"] = str(self.lab_ca)
        result = self.run_bootstrap("ensure_lab_ca")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not belong", result.stderr)
        self.assertEqual((self.lab_ca / "ca.crt").read_bytes(), certificate)
        self.assertNotEqual((self.lab_ca / "ca.key").read_bytes(), key)

    def test_lan_address_must_be_a_private_ipv4_address_on_this_host(self):
        assigned = "lan_address_is_assigned() { return 0; }; validate_lan_address"
        for address in ("8.8.8.8", "100.64.0.1", "127.0.0.1", "fe80::1", "192.168.1", "on"):
            with self.subTest(address=address):
                self.env["LAN_ADDRESS"] = address
                self.assertNotEqual(self.run_bootstrap(assigned).returncode, 0)
        for address in ("192.168.1.50", "off"):
            self.env["LAN_ADDRESS"] = address
            self.assert_success(self.run_bootstrap(assigned))
        self.env["LAN_ADDRESS"] = "192.168.1.50"
        result = self.run_bootstrap("lan_address_is_assigned() { return 1; }; validate_lan_address")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not assigned to this host", result.stderr)

    def test_invalid_lan_address_stops_bootstrap_before_cluster_access(self):
        self.target.write_bytes(PAYLOAD)
        self.env["LAN_ADDRESS"] = "203.0.113.10"
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.lab_ca.exists())

    def host_configmap(self):
        return next(call for call in self.calls() if "configmap" in call and "homelab-host" in call
                    and "create" in call)

    def test_lan_address_is_published_and_probed(self):
        self.target.write_bytes(PAYLOAD)
        self.env["LAN_ADDRESS"] = "192.168.1.50"
        self.assert_success(self.run_bootstrap(self.HOST + "lan_address_is_assigned() { return 0; }; main"))
        self.assertIn("--from-literal=LAN_ADDRESS=192.168.1.50", self.host_configmap())
        self.assertTrue(any(
            call[0] == "curl" and "grafana.lab.internal:443:192.168.1.50" in call for call in self.calls()
        ))
        # A new cluster is in apps mode, where the chat is stopped.
        self.assertFalse(any(
            call[0] == "curl" and "chat.lab.internal:443:192.168.1.50" in call for call in self.calls()
        ))

    def test_without_lan_address_https_stays_on_loopback(self):
        self.target.write_bytes(PAYLOAD)
        self.assert_success(self.run_bootstrap())
        self.assertFalse(any(arg.startswith("--from-literal=LAN_ADDRESS") for arg in self.host_configmap()))
        self.assertFalse(any(call[0] == "curl" and "--resolve" in call for call in self.calls()))
        secret = next(call for call in self.calls() if "secret" in call and "lab-ca" in call)
        self.assertIn(f"--cert={self.lab_ca / 'ca.crt'}", secret)
        self.assertIn(f"--key={self.lab_ca / 'ca.key'}", secret)

    def test_published_lan_address_is_kept_without_lan_address_set(self):
        self.target.write_bytes(PAYLOAD)
        self.env["PUBLISHED_LAN"] = "192.168.1.50"
        self.assert_success(self.run_bootstrap(self.HOST + "lan_address_is_assigned() { return 0; }; main"))
        self.assertIn("--from-literal=LAN_ADDRESS=192.168.1.50", self.host_configmap())

    def test_published_lan_address_no_longer_on_this_host_stops_bootstrap(self):
        self.target.write_bytes(PAYLOAD)
        self.env["PUBLISHED_LAN"] = "192.168.1.50"
        result = self.run_bootstrap(self.HOST + "lan_address_is_assigned() { return 1; }; main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no longer assigned", result.stderr)
        self.assertFalse(any(call[0] == "flux" for call in self.calls()))

    def test_lan_address_can_change_or_be_turned_off(self):
        self.target.write_bytes(PAYLOAD)
        self.env["PUBLISHED_LAN"] = "192.168.1.50"
        self.env["LAN_ADDRESS"] = "192.168.1.60"
        result = self.run_bootstrap(self.HOST + "lan_address_is_assigned() { return 0; }; main")
        self.assert_success(result)
        self.assertIn("192.168.1.50 -> 192.168.1.60", result.stdout)
        self.assertIn("--from-literal=LAN_ADDRESS=192.168.1.60", self.host_configmap())
        self.log.unlink()
        self.env["LAN_ADDRESS"] = "off"
        result = self.run_bootstrap()
        self.assert_success(result)
        self.assertIn("192.168.1.50 -> off", result.stdout)
        self.assertFalse(any(arg.startswith("--from-literal=LAN_ADDRESS") for arg in self.host_configmap()))

    def gateway_probes(self):
        return [
            call[call.index("--header") + 1].removeprefix("Host: ")
            for call in self.calls() if call[0] == "curl" and "--header" in call
        ]

    def test_apps_mode_waits_for_demand_controller_not_llm_or_chat(self):
        self.target.write_bytes(PAYLOAD)
        result = self.run_bootstrap()
        self.assert_success(result)
        calls = self.calls()
        self.assertIn(
            ["kubectl", "--context", "homelab-dev", "--namespace", "llm",
             "rollout", "status", "deployment/llm-demand", "--timeout=5m"],
            calls,
        )
        # Routes are still checked even though their backends are stopped.
        for namespace, route in (("llm", "httproute/llm"), ("chat", "httproute/open-webui")):
            self.assertTrue(any(
                call[:6] == ["kubectl", "--context", "homelab-dev", "--namespace", namespace, "wait"]
                and route in call for call in calls
            ), route)
        self.assertEqual(self.gateway_probes(), ["grafana.localhost", "argocd.localhost", "mlflow.localhost"])
        self.assertIn("apps mode", result.stdout)

    def test_llm_mode_also_waits_for_llm_and_chat(self):
        self.target.write_bytes(PAYLOAD)
        self.env["LAB_MODE"] = "llm"
        self.env["PUBLISHED_LAN"] = "192.168.1.50"
        result = self.run_bootstrap(self.HOST + "lan_address_is_assigned() { return 0; }; main")
        self.assert_success(result)
        self.assertEqual(
            self.gateway_probes(),
            ["grafana.localhost", "argocd.localhost", "mlflow.localhost", "llm.localhost", "chat.localhost"],
        )
        self.assertTrue(any(
            call[0] == "curl" and "chat.lab.internal:443:192.168.1.50" in call for call in self.calls()
        ))
        self.assertIn("llm mode", result.stdout)
        self.assertIn("http://chat.localhost:8080", result.stdout)

    def test_ml_storage_secrets_are_created_once_privately(self):
        self.assert_success(self.run_bootstrap("ensure_ml_storage_secrets"))
        rpc = (self.ml_storage / "rpc-secret").read_text()
        token = (self.ml_storage / "admin-token").read_text()
        self.assertRegex(rpc, r"^[0-9a-f]{64}$")
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(self.ml_storage.stat().st_mode & 0o777, 0o700)
        for name in ("rpc-secret", "admin-token"):
            self.assertEqual((self.ml_storage / name).stat().st_mode & 0o777, 0o600)
        self.assert_success(self.run_bootstrap("ensure_ml_storage_secrets"))
        self.assertEqual((self.ml_storage / "rpc-secret").read_text(), rpc)
        self.assertEqual((self.ml_storage / "admin-token").read_text(), token)
        self.assertEqual(self.calls(), [])

    def test_partial_ml_storage_secrets_are_never_replaced(self):
        self.assert_success(self.run_bootstrap("ensure_ml_storage_secrets"))
        token = (self.ml_storage / "admin-token").read_text()
        (self.ml_storage / "rpc-secret").unlink()
        result = self.run_bootstrap("ensure_ml_storage_secrets")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Only part of the Garage secrets", result.stderr)
        self.assertEqual((self.ml_storage / "admin-token").read_text(), token)
        self.assertFalse((self.ml_storage / "rpc-secret").exists())

    def test_garage_secret_is_applied_through_stdin_only(self):
        self.assert_success(self.run_bootstrap("ensure_ml_storage_secrets; load_ml_storage_secrets"))
        token = (self.ml_storage / "admin-token").read_text()
        self.assertFalse(any(token in " ".join(call) for call in self.calls()))
        applied = [json.loads(line) for line in Path(str(self.log) + ".stdin").read_text().splitlines()
                   if line.startswith("{")]
        secret = next(item for item in applied if item.get("kind") == "Secret")
        self.assertEqual(secret["metadata"], {"name": "garage", "namespace": "ml-storage"})
        self.assertEqual(base64.b64decode(secret["data"]["GARAGE_ADMIN_TOKEN"]).decode(), token)
        self.assertTrue(any("--server-side" in call and "apply" in call for call in self.calls()))

    def test_malformed_garage_rpc_secret_stops_loading(self):
        self.ml_storage.mkdir()
        (self.ml_storage / "rpc-secret").write_text("not hex")
        (self.ml_storage / "admin-token").write_text("x" * 44)
        result = self.run_bootstrap("load_ml_storage_secrets")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("64 hexadecimal characters", result.stderr)

    def test_bootstrap_loads_garage_secrets_and_creates_mlflow_credentials_in_order(self):
        self.target.write_bytes(PAYLOAD)
        result = self.run_bootstrap()
        self.assert_success(result)
        calls = self.calls()
        def index(predicate):
            return next(i for i, call in enumerate(calls) if predicate(call))
        reconcile = lambda name: index(lambda c: c[:4] == ["flux", "reconcile", "kustomization", name])
        credentials = index(lambda c: c == ["ml-credentials.py", "mlflow", "--bucket", "mlflow-artifacts"])
        self.assertLess(reconcile("ml-storage"), credentials)
        self.assertLess(credentials, reconcile("mlflow"))
        self.assertTrue((self.ml_storage / "rpc-secret").exists())
        self.assertTrue(any(c[:6] == ["kubectl", "--context", "homelab-dev", "--namespace", "mlflow", "wait"]
                            and "httproute/mlflow" in c for c in calls))
        self.assertIn("http://mlflow.localhost:8080", result.stdout)

    def test_unknown_lab_mode_is_treated_as_apps(self):
        self.env["LAB_MODE"] = "chat"
        result = self.run_bootstrap("current_lab_mode")
        self.assert_success(result)
        self.assertEqual(result.stdout.strip(), "apps")


if __name__ == "__main__":
    unittest.main()
