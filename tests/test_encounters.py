"""Encounters: stat blocks for a fight, its XP and its difficulty."""

import json

from fastapi.testclient import TestClient

from campaign import encounters
from campaign.db import Store
from campaign.tools import ToolContext, build_tools
from campaign.web import create_app
from campaign.world import World


def block(ac, hp, challenge, dex=10):
    return {"armor_class": ac, "hit_points": hp, "speed": "30 ft.", "strength": 10, "dexterity": dex,
            "constitution": 10, "intelligence": 10, "wisdom": 10, "charisma": 10, "challenge": challenge,
            "traits": [], "actions": [], "other": []}


def entry(name, sb, aliases=()):
    return {"name": name, "kind": "monster", "aliases": list(aliases), "summary": f"{name}.", "pages": [1],
            "facts": [], "stat_block": sb}


def setup():
    store = Store(":memory:")
    camp = store.create_campaign("Chateau", "dm")
    src = store.add_source(camp, "Bestiary", "module", [{"heading": "", "text": "x", "page": 1}])
    World(store, camp).apply_book(src, [
        entry("Skeleton", block("13 (armor scraps)", "13 (2d8 + 4)", "1/4 (50 XP)", dex=14)),
        entry("Violet Fungus", block("5", "18 (4d8)", "1/4"), aliases=["the flower"]),
        entry("Grinning Gourd", block("12", "66 (12d6 + 24)", "3")),
    ], key="k")
    return store, camp


def test_xp_and_multipliers_follow_the_dmg():
    assert encounters.challenge_xp("1/4 (50 XP)") == ("1/4", 50)
    assert encounters.challenge_xp("5 (1,800 XP; PB +3)") == ("5", 1800)
    assert encounters.challenge_xp("3") == ("3", 700)
    assert encounters.challenge_xp("") == (None, None)
    assert encounters.multiplier(1, 4) == 1 and encounters.multiplier(4, 4) == 2
    assert encounters.multiplier(4, 2) == 2.5 and encounters.multiplier(4, 6) == 1.5  # small and big parties
    assert encounters.multiplier(1, 6) == 0.5


def test_a_fight_comes_together_with_the_adventures_changes():
    store, camp = setup()
    spec = encounters.Spec(creatures=[
        {"name": "skeleton", "count": 3}, {"name": "Grinning Gourd"},
        {"name": "the flower", "hp": "45 (10d8)", "note": "vulnerable to fire"},
        {"name": "Bone Knight"},
    ], party={"size": 4, "level": 4})
    r = encounters.build(store, camp, spec)
    by = {c["asked_for"]: c for c in r["creatures"]}
    assert by["skeleton"]["name"] == "Skeleton" and by["skeleton"]["xp_each"] == 50
    assert by["the flower"]["name"] == "Violet Fungus" and by["the flower"]["hp"] == "45 (10d8)"
    assert by["the flower"]["hp_changed"] and by["the flower"]["note"] == "vulnerable to fire"
    assert not by["Bone Knight"]["found"] and not r["xp_complete"]
    # 3*50 + 700 + 50 = 900 XP for 6 monsters: x2 = 1800 adjusted; level 4 x4: hard 1500, deadly 2000.
    assert (r["xp"], r["monsters"], r["multiplier"], r["adjusted_xp"]) == (900, 6, 2, 1800)
    assert r["difficulty"] == "hard" and r["thresholds"]["deadly"] == 2000


def test_the_party_defaults_to_the_campaigns_characters():
    store, camp = setup()
    for name, level in (("Ana", 3), ("Bo", 5), ("Cy", 4)):
        store.create_character({"name": name, "classes": [{"name": "Fighter", "level": level}]}, camp.id)
    r = encounters.build(store, camp, encounters.Spec(creatures=[{"name": "Skeleton"}]))
    assert r["party"] == {"size": 3, "level": 4} and r["difficulty"] == "trivial"


