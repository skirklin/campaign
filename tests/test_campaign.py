import json
from pathlib import Path

import pymupdf
import pytest

from campaign.charsheet import Attack, Bonus, Damage, Item, CharacterSheet, merge_patch
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
    assert d["saves"]["wis"]["total"] == 5 and d["skills"]["insight"]["total"] == 5
    assert d["spell_save_dc"]["total"] == 13 and d["passive_perception"]["total"] == 13

    t = tools(store, "chars")
    t["update_character"](name=sheet.name, patch=sheet.model_dump(), reason="import")
    assert "Updated" in t["update_character"](name=sheet.name, patch={"hp": {"current": 10}}, reason="ghoul")
    assert "Rejected" in t["update_character"](name=sheet.name, patch={"hp": {"current": "lots"}}, reason="bad")
    saved = json.loads(t["get_character"](name=sheet.name))
    assert saved["sheet"]["hp"] == {"base": 18, "current": 10, "temp": 0} and saved["derived"]["hp_max"]["total"] == 27
    character_id = store.find_character(sheet.name)["id"]
    assert [r["reason"] for r in store.character_log(character_id)][:2] == ["ghoul", "import"]


def parts(value):
    return {p["label"]: p["value"] for p in value["parts"]}


def test_numbers_follow_the_character():
    """Numbers are kept as parts: change a score or a level and everything built on it moves."""
    sheet = CharacterSheet.model_validate_json(EXAMPLE.read_text())
    d = sheet.derived()
    assert d["attacks"][0]["to_hit"]["total"] == 4 and d["attacks"][0]["text"] == "1d8+2 bludgeoning"
    assert parts(d["ac"]) == {"Chain mail": 16, "Shield": 2}
    assert parts(d["hp_max"]) == {"Hit dice": 18, "CON x 3": 6, "Dwarven Toughness": 3}

    sheet.abilities["str"] = 18  # +2 -> +4
    sheet.abilities["con"] = 17  # +2 -> +3
    sheet.classes[0].level = 5  # proficiency +2 -> +3
    d = sheet.derived()
    assert parts(d["attacks"][0]["to_hit"]) == {"STR": 4, "Proficiency": 3}
    assert d["attacks"][0]["text"] == "1d8+4 bludgeoning"
    assert d["skills"]["athletics"]["total"] == 4 and d["saves"]["wis"]["total"] == 3 + 3
    assert d["spell_save_dc"]["total"] == 8 + 3 + 3
    assert d["hp_max"]["total"] == 18 + 3 * 5 + 5  # CON and Dwarven Toughness both scale with level

    # An item's bonuses count while it's equipped (and attuned, if it needs it).
    sheet.inventory[2].equipped = False  # shield off
    sheet.inventory.append(Item(name="Cloak of protection", equipped=True, requires_attunement=True,
                                bonuses=[Bonus(target="ac", amount=1), Bonus(target="save", amount=1)]))
    assert sheet.derived()["ac"]["total"] == 16
    sheet.inventory[-1].attuned = True
    d = sheet.derived()
    assert parts(d["ac"]) == {"Chain mail": 16, "Cloak of protection": 1}
    assert parts(d["saves"]["dex"]) == {"Cloak of protection": 1}  # DEX +0 isn't listed

    # References in amounts follow the sheet; a switched-off bonus doesn't count.
    sheet.bonuses.append(Bonus(target="initiative", amount="half_prof", source="Jack of all trades"))
    sheet.bonuses.append(Bonus(target="damage:str", amount=2, source="Rage", active=False))
    d = sheet.derived()
    assert d["initiative"]["total"] == 1 and d["attacks"][0]["text"] == "1d8+4 bludgeoning"


def test_attacks_resolve_finesse_spell_and_magic():
    sheet = CharacterSheet(
        name="Vex", classes=[{"name": "Rogue", "level": 1}], abilities={"str": 10, "dex": 16, "int": 14},
        spellcasting_ability="int",
        attacks=[
            Attack(name="Rapier +1", ability="finesse", bonus=1,
                   damage=[Damage(dice="1d8", type="piercing", add_ability=True), Damage(dice="1d6", type="fire")]),
            Attack(name="Fire bolt", ability="spell", damage=[Damage(dice="1d10", type="fire")]),
        ],
    )
    rapier, bolt = sheet.derived()["attacks"]
    assert parts(rapier["to_hit"]) == {"DEX": 3, "Proficiency": 2, "Magic": 1}
    assert rapier["text"] == "1d8+4 piercing + 1d6 fire"
    assert bolt["to_hit"]["total"] == 4 and bolt["text"] == "1d10 fire"


def test_sheets_from_before_parts_keep_their_numbers():
    old = {
        "name": "Old", "classes": [{"name": "Fighter", "level": 4}], "abilities": {"con": 14},
        "ac": 17, "hp": {"max": 38, "current": 30, "temp": 0},
        "attacks": [{"name": "Longsword", "to_hit": "+6", "damage": "1d8+3 slashing + 1d6 radiant", "notes": "silvered"}],
    }
    sheet = CharacterSheet.model_validate(old)
    d = sheet.derived()
    assert d["ac"]["total"] == 17 and d["hp_max"]["total"] == 38 and sheet.hp.base == 30
    assert d["attacks"][0]["to_hit"]["total"] == 6
    assert d["attacks"][0]["text"] == "1d8+3 slashing + 1d6 radiant" and sheet.attacks[0].notes == "silvered"
    # A patch that sets the maximum (the old way) still works.
    sheet = CharacterSheet.model_validate(merge_patch(sheet.model_dump(), {"hp": {"max": 40}}))
    assert sheet.hp_max == 40
    with pytest.raises(ValueError):
        Bonus(target="ac", amount="lots")


def test_merge_patch():
    assert merge_patch({"a": {"b": 1, "c": 2}, "d": [1]}, {"a": {"b": None, "e": 3}, "d": [2]}) == {
        "a": {"c": 2, "e": 3},
        "d": [2],
    }
