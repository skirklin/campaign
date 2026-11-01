"""Digest tree, Merkle cache invariants, entity merging, persistence."""

import json
import re
import time
from types import SimpleNamespace as NS

import pymupdf
import pytest

from campaign import digest, library
from campaign.db import BASELINE, SCHEMA_VERSION, Store
from campaign.digest import BranchOutput, Entity, LeafOutput, Merge, RollTable, TableRow
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
                key = re.search(rf"^KEY {re.escape(m[2])} (\S+)$", user, re.M)
                ents.append(Entity(
                    name=m[2], kind=m[1], aliases=[m[3]] if m[3] else [], summary=f"{m[2]} in {title}",
                    pages=[], facts=[f"seen in {title}"], stat_block=None, area_key=key[1] if key else None,
                    read_aloud=None, treasure=re.findall(rf"^TREASURE {re.escape(m[2])}: (.+)$", user, re.M),
                ))
            tables = [
                RollTable(name=t[1], dice=t[2], pages=[], rows=[
                    TableRow(roll=r.split("=")[0], result=r.split("=")[1]) for r in t[3].split(";")
                ])
                for t in re.finditer(r"^TABLE (.+?) \((\w+)\): (.+)$", user, re.M)
            ]
            summary = f"Leaf summary of {title}"
            if self.text_sensitive:
                summary += f" ({len(user)} chars)"
            parsed = LeafOutput(summary=summary, entities=ents, tables=tables)
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


def test_failure_is_isolated_and_resumable(env, monkeypatch):
    store, camp, db = env
    r = upload(store, camp)
    stats = digest.run(db, r["id"], client=FakeDigest(fail_on="Windmill"))
    assert stats["failed"] == 1 and stats["digested"] == 5  # 4 other leaves + Chapter 1
    outline = {n["title"]: n for n in store.digest_outline(r["id"])}
    assert outline["Windmill"]["error"] == "boom"
    assert outline["Chapter 2"]["error"] == "waiting on a failed subsection"
    assert outline["Chapter 1"]["summary"] == "Branch summary of Chapter 1"
    assert World(store, camp).list() == []  # nothing applied until the root is done

    # Running again right away doesn't pay for the same failure again: it backs off.
    fake = FakeDigest()
    assert digest.run(db, r["id"], client=fake)["waiting"] == 1 and fake.calls == []
    # Once the backoff has passed, the next run retries it and finishes the book.
    later = time.time() + digest.RETRY_SECONDS + 1
    monkeypatch.setattr(digest.time, "time", lambda: later)
    digest.run(db, r["id"], client=fake)
    assert fake.calls == [("leaf", "Windmill"), ("branch", "Chapter 2"), ("branch", "Book")]
    assert len(World(store, camp).list()) == 3


def test_a_section_that_keeps_failing_waits_to_be_asked_again(env, monkeypatch):
    store, camp, db = env
    r = upload(store, camp)
    clock = [time.time()]
    monkeypatch.setattr(digest.time, "time", lambda: clock[0])
    for _ in range(digest.MAX_ATTEMPTS):
        assert digest.run(db, r["id"], client=FakeDigest(fail_on="Windmill"))["failed"] == 1
        clock[0] += 3600
    outline = {n["title"]: n for n in store.digest_outline(r["id"])}
    assert outline["Windmill"]["error"].startswith("boom (gave up after 3 tries)")
    # However long it waits, it doesn't try again on its own...
    clock[0] += 10 * 86400
    fake = FakeDigest()
    assert digest.run(db, r["id"], client=fake)["waiting"] == 1 and fake.calls == []
    # ...until someone asks.
    digest.retry_failed(store, r["id"])
    digest.run(db, r["id"], client=fake)
    assert ("leaf", "Windmill") in fake.calls and store.digest_status(r["id"])["done"] == 8


def test_replanning_keeps_row_ids(env):
    store, camp, db = env
    r = upload(store, camp)
    before = [n["id"] for n in store.digest_outline(r["id"])]
    digest.run(db, r["id"], client=FakeDigest())
    digest.plan(store, r["id"])
    assert [n["id"] for n in store.digest_outline(r["id"])] == before
    # A content change re-plans from scratch.
    upload(store, camp, church="Donavich prays. His son Doru is locked below.")
    assert digest.plan(store, r["id"]) is not None


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
        return FakeStream(NS(stop_reason="end_turn", parsed_output=LeafOutput(summary="s", entities=[], tables=[]),
                             usage=NS(input_tokens=1, output_tokens=1)))

    client = NS(beta=NS(messages=NS(stream=stream)))
    digest._call(client, "sys", "user", LeafOutput, "claude-opus-5-5", "medium")
    digest._call(client, "sys", "user", LeafOutput, "claude-haiku-5-5", "medium")
    assert seen[0]["fallbacks"] == "default" and seen[0]["betas"] == [digest.FALLBACK_BETA]
    assert "fallbacks" not in seen[1] and "betas" not in seen[1]


