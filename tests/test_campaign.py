import json
from pathlib import Path

import pymupdf
import pytest

from campaign.charsheet import Attack, Bonus, CharacterFile, CharacterSheet, Damage, Item, ItemMechanics, Status, merge_patch, rest
from campaign.inventory import Inventory, InventoryError
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
    file = CharacterFile.model_validate_json(EXAMPLE.read_text())
    sheet = file.sheet()
    d = file.worn().derived()
    assert d["proficiency_bonus"] == 2 and d["ac"]["total"] == 18  # the shield counts while it's equipped
    assert d["saves"]["wis"]["total"] == 5 and d["skills"]["insight"]["total"] == 5
    assert d["spell_save_dc"]["total"] == 13 and d["passive_perception"]["total"] == 13

    t = tools(store, "chars")
    t["update_character"](name=sheet.name, patch=sheet.model_dump(), reason="import")
    assert "Rejected" in t["update_character"](name=sheet.name, patch={"currency": {"gp": 5}}, reason="bad")
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
    sheet = CharacterFile.model_validate_json(EXAMPLE.read_text()).worn()
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
    assert sheet.worn().derived()["ac"]["total"] == 16
    sheet.inventory[-1].attuned = True
    d = sheet.worn().derived()
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


def test_the_party_inventory(store):
    """One list of everything the party owns: claiming, giving and putting back are changes of hands."""
    camp = store.create_campaign("Loot", "dm")
    inv = Inventory(store, camp)
    agrippa = inv.character_holder(store.create_character(CharacterSheet(name="Agrippa").model_dump(), camp.id))
    brannoc = inv.character_holder(store.create_character(CharacterSheet(name="Brannoc").model_dump(), camp.id))
    potions = inv.add("Potion of Healing", 3, reason="the crypt")
    pearl = inv.add("Black pearl", value_gp=1000)
    assert inv.add("potion of healing", 2) == potions and inv.holding(potions)["qty"] == 5  # the same thing stacks

    # Claiming part of a stack splits it; claiming the rest joins the claimed part.
    assert inv.move(potions, agrippa, 2) == "Agrippa took Potion of Healing ×2"
    mine = inv.holdings(agrippa)[0]
    assert mine["qty"] == 2 and inv.holding(potions)["qty"] == 3
    inv.move(potions, agrippa)
    assert [(h["name"], h["qty"]) for h in inv.holdings(agrippa)] == [("Potion of Healing", 5)]
    assert [h["name"] for h in inv.holdings(None)] == ["Black pearl"]
    inv.change(mine["id"], equipped=True)
    assert inv.move(mine["id"], brannoc, 1) == "Agrippa gave Potion of Healing to Brannoc"
    assert not inv.holdings(brannoc)[0]["equipped"]  # it arrives unequipped
    with pytest.raises(InventoryError, match="only 4"):
        inv.move(mine["id"], None, 5)
    vulkan = inv.resolve_holder("Vulkan")  # someone the world didn't know is an NPC now
    inv.move(pearl, vulkan, reason="to set in the trident")
    assert inv.world.entity(vulkan)["kind"] == "npc" and inv.holdings(vulkan)[0]["name"] == "Black pearl"
    assert inv.change(mine["id"], qty=0).startswith("Potion of Healing ×4 gone")

    # Coin: changes, not totals; handing over; sharing the party's purse out evenly.
    inv.coin(None, {"gp": 101, "sp": 3})
    with pytest.raises(InventoryError, match="only has 101 gp"):
        inv.coin(None, {"gp": -102})
    inv.split()
    assert inv.purse(agrippa)["gp"] == 50 and inv.purse(brannoc)["sp"] == 1 and inv.purse(None) == \
        {"cp": 0, "sp": 1, "ep": 0, "gp": 1, "pp": 0}
    inv.give_coin(agrippa, brannoc, {"gp": 10})
    assert inv.purse(brannoc)["gp"] == 60

    overview = inv.overview()
    assert [h["name"] for h in overview["holders"]] == ["The party", "Agrippa", "Brannoc", "Vulkan"]
    assert overview["worth_gp"] == 1000 + 1.1 + 40.1 + 60.1  # the pearl, then each purse
    assert inv.log()[0]["text"] == "Agrippa gave 10 gp to Brannoc"
    assert any("Black pearl" in r["statement"] for r in inv.world.timeline(vulkan))  # on the world's timeline too