def test_the_tool_saves_the_fight_and_hands_back_the_card(tmp_path):
    db = tmp_path / "c.db"
    store = Store(db)
    camp = store.create_campaign("Chateau", "dm")
    src = store.add_source(camp, "Bestiary", "module", [{"heading": "", "text": "x", "page": 1}])
    World(store, camp).apply_book(src, [entry("Skeleton", block("13 (armor scraps)", "13 (2d8 + 4)", "1/4 (50 XP)"))],
                                  key="k")
    conv = store.create_conversation(camp, None, "Crypt prep")
    tools = {f.__name__: f for f in build_tools(ToolContext(store, camp, conversation_id=conv))}
    got = json.loads(tools["encounter"](title="Crypt skeletons", creatures=[{"name": "Skeleton", "count": 3}],
                                        party_size=4, party_level=4))
    assert got["creatures"] == [{"name": "Skeleton", "count": 3, "found": True, "ac": "13 (armor scraps)",
                                 "hp": "13 (2d8 + 4)", "cr": "1/4", "xp_each": 50}]
    assert got["difficulty"] == "trivial" and "stat_block" not in json.dumps(got)  # the card shows them
    (saved,) = store.encounters(camp, None)
    assert got["show"] == "```encounter\n" + json.dumps({"id": saved["id"]}) + "\n```"
    assert saved["title"] == "Crypt skeletons" and saved["conversation_id"] == conv
    assert saved["spec"] == {"creatures": [{"name": "Skeleton", "id": 1, "count": 3}], "party": {"size": 4, "level": 4}}
    # The same fight shown again in that conversation is the same encounter, not another copy.
    tools["encounter"](title="Crypt skeletons", creatures=[{"name": "Skeleton", "count": 3}], party_size=4, party_level=4)
    assert len(store.encounters(camp, None)) == 1
    store.close()

    with TestClient(create_app(db)) as client:
        base = f"/api/campaigns/chateau/encounters/{saved['id']}"
        e = client.get(base).json()
        assert e["fight"]["creatures"][0]["stat_block"]["armor_class"] == "13 (armor scraps)"
        assert e["fight"]["adjusted_xp"] == 300 and e["state"] is None and e["conversation_title"] == "Crypt prep"
        (row,) = client.get("/api/campaigns/chateau/encounters").json()
        assert row["difficulty"] == "trivial" and row["creatures"] == [{"name": "Skeleton", "count": 3}]
        # Running it: the tracker keeps the fight's state on the server, for a reload or another device.
        state = {"round": 2, "turn": 1, "combatants": [{"key": "m0-1", "name": "Skeleton 1", "hp": 7}]}
        assert client.patch(base, json={"state": state}).json()["state"] == state
        assert client.get(base).json()["state"] == state
        assert client.patch(base, json={"title": "The crypt"}).json()["title"] == "The crypt"
        # Deleting the conversation it was made in keeps the encounter.
        assert client.delete(f"/api/campaigns/chateau/chats/{conv}").status_code == 204
        assert client.get("/api/campaigns/chateau/chats").json() == []
        e = client.get(base).json()
        assert e["conversation_id"] is None and e["state"] == state
        assert client.patch(base, json={"reset": True}).json()["state"] is None  # the fight ended
        assert client.delete(base).status_code == 204
        assert client.get(base).status_code == 404


def test_no_difficulty_without_a_known_level():
    store, camp = setup()  # no characters
    tools = {f.__name__: f for f in build_tools(ToolContext(store, camp))}
    got = json.loads(tools["encounter"](title="t", creatures=[{"name": "Skeleton", "count": 3}], party_size=4))
    assert "difficulty" not in got and got.get("party") is None
    assert "level" in got["no_difficulty"] and '"party"' not in got["show"]
    got = json.loads(tools["encounter"](title="t", creatures=[{"name": "Skeleton"}]))
    assert "size and level" in got["no_difficulty"]