def test_estimate_counts_pending_calls_and_calibrates(env, monkeypatch):
    store, camp, db = env
    r = upload(store, camp)
    est = r["estimate"]
    assert est["calls"] == 8 and est["calibrated_on"] == 0  # nothing digested with this model yet
    assert est["input_tokens"] > 5 * digest.LEAF_OVERHEAD and est["usd"] == pytest.approx(
        digest.cost(digest.MODEL, est["input_tokens"], est["output_tokens"]))

    digest.run(db, r["id"], client=FakeDigest())
    assert store.digest_usage(r["id"]) == [{"model": digest.MODEL, "input_tokens": 800, "output_tokens": 80}]

    # After an edit only the changed path is estimated. Calibration needs each leaf's text
    # size, known only for leaves still in a plan: the old Church leaf no longer is.
    r2 = upload(store, camp, church="Donavich prays. His son Doru is locked below.")
    assert r2["estimate"]["calls"] == 3 and r2["estimate"]["calibrated_on"] == 0
    monkeypatch.setattr(digest, "MIN_SAMPLES", 4)
    est = digest.estimate(store, digest.plan(store, r["id"]))
    assert est["calls"] == 3 and est["calibrated_on"] == 4
    # Measured: 0.1 output tokens per input token on leaves, instead of the 0.5 default.
    assert est["output_tokens"] < 0.5 * est["input_tokens"]

def test_cost_needs_a_known_model():
    assert digest.cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert digest.cost("some-new-model", 1, 1) is None
    assert digest.spent([{"model": "some-new-model", "input_tokens": 1, "output_tokens": 1}]) is None
    assert digest.spent([]) == 0


def test_reconciler_converges_and_a_new_one_picks_up_where_the_old_one_died(env):
    store, camp, db = env
    r = upload(store, camp)
    first = FakeDigest()
    dying = digest.Reconciler(db, client_factory=lambda: first, workers=1)
    # Nothing is asked for yet, so nothing happens.
    assert dying.reconcile_once() is False and first.calls == []

    store.set_digest_target(r["id"], digest.MODEL, "medium")
    # The first loop dies after two calls (say, the pod was replaced mid-digest).
    original = first.stream

    def stream(**kw):
        if len(first.calls) == 2:
            dying._stop.set()
        return original(**kw)

    first.beta.messages.stream = stream
    dying.reconcile_once()
    done_before = {t for _, t in first.calls}
    assert 0 < len(done_before) < 8 and store.digest_status(r["id"])["done"] < 8

    # It died holding the lease, so a fresh loop waits until that runs out rather than doubling up...
    second = FakeDigest()
    fresh = digest.Reconciler(db, client_factory=lambda: second)
    assert fresh.reconcile_once() is False and second.calls == []
    store.conn.execute("UPDATE leases SET expires_at = 0")
    store.conn.commit()
    # ...then, knowing nothing, finishes the job without redoing anything.
    while fresh.reconcile_once():
        pass
    assert store.digest_status(r["id"])["done"] == 8
    assert not done_before & {t for k, t in second.calls if k == "leaf"}
    assert {e["name"] for e in store.source_catalog(r["id"])} == {"Ismark", "Donavich", "Morgantha"}
    # Converged: further passes do nothing.
    assert fresh.reconcile_once() is False


def test_clearing_the_target_stops_the_loop(env):
    store, camp, db = env
    r = upload(store, camp)
    store.set_digest_target(r["id"], digest.MODEL, "medium")
    fake = FakeDigest()
    loop = digest.Reconciler(db, client_factory=lambda: fake, workers=1)
    original = fake.stream

    def stream(**kw):
        store.set_digest_target(r["id"], None)  # someone clicked Stop during the first call
        return original(**kw)

    fake.beta.messages.stream = stream
    loop.reconcile_once()
    assert len(fake.calls) == 1  # the call in flight finished and was kept; nothing more started
    assert store.digest_status(r["id"])["done"] == 1
    assert loop.reconcile_once() is False