def test_items_are_defined_once_and_counted_from_what_is_equipped(store):
    """An item that does something points at its world item's definition; sheets count what's equipped."""
    camp = store.create_campaign("Defs", "dm")
    inv = Inventory(store, camp)
    ch = store.create_character(CharacterSheet(name="Agrippa", ac={"base": 16, "abilities": []}).model_dump(), camp.id)
    agrippa = inv.character_holder(ch)
    cloak = ItemMechanics(requires_attunement=True, bonuses=[Bonus(target="ac", amount=1)], value_gp=500)
    item = inv.define("Cloak of Protection", cloak)
    held = inv.add("cloak of protection", holder_id=agrippa)  # by name, linked to the definition
    assert inv.holding(held)["item"]["id"] == item and inv.holding(held)["value_gp"] == 500
    assert inv.sheet(store.character(ch)).derived()["ac"]["total"] == 16
    inv.change(held, equipped=True, attuned=True)
    assert inv.sheet(store.character(ch)).derived()["ac"]["total"] == 17
    # Fixing the definition fixes it for every copy.
    inv.define("Cloak of Protection", cloak.model_copy(update={"bonuses": [Bonus(target="ac", amount=2)]}))
    assert inv.sheet(store.character(ch)).derived()["ac"]["total"] == 18
    assert inv.world.held_by(item) == [{"holder": "Agrippa", "qty": 1}]

    # A secret definition: the DM's sheet counts it, a player's doesn't, and a player can't overwrite it.
    sword = inv.add("Xiphos sword of magical", holder_id=agrippa)
    secret = inv.define("Sword of Vengeance", ItemMechanics(bonuses=[Bonus(target="attack", amount=-1)]), party_knows=False)
    inv.link(sword, secret)
    inv.change(sword, equipped=True)
    from dataclasses import replace
    player = Inventory(store, replace(camp, role="player"))
    assert player.holding(sword)["item"] == {"id": None, "name": None, "defined": False, "secret": False}
    assert player.holding(sword)["name"] == "Xiphos sword of magical"
    assert inv.holding(sword)["item"]["secret"] and inv.holding(sword)["bonuses"]
    with pytest.raises(InventoryError, match="DM"):
        player.define("Sword of Vengeance", ItemMechanics())
    assert [g.name for g in inv.gear(agrippa)] == ["cloak of protection", "Xiphos sword of magical"]
    assert [g.name for g in player.gear(agrippa)] == ["cloak of protection"]

    # A character file brings its gear in: items that do something get definitions.
    file = CharacterFile.model_validate_json(EXAMPLE.read_text())
    other = store.create_character(file.sheet().model_dump(), camp.id)
    inv.bring_in(inv.character_holder(other), file.inventory, file.currency)
    assert inv.sheet(store.character(other)).derived()["ac"]["total"] == 18  # chain mail 16 + the shield's 2
    shield = next(h for h in inv.holdings(inv.character_holder(other)) if h["name"] == "Shield")
    assert shield["item"]["name"] == "Shield" and inv.purse(inv.character_holder(other))["gp"] == 38
    assert [i.name for i in inv.carried(inv.character_holder(other))] == [i.name for i in file.inventory]


