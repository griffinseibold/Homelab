"""Kafka and Kueue regressions without network or cluster access.

The JMX agent fetcher runs against a fake download. The manifest checks are
plain text, like the other manifest tests: the repository has no YAML parser
dependency. They pin the contract applications are built against.
"""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
KAFKA = ROOT / "kubernetes/infrastructure/kafka"
KUEUE = ROOT / "kubernetes/infrastructure/kueue"
FLUX = ROOT / "kubernetes/clusters/dev/infrastructure"
spec = importlib.util.spec_from_file_location("fetch_jmx_agent", KAFKA / "fetch-jmx-agent.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)

JAR = b"pretend agent jar\n" * 1000
JAR_SHA256 = hashlib.sha256(JAR).hexdigest()


class FakeDownload:
    def __init__(self, payload=JAR, error=None):
        self.payload = payload
        self.error = error
        self.urls = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.error:
            raise self.error
        return io.BytesIO(self.payload)


def quiet(function, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = function(*args, **kwargs)
    return result, out.getvalue()


class FetchJmxAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "jmx-agent"
        self.target = self.directory / "jmx_prometheus_javaagent-1.6.0.jar"

    def ensure(self, download, expected=JAR_SHA256):
        return quiet(fetch.ensure_agent, "https://example.invalid/agent.jar", expected, self.target,
                     opener=download)

    def test_downloads_verifies_and_then_reuses_the_cached_agent(self):
        download = FakeDownload()
        (downloaded, _) = self.ensure(download)
        self.assertTrue(downloaded)
        self.assertEqual(self.target.read_bytes(), JAR)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o444)
        (downloaded, out) = self.ensure(download)
        self.assertFalse(downloaded)
        self.assertIn("present and verified", out)
        self.assertEqual(len(download.urls), 1)

    def test_mismatched_download_is_discarded_and_never_installed(self):
        with self.assertRaisesRegex(fetch.FetchError, "refusing to use it"):
            self.ensure(FakeDownload(payload=b"tampered"))
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_corrupted_cache_is_replaced(self):
        self.directory.mkdir()
        self.target.write_bytes(b"truncated")
        (downloaded, out) = self.ensure(FakeDownload())
        self.assertTrue(downloaded)
        self.assertIn("does not match", out)
        self.assertEqual(self.target.read_bytes(), JAR)

    def test_corrupted_cache_is_kept_out_of_use_when_the_download_fails(self):
        self.directory.mkdir()
        self.target.write_bytes(b"truncated")
        with self.assertRaisesRegex(fetch.FetchError, "Could not download"):
            self.ensure(FakeDownload(error=urllib.error.URLError("no route to host")))
        self.assertEqual(self.target.read_bytes(), b"truncated")  # Kafka cannot start on it either.

    def test_old_versions_and_partial_downloads_are_removed(self):
        self.directory.mkdir()
        (self.directory / "jmx_prometheus_javaagent-1.5.0.jar").write_bytes(b"old")
        (self.directory / ".download-abc.partial").write_bytes(b"half")
        (self.directory / "notes.txt").write_text("kept")
        self.ensure(FakeDownload())
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["jmx_prometheus_javaagent-1.6.0.jar", "notes.txt"])

    def test_malformed_pinned_hash_is_rejected_before_any_download(self):
        download = FakeDownload()
        with self.assertRaisesRegex(fetch.FetchError, "64 hexadecimal"):
            self.ensure(download, expected="not-a-hash")
        self.assertEqual(download.urls, [])

    def test_manifest_pins_the_agent_url_and_hash_together(self):
        kafka = (KAFKA / "kafka.yaml").read_text()
        url = re.search(r"value: (https://github.com/prometheus/jmx_exporter/releases/download/(\S+)/"
                        r"jmx_prometheus_javaagent-(\S+)\.jar)", kafka)
        self.assertIsNotNone(url)
        self.assertEqual(url.group(2), url.group(3))
        self.assertRegex(kafka, r"name: AGENT_SHA256\n\s+value: [0-9a-f]{64}\n")
        paths = set(re.findall(r"name: AGENT_PATH\n\s+value: (\S+)", kafka))
        self.assertEqual(paths, {f"/var/lib/kafka/data/jmx-agent/jmx_prometheus_javaagent-{url.group(3)}.jar"})


