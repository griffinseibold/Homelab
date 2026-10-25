"""S3 credential provisioning regressions against a fake kubectl and Garage; no cluster access."""
import base64
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
spec = importlib.util.spec_from_file_location("ml_credentials", ROOT / "scripts/ml-credentials.py")
creds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(creds)
REAL_KUBECTL = creds.kubectl


class FakeGarage:
    """The subset of Garage's admin API v2 that the script uses, as `garage json-api` answers it."""

    def __init__(self):
        self.buckets = {name: {"id": f"bucket-{name}", "globalAliases": [name], "keys": []}
                        for name in ("datasets", "checkpoints", "mlflow-artifacts")}
        self.keys = {}
        self.counter = 0
        self.ignore_grants = False

    def call(self, endpoint, body):
        if endpoint == "ListKeys":
            return [{"id": key["accessKeyId"], "name": key["name"]} for key in self.keys.values()]
        if endpoint == "CreateKey":
            self.counter += 1
            key = {"accessKeyId": f"GK{self.counter:024x}", "secretAccessKey": f"secret-{self.counter}",
                   "name": body["name"], "buckets": []}
            self.keys[key["accessKeyId"]] = key
            return copy.deepcopy(key)
        if endpoint == "GetKeyInfo":
            key = copy.deepcopy(self.keys[body["id"]])
            if not body.get("showSecretKey"):
                key["secretAccessKey"] = None
            return key
        if endpoint == "GetBucketInfo":
            matches = [b for b in self.buckets.values() if body["globalAlias"] in b["globalAliases"]]
            if not matches:
                raise subprocess.CalledProcessError(1, ["garage", "json-api", endpoint])
            return copy.deepcopy(matches[0])
        if endpoint == "AllowBucketKey":
            bucket = next(b for b in self.buckets.values() if b["id"] == body["bucketId"])
            if not self.ignore_grants:
                entry = next((k for k in bucket["keys"] if k["accessKeyId"] == body["accessKeyId"]), None)
                if entry is None:
                    entry = {"accessKeyId": body["accessKeyId"], "permissions": {}}
                    bucket["keys"].append(entry)
                for name, value in body["permissions"].items():
                    if value:
                        entry["permissions"][name] = True
            return copy.deepcopy(bucket)
        raise AssertionError(f"unexpected Garage endpoint {endpoint}")


class FakeCluster:
    def __init__(self):
        self.garage = FakeGarage()
        self.namespaces = {"mlflow", "crosswalk", "ml-storage"}
        self.secrets = {}
        self.calls = []

    def __call__(self, *args, stdin=None):
        args = list(args)
        self.calls.append((args, stdin))
        if args[:2] == ["get", "namespace"]:
            return f"namespace/{args[2]}\n" if args[2] in self.namespaces else ""
        if "exec" in args:
            assert args[:2] == ["-n", "ml-storage"] and args[args.index("--") + 1:][:2] == ["/garage", "json-api"]
            endpoint = args[args.index("json-api") + 1]
            assert args[-1] == "-"
            return json.dumps(self.garage.call(endpoint, json.loads(stdin)))
        if args[0] == "apply":
            item = json.loads(stdin)
            self.secrets[(item["metadata"]["namespace"], item["metadata"]["name"])] = item
            return ""
        raise AssertionError(f"unexpected kubectl call {args}")


class MlCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        patcher = patch.object(creds, "kubectl", side_effect=self.cluster)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = creds.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_ok(self, *argv):
        code, out, err = self.invoke(*argv)
        self.assertEqual(code, 0, out + err)
        return out

    def secret(self, namespace):
        item = self.cluster.secrets[(namespace, "s3-credentials")]
        return {name: base64.b64decode(value).decode() for name, value in item["data"].items()}

    def test_kubectl_targets_the_dev_context(self):
        with patch.object(creds, "run") as run:
            run.return_value.stdout = ""
            REAL_KUBECTL("get", "namespace", "x")
        args = run.call_args.args
        self.assertEqual(args[:3], ("kubectl", "--context", "kind-homelab-dev"))

    def test_creates_key_grants_default_buckets_and_writes_secret(self):
        out = self.assert_ok("crosswalk")
        key = next(iter(self.cluster.garage.keys.values()))
        self.assertEqual(key["name"], "k8s-crosswalk")
        self.assertEqual(self.secret("crosswalk"), {"AWS_ACCESS_KEY_ID": key["accessKeyId"],
                                                    "AWS_SECRET_ACCESS_KEY": key["secretAccessKey"]})
        buckets = self.cluster.garage.buckets
        for name in ("datasets", "checkpoints"):
            self.assertEqual(buckets[name]["keys"][0]["permissions"], {"read": True, "write": True})
        self.assertEqual(buckets["mlflow-artifacts"]["keys"], [])
        self.assertIn("http://s3.ml-storage.svc.cluster.local:9000", out)

    def test_rerun_reuses_the_key_and_rewrites_the_same_secret(self):
        self.assert_ok("crosswalk")
        first = self.secret("crosswalk")
        del self.cluster.secrets[("crosswalk", "s3-credentials")]  # e.g. after a rebuild
        out = self.assert_ok("crosswalk", "--bucket", "datasets")
        self.assertIn("Reusing", out)
        self.assertEqual(len(self.cluster.garage.keys), 1)
        self.assertEqual(self.secret("crosswalk"), first)

    def test_explicit_buckets_and_separate_keys_per_namespace(self):
        self.assert_ok("mlflow", "--bucket", "mlflow-artifacts")
        self.assert_ok("crosswalk")
        self.assertEqual(len(self.cluster.garage.keys), 2)
        artifacts = self.cluster.garage.buckets["mlflow-artifacts"]["keys"]
        self.assertEqual([k["accessKeyId"] for k in artifacts], [self.secret("mlflow")["AWS_ACCESS_KEY_ID"]])
        self.assertNotEqual(self.secret("mlflow"), self.secret("crosswalk"))

    def test_secrets_never_appear_on_a_command_line(self):
        self.assert_ok("crosswalk")
        secret = self.secret("crosswalk")["AWS_SECRET_ACCESS_KEY"]
        self.assertFalse(any(secret in " ".join(args) for args, _ in self.cluster.calls))
        apply = next(args for args, _ in self.cluster.calls if args[0] == "apply")
        self.assertIn("--server-side", apply)
        self.assertEqual(apply[-2:], ["-f", "-"])

    def test_missing_namespace_fails_before_touching_garage(self):
        code, _, err = self.invoke("nowhere")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertEqual(self.cluster.garage.keys, {})
        self.assertFalse(any("exec" in args for args, _ in self.cluster.calls))

    def test_invalid_namespace_and_unknown_bucket_are_rejected(self):
        for argv in (["Bad_Name"], ["crosswalk", "--bucket", "other"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                creds.main(argv)
        self.assertEqual(self.cluster.calls, [])

    def test_unexpected_garage_response_fails_with_the_response(self):
        original = self.cluster.garage.call
        def broken(endpoint, body):
            return {"unexpected": True} if endpoint == "ListKeys" else original(endpoint, body)
        self.cluster.garage.call = broken
        code, _, err = self.invoke("crosswalk")
        self.assertEqual(code, 1)
        self.assertIn("Unexpected Garage ListKeys response", err)
        self.assertIn('"unexpected": true', err)
        self.assertEqual(self.cluster.secrets, {})

    def test_create_response_without_secret_is_read_back(self):
        original = self.cluster.garage.call
        def no_secret(endpoint, body):
            response = original(endpoint, body)
            if endpoint == "CreateKey":
                response["secretAccessKey"] = None
            return response
        self.cluster.garage.call = no_secret
        self.assert_ok("crosswalk")
        key = next(iter(self.cluster.garage.keys.values()))
        self.assertEqual(self.secret("crosswalk")["AWS_SECRET_ACCESS_KEY"], key["secretAccessKey"])

    def test_grant_that_did_not_take_effect_fails_without_a_secret(self):
        self.cluster.garage.ignore_grants = True
        code, _, err = self.invoke("crosswalk")
        self.assertEqual(code, 1)
        self.assertIn("did not grant", err)
        self.assertEqual(self.cluster.secrets, {})

    def test_duplicate_key_names_are_refused(self):
        self.cluster.garage.keys = {
            "GK1": {"accessKeyId": "GK1", "secretAccessKey": "a", "name": "k8s-crosswalk"},
            "GK2": {"accessKeyId": "GK2", "secretAccessKey": "b", "name": "k8s-crosswalk"},
        }
        code, _, err = self.invoke("crosswalk")
        self.assertEqual(code, 1)
        self.assertIn("2 keys named k8s-crosswalk", err)

    def test_garage_unavailable_is_reported(self):
        def unavailable(*args, stdin=None):
            if "exec" in args:
                raise subprocess.CalledProcessError(1, ["kubectl", "exec"])
            return self.cluster(*args, stdin=stdin)
        with patch.object(creds, "kubectl", side_effect=unavailable):
            code, _, err = self.invoke("crosswalk")
        self.assertEqual(code, 1)
        self.assertIn("Is Garage running?", err)


if __name__ == "__main__":
    unittest.main()
