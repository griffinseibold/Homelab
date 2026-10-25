"""LLM demand controller regressions with a fake Kubernetes API; no cluster access."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEMAND = ROOT / "kubernetes/infrastructure/llm/demand"
spec = importlib.util.spec_from_file_location("llm_demand", DEMAND / "controller.py")
demand = importlib.util.module_from_spec(spec)
sys.modules["llm_demand"] = demand  # dataclasses resolve annotations through sys.modules.
spec.loader.exec_module(demand)

GRACE = 300


def workload(name, namespace="app", replicas=1, labels=None, kind=None, created=None):
    item = {"metadata": {"name": name, "namespace": namespace,
                         "labels": {"homelab.local/llm": "requested"} if labels is None else labels},
            "spec": {} if replicas is None else {"replicas": replicas}}
    if kind:
        item["kind"] = kind
    if created:
        item["metadata"]["creationTimestamp"] = created
    return item


class DecisionTests(unittest.TestCase):
    def test_missing_replicas_counts_as_one_and_zero_is_not_a_request(self):
        self.assertEqual(demand.replicas_of({"spec": {}}), 1)
        requests = demand.active_requests([
            workload("default-replicas", replicas=None, kind="Deployment"),
            workload("stopped", replicas=0, kind="Deployment"),
            workload("db", replicas=2, kind="StatefulSet"),
            workload("unlabeled", labels={}, kind="Deployment"),
            workload("other-value", labels={"homelab.local/llm": "no"}, kind="Deployment"),
        ])
        self.assertEqual(requests, ["Deployment app/default-replicas", "StatefulSet app/db"])

    def test_mode_defaults_to_apps_and_rejects_unknown_values(self):
        self.assertEqual(demand.parse_mode(None), ("apps", None))
        self.assertEqual(demand.parse_mode({"data": {}}), ("apps", None))
        self.assertEqual(demand.parse_mode({"data": {"mode": " LLM\n"}}), ("llm", None))
        mode, warning = demand.parse_mode({"data": {"mode": "chat"}})
        self.assertEqual(mode, "apps")
        self.assertIn("unknown lab mode", warning)

    def test_llm_mode_runs_llm_and_chat(self):
        decision = demand.decide("llm", [], 0, None, 100, GRACE)
        self.assertEqual((decision.llm_replicas, decision.chat_replicas), (1, 1))
        self.assertIn("llm mode", decision.reason)

    def test_request_runs_llm_without_chat_and_names_requesters(self):
        decision = demand.decide("apps", ["Deployment app/a"], 0, 50, 100, GRACE)
        self.assertEqual((decision.llm_replicas, decision.chat_replicas), (1, 0))
        self.assertIsNone(decision.idle_since)
        self.assertIn("Deployment app/a", decision.reason)

    def test_idle_grace_keeps_running_server_then_stops_it(self):
        first = demand.decide("apps", [], 1, None, 1000, GRACE)
        self.assertEqual(first.llm_replicas, 1)
        self.assertEqual(first.idle_since, 1000)
        during = demand.decide("apps", [], 1, first.idle_since, 1000 + GRACE - 1, GRACE)
        self.assertEqual(during.llm_replicas, 1)
        after = demand.decide("apps", [], 1, first.idle_since, 1000 + GRACE, GRACE)
        self.assertEqual(after.llm_replicas, 0)

    def test_request_returning_during_grace_resets_the_timer(self):
        idle = demand.decide("apps", [], 1, None, 1000, GRACE)
        back = demand.decide("apps", ["Deployment app/a"], 1, idle.idle_since, 1100, GRACE)
        self.assertIsNone(back.idle_since)
        idle_again = demand.decide("apps", [], 1, back.idle_since, 1200, GRACE)
        self.assertEqual(idle_again.idle_since, 1200)

    def test_stopped_server_is_not_started_without_demand(self):
        decision = demand.decide("apps", [], 0, None, 1000, GRACE)
        self.assertEqual((decision.llm_replicas, decision.chat_replicas), (0, 0))


class FakeApi:
    """Answers the controller's few API paths from in-memory state."""

    def __init__(self):
        self.mode = None
        self.requests = {"deployments": [], "statefulsets": []}
        self.llm_replicas = 1
        self.chat = {"deployments": [], "statefulsets": []}
        self.chat_error = None
        self.fail = None
        self.scaled = []

    def get(self, path, query=None):
        if self.fail:
            raise self.fail
        if path.startswith("/apis/apps/v1/") and "/namespaces/" not in path:
            self.last_selector = query["labelSelector"]
            return {"items": [dict(item) for item in self.requests[path.rsplit("/", 1)[1]]]}
        if path.startswith("/apis/apps/v1/namespaces/chat/"):
            if self.chat_error:
                raise self.chat_error
            return {"items": self.chat[path.rsplit("/", 1)[1]]}
        raise AssertionError(f"unexpected GET {path}")

    def get_or_none(self, path):
        if self.fail:
            raise self.fail
        if path == "/api/v1/namespaces/llm/configmaps/lab-mode":
            return None if self.mode is None else {"data": {"mode": self.mode}}
        if path == "/apis/apps/v1/namespaces/llm/deployments/llama-server/scale":
            return None if self.llm_replicas is None else {"spec": {"replicas": self.llm_replicas}}
        raise AssertionError(f"unexpected GET {path}")

    def scale(self, path, replicas):
        self.scaled.append((path, replicas))
        if path.endswith("llama-server"):
            self.llm_replicas = replicas


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.clock = Clock()
        self.wall = 2_000_000_000.0
        self.controller = demand.Controller(self.api, demand.Settings(idle_grace_seconds=GRACE),
                                            clock=self.clock, wall_clock=lambda: self.wall)
        logger = demand.log
        self._level = logger.level
        logger.setLevel("CRITICAL")
        self.addCleanup(logger.setLevel, self._level)

    def llm_scales(self):
        return [r for p, r in self.api.scaled if p.endswith("llama-server")]

    def test_lists_both_kinds_with_the_request_selector(self):
        self.api.requests["statefulsets"] = [workload("db")]
        self.assertTrue(self.controller.run_once())
        self.assertEqual(self.api.last_selector, "homelab.local/llm=requested")
        self.assertEqual(self.controller.requests, ["StatefulSet app/db"])

    def test_request_starts_stopped_llm_once(self):
        self.api.llm_replicas = 0
        self.api.requests["deployments"] = [workload("hello")]
        self.controller.run_once()
        self.controller.run_once()
        self.assertEqual(self.llm_scales(), [1])
        self.assertEqual(self.api.scaled[0][0], "/apis/apps/v1/namespaces/llm/deployments/llama-server")

    def test_llm_stops_only_after_grace(self):
        self.controller.run_once()
        self.clock.now += GRACE - 1
        self.controller.run_once()
        self.assertEqual(self.llm_scales(), [])
        self.clock.now += 1
        self.controller.run_once()
        self.assertEqual(self.llm_scales(), [0])

    def test_llm_mode_scales_chat_workloads_of_both_kinds(self):
        self.api.mode = "llm"
        self.api.llm_replicas = 0
        self.api.chat["statefulsets"] = [workload("open-webui", "chat", replicas=0, labels={})]
        self.api.chat["deployments"] = [workload("open-webui-redis", "chat", replicas=0, labels={}),
                                        workload("already", "chat", replicas=1, labels={})]
        self.controller.run_once()
        self.assertIn(("/apis/apps/v1/namespaces/chat/statefulsets/open-webui", 1), self.api.scaled)
        self.assertIn(("/apis/apps/v1/namespaces/chat/deployments/open-webui-redis", 1), self.api.scaled)
        self.assertNotIn(("/apis/apps/v1/namespaces/chat/deployments/already", 1), self.api.scaled)
        self.assertEqual(self.llm_scales(), [1])

    def test_apps_mode_stops_settled_chat_but_spares_a_new_install(self):
        self.api.llm_replicas = 0
        self.api.chat["statefulsets"] = [workload("old", "chat", labels={}, created="2020-01-01T00:00:00Z")]
        # Unix time 2,000,000,000; "new" was created 60 seconds before the loop.
        self.api.chat["deployments"] = [workload("new", "chat", labels={}, created="2033-05-18T03:33:20Z")]
        self.wall = 2_000_000_060
        self.controller.run_once()
        self.assertIn(("/apis/apps/v1/namespaces/chat/statefulsets/old", 0), self.api.scaled)
        self.assertFalse(any(p.endswith("/new") for p, _ in self.api.scaled))
        self.assertIn("leaving new Deployment chat/new", self.controller.chat_note)

    def test_missing_chat_namespace_or_role_is_tolerated(self):
        for status in (403, 404):
            with self.subTest(status=status):
                self.api.chat_error = demand.ApiError(status, "not yet")
                self.assertTrue(self.controller.run_once())
                self.assertIn("not ready", self.controller.chat_note)

    def test_api_failure_keeps_last_decision_and_counts_errors(self):
        self.api.requests["deployments"] = [workload("hello")]
        self.controller.run_once()
        decision = self.controller.decision
        self.api.fail = demand.ApiError(None, "connection refused")
        self.assertFalse(self.controller.run_once())
        self.assertIs(self.controller.decision, decision)
        self.assertIn("llm_demand_errors_total 1", self.controller.render_metrics())

    def test_failed_scale_does_not_advance_the_idle_timer(self):
        self.controller.run_once()
        idle_since = self.controller.idle_since
        self.clock.now += GRACE
        original = self.api.scale
        self.api.scale = lambda *_: (_ for _ in ()).throw(demand.ApiError(500, "boom"))
        self.assertFalse(self.controller.run_once())
        self.api.scale = original
        self.assertEqual(self.controller.idle_since, idle_since)
        self.controller.run_once()
        self.assertEqual(self.llm_scales(), [0])

    def test_unexpected_error_does_not_escape_the_loop(self):
        self.api.fail = KeyError("items")
        self.assertFalse(self.controller.run_once())

    def test_missing_llm_deployment_is_not_scaled(self):
        self.api.llm_replicas = None
        self.api.mode = "llm"
        self.assertTrue(self.controller.run_once())
        self.assertEqual(self.llm_scales(), [])

    def test_metrics_report_mode_requests_and_desired_replicas(self):
        self.api.mode = "llm"
        self.api.requests["deployments"] = [workload("a"), workload("b", replicas=0)]
        self.controller.run_once()
        metrics = self.controller.render_metrics()
        self.assertIn('llm_demand_mode{mode="llm"} 1', metrics)
        self.assertIn('llm_demand_mode{mode="apps"} 0', metrics)
        self.assertIn("llm_demand_requests 1", metrics)
        self.assertIn('llm_demand_desired_replicas{target="llm"} 1', metrics)
        self.assertIn('llm_demand_desired_replicas{target="chat"} 1', metrics)
        self.assertNotIn("llm_demand_last_success_timestamp_seconds 0.000", metrics)
        self.assertTrue(self.controller.ready())

    def test_health_turns_bad_when_the_loop_stalls(self):
        self.controller.run_once()
        self.assertTrue(self.controller.healthy())
        self.clock.now += 10_000
        self.assertFalse(self.controller.healthy())
        self.assertFalse(self.controller.ready())


class ApiTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(demand.ssl, "create_default_context")
        patcher.start()
        self.addCleanup(patcher.stop)
        token = patch.object(Path, "read_text", return_value="secret-token\n")
        token.start()
        self.addCleanup(token.stop)
        self.api = demand.Api(host="10.96.0.1", port="443", account=Path("/sa"))

    def test_scale_uses_merge_patch_on_the_scale_subresource(self):
        captured = {}

        def urlopen(request, **_kwargs):
            captured["request"] = request
            return io.BytesIO(b'{"spec": {"replicas": 0}}')

        with patch.object(urllib.request, "urlopen", side_effect=urlopen):
            self.api.scale("/apis/apps/v1/namespaces/llm/deployments/llama-server", 0)
        request = captured["request"]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.full_url, "https://10.96.0.1:443/apis/apps/v1/namespaces/llm/"
                                           "deployments/llama-server/scale?fieldManager=llm-demand")
        self.assertEqual(request.get_header("Content-type"), "application/merge-patch+json")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(json.loads(request.data), {"spec": {"replicas": 0}})

    def test_not_found_is_none_and_other_errors_raise(self):
        def fail(status):
            def urlopen(request, **_kwargs):
                raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(b"denied"))
            return urlopen

        with patch.object(urllib.request, "urlopen", side_effect=fail(404)):
            self.assertIsNone(self.api.get_or_none("/api/v1/namespaces/llm/configmaps/lab-mode"))
        with patch.object(urllib.request, "urlopen", side_effect=fail(403)):
            with self.assertRaises(demand.ApiError) as raised:
                self.api.get_or_none("/api/v1/namespaces/llm/configmaps/lab-mode")
        self.assertEqual(raised.exception.status, 403)
        with patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(demand.ApiError) as raised:
                self.api.get("/apis/apps/v1/deployments")
        self.assertIsNone(raised.exception.status)


class ManifestTests(unittest.TestCase):
    """Plain-text checks: the repository has no YAML parser dependency."""

    def test_llama_server_replicas_are_left_to_the_controller(self):
        manifest = (ROOT / "kubernetes/infrastructure/llm/deployment.yaml").read_text()
        self.assertNotRegex(manifest, r"(?m)^\s*replicas:")

    def test_controller_is_part_of_the_llm_kustomization(self):
        kustomization = (ROOT / "kubernetes/infrastructure/llm/kustomization.yaml").read_text()
        self.assertIn("  - demand\n", kustomization)
        self.assertIn("controller.py", (DEMAND / "kustomization.yaml").read_text())

    def test_controller_image_is_pinned_to_a_patch_release(self):
        self.assertRegex((DEMAND / "deployment.yaml").read_text(),
                         r"image: python:3\.\d+\.\d+-alpine\d+\.\d+\n")

    def test_chat_drift_detection_ignores_replicas(self):
        release = (ROOT / "kubernetes/infrastructure/chat/helm-release.yaml").read_text()
        for kind in ("StatefulSet", "Deployment"):
            self.assertIn(f'- paths: ["/spec/replicas"]\n        target:\n          kind: {kind}\n', release)


if __name__ == "__main__":
    unittest.main()