class KafkaContractTests(unittest.TestCase):
    def setUp(self):
        self.properties = dict(
            line.split("=", 1) for line in (KAFKA / "server.properties").read_text().splitlines()
            if line and not line.startswith("#"))
        self.kafka = (KAFKA / "kafka.yaml").read_text()

    def test_single_kraft_node_without_zookeeper(self):
        self.assertEqual(self.properties["process.roles"], "broker,controller")
        self.assertEqual(self.properties["controller.quorum.voters"], "1@127.0.0.1:9093")
        self.assertFalse(any(key.startswith("zookeeper") for key in self.properties))

    def test_clients_get_plaintext_on_the_service_name(self):
        self.assertEqual(self.properties["advertised.listeners"],
                         "PLAINTEXT://kafka.kafka.svc.cluster.local:9092")
        self.assertIn("PLAINTEXT://0.0.0.0:9092", self.properties["listeners"])
        # The controller listener never leaves the pod.
        self.assertIn("CONTROLLER://127.0.0.1:9093", self.properties["listeners"])
        self.assertIn("kind: Service\nmetadata:\n  name: kafka\n", self.kafka)

    def test_applications_create_topics_with_one_replica(self):
        self.assertEqual(self.properties["auto.create.topics.enable"], "false")
        for key in ("default.replication.factor", "offsets.topic.replication.factor",
                    "transaction.state.log.replication.factor", "min.insync.replicas"):
            self.assertEqual(self.properties[key], "1", key)

    def test_retention_is_bounded_by_time_and_size(self):
        self.assertEqual(int(self.properties["log.retention.hours"]), 7 * 24)
        self.assertEqual(int(self.properties["log.retention.bytes"]), 2 * 1024 ** 3)
        self.assertTrue(self.properties["log.dirs"].startswith("/var/lib/kafka/data/"))

    def test_kafka_is_never_on_the_gateway_or_home_network(self):
        for path in sorted(p for p in KAFKA.iterdir() if p.is_file()):
            text = path.read_text()
            self.assertNotIn("HTTPRoute", text, path.name)
            self.assertNotIn("gateway.homelab/", text.replace("No gateway.homelab/access or gateway.homelab/lan", ""),
                             path.name)
            self.assertNotRegex(text, r"type: (NodePort|LoadBalancer)", path.name)

    def test_broker_and_exporter_versions_match(self):
        image = re.search(r"image: apache/kafka:(\d+\.\d+\.\d+)\n", self.kafka)
        self.assertIsNotNone(image)
        exporter = (KAFKA / "exporter.yaml").read_text()
        self.assertIn(f"--kafka.version={image.group(1)}", exporter)
        self.assertRegex(exporter, r"image: danielqsj/kafka-exporter:v\d+\.\d+\.\d+\n")
        self.assertIn(f"Apache Kafka {image.group(1)}",
                      json.loads((KAFKA / "dashboard.json").read_text())["description"])

    def test_pods_are_locked_down(self):
        for name in ("kafka.yaml", "exporter.yaml"):
            text = (KAFKA / name).read_text()
            self.assertIn("automountServiceAccountToken: false", text)
            self.assertIn("runAsNonRoot: true", text)
            self.assertIn("type: RuntimeDefault", text)
            containers = text.count("allowPrivilegeEscalation: false")
            self.assertEqual(containers, text.count("readOnlyRootFilesystem: true"))
            self.assertEqual(containers, text.count('drop: ["ALL"]'))
            self.assertNotIn("privileged: true", text)
        self.assertEqual(self.kafka.count("allowPrivilegeEscalation: false"), 2)  # init + broker
        self.assertIn("enableServiceLinks: false", self.kafka)
        self.assertIn("pod-security.kubernetes.io/enforce: restricted", (KAFKA / "namespace.yaml").read_text())

    def test_volume_is_a_default_class_claim_the_backup_finds(self):
        self.assertRegex(self.kafka, r"kind: PersistentVolumeClaim\nmetadata:\n  name: kafka\n")
        claim = self.kafka.split("---")[0]
        self.assertNotIn("storageClassName", claim)
        self.assertIn("storage: 20Gi", claim)
        self.assertIn("type: Recreate", self.kafka)

    def test_dashboard_queries_only_metrics_that_are_exported(self):
        dashboard = json.loads((KAFKA / "dashboard.json").read_text())
        self.assertEqual(dashboard["uid"], "homelab-kafka")
        # Each rule's metric name with $1 replaced by every alternative its
        # pattern's name=(A|B|C) group allows; lowercaseOutputName is on.
        rules = (KAFKA / "jmx-exporter.yaml").read_text()
        exported = {"kafka_consumergroup_lag", "up"}  # kafka-exporter and Prometheus.
        for pattern, name in re.findall(r"- pattern: (.+)\n\s+name: (\S+)", rules):
            alternatives = re.search(r"name=\(([^)]*)\)", pattern)
            for option in alternatives.group(1).split("|") if "$1" in name else [""]:
                exported.add(name.replace("$1", option).lower())
        titles = set()
        for panel in dashboard["panels"]:
            titles.add(panel["title"])
            for target in panel["targets"]:
                for metric in re.findall(r"\b(kafka_[a-z_]+|up)\{", target["expr"]):
                    self.assertIn(metric, exported, panel["title"])
        for wanted in ("Broker", "Under-replicated partitions", "Consumer group lag per topic",
                       "Bytes in per topic", "Bytes out per topic"):
            self.assertIn(wanted, titles)

    def test_metrics_are_scraped_and_the_dashboard_is_provisioned(self):
        monitors = (KAFKA / "service-monitor.yaml").read_text()
        self.assertEqual(monitors.count("kind: ServiceMonitor"), 2)
        kustomization = (KAFKA / "kustomization.yaml").read_text()
        self.assertIn('grafana_dashboard: "1"', kustomization)
        for resource in ("namespace.yaml", "kafka.yaml", "exporter.yaml", "service-monitor.yaml",
                         "prometheus-rule.yaml"):
            self.assertIn(f"  - {resource}\n", kustomization)


