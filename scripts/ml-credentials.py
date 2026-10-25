#!/usr/bin/env python3
"""Give a namespace its own S3 credentials for the platform's object storage.

Creates (or reuses) a Garage access key named k8s-<namespace>, grants it read
and write on the requested buckets, and writes the Secret s3-credentials in
that namespace with the keys AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
The endpoint is http://s3.ml-storage.svc.cluster.local:9000 (path-style).

Safe to rerun: an existing key keeps its credentials, permissions are only
added, and the Secret is rewritten from the key Garage holds. Credentials
live in Garage and the Secret, never in Git; after a cluster rebuild with a
restored Garage volume, rerunning this recreates the Secret with the same key.

Garage is driven through `garage json-api` inside its pod (kubectl exec), which
uses Garage's internal RPC, so the admin token never leaves the cluster.
"""

import argparse
import base64
import json
import re
import subprocess
import sys

CLUSTER = "homelab-dev"
CONTEXT = f"kind-{CLUSTER}"
STORAGE_NAMESPACE = "ml-storage"
GARAGE = "deployment/garage"
SECRET = "s3-credentials"
FIELD_MANAGER = "homelab-ml-credentials"
DEFAULT_BUCKETS = ("datasets", "checkpoints")
KNOWN_BUCKETS = ("datasets", "checkpoints", "mlflow-artifacts")
ENDPOINT = "http://s3.ml-storage.svc.cluster.local:9000"
# A Kubernetes namespace name (RFC 1123 label).
NAMESPACE_PATTERN = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


class CredentialsError(Exception):
    """A problem the user has to fix; the message says what to check."""


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def kubectl(*args, stdin=None):
    """Run kubectl against the dev cluster and return its standard output."""
    return run("kubectl", "--context", CONTEXT, "--request-timeout=30s", *args,
               input=stdin, stdout=subprocess.PIPE, text=True, timeout=60).stdout


def snippet(value):
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= 300 else text[:300] + "..."


def garage(endpoint, body=None):
    """Call one Garage admin API v2 endpoint through `garage json-api` and return its JSON."""
    try:
        out = kubectl("-n", STORAGE_NAMESPACE, "exec", "-i", GARAGE, "-c", "garage", "--",
                      "/garage", "json-api", endpoint, "-",
                      stdin=json.dumps({} if body is None else body))
    except subprocess.CalledProcessError as error:
        raise CredentialsError(
            f"Garage {endpoint} failed (exit {error.returncode}). Is Garage running? "
            f"kubectl --context {CONTEXT} -n {STORAGE_NAMESPACE} get pods") from error
    try:
        return json.loads(out)
    except ValueError as error:
        raise CredentialsError(f"Garage {endpoint} did not return JSON: {snippet(out)}") from error


def require(condition, endpoint, response, expected):
    if not condition:
        raise CredentialsError(f"Unexpected Garage {endpoint} response; expected {expected}: {snippet(response)}")


# --- Garage ---

def key_name(namespace):
    return f"k8s-{namespace}"


def find_key_id(name):
    response = garage("ListKeys")
    keys = response.get("keys") if isinstance(response, dict) else response
    require(isinstance(keys, list) and all(isinstance(k, dict) and isinstance(k.get("id"), str)
                                           and isinstance(k.get("name"), str) for k in keys),
            "ListKeys", response, "a list of keys with id and name")
    matches = [k["id"] for k in keys if k["name"] == name]
    if len(matches) > 1:
        raise CredentialsError(f"Garage has {len(matches)} keys named {name}; delete the extras with "
                               f"`garage key delete` inside the garage pod, then rerun")
    return matches[0] if matches else None


def checked_key(endpoint, response):
    require(isinstance(response, dict) and isinstance(response.get("accessKeyId"), str)
            and isinstance(response.get("secretAccessKey"), str) and response["accessKeyId"]
            and response["secretAccessKey"], endpoint, response, "accessKeyId and secretAccessKey")
    return response


