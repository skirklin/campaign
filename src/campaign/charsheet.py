"""A D&D 5e character sheet.

Fields cover what's on the paper sheet; anything else (homebrew resources,
custom trackers) can go in `extra`. Derived numbers (modifiers, proficiency
bonus, skill totals) are computed rather than stored so they can't drift.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ABILITIES = ("str", "dex", "con", "int", "wis", "cha")

SKILLS = {
    "acrobatics": "dex",
    "animal handling": "wis",
    "arcana": "int",
    "athletics": "str",
    "deception": "cha",
    "history": "int",
    "insight": "wis",
    "intimidation": "cha",
    "investigation": "int",
    "medicine": "wis",
    "nature": "int",
    "perception": "wis",
    "performance": "cha",
    "persuasion": "cha",
    "religion": "int",
    "sleight of hand": "dex",
    "stealth": "dex",
    "survival": "wis",
}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ClassLevel(Model):
    name: str
    level: int = Field(ge=1, le=20)
    subclass: str = ""


class HitPoints(Model):
    max: int = 0
    current: int = 0
    temp: int = 0


class Item(Model):
    name: str
    qty: int = 1
    equipped: bool = False
    attuned: bool = False
    notes: str = ""


class Attack(Model):
    name: str
    to_hit: str = ""  # "+7"
    damage: str = ""  # "1d8+4 slashing"
    notes: str = ""


class Resource(Model):
    """Anything with uses: spell slots, ki, rage, hit dice, a wand's charges."""

    name: str
    max: int
    used: int = 0
    recharge: str = ""  # "long rest", "short rest", "dawn"


class Spell(Model):
    name: str
    level: int = Field(ge=0, le=9)
    prepared: bool = True
    notes: str = ""


class CharacterSheet(Model):
    name: str
    player: str = ""
    species: str = ""
    background: str = ""
    alignment: str = ""
    classes: list[ClassLevel] = []
    xp: int = 0
    abilities: dict[str, int] = Field(default_factory=lambda: {a: 10 for a in ABILITIES})
    save_proficiencies: list[str] = []
    skill_proficiencies: list[str] = []
    skill_expertise: list[str] = []
    ac: int = 10
    speed: int = 30
    hp: HitPoints = HitPoints()
    conditions: list[str] = []
    death_saves: dict[str, int] = Field(default_factory=lambda: {"successes": 0, "failures": 0})
    attacks: list[Attack] = []
    resources: list[Resource] = []
    spellcasting_ability: str = ""
    spells: list[Spell] = []
    inventory: list[Item] = []
    currency: dict[str, int] = Field(
        default_factory=lambda: {"cp": 0, "sp": 0, "ep": 0, "gp": 0, "pp": 0}
    )
    features: list[str] = []
    proficiencies: list[str] = []  # armor, weapons, tools, languages
    personality: str = ""
    notes: str = ""
    extra: dict[str, Any] = {}

    @property
    def level(self) -> int:
        return sum(c.level for c in self.classes)

    @property
    def proficiency_bonus(self) -> int:
        return 2 + (max(self.level, 1) - 1) // 4

    def mod(self, ability: str) -> int:
        return (self.abilities.get(ability, 10) - 10) // 2

    def save(self, ability: str) -> int:
        bonus = self.proficiency_bonus if ability in self.save_proficiencies else 0
        return self.mod(ability) + bonus

    def skill(self, skill: str) -> int:
        total = self.mod(SKILLS[skill])
        if skill in self.skill_expertise:
            total += 2 * self.proficiency_bonus
        elif skill in self.skill_proficiencies:
            total += self.proficiency_bonus
        return total

    def derived(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "level": self.level,
            "proficiency_bonus": self.proficiency_bonus,
            "modifiers": {a: self.mod(a) for a in ABILITIES},
            "saves": {a: self.save(a) for a in ABILITIES},
            "skills": {s: self.skill(s) for s in SKILLS},
            "passive_perception": 10 + self.skill("perception"),
            "initiative": self.mod("dex"),
        }
        if self.spellcasting_ability:
            m = self.mod(self.spellcasting_ability) + self.proficiency_bonus
            out["spell_save_dc"] = 8 + m
            out["spell_attack"] = m
        return out


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396 JSON merge patch: objects merge recursively, null deletes, anything else replaces."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            result.pop(k, None)
        else:
            result[k] = merge_patch(result.get(k), v)
    return result


def render(sheet: CharacterSheet) -> str:
    """Plain-text sheet for the terminal or for pasting into a chat."""
    d = sheet.derived()

    def sign(n: int) -> str:
        return f"+{n}" if n >= 0 else str(n)

    classes = " / ".join(
        f"{c.name}{f' ({c.subclass})' if c.subclass else ''} {c.level}" for c in sheet.classes
    )
    lines = [
        f"{sheet.name} — {sheet.species} {classes}".strip(),
        f"Level {d['level']}  Prof {sign(d['proficiency_bonus'])}  AC {sheet.ac}  "
        f"HP {sheet.hp.current}/{sheet.hp.max}" + (f" (+{sheet.hp.temp} temp)" if sheet.hp.temp else "")
        + f"  Speed {sheet.speed}  Init {sign(d['initiative'])}  PP {d['passive_perception']}",
        "  ".join(
            f"{a.upper()} {sheet.abilities.get(a, 10)} ({sign(d['modifiers'][a])})" for a in ABILITIES
        ),
        "Saves: "
        + ", ".join(
            f"{a.upper()}{'*' if a in sheet.save_proficiencies else ''} {sign(d['saves'][a])}"
            for a in ABILITIES
        ),
        "Skills: "
        + ", ".join(
            f"{s} {sign(d['skills'][s])}"
            for s in SKILLS
            if s in sheet.skill_proficiencies or s in sheet.skill_expertise
        ),
    ]
    if sheet.conditions:
        lines.append("Conditions: " + ", ".join(sheet.conditions))
    if sheet.attacks:
        lines.append("Attacks: " + "; ".join(f"{a.name} {a.to_hit} {a.damage}".strip() for a in sheet.attacks))
    if sheet.resources:
        lines.append(
            "Resources: "
            + "; ".join(f"{r.name} {r.max - r.used}/{r.max}" for r in sheet.resources)
        )
    if sheet.spells:
        if "spell_save_dc" in d:
            lines.append(f"Spell DC {d['spell_save_dc']}  Spell attack {sign(d['spell_attack'])}")
        by_level: dict[int, list[str]] = {}
        for s in sheet.spells:
            by_level.setdefault(s.level, []).append(s.name + ("" if s.prepared else " (unprepared)"))
        for lvl in sorted(by_level):
            lines.append(f"  {'Cantrips' if lvl == 0 else f'Level {lvl}'}: {', '.join(by_level[lvl])}")
    if sheet.inventory:
        lines.append(
            "Inventory: "
            + ", ".join(
                f"{i.name}{f' x{i.qty}' if i.qty != 1 else ''}{' (E)' if i.equipped else ''}"
                for i in sheet.inventory
            )
        )
    coins = ", ".join(f"{v} {k}" for k, v in sheet.currency.items() if v)
    if coins:
        lines.append("Coin: " + coins)
    if sheet.features:
        lines.append("Features: " + "; ".join(sheet.features))
    if sheet.notes:
        lines.append("Notes: " + sheet.notes)
    return "\n".join(lines)
