"""Exposure guarantees on k3s, checked against the committed configuration.

The Gateway's http listener must be reachable only on 127.0.0.1:8080, and its
lan listener only on LAN_ADDRESS:443 (loopback when it is unset). Nothing may
be published on 0.0.0.0 or IPv6, and k3s's own ports stay off the network.
These tests render the Flux substitution the way kustomize-controller does
for the variables used here, then read the result.
"""
import ipaddress
from pathlib import Path
import re
import unittest

# PyYAML is not standard library, but Ansible depends on it, so every host
# that ran bootstrap-host.sh has it (Ubuntu's python3-yaml).
import yaml

ROOT = Path(__file__).resolve().parents[1]
ENVOY_PROXY = ROOT / "kubernetes/infrastructure/gateway-api/overlays/dev/envoy-proxy.yaml"
K3S_CONFIG = ROOT / "ansible/roles/k3s/files/config.yaml"
GUARD = ROOT / "ansible/roles/k3s/files/homelab-guard.nft"
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::=([^}]*))?\}")


def substitute(text, variables):
    """Flux post-build substitution for ${VAR} and ${VAR:=default}."""
    def replace(match):
        name, default = match.group(1), match.group(2)
        if name in variables:
            return variables[name]
        if default is not None:
            return default
        raise KeyError(name)
    return VARIABLE.sub(replace, text)


def host_ports(variables):
    proxy = yaml.safe_load(substitute(ENVOY_PROXY.read_text(), variables))
    kubernetes = proxy["spec"]["provider"]["kubernetes"]
    template = kubernetes["envoyDeployment"]["patch"]["value"]["spec"]["template"]["spec"]
    ports = [port for container in template["containers"] for port in container.get("ports", [])]
    return kubernetes, template, {port["name"]: port for port in ports if "hostPort" in port}


def all_manifests():
    for path in sorted((ROOT / "kubernetes").rglob("*.yaml")):
        if "flux-system" in path.parts or "kind" in path.parts:
            continue
        for document in yaml.safe_load_all(VARIABLE.sub("0", path.read_text())):
            if isinstance(document, dict):
                yield path, document


class GatewayExposureTests(unittest.TestCase):
    def test_http_listener_is_published_only_on_loopback_8080(self):
        for variables in ({}, {"LAN_ADDRESS": "192.168.1.50"}):
            with self.subTest(variables=variables):
                _, _, ports = host_ports(variables)
                self.assertEqual(ports["host-http"]["hostIP"], "127.0.0.1")
                self.assertEqual(ports["host-http"]["hostPort"], 8080)
                # Envoy Gateway v1.9.1 runs listener port 80 at 80 + 10000.
                self.assertEqual(ports["host-http"]["containerPort"], 10080)

    def test_lan_listener_defaults_to_loopback_and_follows_lan_address(self):
        _, _, ports = host_ports({})
        self.assertEqual(ports["host-lan"]["hostIP"], "127.0.0.1")
        _, _, ports = host_ports({"LAN_ADDRESS": "192.168.1.50"})
        self.assertEqual(ports["host-lan"]["hostIP"], "192.168.1.50")
        self.assertEqual(ports["host-lan"]["hostPort"], 443)
        self.assertEqual(ports["host-lan"]["containerPort"], 10443)

    def test_every_host_port_names_one_ipv4_address_never_a_wildcard(self):
        for variables in ({}, {"LAN_ADDRESS": "10.0.0.7"}):
            _, _, ports = host_ports(variables)
            self.assertEqual(set(ports), {"host-http", "host-lan"})
            for port in ports.values():
                address = ipaddress.ip_address(port["hostIP"])
                self.assertEqual(address.version, 4)
                self.assertFalse(address.is_unspecified)
                self.assertEqual(port["protocol"], "TCP")

    def test_envoy_service_opens_no_node_or_load_balancer_port(self):
        kubernetes, template, _ = host_ports({})
        self.assertEqual(kubernetes["envoyService"]["type"], "ClusterIP")
        self.assertNotIn("hostNetwork", template)
        self.assertEqual(kubernetes["envoyDeployment"]["replicas"], 1)
        strategy = kubernetes["envoyDeployment"]["patch"]["value"]["spec"]["strategy"]
        self.assertEqual(strategy["type"], "Recreate")

    def test_gateway_listener_ports_match_the_container_ports(self):
        gateway = yaml.safe_load((ROOT / "kubernetes/infrastructure/gateway-api/overlays/dev/gateway.yaml").read_text())
        listeners = {listener["name"]: listener["port"] for listener in gateway["spec"]["listeners"]}
        _, _, ports = host_ports({})
        self.assertEqual(ports["host-http"]["containerPort"], listeners["http"] + 10000)
        self.assertEqual(ports["host-lan"]["containerPort"], listeners["lan"] + 10000)

    def test_gateway_substitution_falls_back_when_the_configmap_is_missing(self):
        kustomization = yaml.safe_load(
            (ROOT / "kubernetes/clusters/dev/infrastructure/gateway-api-config.yaml").read_text())
        source = kustomization["spec"]["postBuild"]["substituteFrom"]
        self.assertEqual(source, [{"kind": "ConfigMap", "name": "homelab-host", "optional": True}])

    def test_no_other_manifest_publishes_on_the_host(self):
        for path, document in all_manifests():
            with self.subTest(path=str(path.relative_to(ROOT)), kind=document.get("kind")):
                if document.get("kind") == "Service":
                    self.assertIn(document.get("spec", {}).get("type", "ClusterIP"), ("ClusterIP",))
                text = yaml.safe_dump(document)
                self.assertNotIn("hostNetwork: true", text)
                if path != ENVOY_PROXY:
                    self.assertNotIn("hostPort", text)


class K3sHostTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load(K3S_CONFIG.read_text())

    def test_bundled_ingress_and_load_balancer_are_disabled_storage_kept(self):
        self.assertEqual(set(self.config["disable"]), {"traefik", "servicelb"})
        self.assertNotIn("local-storage", self.config["disable"])

    def test_node_ports_are_limited_to_loopback(self):
        self.assertIn("nodeport-addresses=127.0.0.1/32", self.config["kube-proxy-arg"])

    def test_admin_kubeconfig_stays_private(self):
        self.assertEqual(self.config["write-kubeconfig-mode"], "0600")
        self.assertTrue(self.config["secrets-encryption"])

    def test_guard_drops_outside_access_to_k3s_ports_but_not_loopback_or_pods(self):
        rules = GUARD.read_text()
        accept_loopback = rules.index('iifname "lo" accept')
        accept_pods = rules.index('iifname "cni0" accept')
        drop = rules.index("tcp dport {")
        self.assertLess(accept_loopback, drop)
        self.assertLess(accept_pods, drop)
        ports = re.search(r"tcp dport \{([^}]*)\} drop", rules).group(1)
        for port in ("6443", "10250", "9100"):
            self.assertIn(port, ports.replace("6443-6444", "6443"))
        self.assertIn("policy accept", rules)  # Does not lock out SSH or other services.
        self.assertIn("table inet", rules)     # IPv4 and IPv6 alike.


if __name__ == "__main__":
    unittest.main()
