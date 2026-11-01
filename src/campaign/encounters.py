"""Encounters: a fight's worth of creatures, with their stat blocks and how hard it is.

An encounter is a small spec, the creatures and how many of each, plus
optional changes the adventure makes to them ("45 HP, vulnerable to fire")
and the party it's for. `build` turns a spec into everything needed to run
it: each creature's stat block from the world, its XP, and the encounter's
difficulty for the party by the 5e (2014) Dungeon Master's Guide rules:
total XP, times a multiplier for the number of monsters, against the
party's thresholds.

The chat assistant makes the spec with the `encounter` tool and puts it in
an answer as a ```encounter block; the app renders that block by building
the spec again, so the answer stays short and the card is always current.
"""

from __future__ import annotations

import json
import re
from fractions import Fraction
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .charsheet import CharacterSheet
from .db import Store
from .world import World


class Creature(BaseModel):
    name: str
    id: int | None = None  # the world entity it was matched to, so a re-render shows the same one
    count: int = Field(default=1, ge=1, le=50)
    hp: str | None = None  # the adventure's own hit points, e.g. "45 (10d8)"
    note: str = ""  # anything else the adventure changes: "vulnerable to fire"


class Party(BaseModel):
    size: int = Field(ge=1, le=12)
    level: int = Field(ge=1, le=20)


class Spec(BaseModel):
    creatures: list[Creature] = Field(min_length=1, max_length=20)
    party: Party | None = None


XP_BY_CR = {
    "0": 10, "1/8": 25, "1/4": 50, "1/2": 100, "1": 200, "2": 450, "3": 700, "4": 1100, "5": 1800,
    "6": 2300, "7": 2900, "8": 3900, "9": 5000, "10": 5900, "11": 7200, "12": 8400, "13": 10000,
    "14": 11500, "15": 13000, "16": 15000, "17": 18000, "18": 20000, "19": 22000, "20": 25000,
    "21": 33000, "22": 41000, "23": 50000, "24": 62000, "25": 75000, "26": 90000, "27": 105000,
    "28": 120000, "29": 135000, "30": 155000,
}
# Per character: easy, medium, hard, deadly (DMG 2014, p. 82).
THRESHOLDS = {
    1: (25, 50, 75, 100), 2: (50, 100, 150, 200), 3: (75, 150, 225, 400), 4: (125, 250, 375, 500),
    5: (250, 500, 750, 1100), 6: (300, 600, 900, 1400), 7: (350, 750, 1100, 1700),
    8: (450, 900, 1400, 2100), 9: (550, 1100, 1600, 2400), 10: (600, 1200, 1900, 2800),
    11: (800, 1600, 2400, 3600), 12: (1000, 2000, 3000, 4500), 13: (1100, 2200, 3400, 5100),
    14: (1250, 2500, 3800, 5700), 15: (1400, 2800, 4300, 6400), 16: (1600, 3200, 4800, 7200),
    17: (2000, 3900, 5900, 8800), 18: (2100, 4200, 6300, 9500), 19: (2400, 4900, 7300, 10900),
    20: (2800, 5700, 8500, 12700),
}
MULTIPLIERS = [0.5, 1, 1.5, 2, 2.5, 3, 4, 5]  # index 1 is one monster; parties of 1-2 / 6+ shift by one


def _multiplier_index(monsters: int) -> int:
    for limit, i in ((1, 1), (2, 2), (6, 3), (10, 4), (14, 5)):
        if monsters <= limit:
            return i
    return 6


def multiplier(monsters: int, party_size: int) -> float:
    i = _multiplier_index(monsters)
    if party_size < 3:
        i += 1
    elif party_size >= 6:
        i -= 1
    return MULTIPLIERS[i]


def challenge_xp(challenge: str) -> tuple[str | None, int | None]:
    """("1/4", 50) from "1/4 (50 XP)", "1/4", "CR 5 (1,800 XP; PB +3)"; (None, None) if there's no CR."""
    text = str(challenge or "")
    cr = re.search(r"(\d+/\d+|\d+)", text)
    xp = re.search(r"([\d,]+)\s*XP", text)
    rating = cr.group(1) if cr else None
    if rating and "/" not in rating and rating not in XP_BY_CR:
        rating = None
    if xp:
        return rating, int(xp.group(1).replace(",", ""))
    if rating:
        return rating, XP_BY_CR.get(rating) or XP_BY_CR.get(str(Fraction(rating)))
    return None, None


