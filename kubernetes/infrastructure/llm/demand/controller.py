#!/usr/bin/env python3
"""Run the platform LLM only while something needs it.

Every POLL_SECONDS this controller reads the lab mode and the applications
that request the LLM, then scales llama-server and the Open WebUI chat:

- llama-server runs while the lab is in llm mode or at least one running
  Deployment or StatefulSet carries the label homelab.local/llm=requested.
  Without either, it stops once demand has been absent for IDLE_GRACE_SECONDS,
  so an application restart or rollout does not unload the model.
- Open WebUI runs only in llm mode. A chat workload younger than
  CHAT_SETTLE_SECONDS is left alone so a fresh Helm install can bind its
  volume before it is stopped (see Controller.scale_chat).

It uses only the Python standard library so it runs in an unmodified official
Python image, and polls instead of watching because a 15-second delay is
irrelevant next to a model that takes a minute or two to load.
"""

from dataclasses import dataclass
from datetime import datetime
import http.server
import json
import logging
import os
from pathlib import Path
import signal
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

REQUEST_LABEL = "homelab.local/llm"
REQUEST_VALUE = "requested"
MODES = ("apps", "llm")
DEFAULT_MODE = "apps"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
# Scaling writes are recorded under this name in managedFields. It must not
# start with "kubectl": Flux's server-side apply takes over fields owned by
# kubectl managers, which would let it reset replicas again.
FIELD_MANAGER = "llm-demand"

log = logging.getLogger("llm-demand")


@dataclass(frozen=True)
class Settings:
    poll_seconds: float = 15
    idle_grace_seconds: float = 300
    chat_settle_seconds: float = 180
    port: int = 8080
    llm_namespace: str = "llm"
    llm_deployment: str = "llama-server"
    chat_namespace: str = "chat"
    mode_configmap: str = "lab-mode"

    @classmethod
    def from_environment(cls, environ=os.environ):
        return cls(
            poll_seconds=float(environ.get("POLL_SECONDS", cls.poll_seconds)),
            idle_grace_seconds=float(environ.get("IDLE_GRACE_SECONDS", cls.idle_grace_seconds)),
            chat_settle_seconds=float(environ.get("CHAT_SETTLE_SECONDS", cls.chat_settle_seconds)),
            port=int(environ.get("PORT", cls.port)),
            llm_namespace=environ.get("LLM_NAMESPACE", cls.llm_namespace),
            llm_deployment=environ.get("LLM_DEPLOYMENT", cls.llm_deployment),
            chat_namespace=environ.get("CHAT_NAMESPACE", cls.chat_namespace),
            mode_configmap=environ.get("MODE_CONFIGMAP", cls.mode_configmap),
        )


@dataclass(frozen=True)
class Decision:
    llm_replicas: int
    chat_replicas: int
    reason: str
    # Monotonic time when demand was last seen to disappear; None while there
    # is demand. The caller passes it back on the next loop.
    idle_since: float | None


# --- Pure decision logic: no I/O, so it is unit-tested without a cluster. ---

def replicas_of(workload):
    """Desired replicas of a Deployment or StatefulSet; Kubernetes defaults a missing field to 1."""
    replicas = workload.get("spec", {}).get("replicas")
    return 1 if replicas is None else int(replicas)


def describe(workload):
    meta = workload.get("metadata", {})
    return f'{workload.get("kind", "Workload")} {meta.get("namespace", "?")}/{meta.get("name", "?")}'


def active_requests(workloads):
    """Workloads that request the LLM and are meant to be running, as sorted descriptions."""
    return sorted(
        describe(workload) for workload in workloads
        if workload.get("metadata", {}).get("labels", {}).get(REQUEST_LABEL) == REQUEST_VALUE
        and replicas_of(workload) > 0
    )


def age_seconds(workload, now):
    """Seconds since the workload was created, from wall-clock Unix time now; None if unknown."""
    created = workload.get("metadata", {}).get("creationTimestamp")
    try:
        return now - datetime.fromisoformat(created).timestamp()
    except (TypeError, ValueError):
        return None


def parse_mode(configmap):
    """The lab mode from the lab-mode ConfigMap; a missing ConfigMap or key means apps."""
    if configmap is None:
        return DEFAULT_MODE, None
    raw = configmap.get("data", {}).get("mode")
    if raw is None:
        return DEFAULT_MODE, None
    mode = raw.strip().lower()
    if mode not in MODES:
        # Falling back to apps never stops an application; it only leaves
        # the chat off until the value is corrected.
        return DEFAULT_MODE, f"unknown lab mode {raw!r}; treating it as {DEFAULT_MODE!r}"
    return mode, None


