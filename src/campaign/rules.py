"""The rules of 5e things, as the SRD 5.1 words them: magic items, spells, weapons, armor and gear.

What a Potion of Healing heals, what a Scroll of Sending casts and at what DC,
that a javelin is 1d6 piercing and thrown 30/120. `about(name)` finds the
rules for a thing the way the table names it ("+1 Arrows", "Very nice dagger",
"Potion of Thunder Resistance", "Scroll of Raise Dead"), so a tap on an item
in the inventory says what it is and does. The book a campaign is built on
says who made the thing and why it matters; this says how it works.

The text is in srd_rules.json, built by scripts/srd_rules.py.
SRD 5.1 © Wizards of the Coast, CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("srd_rules.json")
LICENSE = "SRD 5.1 © Wizards of the Coast, CC BY 4.0"

# A spell scroll's save DC and attack bonus, by the spell's level (the SRD's Spell Scroll table).
SCROLL = {0: (13, 5), 1: (13, 5), 2: (13, 5), 3: (15, 7), 4: (15, 7), 5: (17, 9), 6: (17, 9), 7: (18, 10), 8: (18, 10), 9: (19, 11)}
ORDINAL = {0: "cantrip", 1: "1st", 2: "2nd", 3: "3rd"}

# The magic items whose name stands for a family: "Weapon, +1, +2, or +3" is a +2 longsword.
BONUS_ITEMS = {"weapon": "Weapon, +1, +2, or +3", "armor": "Armor, +1, +2, or +3",
               "ammunition": "Ammunition, +1, +2, or +3", "shield": "Shield, +1, +2, or +3"}
AMMUNITION = {"arrow", "bolt", "bullet", "needle", "ammunition"}


def key(name: str) -> str:
    """A name as it's matched: lower case, plain quotes, no punctuation or spaces ("Short sword" is the shortsword)."""
    name = name.lower().replace("’", "'").replace("'s ", " ")
    return re.sub(r"[^a-z0-9+]", "", name)


def singular(word: str) -> str:
    for end, to in (("ies", "y"), ("ves", "fe"), ("es", ""), ("s", "")):
        if word.endswith(end) and len(word) > len(end) + 2:
            stem = word[: -len(end)] + to
            if end != "es" or stem.endswith(("sh", "ch", "x", "ss")):
                return stem
    return word


# When names collide, the thing before the spell: "Shield" in a pack is the shield, not the spell.
PRIORITY = {"magic item": 0, "armor": 1, "weapon": 2, "gear": 3, "spell": 4}


