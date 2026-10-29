"""Read a character sheet from a scan or a filled-in PDF.

Claude reads the document and returns the sheet in a fixed shape
(structured output), which is then turned into a CharacterSheet. The scan
schema spells out abilities and coins as fields, since structured outputs
can't take open-ended maps; anything Claude couldn't make out is listed
rather than guessed.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Callable

import anthropic
from pydantic import BaseModel, ConfigDict

from .charsheet import ABILITIES, SKILLS, Attack, CharacterSheet, ClassLevel, HitPoints, Item, Resource, Spell
from .digest import fallback_params

MODEL = os.environ.get("CAMPAIGN_SHEET_MODEL", os.environ.get("CAMPAIGN_MODEL", "claude-opus-5-5"))
MAX_BYTES = 20_000_000
MEDIA_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}

SYSTEM = """\
You read Dungeons & Dragons 5e character sheets: scans, photos or filled-in
PDFs, often handwritten. Copy what the sheet says into the output fields.

- Record what's written. Don't compute values the sheet doesn't show, and
  don't invent anything. A blank field stays empty (0 for numbers that are
  blank, except ability scores: use 10 when an ability is unreadable).
- Ability scores are the scores (e.g. 16), not the modifiers (+3).
- Proficiencies: list saving throws and skills marked as proficient. A
  doubled mark or "expertise" goes in skill_expertise as well.
- Classes: one entry per class with its level, e.g. Fighter 3 / Rogue 2.
- Resources are anything with uses: spell slots by level ("Spell slots
  (1st)"), hit dice, ki, rage, channel divinity, charges.
- Spells: name and level (cantrips are level 0); prepared if marked.
- Features and traits: one entry each, keep the name and a short summary.
- Put anything that doesn't fit a field (backstory, allies, notes in the
  margins) in notes, and anything you couldn't read in unreadable, naming
  the field (e.g. "Wisdom score", "third inventory line").
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


class ScannedSheet(Model):
    name: str
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
    ac: int
    speed: int
    hp: HitPoints
    attacks: list[Attack]
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


ABILITY_NAMES = dict(zip(("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"), ABILITIES))


def _ability(name: str) -> str | None:
    n = name.strip().lower()
    return n if n in ABILITIES else ABILITY_NAMES.get(n)


def to_sheet(scan: ScannedSheet) -> tuple[CharacterSheet, list[str]]:
    """The sheet, plus what couldn't be read or placed."""
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
    sheet = CharacterSheet(
        name=scan.name.strip() or "Unnamed character",
        player=scan.player, species=scan.species, background=scan.background, alignment=scan.alignment,
        classes=scan.classes, xp=scan.xp, abilities=abilities,
        save_proficiencies=saves,
        skill_proficiencies=known(scan.skill_proficiencies, set(SKILLS), "skill"),
        skill_expertise=known(scan.skill_expertise, set(SKILLS), "expertise"),
        ac=scan.ac, speed=scan.speed, hp=scan.hp,
        attacks=scan.attacks, resources=scan.resources,
        spellcasting_ability=_ability(scan.spellcasting_ability) or "",
        spells=scan.spells, inventory=scan.inventory, currency=scan.currency.model_dump(),
        features=scan.features, proficiencies=scan.proficiencies,
        personality=scan.personality, notes=scan.notes,
    )
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


def read_sheet(
    data: bytes, filename: str, client: Any = None, model: str = MODEL,
    check: Callable[[], None] | None = None, on_thinking: Callable[[str], None] | None = None,
) -> tuple[CharacterSheet, list[str]]:
    """Read the sheet. `check` is called as the answer streams in and may raise to stop;
    `on_thinking` gets Claude's thinking as it goes, to show progress."""
    media_type = check_upload(data, filename)
    block_type = "document" if media_type == "application/pdf" else "image"
    source = {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode()}
    client = client or anthropic.Anthropic()
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        system=SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": block_type, "source": source},
            {"type": "text", "text": "Read this character sheet."},
        ]}],
        output_format=ScannedSheet,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        **fallback_params(model),
    ) as stream:
        for event in stream:
            if check:
                check()
            delta = getattr(event, "delta", None)
            if on_thinking and getattr(delta, "type", None) == "thinking_delta":
                on_thinking(delta.thinking)
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise ScanError("Claude declined to read this file")
    if msg.stop_reason == "max_tokens" or msg.parsed_output is None:
        raise ScanError("couldn't read a character sheet from this file")
    return to_sheet(msg.parsed_output)