def ensure_key(namespace):
    """Return Garage's key info (with its secret) for the namespace, creating the key if needed."""
    name = key_name(namespace)
    key_id = find_key_id(name)
    if key_id:
        print(f"Reusing Garage key {name} ({key_id})")
        return checked_key("GetKeyInfo", garage("GetKeyInfo", {"id": key_id, "showSecretKey": True}))
    created = garage("CreateKey", {"name": name})
    require(isinstance(created, dict) and isinstance(created.get("accessKeyId"), str)
            and created["accessKeyId"], "CreateKey", created, "accessKeyId")
    print(f"Created Garage key {name} ({created['accessKeyId']})")
    if created.get("secretAccessKey"):
        return checked_key("CreateKey", created)
    # Read the secret back explicitly if the create response omitted it.
    return checked_key("GetKeyInfo", garage("GetKeyInfo", {"id": created["accessKeyId"],
                                                           "showSecretKey": True}))


def bucket_id(bucket):
    response = garage("GetBucketInfo", {"globalAlias": bucket})
    require(isinstance(response, dict) and isinstance(response.get("id"), str)
            and bucket in (response.get("globalAliases") or []), "GetBucketInfo", response,
            f"bucket {bucket} with an id")
    return response["id"]


def grant(key, bucket):
    bid = bucket_id(bucket)
    response = garage("AllowBucketKey", {"bucketId": bid, "accessKeyId": key["accessKeyId"],
                                         "permissions": {"read": True, "write": True}})
    # Verify the grant on the bucket's key list rather than trusting a 200.
    keys = response.get("keys") if isinstance(response, dict) else None
    require(isinstance(keys, list), "AllowBucketKey", response, "the bucket's keys")
    entry = next((k for k in keys if isinstance(k, dict) and k.get("accessKeyId") == key["accessKeyId"]), None)
    permissions = (entry or {}).get("permissions") or {}
    if not (permissions.get("read") and permissions.get("write")):
        raise CredentialsError(f"Garage did not grant read and write on {bucket}: {snippet(response)}")
    print(f"Granted read and write on bucket {bucket}")


# --- Kubernetes ---

def require_namespace(namespace):
    found = kubectl("get", "namespace", namespace, "--ignore-not-found", "-o", "name").strip()
    if not found:
        raise CredentialsError(f"Namespace {namespace} does not exist. Create it first (for example "
                               f"kubectl --context {CONTEXT} create namespace {namespace}), or let "
                               "its Argo CD application create it, then rerun")


def write_secret(namespace, key):
    def encoded(value):
        return base64.b64encode(value.encode()).decode()
    secret = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": SECRET, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": FIELD_MANAGER},
                     "annotations": {"homelab.local/s3-endpoint": ENDPOINT}},
        "data": {"AWS_ACCESS_KEY_ID": encoded(key["accessKeyId"]),
                 "AWS_SECRET_ACCESS_KEY": encoded(key["secretAccessKey"])},
    }
    # The Secret goes through stdin, never the command line, and server-side
    # apply keeps it out of a last-applied annotation.
    kubectl("apply", "--server-side", "--force-conflicts", "--field-manager", FIELD_MANAGER,
            "-f", "-", stdin=json.dumps(secret))
    print(f"Wrote Secret {namespace}/{SECRET}")


def provision(namespace, buckets):
    require_namespace(namespace)
    key = ensure_key(namespace)
    for bucket in buckets:
        grant(key, bucket)
    write_secret(namespace, key)
    print(f"S3 endpoint: {ENDPOINT} (path-style addressing, region us-east-1)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("namespace", help="Namespace that receives the s3-credentials Secret")
    parser.add_argument("--bucket", action="append", choices=KNOWN_BUCKETS, dest="buckets",
                        help="Bucket to grant read and write on; repeat for several "
                             f"(default: {' and '.join(DEFAULT_BUCKETS)})")
    args = parser.parse_args(argv)
    if not NAMESPACE_PATTERN.fullmatch(args.namespace):
        parser.error(f"{args.namespace!r} is not a valid namespace name")
    buckets = list(dict.fromkeys(args.buckets or DEFAULT_BUCKETS))
    try:
        provision(args.namespace, buckets)
    except CredentialsError as error:
        print(f"ml-credentials failed: {error}", file=sys.stderr)
        return 1
    except (subprocess.SubprocessError, OSError) as error:
        print(f"ml-credentials failed: {error}. It is safe to rerun.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
