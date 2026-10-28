from __future__ import annotations

import os
from pathlib import Path


def db_path() -> Path:
    """Database location: $CAMPAIGN_DB, else ~/.local/share/campaign/campaign.db."""
    if env := os.environ.get("CAMPAIGN_DB"):
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "campaign" / "campaign.db"
