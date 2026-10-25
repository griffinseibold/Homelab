#!/usr/bin/env python3
"""Create the platform's S3 buckets in Garage, idempotently.

Runs as a Job after every change to this script. It waits for Garage to be
healthy, then creates any of BUCKETS that does not exist yet through Garage's
admin API v2 (https://garagehq.deuxfleurs.fr/documentation/reference-manual/admin-api/).
Existing buckets and their contents are never changed.

Access keys are not created here: scripts/ml-credentials.py creates one per
namespace that needs S3, so each application's credentials can be rotated
and revoked on their own.

A few response shapes in the admin API could not be checked against a live
server when this was written, so every response is validated before it is
used. Anything unexpected stops the Job with the response in the message
instead of being guessed at.

Standard library only, like the LLM demand controller, so it runs in an
unmodified official Python image.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BUCKETS = ("datasets", "checkpoints", "mlflow-artifacts")


class SetupError(Exception):
    """A problem waiting cannot fix; the message says what to check."""


def log(message):
    print(message, flush=True)


def snippet(value):
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= 300 else text[:300] + "..."


class Admin:
    def __init__(self, base, token, timeout=10):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method, endpoint, query=None, body=None):
        """Return (HTTP status, parsed JSON or raw text); connection errors give status None."""
        url = f"{self.base}/v2/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read().decode(errors="replace")
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read().decode(errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return None, str(error)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw


def wait_until_healthy(admin, sleep=time.sleep, clock=time.monotonic, report_every=60):
    """Wait, without a deadline, until Garage reports a healthy single-node cluster.

    A missing Secret, a starting pod or a volume still binding all fix
    themselves, so the Job waits rather than failing; Flux shows it as in
    progress meanwhile. A wrong admin token does not fix itself.
    """
    last_report = None
    while True:
        status, body = admin.call("GET", "GetClusterHealth")
        if status in (401, 403):
            raise SetupError("Garage rejected the admin token. The garage Secret in ml-storage must "
                             "match the running server; rerun scripts/bootstrap-dev.sh, then "
                             "restart deployment/garage")
        if status == 200:
            if not isinstance(body, dict) or "status" not in body:
                raise SetupError(f"Unexpected GetClusterHealth response: {snippet(body)}")
            if body["status"] == "healthy":
                return body
            reason = f"cluster status {body['status']!r}"
            if body.get("storageNodes") == 0:
                # --single-node assigns the layout on a fresh start; an empty
                # layout means that did not happen (for example on a volume
                # restored from a multi-node cluster).
                reason += ("; no storage node in the layout. Check the garage pod log; "
                           "`garage layout show` inside the pod shows the layout")
        else:
            reason = f"admin API not ready ({status or 'no connection'}: {snippet(body)})"
        now = clock()
        if last_report is None or now - last_report >= report_every:
            log(f"Waiting for Garage: {reason}")
            last_report = now
        sleep(5)


def bucket_items(body):
    """The ListBuckets result as a list of {id, globalAliases} dictionaries."""
    items = body.get("buckets") if isinstance(body, dict) else body
    if not isinstance(items, list) or not all(
            isinstance(item, dict) and isinstance(item.get("id"), str)
            and isinstance(item.get("globalAliases"), list) for item in items):
        raise SetupError(f"Unexpected ListBuckets response; expected a list of buckets with "
                         f"id and globalAliases: {snippet(body)}")
    return items


def existing_aliases(admin):
    status, body = admin.call("GET", "ListBuckets")
    if status != 200:
        raise SetupError(f"ListBuckets failed ({status}): {snippet(body)}")
    return {alias for item in bucket_items(body) for alias in item["globalAliases"]}


def ensure_buckets(admin, buckets=BUCKETS):
    present = existing_aliases(admin)
    created = []
    for name in buckets:
        if name in present:
            log(f"Bucket {name} exists")
            continue
        status, body = admin.call("POST", "CreateBucket", body={"globalAlias": name})
        if status != 200 or not isinstance(body, dict) or name not in (body.get("globalAliases") or []):
            raise SetupError(f"Creating bucket {name} failed ({status}): {snippet(body)}")
        log(f"Created bucket {name} ({body.get('id', '?')})")
        created.append(name)
    # Read back once more, so a create that silently did nothing is caught.
    missing = sorted(set(buckets) - existing_aliases(admin))
    if missing:
        raise SetupError(f"Buckets still missing after creation: {', '.join(missing)}")
    return created


def main(environ=os.environ):
    token = environ.get("GARAGE_ADMIN_TOKEN", "").strip()
    if not token:
        log("GARAGE_ADMIN_TOKEN is empty; the garage Secret is incomplete. Rerun scripts/bootstrap-dev.sh.")
        return 1
    admin = Admin(environ.get("GARAGE_ADMIN_URL", "http://garage-admin.ml-storage.svc:3903"), token)
    try:
        wait_until_healthy(admin)
        created = ensure_buckets(admin)
    except SetupError as error:
        log(f"Garage setup failed: {error}")
        return 1
    log(f"Garage is ready; created {len(created)} bucket(s): {', '.join(BUCKETS)} present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
