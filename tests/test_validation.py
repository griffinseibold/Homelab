"""Regression cases for failures that Kustomize alone cannot catch."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate-manifests.py"
SPEC = importlib.util.spec_from_file_location("validate_manifests", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name).resolve()
        self.root = self.repository / "kubernetes/example"
        self.root.mkdir(parents=True)
        self.roots = {self.root}

    def flux(self, name="example", namespace="flux-system", dependencies=()):
        return {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"path": "./kubernetes/example", "dependsOn": list(dependencies)},
        }

    def test_nested_duplicate_yaml_keys_fail(self):
        with self.assertRaisesRegex(yaml.YAMLError, "duplicate key"):
            validator.load_documents("spec:\n  replicas: 1\n  replicas: 2\n")

    def test_multidocument_yaml_is_preserved(self):
        self.assertEqual(validator.load_documents("name: first\n---\nname: second\n"), [
            {"name": "first"}, {"name": "second"},
        ])

    def test_unreferenced_manifest_is_rejected(self):
        sources = {self.root / "deployment.yaml": [{"apiVersion": "apps/v1", "kind": "Deployment"}]}
        with self.assertRaisesRegex(ValueError, "not referenced"):
            validator.check_coverage(sources)
        sources[self.root / "kustomization.yaml"] = [{"resources": ["deployment.yaml"]}]
        validator.check_coverage(sources)

    def test_kind_config_is_not_treated_as_a_kubernetes_manifest(self):
        validator.check_coverage({self.root / "dev.yaml": [{"apiVersion": "kind.x-k8s.io/v1alpha4", "kind": "Cluster"}]})

    def test_flux_path_must_name_a_local_kustomization(self):
        for path in ("./kubernetes/missing", "../outside"):
            with self.subTest(path=path):
                document = self.flux()
                document["spec"]["path"] = path
                with self.assertRaisesRegex(ValueError, "local Kustomize root"):
                    validator.check_flux_graph([document], self.repository, self.roots)

    def test_unknown_dependency_fails(self):
        with self.assertRaisesRegex(ValueError, "missing dependency"):
            validator.check_flux_graph([self.flux(dependencies=[{"name": "missing"}])], self.repository, self.roots)

    def test_dependency_cycle_fails(self):
        documents = [
            self.flux("a", dependencies=[{"name": "b"}]),
            self.flux("b", dependencies=[{"name": "a"}]),
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            validator.check_flux_graph(documents, self.repository, self.roots)

    def test_cross_namespace_dependencies_are_resolved(self):
        documents = [
            self.flux("a", dependencies=[{"name": "b", "namespace": "other"}]),
            self.flux("b", namespace="other"),
        ]
        validator.check_flux_graph(documents, self.repository, self.roots)

    def test_flux_can_manage_its_own_directory(self):
        validator.check_flux_graph([self.flux("flux-system")], self.repository, self.roots)

    def test_unknown_and_misspelled_kinds_are_not_silently_skipped(self):
        documents = [
            {"apiVersion": "apps/v99", "kind": "Deployment"},
            {"apiVersion": "apps/v1", "kind": "Deploymnet"},
            {"apiVersion": "example.com/v1", "kind": "Unknown"},
            {"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor"},
        ]
        native, skipped = validator.partition_schemas(documents)
        self.assertEqual(native, documents[:3])
        self.assertEqual(skipped, {("monitoring.coreos.com/v1", "ServiceMonitor"): 1})


if __name__ == "__main__":
    unittest.main()
