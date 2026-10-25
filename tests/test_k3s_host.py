"""k3s host configuration regressions: the kubeconfig rename and the playbook wiring."""
from pathlib import Path
import re
import unittest

import yaml  # Installed with Ansible (python3-yaml); see tests/test_exposure.py.

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load((ROOT / "ansible/roles/k3s/defaults/main.yml").read_text())

# The shape of /etc/rancher/k3s/k3s.yaml as k3s writes it.
K3S_KUBECONFIG = """apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: Q0E=
    server: https://127.0.0.1:6443
  name: default
contexts:
- context:
    cluster: default
    user: default
  name: default
current-context: default
kind: Config
preferences: {}
users:
- name: default
  user:
    client-certificate-data: Q0VSVA==
    client-key-data: S0VZ
"""


def rename(text):
    # Ansible's regex_replace filter is Python's re.sub.
    return re.sub(DEFAULTS["k3s_kubeconfig_name_pattern"], DEFAULTS["k3s_kubeconfig_name_replacement"], text)


class K3sKubeconfigTests(unittest.TestCase):
    def test_cluster_user_and_context_are_renamed_to_homelab_dev(self):
        config = yaml.safe_load(rename(K3S_KUBECONFIG))
        self.assertEqual([c["name"] for c in config["clusters"]], ["homelab-dev"])
        self.assertEqual([u["name"] for u in config["users"]], ["homelab-dev"])
        self.assertEqual(config["contexts"], [{"context": {"cluster": "homelab-dev", "user": "homelab-dev"},
                                               "name": "homelab-dev"}])
        self.assertEqual(config["current-context"], "homelab-dev")

    def test_credentials_and_server_are_untouched(self):
        original, renamed = yaml.safe_load(K3S_KUBECONFIG), yaml.safe_load(rename(K3S_KUBECONFIG))
        self.assertEqual(renamed["clusters"][0]["cluster"], original["clusters"][0]["cluster"])
        self.assertEqual(renamed["users"][0]["user"], original["users"][0]["user"])
        # Only whole "default" values change, never substrings.
        self.assertEqual(rename("  name: default-pool\n"), "  name: default-pool\n")

    def test_the_task_uses_the_tested_expression(self):
        tasks = (ROOT / "ansible/roles/k3s/tasks/main.yml").read_text()
        self.assertIn("regex_replace(k3s_kubeconfig_name_pattern, k3s_kubeconfig_name_replacement)", tasks)


class PlaybookTests(unittest.TestCase):
    def test_host_playbook_installs_k3s_after_tools_and_gpu_checks(self):
        play = yaml.safe_load((ROOT / "ansible/playbooks/bootstrap.yml").read_text())[0]
        self.assertEqual(play["roles"], ["base", "kubernetes_tools", "amd_gpu", "k3s"])

    def test_k3s_version_is_pinned_and_installer_matches_it(self):
        self.assertRegex(DEFAULTS["k3s_version"], r"^v1\.\d+\.\d+\+k3s\d+$")
        self.assertIn("{{ k3s_version | urlencode }}", DEFAULTS["k3s_install_script_url"])

    def test_gpu_role_installs_no_rocm_or_dkms_packages(self):
        tasks = (ROOT / "ansible/roles/amd_gpu/tasks/main.yml").read_text()
        modules = [list(task)[1] for task in yaml.safe_load(tasks)]
        self.assertNotIn("ansible.builtin.apt", modules)
        self.assertNotIn("dkms", tasks.split("---", 1)[1].split("\n- name", 1)[1].lower())


if __name__ == "__main__":
    unittest.main()
