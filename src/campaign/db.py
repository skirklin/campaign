"""SQLite storage for campaigns, reference material, campaign state and characters.

One database file holds every campaign. Reference text is chunked and indexed
with FTS5 so the agent can search it; campaign state is a set of entities
(NPCs, quests, locations, ...) plus an append-only journal of events. Every
entity write keeps a revision so a bad session ingest can be inspected and
rolled back.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('dm', 'player')),
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- A document the campaign draws on: a published module, homebrew notes,
-- or a session transcript. `dm_only` sources are hidden from the agent in
-- player campaigns so it can't spoil the module.
CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('module', 'notes', 'transcript')),
    path         TEXT,
    dm_only      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    UNIQUE (campaign_id, title)
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    page       INTEGER,
    heading    TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    heading, text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, heading, text) VALUES (new.id, new.heading, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, heading, text)
    VALUES ('delete', old.id, old.heading, old.text);
END;

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    number       INTEGER NOT NULL,
    played_on    TEXT,
    title        TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    source_id    INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    UNIQUE (campaign_id, number)
);

-- Anything in the world worth tracking. `data` holds kind-specific detail
-- (quest objectives and rewards, NPC disposition, ...) as JSON.
CREATE TABLE IF NOT EXISTS entities (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    data         TEXT NOT NULL DEFAULT '{}',
    party_knows  INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL,
    UNIQUE (campaign_id, kind, name)
);

CREATE TABLE IF NOT EXISTS entity_revisions (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    session_id  INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    snapshot    TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- The campaign journal: what happened, in order.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    session_id   INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    text         TEXT NOT NULL,
    entity_ids   TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS characters (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    name         TEXT NOT NULL UNIQUE,
    sheet        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_log (
    id            INTEGER PRIMARY KEY,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    change        TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fts_query(text: str) -> str:
    """Turn free text into an FTS5 query: every word optional, ranked by BM25.

    Quoting each term keeps punctuation in user input ("+2 longsword",
    "Strahd's") from being parsed as FTS syntax.
    """
    terms = re.findall(r"[\w']+", text.lower())
    return " OR ".join(f'"{t}"' for t in terms if len(t) > 1)


@dataclass
class Campaign:
    id: int
    slug: str
    name: str
    role: str
    notes: str

    @property
    def spoiler_safe(self) -> bool:
        return self.role == "player"


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            # Lets the web UI read while a background ingest is writing.
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- campaigns -----------------------------------------------------------

    def create_campaign(self, name: str, role: str, notes: str = "") -> Campaign:
        with self.conn:
            self.conn.execute(
                "INSERT INTO campaigns (slug, name, role, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                (slugify(name), name, role, notes, now()),
            )
        return self.campaign(slugify(name))

    def campaign(self, slug: str) -> Campaign:
        row = self.conn.execute(
            "SELECT id, slug, name, role, notes FROM campaigns WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no campaign {slug!r}")
        return Campaign(**dict(row))

    def campaigns(self) -> list[Campaign]:
        rows = self.conn.execute("SELECT id, slug, name, role, notes FROM campaigns ORDER BY id")
        return [Campaign(**dict(r)) for r in rows]

    # -- reference material --------------------------------------------------

    def add_source(
        self,
        campaign: Campaign,
        title: str,
        kind: str,
        chunks: Iterable[dict[str, Any]],
        path: str | None = None,
        dm_only: bool = False,
    ) -> int:
        """Store a document as ordered chunks, replacing any source with the same title."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM sources WHERE campaign_id = ? AND title = ?", (campaign.id, title)
            )
            cur = self.conn.execute(
                "INSERT INTO sources (campaign_id, title, kind, path, dm_only, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (campaign.id, title, kind, path, int(dm_only), now()),
            )
            source_id = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO chunks (source_id, seq, page, heading, text) VALUES (?, ?, ?, ?, ?)",
                [
                    (source_id, i, c.get("page"), c.get("heading", ""), c["text"])
                    for i, c in enumerate(chunks)
                ],
            )
        return source_id

    def sources(self, campaign: Campaign) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.id, s.title, s.kind, s.dm_only, count(c.id) AS chunks,"
            " min(c.page) AS first_page, max(c.page) AS last_page"
            " FROM sources s LEFT JOIN chunks c ON c.source_id = s.id"
            " WHERE s.campaign_id = ? GROUP BY s.id ORDER BY s.id",
            (campaign.id,),
        )
        return [dict(r) for r in rows]

    def delete_source(self, campaign: Campaign, source_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM sources WHERE id = ? AND campaign_id = ?", (source_id, campaign.id)
            )
        return cur.rowcount > 0

    def _visible_sources_clause(self, campaign: Campaign) -> str:
        return "s.campaign_id = ?" + (" AND s.dm_only = 0" if campaign.spoiler_safe else "")

    def search(
        self, campaign: Campaign, query: str, limit: int = 8, source: str | None = None
    ) -> list[dict[str, Any]]:
        q = fts_query(query)
        if not q:
            return []
        sql = (
            "SELECT c.id, s.title AS source, c.page, c.heading,"
            " snippet(chunks_fts, 1, '[', ']', ' … ', 48) AS snippet"
            " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " JOIN sources s ON s.id = c.source_id"
            f" WHERE chunks_fts MATCH ? AND {self._visible_sources_clause(campaign)}"
        )
        params: list[Any] = [q, campaign.id]
        if source:
            sql += " AND s.title = ?"
            params.append(source)
        sql += " ORDER BY bm25(chunks_fts, 4.0, 1.0) LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def read_chunks(
        self, campaign: Campaign, chunk_id: int, before: int = 0, after: int = 0
    ) -> list[dict[str, Any]]:
        """Return a chunk plus neighbours from the same source (stat blocks often span chunks)."""
        anchor = self.conn.execute(
            "SELECT c.source_id, c.seq FROM chunks c JOIN sources s ON s.id = c.source_id"
            f" WHERE c.id = ? AND {self._visible_sources_clause(campaign)}",
            (chunk_id, campaign.id),
        ).fetchone()
        if anchor is None:
            return []
        rows = self.conn.execute(
            "SELECT c.id, s.title AS source, c.page, c.heading, c.text"
            " FROM chunks c JOIN sources s ON s.id = c.source_id"
            " WHERE c.source_id = ? AND c.seq BETWEEN ? AND ? ORDER BY c.seq",
            (anchor["source_id"], anchor["seq"] - before, anchor["seq"] + after),
        )
        return [dict(r) for r in rows]

    def read_pages(
        self, campaign: Campaign, source: str, first: int, last: int
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT c.id, c.page, c.heading, c.text FROM chunks c"
            " JOIN sources s ON s.id = c.source_id"
            f" WHERE s.title = ? AND c.page BETWEEN ? AND ? AND {self._visible_sources_clause(campaign)}"
            " ORDER BY c.seq",
            (source, first, last, campaign.id),
        )
        return [dict(r) for r in rows]

    # -- sessions ------------------------------------------------------------

    def upsert_session(
        self,
        campaign: Campaign,
        number: int,
        played_on: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        source_id: int | None = None,
    ) -> int:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions (campaign_id, number) VALUES (?, ?)"
                " ON CONFLICT (campaign_id, number) DO NOTHING",
                (campaign.id, number),
            )
            for col, val in (
                ("played_on", played_on),
                ("title", title),
                ("summary", summary),
                ("source_id", source_id),
            ):
                if val is not None:
                    self.conn.execute(
                        f"UPDATE sessions SET {col} = ? WHERE campaign_id = ? AND number = ?",
                        (val, campaign.id, number),
                    )
        row = self.conn.execute(
            "SELECT id FROM sessions WHERE campaign_id = ? AND number = ?", (campaign.id, number)
        ).fetchone()
        return row["id"]

    def sessions(self, campaign: Campaign) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, number, played_on, title, summary FROM sessions"
            " WHERE campaign_id = ? ORDER BY number",
            (campaign.id,),
        )
        return [dict(r) for r in rows]

    def next_session_number(self, campaign: Campaign) -> int:
        row = self.conn.execute(
            "SELECT coalesce(max(number), 0) + 1 AS n FROM sessions WHERE campaign_id = ?",
            (campaign.id,),
        ).fetchone()
        return row["n"]

    # -- entities ------------------------------------------------------------

    @staticmethod
    def _entity(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["data"] = json.loads(d["data"])
        d["party_knows"] = bool(d["party_knows"])
        return d

    def find_entities(
        self,
        campaign: Campaign,
        kind: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM entities WHERE campaign_id = ?"
        params: list[Any] = [campaign.id]
        if campaign.spoiler_safe:
            sql += " AND party_knows = 1"
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if name:
            sql += " AND (name LIKE ? OR summary LIKE ?)"
            params += [f"%{name}%", f"%{name}%"]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY kind, name"
        return [self._entity(r) for r in self.conn.execute(sql, params)]

    def get_entity(self, campaign: Campaign, kind: str, name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM entities WHERE campaign_id = ? AND kind = ? AND name = ? COLLATE NOCASE",
            (campaign.id, kind, name),
        ).fetchone()
        if row is None or (campaign.spoiler_safe and not row["party_knows"]):
            return None
        return self._entity(row)

    def upsert_entity(
        self,
        campaign: Campaign,
        kind: str,
        name: str,
        *,
        status: str | None = None,
        summary: str | None = None,
        data: dict[str, Any] | None = None,
        party_knows: bool | None = None,
        session_id: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Create or update an entity. `data` is merged key-by-key (None deletes a key)."""
        existing = self.conn.execute(
            "SELECT * FROM entities WHERE campaign_id = ? AND kind = ? AND name = ? COLLATE NOCASE",
            (campaign.id, kind, name),
        ).fetchone()
        with self.conn:
            if existing is None:
                cur = self.conn.execute(
                    "INSERT INTO entities (campaign_id, kind, name, status, summary, data,"
                    " party_knows, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        campaign.id,
                        kind,
                        name,
                        status or "",
                        summary or "",
                        json.dumps({k: v for k, v in (data or {}).items() if v is not None}),
                        int(True if party_knows is None else party_knows),
                        now(),
                    ),
                )
                entity_id = cur.lastrowid
            else:
                entity_id = existing["id"]
                merged = json.loads(existing["data"])
                for k, v in (data or {}).items():
                    if v is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                self.conn.execute(
                    "UPDATE entities SET status = ?, summary = ?, data = ?, party_knows = ?,"
                    " updated_at = ? WHERE id = ?",
                    (
                        existing["status"] if status is None else status,
                        existing["summary"] if summary is None else summary,
                        json.dumps(merged),
                        existing["party_knows"] if party_knows is None else int(party_knows),
                        now(),
                        entity_id,
                    ),
                )
            row = self.conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
            entity = self._entity(row)
            self.conn.execute(
                "INSERT INTO entity_revisions (entity_id, session_id, snapshot, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (entity_id, session_id, json.dumps(entity), reason, now()),
            )
        return entity

    def entity_history(self, entity_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.id, r.created_at, r.reason, r.snapshot, s.number AS session"
            " FROM entity_revisions r LEFT JOIN sessions s ON s.id = r.session_id"
            " WHERE r.entity_id = ? ORDER BY r.id",
            (entity_id,),
        )
        return [{**dict(r), "snapshot": json.loads(r["snapshot"])} for r in rows]

    def revert_session(self, campaign: Campaign, number: int) -> dict[str, int]:
        """Undo everything an ingest of session `number` wrote to entities and the journal.

        Each touched entity goes back to its last revision from before the
        session, or is deleted if the session created it. Later sessions'
        edits to the same entity are lost, so revert newest-first.
        """
        session = self.conn.execute(
            "SELECT id FROM sessions WHERE campaign_id = ? AND number = ?", (campaign.id, number)
        ).fetchone()
        if session is None:
            raise KeyError(f"no session {number}")
        sid = session["id"]
        touched = self.conn.execute(
            "SELECT entity_id, min(id) AS first_rev FROM entity_revisions"
            " WHERE session_id = ? GROUP BY entity_id",
            (sid,),
        ).fetchall()
        restored = deleted = 0
        with self.conn:
            for t in touched:
                prior = self.conn.execute(
                    "SELECT snapshot FROM entity_revisions WHERE entity_id = ? AND id < ?"
                    " ORDER BY id DESC LIMIT 1",
                    (t["entity_id"], t["first_rev"]),
                ).fetchone()
                if prior is None:
                    self.conn.execute("DELETE FROM entities WHERE id = ?", (t["entity_id"],))
                    deleted += 1
                    continue
                snap = json.loads(prior["snapshot"])
                self.conn.execute(
                    "UPDATE entities SET status = ?, summary = ?, data = ?, party_knows = ?,"
                    " updated_at = ? WHERE id = ?",
                    (
                        snap["status"],
                        snap["summary"],
                        json.dumps(snap["data"]),
                        int(snap["party_knows"]),
                        now(),
                        t["entity_id"],
                    ),
                )
                self.conn.execute(
                    "DELETE FROM entity_revisions WHERE entity_id = ? AND id >= ?",
                    (t["entity_id"], t["first_rev"]),
                )
                restored += 1
            events = self.conn.execute("DELETE FROM events WHERE session_id = ?", (sid,)).rowcount
            self.conn.execute(
                "UPDATE sessions SET summary = '', title = '' WHERE id = ?", (sid,)
            )
        return {"restored": restored, "deleted": deleted, "events": events}

    # -- journal -------------------------------------------------------------

    def add_event(
        self,
        campaign: Campaign,
        text: str,
        session_id: int | None = None,
        entity_ids: list[int] | None = None,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (campaign_id, session_id, text, entity_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (campaign.id, session_id, text, json.dumps(entity_ids or []), now()),
            )
        return cur.lastrowid

    def events(
        self, campaign: Campaign, session_number: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT e.id, e.text, e.entity_ids, s.number AS session, e.created_at FROM events e"
            " LEFT JOIN sessions s ON s.id = e.session_id WHERE e.campaign_id = ?"
        )
        params: list[Any] = [campaign.id]
        if session_number is not None:
            sql += " AND s.number = ?"
            params.append(session_number)
        sql += " ORDER BY e.id DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        for r in rows:
            r["entity_ids"] = json.loads(r["entity_ids"])
        return list(reversed(rows))

    # -- characters ----------------------------------------------------------

    def save_character(
        self, name: str, sheet: dict[str, Any], campaign: Campaign | None = None, reason: str = ""
    ) -> None:
        with self.conn:
            existing = self.conn.execute(
                "SELECT id FROM characters WHERE name = ?", (name,)
            ).fetchone()
            if existing is None:
                cur = self.conn.execute(
                    "INSERT INTO characters (campaign_id, name, sheet, updated_at) VALUES (?, ?, ?, ?)",
                    (campaign.id if campaign else None, name, json.dumps(sheet), now()),
                )
                character_id = cur.lastrowid
            else:
                character_id = existing["id"]
                self.conn.execute(
                    "UPDATE characters SET sheet = ?, updated_at = ?"
                    + (", campaign_id = ?" if campaign else "")
                    + " WHERE id = ?",
                    (json.dumps(sheet), now(), *([campaign.id] if campaign else []), character_id),
                )
            self.conn.execute(
                "INSERT INTO character_log (character_id, change, reason, created_at)"
                " VALUES (?, ?, ?, ?)",
                (character_id, json.dumps(sheet), reason, now()),
            )

    def character(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT sheet FROM characters WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        return None if row is None else json.loads(row["sheet"])

    def characters(self, campaign: Campaign | None = None) -> list[str]:
        if campaign is None:
            rows = self.conn.execute("SELECT name FROM characters ORDER BY name")
        else:
            rows = self.conn.execute(
                "SELECT name FROM characters WHERE campaign_id = ? ORDER BY name", (campaign.id,)
            )
        return [r["name"] for r in rows]

    def rename_character(self, old: str, new: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE characters SET name = ? WHERE name = ? COLLATE NOCASE", (new, old)
            )

    def delete_character(self, name: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM characters WHERE name = ? COLLATE NOCASE", (name,))
        return cur.rowcount > 0

    def character_log(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT l.created_at, l.reason FROM character_log l"
            " JOIN characters c ON c.id = l.character_id"
            " WHERE c.name = ? COLLATE NOCASE ORDER BY l.id DESC LIMIT ?",
            (name, limit),
        )
        return [dict(r) for r in rows]
