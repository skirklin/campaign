"""Digest tree, Merkle cache invariants, entity merging, persistence."""

import json
import re
import time
from types import SimpleNamespace as NS

import pymupdf
import pytest

from campaign import digest, library
from campaign.db import Store
from campaign.digest import (
    BranchOutput, Entity, LeafOutput, Merge, ReadBlock, RollTable, StatBlock, StatBlockOutput, TableRow,
)
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

    def __init__(self, fail_on: str | None = None, text_sensitive: bool = False, fail_kind: str | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self.fail_kind = fail_kind
        self.text_sensitive = text_sensitive
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **kw):
        user = kw["messages"][0]["content"]
        title = re.sub(r" \(pp?\..*\)$", "", user.split("\n", 1)[0].removeprefix("Section: "))
        kind = {LeafOutput: "leaf", BranchOutput: "branch", StatBlockOutput: "statblock"}[kw["output_format"]]
        if kind == "statblock":
            parsed = fake_stat_block(user)
            title = ", ".join(b.stat_block.name if b.stat_block else "?" for b in parsed.blocks)
        self.calls.append((kind, title))
        if self.fail_on and self.fail_on in title and self.fail_kind in (None, kind):
            raise RuntimeError("boom")
        if kind == "statblock":
            pass
        elif kind == "leaf":
            ents = []
            for m in re.finditer(r"^ENTITY (\w+) ([^|\n]+?)(?: \| (.+))?$", user, re.M):
                key = re.search(rf"^KEY {re.escape(m[2])} (\S+)$", user, re.M)
                ents.append(Entity(
                    name=m[2], kind=m[1], aliases=[m[3]] if m[3] else [], summary=f"{m[2]} in {title}",
                    pages=[], facts=[f"seen in {title}"], area_key=key[1] if key else None,
                    stat_block_of=(re.findall(rf"^USES {re.escape(m[2])}: (.+)$", user, re.M) or [None])[0],
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


def fake_stat_block(text):
    """Each marked stat block in `text`, read the way the prompt asks: its title, its numbers, and its
    entries under their headings (the senses line left out, for the check against the text to put back)."""
    blocks = []
    for n in range(1, 5):
        if digest.mark(n) not in text:
            break
        before, after = text.split(digest.mark(n))
        after = after.split(digest.mark(n + 1))[0]
        titles = re.findall(r"^#+ (.+)$", before, re.M)
        if not titles or "Ship" in titles[-1]:
            blocks.append(ReadBlock(mark=n, stat_block=None))
            continue
        field = lambda label: (re.search(rf"^{label} (.+)$", after, re.M) or [None, None])[1]  # noqa: E731
        sections, current = {"traits": [], "actions": [], "legendary_actions": []}, "traits"
        intro = None
        for line in after.split("Challenge", 1)[1].splitlines()[1:]:
            if line.startswith("#"):
                break
            if line.strip() in ("Actions", "Legendary Actions"):
                current = line.strip().lower().replace(" ", "_")
            elif line.strip():
                if current == "legendary_actions" and "legendary actions" in line:
                    intro = line.strip()
                else:
                    sections[current].append(line.strip())
        blocks.append(ReadBlock(mark=n, stat_block=StatBlock(
            name=titles[-1], size_type=(re.findall(r"^_(.+)_$", before, re.M) or [""])[-1],
            armor_class=re.match(r"Armor Class (.+)", after)[1],
            hit_points=field("Hit Points"), speed=field("Speed") or "30 ft.", initiative=None,
            strength=10, dexterity=12, constitution=10, intelligence=10, wisdom=10, charisma=10,
            saving_throws=None, skills=None, damage_vulnerabilities=None, damage_resistances=None,
            damage_immunities=field("Damage Immunities"), condition_immunities=None, senses=None, languages=None,
            challenge=field("Challenge"), bonus_actions=[], reactions=[], legendary_intro=intro, lair_intro=None,
            lair_actions=[], other=[], **sections)))
    return StatBlockOutput(blocks=blocks)


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
    leaves = [n["title"] for n in store.digest_outline(r["id"]) if n["kind"] == "leaf"]
    assert leaves == ["Room 1; Room 2; Room 3", "Room 4; Room 5"]


def test_long_section_is_split(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "MAX_LEAF_CHARS", 3000)
    store = Store(tmp_path / "s.db")
    camp = store.create_campaign("S", "dm")
    body = "\n\n".join("para " * 100 for _ in range(20))  # ~10k chars
    r = library.add_reference(store, camp, f"# Long\n{body}".encode(), "l.md")
    titles = [n["title"] for n in store.digest_outline(r["id"]) if n["kind"] == "leaf"]
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


def test_finding_stat_blocks_in_a_clean_book():
    goblin, ogre = digest.find_stat_blocks([
        {"page": None, "heading": "Bestiary > Goblin", "text": "## Goblin\n\n_Small humanoid, neutral evil_\n\n"
         "**Armor Class** 15 (leather armor, shield)\n**Hit Points** 7 (2d6)\n\n***Scimitar.*** Melee Weapon Attack"},
        {"page": None, "heading": "Bestiary > Ogre", "text": "## Ogre\n\n_Large giant, chaotic evil_\n\n"
         "**AC** 11 **Initiative** -1 (9)\n**Hit Points** 59 (7d10 + 21)\n\n***Greatclub.*** Melee Weapon Attack"},
        {"page": None, "heading": "Bestiary > Rules", "text": "A creature's AC is how hard it is to hit."},
    ])
    assert (goblin.title, ogre.title) == ("Stat block: Goblin", "Stat block: Ogre")
    # Each from its title to the next block's title, with its own Armor Class line marked.
    assert goblin.text.startswith("Section: Bestiary > Goblin\n\n### Goblin") and "Scimitar" in goblin.text and "Ogre" not in goblin.text
    assert goblin.text.count("▶") == 1 and f"{digest.mark(1)}**Armor Class** 15" in goblin.text
    assert "Greatclub" in ogre.text and "how hard it is" in ogre.text
    assert f"{digest.mark(1)}**AC** 11" in ogre.text


def test_a_block_under_a_generic_heading_is_named_by_its_section():
    (treat,) = digest.find_stat_blocks([
        {"page": None, "heading": "Monsters > Swarm of Trickered Treats > Description", "text": "Candy piles."},
        {"page": None, "heading": "Monsters > Swarm of Trickered Treats > Attributes",
         "text": "_Medium swarm of Tiny constructs_\n\n**AC** 12 **Initiative** +2 (12)<br />\n**Hit Points** 28 (4d8 + 10)"},
    ])
    assert treat.title == "Stat block: Swarm of Trickered Treats"
    assert treat.text.startswith("Section: Monsters > Swarm of Trickered Treats > Attributes\n\n### Attributes")


def test_blocks_printed_side_by_side_are_read_together():
    """p. 396 of Odyssey of the Dragonlords: the great boar and the white stag side by side, their
    lines alternating, under one outline section named for the stag."""
    nodes = digest.find_stat_blocks([
        {"page": 396, "heading": "White Stag > Great Boar",
         "text": "there is no mistaking the great boar for an earthly creature.\n\nLarge celestial, unaligned"},
        {"page": 396, "heading": "White Stag > White Stag",
         "text": "Huge celestial, unaligned\n\nArmor Class 14 (natural armor)\nHit Points 52 (5d10 + 25)\nSpeed 40 ft.\n\n"
                 "Armor Class 15 (natural armor)\nHit Points 63 (6d12 + 24)\nSpeed 60 ft.\n\n"
                 "Challenge 3 (700 XP)\n\nChallenge 4 (1,100 XP)\n\nCharge. If the boar moves at least 20 ft."},
        {"page": 397, "heading": "Wolf", "text": "### Wolf\n\nArmor Class 13\nHit Points 11 (2d8 + 2)\nChallenge 1/4"},
    ])
    assert [n.title for n in nodes] == ["Stat blocks: White Stag, White Stag", "Stat block: Wolf"]
    pair = nodes[0]
    assert f"{digest.mark(1)}Armor Class 14" in pair.text and f"{digest.mark(2)}Armor Class 15" in pair.text
    assert "Section: White Stag > Great Boar\n" in pair.text and "great boar" in pair.text  # its name is in view
    assert digest.stat_lines(pair.text) == {}  # whose lines are whose is the reading's job
    assert digest.mark(2) not in nodes[1].text


def test_stat_blocks_are_read_with_sonnet_at_least(env, monkeypatch):
    assert digest.statblock_model("claude-haiku-5-5") == "claude-sonnet-5-5"
    assert digest.statblock_model("claude-opus-5-5") == "claude-opus-5-5"
    store, camp, db = env
    r = library.add_reference(store, camp, BESTIARY.encode(), "bestiary.md", title="Bestiary", kind="module")
    fake = FakeDigest()
    models = []
    stream = fake.stream
    fake.beta.messages.stream = lambda **kw: (models.append((kw["output_format"].__name__, kw["model"])), stream(**kw))[1]
    digest.run(db, r["id"], client=fake, model="claude-haiku-5-5")
    assert {m for f, m in models if f == "StatBlockOutput"} == {"claude-sonnet-5-5"}
    assert {m for f, m in models if f != "StatBlockOutput"} == {"claude-haiku-5-5"}
    used = {u["model"] for u in store.digest_usage(r["id"])}
    assert used == {"claude-haiku-5-5", "claude-sonnet-5-5"}  # what each call cost, at its own price
    assert not digest._unfinished(store, r["id"], "claude-haiku-5-5")  # done, though not all with Haiku


def test_a_stat_block_a_pdf_split_and_interleaved_arrives_whole():
    """Sydon's block on p. 347 of Odyssey of the Dragonlords, as the PDF's text gives it: its legendary
    actions under an outline section of their own, and its columns interleaved."""
    (sydon,) = digest.find_stat_blocks([
        {"page": 347, "heading": "Sydon, The Lord of Storms > Sydon, The Lord of Storms",
         "text": "Manipulative. Lutheria is fond of bargaining with"},
        {"page": 347, "heading": "Sydon, The Lord of Storms > Sydon",
         "text": "Huge celestial (empyrean), lawful evil\n\nArmor Class 22 (natural)\nHit Points 313 (19d12 + 190)\n"
                 "Speed 50 ft., fly 50 ft., swim 50 ft.\n\nGlaive. Melee Weapon Attack: +17 to hit, reach 10 ft.\n\n"
                 "STR\nDEX\nCON\nINT\nWIS\nCHA\n30 (+10) 21 (+5) 30 (+10) 21 (+5) 22 (+6) 27 (+8)\n\n"
                 "Saving Throws Str +17, Int +12, Wis +13, Cha +15\nDamage Immunities bludgeoning, piercing, and slashing"},
        {"page": 347, "heading": "Sydon, The Lord of Storms > Legendary Actions",
         "text": "from nonmagical attacks\nSenses truesight 120 ft., passive Perception 16\nLanguages all\n"
                 "Challenge 23 (50,000 XP)\n\nSydon can take 3 legendary actions, choosing from\nthe options below.\n\n"
                 "Attack. Sydon makes one attack with his glaive or bolt.\nTremor Strike (Costs 2 Actions). Sydon strikes the"},
    ])
    assert sydon.title == "Stat block: Sydon" and sydon.page_start == 347
    # From a PDF, it starts earlier too, and names every section it spans.
    assert sydon.text.startswith("Section: Sydon, The Lord of Storms > Sydon, The Lord of Storms\n"
                                 "Section: Sydon, The Lord of Storms > Sydon\n"
                                 "Section: Sydon, The Lord of Storms > Legendary Actions\n\n")
    assert "Tremor Strike" in sydon.text and f"{digest.mark(1)}Armor Class 22" in sydon.text
    assert digest.mark(2) not in sydon.text
    # The labelled lines the reading leaves out are put back from the text; what it gave is kept.
    got = digest.fill_missing_lines({"name": "Sydon", "senses": None, "languages": "all", "skills": None}, sydon.text)
    assert got["senses"] == "truesight 120 ft., passive Perception 16" and got["languages"] == "all"
    assert got["saving_throws"] == "Str +17, Int +12, Wis +13, Cha +15" and got["skills"] is None


BESTIARY = """\
# Bestiary
## Titans
ENTITY npc Sydon, Lord of Storms | Sydon
Sydon rules the storms.

### Sydon
_Huge celestial (empyrean), lawful evil_

Armor Class 22 (natural)
Hit Points 313 (19d12 + 190)
Speed 50 ft., fly 50 ft.
Damage Immunities lightning
Senses truesight 120 ft., passive Perception 16
Challenge 23 (50,000 XP)
Magic Resistance. Sydon has advantage on saving throws against spells.
Actions
Glaive. Melee Weapon Attack: +17 to hit, reach 10 ft., one target. Hit: 23 (3d8 + 10) slashing damage.
Legendary Actions
Sydon can take 3 legendary actions, choosing from the options below.
Attack. Sydon makes one attack with his glaive.

## Beasts
ENTITY monster The Hill Wolf | the grey wolf
USES The Hill Wolf: wolves
The hill wolf hunts the shepherds.

### Wolf
_Medium beast, unaligned_

Armor Class 13 (natural armor)
Hit Points 11 (2d8 + 2)
Challenge 1/4 (50 XP)
Bite. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 7 (2d4 + 2) piercing damage.

### The Ultros Ship
Armor Class 15
Hit Points 300
"""


def test_stat_blocks_are_read_on_their_own_and_join_the_world(env):
    store, camp, db = env
    r = library.add_reference(store, camp, BESTIARY.encode(), "bestiary.md", title="Bestiary", kind="module")
    fake = FakeDigest()
    stats = digest.run(db, r["id"], client=fake)
    assert stats["failed"] == 0
    assert sorted(t for k, t in fake.calls if k == "statblock") == ["?", "Sydon", "Wolf"]  # the ship isn't one
    status = store.digest_status(r["id"])
    assert status["total"] == status["done"] == stats["nodes"]
    assert [n["kind"] for n in store.digest_outline(r["id"])].count("statblock") == 0  # beside the tree

    world = World(store, camp)
    # Sydon's block joins the Sydon the book's text named; the wolf, which nothing named, is a monster of its own.
    sydon = world.resolve("Sydon, Lord of Storms")
    (sb,) = [json.loads(v) for v in world.attribute_values(sydon["id"], "stat_block")]
    assert sb["legendary_intro"].startswith("Sydon can take 3") and sb["legendary_actions"] == [
        "Attack. Sydon makes one attack with his glaive."]
    assert sb["senses"] == "truesight 120 ft., passive Perception 16"  # put back from the text
    assert world.resolve("Wolf")["kind"] == "monster"
    # A creature the book runs on another's block gets that block, by the name the book gave it.
    sb, source = world.stat_block(world.resolve("the grey wolf")["id"])
    assert source["name"] == "Wolf" and sb["hit_points"] == "11 (2d8 + 2)"

    tools = {t.__name__: t for t in build_tools(ToolContext(store, camp))}
    text = tools["stat_block"]("Sydon")
    assert text.startswith("Sydon, Lord of Storms\nHuge celestial (empyrean), lawful evil\nAC 22 (natural) · HP 313")
    assert "\nDamage Immunities lightning\n" in text
    assert "Legendary Actions:\nSydon can take 3 legendary actions, choosing from the options below.\n- Attack." in text

    # Everything is cached: running again calls nothing, and another campaign gets it free.
    again = FakeDigest()
    assert digest.run(db, r["id"], client=again)["digested"] == 0 and again.calls == []
    other = store.create_campaign("Other", "dm")
    r2 = library.add_reference(store, other, BESTIARY.encode(), "bestiary.md", title="Bestiary", kind="module")
    assert r2["pending"] == 0
    assert World(store, other).attribute_values(World(store, other).resolve("Wolf")["id"], "stat_block")


def test_a_failed_stat_block_doesnt_hold_up_the_book(env, monkeypatch):
    store, camp, db = env
    r = library.add_reference(store, camp, BESTIARY.encode(), "bestiary.md", title="Bestiary", kind="module")
    stats = digest.run(db, r["id"], client=FakeDigest(fail_on="Wolf", fail_kind="statblock"))
    assert stats["failed"] == 1
    world = World(store, camp)
    assert world.attribute_values(world.resolve("Sydon")["id"], "stat_block")  # applied without the wolf's
    assert world.resolve("Wolf") is None
    digest.retry_failed(store, r["id"])
    digest.run(db, r["id"], client=FakeDigest())
    assert world.attribute_values(world.resolve("Wolf")["id"], "stat_block")  # and again once it's read


def test_a_copy_without_a_target_catches_up_from_the_cache_after_a_new_version(env, monkeypatch):
    """What happened to Odyssey's copy of the SRD: the same book in two campaigns, digested once, then
    a new version of the digest. The copy with a target is digested again; the one without is planned
    too, so it shows what's left (and its Digest button), and takes the new catalog from the cache."""
    store, camp, db = env
    copy_camp = store.create_campaign("Odyssey", "dm")
    a = library.add_reference(store, camp, BESTIARY.encode(), "bestiary.md", title="Bestiary", kind="module")
    store.set_digest_target(a["id"], digest.MODEL, "medium")
    loop = digest.Reconciler(db, client_factory=FakeDigest)
    while loop.pass_once():
        pass
    b = library.add_reference(store, copy_camp, BESTIARY.encode(), "bestiary.md", title="Bestiary", kind="module")
    assert b["pending"] == 0 and store.source(b["id"])["digest_model"] is None

    monkeypatch.setattr(digest, "STATBLOCK_VERSION", digest.STATBLOCK_VERSION + 1)
    store.conn.execute("UPDATE leases SET expires_at = 0")
    store.conn.commit()
    loop = digest.Reconciler(db, client_factory=FakeDigest)
    loop.replan()
    status = store.digest_status(b["id"])
    assert status["done"] < status["total"]  # out of date, and the page says so
    while loop.reconcile_once():  # the one with a target is digested again...
        pass
    loop = digest.Reconciler(db, client_factory=FakeDigest)
    loop.replan()  # ...and the next start finds the copy's new catalog in the cache, for nothing
    status = store.digest_status(b["id"])
    assert status["done"] == status["total"]
    keys = {r["input_key"] for r in store.conn.execute("SELECT input_key FROM runs WHERE kind = 'book'")}
    assert len(keys) == 1  # both campaigns applied the same new catalog
