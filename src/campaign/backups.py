"""Nightly snapshots of the database, and the newest one for whoever keeps a copy elsewhere.

Once a day the backup loop takes a consistent copy of the database (SQLite's
online backup, so the app keeps running), gzips it into `backups/` as
`nightly-<UTC date>.db.gz`, and keeps the newest KEEP. They sit beside the
snapshots taken before each schema upgrade (db.Store.backup), on the same
volume: they undo a mistake, a bad import or a broken upgrade, but not the
loss of the volume. For that, something off the volume copies the newest
one away: an admin downloads it from the Admin page, or a job fetches
/api/backups/latest with the CAMPAIGN_BACKUP_TOKEN (docs/deploy.md).

The originals (uploaded books and scans, in blobs/) aren't in a snapshot:
the database holds everything the app reads from them.

To restore: stop the server, `gunzip -c nightly-….db.gz > campaign.db` in
the data directory, start it again.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import Store
from .loops import LeasedLoop

KEEP = 7
EVERY_SECONDS = 24 * 3600
PATTERN = "nightly-*.db.gz"


def folder(db: Path | str) -> Path:
    return Path(db).parent / "backups"


def nightlies(db: Path | str) -> list[Path]:
    """The nightly snapshots, oldest first."""
    return sorted(folder(db).glob(PATTERN))


def latest(db: Path | str) -> Path | None:
    found = nightlies(db)
    return found[-1] if found else None


def snapshot(db: Path | str) -> Path:
    """Take tonight's snapshot (replacing one already taken today), and keep the newest KEEP."""
    out = folder(db)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest = out / f"nightly-{stamp}.db.gz"
    raw = out / f".nightly-{stamp}.db.tmp"
    part = out / f".nightly-{stamp}.db.gz.tmp"
    try:
        s = Store(db)
        try:
            target = sqlite3.connect(raw)
            with target:
                s.conn.backup(target)
            target.close()
        finally:
            s.close()
        with open(raw, "rb") as src, gzip.open(part, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(src, gz, 1 << 20)
        part.replace(dest)  # never a half-written snapshot under the real name
    finally:
        raw.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    for old in nightlies(db)[:-KEEP]:
        old.unlink()
    return dest


def due(db: Path | str, now: float | None = None) -> bool:
    """Whether a day has passed since the newest snapshot (or there's none)."""
    newest = latest(db)
    return newest is None or (now or time.time()) - newest.stat().st_mtime >= EVERY_SECONDS - 3600


class BackupLoop(LeasedLoop):
    """Takes the nightly snapshot when one is due; looks again every hour."""

    LEASE = "backup"
    IDLE_SECONDS = 3600

    def pass_once(self) -> bool:
        if due(self.db) and self.holds_lease():
            snapshot(self.db)
        return False
