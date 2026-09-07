#!/usr/bin/env python3
"""Validate repository YAML, Kustomize builds and local Flux wiring."""

from collections import Counter
from pathlib import Path
import subprocess
import tempfile

import yaml


KUSTOMIZATION_NAMES = {"kustomization.yaml", "kustomization.yml", "Kustomization"}
# These CR instances and CRD definitions do not have schemas in kubeconform's
# Kubernetes 1.36 registry.
# Keep skips explicit: a new or misspelled native kind must still fail.
# Their YAML/builds and the Flux graph are checked; Helm charts are not rendered.
CUSTOM_SCHEMA_SKIPS = {
    ("apiextensions.k8s.io/v1", "CustomResourceDefinition"),
    ("gateway.envoyproxy.io/v1alpha1", "EnvoyProxy"),
    ("gateway.networking.k8s.io/v1", "Gateway"),
    ("gateway.networking.k8s.io/v1", "GatewayClass"),
    ("gateway.networking.k8s.io/v1", "HTTPRoute"),
    ("helm.toolkit.fluxcd.io/v2", "HelmRelease"),
    ("kustomize.toolkit.fluxcd.io/v1", "Kustomization"),
    ("monitoring.coreos.com/v1", "PrometheusRule"),
    ("monitoring.coreos.com/v1", "ServiceMonitor"),
    ("source.toolkit.fluxcd.io/v1", "GitRepository"),
    ("source.toolkit.fluxcd.io/v1", "HelmRepository"),
    ("source.toolkit.fluxcd.io/v1", "OCIRepository"),
}


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in keys:
                    raise yaml.constructor.ConstructorError(
                        "while reading mapping", node.start_mark,
                        f"duplicate key: {key!r}", key_node.start_mark,
                    )
                keys.add(key)
            except TypeError as error:
                raise ValueError(f"Unhashable YAML key at {key_node.start_mark}") from error
        return super().construct_mapping(node, deep=deep)


def load_documents(text):
    return [document for document in yaml.load_all(text, Loader=UniqueKeyLoader) if document is not None]


def check_coverage(sources):
    """Reject manifests that would otherwise never appear in a Kustomize build."""
    referenced = set()
    for path, documents in sources.items():
        if path.name not in KUSTOMIZATION_NAMES:
            continue
        for document in documents:
            for field in ("resources", "bases", "components"):
                for resource in document.get(field, []):
                    referenced.add((path.parent / resource).resolve())
            for patch in document.get("patches", []):
                if isinstance(patch, dict) and "path" in patch:
                    referenced.add((path.parent / patch["path"]).resolve())
    for path, documents in sources.items():
        if path.name in KUSTOMIZATION_NAMES:
            continue
        for document in documents:
            if not isinstance(document, dict) or "apiVersion" not in document or "kind" not in document:
                continue
            if document["apiVersion"].startswith("kind.x-k8s.io/"):
                continue
            if path.resolve() not in referenced:
                raise ValueError(f"Manifest is not referenced by a Kustomization: {path}")


def check_flux_graph(documents, repository, roots):
    flux = {}
    for document in documents:
        if document.get("kind") != "Kustomization" or not document.get("apiVersion", "").startswith("kustomize.toolkit.fluxcd.io/"):
            continue
        metadata = document["metadata"]
        key = (metadata.get("namespace", "default"), metadata["name"])
        if key in flux:
            raise ValueError(f"Duplicate Flux Kustomization: {key}")
        flux[key] = document

    graph = {}
    for key, document in flux.items():
        spec = document["spec"]
        path = (repository / spec.get("path", ".")).resolve()
        if not path.is_relative_to(repository.resolve()) or path not in roots:
            raise ValueError(f"Flux {key}: path does not name a local Kustomize root: {spec.get('path')}")
        graph[key] = []
        for dependency in spec.get("dependsOn", []):
            target = (dependency.get("namespace", key[0]), dependency["name"])
            if target not in flux:
                raise ValueError(f"Flux {key}: missing dependency {target}")
            graph[key].append(target)

    visited, active = set(), set()

    def visit(key):
        if key in active:
            raise ValueError(f"Flux dependency cycle at {key}")
        if key in visited:
            return
        active.add(key)
        for dependency in graph[key]:
            visit(dependency)
        active.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)


def partition_schemas(documents):
    native, skipped = [], Counter()
    for document in documents:
        identity = (document["apiVersion"], document["kind"])
        if identity in CUSTOM_SCHEMA_SKIPS:
            skipped[identity] += 1
        else:
            native.append(document)
    return native, skipped


def main():
    repository = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository,
    ).decode().split("\0")
    paths = sorted({
        repository / name for name in tracked
        if name and (Path(name).suffix in (".yaml", ".yml") or Path(name).name == "Kustomization")
        and (repository / name).is_file()
    })
    sources = {}
    for path in paths:
        try:
            sources[path] = load_documents(path.read_text())
        except (yaml.YAMLError, ValueError) as error:
            raise ValueError(f"{path.relative_to(repository)}: {error}") from error
    print(f"Parsed {len(paths)} YAML files with duplicate-key checks.", flush=True)
    kubernetes_sources = {path: docs for path, docs in sources.items() if path.is_relative_to(repository / "kubernetes")}
    check_coverage(kubernetes_sources)
    roots = {path.parent.resolve() for path in kubernetes_sources if path.name in KUSTOMIZATION_NAMES}
    if not roots:
        raise ValueError("No Kustomize roots found")

    skipped_total = Counter()
    with tempfile.TemporaryDirectory(prefix="homelab-validation-") as directory:
        directory = Path(directory)
        native_paths = []
        for index, root in enumerate(sorted(roots)):
            print(f"Building {root.relative_to(repository)}", flush=True)
            build = subprocess.check_output(["kubectl", "kustomize", str(root)], text=True)
            documents = load_documents(build)
            check_flux_graph(documents, repository, roots)
            native, skipped = partition_schemas(documents)
            skipped_total.update(skipped)
            if native:
                native_path = directory / f"{index}-native.yaml"
                native_path.write_text(yaml.safe_dump_all(native))
                native_paths.append(str(native_path))
        if not native_paths:
            raise ValueError("No native Kubernetes resources found to validate")
        subprocess.run([
            "kubeconform", "-strict", "-summary", "-kubernetes-version", "1.36.0",
            *native_paths,
        ], check=True)

    print("Custom-resource schemas skipped (counts include overlapping Kustomize builds):")
    for (version, kind), count in sorted(skipped_total.items()):
        print(f"  {version} {kind}: {count}")
    print(f"All {len(roots)} Kustomize roots built; Flux paths/dependencies and native schemas passed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, yaml.YAMLError, subprocess.CalledProcessError, FileNotFoundError) as error:
        raise SystemExit(f"Manifest validation failed: {error}")
