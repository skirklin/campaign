"""Digest tree, Merkle cache invariants, entity merging, persistence."""

import json
import re
from types import SimpleNamespace as NS

import pymupdf
import pytest

from campaign import digest, library
from campaign.db import BASELINE, SCHEMA_VERSION, Store
from campaign.digest import BranchOutput, Entity, LeafOutput, Merge
from campaign.ingest import parse_pdf
from campaign.tools import ToolContext, build_tools
from campaign.world import World

from test_agent import FakeStream

BOOK = """\
# Book
The mists close in. {intro}
## Chapter 1
### Village
ENTITY npc Ismark | Ismark the Lesser
The village is grim.
### Church
ENTITY npc Donavich
{church}
## Chapter 2
### Windmill
ENTITY npc Morgantha | the windmill hag
ENTITY npc Ismark
### Old Bonegrinder
ENTITY npc The Windmill Hag
"""


class FakeDigest:
    """Stands in for Claude. Leaves: entities come from ENTITY lines.
    Branches: merges entities whose alias is another entity's name."""

    def __init__(self, fail_on: str | None = None, text_sensitive: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self.text_sensitive = text_sensitive
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **kw):
        user = kw["messages"][0]["content"]
        title = re.sub(r" \(pp?\..*\)$", "", user.split("\n", 1)[0].removeprefix("Section: "))
        kind = "leaf" if kw["output_format"] is LeafOutput else "branch"
        self.calls.append((kind, title))
        if self.fail_on and self.fail_on in title:
            raise RuntimeError("boom")
        if kind == "leaf":
            ents = []
            for m in re.finditer(r"^ENTITY (\w+) ([^|\n]+?)(?: \| (.+))?$", user, re.M):
                ents.append(Entity(name=m[2], kind=m[1], aliases=[m[3]] if m[3] else [], summary=f"{m[2]} in {title}",
                                   pages=[], facts=[f"seen in {title}"], stat_block=None))
            summary = f"Leaf summary of {title}"
            if self.text_sensitive:
                summary += f" ({len(user)} chars)"
            parsed = LeafOutput(summary=summary, entities=ents)
        else:
            rows = re.findall(r"^(e\d+) \| \w+ \| ([^|]+?) \| ([^|]*?) \|", user, re.M)
            merges = []
            for eid, name, aliases in rows:
                for other, oname, _ in rows:
                    if other != eid and oname.lower() in [a.strip().lower() for a in aliases.split(";") if a.strip()]:
                        merges.append(Merge(ids=[eid, other], name=name, summary=f"{name}, merged"))
            summary = f"Branch summary of {title}"
            if self.text_sensitive:
                summary += f" ({len(user)} chars)"
            parsed = BranchOutput(summary=summary, merges=merges)
        return FakeStream(NS(stop_reason="end_turn", parsed_output=parsed, usage=NS(input_tokens=100, output_tokens=10)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "MIN_LEAF_CHARS", 1)  # no packing; packing has its own test
    store = Store(tmp_path / "c.db")
    camp = store.create_campaign("Strahd", "dm")
    yield store, camp, tmp_path / "c.db"
    store.close()


def upload(store, camp, intro="", church="Donavich prays."):
    text = BOOK.format(intro=intro, church=church).encode()
    return library.add_reference(store, camp, text, "book.md", title="Book", kind="module")


def test_full_digest_then_everything_cached(env):
    store, camp, db = env
    r = upload(store, camp)
    assert r["pending"] == 8  # 5 leaves + Chapter 1, Chapter 2, Book
    fake = FakeDigest()
    stats = digest.run(db, r["id"], client=fake)
    assert stats["digested"] == 8 and stats["failed"] == 0
    assert sorted(t for k, t in fake.calls if k == "branch") == ["Book", "Chapter 1", "Chapter 2"]

    # Leaves before their branches.
    order = [t for _, t in fake.calls]
    assert order.index("Church") < order.index("Chapter 1") < order.index("Book")

    ents = {e["name"]: e for e in store.source_catalog(r["id"])}
    assert set(ents) == {"Ismark", "Donavich", "Morgantha"}
    assert ents["Ismark"]["facts"] == ["seen in Village", "seen in Windmill"]  # exact-name merge
    assert "The Windmill Hag" in ents["Morgantha"]["aliases"]  # Claude-proposed merge

    # ...and the catalog is now the world before play.
    hag = World(store, camp).lookup("the windmill hag")[0]
    assert hag["name"] == "Morgantha"
    assert {f["statement"] for f in hag["facts"]} >= {"Morgantha, merged", "seen in Windmill", "seen in Old Bonegrinder"}
    assert all(f["sessions"] == "0-" and f["source"] == "Book" for f in hag["facts"])

    again = FakeDigest()
    assert digest.run(db, r["id"], client=again)["cached"] == 8 and again.calls == []
    assert upload(store, camp)["pending"] == 0  # identical re-upload


def test_same_book_in_another_campaign_is_free(env):
    store, camp, db = env
    digest.run(db, upload(store, camp)["id"], client=FakeDigest())
    other = store.create_campaign("Second table", "dm")
    r = upload(store, other)
    assert r["pending"] == 0
    assert len(World(store, other).list()) == 3


def test_editing_one_section_redigests_only_its_path_to_the_root(env):
    store, camp, db = env
    r = upload(store, camp)
    digest.run(db, r["id"], client=FakeDigest())
    r2 = upload(store, camp, church="Donavich prays. His son Doru is locked below.")
    assert r2["id"] == r["id"]  # same source, same id
    assert r2["pending"] == 3  # the leaf, plus the two branches whose keys depend on it
    fake = FakeDigest(text_sensitive=True)
    digest.run(db, r["id"], client=fake)
    assert fake.calls == [("leaf", "Church"), ("branch", "Chapter 1"), ("branch", "Book")]


def test_unchanged_digest_stops_propagation(env):
    """Parents hash their children's outputs, so a text edit that doesn't change
    the leaf's digest (a typo fix, say) stops there."""
    store, camp, db = env
    r = upload(store, camp)
    digest.run(db, r["id"], client=FakeDigest())
    upload(store, camp, church="Donavich prays!")
    fake = FakeDigest()  # same digest for any text
    digest.run(db, r["id"], client=fake)
    assert fake.calls == [("leaf", "Church")]


def test_branch_version_bump_skips_leaves(env, monkeypatch):
    store, camp, db = env
    r = upload(store, camp)
    digest.run(db, r["id"], client=FakeDigest())
    monkeypatch.setattr(digest, "BRANCH_VERSION", digest.BRANCH_VERSION + 1)
    fake = FakeDigest()
    digest.run(db, r["id"], client=fake)
    assert {k for k, _ in fake.calls} == {"branch"} and len(fake.calls) == 3


def test_failure_is_isolated_and_resumable(env):
    store, camp, db = env
    r = upload(store, camp)
    stats = digest.run(db, r["id"], client=FakeDigest(fail_on="Windmill"))
    assert stats["failed"] == 1 and stats["digested"] == 5  # 4 other leaves + Chapter 1
    outline = {n["title"]: n for n in store.digest_outline(r["id"])}
    assert outline["Windmill"]["error"] == "boom"
    assert outline["Chapter 2"]["error"] == "waiting on a failed subsection"
    assert outline["Chapter 1"]["summary"] == "Branch summary of Chapter 1"
    assert World(store, camp).list() == []  # nothing applied until the root is done

    fake = FakeDigest()
    digest.run(db, r["id"], client=fake)
    assert fake.calls == [("leaf", "Windmill"), ("branch", "Chapter 2"), ("branch", "Book")]
    assert len(World(store, camp).list()) == 3


def test_packing_small_siblings(tmp_path):
    rooms = "\n".join(f"### Room {i}\n" + "x" * 2500 for i in range(1, 6))
    store = Store(tmp_path / "p.db")
    camp = store.create_campaign("P", "dm")
    r = library.add_reference(store, camp, f"# Dungeon\n{rooms}".encode(), "d.md")
    leaves = [n["title"] for n in store.digest_outline(r["id"]) if n["is_leaf"]]
    assert leaves == ["Room 1; Room 2; Room 3", "Room 4; Room 5"]


def test_long_section_is_split(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "MAX_LEAF_CHARS", 3000)
    store = Store(tmp_path / "s.db")
    camp = store.create_campaign("S", "dm")
    body = "\n\n".join("para " * 100 for _ in range(20))  # ~10k chars
    r = library.add_reference(store, camp, f"# Long\n{body}".encode(), "l.md")
    titles = [n["title"] for n in store.digest_outline(r["id"]) if n["is_leaf"]]
    assert len(titles) >= 3 and all("(part" in t for t in titles)


def test_tools_use_digest(env):
    store, camp, db = env
    r = upload(store, camp)
    digest.run(db, r["id"], client=FakeDigest())
    tools = {f.__name__: f for f in build_tools(ToolContext(store, camp))}
    hit = json.loads(tools["lookup"](name="the windmill hag"))
    assert hit[0]["name"] == "Morgantha"
    top = json.loads(tools["outline"]())
    assert [s["title"] for s in top[0]["sections"]] == ["Book (introduction)", "Chapter 1", "Chapter 2"]
    ch2 = json.loads(tools["outline"](section_id=top[0]["sections"][2]["id"]))
    assert [s["title"] for s in ch2["subsections"]] == ["Windmill", "Old Bonegrinder"]

    store.upsert_session(camp, 4)
    assert "Recorded" in tools["record_fact"](entity="the windmill hag", statement="Morgantha was slain", attribute="status", value="dead")
    now = json.loads(tools["lookup"](name="Morgantha"))[0]
    assert now["current"] == {"status": "dead"}
    assert any(f["source"] == "Book" for f in now["facts"])  # the book's facts still stand alongside
    before = json.loads(tools["lookup"](name="Morgantha", as_of_session=3))[0]
    assert before["current"] == {}

    from campaign.agent import overview, catalog
    assert "Branch summary of Chapter 2" in overview(store, camp)
    assert "Morgantha | The Windmill Hag" in catalog(store, camp)


def test_player_campaign_cannot_see_dm_only_digest(env):
    store, _, db = env
    player = store.create_campaign("Players", "player")
    r = upload(store, player)  # modules default to DM-only
    digest.run(db, r["id"], client=FakeDigest())
    tools = {f.__name__: f for f in build_tools(ToolContext(store, player))}
    assert "No match" in tools["lookup"](name="Morgantha")
    assert World(store, player).list() == []  # DM-only book facts are invisible to players
    assert "Nothing has been digested" in tools["outline"]()
    assert tools["outline"](section_id=store.digest_outline(r["id"])[0]["id"]).startswith("No section")


def test_reindex_from_original_keeps_id_and_cache(env, monkeypatch):
    store, camp, db = env
    r = upload(store, camp)
    digest.run(db, r["id"], client=FakeDigest())
    monkeypatch.setattr(library, "CHUNKER_VERSION", 99)
    done = library.reindex(store)
    assert done == [{"id": r["id"], "title": "Book", "chunks": done[0]["chunks"], "pending": 0}]
    assert library.reindex(store) == []  # now up to date
    src = store.source(r["id"])
    assert store.blobs.get(src["blob"]) == BOOK.format(intro="", church="Donavich prays.").encode()


def test_pre_baseline_database_is_retired_and_restored(env, monkeypatch):
    store, camp, db = env
    r = upload(store, camp)
    digest.run(db, r["id"], client=FakeDigest())
    store.create_character({"name": "Brannoc"}, campaign_id=camp.id, reason="made")
    store.upsert_session(camp, 1)
    run = World(store, camp).start_run("ingest", session=1)
    World(store, camp).add_event("Something happened", 1, run)
    store.conn.execute(f"PRAGMA user_version = {BASELINE - 1}")  # pretend it's an old database
    store.close()

    fresh = Store(db)
    assert fresh.schema_version == SCHEMA_VERSION
    assert list((db.parent / "backups").glob("campaign-*-retired.db"))
    assert fresh.find_character("Brannoc")["sheet"] == {"name": "Brannoc"}
    camp2 = fresh.campaign("strahd")
    src = fresh.sources(camp2)[0]
    assert src["title"] == "Book" and src["has_original"]
    assert fresh.digest_status(src["id"])["done"] == 8  # digest cache carried over: nothing to redo
    world = World(fresh, camp2)
    assert world.lookup("Morgantha") and world.events() == [] and fresh.sessions(camp2) == []
    fresh.close()
    assert Store(db).schema_version == SCHEMA_VERSION  # reopening is a no-op


def test_pdf_outline_tree(tmp_path):
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Chapter 1: Village", fontsize=20)
    p1.insert_text((72, 110), "The village is grim.", fontsize=10)
    p1.insert_text((72, 300), "Church", fontsize=16)  # subsection starts mid-page
    p1.insert_text((72, 330), "Donavich prays.", fontsize=10)
    p2 = doc.new_page()
    p2.insert_text((72, 72), "Chapter 2: Windmill", fontsize=20)
    p2.insert_text((72, 110), "The hags bake pies.", fontsize=10)
    doc.set_toc([[1, "Chapter 1: Village", 1], [2, "Church", 1], [1, "Chapter 2: Windmill", 2]])
    path = tmp_path / "m.pdf"
    doc.save(path)

    parsed = parse_pdf(path)
    assert [(n["title"], n["level"], n["parent"], n["page_start"], n["page_end"]) for n in parsed.nodes] == [
        ("Chapter 1: Village", 1, None, 1, 1),
        ("Church", 2, 0, 1, 1),
        ("Chapter 2: Windmill", 1, None, 2, 2),
    ]
    by_node = {}
    for c in parsed.chunks:
        by_node.setdefault(c["node"], []).append(c["text"])
    assert by_node[0] == ["The village is grim."]
    assert by_node[1] == ["Donavich prays."]
    assert by_node[2] == ["The hags bake pies."]


def test_fallback_only_for_models_that_support_it():
    seen = []

    def stream(**kw):
        seen.append(kw)
        return FakeStream(NS(stop_reason="end_turn", parsed_output=LeafOutput(summary="s", entities=[]),
                             usage=NS(input_tokens=1, output_tokens=1)))

    client = NS(beta=NS(messages=NS(stream=stream)))
    digest._call(client, "sys", "user", LeafOutput, "claude-opus-5-5", "medium")
    digest._call(client, "sys", "user", LeafOutput, "claude-haiku-5-5", "medium")
    assert seen[0]["fallbacks"] == "default" and seen[0]["betas"] == [digest.FALLBACK_BETA]
    assert "fallbacks" not in seen[1] and "betas" not in seen[1]