def party_of(store: Store, campaign: Any) -> Party | None:
    """The campaign's party from its character sheets: how many, and their average level."""
    levels = []
    for c in store.characters([campaign.id]):
        if c["campaign_id"] != campaign.id:
            continue
        try:
            levels.append(CharacterSheet.model_validate(c["sheet"]).level)
        except ValidationError:
            continue
    levels = [lv for lv in levels if lv]
    if not levels:
        return None
    return Party(size=len(levels), level=max(1, min(20, round(sum(levels) / len(levels)))))


def find_stat_block(world: World, name: str, entity_id: int | None = None) -> tuple[int | None, str, dict[str, Any] | None]:
    """The entity's id, name and stat block (the latest, if the book gives several), or None.

    By `entity_id` when given (and still there), else by name or alias.
    """
    view = world.view(entity_id) if entity_id else None
    if view is None:
        e = world.resolve(name)
        view = world.view(e["id"]) if e else None
    if view is None:
        hits = world.lookup(name, limit=1)
        view = hits[0] if hits else None
    if view is None:
        return None, name, None
    blocks = world.attribute_values(view["id"], "stat_block")
    return view["id"], view["name"], json.loads(blocks[-1]) if blocks else None


def book_passage(store: Store, campaign: Any, entity_id: int) -> list[dict[str, Any]]:
    """The book's own text for an entity's stat block: the chunk that best matches its name in the
    source the stat block came from, and the one after (stat blocks often run on). To check the
    stat block against, not to replace it."""
    world = World(store, campaign)
    e = world.entity(entity_id)
    if e is None:
        return []
    facts = [f for f in world.facts(entity_id) if f["attribute"] == "stat_block" and f["source_id"]]
    source = store.source(facts[-1]["source_id"]) if facts else None
    if source is None:
        return []
    hits = store.search(campaign, f"{e['name']} armor class hit points", limit=5, source=source["title"])
    if not hits:
        return []
    name = e["name"].lower()
    best = next((h for h in hits if name in (h["heading"] or "").lower()), hits[0])
    return store.read_chunks(campaign, best["id"], after=1)


def build(store: Store, campaign: Any, spec: Spec) -> dict[str, Any]:
    world = World(store, campaign)
    party = spec.party or party_of(store, campaign)
    creatures = []
    for c in spec.creatures:
        entity_id, name, sb = find_stat_block(world, c.name, c.id)
        rating, xp = challenge_xp(sb.get("challenge", "")) if sb else (None, None)
        creatures.append({
            "asked_for": c.name, "id": entity_id, "name": name, "count": c.count, "found": sb is not None,
            "stat_block": sb, "hp": c.hp or (sb or {}).get("hit_points"), "hp_changed": bool(c.hp),
            "note": c.note, "cr": rating, "xp_each": xp,
        })
    monsters = sum(c["count"] for c in creatures)
    known = [c for c in creatures if c["xp_each"] is not None]
    xp = sum(c["xp_each"] * c["count"] for c in known)
    out: dict[str, Any] = {
        "creatures": creatures, "monsters": monsters, "xp": xp,
        "xp_complete": len(known) == len(creatures), "party": party.model_dump() if party else None,
    }
    if party:
        m = multiplier(monsters, party.size)
        adjusted = round(xp * m)
        easy, medium, hard, deadly = (t * party.size for t in THRESHOLDS[party.level])
        difficulty = ("deadly" if adjusted >= deadly else "hard" if adjusted >= hard else
                      "medium" if adjusted >= medium else "easy" if adjusted >= easy else "trivial")
        out |= {"multiplier": m, "adjusted_xp": adjusted, "difficulty": difficulty,
                "thresholds": {"easy": easy, "medium": medium, "hard": hard, "deadly": deadly},
                "xp_per_character": xp // party.size}
    return out


def summary(result: dict[str, Any]) -> dict[str, Any]:
    """What the assistant needs to talk about an encounter, without the stat blocks themselves."""
    rows = []
    for c in result["creatures"]:
        sb = c["stat_block"] or {}
        rows.append({k: v for k, v in {
            "name": c["name"], "count": c["count"], "found": c["found"],
            "asked_for": c["asked_for"] if c["asked_for"].lower() != c["name"].lower() else None,
            "ac": sb.get("armor_class"), "hp": c["hp"], "cr": c["cr"], "xp_each": c["xp_each"],
            "note": c["note"] or None,
        }.items() if v is not None})
    keep = ("monsters", "xp", "xp_complete", "party", "multiplier", "adjusted_xp", "difficulty", "thresholds",
            "xp_per_character")
    return {"creatures": rows} | {k: result[k] for k in keep if k in result}
