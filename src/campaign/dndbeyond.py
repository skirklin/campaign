"""Characters from D&D Beyond: a sheet mirrored from there, refreshed by hand.

D&D Beyond has no public API. Its character service answers
`GET https://character-service.dndbeyond.com/character/v5/character/{id}` with
the character as JSON, without signing in, when the character is set to
Public; otherwise its owner can open that link in their own logged-in
browser and paste what it shows. Nothing here signs in as anyone, polls, or
writes back.

D&D Beyond stays the source of truth for the sheet: `to_file` turns its JSON
into a CharacterFile (the sheet, what they carry, their coin), and a refresh
replaces the app's sheet with it. What the character carries is the party
inventory's business, so a refresh only says how it differs (`differences`)
and the table chooses what to bring in.

The JSON is undocumented and changes without notice, so everything is read
defensively, and what can't be placed is listed rather than guessed.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .charsheet import (
    ABILITIES, SKILLS, ArmorClass, Attack, Bonus, CharacterFile, ClassLevel, Damage, HitPoints, Item, Spell,
)

SERVICE = "https://character-service.dndbeyond.com/character/v5/character/{id}"
ABILITY_BY_ID = dict(enumerate(ABILITIES, start=1))
ABILITY_BY_NAME = {"strength": "str", "dexterity": "dex", "constitution": "con",
                   "intelligence": "int", "wisdom": "wis", "charisma": "cha"}
ALIGNMENTS = {1: "LG", 2: "NG", 3: "CG", 4: "LN", 5: "N", 6: "CN", 7: "LE", 8: "NE", 9: "CE"}
SOURCES = ("race", "class", "background", "feat", "condition", "item")  # data.modifiers' groups
ARMOR_LIGHT, ARMOR_MEDIUM, ARMOR_HEAVY, ARMOR_SHIELD = 1, 2, 3, 4


class DndBeyondError(Exception):
    """Why a character couldn't be read from D&D Beyond, said so a person can act on it."""


def character_id(link: str) -> str:
    """The character id in a D&D Beyond link ("…/characters/123456", "…/profile/x/characters/123456") or a bare id."""
    link = (link or "").strip()
    if re.fullmatch(r"\d+", link):
        return link
    m = re.search(r"dndbeyond\.com/(?:[^?#]*/)?characters?/(\d+)", link)
    if not m:
        raise DndBeyondError("that isn't a D&D Beyond character link (it looks like dndbeyond.com/characters/123456)")
    return m.group(1)


def service_url(link: str) -> str:
    """Where a character's data is, for its owner to open in their browser if it isn't public."""
    return SERVICE.format(id=character_id(link))


