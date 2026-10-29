import json
from pathlib import Path

import pymupdf
import pytest

from campaign.charsheet import CharacterSheet, PartyStash, merge_patch, move
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


def test_read_only_tools_exclude_writers(store):
    store.create_campaign("RO", "dm")
    names = {f.__name__ for f in build_tools(ToolContext(store, store.campaign("ro")), writable=False)}
    assert "record_fact" not in names and "search_reference" in names and "lookup" in names


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
    character_id = store.find_character(sheet.name)["id"]
    assert [r["reason"] for r in store.character_log(character_id)][:2] == ["ghoul", "import"]


def test_moving_between_a_character_and_the_party_stash():
    sheet = CharacterSheet.model_validate_json(EXAMPLE.read_text())
    stash = PartyStash(items=[{"name": "Potion of Healing", "qty": 3}], currency={"gp": 100})
    # Taking stacks onto what the character has, by name, ignoring case.
    assert move(sheet, stash, "character", {"potion of healing": 2}, {"gp": 40}) == "Potion of Healing ×2, 40 gp"
    assert next(i for i in sheet.inventory if i.name == "Potion of healing").qty == 4
    assert stash.items[0].qty == 1 and stash.currency["gp"] == 60 and sheet.currency["gp"] == 78
    # Putting in moves the whole stack when it's all of it, and makes a stash item.
    move(sheet, stash, "stash", {"Shield": 1})
    assert "Shield" not in [i.name for i in sheet.inventory] and stash.items[-1].name == "Shield"
    # Not enough changes nothing.
    before = (sheet.model_dump(), stash.model_dump())
    with pytest.raises(ValueError, match="only 1"):
        move(sheet, stash, "character", {"Potion of Healing": 1, "Shield": 2})
    with pytest.raises(ValueError, match="only 60 gp"):
        move(sheet, stash, "character", {}, {"gp": 61})
    with pytest.raises(ValueError, match="nothing"):
        move(sheet, stash, "stash")
    assert (sheet.model_dump(), stash.model_dump()) == before


def test_party_stash_tools(store):
    store.create_campaign("Loot", "dm")
    t = tools(store, "loot")
    assert json.loads(t["get_party_stash"]())["items"] == []
    assert "Updated" in t["update_party_stash"](patch={"currency": {"gp": 340}}, reason="the crypt")
    assert "Rejected" in t["update_party_stash"](patch={"items": [{"qty": 2}]}, reason="bad")
    t["update_party_stash"](patch={"items": [{"name": "Silver mirror"}]}, reason="the crypt")
    stash = json.loads(t["get_party_stash"]())
    assert stash["currency"]["gp"] == 340 and stash["items"] == [{"name": "Silver mirror", "qty": 1, "notes": ""}]
    assert [r["reason"] for r in store.party_stash_log(store.campaign("loot").id)] == ["the crypt", "the crypt"]


def test_merge_patch():
    assert merge_patch({"a": {"b": 1, "c": 2}, "d": [1]}, {"a": {"b": None, "e": 3}, "d": [2]}) == {
        "a": {"c": 2, "e": 3},
        "d": [2],
    }
