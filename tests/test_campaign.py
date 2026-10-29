import json
from pathlib import Path

import pymupdf
import pytest

from campaign.charsheet import Attack, Bonus, CharacterSheet, Damage, Item, PartyStash, Status, merge_patch, move, rest
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
    # A magic item keeps its bonuses through the stash; it comes out unequipped.
    sheet.inventory.append(Item(name="Cloak of protection", equipped=True, attuned=True, requires_attunement=True,
                                bonuses=[Bonus(target="ac", amount=1)]))
    move(sheet, stash, "stash", {"Cloak of protection": 1})
    move(sheet, stash, "character", {"Cloak of protection": 1})
    cloak = sheet.inventory[-1]
    assert cloak.bonuses[0].amount == 1 and cloak.requires_attunement and not cloak.equipped and not cloak.attuned
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
    assert stash["currency"]["gp"] == 340 and [(i["name"], i["qty"]) for i in stash["items"]] == [("Silver mirror", 1)]
    assert [r["reason"] for r in store.party_stash_log(store.campaign("loot").id)] == ["the crypt", "the crypt"]


def test_merge_patch():
    assert merge_patch({"a": {"b": 1, "c": 2}, "d": [1]}, {"a": {"b": None, "e": 3}, "d": [2]}) == {
        "a": {"c": 2, "e": 3},
        "d": [2],
    }


def test_statuses_switch_their_bonuses_and_rests_end_them():
    cid = CharacterSheet(
        name="Cid", classes=[{"name": "Barbarian", "level": 7}], abilities={"str": 20},
        attacks=[Attack(name="Greataxe", damage=[Damage(dice="1d12", type="slashing", add_ability=True)])],
        statuses=[Status(name="Rage", resource="Rage")],
        bonuses=[Bonus(target="damage:str", amount=2, source="Rage", during="Rage")],
    )
    assert cid.derived()["attacks"][0]["text"] == "1d12+5 slashing"
    cid.statuses[0].active = True
    assert parts(cid.derived()["attacks"][0]["damage"][0]["bonus"]) == {"STR": 5, "Rage": 2}
    rest(cid, long=False)  # an hour: Rage is long over
    assert not cid.statuses[0].active and cid.derived()["attacks"][0]["text"] == "1d12+5 slashing"


def test_situational_damage_and_dice_that_scale_with_a_class():
    vex = CharacterSheet(
        name="Vex", classes=[{"name": "Rogue", "level": 7}, {"name": "Fighter", "level": 2}], abilities={"dex": 16},
        attacks=[Attack(name="Rapier", ability="finesse", damage=[
            Damage(dice="1d8", type="piercing", add_ability=True),
            Damage(dice="(half_level:rogue)d6", type="piercing", situational="Sneak Attack"),
            Damage(dice="2d6", type="radiant", situational="vs undead")])],
    )
    a = vex.derived()["attacks"][0]
    # Only what always applies is the attack's damage; the rest is offered when rolling.
    assert a["text"] == "1d8+3 piercing (Sneak Attack +4d6 piercing) (vs undead +2d6 radiant)"
    assert [(d["dice"], d["situational"]) for d in a["damage"]] == [
        ("1d8", ""), ("4d6", "Sneak Attack"), ("2d6", "vs undead")]  # rogue 7 of level 9: 4d6
    assert vex.amount("level:fighter") == 2 and vex.amount("half_level") == 5
    with pytest.raises(ValueError):
        Damage(dice="lots of d6")
    with pytest.raises(ValueError):
        Bonus(target="ac", amount="prof:rogue")


