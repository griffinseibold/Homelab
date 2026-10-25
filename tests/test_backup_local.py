"""Opt-in archive/restore round trip with real tar on synthetic local volumes.

RUN_ROOT_TESTS=1 python3 -m unittest discover -s tests -p test_backup_local.py -v
Needs root or passwordless sudo (the backup copies volumes as root). It only
reads and writes inside a new temporary directory, never touches k3s, its
storage directory or any cgroup: freezing is replaced by a no-op.
"""
from contextlib import nullcontext
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

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup_local", ROOT / "scripts/backup-dev.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@unittest.skipUnless(os.environ.get("RUN_ROOT_TESTS") == "1", "opt-in test that copies files as root")
class LocalRecoveryTests(unittest.TestCase):
    def test_sqlite_data_and_permissions_survive_real_archive_restore(self):
        sudo = () if os.geteuid() == 0 else ("sudo", "-n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.addCleanup(subprocess.run, [*sudo, "rm", "-rf", "--", str(root / "volumes")], check=False)
            source_dir, target_dir = root / "volumes/source", root / "volumes/destination"
            source_dir.mkdir(parents=True)
            target_dir.mkdir()
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
            subprocess.run([*sudo, "tar", "--extract", "--same-owner", "--numeric-owner",
                            f"--file={seed}", f"--directory={source_dir}"], check=True)
            source = {"namespace": "test", "claim": "source", "node": "host",
                      "path": str(source_dir), "capacity": "1Gi", "archive": "test.tar"}
            target = dict(source, claim="destination", path=str(target_dir))
            directory = root / "snapshot"
            common = [patch.object(backup, "SUDO", sudo), patch.object(backup, "nodes", return_value=["host"]),
                      patch.object(backup, "freezer_cgroups", return_value=[]),
                      patch.object(backup, "paused", side_effect=lambda _: nullcontext()),
                      patch.object(backup, "run", side_effect=lambda *a, **k: None if a == ("sudo", "-v")
                                   else subprocess.run(a, check=True, **k))]
            with common[0], common[1], common[2], common[3], common[4], \
                 patch.object(backup, "volume_inventory", return_value=[source]), \
                 patch.object(backup, "registrations", return_value={"apiVersion": "v1", "kind": "List", "items": []}), \
                 patch.object(backup, "lab_mode", return_value="apps"):
                backup.create(directory)  # Real tar as root.
            with common[0], common[1], common[2], common[3], common[4], \
                 patch.object(backup, "volume_inventory", return_value=[target]), \
                 patch.object(backup, "require_unused_volume"):
                backup.restore(directory, "test/source", "test/destination")
            restored = subprocess.run([*sudo, "cat", str(target_dir / "fixture.db")],
                                      check=True, stdout=subprocess.PIPE).stdout
            self.assertEqual(content, restored)
            (root / "restored.db").write_bytes(restored)
            with sqlite3.connect(root / "restored.db") as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT value FROM records").fetchone()[0], "synthetic recovery record")
            metadata = subprocess.check_output([*sudo, "stat", "-c", "%u:%g:%a", str(target_dir / "fixture.db")],
                                               text=True).strip()
            self.assertEqual(metadata, "1000:1000:640")
            self.assertEqual(os.readlink(target_dir / "snapshots/revision/model"), "../../fixture.db")


if __name__ == "__main__":
    unittest.main()
