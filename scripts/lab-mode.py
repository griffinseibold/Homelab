#!/usr/bin/env python3
"""Switch the dev cluster between apps mode and llm mode.

apps mode (the default) runs Argo CD applications; the LLM runs only while one
of them requests it. llm mode stops every Argo CD application and runs only
the LLM and the Open WebUI chat, for personal use.

The LLM demand controller in the llm namespace does the LLM and chat scaling;
this command pauses and restores the applications and records the mode.
"""

import argparse
import json
import subprocess
import sys
import time

CLUSTER = "homelab-dev"
CONTEXT = CLUSTER  # The k3s kubeconfig context (ansible/roles/k3s).
MODE_NAMESPACE = "llm"
MODE_CONFIGMAP = "lab-mode"
MODES = ("apps", "llm")
REQUEST_LABEL = "homelab.local/llm"
SAVED_POLICY = "homelab.local/saved-sync-policy"
SAVED_REPLICAS = "homelab.local/saved-replicas"
FIELD_MANAGER = "homelab-lab-mode"
WORKLOADS = "deployments.apps,statefulsets.apps"
# Applications are only ever stopped in their own destination namespaces.
# These hold the platform itself (and Kind's system pieces); an Application
# that targets one of them is skipped with a warning instead.
# ml-storage and mlflow are platform services that applications (and the
# person using llm mode) keep relying on; stopping them would only lose
# in-flight uploads.
PLATFORM_NAMESPACES = frozenset({
    "argocd", "cert-manager", "chat", "envoy-gateway-system", "flux-system",
    "gateway-system", "kube-node-lease", "kube-public", "kube-system", "llm",
    "local-path-storage", "ml-storage", "mlflow", "monitoring",
})
IN_CLUSTER = {"https://kubernetes.default.svc", "in-cluster"}
CHAT_URLS = ("http://chat.localhost:8080", "https://chat.lab.internal (if home-network access is on)")
API_URL = "http://llm.localhost:8080/v1"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return run(*args, stdout=subprocess.PIPE, text=True, timeout=45).stdout


def kubectl(*args, stdin=None):
    """Run kubectl against the dev cluster and return its standard output."""
    command = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", *args]
    if stdin is None:
        return output(*command)
    return run(*command, input=stdin, stdout=subprocess.PIPE, text=True, timeout=45).stdout


def get(resource, namespace=None, selector=None):
    args = ["get", resource]
    args += ["-n", namespace] if namespace else ["-A"]
    if selector:
        args += ["-l", selector]
    return json.loads(kubectl(*args, "-o", "json"))["items"]


def patch(kind, namespace, name, body, patch_type="merge"):
    kubectl("patch", kind, name, "-n", namespace, "--type", patch_type,
            "--field-manager", FIELD_MANAGER, "-p", json.dumps(body))


def warn(message):
    print(f"Warning: {message}", file=sys.stderr)


def annotations(item):
    return item["metadata"].get("annotations") or {}


def replicas_of(workload):
    replicas = workload.get("spec", {}).get("replicas")
    return 1 if replicas is None else int(replicas)


def ref(workload):
    meta = workload["metadata"]
    return f'{workload["kind"]} {meta["namespace"]}/{meta["name"]}'


def kind_of(workload):
    return {"Deployment": "deployment.apps", "StatefulSet": "statefulset.apps"}[workload["kind"]]


# --- Reading state ---

def read_mode():
    configmap = kubectl("get", "configmap", MODE_CONFIGMAP, "-n", MODE_NAMESPACE,
                        "--ignore-not-found", "-o", "json").strip()
    if not configmap:
        return "apps"
    return json.loads(configmap).get("data", {}).get("mode", "apps").strip().lower() or "apps"


def applications():
    return get("applications.argoproj.io", "argocd")


def destination_namespaces(apps):
    """Namespaces the Applications deploy into that this command may stop."""
    namespaces = set()
    for app in apps:
        name = app["metadata"]["name"]
        destination = app.get("spec", {}).get("destination", {})
        namespace = destination.get("namespace")
        target = destination.get("server") or destination.get("name") or "https://kubernetes.default.svc"
        if target not in IN_CLUSTER:
            warn(f"Application {name} deploys to another cluster ({target}); its workloads are not stopped")
        elif not namespace:
            warn(f"Application {name} has no destination namespace; its workloads are not stopped")
        elif namespace in PLATFORM_NAMESPACES:
            warn(f"Application {name} deploys into platform namespace {namespace}; it is left running")
        else:
            namespaces.add(namespace)
    return sorted(namespaces)


def application_set_owned(app):
    return any(owner.get("kind") == "ApplicationSet"
               for owner in app["metadata"].get("ownerReferences", []))


# --- llm mode ---

