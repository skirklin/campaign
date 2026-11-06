"""Nightly snapshots: taken while the app runs, kept a week, and handed out only to admins or the backup token."""

import gzip
import sqlite3

import pytest
from fastapi.testclient import TestClient

from campaign import backups
from campaign.db import Store
from campaign.web import create_app


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "campaign.db"
    s = Store(path)
    s.create_campaign("Strahd", "dm")
    s.close()
    return path


def test_a_snapshot_is_the_whole_database_gzipped(db, tmp_path):
    assert backups.due(db) and backups.latest(db) is None
    snap = backups.snapshot(db)
    assert snap.name.startswith("nightly-") and snap.name.endswith(".db.gz") and not backups.due(db)
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(snap.read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT name FROM campaigns").fetchall() == [("Strahd",)]
    assert [p.name for p in backups.folder(db).iterdir()] == [snap.name]  # no temporary files left behind


def test_only_the_newest_week_is_kept(db):
    folder = backups.folder(db)
    folder.mkdir()
    for day in range(1, 11):
        (folder / f"nightly-202610{day:02d}.db.gz").write_bytes(b"old")
    backups.snapshot(db)
    names = [p.name for p in backups.nightlies(db)]
    assert len(names) == backups.KEEP and names[0] == "nightly-20261005.db.gz"


def test_the_loop_takes_one_when_due(db):
    loop = backups.BackupLoop(db)
    loop.pass_once()
    loop.pass_once()
    assert len(backups.nightlies(db)) == 1


def test_downloads_need_the_token(db, monkeypatch):
    client = TestClient(create_app(db))
    assert client.get("/api/admin/backups/latest").status_code == 404  # none yet
    backups.snapshot(db)
    assert client.get("/api/admin/backups").json()[0]["name"].startswith("nightly-")
    assert client.get("/api/admin/backups/latest").headers["content-type"] == "application/gzip"
    assert client.get("/backups/latest").status_code == 404  # no token configured: not there at all
    monkeypatch.setenv("CAMPAIGN_BACKUP_TOKEN", "s3cret")
    assert client.get("/backups/latest", headers={"Authorization": "Bearer wrong"}).status_code == 404
    got = client.get("/backups/latest", headers={"Authorization": "Bearer s3cret"})
    assert got.status_code == 200 and gzip.decompress(got.content)[:16] == b"SQLite format 3\x00"
