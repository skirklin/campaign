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


def test_the_tool_answers_briefly_and_hands_back_the_card(tmp_path):
    store, camp = setup()
    tools = {f.__name__: f for f in build_tools(ToolContext(store, camp))}
    got = json.loads(tools["encounter"](creatures=[{"name": "Skeleton", "count": 3}], party_size=4, party_level=4))
    assert got["creatures"] == [{"name": "Skeleton", "count": 3, "found": True, "ac": "13 (armor scraps)",
                                 "hp": "13 (2d8 + 4)", "cr": "1/4", "xp_each": 50}]
    assert got["difficulty"] == "trivial" and got["show"].startswith("```encounter\n{")
    spec = json.loads(got["show"].split("\n")[1])
    assert spec == {"creatures": [{"name": "Skeleton", "id": 1, "count": 3}], "party": {"size": 4, "level": 4}}
    assert "stat_block" not in json.dumps(got)  # the card shows them; the answer needn't

    # The card's endpoint builds the same spec, stat blocks and all.
    db = tmp_path / "c.db"
    s = Store(db)
    s.create_campaign("Chateau", "dm")
    src = s.add_source(s.campaign("chateau"), "Bestiary", "module", [{"heading": "", "text": "x", "page": 1}])
    World(s, s.campaign("chateau")).apply_book(src, [entry("Skeleton", block("13", "13 (2d8 + 4)", "1/4"))], key="k")
    s.close()
    with TestClient(create_app(db)) as client:
        r = client.post("/api/campaigns/chateau/encounter", json=spec).json()
    assert r["creatures"][0]["stat_block"]["armor_class"] == "13" and r["adjusted_xp"] == 300


def test_no_difficulty_without_a_known_level():
    store, camp = setup()  # no characters
    tools = {f.__name__: f for f in build_tools(ToolContext(store, camp))}
    got = json.loads(tools["encounter"](creatures=[{"name": "Skeleton", "count": 3}], party_size=4))
    assert "difficulty" not in got and got.get("party") is None
    assert "level" in got["no_difficulty"] and '"party"' not in got["show"]
    got = json.loads(tools["encounter"](creatures=[{"name": "Skeleton"}]))
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