def fetch(link: str, timeout: float = 15) -> dict[str, Any]:
    """The character's data from D&D Beyond, if it's public."""
    req = urllib.request.Request(service_url(link), headers={"Accept": "application/json", "User-Agent": "campaign"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse(r.read())
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise DndBeyondError("D&D Beyond won't share that character: it isn't public. Set its privacy to Public"
                                 " in the sheet's settings, or paste its data instead.") from err
        if err.code == 404:
            raise DndBeyondError("D&D Beyond has no character with that id") from err
        raise DndBeyondError(f"D&D Beyond answered {err.code}; try again, or paste the character's data") from err
    except (urllib.error.URLError, TimeoutError) as err:
        raise DndBeyondError("couldn't reach D&D Beyond from the server; paste the character's data instead") from err


def parse(raw: bytes | str) -> dict[str, Any]:
    """The character in a D&D Beyond answer (or pasted text), unwrapped from its {"success", "data"} envelope."""
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError) as err:
        raise DndBeyondError("that isn't the character's data: paste everything the page shows, from { to }") from err
    data = doc.get("data", doc) if isinstance(doc, dict) else None
    if not isinstance(data, dict) or "stats" not in data or "name" not in data:
        raise DndBeyondError("that doesn't look like a D&D Beyond character")
    return data


# -- reading the JSON ----------------------------------------------------------------

def _list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _num(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _slug(text: str) -> str:
    return text.strip().lower().replace("-", " ")


def _modifiers(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every modifier with the group it's from, except items' (those belong to the item)."""
    mods = data.get("modifiers") or {}
    return [(group, m) for group in SOURCES if group != "item" for m in _list(mods.get(group)) if isinstance(m, dict)]


def _item_modifiers(data: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    """An item's own modifiers: on its definition, or in data.modifiers.item by componentId."""
    own = [m for m in _list((item.get("definition") or {}).get("grantedModifiers")) if isinstance(m, dict)]
    if own:
        return own
    ids = {item.get("definition", {}).get("id"), item.get("id")}
    return [m for m in _list((data.get("modifiers") or {}).get("item"))
            if isinstance(m, dict) and m.get("componentId") in ids]


def _bonus(m: dict[str, Any], source: str, leftover: list[str]) -> Bonus | None:
    """A D&D Beyond modifier as one of the sheet's bonuses, if it's one the sheet has."""
    kind, sub, value = m.get("type"), (m.get("subType") or ""), m.get("value")
    if value is None and m.get("fixedValue") is not None:
        value = m.get("fixedValue")
    if kind not in ("bonus", "set") or value is None:
        return None
    value = _num(value)
    targets = {"armor-class": "ac", "saving-throws": "save", "initiative": "initiative", "speed": "speed",
               "attack": "attack", "damage": "damage", "spell-save-dc": "spell_dc", "spell-attacks": "spell_attack",
               "ability-checks": "skill", "passive-perception": "passive_perception", "hit-points": "hp_max"}
    target = None
    if sub.endswith("-score") and sub[:-6] in ABILITY_BY_NAME:
        target = f"{'score_set' if kind == 'set' else 'score'}:{ABILITY_BY_NAME[sub[:-6]]}"
    elif kind == "bonus" and sub.endswith("-saving-throws") and sub[:-14] in ABILITY_BY_NAME:
        target = f"save:{ABILITY_BY_NAME[sub[:-14]]}"
    elif kind == "bonus" and _slug(sub) in SKILLS:
        target = f"skill:{_slug(sub)}"
    elif kind == "bonus":
        target = targets.get(sub)
    if target is None:
        if kind == "bonus" and sub not in ("magic", "unarmored-armor-class", "hit-points-per-level"):
            leftover.append(f"{source}: {m.get('friendlySubtypeName') or sub} {value:+d}")
        return None
    return Bonus(target=target, amount=value, source=source)


def to_file(data: dict[str, Any]) -> tuple[CharacterFile, list[str]]:
    """The character as a sheet file, plus what couldn't be placed on it."""
    leftover: list[str] = []
    mods = _modifiers(data)
    classes = [c for c in _list(data.get("classes")) if isinstance(c, dict)]
    level = sum(_num(c.get("level")) for c in classes) or 1

    # Ability scores: rolled, plus bonuses from race, class, background and feats; an override replaces.
    scores = {a: 10 for a in ABILITIES}
    for s in _list(data.get("stats")):
        if ABILITY_BY_ID.get(s.get("id")) and s.get("value") is not None:
            scores[ABILITY_BY_ID[s["id"]]] = _num(s["value"], 10)
    for s in _list(data.get("bonusStats")):
        if ABILITY_BY_ID.get(s.get("id")) and s.get("value"):
            scores[ABILITY_BY_ID[s["id"]]] += _num(s["value"])
    for _, m in mods:
        sub = m.get("subType") or ""
        if m.get("type") == "bonus" and sub.endswith("-score") and sub[:-6] in ABILITY_BY_NAME:
            scores[ABILITY_BY_NAME[sub[:-6]]] += _num(m.get("value"))
    for s in _list(data.get("overrideStats")):
        if ABILITY_BY_ID.get(s.get("id")) and s.get("value"):
            scores[ABILITY_BY_ID[s["id"]]] = _num(s["value"], 10)

    # Proficiencies, expertise, languages.
    saves, skills, expertise, profs = [], [], [], []
    for _, m in mods:
        kind, sub, label = m.get("type"), m.get("subType") or "", m.get("friendlySubtypeName") or ""
        if kind == "proficiency" and sub.endswith("-saving-throws") and sub[:-14] in ABILITY_BY_NAME:
            saves.append(ABILITY_BY_NAME[sub[:-14]])
        elif kind == "proficiency" and _slug(sub) in SKILLS:
            skills.append(_slug(sub))
        elif kind == "expertise" and _slug(sub) in SKILLS:
            expertise.append(_slug(sub))
        elif kind in ("proficiency", "language") and label:
            profs.append(label)

    # Bonuses from features, feats and the like (not items: those are the item's).
    bonuses = [b for group, m in mods if (b := _bonus(m, (m.get("friendlyTypeName") or group).strip(), leftover))
               and not b.target.startswith("score")]
    hp_per_level = sum(_num(m.get("value")) for _, m in mods
                       if m.get("type") == "bonus" and m.get("subType") == "hit-points-per-level")
    if hp_per_level:
        bonuses.append(Bonus(target="hp_max", amount=f"{hp_per_level}*level", source="Hit points per level"))

    # What they carry; equipped armor sets the AC formula, a shield and magic are bonuses on the item.
    inventory, attacks = [], []
    armor = None
    for raw in _list(data.get("inventory")):
        d = raw.get("definition") or {}
        if not isinstance(raw, dict) or not d.get("name"):
            continue
        item_bonuses = [b for m in _item_modifiers(data, raw) if (b := _bonus(m, d["name"], leftover))]
        armor_type = d.get("armorTypeId")
        if armor_type == ARMOR_SHIELD and d.get("armorClass"):
            item_bonuses.insert(0, Bonus(target="ac", amount=_num(d["armorClass"]), source=d["name"]))
        elif armor_type in (ARMOR_LIGHT, ARMOR_MEDIUM, ARMOR_HEAVY) and raw.get("equipped") and armor is None:
            armor = d
        cost = d.get("cost")
        inventory.append(Item(
            name=d["name"], qty=max(1, _num(raw.get("quantity"), 1)), equipped=bool(raw.get("equipped")),
            attuned=bool(raw.get("isAttuned")), requires_attunement=bool(d.get("canAttune")),
            bonuses=item_bonuses, value_gp=float(cost) if isinstance(cost, (int, float)) and cost > 0 else 0))
        if d.get("filterType") == "Weapon" and raw.get("equipped") and d.get("damage"):
            attacks.append(_attack(d, _item_modifiers(data, raw)))

    if armor is not None:
        kind = armor.get("armorTypeId")
        ac = ArmorClass(label=armor["name"], base=_num(armor.get("armorClass"), 10),
                        abilities=[] if kind == ARMOR_HEAVY else ["dex"], max_dex=2 if kind == ARMOR_MEDIUM else None)
    else:
        extra = [ABILITY_BY_ID[_num(m.get("statId"))] for _, m in mods
                 if m.get("type") == "set" and m.get("subType") == "unarmored-armor-class"
                 and ABILITY_BY_ID.get(_num(m.get("statId")))]
        ac = ArmorClass(label="Unarmored defense" if extra else "Unarmored", base=10, abilities=["dex", *extra])

    spell_ability = ""
    for c in classes:
        if (sid := (c.get("definition") or {}).get("spellCastingAbilityId")) and ABILITY_BY_ID.get(sid):
            spell_ability = ABILITY_BY_ID[sid]
            break
    spells = []
    for group in _list(data.get("classSpells")):
        for sp in _list(group.get("spells")):
            d = sp.get("definition") or {}
            if d.get("name"):
                spells.append(Spell(name=d["name"], level=min(9, max(0, _num(d.get("level")))),
                                    prepared=bool(sp.get("prepared") or sp.get("alwaysPrepared") or not d.get("level"))))
    for group in (data.get("spells") or {}).values():
        for sp in _list(group):
            d = sp.get("definition") or {}
            if d.get("name"):
                spells.append(Spell(name=d["name"], level=min(9, max(0, _num(d.get("level")))), notes="innate"))

    features = []
    for c in classes:
        for f in _list(c.get("classFeatures")):
            d = f.get("definition") or {}
            if d.get("name") and _num(d.get("requiredLevel"), 1) <= _num(c.get("level")):
                features.append(d["name"])
    for t in _list((data.get("race") or {}).get("racialTraits")):
        if (name := (t.get("definition") or {}).get("name")):
            features.append(name)
    for f in _list(data.get("feats")):
        if (name := (f.get("definition") or {}).get("name")):
            features.append(f"Feat: {name}")
    features = list(dict.fromkeys(n for n in features if n not in ("Ability Score Improvement", "Hit Points",
                                                                    "Proficiencies", "Equipment")))

    race = data.get("race") or {}
    speed = _num(((race.get("weightSpeeds") or {}).get("normal") or {}).get("walk"), 30)
    con = (scores["con"] - 10) // 2
    hp_base = _num(data.get("overrideHitPoints")) - con * level if data.get("overrideHitPoints") else \
        _num(data.get("baseHitPoints")) + _num(data.get("bonusHitPoints"))
    coins = data.get("currencies") or {}
    file = CharacterFile(
        name=str(data.get("name") or "Unnamed").strip(),
        player=str((data.get("username") or "")),
        species=str(race.get("fullName") or race.get("baseName") or ""),
        background=str(((data.get("background") or {}).get("definition") or {}).get("name") or ""),
        alignment=ALIGNMENTS.get(_num(data.get("alignmentId")), ""),
        classes=[ClassLevel(name=(c.get("definition") or {}).get("name") or "Class", level=min(20, max(1, _num(c.get("level"), 1))),
                            subclass=(c.get("subclassDefinition") or {}).get("name") or "") for c in classes],
        xp=_num(data.get("currentXp")),
        abilities=scores,
        save_proficiencies=sorted(set(saves)), skill_proficiencies=sorted(set(skills)),
        skill_expertise=sorted(set(expertise)),
        ac=ac, speed=speed,
        hp=HitPoints(base=max(0, hp_base), current=0, temp=_num(data.get("temporaryHitPoints"))),
        attacks=attacks, bonuses=bonuses,
        spellcasting_ability=spell_ability, spells=spells,
        inventory=inventory,
        currency={c: max(0, _num(coins.get(c))) for c in ("cp", "sp", "ep", "gp", "pp")},
        features=features, proficiencies=list(dict.fromkeys(profs)),
        personality="; ".join(str((data.get("traits") or {}).get(k) or "").strip()
                              for k in ("personalityTraits", "ideals", "bonds", "flaws")
                              if (data.get("traits") or {}).get(k)),
        notes="",
    ).worn()
    # Current HP is what's left of the maximum the sheet works out.
    file.hp = file.hp.model_copy(update={"current": max(0, file.hp_max - _num(data.get("removedHitPoints")))})
    if any(_list(c.get("classFeatures")) and _num(c.get("level")) for c in classes):
        leftover.append("limited uses (Rage, Channel Divinity, spell slots) aren't read from D&D Beyond: add them as resources")
    return file, list(dict.fromkeys(leftover))


def _attack(d: dict[str, Any], mods: list[dict[str, Any]]) -> Attack:
    props = [p.get("name", "").lower() for p in _list(d.get("properties")) if isinstance(p, dict)]
    ranged = d.get("attackType") == 2
    magic = sum(_num(m.get("value")) for m in mods if m.get("type") == "bonus" and m.get("subType") == "magic")
    rng = f"{d['range']}/{d['longRange']} ft" if ranged and d.get("longRange") else (
        f"{d.get('range') or 5} ft")
    dice = (d.get("damage") or {}).get("diceString") or ""
    return Attack(
        name=d["name"], ability="finesse" if "finesse" in props else "dex" if ranged else "str",
        bonus=magic, damage=[Damage(dice=dice if re.fullmatch(r"\d+d\d+", dice) else "",
                                    type=(d.get("damageType") or "").lower(), add_ability=True)],
        range=rng, properties=[p for p in props if p],
    )


def differences(file: CharacterFile, holdings: list[dict[str, Any]], purse: dict[str, int]) -> dict[str, Any]:
    """How what D&D Beyond says they carry differs from what the party's inventory says.

    Matched by name, ignoring case. `missing` is on D&D Beyond but not here (or
    fewer here), `extra` is here but not on D&D Beyond; `coin` is each coin's
    count on both sides where they differ.
    """
    def count(names: list[tuple[str, int]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, qty in names:
            out[name.strip().lower()] = out.get(name.strip().lower(), 0) + qty
        return out

    here = count([(h["name"], h["qty"]) for h in holdings])
    there = count([(i.name, i.qty) for i in file.inventory])
    missing, seen = [], set()
    for n, i in enumerate(file.inventory):
        key = i.name.strip().lower()
        if key not in seen and there[key] > here.get(key, 0):
            seen.add(key)
            missing.append({"index": n, "name": i.name, "qty": there[key] - here.get(key, 0)})
    extra = [{"name": h["name"], "qty": h["qty"]} for h in holdings if h["name"].strip().lower() not in there]
    coin = {c: {"dndbeyond": file.currency.get(c, 0), "here": purse.get(c, 0)}
            for c in ("cp", "sp", "ep", "gp", "pp") if file.currency.get(c, 0) != purse.get(c, 0)}
    return {"missing": missing, "extra": extra, "coin": coin}