def test_a_pinned_id_wins_over_the_name_and_the_book_text_is_at_hand():
    store = Store(":memory:")
    camp = store.create_campaign("Chateau", "dm")
    src = store.add_source(camp, "Bestiary", "module", [
        {"heading": "Skeleton", "text": "Skeleton. Armor Class 13 (armor scraps). Hit Points 13 (2d8 + 4).", "page": 1},
        {"heading": "Skeleton", "text": "Actions. Shortsword. +4 to hit.", "page": 1},
        {"heading": "Zombie", "text": "Zombie. Armor Class 8. Hit Points 22.", "page": 2},
    ])
    World(store, camp).apply_book(src, [
        entry("Skeleton", block("13 (armor scraps)", "13 (2d8 + 4)", "1/4")),
        entry("Zombie", block("8", "22 (3d8 + 9)", "1/4")),
    ], key="k")
    world = World(store, camp)
    zombie = world.resolve("Zombie")["id"]
    r = encounters.build(store, camp, encounters.Spec(creatures=[{"name": "Skeleton", "id": zombie}]))
    assert r["creatures"][0]["name"] == "Zombie"  # what was matched when the encounter was made
    r = encounters.build(store, camp, encounters.Spec(creatures=[{"name": "Skeleton", "id": 9999}]))
    assert r["creatures"][0]["name"] == "Skeleton"  # gone: back to the name

    passage = encounters.book_passage(store, camp, world.resolve("Skeleton")["id"])
    assert [p["text"][:9] for p in passage] == ["Skeleton.", "Actions. "]


def test_a_creature_runs_on_the_stat_block_the_book_says_it_uses():
    store = Store(":memory:")
    camp = store.create_campaign("Odyssey", "dm")
    src = store.add_source(camp, "Odyssey", "module", [{"heading": "", "text": "x", "page": 1}])
    World(store, camp).apply_book(src, [
        entry("Great Boar", block("14", "52 (7d10 + 14)", "3 (700 XP)")),
        entry("Scout", block("13 (leather armor)", "16 (3d8 + 3)", "1/2 (100 XP)")),
        {**entry("Corrupted White Boar", None, aliases=["the boar"]), "stat_block_of": "great boar",
         "facts": ["Gains one level of exhaustion at the end of each of its turns"]},
        {**entry("Taneias", None), "kind": "npc", "stat_block_of": "scouts"},  # as the book says it: plural
        {**entry("Kyrah", None), "kind": "npc"},
    ], key="k")
    world = World(store, camp)
    boar = world.resolve("Corrupted White Boar")
    assert world.lookup("the boar")[0]["name"] == "Corrupted White Boar"
    sb, source = world.stat_block(boar["id"])
    assert source["name"] == "Great Boar" and sb["hit_points"] == "52 (7d10 + 14)"
    assert world.stat_block(world.resolve("Taneias")["id"])[1]["name"] == "Scout"
    assert world.stat_block(world.resolve("Kyrah")["id"]) is None

    # The encounter: the creature the adventure names, run on the bestiary's block, with its XP.
    r = encounters.build(store, camp, encounters.Spec(creatures=[{"name": "the boar"}], party={"size": 4, "level": 1}))
    (c,) = r["creatures"]
    assert (c["name"], c["block_from"]["name"], c["xp_each"]) == ("Corrupted White Boar", "Great Boar", 700)
    assert encounters.summary(r)["creatures"][0]["stat_block_of"] == "Great Boar"
    # A card pinned to the creature when it had no block finds the one it uses now.
    r = encounters.build(store, camp, encounters.Spec(creatures=[{"name": "Corrupted Boar", "id": boar["id"]}]))
    assert r["creatures"][0]["found"] and r["creatures"][0]["name"] == "Corrupted White Boar"

    tools = {t.__name__: t for t in build_tools(ToolContext(store, camp))}
    text = tools["stat_block"]("Taneias")
    assert text.startswith("Taneias (Scout)\nAC 13 (leather armor) · HP 16 (3d8 + 3)")
    assert "no stat block" in tools["stat_block"]("Kyrah")


