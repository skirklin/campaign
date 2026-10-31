"""Read a character sheet from a scan or a filled-in PDF.

Claude reads the document and answers with the sheet as JSON in a fixed
shape (ScannedSheet, checked on arrival), which is then turned into a
CharacterSheet. The scan schema spells out abilities and coins as fields
rather than open-ended maps; anything Claude couldn't make out is listed
rather than guessed.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .charsheet import (
    ABILITIES, RETIRED_SHEET_FIELDS, SKILLS, ArmorClass, Attack, AttackOption, Bonus, CharacterSheet, ClassLevel,
    HitPoints, Item, Resource, Spell, Status, without,
)
from .tools import ToolContext

MODEL = os.environ.get("CAMPAIGN_SHEET_MODEL", os.environ.get("CAMPAIGN_MODEL", "claude-opus-5-5"))
MAX_BYTES = 20_000_000
MEDIA_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}

SYSTEM = """\
You read Dungeons & Dragons 5e character sheets: scans, photos or filled-in
PDFs, often handwritten, for the campaign "{name}". Copy what the sheet
says into the output fields.

- Record what's written. Don't compute values the sheet doesn't show, and
  don't invent anything. A blank field stays empty (0 for numbers that are
  blank, except ability scores: use 10 when an ability is unreadable).
- name is the character's name only; a nickname or "aka" goes in
  nicknames.
- Ability scores are the scores (e.g. 16), not the modifiers (+3).
- Proficiencies: list saving throws and skills marked as proficient. A
  doubled mark or "expertise" goes in skill_expertise as well.
