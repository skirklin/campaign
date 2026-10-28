"""SQLite storage for campaigns, reference material, campaign state and characters.

One database file holds every campaign. Reference text is chunked and indexed
with FTS5 so the agent can search it; campaign state is a set of entities
(NPCs, quests, locations, ...) plus an append-only journal of events. Every
entity write keeps a revision so a bad session ingest can be inspected and
rolled back.

The schema is versioned with `PRAGMA user_version`. Opening a database runs
any pending migrations, after snapshotting the file to `backups/`, so data
uploaded with an older build keeps working with a newer one.
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

SCHEMA_V1 = """
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


# v2: keep originals, a section tree per source, and the digest.
SCHEMA_V2 = """
ALTER TABLE sources ADD COLUMN blob TEXT;          -- SHA-256 of the original upload
ALTER TABLE sources ADD COLUMN filename TEXT;
ALTER TABLE sources ADD COLUMN chunker_version INTEGER NOT NULL DEFAULT 1;

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
ALTER TABLE chunks ADD COLUMN node_id INTEGER REFERENCES nodes(id) ON DELETE SET NULL;

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

-- The digest tree for a source as last planned: which node maps to which
-- digest key. Rebuilt on every plan; cheap, since outputs live in `digests`.
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

-- What the book says about each NPC, monster, place, item and quest,
-- materialized from the root digest. Campaign state stays in `entities`.
CREATE TABLE ref_entities (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    aliases     TEXT NOT NULL DEFAULT '[]',
    summary     TEXT NOT NULL DEFAULT '',
    pages       TEXT NOT NULL DEFAULT '[]',
    facts       TEXT NOT NULL DEFAULT '[]',
    stat_block  TEXT
);
CREATE INDEX ref_entities_source ON ref_entities(source_id);
CREATE VIRTUAL TABLE ref_entities_fts USING fts5(
    name, aliases, summary, content='ref_entities', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER ref_entities_ai AFTER INSERT ON ref_entities BEGIN
    INSERT INTO ref_entities_fts(rowid, name, aliases, summary) VALUES (new.id, new.name, new.aliases, new.summary);
END;
CREATE TRIGGER ref_entities_ad AFTER DELETE ON ref_entities BEGIN
    INSERT INTO ref_entities_fts(ref_entities_fts, rowid, name, aliases, summary)
    VALUES ('delete', old.id, old.name, old.aliases, old.summary);
END;
"""


# Append only. Each step runs in its own transaction, then user_version is
# set to its number; a database is migrated by running every later step.
MIGRATIONS: list[tuple[int, str | Callable[[sqlite3.Connection], None]]] = [
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


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
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if not memory:
            # Lets the web UI read while a background job is writing.
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        current = self.schema_version
        pending = [(v, step) for v, step in MIGRATIONS if v > current]
        if not pending:
            return
        has_data = self.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        if has_data and self.path != ":memory:":
            self.backup(f"pre-v{pending[-1][0]}")
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
                raise

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
        entities = self.conn.execute(
            "SELECT count(*) FROM ref_entities WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
        return {**dict(row), "entities": entities}

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

    def replace_ref_entities(self, source_id: int, entities: list[dict[str, Any]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM ref_entities WHERE source_id = ?", (source_id,))
            self.conn.executemany(
                "INSERT INTO ref_entities (source_id, kind, name, aliases, summary, pages, facts, stat_block)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (source_id, e["kind"], e["name"], json.dumps(e.get("aliases", [])), e.get("summary", ""),
                     json.dumps(e.get("pages", [])), json.dumps(e.get("facts", [])),
                     json.dumps(e["stat_block"]) if e.get("stat_block") else None)
                    for e in entities
                ],
            )

    @staticmethod
    def _ref(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("aliases", "pages", "facts"):
            d[k] = json.loads(d[k])
        d["stat_block"] = json.loads(d["stat_block"]) if d["stat_block"] else None
        return d

    def ref_entities(
        self, campaign: Campaign, source_id: int | None = None, kind: str | None = None,
        q: str | None = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*, s.title AS source FROM ref_entities r JOIN sources s ON s.id = r.source_id"
            f" WHERE {self._visible_sources_clause(campaign)}"
        )
        params: list[Any] = [campaign.id]
        if source_id is not None:
            sql += " AND r.source_id = ?"
            params.append(source_id)
        if kind:
            sql += " AND r.kind = ?"
            params.append(kind)
        if q and (fq := fts_query(q)):
            sql += " AND r.id IN (SELECT rowid FROM ref_entities_fts WHERE ref_entities_fts MATCH ?)"
            params.append(fq)
        sql += " ORDER BY r.kind, r.name LIMIT ?"
        params.append(limit)
        return [self._ref(r) for r in self.conn.execute(sql, params)]

    def lookup_ref(self, campaign: Campaign, name: str, limit: int = 5) -> list[dict[str, Any]]:
        """Reference entities called `name`, by canonical name or alias first, then by search."""
        needle = name.strip().lower()
        exact = [
            e for e in self.ref_entities(campaign, q=name, limit=200)
            if e["name"].lower() == needle or needle in (a.lower() for a in e["aliases"])
        ]
        if exact:
            return exact[:limit]
        q = fts_query(name)
        if not q:
            return []
        rows = self.conn.execute(
            "SELECT r.*, s.title AS source FROM ref_entities_fts f"
            " JOIN ref_entities r ON r.id = f.rowid JOIN sources s ON s.id = r.source_id"
            f" WHERE ref_entities_fts MATCH ? AND {self._visible_sources_clause(campaign)}"
            " ORDER BY bm25(ref_entities_fts, 5.0, 3.0, 1.0) LIMIT ?",
            (q, campaign.id, limit),
        )
        return [self._ref(r) for r in rows]

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
