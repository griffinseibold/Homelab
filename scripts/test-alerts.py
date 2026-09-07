#!/usr/bin/env python3
"""Check LLM alert syntax and behavior without connecting to Kubernetes."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml


def main():
    repository = Path(__file__).resolve().parents[1]
    llm = repository / "kubernetes/infrastructure/llm"
    rule_spec = yaml.safe_load((llm / "prometheus-rule.yaml").read_text())["spec"]
    tests = yaml.safe_load((repository / "tests/alerts.test.yaml").read_text())
    promtool = os.environ.get("PROMTOOL") or "promtool"

    with tempfile.TemporaryDirectory(prefix="homelab-alerts-") as directory:
        workdir = Path(directory)
        rules_path = workdir / "rules.yaml"
        rules_path.write_text(yaml.safe_dump(rule_spec))
        tests["rule_files"] = [str(rules_path)]
        tests_path = workdir / "tests.yaml"
        tests_path.write_text(yaml.safe_dump(tests))

        # Parse dashboard queries with Prometheus too; Grafana substitutes this
        # interval at runtime using the panel's current time range.
        dashboard = json.loads((llm / "dashboard.json").read_text())
        dashboard_rules = [
            {
                "record": f"dashboard_panel_{panel['id']}_{target['refId']}",
                "expr": target["expr"].replace("$__rate_interval", "5m"),
            }
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
        ]
        dashboard_path = workdir / "dashboard-rules.yaml"
        dashboard_path.write_text(yaml.safe_dump({
            "groups": [{"name": "dashboard-syntax", "rules": dashboard_rules}]
        }))

        try:
            subprocess.run(
                [promtool, "check", "rules", str(rules_path), str(dashboard_path)],
                check=True,
            )
            subprocess.run([promtool, "test", "rules", str(tests_path)], check=True)
        except FileNotFoundError:
            raise SystemExit("Install promtool or set PROMTOOL to its executable path.")
        except subprocess.CalledProcessError as error:
            raise SystemExit(error.returncode)


if __name__ == "__main__":
    main()
