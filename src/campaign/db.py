"""SQLite storage: campaigns, reference material, the digest cache, sessions and characters.

The world model (entities, facts, events) lives in world.py on the same
database. Everything is in one file under the data directory (config.py).

The schema is versioned with `PRAGMA user_version`. Small changes are
migrations appended to MIGRATIONS. A database older than BASELINE is
retired instead: it's moved to backups/ and a fresh one is started,
carrying over what isn't world state and is costly to recreate (campaigns,
character sheets, reference uploads re-read from their stored originals,
and the digest cache).
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .blobs import BlobStore
from .ingest import CHUNKER_VERSION, ParsedDoc

SCHEMA = """
CREATE TABLE campaigns (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('dm', 'player')),
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- A document the campaign draws on: a published module, homebrew notes, or
-- a session transcript. `dm_only` sources are hidden in player campaigns.
CREATE TABLE sources (
    id               INTEGER PRIMARY KEY,
    campaign_id      INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('module', 'notes', 'transcript')),
    path             TEXT,
    dm_only          INTEGER NOT NULL DEFAULT 0,
    blob             TEXT,                 -- SHA-256 of the original upload
    filename         TEXT,
    chunker_version  INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    UNIQUE (campaign_id, title)
);

-- The document's own structure: outline entries, Markdown headings, or page ranges.
CREATE TABLE nodes (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    level       INTEGER NOT NULL,
    title       TEXT NOT NULL,
    page_start  INTEGER,
    page_end    INTEGER
);
CREATE INDEX nodes_source ON nodes(source_id, seq);

CREATE TABLE chunks (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    node_id    INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    seq        INTEGER NOT NULL,
    page       INTEGER,
    heading    TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL
);
CREATE INDEX chunks_source ON chunks(source_id, seq);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    heading, text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, heading, text) VALUES (new.id, new.heading, new.text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, heading, text) VALUES ('delete', old.id, old.heading, old.text);
END;

-- Digest outputs, addressed by a hash of everything that went into them.
-- Not tied to a source: re-uploading or re-chunking a document, or loading
-- the same module into another campaign, reuses every unchanged node.
CREATE TABLE digests (
    key            TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('leaf', 'branch')),
    output         TEXT NOT NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

-- The digest tree for a source as last planned: which node maps to which key.
CREATE TABLE digest_nodes (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES digest_nodes(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    depth       INTEGER NOT NULL,
    title       TEXT NOT NULL,
    page_start  INTEGER,
    page_end    INTEGER,
    is_leaf     INTEGER NOT NULL,
    chars       INTEGER NOT NULL DEFAULT 0,
    digest_key  TEXT,
    error       TEXT
);
CREATE INDEX digest_nodes_source ON digest_nodes(source_id, seq);

-- Sessions are the world's clock: facts are true from one session number
-- to another. Session 0 is the state of the world before play begins.
CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    number       INTEGER NOT NULL,
    played_on    TEXT,
    title        TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    source_id    INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    UNIQUE (campaign_id, number)
);

-- Everything that writes to the world model does so as a run: a digest
-- being applied, a session ingest, a chat, a manual edit. Deleting a run
-- undoes it (see the triggers and cascades below).
CREATE TABLE runs (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('book', 'ingest', 'chat', 'manual')),
    session      INTEGER,
    source_id    INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    input_key    TEXT,                     -- for book runs: the digest that was applied
    label        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

-- Identity only: one row per person, place or thing, whichever source
-- mentions it. What's true about it lives in facts.
CREATE TABLE entities (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (campaign_id, kind, name)
);

CREATE TABLE aliases (
    id         INTEGER PRIMARY KEY,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias      TEXT NOT NULL COLLATE NOCASE,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    UNIQUE (entity_id, alias)
);