- Classes: one entry per class with its level, e.g. Fighter 3 / Rogue 2.
- Resources are anything with uses: spell slots by level ("Spell slots
  (1st)"), hit dice, ki, rage, channel divinity, charges.
- Spells: name and level (cantrips are level 0); prepared if marked.
- Features and traits: one entry each, "Name: what it does" in a few
  words, e.g. "Rage: +2 melee damage, resist B/P/S, 3/long rest".
- Only real things become entries: a checkbox, a tally mark or a scribble
  isn't an item, a proficiency or a resource.
- Put what the sheet says that has no field of its own (backstory, allies,
  notes in the margins) in notes, and anything you couldn't read in
  unreadable: the field and what's unclear, a few words ("Wisdom score
  smudged", "third inventory line").

Every text field describes the character as they are, in the terse words a
player writes on a sheet. Never explain how you read the sheet, what you
looked up, or why a field is filled the way it is: that reasoning belongs
nowhere in the answer, not in notes, features, items or unreadable.

The tools search this campaign's reference material: the books and notes
the table plays from, which may include a setting's own subclasses,
species, feats, gifts and magic items. Use them (search_reference, then
read_reference or outline; lookup for things named in the campaign) when
the sheet names something you don't know or can't read with confidence:
an abbreviated, misspelt or smudged class, subclass, species, background,
feature, spell or item, or anything that isn't standard 5e. Find what it
stands for and write the full name: two letters beside a class are often
the initials of a subclass from the setting's book, and a letter that
reads one way may be another that matches a real option. When a magic
item or a weapon with special properties is named but the sheet doesn't
say what it does, look it up and put what it does in that item's notes
(and its attack's notes) in a few words, with a short citation such as
"(Odyssey p. 112)". Don't look up standard rules you already know, and don't
change anything the sheet says plainly. If a lookup finds nothing, keep
what the sheet says and list it in unreadable.

Record numbers as their parts, which are summed when the sheet is shown
(so they follow the character as it levels up); copy the totals the sheet
prints into the printed_ fields, which are only used to check your parts:
- Attacks: the ability used ("str", "dex", "finesse" for the better of the
  two, "spell" for the spellcasting ability, "none"), whether proficient,
  a magic weapon's +N as bonus, and damage as parts: the weapon's dice and
  type with add_ability true, then any extra dice (1d6 fire) as more parts.
  A part's type is only the damage type ("slashing"), never dice, numbers
  or words about when it applies. Weapon properties go in properties
  ("finesse", "versatile 1d10", "thrown 20/60", "reach", "heavy"). Extra
  damage that only applies sometimes is its own part with situational
  saying when, in a few words: a weapon's "+3d6 force vs giants", Sneak
  Attack (dice "(half_level:rogue)d6", situational "Sneak Attack"), a
  centaur's Charge. Dice that grow with level are written that way.
- Choices made before an attack roll are attack_options: Reckless Attack
  (applies_to ["str", "melee"], advantage "advantage", note "attacks
  against you have advantage until your next turn"), Great Weapon Master
  (["heavy", "melee"], to_hit -5, damage 10), Sharpshooter (["ranged",
  "weapon"], to_hit -5, damage 10). Record them there as well as in
  features.
- AC as a formula: base, the abilities that add to it and max_dex, e.g.
  chain mail is base 16 with no abilities; leather is 11 with dex; a
  barbarian's unarmored defense is 10 with dex and con. A shield or magic
  armor is a bonus to "ac" on that item.
- Anything else that adds to a number is a bonus: on the item that grants
  it (counts while equipped; set requires_attunement for items that need
  it), or in bonuses for features and effects. An item that raises an
  ability score is a "score:cha" bonus (or "score_set:str" for one that
  sets it, like Gauntlets of Ogre Power at 19). Amounts can reference the
  sheet: "prof", "level", "2*level", "cha".
- Statuses are what the character switches on in play: Rage for a
  barbarian (resource "Rage", spent when it starts, duration 10 rounds),
  and Concentration for anyone who casts concentration spells. A bonus that only counts
  during one names it in during: Rage's damage is a "damage:str" bonus
  during "Rage".
- hp.max is the printed maximum; the hit-dice part is worked out from it.
"""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Abilities(Model):
    strength: int
    dexterity: int
    constitution: int
    intelligence: int
    wisdom: int
    charisma: int


class Coins(Model):
    cp: int = 0
    sp: int = 0
    ep: int = 0
    gp: int = 0
    pp: int = 0


class ScannedHitPoints(Model):
    max: int
    current: int
    temp: int


class ScannedAttack(Attack):
    printed_to_hit: str  # as the sheet shows it, e.g. "+7"; "" if it doesn't
    printed_damage: str  # e.g. "1d12+4 slashing"


class ScannedSheet(Model):
    name: str
    nicknames: list[str]
    player: str
    species: str
    background: str
    alignment: str
    classes: list[ClassLevel]
    xp: int
    abilities: Abilities
    save_proficiencies: list[str]  # ability names or abbreviations
    skill_proficiencies: list[str]
    skill_expertise: list[str]
    ac: ArmorClass
    printed_ac: int  # 0 if the sheet doesn't show one
    speed: int
    hp: ScannedHitPoints
    attacks: list[ScannedAttack]
    bonuses: list[Bonus]
    statuses: list[Status]
    attack_options: list[AttackOption] = []  # optional: scans read before it existed have none
    resources: list[Resource]
    spellcasting_ability: str
    spells: list[Spell]
    inventory: list[Item]
    currency: Coins
    features: list[str]
    proficiencies: list[str]  # armor, weapons, tools, languages
    personality: str
    notes: str
    unreadable: list[str]

    @model_validator(mode="before")
    @classmethod
    def _retired(cls, data: Any) -> Any:
        return without(data, *RETIRED_SHEET_FIELDS)  # a scan stored while they existed still re-runs


# The answer's shape, spelled out for Claude (it's checked against ScannedSheet when it arrives).
# Braces are doubled because SYSTEM is a format string (the campaign's name).
SYSTEM += (
    "\nWhen you're done looking things up, answer with only a JSON object matching this JSON Schema,"
    " with no other text:\n"
    + json.dumps(ScannedSheet.model_json_schema(), separators=(",", ":")).replace("{", "{{").replace("}", "}}")
)

ABILITY_NAMES = dict(zip(("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"), ABILITIES))


def _ability(name: str) -> str | None:
    n = name.strip().lower()
    return n if n in ABILITIES else ABILITY_NAMES.get(n)


def _dice(text: str) -> list[str]:
    """The dice and modifiers in a damage string, to compare two: "1d12 + 4 slashing" -> ["1d12+4"]."""
    return re.findall(r"\d+d\d+(?:[+-]\d+)?", text.replace(" ", "").lower())


def to_sheet(scan: ScannedSheet) -> tuple[CharacterSheet, list[str]]:
    """The sheet, plus what couldn't be read or placed, or doesn't add up to what the sheet prints."""
    leftover = list(scan.unreadable)

    def known(names: list[str], valid: set[str], what: str) -> list[str]:
        out = []
        for n in names:
            key = n.strip().lower()
            if key in valid:
                out.append(key)
            else:
                leftover.append(f"{what}: {n}")
        return out

    saves = [a for a in (_ability(n) for n in scan.save_proficiencies) if a]
    abilities = {ABILITY_NAMES[k]: v for k, v in scan.abilities.model_dump().items()}
    attacks = [Attack.model_validate(a.model_dump(exclude={"printed_to_hit", "printed_damage"})) for a in scan.attacks]
    sheet = CharacterSheet(
        name=scan.name.strip() or "Unnamed character",
        player=scan.player, species=scan.species, background=scan.background, alignment=scan.alignment,
        classes=scan.classes, xp=scan.xp, abilities=abilities,
        save_proficiencies=saves,
        skill_proficiencies=known(scan.skill_proficiencies, set(SKILLS), "skill"),
        skill_expertise=known(scan.skill_expertise, set(SKILLS), "expertise"),
        ac=scan.ac, speed=scan.speed, hp=HitPoints(current=scan.hp.current, temp=scan.hp.temp),
        nicknames=scan.nicknames, attacks=attacks, bonuses=scan.bonuses, statuses=scan.statuses,
        attack_options=scan.attack_options,
        resources=scan.resources,
        spellcasting_ability=_ability(scan.spellcasting_ability) or "",
        spells=scan.spells, inventory=scan.inventory, currency=scan.currency.model_dump(),
        features=scan.features, proficiencies=scan.proficiencies,
        personality=scan.personality, notes=scan.notes,
    )
    # The hit-dice part is what's left of the printed maximum once CON and bonuses are taken off.
    sheet.hp = sheet.hp.model_copy(update={"base": scan.hp.max - sum(v for _, v in sheet.hp_max_parts())})

    # Check the parts against the totals the sheet prints.
    d = sheet.derived()
    if scan.printed_ac and scan.printed_ac != d["ac"]["total"]:
        leftover.append(f"AC: the sheet says {scan.printed_ac}, its parts add up to {d['ac']['total']}")
    for a, x in zip(scan.attacks, d["attacks"]):
        hit = x["to_hit"]["total"]
        if re.fullmatch(r"[+-]?\d+", a.printed_to_hit.replace(" ", "")) and int(a.printed_to_hit.replace(" ", "")) != hit:
            leftover.append(f"{a.name}: the sheet says {a.printed_to_hit} to hit, its parts add up to {hit:+d}")
        if _dice(a.printed_damage) and _dice(a.printed_damage) != _dice(x["text"]):
            leftover.append(f"{a.name}: the sheet says {a.printed_damage} damage, its parts add up to {x['text']}")
    return sheet, leftover


class ScanError(Exception):
    pass


def check_upload(data: bytes, filename: str) -> str:
    """The upload's media type, or ScanError if it isn't something Claude can read as a sheet."""
    media_type = MEDIA_TYPES.get(Path(filename).suffix.lower())
    if media_type is None:
        raise ScanError("upload a PDF or a photo (.png, .jpg, .webp) of the sheet")
    if len(data) > MAX_BYTES:
        raise ScanError("that file is too big; keep scans under 20 MB")
    return media_type


# Longest a short text field should be; longer means explanation crept in.
SHORT = {"attack notes": 160, "item notes": 300, "feature": 200, "unreadable entry": 100}


def _problems(scan: ScannedSheet) -> list[str]:
    """What breaks the answer's rules though it fits the schema: prose in a damage type, notes that explain."""
    out = []
    for a in scan.attacks:
        for d in a.damage:
            if re.search(r"\d", d.type) or len(d.type.split()) > 2:
                out.append(f"{a.name}: damage type {d.type!r} should be only the type; dice go in dice, "
                           "extra damage in its own part, conditions in the attack's notes")
        if len(a.notes) > SHORT["attack notes"]:
            out.append(f"{a.name}: notes are too long; say what it does in a few words")
    texts = [("item notes", i.name, i.notes) for i in scan.inventory] + \
        [("feature", f[:30], f) for f in scan.features] + [("unreadable entry", u[:30], u) for u in scan.unreadable]
    for kind, label, text in texts:
        if len(text) > SHORT[kind]:
            out.append(f"{kind} {label!r} is too long: describe what is, without explaining how you read it")
    return out


def _json_object(text: str) -> str:
    """The JSON object in a reply, without any fence or words around it."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def read_sheet(
    data: bytes, filename: str, ctx: ToolContext, model: str = MODEL,
    check: Callable[[], None] | None = None, on_thinking: Callable[[str], None] | None = None,
    on_tool: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[CharacterSheet, list[str]]:
    """Read the sheet, looking things up in the campaign `ctx` as needed (read-only).

    `check` is called as the answer streams in and before each lookup, and
    may raise to stop; `on_thinking` gets Claude's thinking as it goes, and
    `on_tool` each lookup, to show progress.

    The answer is plain JSON checked against ScannedSheet, not a constrained
    structured output: the sheet's schema is too big for the API to compile
    into a grammar. A reply that doesn't fit gets one chance to be corrected.
    """
    from .agent import Agent

    media_type = check_upload(data, filename)
    block_type = "document" if media_type == "application/pdf" else "image"
    source = {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode()}

    def thinking(text: str) -> None:
        if check:
            check()
        if on_thinking:
            on_thinking(text)

    def tool(name: str, args: dict[str, Any]) -> None:
        if check:
            check()
        if on_tool:
            on_tool(name, args)

    agent = Agent(
        store=ctx.store, ctx=ctx, system=SYSTEM.format(name=ctx.campaign.name), writable=False, model=model,
        on_text=lambda _: None, on_thinking=thinking, on_tool=tool,
    )
    message: Any = [
        {"type": block_type, "source": source},
        {"type": "text", "text": "Read this character sheet."},
    ]
    for attempt in range(2):
        text = agent.send(message, max_turns=20)
        if agent.stop_reason == "refusal":
            raise ScanError("Claude declined to read this file")
        if agent.stop_reason == "max_tokens":
            raise ScanError("the sheet's answer ran too long to finish")
        try:
            scan = ScannedSheet.model_validate_json(_json_object(text))
        except ValidationError as err:
            if attempt:
                raise ScanError(f"couldn't read a character sheet from this file ({err.error_count()} problems)") from err
            message = f"That doesn't match the schema:\n{err}\nReply again with only the corrected JSON object."
            continue
        problems = _problems(scan)
        if problems and not attempt:  # one chance to tidy up; after that, take it as it is
            message = ("That fits the schema, but fix these:\n- " + "\n- ".join(problems)
                       + "\nReply again with only the corrected JSON object.")
            continue
        return to_sheet(scan)
    raise AssertionError("unreachable")
