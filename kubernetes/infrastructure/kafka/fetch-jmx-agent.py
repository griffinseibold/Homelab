#!/usr/bin/env python3
"""Keep a verified copy of the Prometheus JMX exporter agent on Kafka's volume.

Runs as Kafka's init container. The official apache/kafka image has no
Prometheus endpoint, and the JMX exporter project publishes its Java agent as
a release jar rather than an image, so this fetches the pinned jar and keeps it
on the data volume. Every start checks the file's SHA-256 and downloads it
again only when it is missing or does not match. After the first start Kafka
no longer needs GitHub to come up, for example after a reboot.

A download that does not match the pinned hash is discarded and the container
fails, so Kafka never loads an unverified agent; the kubelet retries it.

Standard library only, like the other in-cluster scripts, so it runs in an
unmodified official Python image.
"""

import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request

CHUNK = 1 << 20
PARTIAL_PREFIX = ".download-"


class FetchError(Exception):
    """A problem the message explains; the init container exits with it."""


def log(message):
    print(message, flush=True)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_leftovers(directory, keep):
    """Remove agents of other versions and partial downloads from earlier runs."""
    for path in directory.iterdir():
        if path.name == keep or not path.is_file():
            continue
        if path.suffix == ".jar" or path.name.startswith(PARTIAL_PREFIX):
            path.unlink()
            log(f"Removed {path.name}")


def ensure_agent(url, expected, target, opener=urllib.request.urlopen, timeout=60):
    """Make target a file with SHA-256 expected; return True if it was downloaded."""
    expected = expected.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise FetchError(f"AGENT_SHA256 must be 64 hexadecimal characters; got {expected!r}")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    remove_leftovers(target.parent, target.name)
    if target.is_file():
        if sha256_of(target) == expected:
            log(f"{target} is present and verified")
            return False
        log(f"{target} does not match the pinned SHA-256; downloading it again")

    descriptor, partial = tempfile.mkstemp(dir=target.parent, prefix=PARTIAL_PREFIX, suffix=".partial")
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as out:
            try:
                with opener(url, timeout=timeout) as response:
                    for chunk in iter(lambda: response.read(CHUNK), b""):
                        digest.update(chunk)
                        out.write(chunk)
            except (urllib.error.URLError, OSError) as error:
                raise FetchError(f"Could not download {url}: {error}") from error
            out.flush()
            os.fsync(out.fileno())
        actual = digest.hexdigest()
        if actual != expected:
            raise FetchError(f"{url} has SHA-256 {actual}, not the pinned {expected}; refusing to use it")
        os.chmod(partial, 0o444)
        # Atomic on one filesystem, so Kafka never sees half a jar.
        os.replace(partial, target)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise
    log(f"Downloaded and verified {target}")
    return True


def main():
    try:
        settings = {name: os.environ[name] for name in ("AGENT_URL", "AGENT_SHA256", "AGENT_PATH")}
    except KeyError as missing:
        print(f"Missing environment variable {missing}", file=sys.stderr)
        return 1
    try:
        ensure_agent(settings["AGENT_URL"], settings["AGENT_SHA256"], settings["AGENT_PATH"])
    except FetchError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