def test_a_name_finds_the_creature_with_the_stat_block_not_the_lore():
    """Odyssey's world has "Sydon", a piece of lore about the Titans, beside the Titan himself, filed
    as "Sydon, Lord of Storms" with "Sydon" among his aliases, and it's his block a fight wants."""
    store = Store(":memory:")
    camp = store.create_campaign("Odyssey", "dm")
    src = store.add_source(camp, "Odyssey", "module", [{"heading": "", "text": "x", "page": 1}])
    World(store, camp).apply_book(src, [
        {**entry("Sydon", None), "kind": "lore", "facts": ["The Titans are feared everywhere."]},
        {**entry("Sydon, Lord of Storms", block("22 (natural)", "313 (19d12 + 190)", "23 (50,000 XP)"),
                 aliases=["Sydon"]), "kind": "npc"},
        {**entry("Galimus", None), "kind": "npc", "stat_block_of": "Sydon"},  # a stand-in, run on the Titan's block
    ], key="k")
    world = World(store, camp)
    assert world.resolve("Sydon")["kind"] == "lore"  # names still mean what they did everywhere else
    assert world.creature("Sydon")["name"] == "Sydon, Lord of Storms"

    tools = {t.__name__: t for t in build_tools(ToolContext(store, camp))}
    assert tools["stat_block"]("Sydon").startswith("Sydon, Lord of Storms\nAC 22 (natural) · HP 313")
    (c,) = encounters.build(store, camp, encounters.Spec(creatures=[{"name": "Sydon"}]))["creatures"]
    assert c["name"] == "Sydon, Lord of Storms" and c["xp_each"] == 50000
    assert world.stat_block(world.resolve("Galimus")["id"])[1]["name"] == "Sydon, Lord of Storms"


def test_an_entity_page_shows_the_stat_block_it_runs_on(tmp_path):
    db = tmp_path / "c.db"
    store = Store(db)
    camp = store.create_campaign("Odyssey", "dm")
    src = store.add_source(camp, "Odyssey", "module", [{"heading": "", "text": "x", "page": 1}])
    World(store, camp).apply_book(src, [
        entry("Sea Hag", block("14 (natural armor)", "52 (7d8 + 21)", "2 (450 XP)")),
        {**entry("Heleka", None), "stat_block_of": "sea hag"},
    ], key="k")
    heleka, hag = (World(store, camp).resolve(n)["id"] for n in ("Heleka", "Sea Hag"))
    store.close()
    with TestClient(create_app(db)) as client:
        page = client.get(f"/api/campaigns/odyssey/entities/{heleka}").json()
        assert page["borrowed_stat_block"]["from"] == {"id": hag, "name": "Sea Hag"}
        assert page["borrowed_stat_block"]["stat_block"]["hit_points"] == "52 (7d8 + 21)"
        assert client.get(f"/api/campaigns/odyssey/entities/{hag}").json()["borrowed_stat_block"] is None  # its own


def test_the_party_comes_with_what_a_dm_wants_of_it_in_a_fight(tmp_path):
    db = tmp_path / "c.db"
    store = Store(db)
    camp = store.create_campaign("Odyssey", "dm")
    store.create_character({"name": "Cid", "abilities": {"dex": 14, "wis": 12}, "skill_proficiencies": ["perception"],
                            "classes": [{"name": "Barbarian", "level": 5}]}, camp.id)
    store.create_character(None, camp.id, name="Ione")  # her sheet is kept elsewhere: nothing to show
    store.close()
    with TestClient(create_app(db)) as client:
        cid, ione = client.get("/api/campaigns/odyssey/characters").json()
    assert cid["stats"] == {"ac": 12, "passive_perception": 14, "initiative": 2}  # 10 + Dex; 10 + Wis + proficiency
    assert ione["stats"] is None
