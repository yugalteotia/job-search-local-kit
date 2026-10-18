"""Fail-closed checks for the isolated, no-submit Naukri prototype."""
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import naukri_direct_playwright as direct


class PrivateProfileTests(unittest.TestCase):
    def test_profile_is_owner_only_and_outside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            with patch.object(direct, "PRIVATE_ROOT", root), patch.object(direct, "PROFILE", root / "user-data"):
                profile = direct._private_profile()
                self.assertFalse(profile.is_relative_to(ROOT))
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)

    def test_symlinked_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            root.mkdir()
            (root / "user-data").symlink_to(Path(tmp) / "elsewhere")
            with patch.object(direct, "PRIVATE_ROOT", root), patch.object(direct, "PROFILE", root / "user-data"):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    direct._private_profile()


class DedupeGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "jobs.sqlite3"
        db = sqlite3.connect(self.path)
        db.executescript("""
          CREATE TABLE jobs(id INTEGER PRIMARY KEY,source TEXT,state TEXT,employer_key TEXT,
                            requisition_key TEXT,canonical_url TEXT,fit TEXT);
          CREATE TABLE submission_attempts(job_id INTEGER);
          CREATE TABLE history_keys(employer_key TEXT,requisition_key TEXT);
        """)
        db.executemany("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)", [
            (1, "naukri_portal_native", "Verified", "example", "req:1",
             "https://www.naukri.com/job-listings-example-1", "Medium"),
            (2, "naukri_portal_native", "Applied", "example", "req:2",
             "https://www.naukri.com/job-listings-example-2", "High"),
            (3, "naukri_portal_native", "Verified", "example", "req:3",
             "https://www.naukri.com/job-listings-example-3", "High"),
        ])
        db.execute("INSERT INTO submission_attempts VALUES(3)")
        db.commit()
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_unapplied_unclaimed_naukri_job_can_be_inspected(self):
        def connection():
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            return db
        with patch.object(direct, "connect", connection):
            self.assertEqual(direct._job_for_readonly_inspection(1)["job_id"], 1)
            with self.assertRaisesRegex(RuntimeError, "Existing or uncertain"):
                direct._job_for_readonly_inspection(2)
            with self.assertRaisesRegex(RuntimeError, "Existing attempt"):
                direct._job_for_readonly_inspection(3)


if __name__ == "__main__":
    unittest.main()
