"""Lab mode switch regressions against a fake kubectl; no cluster access."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lab_mode", ROOT / "scripts/lab-mode.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
REAL_KUBECTL = lab.kubectl

AUTOMATED = {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]}
KINDS = {"deployment.apps": "Deployment", "statefulset.apps": "StatefulSet",
         "application.argoproj.io": "Application"}


def merge(target, body):
    """RFC 7386 JSON merge patch."""
    for key, value in body.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def pointer(path):
    return [part.replace("~1", "/").replace("~0", "~") for part in path.lstrip("/").split("/")]


def json_patch(target, operations):
    """The subset of RFC 6902 the script uses; all-or-nothing like the API server."""
    result = copy.deepcopy(target)
    for operation in operations:
        *parents, last = pointer(operation["path"])
        node = result
        for part in parents:
            node = node[part]
        if operation["op"] == "test":
            if node.get(last) != operation["value"]:
                raise subprocess.CalledProcessError(1, ["kubectl", "patch"])
        elif operation["op"] == "add":
            node[last] = copy.deepcopy(operation["value"])
        elif operation["op"] == "remove":
            if last not in node:
                raise subprocess.CalledProcessError(1, ["kubectl", "patch"])
            del node[last]
    target.clear()
    target.update(result)


class FakeCluster:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_on = None  # (kind, name) to fail the next patch of
        self.healthy = True

    def add(self, kind, namespace, name, spec=None, labels=None, annotations=None, status=None):
        item = {"kind": kind, "metadata": {"name": name, "namespace": namespace}, "spec": spec or {}}
        if labels:
            item["metadata"]["labels"] = labels
        if annotations:
            item["metadata"]["annotations"] = annotations
        if status:
            item["status"] = status
        self.objects[(kind, namespace, name)] = item
        return item

    def obj(self, kind, namespace, name):
        return self.objects[(kind, namespace, name)]

    def __call__(self, *args, stdin=None):
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if verb == "get" and args[1] == "--raw":
            if not self.healthy:
                raise subprocess.CalledProcessError(1, ["kubectl", "get", "--raw"])
            return "ok"
        if verb == "get" and args[1] == "configmap":
            item = self.objects.get(("ConfigMap", args[4], args[2]))
            return json.dumps(item) if item else ""
        if verb == "get":
            kinds = {{"deployments.apps": "Deployment", "statefulsets.apps": "StatefulSet",
                      "applications.argoproj.io": "Application"}[r] for r in args[1].split(",")}
            namespace = args[args.index("-n") + 1] if "-n" in args else None
            selector = args[args.index("-l") + 1] if "-l" in args else None
            items = []
            for (kind, ns, _), item in sorted(self.objects.items()):
                if kind not in kinds or (namespace and ns != namespace):
                    continue
                if selector:
                    key, value = selector.split("=")
                    if item["metadata"].get("labels", {}).get(key) != value:
                        continue
                items.append(copy.deepcopy(item))
            return json.dumps({"items": items})
        if verb == "patch":
            kind, name = KINDS[args[1]], args[2]
            namespace = args[args.index("-n") + 1]
            if self.fail_on == (kind, name):
                self.fail_on = None
                raise subprocess.CalledProcessError(1, ["kubectl", "patch"])
            body = json.loads(args[args.index("-p") + 1])
            target = self.obj(kind, namespace, name)
            if args[args.index("--type") + 1] == "json":
                json_patch(target, body)
            else:
                merge(target, body)
            # The API server omits an emptied annotations map.
            if not target["metadata"].get("annotations"):
                target["metadata"].pop("annotations", None)
            return ""
        if verb == "apply":
            item = json.loads(stdin)
            meta = item["metadata"]
            self.objects[(item["kind"], meta["namespace"], meta["name"])] = item
            return ""
        raise AssertionError(f"unexpected kubectl call {args}")


class LabModeTests(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        c = self.cluster
        c.add("Application", "argocd", "hello-crud",
              {"destination": {"server": "https://kubernetes.default.svc", "namespace": "hello-crud"},
               "syncPolicy": copy.deepcopy(AUTOMATED)})
        c.add("Application", "argocd", "manual",
              {"destination": {"name": "in-cluster", "namespace": "manual"}})
        c.add("Application", "argocd", "sneaky",
              {"destination": {"namespace": "monitoring"}, "syncPolicy": {"automated": {}}})
        c.add("Deployment", "hello-crud", "web", {"replicas": 3},
              labels={"homelab.local/llm": "requested"})
        c.add("StatefulSet", "hello-crud", "db", {})  # Missing replicas means 1.
        c.add("Deployment", "manual", "idle", {"replicas": 0})
        c.add("Deployment", "monitoring", "grafana", {"replicas": 1})
        c.add("Deployment", "llm", "llama-server", {"replicas": 0})
        c.add("StatefulSet", "chat", "open-webui", {"replicas": 0})
        self.original = copy.deepcopy(c.objects)
        patcher = patch.object(lab, "kubectl", side_effect=c)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lab.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_ok(self, *argv):
        code, out, err = self.invoke(*argv)
        self.assertEqual(code, 0, out + err)
        return out, err

    def test_kubectl_targets_the_dev_context(self):
        with patch.object(lab, "output", return_value="") as output:
            REAL_KUBECTL("get", "pods")
        args = output.call_args.args
        self.assertEqual(args[:3], ("kubectl", "--context", "kind-homelab-dev"))
        self.assertLess(args.index("--context"), args.index("get"))

    def test_llm_mode_pauses_apps_stops_their_workloads_and_sets_mode(self):
        out, err = self.assert_ok("llm")
        c = self.cluster
        app = c.obj("Application", "argocd", "hello-crud")
        self.assertNotIn("automated", app["spec"]["syncPolicy"])
        self.assertEqual(app["spec"]["syncPolicy"]["syncOptions"], ["CreateNamespace=true"])
        self.assertEqual(json.loads(app["metadata"]["annotations"][lab.SAVED_POLICY]), AUTOMATED)
        self.assertEqual(c.obj("Deployment", "hello-crud", "web")["spec"]["replicas"], 0)
        self.assertEqual(c.obj("Deployment", "hello-crud", "web")["metadata"]["annotations"][lab.SAVED_REPLICAS], "3")
        self.assertEqual(c.obj("StatefulSet", "hello-crud", "db")["metadata"]["annotations"][lab.SAVED_REPLICAS], "1")
        self.assertEqual(c.obj("Deployment", "manual", "idle")["metadata"]["annotations"][lab.SAVED_REPLICAS], "0")
        self.assertEqual(c.obj("ConfigMap", "llm", "lab-mode")["data"], {"mode": "llm"})
        self.assertIn("http://chat.localhost:8080", out)
        self.assertIn("http://llm.localhost:8080/v1", out)

    def test_platform_namespaces_are_never_touched(self):
        _, err = self.assert_ok("llm")
        self.assertIn("platform namespace monitoring", err)
        for key in (("Deployment", "monitoring", "grafana"), ("Deployment", "llm", "llama-server"),
                    ("StatefulSet", "chat", "open-webui")):
            self.assertEqual(self.cluster.objects[key], self.original[key])
        patched = [call for call in self.cluster.calls if call[0] == "patch" and call[1] != "application.argoproj.io"]
        self.assertTrue(all(call[call.index("-n") + 1] in ("hello-crud", "manual") for call in patched))

    def test_ml_platform_namespaces_are_never_stopped(self):
        c = self.cluster
        c.add("Application", "argocd", "crosswalk",
              {"destination": {"server": "https://kubernetes.default.svc", "namespace": "crosswalk"},
               "syncPolicy": copy.deepcopy(AUTOMATED)})
        c.add("Application", "argocd", "misplaced",
              {"destination": {"namespace": "mlflow"}, "syncPolicy": copy.deepcopy(AUTOMATED)})
        c.add("Deployment", "crosswalk", "api", {"replicas": 1})
        storage = c.add("Deployment", "ml-storage", "garage", {"replicas": 1})
        tracking = c.add("Deployment", "mlflow", "mlflow", {"replicas": 1})
        before = copy.deepcopy((storage, tracking))
        _, err = self.assert_ok("llm")
        self.assertIn("platform namespace mlflow", err)
        self.assertEqual((storage, tracking), before)
        self.assertEqual(c.obj("Deployment", "crosswalk", "api")["spec"]["replicas"], 0)
        storage["metadata"]["annotations"] = {lab.SAVED_REPLICAS: "5"}  # Never set by lab mode.
        self.assert_ok("apps")
        self.assertEqual(storage["spec"]["replicas"], 1)
        self.assertEqual(c.obj("Deployment", "crosswalk", "api")["spec"]["replicas"], 1)

    def test_round_trip_restores_everything_exactly(self):
        self.assert_ok("llm")
        self.assert_ok("apps")
        c = self.cluster
        self.assertEqual(c.obj("ConfigMap", "llm", "lab-mode")["data"], {"mode": "apps"})
        for key, original in self.original.items():
            current = copy.deepcopy(c.objects[key])
            if key == ("StatefulSet", "hello-crud", "db"):
                # The missing field is restored as its explicit default.
                self.assertEqual(current["spec"].pop("replicas"), 1)
            self.assertEqual(current, original, key)

    def test_switching_to_llm_twice_never_loses_the_saved_state(self):
        self.assert_ok("llm")
        self.assert_ok("llm")
        app = self.cluster.obj("Application", "argocd", "hello-crud")
        self.assertEqual(json.loads(app["metadata"]["annotations"][lab.SAVED_POLICY]), AUTOMATED)
        web = self.cluster.obj("Deployment", "hello-crud", "web")
        self.assertEqual(web["metadata"]["annotations"][lab.SAVED_REPLICAS], "3")
        self.assert_ok("apps")
        self.assertEqual(self.cluster.obj("Application", "argocd", "hello-crud")["spec"]["syncPolicy"], AUTOMATED)
        self.assertEqual(self.cluster.obj("Deployment", "hello-crud", "web")["spec"]["replicas"], 3)

    def test_sync_reenabled_by_a_parent_during_the_switch_is_paused_again(self):
        original = lab.pause_application
        healed = []
        def pause_then_parent_heals(app):
            original(app)
            if app["metadata"]["name"] == "hello-crud" and not healed:
                healed.append(True)
                live = self.cluster.obj("Application", "argocd", "hello-crud")
                live["spec"]["syncPolicy"]["automated"] = {"selfHeal": True}
        with patch.object(lab, "pause_application", side_effect=pause_then_parent_heals):
            self.assert_ok("llm")
        app = self.cluster.obj("Application", "argocd", "hello-crud")
        self.assertNotIn("automated", app["spec"]["syncPolicy"])
        self.assertEqual(json.loads(app["metadata"]["annotations"][lab.SAVED_POLICY]), AUTOMATED)

    def test_application_without_sync_policy_is_restored_without_one(self):
        self.assert_ok("llm")
        manual = self.cluster.obj("Application", "argocd", "manual")
        self.assertEqual(manual["metadata"]["annotations"][lab.SAVED_POLICY], "null")
        self.assert_ok("apps")
        self.assertNotIn("syncPolicy", self.cluster.obj("Application", "argocd", "manual")["spec"])

    def test_reenabled_automated_sync_is_paused_again_without_overwriting(self):
        self.assert_ok("llm")
        app = self.cluster.obj("Application", "argocd", "hello-crud")
        app["spec"]["syncPolicy"]["automated"] = {"prune": False}  # e.g. an ApplicationSet
        self.assert_ok("llm")
        self.assertNotIn("automated", app["spec"]["syncPolicy"])
        self.assertEqual(json.loads(app["metadata"]["annotations"][lab.SAVED_POLICY]), AUTOMATED)

    def test_partial_failure_switching_to_llm_can_be_rerun_or_reverted(self):
        self.cluster.fail_on = ("StatefulSet", "db")
        code, _, err = self.invoke("llm")
        self.assertEqual(code, 1)
        self.assertIn("safe to rerun", err)
        self.assertNotIn(("ConfigMap", "llm", "lab-mode"), self.cluster.objects)
        self.assert_ok("llm")
        self.assertEqual(self.cluster.obj("StatefulSet", "hello-crud", "db")["spec"]["replicas"], 0)
        self.assert_ok("apps")
        self.assertEqual(self.cluster.obj("Deployment", "hello-crud", "web")["spec"]["replicas"], 3)

    def test_partial_failure_switching_to_apps_keeps_state_until_rerun(self):
        self.assert_ok("llm")
        self.cluster.fail_on = ("Application", "hello-crud")
        self.assertEqual(self.invoke("apps")[0], 1)
        app = self.cluster.obj("Application", "argocd", "hello-crud")
        self.assertIn(lab.SAVED_POLICY, app["metadata"]["annotations"])
        self.assertEqual(self.cluster.obj("Deployment", "hello-crud", "web")["spec"]["replicas"], 3)
        _, err = self.assert_ok("status")
        self.assertIn("still paused", err)
        self.assert_ok("apps")
        self.assert_ok("apps")  # Nothing left to do; still succeeds.
        self.assertEqual(self.cluster.obj("Application", "argocd", "hello-crud")["spec"]["syncPolicy"], AUTOMATED)

    def test_apps_restores_workloads_of_a_removed_application(self):
        self.assert_ok("llm")
        del self.cluster.objects[("Application", "argocd", "manual")]
        self.cluster.obj("Deployment", "manual", "idle")["metadata"]["annotations"][lab.SAVED_REPLICAS] = "2"
        self.assert_ok("apps")
        idle = self.cluster.obj("Deployment", "manual", "idle")
        self.assertEqual(idle["spec"]["replicas"], 2)
        self.assertNotIn("annotations", idle["metadata"])

    def test_resume_refuses_a_saved_policy_changed_since_it_was_read(self):
        self.assert_ok("llm")
        app = copy.deepcopy(self.cluster.obj("Application", "argocd", "hello-crud"))
        live = self.cluster.obj("Application", "argocd", "hello-crud")
        live["metadata"]["annotations"][lab.SAVED_POLICY] = json.dumps({"automated": {"prune": False}})
        with self.assertRaises(subprocess.CalledProcessError):
            lab.resume_application(app)
        self.assertIn(lab.SAVED_POLICY, live["metadata"]["annotations"])

    def test_unreadable_saved_values_are_left_alone(self):
        self.assert_ok("llm")
        self.cluster.obj("Deployment", "hello-crud", "web")["metadata"]["annotations"][lab.SAVED_REPLICAS] = "lots"
        self.cluster.obj("Application", "argocd", "hello-crud")["metadata"]["annotations"][lab.SAVED_POLICY] = "{"
        _, err = self.assert_ok("apps")
        self.assertIn("unreadable", err)
        self.assertIn(lab.SAVED_POLICY, self.cluster.obj("Application", "argocd", "hello-crud")["metadata"]["annotations"])

    def test_status_reports_mode_requests_chat_and_paused_apps(self):
        c = self.cluster
        c.add("Deployment", "llm", "llm-demand", {"replicas": 1}, status={"readyReplicas": 1})
        c.obj("Deployment", "llm", "llama-server").update(spec={"replicas": 1}, status={"readyReplicas": 0})
        out, _ = self.assert_ok("status")
        self.assertIn("Mode: apps", out)
        self.assertIn("1 desired, 0 ready", out)
        self.assertIn("Demand controller: ready", out)
        self.assertIn("Deployment hello-crud/web", out)
        self.assertIn("Chat: stopped", out)
        self.assertIn("Paused Argo CD applications: none", out)
        self.assert_ok("llm")
        c.obj("StatefulSet", "chat", "open-webui").update(spec={"replicas": 1}, status={"readyReplicas": 1})
        out, _ = self.assert_ok("status")
        self.assertIn("Mode: llm", out)
        self.assertIn("Requesting the LLM: none", out)  # Stopped workloads are not requests.
        self.assertIn("Chat: running\n", out)
        self.assertIn("  hello-crud\n", out)
        self.assertIn("Stopped application workloads: 3", out)

    def test_wait_polls_until_llm_and_chat_are_ready(self):
        c = self.cluster
        c.healthy = False
        clock = iter(range(0, 1000, 5))
        sleeps = []

        def sleep(_seconds):
            sleeps.append(_seconds)
            if len(sleeps) == 2:
                c.healthy = True
                c.obj("StatefulSet", "chat", "open-webui").update(spec={"replicas": 1}, status={"readyReplicas": 1})

        with redirect_stdout(io.StringIO()):
            lab.wait_until_ready(600, sleep=sleep, clock=lambda: next(clock))
        self.assertEqual(len(sleeps), 2)
        c.healthy = False
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "not ready after"):
            lab.wait_until_ready(10, sleep=lambda _: None, clock=lambda: next(clock))


if __name__ == "__main__":
    unittest.main()