def test_party_inventory_tools(store):
    store.create_campaign("Loot", "dm")
    t = tools(store, "loot")
    t["update_character"](name="Agrippa", patch={}, reason="new")
    assert "Added for the party: Silver mirror, Potion of healing ×2" in t["add_items"](
        items=[{"name": "Silver mirror", "value_gp": 25}, {"name": "Potion of healing", "qty": 2}], reason="the crypt")
    assert "Not done" in t["add_items"](items=[{"qty": 2}], reason="bad")
    assert "+340 gp" in t["change_coin"](holder="party", change={"gp": 340}, reason="the crypt")
    everything = json.loads(t["inventory"]())
    party = everything["holders"][0]
    assert party["purse"] == {"gp": 340} and party["items"][0] == {"id": 1, "name": "Silver mirror", "value_gp": 25}
    assert t["give_item"](item_id=2, to="Agrippa", qty=1) == "Agrippa took Potion of healing"
    assert "AC" not in t["define_item"](name="Ring of Protection", item_id=1, requires_attunement=True,
                                        bonuses=[{"target": "ac", "amount": 1}], value_gp=3500)
    mirror = json.loads(t["inventory"]())["holders"][0]["items"][0]
    assert mirror["world_item"] == "Ring of Protection" and mirror["requires_attunement"]
    character = json.loads(t["get_character"](name="Agrippa"))
    assert character["carrying"] == [{"id": 3, "name": "Potion of healing"}]


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
    sheet = CharacterFile(name="Cid", classes=[{"name": "Barbarian", "level": 7}], abilities={"str": 17, "cha": 10},
                          skill_proficiencies=["athletics"], inventory=[
        Item(name="Breastplate of Mytros", equipped=True, bonuses=[Bonus(target="score:cha", amount=1)]),
        Item(name="Gauntlets of Ogre Power", equipped=True, bonuses=[Bonus(target="score_set:str", amount=19)]),
    ]).worn()
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


def test_sheets_from_when_turns_were_tracked_still_load():
    """Turn tracking (attacks per action, what an attack or status took) came and went: those
    fields are read and dropped, so the sheets saved meanwhile load and save without them."""
    saved = {
        "name": "Cid", "attacks_per_action": 2, "grapple_bonus_action": True,
        "statuses": [{"name": "Rage", "resource": "Rage", "uses": "bonus", "duration": 10}],
        "attacks": [{"name": "Greataxe", "uses": "action"}, {"name": "Handaxe", "uses": "bonus"}],
    }
    sheet = CharacterSheet.model_validate(saved)
    dumped = sheet.model_dump()
    assert "attacks_per_action" not in dumped and "grapple_bonus_action" not in dumped
    assert "uses" not in dumped["statuses"][0] and all("uses" not in a for a in dumped["attacks"])
    assert dumped["statuses"][0]["duration"] == 10  # what's still a field stays


def test_markdown_footnotes_travel_with_what_cites_them():
    from campaign.ingest import parse_markdown

    doc = parse_markdown(
        "# Guadaña\n## Hosts\nThey use the _bone knight_[^1] stat block.\n## Games\nA roll[^2] on the table.\n"
        "## Notes\n[^1]: Source: _Eberron: Rising from the Last War_\n[^2]: A d10,\n    or a d20 for big parties.\n"
    )
    text = "\n".join(c["text"] for c in doc.chunks)
    assert "bone knight_ (Source: _Eberron: Rising from the Last War_) stat block" in text
    assert "roll (A d10, or a d20 for big parties.) on the table" in text
    assert "[^" not in text and "]:" not in text


def test_inventories_and_the_stash_move_into_the_party_inventory(tmp_path):
    """v15: what sheets carried, and the party stash, become holdings and purses."""
    from campaign.db import V12_PARTY_STASH

    db = tmp_path / "old.db"
    s = Store(db)
    camp = s.create_campaign("Odyssey", "dm")
    old = json.loads(EXAMPLE.read_text())  # a sheet as v14 kept it, inventory and coin and all
    cid = s.create_character(old, camp.id)
    s.conn.executescript(f"DROP TABLE inventory_log; DROP TABLE purses; DROP TABLE holdings; {V12_PARTY_STASH}"
                         " ALTER TABLE sessions DROP COLUMN kind; ALTER TABLE sessions DROP COLUMN covers; PRAGMA user_version = 14;")
    s.conn.execute("INSERT INTO party_stash VALUES (?, ?, 'then')", (camp.id, json.dumps(
        {"items": [{"name": "Black pearl", "value_gp": 1000}], "currency": {"gp": 809, "pp": 56}})))
    s.conn.commit()
    s.close()

    s = Store(db)  # migrates
    inv = Inventory(s, camp)
    ch = s.character(cid)
    assert "inventory" not in ch["sheet"] and "currency" not in ch["sheet"]
    holder = inv.character_holder(cid)
    assert [i.name for i in inv.carried(holder)] == [i["name"] for i in old["inventory"]]
    assert inv.purse(holder)["sp"] == 14 and inv.sheet(ch).derived()["ac"]["total"] == 18  # the shield, now defined
    assert inv.holdings(None)[0]["name"] == "Black pearl" and inv.holdings(None)[0]["value_gp"] == 1000
    assert inv.purse(None) == {"cp": 0, "sp": 0, "ep": 0, "gp": 809, "pp": 56}
    assert not s.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'party_stash'").fetchone()
    s.close()


