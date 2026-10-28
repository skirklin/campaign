"""The world model: entities, facts over time, events, and the runs that wrote them.

**Identity.** An entity is one person, place or thing in the campaign, with
a canonical name and aliases. Book material, session transcripts and chat
all attach to the same entity, so "Morgantha", "the hag" and "the baker"
share one record.

**Facts over time.** A fact is true from one session to another, according
to a source. Session numbers are the clock; session 0 is the world before
play, which is where the book's facts start. A fact with an `attribute`
fills a single-valued slot (status, location, holder...): recording a new
value ends the old one at that session, and the old one stays as history.
A fact without an attribute is a statement that holds until it's ended.
Nothing is overwritten, so the past is always queryable: "as of session 4,
where was Ireena?"

**Sources and knowledge.** Each fact records where it came from (a book
page, a session, chat) and since when the party has known it, so spoiler
filtering is per fact: the party can know Morgantha runs a bakery long
before they learn she's a hag.

**Runs.** Every write belongs to a run: applying a book's digest, ingesting
a session, a chat, a manual edit. Deleting a run undoes exactly what it
did, including reopening facts it ended (see the schema's triggers).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from .db import Campaign, Store, now

# Common single-valued attributes. Any attribute works; these are suggested.
ATTRIBUTES = ("status", "location", "holder", "disposition", "leader", "allegiance", "condition")
SUMMARY_VERSION = 1
SUMMARY_MODEL = os.environ.get("CAMPAIGN_MODEL", "claude-opus-5-5")

FACT_COLUMNS = (
    "f.id, f.entity_id, f.attribute, f.value, f.statement, f.modality, f.valid_from, f.valid_to,"
    " f.party_knows_from, f.pages, f.source_id, s.title AS source, r.kind AS origin, r.session AS run_session"
)


class World:
    def __init__(self, store: Store, campaign: Campaign):
        self.store = store
        self.campaign = campaign
        self.conn = store.conn

    @property
    def now(self) -> int:
        return self.store.current_session(self.campaign)

    # -- runs -----------------------------------------------------------------

    def start_run(self, kind: str, session: int | None = None, source_id: int | None = None, label: str = "") -> int:
        with self.conn:
            return self.conn.execute(
                "INSERT INTO runs (campaign_id, kind, session, source_id, label, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.campaign.id, kind, session, source_id, label, now()),
            ).lastrowid

    def revert_run(self, run_id: int) -> dict[str, int]:
        counts = {
            "facts": self.conn.execute("SELECT count(*) FROM facts WHERE run_id = ?", (run_id,)).fetchone()[0],
            "reopened": self.conn.execute("SELECT count(*) FROM facts WHERE closed_by_run = ?", (run_id,)).fetchone()[0],
            "events": self.conn.execute("SELECT count(*) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0],
        }
        with self.conn:
            self.conn.execute("DELETE FROM runs WHERE id = ? AND campaign_id = ?", (run_id, self.campaign.id))
        counts["entities_removed"] = self._remove_orphans()
        return counts

    def revert_session(self, number: int) -> dict[str, int]:
        """Undo every ingest of session `number`, and clear its recap."""
        runs = self.conn.execute(
            "SELECT id FROM runs WHERE campaign_id = ? AND kind = 'ingest' AND session = ? ORDER BY id DESC",
            (self.campaign.id, number),
        ).fetchall()
        if not runs and not self.conn.execute(
            "SELECT 1 FROM sessions WHERE campaign_id = ? AND number = ?", (self.campaign.id, number)
        ).fetchone():
            raise KeyError(f"no session {number}")
        total = {"facts": 0, "reopened": 0, "events": 0, "entities_removed": 0}
        for r in runs:
            for k, v in self.revert_run(r["id"]).items():
                total[k] += v
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET title = '', summary = '' WHERE campaign_id = ? AND number = ?",
                (self.campaign.id, number),
            )
        return total

    def _remove_orphans(self) -> int:
        with self.conn:
            return self.conn.execute(
                "DELETE FROM entities WHERE campaign_id = ?"
                " AND id NOT IN (SELECT entity_id FROM facts)"
                " AND id NOT IN (SELECT entity_id FROM aliases)"
                " AND id NOT IN (SELECT entity_id FROM event_entities)",
                (self.campaign.id,),
            ).rowcount

    # -- identity -------------------------------------------------------------

    def entity(self, entity_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, kind, name FROM entities WHERE id = ? AND campaign_id = ?", (entity_id, self.campaign.id)
        ).fetchone()
        if row is None:
            return None
        e = dict(row)
        e["aliases"] = [r["alias"] for r in self.conn.execute(
            "SELECT alias FROM aliases WHERE entity_id = ? ORDER BY id", (entity_id,))]
        return e

    def resolve(self, name: str, kind: str | None = None) -> dict[str, Any] | None:
        """The entity called `name`, by canonical name or alias; same kind preferred."""
        rows = self.conn.execute(
            "SELECT e.id, e.kind FROM entities e WHERE e.campaign_id = ? AND (e.name = ? COLLATE NOCASE"
            " OR e.id IN (SELECT entity_id FROM aliases WHERE alias = ?)) ORDER BY e.id",
            (self.campaign.id, name.strip(), name.strip()),
        ).fetchall()
        if not rows:
            return None
        best = next((r for r in rows if r["kind"] == kind), rows[0])
        return self.entity(best["id"])

    def ensure_entity(self, name: str, kind: str) -> dict[str, Any]:
        found = self.resolve(name, kind)
        if found:
            return found
        with self.conn:
            entity_id = self.conn.execute(
                "INSERT INTO entities (campaign_id, kind, name, created_at) VALUES (?, ?, ?, ?)",
                (self.campaign.id, kind, name.strip(), now()),
            ).lastrowid
        return self.entity(entity_id)

    def add_alias(self, entity_id: int, alias: str, run_id: int) -> None:
        alias = alias.strip()
        e = self.entity(entity_id)
        if not alias or not e or alias.lower() == e["name"].lower():
            return
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO aliases (entity_id, alias, run_id) VALUES (?, ?, ?)", (entity_id, alias, run_id)
            )

    def merge(self, keep_id: int, other_id: int) -> dict[str, Any]:
        """Fold `other` into `keep`: its facts, aliases and events move over and its name becomes an alias."""
        keep, other = self.entity(keep_id), self.entity(other_id)
        if not keep or not other or keep_id == other_id:
            raise KeyError("both entities must exist and differ")
        run = self.start_run("manual", label=f"merged {other['name']} into {keep['name']}")
        with self.conn:
            self.conn.execute("UPDATE facts SET entity_id = ? WHERE entity_id = ?", (keep_id, other_id))
            self.conn.execute(
                "UPDATE OR IGNORE aliases SET entity_id = ? WHERE entity_id = ?", (keep_id, other_id)
            )
            self.conn.execute(
                "UPDATE OR IGNORE event_entities SET entity_id = ? WHERE entity_id = ?", (keep_id, other_id)
            )
            self.conn.execute("DELETE FROM entities WHERE id = ?", (other_id,))
            # Both may have had a current value for the same attribute: the newer one wins.
            open_facts = self.conn.execute(
                "SELECT id, attribute, valid_from FROM facts WHERE entity_id = ? AND attribute IS NOT NULL"
                " AND valid_to IS NULL AND modality = 'fact' ORDER BY attribute, valid_from DESC, id DESC",
                (keep_id,),
            ).fetchall()
            newest: dict[str, int] = {}
            for f in open_facts:
                if f["attribute"] in newest:
                    self.conn.execute(
                        "UPDATE facts SET valid_to = ?, closed_by_run = ? WHERE id = ?",
                        (max(newest[f["attribute"]], f["valid_from"]), run, f["id"]),
                    )
                else:
                    newest[f["attribute"]] = f["valid_from"]
        self.add_alias(keep_id, other["name"], run)
        return self.entity(keep_id)

    # -- facts ----------------------------------------------------------------

    def record(
        self,
        entity_id: int,
        statement: str,
        run_id: int,
        session: int,
        attribute: str | None = None,
        value: str | None = None,
        modality: str = "fact",
        party_knows: bool = True,
        source_id: int | None = None,
        pages: list[int] | tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """Add a fact true from `session` on. A new attribute value ends the current one."""
        attribute = attribute.strip().lower() if attribute else None
        closed = 0
        with self.conn:
            if attribute and modality == "fact":
                closed = self.conn.execute(
                    "UPDATE facts SET valid_to = ?, closed_by_run = ?"
                    " WHERE entity_id = ? AND attribute = ? AND valid_to IS NULL AND modality = 'fact'",
                    (session, run_id, entity_id, attribute),
                ).rowcount
            fact_id = self.conn.execute(
                "INSERT INTO facts (campaign_id, entity_id, attribute, value, statement, modality, valid_from,"
                " party_knows_from, source_id, pages, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.campaign.id, entity_id, attribute, value, statement.strip(), modality, session,
                 session if party_knows else None, source_id, json.dumps(sorted(set(pages))), run_id, now()),
            ).lastrowid
        return {"id": fact_id, "ended": closed}

    def end_fact(self, fact_id: int, run_id: int, session: int) -> bool:
        with self.conn:
            return self.conn.execute(
                "UPDATE facts SET valid_to = ?, closed_by_run = ?"
                " WHERE id = ? AND campaign_id = ? AND valid_to IS NULL",
                (session, run_id, fact_id, self.campaign.id),
            ).rowcount > 0

    def party_learned(self, fact_id: int, session: int) -> bool:
        with self.conn:
            return self.conn.execute(
                "UPDATE facts SET party_knows_from = ? WHERE id = ? AND campaign_id = ?"
                " AND (party_knows_from IS NULL OR party_knows_from > ?)",
                (session, fact_id, self.campaign.id, session),
            ).rowcount > 0

    # -- events ---------------------------------------------------------------

    def add_event(self, text: str, session: int, run_id: int, entity_ids: list[int] = ()) -> int:
        with self.conn:
            event_id = self.conn.execute(
                "INSERT INTO events (campaign_id, session, text, run_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.campaign.id, session, text.strip(), run_id, now()),
            ).lastrowid
            self.conn.executemany(
                "INSERT OR IGNORE INTO event_entities (event_id, entity_id) VALUES (?, ?)",
                [(event_id, e) for e in entity_ids],
            )
        return event_id

    def events(self, session: int | None = None, entity_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = (
            "SELECT ev.id, ev.session, ev.text, (SELECT json_group_array(json_object('id', e.id, 'name', e.name))"
            " FROM event_entities x JOIN entities e ON e.id = x.entity_id WHERE x.event_id = ev.id) AS entities"
            " FROM events ev WHERE ev.campaign_id = ?"
        )
        params: list[Any] = [self.campaign.id]
        if session is not None:
            sql += " AND ev.session = ?"
            params.append(session)
        if entity_id is not None:
            sql += " AND ev.id IN (SELECT event_id FROM event_entities WHERE entity_id = ?)"
            params.append(entity_id)
        sql += " ORDER BY ev.session DESC, ev.id DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        for r in rows:
            r["entities"] = json.loads(r["entities"])
        return list(reversed(rows))

    # -- reading --------------------------------------------------------------

    def _visible(self, as_of: int | None) -> tuple[str, list[Any]]:
        """Spoiler filter: in a player campaign, only facts the party knew by `as_of`."""
        if not self.campaign.spoiler_safe:
            return "", []
        return " AND f.party_knows_from IS NOT NULL AND f.party_knows_from <= ?", [
            self.now if as_of is None else as_of
        ]

    def facts(self, entity_id: int, as_of: int | None = None) -> list[dict[str, Any]]:
        """Every visible fact about an entity, oldest first. With `as_of`, nothing later."""
        clause, params = self._visible(as_of)
        sql = (
            f"SELECT {FACT_COLUMNS} FROM facts f JOIN runs r ON r.id = f.run_id"
            " LEFT JOIN sources s ON s.id = f.source_id WHERE f.entity_id = ?" + clause
        )
        if as_of is not None:
            sql += " AND f.valid_from <= ?"
            params.append(as_of)
        sql += " ORDER BY f.valid_from, f.id"
        rows = [dict(r) for r in self.conn.execute(sql, [entity_id, *params])]
        for r in rows:
            r["pages"] = json.loads(r["pages"])
            if as_of is not None and r["valid_to"] is not None and r["valid_to"] > as_of:
                r["valid_to"] = None  # hadn't ended yet at that point
        return rows

    def view(self, entity_id: int, as_of: int | None = None) -> dict[str, Any] | None:
        """An entity as of a session (default: now): what's true, what was, and what might be."""
        e = self.entity(entity_id)
        if e is None:
            return None
        facts = self.facts(entity_id, as_of)
        events = [ev for ev in self.events(entity_id=entity_id) if as_of is None or ev["session"] <= as_of]
        if self.campaign.spoiler_safe and not facts and not events:
            return None
        cur = [f for f in facts if f["valid_to"] is None and f["modality"] == "fact"]
        e.update(
            as_of=self.now if as_of is None else as_of,
            current={f["attribute"]: f["value"] for f in cur if f["attribute"] and f["attribute"] != "stat_block"},
            facts=[_brief(f) for f in cur],
            past=[_brief(f) for f in facts if f["valid_to"] is not None],
            possible=[_brief(f) for f in facts if f["modality"] != "fact" and f["valid_to"] is None],
            events=[{"session": ev["session"], "text": ev["text"]} for ev in events][-20:],
            summary=self.cached_summary(entity_id) if as_of is None else None,
        )
        return e

    def list(self, kind: str | None = None, q: str | None = None, in_play: bool = False) -> list[dict[str, Any]]:
        """Entities with their current attributes and fact counts.

        `in_play` keeps only those the table has touched (any fact or event
        that didn't come from the book).
        """
        clause, params = self._visible(None)
        sql = (
            "SELECT e.id, e.kind, e.name,"
            " (SELECT group_concat(alias, '; ') FROM aliases WHERE entity_id = e.id) AS aliases,"
            f" (SELECT count(*) FROM facts f WHERE f.entity_id = e.id AND f.valid_to IS NULL{clause}) AS current_facts,"
            f" (SELECT count(*) FROM facts f WHERE f.entity_id = e.id AND f.valid_to IS NOT NULL{clause}) AS past_facts,"
            " (SELECT count(*) FROM facts f JOIN runs r ON r.id = f.run_id WHERE f.entity_id = e.id AND r.kind != 'book')"
            " + (SELECT count(*) FROM event_entities WHERE entity_id = e.id) AS table_activity,"
            f" (SELECT json_group_object(f.attribute, f.value) FROM facts f WHERE f.entity_id = e.id"
            f"  AND f.valid_to IS NULL AND f.attribute IS NOT NULL AND f.attribute != 'stat_block'"
            f"  AND f.modality = 'fact'{clause}) AS attrs,"
            f" (SELECT f.statement FROM facts f WHERE f.entity_id = e.id AND f.valid_to IS NULL"
            f"  AND f.attribute IS NULL AND f.modality = 'fact'{clause} ORDER BY f.valid_from DESC, f.id LIMIT 1) AS headline"
            " FROM entities e WHERE e.campaign_id = ?"
        )
        args = params * 4 + [self.campaign.id]  # one per {clause} above
        if kind:
            sql += " AND e.kind = ?"
            args.append(kind)
        if q:
            sql += (" AND (e.name LIKE ? OR e.id IN (SELECT entity_id FROM aliases WHERE alias LIKE ?)"
                    " OR e.id IN (SELECT entity_id FROM facts WHERE statement LIKE ?))")
            args += [f"%{q}%"] * 3
        sql += " ORDER BY e.kind, e.name"
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            if self.campaign.spoiler_safe and not (d["current_facts"] or d["past_facts"] or d["table_activity"]):
                continue
            if in_play and not d["table_activity"]:
                continue
            d["aliases"] = d["aliases"].split("; ") if d["aliases"] else []
            d["current"] = json.loads(d.pop("attrs") or "{}")
            out.append(d)
        return out

    def lookup(self, name: str, as_of: int | None = None, limit: int = 3) -> list[dict[str, Any]]:
        """Find entities by name or alias, then by partial name, then by what's said about them."""
        hit = self.resolve(name)
        ids = [hit["id"]] if hit else []
        if not ids:
            ids = [r["id"] for r in self.list(q=name)][:limit]
        if not ids:
            from .db import fts_query

            if q := fts_query(name):
                ids = list(dict.fromkeys(r["entity_id"] for r in self.conn.execute(
                    "SELECT f.entity_id FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid"
                    " WHERE facts_fts MATCH ? AND f.campaign_id = ? ORDER BY bm25(facts_fts) LIMIT 30",
                    (q, self.campaign.id),
                )))[:limit]
        return [v for v in (self.view(i, as_of) for i in ids) if v]

    def timeline(self, entity_id: int) -> list[dict[str, Any]]:
        """What changed for an entity, session by session: facts starting and ending, and events."""
        items: list[dict[str, Any]] = []
        for f in self.facts(entity_id):
            items.append({"session": f["valid_from"], "change": "began", **_brief(f)})
            if f["valid_to"] is not None:
                items.append({"session": f["valid_to"], "change": "ended", **_brief(f)})
        for ev in self.events(entity_id=entity_id):
            items.append({"session": ev["session"], "change": "event", "statement": ev["text"]})
        order = {"ended": 0, "began": 1, "event": 2}
        return sorted(items, key=lambda i: (i["session"], order[i["change"]]))

    # -- the book's starting state ---------------------------------------------

    def apply_book(self, source_id: int, entities: list[dict[str, Any]], key: str | None = None) -> int | None:
        """Make a source's digest catalog the state of the world before play (session 0).

        Idempotent: does nothing if this digest (`key`) is already applied.
        When the catalog changes, the old book facts are replaced, but what
        the table did to them carries over: a book fact the table ended, or
        that the party learned, keeps that history if the new catalog still
        says the same thing. Entities are matched by name and alias, so table
        facts stay attached too.
        """
        old = self.conn.execute(
            "SELECT id, input_key FROM runs WHERE kind = 'book' AND source_id = ?", (source_id,)
        ).fetchall()
        if key is not None and len(old) == 1 and old[0]["input_key"] == key:
            return None
        carried = {
            (r["entity_id"], r["statement"]): r
            for r in self.conn.execute(
                "SELECT f.entity_id, f.statement, f.valid_to, f.closed_by_run, f.party_knows_from FROM facts f"
                " JOIN runs r ON r.id = f.run_id WHERE r.kind = 'book' AND r.source_id = ?"
                " AND (f.valid_to IS NOT NULL OR f.party_knows_from IS NOT NULL)",
                (source_id,),
            )
        }
        src = self.store.source(source_id)
        with self.conn:
            self.conn.execute("DELETE FROM runs WHERE kind = 'book' AND source_id = ?", (source_id,))
            run = self.conn.execute(
                "INSERT INTO runs (campaign_id, kind, session, source_id, input_key, label, created_at)"
                " VALUES (?, 'book', 0, ?, ?, ?, ?)",
                (self.campaign.id, source_id, key, src["title"], now()),
            ).lastrowid
        known = not src["dm_only"]
        for item in entities:
            e = None
            for name in (item["name"], *item.get("aliases", [])):
                if e := self.resolve(name, item["kind"]):
                    break
            e = e or self.ensure_entity(item["name"], item["kind"])
            for alias in (item["name"], *item.get("aliases", [])):
                self.add_alias(e["id"], alias, run)
            pages = item.get("pages", [])
            statements = ([item["summary"]] if item.get("summary") else []) + list(item.get("facts", []))
            for statement in statements:
                fact = self.record(e["id"], statement, run, 0, party_knows=known, source_id=source_id, pages=pages)
                if prev := carried.get((e["id"], statement.strip())):
                    with self.conn:
                        self.conn.execute(
                            "UPDATE facts SET valid_to = ?, closed_by_run = ?,"
                            " party_knows_from = coalesce(party_knows_from, ?) WHERE id = ?",
                            (prev["valid_to"], prev["closed_by_run"], prev["party_knows_from"], fact["id"]),
                        )
            if sb := item.get("stat_block"):
                self.record(
                    e["id"], f"Stat block: AC {sb['armor_class']}, HP {sb['hit_points']}"
                    + (f", CR {sb['challenge']}" if sb.get("challenge") else ""),
                    run, 0, attribute="stat_block", value=json.dumps(sb), party_knows=known,
                    source_id=source_id, pages=pages,
                )
        self._remove_orphans()
        return run

    # -- synthesized summaries --------------------------------------------------

    def summary_key(self, entity_id: int) -> str:
        e = self.entity(entity_id)
        facts = [
            (f["statement"], f["modality"], f["valid_from"], f["valid_to"], _origin(f))
            for f in self.facts(entity_id)
        ]
        events = [(ev["session"], ev["text"]) for ev in self.events(entity_id=entity_id)]
        blob = json.dumps([SUMMARY_VERSION, e["name"], facts, events], ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def cached_summary(self, entity_id: int) -> str | None:
        row = self.conn.execute("SELECT text FROM summaries WHERE key = ?", (self.summary_key(entity_id),)).fetchone()
        return row["text"] if row else None

    def synthesize(self, entity_id: int, client: Any = None) -> str:
        """Write (or reuse) a summary from everything known about an entity, past and present."""
        key = self.summary_key(entity_id)
        if cached := self.cached_summary(entity_id):
            return cached
        import anthropic

        v = self.view(entity_id)
        prompt = json.dumps({k: v[k] for k in ("name", "kind", "aliases", "current", "facts", "past", "possible", "events")},
                            ensure_ascii=False, indent=1)
        client = client or anthropic.Anthropic()
        with client.beta.messages.stream(
            model=SUMMARY_MODEL,
            max_tokens=4000,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise RuntimeError("Claude declined to summarize this entity")
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO summaries (key, text, created_at) VALUES (?, ?, ?)", (key, text, now())
            )
        return text


SUMMARY_SYSTEM = """\
You write the summary line for one person, place or thing in a tabletop RPG
campaign record, for the game master. You get every fact known about it,
each with the sessions it was true for (session 0 is before play began) and
its source, plus the events it was part of.

Write 2-4 sentences: its situation now first, then the history that matters.
Where the table has diverged from the book (a book fact that has since been
superseded), say so briefly. Use only the facts given. Plain text, no
headings or lists.
"""


def _origin(f: dict[str, Any]) -> str:
    if f["origin"] == "book" or f["source"]:
        return f["source"] or "book"
    return {"ingest": f"session {f['run_session']}", "chat": "chat", "manual": "edited by hand"}.get(f["origin"], f["origin"])


def _brief(f: dict[str, Any]) -> dict[str, Any]:
    out = {
        "fact_id": f["id"],
        "statement": f["statement"],
        "sessions": f"{f['valid_from']}-{'' if f['valid_to'] is None else f['valid_to']}",
        "source": _origin(f) + (f" p. {', '.join(map(str, f['pages']))}" if f["pages"] else ""),
    }
    if f["attribute"]:
        out["attribute"] = f["attribute"]
    if f["modality"] != "fact":
        out["modality"] = f["modality"]
    if f["party_knows_from"] is None:
        out["party_knows"] = False
    return out