-- A fact is true over a range of sessions, according to a source.
-- With an `attribute` it fills a single-valued slot (status, location,
-- holder, ...): recording a new value ends the old one, which stays as
-- history. Without one it's a statement that holds until explicitly ended.
CREATE TABLE facts (
    id                INTEGER PRIMARY KEY,
    campaign_id       INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    entity_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    attribute         TEXT,
    value             TEXT,
    statement         TEXT NOT NULL,
    modality          TEXT NOT NULL DEFAULT 'fact' CHECK (modality IN ('fact', 'possible', 'rumor')),
    valid_from        INTEGER NOT NULL,      -- session it became true; 0 = before play
    valid_to          INTEGER,               -- session it stopped being true; NULL = still true
    party_knows_from  INTEGER,               -- session the party learned it; NULL = they don't know
    source_id         INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    pages             TEXT NOT NULL DEFAULT '[]',
    run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    closed_by_run     INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX facts_entity ON facts(entity_id, attribute, valid_to);
CREATE VIRTUAL TABLE facts_fts USING fts5(
    statement, content='facts', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, statement) VALUES (new.id, new.statement);
END;
CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, statement) VALUES ('delete', old.id, old.statement);
END;
-- Undoing a run reopens the facts it ended.
CREATE TRIGGER runs_bd BEFORE DELETE ON runs BEGIN
    UPDATE facts SET valid_to = NULL, closed_by_run = NULL WHERE closed_by_run = old.id;
END;

-- What happened, in order.
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    session      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL
);
CREATE TABLE event_entities (
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, entity_id)
);

-- Synthesized entity summaries, keyed by a hash of the facts they were written from.
CREATE TABLE summaries (
    key         TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- People. Users sign in (when sign-in is on), belong to campaigns as a DM
-- or a player with owner, editor or viewer access, and arrive by invite.
CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

-- role decides what you see (players get the spoiler-filtered world);
-- access decides what you can change (owners also manage members and invites).
CREATE TABLE memberships (
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('dm', 'player')),
    access       TEXT NOT NULL CHECK (access IN ('owner', 'editor', 'viewer')),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (campaign_id, user_id)
);

-- Only a hash of each invite's token is stored; the link itself is shown once.
CREATE TABLE invites (
    id           INTEGER PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    campaign_id  INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,  -- NULL: app access only
    role         TEXT CHECK (role IN ('dm', 'player')),
    access       TEXT CHECK (access IN ('editor', 'viewer')),
    email        TEXT COLLATE NOCASE,                                  -- if set, only this account can use it
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    max_uses     INTEGER NOT NULL DEFAULT 1,
    uses         INTEGER NOT NULL DEFAULT 0,
    revoked      INTEGER NOT NULL DEFAULT 0
);

-- A sheet belongs to the person who made it and, optionally, a campaign.
-- (v5 makes the campaign required and links the world entity; see V5_CHARACTERS.)
CREATE TABLE characters (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    owner_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    sheet        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (campaign_id, name)
);
CREATE INDEX characters_owner ON characters(owner_id);