def test_items_are_world_items_by_their_own_name_or_when_someone_says_so(store):
    """The book's aliases are too loose to go by: "+1 arrows" names a quiver, "breastplate" the Breastplate of Mytros."""
    camp = store.create_campaign("Names", "dm")
    world = Inventory(store, camp).world
    run = world.start_run("manual")
    quiver = world.ensure_entity("Quiver of Ten +1 Arrows", "item")["id"]
    world.add_alias(quiver, "+1 Arrows", run)
    shield = world.ensure_entity("Shield of Volkan", "item")["id"]
    world.add_alias(shield, "lion shield", run)
    t = tools(store, "names")
    t["add_items"](items=[{"name": "+1 Arrows", "qty": 20}, {"name": "shield of volkan"},
                          {"name": "Shield w/ Lion Head", "world_item": "lion shield"}], reason="loot")
    items = json.loads(t["inventory"]())["holders"][0]["items"]
    assert [i.get("world_item") for i in items] == [None, "Shield of Volkan", "Shield of Volkan"]
    assert "Not done" in t["add_items"](items=[{"name": "Odd", "world_item": "Nothing like it"}], reason="x")
    assert t["link_item"](item_id=items[0]["id"], world_item="Quiver of Ten +1 Arrows") == "+1 Arrows is Quiver of Ten +1 Arrows."
    assert t["link_item"](item_id=items[0]["id"], world_item=None) == "+1 Arrows is a plain item now."
    assert "world_item" not in json.loads(t["inventory"]())["holders"][0]["items"][0]


def test_recaps_put_past_play_on_the_same_timeline(store):
    """Play before the app (or never recorded) is written up as recaps, and what happened is dated to them."""
    camp = store.create_campaign("Odyssey", "dm")
    t = tools(store, "odyssey")
    assert t["add_recap"](title="The call", summary="The boar hunt; the Oracle.", covers=3, played="2024") == \
        "Recap 1: The call (about 3 sessions)."
    assert t["add_recap"](title="Estoria", summary="The Great Labors.", covers=15) == "Recap 2: Estoria (about 15 sessions)."
    sessions = json.loads(t["list_sessions"]())
    assert [(s["number"], s["kind"], s["covers"]) for s in sessions] == [(1, "recap", 3), (2, "recap", 15)]

    # Writing up recap 1 after recap 2 exists: dated to it, and a value doesn't end one recorded later.
    t["record_fact"](entity="Versi", kind="npc", statement="Versi is in Estoria.", attribute="location",
                     value="Estoria", session=2)
    t["record_fact"](entity="Versi", statement="Versi is held at the Temple of the Oracle.", attribute="location",
                     value="the Temple of the Oracle", session=1)
    t["add_journal_entry"](text="The party freed Versi.", entities=["Versi"], session=1)
    from campaign.world import World
    world = World(store, camp)
    versi = world.resolve("Versi")["id"]
    assert world.view(versi)["current"] == {"location": "Estoria"}
    assert world.view(versi, as_of=1)["current"] == {"location": "the Temple of the Oracle"}
    assert world.events(session=1)[0]["text"] == "The party freed Versi."
    assert "no session 7" in t["add_journal_entry"](text="Later.", session=7)

    # Rewriting a recap; the next played session comes after them.
    assert t["add_recap"](title="The call", summary="The boar hunt, the Oracle, the prophecy.", number=1).startswith("Recap 1")
    assert "isn't a recap" in t["add_recap"](title="x", summary="y", number=5)
    assert store.next_ingest_session(camp) == 3
