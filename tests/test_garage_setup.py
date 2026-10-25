"""Garage bucket setup Job regressions against a fake admin API; no cluster access."""
import importlib.util
import io
from pathlib import Path
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "garage_setup", ROOT / "kubernetes/infrastructure/ml-storage/setup/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class FakeAdmin:
    def __init__(self, health=None):
        self.health = list(health or [(200, {"status": "healthy", "storageNodes": 1, "storageNodesUp": 1})])
        self.buckets = []
        self.calls = []
        self.create_does_nothing = False
        self.list_response = None

    def call(self, method, endpoint, query=None, body=None):
        self.calls.append((method, endpoint, body))
        if endpoint == "GetClusterHealth":
            return self.health.pop(0) if len(self.health) > 1 else self.health[0]
        if endpoint == "ListBuckets":
            if self.list_response is not None:
                return 200, self.list_response
            return 200, [{"id": f"id-{name}", "globalAliases": [name], "localAliases": []}
                         for name in self.buckets]
        if endpoint == "CreateBucket":
            if not self.create_does_nothing:
                self.buckets.append(body["globalAlias"])
            return 200, {"id": f'id-{body["globalAlias"]}', "globalAliases": [body["globalAlias"]]}
        raise AssertionError(endpoint)


def quiet(function, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = function(*args, **kwargs)
    return result, out.getvalue()


class GarageSetupTests(unittest.TestCase):
    def test_creates_only_missing_buckets_and_is_idempotent(self):
        admin = FakeAdmin()
        admin.buckets = ["datasets"]
        created, _ = quiet(setup.ensure_buckets, admin)
        self.assertEqual(created, ["checkpoints", "mlflow-artifacts"])
        created, out = quiet(setup.ensure_buckets, admin)
        self.assertEqual(created, [])
        self.assertIn("Bucket mlflow-artifacts exists", out)
        creates = [c for c in admin.calls if c[1] == "CreateBucket"]
        self.assertEqual([c[2] for c in creates], [{"globalAlias": "checkpoints"}, {"globalAlias": "mlflow-artifacts"}])

    def test_list_wrapped_in_an_object_is_accepted(self):
        admin = FakeAdmin()
        admin.list_response = {"buckets": [{"id": "x", "globalAliases": list(setup.BUCKETS)}]}
        created, _ = quiet(setup.ensure_buckets, admin)
        self.assertEqual(created, [])

    def test_unexpected_list_shape_fails_with_the_response(self):
        admin = FakeAdmin()
        admin.list_response = [{"name": "datasets"}]
        with self.assertRaisesRegex(setup.SetupError, "Unexpected ListBuckets response.*datasets"):
            quiet(setup.ensure_buckets, admin)
        self.assertFalse(any(c[1] == "CreateBucket" for c in admin.calls))

    def test_create_that_did_nothing_is_caught(self):
        admin = FakeAdmin()
        admin.create_does_nothing = True
        with self.assertRaisesRegex(setup.SetupError, "still missing"):
            quiet(setup.ensure_buckets, admin)

    def test_waits_for_health_then_returns(self):
        admin = FakeAdmin([(None, "connection refused"), (503, "starting"),
                           (200, {"status": "unavailable", "storageNodes": 0}),
                           (200, {"status": "healthy", "storageNodes": 1})])
        sleeps = []
        _, out = quiet(setup.wait_until_healthy, admin, sleep=sleeps.append, clock=lambda: 0, report_every=0)
        self.assertEqual(len(sleeps), 3)
        self.assertIn("no storage node in the layout", out)

    def test_wrong_token_fails_instead_of_waiting(self):
        admin = FakeAdmin([(403, "Forbidden")])
        with self.assertRaisesRegex(setup.SetupError, "rejected the admin token"):
            quiet(setup.wait_until_healthy, admin, sleep=lambda _: self.fail("should not wait"))

    def test_missing_token_fails_before_any_request(self):
        result, out = quiet(setup.main, {"GARAGE_ADMIN_TOKEN": " "})
        self.assertEqual(result, 1)
        self.assertIn("garage Secret is incomplete", out)

    def test_requests_send_the_bearer_token(self):
        admin = setup.Admin("http://garage-admin.invalid:3903/", "token-value", timeout=0.01)
        captured = {}
        def fake_urlopen(request, timeout):
            captured["url"], captured["auth"] = request.full_url, request.get_header("Authorization")
            raise setup.urllib.error.URLError("offline")
        original = setup.urllib.request.urlopen
        setup.urllib.request.urlopen = fake_urlopen
        try:
            status, _ = admin.call("GET", "GetBucketInfo", {"globalAlias": "datasets"})
        finally:
            setup.urllib.request.urlopen = original
        self.assertIsNone(status)
        self.assertEqual(captured["url"], "http://garage-admin.invalid:3903/v2/GetBucketInfo?globalAlias=datasets")
        self.assertEqual(captured["auth"], "Bearer token-value")


if __name__ == "__main__":
    unittest.main()
