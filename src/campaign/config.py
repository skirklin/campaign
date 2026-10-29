"""Where campaign data lives.

Everything is under one data directory, outside the code checkout, so
reinstalling, switching branches or re-cloning never touches it:

    <data dir>/campaign.db     SQLite database
    <data dir>/blobs/          original uploaded files, named by SHA-256
    <data dir>/backups/        database snapshots taken before each schema migration

The data directory is $CAMPAIGN_HOME, else $XDG_DATA_HOME/campaign, else
~/.local/share/campaign. $CAMPAIGN_DB can point at a different database file;
its blobs and backups then sit next to it.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    if env := os.environ.get("CAMPAIGN_HOME"):
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "campaign"


def db_path() -> Path:
    if env := os.environ.get("CAMPAIGN_DB"):
        return Path(env).expanduser()
    return data_dir() / "campaign.db"