def test_two_servers_overlapping_take_turns(env):
    """A rolling deploy runs two servers for a few seconds: only the lease holder digests."""
    store, camp, db = env
    r = upload(store, camp)
    store.set_digest_target(r["id"], digest.MODEL, "medium")
    old_calls, new_calls = FakeDigest(), FakeDigest()
    old = digest.Reconciler(db, client_factory=lambda: old_calls, workers=1)
    new = digest.Reconciler(db, client_factory=lambda: new_calls, workers=1)
    now = int(time.time())
    assert store.take_lease(old.LEASE, old.holder, 300, now)
    assert not store.take_lease(new.LEASE, new.holder, 300, now)
    new.reconcile_once()
    assert new_calls.calls == []  # it never got to start a call
    old.stop()  # the old server shuts down and hands the lease over
    assert store.take_lease(new.LEASE, new.holder, 300, now)
    while new.reconcile_once():
        pass
    assert store.digest_status(r["id"])["done"] == 8 and len(new_calls.calls) == 8


def test_merges_only_join_the_same_kind_of_thing():
    def c(name, kind):
        return {"name": name, "kind": kind, "aliases": [], "summary": "", "pages": [1], "facts": [], "stat_block": None}

    cands = [c("Heleka", "npc"), c("the giantess", "monster"), c("Lutheria", "location"), c("the Titan", "npc")]
    out = digest.apply_merges(cands, [
        Merge(ids=["e0", "e1"], name="Heleka", summary="A giantess."),  # npc + monster: one creature
        Merge(ids=["e2", "e3"], name="Lutheria", summary="lumped"),  # a place into a person: refused
    ])
    assert sorted(e["name"] for e in out) == ["Heleka", "Lutheria", "the Titan"]
    many = [c(f"Guard {i}", "npc") for i in range(digest.MAX_MERGE + 1)]
    out = digest.apply_merges(many, [Merge(ids=[f"e{i}" for i in range(len(many))], name="Guard", summary="")])
    assert len(out) == len(many)  # too many to be one thing


def test_a_new_reconciler_reapplies_catalogs_under_new_rules(env, monkeypatch):
    import campaign.world as world_mod

    store, camp, db = env
    r = upload(store, camp)
    store.set_digest_target(r["id"], digest.MODEL, "medium")
    first = digest.Reconciler(db, client_factory=FakeDigest)
    while first.pass_once():
        pass
    applied = store.conn.execute("SELECT input_key FROM runs WHERE kind = 'book'").fetchone()[0]
    monkeypatch.setattr(world_mod, "APPLY_VERSION", world_mod.APPLY_VERSION + 1)
    fake = FakeDigest()
    store.conn.execute("UPDATE leases SET expires_at = 0")
    store.conn.commit()
    again = digest.Reconciler(db, client_factory=lambda: fake)
    again.pass_once()
    assert fake.calls == []  # all from the cache
    assert store.conn.execute("SELECT input_key FROM runs WHERE kind = 'book'").fetchone()[0] != applied


def test_keyed_areas_treasure_and_tables_reach_the_tools(env):
    store, camp, db = env
    church = ("ENTITY location Crypt\nKEY Crypt E4\nTREASURE Crypt: 3 potions of healing\n"
              "TABLE Night Encounters (d6): 1-2=2d4 wolves;3-5=nothing;6=Strahd himself")
    r = upload(store, camp, church=church)
    digest.run(db, r["id"], client=FakeDigest())
    tools = {f.__name__: f for f in build_tools(ToolContext(store, camp))}
    assert json.loads(tools["lookup"](name="E4"))[0]["name"] == "Crypt"
    loot = json.loads(tools["treasure"](place="E4"))
    assert [f["statement"] for f in loot["treasure"]] == ["Treasure: 3 potions of healing"]
    # Taken: it moves to already_taken, and the book's apply doesn't bring it back.
    store.upsert_session(camp, 1)
    tools["end_fact"](fact_id=loot["treasure"][0]["fact_id"])
    loot = json.loads(tools["treasure"](place="Crypt"))
    assert loot["treasure"] == [] and len(loot["already_taken"]) == 1

    assert json.loads(tools["roll_table"](name=""))["tables"][0]["name"] == "Night Encounters"
    rolled = json.loads(tools["roll_table"](name="night", roll=6))
    assert rolled["result"] == "Strahd himself" and rolled["source"] == "Book"
    assert json.loads(tools["roll_table"](name="night", roll=2))["result"] == "2d4 wolves"
    assert 1 <= json.loads(tools["roll_table"](name="night encounters"))["roll"] <= 6