def pause_application(app):
    """Save the sync policy once, then turn off automated sync.

    The saved copy is never overwritten: after a second llm switch the live
    policy no longer has automated sync, and saving it would lose the original.
    """
    name = app["metadata"]["name"]
    policy = app.get("spec", {}).get("syncPolicy")
    body = {}
    if SAVED_POLICY not in annotations(app):
        body["metadata"] = {"annotations": {SAVED_POLICY: json.dumps(policy, sort_keys=True)}}
    if policy and "automated" in policy:
        body["spec"] = {"syncPolicy": {"automated": None}}
    if body:
        # One merge patch records the policy and disables sync together.
        patch("application.argoproj.io", "argocd", name, body)
        print(f"Paused Argo CD application {name}")
    if application_set_owned(app):
        warn(f"Application {name} is generated by an ApplicationSet, which may turn automated "
             "sync back on; check it with: ./scripts/lab-mode.py status")


def park_workload(workload):
    """Scale to zero, recording the previous replicas once in the same patch."""
    current = replicas_of(workload)
    body = {}
    if SAVED_REPLICAS not in annotations(workload):
        body["metadata"] = {"annotations": {SAVED_REPLICAS: str(current)}}
    if current != 0:
        body["spec"] = {"replicas": 0}
    if body:
        meta = workload["metadata"]
        patch(kind_of(workload), meta["namespace"], meta["name"], body)
        print(f"Stopped {ref(workload)} (was {annotations(workload).get(SAVED_REPLICAS, current)} replicas)")


def set_mode(mode):
    configmap = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": MODE_CONFIGMAP, "namespace": MODE_NAMESPACE,
                     "labels": {"app.kubernetes.io/name": "lab-mode",
                                "app.kubernetes.io/managed-by": FIELD_MANAGER}},
        "data": {"mode": mode},
    }
    kubectl("apply", "--server-side", "--force-conflicts", "--field-manager", FIELD_MANAGER,
            "-f", "-", stdin=json.dumps(configmap))
    print(f"Lab mode set to {mode}")


def switch_to_llm(wait=False, timeout=600):
    apps = applications()
    # Stop sync first so Argo CD cannot restore a workload while it is stopped.
    for app in apps:
        pause_application(app)
    # An app-of-apps parent with self-heal may have re-enabled a child's sync
    # before the parent itself was paused; a second pass catches that.
    apps = applications()
    for app in apps:
        if "automated" in (app.get("spec", {}).get("syncPolicy") or {}):
            pause_application(app)
    for namespace in destination_namespaces(apps):
        for workload in get(WORKLOADS, namespace):
            park_workload(workload)
    set_mode("llm")
    print("The demand controller starts the LLM and chat within about 15 seconds;")
    print("the model then takes a minute or two to load.")
    if wait:
        wait_until_ready(timeout)
    print(f"Chat: {CHAT_URLS[0]}")
    print(f"      {CHAT_URLS[1]}")
    print(f"LLM API: {API_URL}")


def llm_ready():
    try:
        # The API server's service proxy works on clusters without the
        # localhost port mapping; llama.cpp answers 503 while loading.
        kubectl("get", "--raw", f"/api/v1/namespaces/{MODE_NAMESPACE}/services/llama-server:http/proxy/health")
        return True
    except subprocess.CalledProcessError:
        return False


def chat_ready():
    workloads = get(WORKLOADS, "chat")
    return bool(workloads) and all(
        replicas_of(w) > 0 and w.get("status", {}).get("readyReplicas", 0) >= replicas_of(w) for w in workloads)


def wait_until_ready(timeout, sleep=time.sleep, clock=time.monotonic):
    print("Waiting for the LLM and chat to become ready...", flush=True)
    deadline = clock() + timeout
    while not (llm_ready() and chat_ready()):
        if clock() >= deadline:
            raise RuntimeError(f"The LLM or chat was not ready after {timeout:.0f}s; "
                               "check ./scripts/lab-mode.py status and the llm-demand logs")
        sleep(5)
    print("The LLM and chat are ready.")


# --- apps mode ---

def unpark_workload(workload):
    raw = annotations(workload)[SAVED_REPLICAS]
    try:
        replicas = int(raw)
        if replicas < 0:
            raise ValueError
    except ValueError:
        warn(f"{ref(workload)} has an unreadable {SAVED_REPLICAS} value {raw!r}; leaving it for Argo CD")
        return
    meta = workload["metadata"]
    # One patch restores the count and drops the record, so a rerun after a
    # failure either repeats this whole step or finds nothing left to do.
    patch(kind_of(workload), meta["namespace"], meta["name"],
          {"metadata": {"annotations": {SAVED_REPLICAS: None}}, "spec": {"replicas": replicas}})
    print(f"Started {ref(workload)} ({replicas} replicas)")