CREATE TABLE character_log (
    id            INTEGER PRIMARY KEY,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    change        TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
"""

# Databases older than this are retired and replaced, not migrated. While
# there's no data worth migrating, schema changes edit SCHEMA and bump this.
BASELINE = 4


# v5: a character belongs to exactly one campaign, is the same character as a
# player-character entity in that campaign's world (entity_id; linked when
# the sheet is next touched), and remembers the sheet it was copied from.
# Sheets that weren't in any campaign are dropped.
V5_CHARACTERS = """
DELETE FROM characters WHERE campaign_id IS NULL;
DELETE FROM character_log WHERE character_id NOT IN (SELECT id FROM characters);
CREATE TABLE characters_v5 (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    owner_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    sheet        TEXT NOT NULL,
    entity_id    INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    copied_from  INTEGER REFERENCES characters(id) ON DELETE SET NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (campaign_id, name)
);
INSERT INTO characters_v5 (id, campaign_id, owner_id, name, sheet, updated_at)
    SELECT id, campaign_id, owner_id, name, sheet, updated_at FROM characters;
DROP TABLE characters;
ALTER TABLE characters_v5 RENAME TO characters;
CREATE INDEX characters_owner ON characters(owner_id);
CREATE INDEX characters_entity ON characters(entity_id);
"""

# Append only, for changes small enough to migrate. Each step runs in its
# own transaction and then sets user_version to its number.
MIGRATIONS: list[tuple[int, str | Callable[[sqlite3.Connection], None]]] = [
    (BASELINE, SCHEMA),
    (5, V5_CHARACTERS),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# Words too common to rank anything by ("the hag" should search for "hag").
STOPWORDS = frozenset(
    "the a an and or of to in on at by for from with is are was were be been it its this that as into".split()
)


def fts_query(text: str) -> str:
    """Turn free text into an FTS5 query: every word optional, ranked by BM25.

    Quoting each term keeps punctuation in user input ("+2 longsword",
    "Strahd's") from being parsed as FTS syntax.
    """
    terms = re.findall(r"[\w']+", text.lower())
    return " OR ".join(f'"{t}"' for t in terms if len(t) > 1 and t not in STOPWORDS)


@dataclass
class Campaign:
    """A campaign as one person sees it.

    `role` is how they see it: a player sees the spoiler-filtered world.
    `access` is what they may change. Without sign-in, the person running
    the app is the owner, seeing it with the campaign's own role.
    """

    id: int
    slug: str
    name: str
    role: str
    notes: str
    access: str = "owner"

    @property
    def spoiler_safe(self) -> bool:
        return self.role == "player"


class Store:
    def __init__(self, path: Path | str, blob_dir: Path | str | None = None):
        self.path = str(path)
        memory = self.path == ":memory:"
        if not memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if blob_dir is None:
            if memory:
                self._tmp = tempfile.TemporaryDirectory()
                blob_dir = self._tmp.name
            else:
                blob_dir = Path(self.path).parent / "blobs"
        self.blobs = BlobStore(Path(blob_dir))
        self._carry_over: Path | None = None
        self._connect()
        self._migrate()

    def _connect(self) -> None:
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            # Lets the web UI read while a background job is writing.
            self.conn.execute("PRAGMA journal_mode = WAL")

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        current = self.schema_version
        has_tables = self.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        if has_tables and current < BASELINE:
            self._retire()
            current = 0
        pending = [(v, step) for v, step in MIGRATIONS if v > current]
        if not pending:
            return
        if current and self.path != ":memory:":
            self.backup(f"pre-v{pending[-1][0]}")
        # Steps may rebuild tables, which SQLite only allows safely with foreign
        # keys off (and that can't change inside a transaction); check after.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        for version, step in pending:
            try:
                if isinstance(step, str):
                    # executescript would commit between statements; wrap it so a
                    # failed step leaves the database exactly as it was.
                    self.conn.executescript(f"BEGIN;\n{step}\nPRAGMA user_version = {version};\nCOMMIT;")
                else:
                    self.conn.execute("BEGIN")
                    step(self.conn)
                    self.conn.execute(f"PRAGMA user_version = {version}")
                    self.conn.commit()
            except Exception:
                self.conn.rollback()
                self.conn.execute("PRAGMA foreign_keys = ON")
                raise
        self.conn.execute("PRAGMA foreign_keys = ON")
        if broken := self.conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError(f"migration left dangling references: {[tuple(r) for r in broken[:5]]}")
        if self._carry_over:
            self._restore_from(self._carry_over)

    def _retire(self) -> None:
        """Move a pre-baseline database to backups/ and start an empty one in its place."""
        self._carry_over = self.backup("retired")
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(self.path + suffix).unlink(missing_ok=True)
        self._connect()

    def _restore_from(self, old_db: Path) -> None:
        """Bring back campaigns, character sheets, the digest cache and reference uploads.

        World state, sessions and transcripts are not carried over. Reference
        sources are re-read from their stored originals, which re-plans their
        digests and re-applies the book's facts without any API calls.
        """
        self._carry_over = None
        old = sqlite3.connect(old_db)
        old.row_factory = sqlite3.Row
        tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        with self.conn:
            for r in old.execute("SELECT id, slug, name, role, notes, created_at FROM campaigns"):
                self.conn.execute("INSERT INTO campaigns VALUES (?, ?, ?, ?, ?, ?)", tuple(r))
            for r in old.execute("SELECT id, campaign_id, name, sheet, updated_at FROM characters"
                                 " WHERE campaign_id IS NOT NULL"):
                self.conn.execute(
                    "INSERT INTO characters (id, campaign_id, name, sheet, updated_at) VALUES (?, ?, ?, ?, ?)", tuple(r)
                )
            for r in old.execute("SELECT l.id, l.character_id, l.change, l.reason, l.created_at FROM character_log l"
                                 " JOIN characters c ON c.id = l.character_id WHERE c.campaign_id IS NOT NULL"):
                self.conn.execute("INSERT INTO character_log VALUES (?, ?, ?, ?, ?)", tuple(r))
            if "digests" in tables:
                for r in old.execute("SELECT * FROM digests"):
                    self.conn.execute("INSERT INTO digests VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(r))
        sources = []
        if "blob" in {r[1] for r in old.execute("PRAGMA table_info(sources)")}:
            sources = old.execute(
                "SELECT s.*, c.slug FROM sources s JOIN campaigns c ON c.id = s.campaign_id"
                " WHERE s.kind != 'transcript' AND s.blob IS NOT NULL ORDER BY s.id"
            ).fetchall()
        old.close()
        from .library import add_reference  # library -> digest -> db

        for src in sources:
            if self.blobs.exists(src["blob"]):
                add_reference(
                    self, self.campaign(src["slug"]), self.blobs.get(src["blob"]),
                    src["filename"] or f"{src['title']}.pdf", src["title"], src["kind"], bool(src["dm_only"]),
                )

    def backup(self, label: str = "manual") -> Path:
        """Snapshot the database into backups/, keeping the 20 most recent."""
        folder = Path(self.path).parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = folder / f"campaign-{stamp}-v{self.schema_version}-{label}.db"
        target = sqlite3.connect(dest)
        with target:
            self.conn.backup(target)
        target.close()
        for old in sorted(folder.glob("campaign-*.db"))[:-20]:
            old.unlink()
        return dest

    def close(self) -> None:
        self.conn.close()

    # -- campaigns -----------------------------------------------------------

    def create_campaign(self, name: str, role: str, notes: str = "") -> Campaign:
        """Create a campaign. Its slug is unique, so two people can both have a "Curse of Strahd"."""
        base = slugify(name) or "campaign"
        slug, n = base, 1
        while self.conn.execute("SELECT 1 FROM campaigns WHERE slug = ?", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO campaigns (slug, name, role, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                (slug, name, role, notes, now()),
            )
        return self.campaign(slug)

    def campaign(self, slug: str) -> Campaign:
        row = self.conn.execute(
            "SELECT id, slug, name, role, notes FROM campaigns WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no campaign {slug!r}")
        return Campaign(**dict(row))

    def campaign_by_id(self, campaign_id: int) -> Campaign:
        row = self.conn.execute(
            "SELECT id, slug, name, role, notes FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
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
        content: ParsedDoc | Iterable[dict[str, Any]],
        path: str | None = None,
        dm_only: bool = False,
        blob: str | None = None,
        filename: str | None = None,
        chunker_version: int | None = None,
    ) -> int:
        """Store a document, replacing the content of any source with the same title.

        Replacing keeps the source's id, so sessions and digests that refer to
        it stay attached.
        """
        if not isinstance(content, ParsedDoc):  # bare chunks: no section tree
            content = ParsedDoc(chunks=[{**c, "node": None} for c in content])
        doc = content
        existing = self.conn.execute(
            "SELECT id FROM sources WHERE campaign_id = ? AND title = ?", (campaign.id, title)
        ).fetchone()
        with self.conn:
            if existing:
                source_id = existing["id"]
                self.conn.execute(
                    "UPDATE sources SET kind = ?, path = ?, dm_only = ?, blob = ?, filename = ? WHERE id = ?",
                    (kind, path, int(dm_only), blob, filename, source_id),
                )
            else:
                source_id = self.conn.execute(
                    "INSERT INTO sources (campaign_id, title, kind, path, dm_only, blob, filename, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (campaign.id, title, kind, path, int(dm_only), blob, filename, now()),
                ).lastrowid
            self._write_content(source_id, doc, chunker_version or CHUNKER_VERSION)
        return source_id

    def replace_content(self, source_id: int, doc: ParsedDoc, chunker_version: int) -> None:
        with self.conn:
            self._write_content(source_id, doc, chunker_version)

    def _write_content(self, source_id: int, doc: ParsedDoc, chunker_version: int) -> None:
        self.conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
        self.conn.execute("DELETE FROM nodes WHERE source_id = ?", (source_id,))
        ids: list[int] = []
        for seq, n in enumerate(doc.nodes):  # parents always precede children
            ids.append(self.conn.execute(
                "INSERT INTO nodes (source_id, parent_id, seq, level, title, page_start, page_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source_id, None if n["parent"] is None else ids[n["parent"]], seq, n["level"],
                 n["title"], n.get("page_start"), n.get("page_end")),
            ).lastrowid)
        self.conn.executemany(
            "INSERT INTO chunks (source_id, seq, page, heading, text, node_id) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (source_id, i, c.get("page"), c.get("heading", ""), c["text"],
                 None if c.get("node") is None else ids[c["node"]])
                for i, c in enumerate(doc.chunks)
            ],
        )
        self.conn.execute(
            "UPDATE sources SET chunker_version = ? WHERE id = ?", (chunker_version, source_id)
        )

    def source(self, source_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return None if row is None else dict(row)

    def all_sources(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM sources ORDER BY id")]

    def sources(self, campaign: Campaign) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.id, s.title, s.kind, s.dm_only, s.filename, s.blob IS NOT NULL AS has_original,"
            " s.chunker_version, count(c.id) AS chunks,"
            " min(c.page) AS first_page, max(c.page) AS last_page"
            " FROM sources s LEFT JOIN chunks c ON c.source_id = s.id"
            " WHERE s.campaign_id = ? GROUP BY s.id ORDER BY s.id",
            (campaign.id,),
        )
        return [dict(r) for r in rows]

    def source_structure(self, source_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The section tree and its chunks, in document order."""
        nodes = self.conn.execute(
            "SELECT id, parent_id, seq, level, title, page_start, page_end FROM nodes"
            " WHERE source_id = ? ORDER BY seq",
            (source_id,),
        ).fetchall()
        chunks = self.conn.execute(
            "SELECT node_id, page, heading, text FROM chunks WHERE source_id = ? ORDER BY seq",
            (source_id,),
        ).fetchall()
        return [dict(n) for n in nodes], [dict(c) for c in chunks]

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

    # -- digest --------------------------------------------------------------

    def get_digest(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT output FROM digests WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row["output"])

    def put_digest(
        self, key: str, kind: str, output: dict[str, Any], model: str,
        input_tokens: int = 0, output_tokens: int = 0,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO digests (key, kind, output, model, input_tokens, output_tokens, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, kind, json.dumps(output, ensure_ascii=False), model, input_tokens, output_tokens, now()),
            )

    def save_digest_plan(self, source_id: int, rows: list[dict[str, Any]]) -> list[int]:
        """Replace a source's digest tree. `rows[i]["parent"]` indexes an earlier row."""
        ids: list[int] = []
        with self.conn:
            self.conn.execute("DELETE FROM digest_nodes WHERE source_id = ?", (source_id,))
            for seq, r in enumerate(rows):
                ids.append(self.conn.execute(
                    "INSERT INTO digest_nodes (source_id, parent_id, seq, depth, title, page_start,"
                    " page_end, is_leaf, chars, digest_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_id, None if r["parent"] is None else ids[r["parent"]], seq, r["depth"],
                     r["title"], r["page_start"], r["page_end"], int(r["is_leaf"]), r["chars"], r["key"]),
                ).lastrowid)
        return ids

    def set_digest_node(self, node_id: int, key: str | None, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE digest_nodes SET digest_key = coalesce(?, digest_key), error = ? WHERE id = ?",
                (key, error, node_id),
            )

    def digest_status(self, source_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT count(n.id) AS total, count(d.key) AS done, count(n.error) AS failed,"
            " coalesce(sum(d.input_tokens), 0) AS input_tokens,"
            " coalesce(sum(d.output_tokens), 0) AS output_tokens"
            " FROM digest_nodes n LEFT JOIN digests d ON d.key = n.digest_key"
            " WHERE n.source_id = ?",
            (source_id,),
        ).fetchone()
        return {**dict(row), "entities": len(self.source_catalog(source_id))}

    def digest_usage(self, source_id: int) -> list[dict[str, Any]]:
        """Tokens spent on a source's digested nodes, per model."""
        rows = self.conn.execute(
            "SELECT d.model, sum(d.input_tokens) AS input_tokens, sum(d.output_tokens) AS output_tokens"
            " FROM digest_nodes n JOIN digests d ON d.key = n.digest_key WHERE n.source_id = ? GROUP BY d.model",
            (source_id,),
        )
        return [dict(r) for r in rows]

    def digest_history(self, model: str) -> list[dict[str, Any]]:
        """Every digest call made with a model: kind, tokens, and a leaf's text size if still planned."""
        rows = self.conn.execute(
            "SELECT d.kind, d.input_tokens, d.output_tokens,"
            " (SELECT max(n.chars) FROM digest_nodes n WHERE n.digest_key = d.key) AS chars"
            " FROM digests d WHERE d.model = ? AND d.input_tokens > 0",
            (model,),
        )
        return [dict(r) for r in rows]

    def digest_node_output(self, node_id: int) -> tuple[int, dict[str, Any] | None] | None:
        """A digest node's source and its output (None until digested)."""
        row = self.conn.execute(
            "SELECT n.source_id, d.output FROM digest_nodes n LEFT JOIN digests d ON d.key = n.digest_key"
            " WHERE n.id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return row["source_id"], json.loads(row["output"]) if row["output"] else None

    def digest_outline(self, source_id: int) -> list[dict[str, Any]]:
        """The digest tree with each node's summary (None until digested)."""
        rows = self.conn.execute(
            "SELECT n.id, n.parent_id, n.depth, n.title, n.page_start, n.page_end, n.is_leaf,"
            " n.chars, n.error, d.output FROM digest_nodes n"
            " LEFT JOIN digests d ON d.key = n.digest_key WHERE n.source_id = ? ORDER BY n.seq",
            (source_id,),
        )
        out = []
        for r in rows:
            d = dict(r)
            output = json.loads(d.pop("output")) if r["output"] else None
            d["summary"] = output["summary"] if output else None
            d["entity_count"] = len(output["entities"]) if output else None
            out.append(d)
        return out

    def digest_node_source(self, node_id: int) -> int | None:
        row = self.conn.execute("SELECT source_id FROM digest_nodes WHERE id = ?", (node_id,)).fetchone()
        return None if row is None else row["source_id"]

    def source_catalog(self, source_id: int) -> list[dict[str, Any]]:
        """The entities the digest found in a source, once its root is digested."""
        row = self.conn.execute(
            "SELECT d.output FROM digest_nodes n JOIN digests d ON d.key = n.digest_key"
            " WHERE n.source_id = ? AND n.parent_id IS NULL",
            (source_id,),
        ).fetchone()
        return json.loads(row["output"])["entities"] if row else []

    def source_overviews(self, campaign: Campaign) -> list[dict[str, Any]]:
        """Root and top-level summaries for each digested, visible source."""
        out = []
        for src in self.conn.execute(
            f"SELECT s.id, s.title, s.kind FROM sources s WHERE {self._visible_sources_clause(campaign)}"
            " AND s.kind != 'transcript' ORDER BY s.id",
            (campaign.id,),
        ).fetchall():
            outline = self.digest_outline(src["id"])
            if not outline or outline[0]["summary"] is None:
                continue
            root = outline[0]
            sections = [n for n in outline if n["parent_id"] == root["id"]]
            out.append({"source": src["title"], "summary": root["summary"], "sections": sections})
        return out

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

    def current_session(self, campaign: Campaign) -> int:
        """The latest session played, or 0 before the first one."""
        row = self.conn.execute(
            "SELECT coalesce(max(number), 0) AS n FROM sessions WHERE campaign_id = ?", (campaign.id,)
        ).fetchone()
        return row["n"]

    def next_session_number(self, campaign: Campaign) -> int:
        row = self.conn.execute(
            "SELECT coalesce(max(number), 0) + 1 AS n FROM sessions WHERE campaign_id = ?",
            (campaign.id,),
        ).fetchone()
        return row["n"]

    # -- characters ----------------------------------------------------------

    @staticmethod
    def _character(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["sheet"] = json.loads(d["sheet"])
        return d

    def character(self, character_id: int) -> dict[str, Any] | None:
        """{id, campaign_id, owner_id, name, sheet, entity_id, copied_from, updated_at}, or None."""
        row = self.conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
        return None if row is None else self._character(row)

    def find_character(self, name: str, campaign: Campaign | None = None) -> dict[str, Any] | None:
        """A character by name: in `campaign` if given, else the first anywhere (for the CLI)."""
        sql, params = "SELECT * FROM characters WHERE name = ? COLLATE NOCASE", [name.strip()]
        if campaign is not None:
            sql += " AND campaign_id = ?"
            params.append(campaign.id)
        row = self.conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
        return None if row is None else self._character(row)

    def characters(
        self, campaign_ids: Iterable[int] | None = None, owner_id: int | None = None
    ) -> list[dict[str, Any]]:
        """All characters, or those in `campaign_ids` or owned by `owner_id` (either matches)."""
        sql = "SELECT * FROM characters"
        if campaign_ids is not None or owner_id is not None:
            ids = list(campaign_ids or [])
            sql += f" WHERE campaign_id IN ({','.join('?' * len(ids)) or 'NULL'}) OR owner_id = ?"
            params: list[Any] = [*ids, owner_id]
        else:
            params = []
        return [self._character(r) for r in self.conn.execute(sql + " ORDER BY name", params)]

    def create_character(
        self, sheet: dict[str, Any], campaign_id: int, owner_id: int | None = None,
        reason: str = "created", copied_from: int | None = None,
    ) -> int:
        """Add a character to a campaign and link it to its world entity.

        Raises sqlite3.IntegrityError if the campaign already has a character by that name.
        """
        with self.conn:
            character_id = self.conn.execute(
                "INSERT INTO characters (campaign_id, owner_id, name, sheet, copied_from, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (campaign_id, owner_id, sheet["name"], json.dumps(sheet), copied_from, now()),
            ).lastrowid
            self._log_character(character_id, sheet, reason)
        self.link_character(character_id)
        return character_id

    def update_character(self, character_id: int, sheet: dict[str, Any], reason: str = "") -> None:
        before = self.character(character_id)
        with self.conn:
            self.conn.execute(
                "UPDATE characters SET name = ?, sheet = ?, updated_at = ? WHERE id = ?",
                (sheet["name"], json.dumps(sheet), now(), character_id),
            )
            self._log_character(character_id, sheet, reason)
        entity_id = self.link_character(character_id)
        if before and before["name"] != sheet["name"]:
            self._world(before["campaign_id"]).rename_character(entity_id, sheet["name"])

    def _world(self, campaign_id: int) -> Any:
        from .world import World  # world -> db

        return World(self, self.campaign_by_id(campaign_id))

    def link_character(self, character_id: int) -> int:
        """The id of the player-character entity this sheet is, linking one if it has none yet."""
        ch = self.character(character_id)
        world = self._world(ch["campaign_id"])
        if ch["entity_id"] is not None and world.entity(ch["entity_id"]):
            return ch["entity_id"]
        entity_id = world.character_entity(ch["name"], character_id)
        with self.conn:
            self.conn.execute("UPDATE characters SET entity_id = ? WHERE id = ?", (entity_id, character_id))
        return entity_id

    def character_for_entity(self, entity_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT id, name FROM characters WHERE entity_id = ?", (entity_id,)).fetchone()
        return None if row is None else dict(row)

    def _log_character(self, character_id: int, sheet: dict[str, Any], reason: str) -> None:
        self.conn.execute(
            "INSERT INTO character_log (character_id, change, reason, created_at) VALUES (?, ?, ?, ?)",
            (character_id, json.dumps(sheet), reason, now()),
        )

    def delete_character(self, character_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM characters WHERE id = ?", (character_id,)).rowcount > 0

    def character_log(self, character_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT created_at, reason FROM character_log WHERE character_id = ? ORDER BY id DESC LIMIT ?",
            (character_id, limit),
        )
        return [dict(r) for r in rows]