def test_a_section_starts_as_soon_as_its_own_subsections_are_done(env):
    """A slow section in Chapter 2 doesn't hold up Chapter 1's summary."""
    import threading

    store, camp, db = env
    r = upload(store, camp)
    fake = FakeDigest()
    chapter1_started = threading.Event()
    original = fake.stream

    def stream(**kw):
        title = kw["messages"][0]["content"].split("\n", 1)[0]
        if "Chapter 1" in title:
            chapter1_started.set()
        if "Windmill" in title:  # held until Chapter 1 has been asked for (or give up)
            assert chapter1_started.wait(5), "Chapter 1 waited for Chapter 2's slow section"
        return original(**kw)

    fake.beta.messages.stream = stream
    stats = digest.run(db, r["id"], client=fake, workers=8)
    assert stats["failed"] == 0 and stats["digested"] == 8


def test_sources_are_digested_side_by_side(env):
    import threading

    store, camp, db = env
    a, b = upload(store, camp), library.add_reference(store, camp, b"# Other\n## One\nENTITY npc Zed\n", "other.md", title="Other", kind="module")
    for src in (a, b):
        store.set_digest_target(src["id"], digest.MODEL, "medium")
    both = threading.Barrier(2, timeout=5)
    seen: set[str] = set()

    class Meeting(FakeDigest):
        def stream(self, **kw):
            book = "Other" if "Zed" in kw["messages"][0]["content"] or "One" in kw["messages"][0]["content"].split("\n")[0] else "Book"
            if book not in seen:
                seen.add(book)
                both.wait()  # each source's first call waits for the other's: only works side by side
            return super().stream(**kw)

    loop = digest.Reconciler(db, client_factory=Meeting)
    while loop.reconcile_once():
        pass
    assert not both.broken and seen == {"Book", "Other"}


def test_one_limit_on_calls_in_flight_across_sources(env, monkeypatch):
    import threading
    import time as _time

    store, camp, db = env
    a = upload(store, camp)
    b = library.add_reference(store, camp, b"# Other\n## One\nENTITY npc Zed\n## Two\nENTITY npc Yan\n", "other.md",
                              title="Other", kind="module")
    for src in (a, b):
        store.set_digest_target(src["id"], digest.MODEL, "medium")
    monkeypatch.setattr(digest, "_IN_FLIGHT", threading.BoundedSemaphore(2))
    now, most, lock = 0, 0, threading.Lock()

    class Counting(FakeDigest):
        def stream(self, **kw):
            nonlocal now, most
            with lock:
                now += 1
                most = max(most, now)
            _time.sleep(0.02)
            with lock:
                now -= 1
            return super().stream(**kw)

    loop = digest.Reconciler(db, client_factory=Counting, workers=8)
    while loop.reconcile_once():
        pass
    assert most == 2 and store.digest_status(a["id"])["done"] == 8


def test_a_stat_block_gets_back_the_lines_the_model_dropped():
    text = """# Swarm of Trickered Treats

_Medium swarm of Tiny constructs_

**AC** 12 **Initiative** +2 (12)<br />
**Hit Points** 28 (4d8 + 10)<br />

**Resistances** Bludgeoning, Piercing, Slashing<br />
**Immunities** Blinded, Charmed<br />
**Senses** Blindsight 10 ft., Passive Perception 6<br />
**Languages** understands the languages of its creator but cannot speak<br />
**Challenge** 1/4 (50 XP)

The **Swarm of Trickered Treats** Armor Class 99 is just a mention.
"""
    sb = {"armor_class": "12", "other": ["Senses blindsight 10 ft., passive Perception 6", "Immunity: blinded, charmed"]}
    got = digest.complete_stat_block(sb, text, "Swarm of Trickered Treats")
    assert got["other"] == [
        "Senses blindsight 10 ft., passive Perception 6", "Immunity: blinded, charmed",
        "Damage Resistances Bludgeoning, Piercing, Slashing",
        "Languages understands the languages of its creator but cannot speak",
    ]
    assert digest.complete_stat_block(got, text, "Swarm of Trickered Treats") == got  # nothing more to add
    assert digest.complete_stat_block(sb, text, "Imp") == sb  # not in this text: left alone