def resume_application(app):
    name = app["metadata"]["name"]
    raw = annotations(app)[SAVED_POLICY]
    try:
        policy = json.loads(raw)
    except ValueError:
        warn(f"Application {name} has an unreadable saved sync policy; leaving it paused")
        return
    escaped = SAVED_POLICY.replace("~", "~0").replace("/", "~1")
    # The test operation makes the patch fail, without changing anything, if
    # the saved policy changed since it was read.
    operations = [{"op": "test", "path": f"/metadata/annotations/{escaped}", "value": raw}]
    if policy is None:
        if "syncPolicy" in app.get("spec", {}):
            operations.append({"op": "remove", "path": "/spec/syncPolicy"})
    else:
        operations.append({"op": "add", "path": "/spec/syncPolicy", "value": policy})
    operations.append({"op": "remove", "path": f"/metadata/annotations/{escaped}"})
    patch("application.argoproj.io", "argocd", name, operations, patch_type="json")
    print(f"Resumed Argo CD application {name}")


def switch_to_apps():
    set_mode("apps")
    # Search every namespace, not only current destinations, so a workload
    # whose Application was removed or retargeted is still restored.
    for workload in get(WORKLOADS):
        if workload["metadata"]["namespace"] in PLATFORM_NAMESPACES:
            continue
        if SAVED_REPLICAS in annotations(workload):
            unpark_workload(workload)
    # Sync resumes last: Argo CD then finds the workloads already restored.
    for app in applications():
        if SAVED_POLICY in annotations(app):
            resume_application(app)
    print("Apps mode: the chat stops within about 15 seconds, and the LLM stops after its idle")
    print("grace unless an application requests it.")


# --- status ---

def status():
    mode = read_mode()
    print(f"Mode: {mode}")
    llm = get("deployments.apps", MODE_NAMESPACE)
    servers = {d["metadata"]["name"]: d for d in llm}
    server = servers.get("llama-server")
    if server:
        print(f'LLM (llama-server): {replicas_of(server)} desired, '
              f'{server.get("status", {}).get("readyReplicas", 0)} ready')
    else:
        print("LLM (llama-server): not installed")
    controller = servers.get("llm-demand")
    controller_ready = bool(controller) and controller.get("status", {}).get("readyReplicas", 0) > 0
    print(f'Demand controller: {"ready" if controller_ready else "NOT READY"}')

    requests = [w for w in get(WORKLOADS, selector=f"{REQUEST_LABEL}=requested") if replicas_of(w) > 0]
    print("Requesting the LLM:" + ("" if requests else " none"))
    for workload in sorted(requests, key=ref):
        print(f"  {ref(workload)}")

    chat = get(WORKLOADS, "chat")
    running = [w for w in chat if replicas_of(w) > 0]
    ready = all(w.get("status", {}).get("readyReplicas", 0) >= replicas_of(w) for w in running)
    if not chat:
        print("Chat: not installed")
    elif running:
        print(f'Chat: running{"" if ready else " (starting)"}')
    else:
        print("Chat: stopped")

    apps = applications()
    paused = sorted(a["metadata"]["name"] for a in apps if SAVED_POLICY in annotations(a))
    print("Paused Argo CD applications:" + ("" if paused else " none"))
    for name in paused:
        print(f"  {name}")
    parked = sorted(ref(w) for w in get(WORKLOADS)
                    if SAVED_REPLICAS in annotations(w) and w["metadata"]["namespace"] not in PLATFORM_NAMESPACES)
    if parked:
        print(f"Stopped application workloads: {len(parked)}")

    resumed = [a["metadata"]["name"] for a in apps
               if SAVED_POLICY in annotations(a) and "automated" in (a.get("spec", {}).get("syncPolicy") or {})]
    if mode == "apps" and (paused or parked):
        warn("apps mode, but some applications are still paused; rerun: ./scripts/lab-mode.py apps")
    if mode == "llm" and resumed:
        warn("automated sync was turned back on for " + ", ".join(resumed)
             + "; rerun ./scripts/lab-mode.py llm or check its ApplicationSet")
    if mode not in MODES:
        warn(f"unknown mode {mode!r}; the controller treats it as apps")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show the mode, the LLM, its requesters, chat and paused applications")
    llm_parser = sub.add_parser("llm", help="Stop Argo CD applications and run only the LLM and chat")
    llm_parser.add_argument("--wait", action="store_true", help="Wait until the LLM and chat are ready")
    llm_parser.add_argument("--timeout", type=float, default=600, help="Seconds to wait (default 600)")
    sub.add_parser("apps", help="Restore Argo CD applications; the LLM runs only on request")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            status()
        elif args.command == "llm":
            switch_to_llm(args.wait, args.timeout)
        else:
            switch_to_apps()
    except subprocess.CalledProcessError as error:
        print(f"Lab mode change failed: {' '.join(error.cmd[:6])}... exited with {error.returncode}. "
              "It is safe to rerun the same command.", file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Lab mode change failed: {error}. It is safe to rerun the same command.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