class KueueContractTests(unittest.TestCase):
    def setUp(self):
        self.release = (KUEUE / "controller/helm-release.yaml").read_text()
        self.queues = (KUEUE / "queues/queues.yaml").read_text()

    def test_chart_is_pinned_from_the_official_registry(self):
        source = (KUEUE / "controller/oci-repository.yaml").read_text()
        self.assertIn("url: oci://registry.k8s.io/kueue/charts/kueue\n", source)
        self.assertRegex(source, r"tag: \d+\.\d+\.\d+\n")

    def test_gpu_cluster_queue_has_one_gpu_for_opted_in_namespaces(self):
        self.assertIn("kind: ResourceFlavor\nmetadata:\n  name: amd-gpu\n", self.queues)
        self.assertIn("kind: ClusterQueue\nmetadata:\n  name: gpu\n", self.queues)
        self.assertIn('namespaceSelector:\n    matchLabels:\n      kueue.homelab/queues: "true"\n', self.queues)
        self.assertIn("- name: amd-gpu\n", self.queues)
        self.assertRegex(self.queues, r"- name: amd\.com/gpu\n\s+nominalQuota: 1\n")
        self.assertRegex(self.queues, r"- name: cpu\n\s+nominalQuota: \d+\n")
        self.assertRegex(self.queues, r"- name: memory\n\s+nominalQuota: \d+Gi\n")
        self.assertIn("apiVersion: kueue.x-k8s.io/v1beta2", self.queues)

    def test_only_batch_jobs_in_labeled_namespaces_pass_through_kueue(self):
        frameworks = re.search(r"frameworks:\n((?:\s+- \S+\n)+)", self.release).group(1)
        self.assertEqual(frameworks.split(), ["-", "batch/job"])
        self.assertIn('managedJobsNamespaceSelector:\n          matchLabels:\n'
                      '            kueue.homelab/queues: "true"\n', self.release)
        self.assertIn("quotaCheckStrategy: IgnoreUndeclared", self.release)

    def test_queues_wait_for_the_controller_and_its_crds(self):
        controller = (FLUX / "kueue.yaml").read_text()
        queues = (FLUX / "kueue-queues.yaml").read_text()
        self.assertIn("path: ./kubernetes/infrastructure/kueue/controller", controller)
        self.assertIn("path: ./kubernetes/infrastructure/kueue/queues", queues)
        self.assertIn("dependsOn:\n    # The ResourceFlavor", queues)
        self.assertIn("- name: kueue\n", queues)
        self.assertIn("pod-security.kubernetes.io/enforce: restricted",
                      (KUEUE / "controller/namespace.yaml").read_text())


class FluxOrderingTests(unittest.TestCase):
    def kustomizations(self):
        return sorted(path.name for path in FLUX.glob("*.yaml") if path.name != "kustomization.yaml")

    def test_every_flux_kustomization_is_listed_and_waited_for(self):
        listed = (FLUX / "kustomization.yaml").read_text()
        bootstrap = (ROOT / "scripts/bootstrap-dev.sh").read_text()
        for name in self.kustomizations():
            self.assertIn(f"  - {name}\n", listed)
            self.assertIn(f"/kubernetes/clusters/dev/infrastructure/{name}\"", bootstrap)

    def test_kafka_shell_variables_are_not_substituted_by_flux(self):
        self.assertIn("${AGENT_PATH}", (KAFKA / "kafka.yaml").read_text())
        self.assertNotIn("\n  postBuild:", (FLUX / "kafka.yaml").read_text())

    def test_service_monitor_users_depend_on_monitoring(self):
        for name in ("kafka.yaml", "kueue.yaml"):
            self.assertIn("dependsOn:\n    # ", (FLUX / name).read_text())
            self.assertIn("    - name: monitoring\n", (FLUX / name).read_text(), name)

    def test_queues_are_reconciled_after_the_controller(self):
        bootstrap = (ROOT / "scripts/bootstrap-dev.sh").read_text()
        self.assertLess(bootstrap.index("infrastructure/kueue.yaml"),
                        bootstrap.index("infrastructure/kueue-queues.yaml"))
        self.assertLess(bootstrap.index("infrastructure/monitoring.yaml"),
                        bootstrap.index("infrastructure/kafka.yaml"))


if __name__ == "__main__":
    unittest.main()
