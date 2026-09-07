"""Opt-in recovery round trip on synthetic data in one disposable Docker container.

RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p test_backup_docker.py -v
Never creates, pauses, or writes to a Kind node or any existing container.
"""
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup_docker", ROOT / "scripts/backup-dev.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@unittest.skipUnless(os.environ.get("RUN_DOCKER_TESTS") == "1", "opt-in disposable Docker test")
class DockerRecoveryTests(unittest.TestCase):
    def test_sqlite_data_and_permissions_survive_real_archive_restore(self):
        container = "homelab-recovery-test-" + uuid.uuid4().hex[:12]
        subprocess.run(["docker", "run", "--detach", "--network", "none", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges", "--name", container,
                        "busybox:1.37.0", "sleep", "600"], check=True, stdout=subprocess.DEVNULL)
        self.addCleanup(subprocess.run, ["docker", "rm", "--force", container],
                        check=True, stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "fixture.db"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO records VALUES (1, 'synthetic recovery record')")
            content = database.read_bytes()
            seed = root / "seed.tar"
            with tarfile.open(seed, "w") as archive:
                entry = tarfile.TarInfo("fixture.db")
                entry.size, entry.uid, entry.gid, entry.mode = len(content), 1000, 1000, 0o640
                archive.addfile(entry, io.BytesIO(content))
                for name in ("snapshots", "snapshots/revision"):
                    entry = tarfile.TarInfo(name)
                    entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                    archive.addfile(entry)
                entry = tarfile.TarInfo("snapshots/revision/model")
                entry.type, entry.linkname = tarfile.SYMTYPE, "../../fixture.db"
                archive.addfile(entry)
            subprocess.run(["docker", "exec", container, "mkdir", "/source", "/destination"], check=True)
            with seed.open("rb") as handle:
                subprocess.run(["docker", "cp", "-a", "-", f"{container}:/source"], stdin=handle, check=True)
            source = {"namespace": "test", "claim": "source", "node": container,
                      "path": "/source", "capacity": "1Gi", "archive": "test.tar"}
            target = dict(source, claim="destination", path="/destination")
            directory = root / "snapshot"
            with patch.object(backup, "nodes", return_value=[container]), \
                 patch.object(backup, "volume_inventory", return_value=[source]), \
                 patch.object(backup, "registrations", return_value={"apiVersion": "v1", "kind": "List", "items": []}):
                backup.create(directory)  # Real Docker pause/copy/unpause.
            with patch.object(backup, "nodes", return_value=[container]), \
                 patch.object(backup, "volume_inventory", return_value=[target]), \
                 patch.object(backup, "require_unused_volume"):
                backup.restore(directory, "test/source", "test/destination")
            restored = root / "restored.db"
            subprocess.run(["docker", "cp", f"{container}:/destination/fixture.db", str(restored)], check=True)
            self.assertEqual(content, restored.read_bytes())
            with sqlite3.connect(restored) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT value FROM records").fetchone()[0], "synthetic recovery record")
            metadata = subprocess.check_output(["docker", "exec", container, "stat", "-c", "%u:%g:%a",
                                                "/destination/fixture.db"], text=True).strip()
            self.assertEqual(metadata, "1000:1000:640")
            link = subprocess.check_output(["docker", "exec", container, "readlink",
                                            "/destination/snapshots/revision/model"], text=True).strip()
            self.assertEqual(link, "../../fixture.db")
            linked_content = subprocess.check_output(["docker", "exec", "--user", "1000:1000", container, "cat",
                                                      "/destination/snapshots/revision/model"])
            self.assertEqual(linked_content, content)


if __name__ == "__main__":
    unittest.main()