@cache
def _data() -> tuple[dict[str, list[dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    """The entries by key (things before spells), and (key, entry) for weapons, armor and magic items,
    longest first, for "a very nice dagger"."""
    entries = sorted(json.loads(PATH.read_text())["entries"], key=lambda e: PRIORITY[e["kind"]])
    by: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        names = [e["name"], re.sub(r"\s*\(.*?\)", "", e["name"])]  # "Arrows (20)" is arrows
        if e["kind"] == "armor" and e["name"] != "Shield":
            names.append(f"{e['name']} armor")  # "studded leather armor"
        if e["kind"] == "weapon" and "," in e["name"]:
            a, b = e["name"].split(",", 1)
            names.append(f"{b} {a}")  # "Crossbow, light" is a light crossbow
        for k in dict.fromkeys(key(n) for n in names):
            by.setdefault(k, []).append(e)
    nouns = sorted(((k, es[0]) for k, es in by.items() if es[0]["kind"] in ("weapon", "armor", "magic item") and len(k) > 3),
                   key=lambda ke: -len(ke[0]))
    return by, nouns


def label(e: dict[str, Any]) -> str:
    """How the rules name a thing in a sentence: "leather armor", "a +1 weapon"'s "greataxe"."""
    name = re.sub(r"\s*\(.*?\)", "", e["name"].lower() if e["kind"] != "magic item" else e["name"])
    bare = e["kind"] == "armor" and not re.search(r"(mail|shirt|breastplate|half plate|shield)$", name)
    return f"{name} armor" if bare else name


def _entry(e: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"name": e["name"], "kind": e["kind"], "subtitle": e["subtitle"], "text": e["text"], **extra}


def find(name: str, kind: str | None = None) -> dict[str, Any] | None:
    """The entry called this (give or take case, plurals and punctuation), of `kind` if given.
    A spell can be named as it's used: "Water Walking" is Water Walk."""
    by, _ = _data()
    words = name.split()
    tries = [name] + ([" ".join(words[:-1] + [singular(words[-1])]), " ".join(singular(w) for w in words)] if words else [])
    for n in tries:
        if e := next((e for e in by.get(key(n), []) if kind in (None, e["kind"])), None):
            return e
    if kind == "spell" and len(k := key(name)) > 5:
        return next((es[0] for kk, es in by.items() if es[0]["kind"] == "spell" and k.startswith(kk) and len(k) - len(kk) <= 3), None)
    return None


def about(name: str) -> list[dict[str, Any]]:
    """The rules for a thing as the table names it, most specific first; [] if the SRD doesn't have it.

    Each is an entry (name, kind, subtitle, markdown text) with `note` saying how
    it applies when it isn't the thing itself: what a scroll casts it at, that
    "+1 Arrows" are magic arrows, that "a very nice dagger" is a dagger.
    """
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return []
    out: list[dict[str, Any]] = []
    plain = re.sub(r"\s*\(.*?\)$", "", name)  # "Shield and Breastplate (Mithral)"

    # A spell scroll: the spell, at the scroll's DC, then how scrolls work.
    if m := re.match(r"(?i)^(?:spell\s+)?scrolls?\s+(?:of\s+)?(?:the\s+)?(.+)$", plain) or re.match(r"(?i)^(.+?)\s+scrolls?$", plain):
        if spell := find(m.group(1), "spell"):
            level = spell.get("level", 0)
            dc, attack = SCROLL[level]
            lvl = ORDINAL.get(level, f"{level}th")
            out.append(_entry(spell, note=f"From a spell scroll ({lvl}{'' if level == 0 else ' level'}): save DC {dc}, +{attack} to hit."))
            out.append(_entry(find("Spell Scroll")))
            return out

    # "Potion of Thunder Resistance", "Ring of Resistance (Fire)": the item, with its damage type.
    if m := re.match(r"(?i)^(potion|ring) of (?:(\w+) resistance|resistance(?: \((\w+)\))?)$", name):
        e = find(f"{m.group(1)} of Resistance")
        kind = (m.group(2) or m.group(3) or "").lower()
        return [_entry(e, note=f"Resistance to {kind} damage." if kind else None)] if e else []

    if e := find(plain) or find(name):
        return [_entry(e)]

    # "+1 Arrows", "Dragon Greataxe +1", "Longsword, +2": the +N family, then what it's made from.
    if m := re.search(r"(?:^|\s|,\s*)\+([123])(?:\s|$|,)", plain):
        n = m.group(1)
        rest = (plain[: m.start()] + " " + plain[m.end():]).replace(",", " ").strip()
        base = (_base(rest) or find(rest)) if rest else None
        kind = "ammunition" if base and singular(base["name"].split()[0].lower()) in AMMUNITION or any(
            singular(w.lower()) in AMMUNITION for w in rest.split()) else \
            "shield" if base and base["name"] == "Shield" or "shield" in rest.lower() else \
            "armor" if base and base["kind"] == "armor" else "weapon"
        out.append(_entry(find(BONUS_ITEMS[kind]), note=f"{'A ' if kind in ('weapon', 'shield') else ''}+{n} {kind}: +{n} to {'AC' if kind in ('armor', 'shield') else 'attack and damage rolls'}."))
        if base:
            out.append(_entry(base, note=f"What it's made from: {label(base)}."))
        return out

    # "Very nice dagger", "Javelins of Adamantium", "Shield of Volkan": what it is underneath.
    made = re.match(r"(?i)^(.+?) of ", plain)
    if base := _base(plain) or (made and _base(made.group(1), plain_only=True)):
        said = label(base)
        a = "a " if base["kind"] == "weapon" or re.search(r"(shield|breastplate|shirt)$", said) else ""
        return [_entry(base, note=f"In the rules, {a}{said}.")]
    return []


def _base(name: str, plain_only: bool = False) -> dict[str, Any] | None:
    """The weapon, armor or magic item a name ends with: "Silver-tipped Spear", "Cid's Cloak of Protection".
    `plain_only`: weapons and armor only (what "Trident of Adamantium" is made as)."""
    _, nouns = _data()
    if plain_only:
        nouns = [(k, e) for k, e in nouns if e["kind"] != "magic item"]
    words = name.split()
    tails = {key(" ".join(words[i:])) for i in range(len(words))}
    tails |= {key(" ".join(words[i:-1] + [singular(words[-1])])) for i in range(len(words))} if words else set()
    return next((e for k, e in nouns if k in tails), None)


def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Entries whose name has all the query's words, then any whose text does: for the assistant's rules lookup."""
    entries = [e for es in _data()[0].values() for e in es]
    words = [key(w) for w in query.split() if key(w)]
    seen: dict[str, dict[str, Any]] = {}
    for e in entries:
        if all(w in key(e["name"]) for w in words):
            seen.setdefault(e["name"], e)
    for e in entries:
        if len(seen) >= limit:
            break
        if all(w in key(e["text"]) for w in words):
            seen.setdefault(e["name"], e)
    return [_entry(e) for e in list(seen.values())[:limit]]