def test_items_can_raise_or_set_ability_scores():
    sheet = CharacterSheet(name="Cid", classes=[{"name": "Barbarian", "level": 7}], abilities={"str": 17, "cha": 10},
                           skill_proficiencies=["athletics"], inventory=[
        Item(name="Breastplate of Mytros", equipped=True, bonuses=[Bonus(target="score:cha", amount=1)]),
        Item(name="Gauntlets of Ogre Power", equipped=True, bonuses=[Bonus(target="score_set:str", amount=19)]),
    ])
    d = sheet.derived()
    assert parts(d["scores"]["cha"]) == {"Base": 10, "Breastplate of Mytros": 1} and d["modifiers"]["cha"] == 0
    assert d["scores"]["str"]["total"] == 19 and d["skills"]["athletics"]["total"] == 4 + 3
    sheet.abilities["str"] = 20  # better than the gauntlets: they stop mattering
    assert parts(sheet.derived()["scores"]["str"]) == {"Base": 20}


def test_nicknames_are_world_aliases(store):
    camp = store.create_campaign("Odyssey", "dm")
    t = tools(store, "odyssey")
    t["update_character"](name="Cid", patch={"nicknames": ["Lycidus"]}, reason="new")
    assert "Cid" in t["get_character"](name="Lycidus")
    from campaign.world import World
    assert World(store, camp).resolve("Lycidus")["name"] == "Cid"


def test_attack_options_apply_to_the_attacks_they_name():
    sheet = CharacterSheet(
        name="Cid", classes=[{"name": "Barbarian", "level": 7}], abilities={"str": 20},
        attacks=[
            Attack(name="Greataxe", range="5 ft", properties=["heavy", "two-handed"],
                   damage=[Damage(dice="1d12", type="slashing", add_ability=True)]),
            Attack(name="Spear", range="20/60 ft", properties=["thrown 20/60", "versatile 1d10"],
                   damage=[Damage(dice="1d8", type="piercing", add_ability=True)]),
            Attack(name="Longbow", range="150/600 ft", properties=["heavy", "ammunition"],
                   damage=[Damage(dice="1d8", type="piercing", add_ability=True),
                           Damage(dice="2d8", type="thunder", situational="Thunderous Shot", spends="Thunderous Shot")]),
            Attack(name="Fire bolt", ability="spell", range="120 ft"),
        ],
        attack_options=[
            {"name": "Reckless Attack", "applies_to": ["str", "melee"], "advantage": "advantage",
             "note": "Attacks against you have advantage until your next turn."},
            {"name": "Great Weapon Master", "applies_to": ["heavy", "melee"], "to_hit": -5, "damage": 10},
            {"name": "Sharpshooter", "applies_to": ["ranged", "weapon"], "to_hit": -5, "damage": "2*prof"},
            {"name": "Bless", "applies_to": []},
        ],
    )
    d = sheet.derived()
    names = [[sheet.attack_options[i].name for i in a["options"]] for a in d["attacks"]]
    assert names == [
        ["Reckless Attack", "Great Weapon Master", "Bless"],  # heavy melee, STR
        ["Reckless Attack", "Sharpshooter", "Bless"],  # thrown: melee and ranged
        ["Sharpshooter", "Bless"],  # ammunition: ranged only
        ["Bless"],  # a spell, not a weapon
    ]
    assert d["attack_options"][1] == {"to_hit": -5, "damage": 10}
    assert d["attack_options"][2] == {"to_hit": -5, "damage": 6}  # 2 x proficiency, which follows the level
    assert d["attacks"][2]["damage"][1]["spends"] == "Thunderous Shot"
    with pytest.raises(ValueError):
        CharacterSheet(name="X", attack_options=[{"name": "Odd", "advantage": "always"}])


def test_statuses_can_say_how_many_rounds_they_last():
    sheet = CharacterSheet(name="Cid", statuses=[{"name": "Rage", "resource": "Rage", "duration": 10}])
    assert sheet.statuses[0].duration == 10
    assert CharacterSheet(name="X", statuses=[{"name": "Bless"}]).statuses[0].duration == 0  # open-ended
    with pytest.raises(ValueError):
        CharacterSheet(name="X", statuses=[{"name": "Odd", "duration": -1}])