def decide(mode, requests, llm_current, idle_since, now, grace):
    """Desired llama-server and chat replicas.

    llm_current is llama-server's current replica count (None if unknown);
    idle_since and now are monotonic seconds.
    """
    chat = 1 if mode == "llm" else 0
    if mode == "llm":
        return Decision(1, chat, "the lab is in llm mode", None)
    if requests:
        return Decision(1, chat, "requested by " + ", ".join(requests), None)
    idle_since = now if idle_since is None else idle_since
    idle = now - idle_since
    if llm_current == 0:
        return Decision(0, chat, "no running workload requests the LLM", idle_since)
    if idle >= grace:
        return Decision(0, chat, f"no running workload has requested the LLM for {idle:.0f}s "
                                 f"(idle grace {grace:.0f}s)", idle_since)
    # Keep a running server through the grace period so a rollout or a
    # restarting application does not unload and reload the model.
    return Decision(1, chat, f"no requests; keeping the LLM through the idle grace "
                             f"({grace - idle:.0f}s left)", idle_since)


# --- Thin Kubernetes API layer. ---

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status or 'connection'}: {message}")
        self.status = status


class Api:
    def __init__(self, host=None, port=None, account=SERVICE_ACCOUNT, timeout=10):
        host = host or os.environ["KUBERNETES_SERVICE_HOST"]
        port = port or os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host:
            host = f"[{host}]"
        self.base = f"https://{host}:{port}"
        self.token_path = account / "token"
        self.context = ssl.create_default_context(cafile=str(account / "ca.crt"))
        self.timeout = timeout

    def request(self, method, path, query=None, body=None, content_type=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        # Projected tokens rotate on disk; read the current one every time.
        headers = {"Authorization": "Bearer " + self.token_path.read_text().strip(),
                   "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            raise ApiError(error.code, f"{method} {path}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise ApiError(None, f"{method} {path}: {error}") from error

    def get(self, path, query=None):
        return self.request("GET", path, query)

    def get_or_none(self, path):
        try:
            return self.get(path)
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def scale(self, path, replicas):
        return self.request("PATCH", path + "/scale", {"fieldManager": FIELD_MANAGER},
                            {"spec": {"replicas": replicas}}, "application/merge-patch+json")


def workload_path(namespace, plural, name=None):
    path = f"/apis/apps/v1/namespaces/{namespace}/{plural}"
    return f"{path}/{name}" if name else path


class Controller:
    def __init__(self, api, settings, clock=time.monotonic, wall_clock=time.time):
        self.api = api
        self.settings = settings
        self.clock = clock
        self.wall_clock = wall_clock
        self.mode = DEFAULT_MODE
        self.requests = []
        self.idle_since = None
        self.decision = None
        self.last_state = None
        self.chat_note = None
        self.lock = threading.Lock()
        self.metrics = {"last_success": 0.0, "last_attempt_monotonic": clock(), "errors": 0,
                        "last_success_monotonic": None}

    def read_mode(self):
        s = self.settings
        configmap = self.api.get_or_none(f"/api/v1/namespaces/{s.llm_namespace}/configmaps/{s.mode_configmap}")
        mode, warning = parse_mode(configmap)
        if warning:
            log.warning(warning)
        return mode

    def read_requests(self):
        selector = {"labelSelector": f"{REQUEST_LABEL}={REQUEST_VALUE}"}
        workloads = []
        for plural, kind in (("deployments", "Deployment"), ("statefulsets", "StatefulSet")):
            for item in self.api.get(f"/apis/apps/v1/{plural}", selector).get("items", []):
                # List items omit kind; restore it for readable reasons.
                item["kind"] = kind
                workloads.append(item)
        return active_requests(workloads)

    def scale_llm(self, decision, current):
        s = self.settings
        if current is None or current == decision.llm_replicas:
            return
        log.info("Scaling %s/%s from %d to %d: %s", s.llm_namespace, s.llm_deployment,
                 current, decision.llm_replicas, decision.reason)
        self.api.scale(workload_path(s.llm_namespace, "deployments", s.llm_deployment), decision.llm_replicas)

    def scale_chat(self, decision):
        """Scale every chat workload; tolerate the namespace or its Role not existing yet."""
        namespace = self.settings.chat_namespace
        reason = f"the lab is in {self.mode} mode"
        notes = []
        try:
            for plural, kind in (("deployments", "Deployment"), ("statefulsets", "StatefulSet")):
                for item in self.api.get(workload_path(namespace, plural)).get("items", []):
                    name = item["metadata"]["name"]
                    current = replicas_of(item)
                    if current == decision.chat_replicas:
                        continue
                    age = age_seconds(item, self.wall_clock())
                    if decision.chat_replicas == 0 and age is not None and age < self.settings.chat_settle_seconds:
                        # Flux waits for a new Helm release to become healthy,
                        # and kstatus treats a Pending claim as unhealthy. The
                        # local-path claim binds only once a pod is scheduled,
                        # so stopping a brand-new chat at once could leave the
                        # install retrying forever.
                        notes.append(f"leaving new {kind} {namespace}/{name} running until it is "
                                     f"{self.settings.chat_settle_seconds:.0f}s old")
                        continue
                    log.info("Scaling %s %s/%s from %d to %d: %s", kind, namespace, name,
                             current, decision.chat_replicas, reason)
                    try:
                        self.api.scale(workload_path(namespace, plural, name), decision.chat_replicas)
                    except ApiError as error:
                        if error.status != 404:  # Deleted since the list.
                            raise
        except ApiError as error:
            # The chat Flux Kustomization depends on this one, so on a fresh
            # cluster its namespace and the Role granting access come later.
            if error.status not in (403, 404):
                raise
            notes = [f"chat namespace {namespace!r} is not ready for scaling yet ({error.status})"]
        note = "; ".join(notes) or None
        if note and note != self.chat_note:
            log.info(note)
        self.chat_note = note

    def reconcile(self):
        s = self.settings
        with self.lock:
            self.metrics["last_attempt_monotonic"] = self.clock()
        mode = self.read_mode()
        requests = self.read_requests()
        scale = self.api.get_or_none(workload_path(s.llm_namespace, "deployments", s.llm_deployment) + "/scale")
        current = None if scale is None else int(scale.get("spec", {}).get("replicas", 0))
        if scale is None:
            log.warning("Deployment %s/%s not found; not scaling the LLM", s.llm_namespace, s.llm_deployment)
        decision = decide(mode, requests, current, self.idle_since, self.clock(), s.idle_grace_seconds)
        # Log when the inputs change, not every loop: the grace countdown in
        # the reason would otherwise repeat the same line every 15 seconds.
        state = (mode, tuple(requests), current, decision.llm_replicas)
        if state != self.last_state and (current is None or current == decision.llm_replicas):
            log.info("LLM stays at %s replica(s): %s", current, decision.reason)
        self.mode, self.requests = mode, requests
        self.scale_llm(decision, current)
        # Record the decision and idle timer only after the LLM write
        # succeeded, so a failed write is retried with the same reasoning.
        self.idle_since, self.decision, self.last_state = decision.idle_since, decision, state
        self.scale_chat(decision)
        with self.lock:
            self.metrics["last_success"] = time.time()
            self.metrics["last_success_monotonic"] = self.clock()

    def run_once(self):
        try:
            self.reconcile()
            return True
        except Exception as error:  # noqa: BLE001 - one bad loop must not stop the controller.
            # Keep the last decision; the next loop retries from fresh state.
            with self.lock:
                self.metrics["errors"] += 1
            if isinstance(error, ApiError):
                log.error("Reconcile failed; keeping the last decision: %s", error)
            else:
                log.exception("Unexpected reconcile failure; keeping the last decision")
            return False

    def healthy(self):
        """The loop is still running (it may be failing against the API)."""
        with self.lock:
            return self.clock() - self.metrics["last_attempt_monotonic"] < 4 * self.settings.poll_seconds + 30

    def ready(self):
        """A loop has succeeded recently."""
        with self.lock:
            last = self.metrics["last_success_monotonic"]
        return last is not None and self.clock() - last < 4 * self.settings.poll_seconds + 30

    def render_metrics(self):
        with self.lock:
            metrics = dict(self.metrics)
        decision = self.decision
        lines = [
            "# HELP llm_demand_mode Current lab mode (1 for the active mode).",
            "# TYPE llm_demand_mode gauge",
            *(f'llm_demand_mode{{mode="{mode}"}} {int(mode == self.mode)}' for mode in MODES),
            "# HELP llm_demand_requests Running workloads labeled homelab.local/llm=requested.",
            "# TYPE llm_demand_requests gauge",
            f"llm_demand_requests {len(self.requests)}",
            "# HELP llm_demand_desired_replicas Replicas the controller last decided on.",
            "# TYPE llm_demand_desired_replicas gauge",
        ]
        if decision is not None:
            lines += [f'llm_demand_desired_replicas{{target="llm"}} {decision.llm_replicas}',
                      f'llm_demand_desired_replicas{{target="chat"}} {decision.chat_replicas}']
        lines += [
            "# HELP llm_demand_last_success_timestamp_seconds Unix time of the last successful loop.",
            "# TYPE llm_demand_last_success_timestamp_seconds gauge",
            f'llm_demand_last_success_timestamp_seconds {metrics["last_success"]:.3f}',
            "# HELP llm_demand_errors_total Loops that failed against the Kubernetes API.",
            "# TYPE llm_demand_errors_total counter",
            f'llm_demand_errors_total {metrics["errors"]}',
        ]
        return "\n".join(lines) + "\n"


def serve(controller, port):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                status, body = (200, "ok\n") if controller.healthy() else (503, "loop stalled\n")
            elif self.path == "/readyz":
                status, body = (200, "ok\n") if controller.ready() else (503, "no recent successful loop\n")
            elif self.path == "/metrics":
                status, body = 200, controller.render_metrics()
            else:
                status, body = 404, "not found\n"
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass  # Probes and scrapes would drown the scaling log.

    server = http.server.ThreadingHTTPServer(("", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_environment()
    controller = Controller(Api(), settings)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    serve(controller, settings.port)
    log.info("Watching for %s=%s every %.0fs; idle grace %.0fs", REQUEST_LABEL, REQUEST_VALUE,
             settings.poll_seconds, settings.idle_grace_seconds)
    while not stop.is_set():
        controller.run_once()
        stop.wait(settings.poll_seconds)


if __name__ == "__main__":
    main()
