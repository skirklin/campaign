import json
from pathlib import Path

import pymupdf
import pytest

from campaign.charsheet import CharacterSheet, merge_patch
from campaign.db import Store
from campaign.ingest import chunk_file, chunk_markdown
from campaign.tools import ToolContext, build_tools

EXAMPLE = Path(__file__).parent.parent / "examples" / "character.json"


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def tools(store, slug, **kw):
    return {f.__name__: f for f in build_tools(ToolContext(store, store.campaign(slug), **kw))}


def make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Death House", fontsize=20)
    page.insert_text((72, 110), "The children Rose and Thorn wait outside the gate.", fontsize=10)
    page = doc.new_page()
    page.insert_text((72, 72), "Grick", fontsize=16)
    page.insert_text((72, 110), "Armor Class 14 (natural armor). Hit Points 27 (6d8).", fontsize=10)
    doc.set_toc([[1, "Chapter 1: Death House", 1], [1, "Appendix: Monsters", 2]])
    doc.save(path)


def test_pdf_chunks_follow_headings_and_outline(tmp_path):
    pdf = tmp_path / "module.pdf"
    make_pdf(pdf)
    chunks = chunk_file(pdf)
    assert [c["page"] for c in chunks] == [1, 2]
    assert chunks[0]["heading"] == "Chapter 1: Death House > Death House"
    assert chunks[1]["heading"] == "Appendix: Monsters > Grick"
    assert "Hit Points 27" in chunks[1]["text"]


def test_markdown_heading_trail():
    chunks = chunk_markdown("# Barovia\nmist\n## Village\nsquare\n## Tser Pool\ncamp\n")
    assert [c["heading"] for c in chunks] == ["Barovia", "Barovia > Village", "Barovia > Tser Pool"]


def test_search_and_spoiler_filter(store, tmp_path):
    pdf = tmp_path / "m.pdf"
    make_pdf(pdf)
    dm = store.create_campaign("Strahd DM", "dm")
    pl = store.create_campaign("Strahd Player", "player")
    for c in (dm, pl):
        store.add_source(c, "CoS", "module", chunk_file(pdf), dm_only=True)
        store.add_source(c, "My notes", "notes", chunk_markdown("# Grick\nwe fought one in the cellar"))
    dm_hits = store.search(dm, "grick hit points")
    assert dm_hits[0]["source"] == "CoS" and dm_hits[0]["page"] == 2
    assert {h["source"] for h in store.search(pl, "grick")} == {"My notes"}
    assert store.read_chunks(pl, dm_hits[0]["id"]) == []
    # punctuation in queries must not break FTS
    assert store.search(dm, "Rose's \"gate\" (AND)")


def test_entity_merge_history_and_revert(store):
    c = store.create_campaign("Test", "dm")
    store.upsert_entity(c, "npc", "Ireena", status="alive", data={"location": "Barovia"}, reason="met")
    s1 = store.upsert_session(c, 1)
    store.upsert_entity(c, "npc", "Ireena", data={"location": "Vallaki", "trust": "high"}, session_id=s1, reason="escorted")
    store.upsert_entity(c, "quest", "Escort Ireena", status="active", session_id=s1, reason="accepted")
    store.add_event(c, "Escorted Ireena to Vallaki", session_id=s1)
    e = store.get_entity(c, "npc", "ireena")
    assert e["data"] == {"location": "Vallaki", "trust": "high"} and e["status"] == "alive"
    assert store.revert_session(c, 1) == {"restored": 1, "deleted": 1, "events": 1}
    assert store.get_entity(c, "npc", "Ireena")["data"] == {"location": "Barovia"}
    assert store.get_entity(c, "quest", "Escort Ireena") is None


def test_player_campaign_hides_undiscovered_entities(store):
    c = store.create_campaign("P", "player")
    store.upsert_entity(c, "npc", "Secret villain", party_knows=False)
    store.upsert_entity(c, "npc", "Innkeeper")
    assert [e["name"] for e in store.find_entities(c)] == ["Innkeeper"]
    assert store.get_entity(c, "npc", "Secret villain") is None


def test_tools_round_trip(store):
    store.create_campaign("Tools", "dm")
    t = tools(store, "tools")
    assert "Saved quest" in t["record_entity"](
        kind="quest", name="Find the Tome", reason="given by Kolyan", status="active",
        data={"reward": "500 gp"},
    )
    t["add_journal_entry"](text="Accepted the quest", entities=["Find the Tome"])
    entity = json.loads(t["get_entity"](kind="quest", name="Find the Tome"))
    assert entity["data"]["reward"] == "500 gp"
    assert entity["history"][0]["reason"] == "given by Kolyan"
    assert json.loads(t["recent_journal"]())[0]["entity_ids"] == [entity["id"]]


def test_read_only_tools_exclude_writers(store):
    store.create_campaign("RO", "dm")
    names = {f.__name__ for f in build_tools(ToolContext(store, store.campaign("ro")), writable=False)}
    assert "record_entity" not in names and "search_reference" in names


def test_character_sheet_derived_and_patch(store):
    store.create_campaign("Chars", "player")
    sheet = CharacterSheet.model_validate_json(EXAMPLE.read_text())
    d = sheet.derived()
    assert d["proficiency_bonus"] == 2
    assert d["saves"]["wis"] == 5 and d["skills"]["insight"] == 5
    assert d["spell_save_dc"] == 13 and d["passive_perception"] == 13

    t = tools(store, "chars")
    t["update_character"](name=sheet.name, patch=sheet.model_dump(), reason="import")
    assert "Updated" in t["update_character"](name=sheet.name, patch={"hp": {"current": 10}}, reason="ghoul")
    assert "Rejected" in t["update_character"](name=sheet.name, patch={"hp": {"current": "lots"}}, reason="bad")
    saved = json.loads(t["get_character"](name=sheet.name))
    assert saved["sheet"]["hp"] == {"max": 27, "current": 10, "temp": 0}
    assert [r["reason"] for r in store.character_log(sheet.name)][:2] == ["ghoul", "import"]


def test_merge_patch():
    assert merge_patch({"a": {"b": 1, "c": 2}, "d": [1]}, {"a": {"b": None, "e": 3}, "d": [2]}) == {
        "a": {"c": 2, "e": 3},
        "d": [2],
    }
