"""A D&D 5e character sheet.

Fields cover what's on the paper sheet; anything else (homebrew resources,
custom trackers) can go in `extra`. Derived numbers (modifiers, proficiency
bonus, skill totals) are computed rather than stored so they can't drift.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

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


# Numbers on the sheet are built from parts and summed when shown, so they
# follow the character: raise Strength and every Strength attack, check and
# save goes up; level up and everything proficient does. An amount is a
# number, or a reference to something on the sheet ("str".."cha" for that
# ability's modifier, "prof", "half_prof", "level"), optionally times a whole
# number ("2*level") or negated ("-dex").
# "level:rogue" and "half_level:rogue" count one class's levels (half rounds up,
# as Sneak Attack's dice do); without a class, the character's whole level.
REFS = (*ABILITIES, "prof", "half_prof", "level", "half_level")
_AMOUNT = re.compile(r"^(?:(-?\d+)\*)?(-?)([a-z_]+)(?::([a-z][a-z' -]*))?$")


def _check_amount(v: int | str) -> int | str:
    if isinstance(v, str):
        m = _AMOUNT.match(v.strip().lower())
        if not m or m.group(3) not in REFS or (m.group(4) and m.group(3) not in ("level", "half_level")):
            try:
                return int(v)
            except ValueError:
                raise ValueError(f"{v!r} isn't a number or one of {', '.join(REFS)} "
                                 "(like 2*level, -dex or half_level:rogue)") from None
        return v.strip().lower()
    return v


Amount = Annotated[int | str, AfterValidator(_check_amount)]

# Where a bonus applies. "attack:str" and "damage:str" apply to attacks made
# with Strength; "check:dex" to every Dexterity check (its skills, initiative).
BONUS_TARGETS = (
    "ac", "initiative", "speed", "hp_max", "attack", *(f"attack:{a}" for a in ABILITIES),
    "damage", *(f"damage:{a}" for a in ABILITIES), "save", *(f"save:{a}" for a in ABILITIES),
    "skill", *(f"skill:{k}" for k in SKILLS), *(f"check:{a}" for a in ABILITIES),
    "passive_perception", "spell_dc", "spell_attack",
    # Ability scores themselves: score: adds to it (+1 CHA), score_set: raises it to at least the amount
    # (Gauntlets of Ogre Power, STR 19). Everything built on the score follows.
    *(f"score:{a}" for a in ABILITIES), *(f"score_set:{a}" for a in ABILITIES),
)


class Bonus(Model):
    """A modifier from a feature, an item or an effect, added wherever `target` is worked out."""

    target: Literal[BONUS_TARGETS]  # type: ignore[valid-type]
    amount: Amount = 0
    source: str = ""  # shown in breakdowns: "Ring of protection", "Rage"
    active: bool = True  # switched off by hand
    during: str = ""  # a status's name: counts only while it's on ("Rage" for Rage's damage)


class Status(Model):
    """Something the character switches on and off in play: Rage, Concentration, Bless, Dodge.

    Bonuses that name it in `during` count only while it's on. Switching it on
    spends a use of `resource` (Rage spends a Rage). A rest ends it.
    """

    name: str
    active: bool = False
    note: str = ""  # what it's on: the spell being concentrated on
    resource: str = ""  # a resource's name, spent when switched on
    duration: int = Field(default=0, ge=0)  # rounds it lasts (Rage, Bless: 10), shown in combat; 0 if open-ended

    @model_validator(mode="before")
    @classmethod
    def _retired(cls, data: Any) -> Any:
        return without(data, "uses")  # what starting it took in a turn, since dropped


class HitPoints(Model):
    base: int = 0  # from hit dice (rolled or averaged), without Constitution: max adds CON x level
    current: int = 0
    temp: int = 0


class ArmorClass(Model):
    """AC = base + the listed abilities' modifiers (Dexterity capped at max_dex) + bonuses.

    Unarmored: base 10 with dex. Leather: 11 with dex. Half plate: 15 with dex,
    max_dex 2. Plate: 18, no abilities. Barbarian unarmored defense: 10 with
    dex and con. Shields and magic armor are item bonuses to "ac".
    """

    label: str = ""  # "Chain mail", "Unarmored defense"
    base: int = 10
    abilities: list[str] = ["dex"]
    max_dex: int | None = None


class Item(Model):
    name: str
    qty: int = 1
    equipped: bool = False
    attuned: bool = False
    requires_attunement: bool = False
    bonuses: list[Bonus] = []  # count while equipped (and attuned, if it needs it)
    notes: str = ""

    @property
    def active(self) -> bool:
        return self.equipped and (self.attuned or not self.requires_attunement)


_DICE = re.compile(r"^\s*(?:(\d+)|\((.+)\))\s*d\s*(\d+)\s*$")


def _check_dice(v: str) -> str:
    m = _DICE.match(v)
    if v.strip() and not m:
        raise ValueError(f"{v!r} isn't dice like 1d8, or (half_level:rogue)d6")
    if m and m.group(2):
        _check_amount(m.group(2))
    return v.strip()


class Damage(Model):
    # "1d12", or dice whose count follows the sheet: "(half_level:rogue)d6"; empty for a flat amount
    dice: Annotated[str, AfterValidator(_check_dice)] = ""
    type: str = ""  # "slashing", "fire"
    add_ability: bool = False  # a weapon's main damage adds the attack's ability modifier
    bonus: int = 0
    # Extra damage that only applies sometimes ("Sneak Attack", "vs giants & titans", "Charge"):
    # left out of the attack's damage, and added when rolling if it applies.
    situational: str = ""
    spends: str = ""  # a resource's name: adding this situational damage spends a use (Divine Smite, a slot)


ATTACK_ABILITIES = (*ABILITIES, "finesse", "spell", "none")


class Attack(Model):
    """To hit = ability modifier + proficiency (if proficient) + bonus + attack bonuses.

    `ability` is an ability, "finesse" (the better of Strength and Dexterity),
    "spell" (the spellcasting ability) or "none". `bonus` is a magic weapon's
    +N: it adds to the hit and to the main damage. `hit_bonus` adds to the
    hit only.
    """

    name: str
    ability: Literal[ATTACK_ABILITIES] = "str"  # type: ignore[valid-type]
    proficient: bool = True
    bonus: int = 0
    hit_bonus: int = 0
    damage: list[Damage] = []
    range: str = ""  # "5 ft", "20/60 ft"
    properties: list[str] = []  # "finesse", "versatile 1d10", "thrown 20/60", "reach"
    notes: str = ""  # what it does beyond its numbers, in a few words

    @model_validator(mode="before")
    @classmethod
    def _retired(cls, data: Any) -> Any:
        return without(data, "uses")  # the action it took, since dropped


class AttackOption(Model):
    """A choice made when attacking, before the roll: Reckless Attack, Great Weapon Master's -5/+10.

    `applies_to` narrows it to some attacks, every entry matching: the ability
    the attack uses ("str"), "melee" or "ranged", "weapon" or "spell", or a
    weapon property ("heavy"). Empty means every attack. `note` is shown when it's chosen, for
    what it costs: "attacks against you have advantage until your next turn".
    """

    name: str
    applies_to: list[str] = []
    advantage: Literal["", "advantage", "disadvantage"] = ""
    to_hit: Amount = 0
    damage: Amount = 0  # added to the attack's main damage
    note: str = ""


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


_DAMAGE_PART = re.compile(r"^\s*(\d+d\d+)?\s*([+-]?\s*\d+)?\s*(.*?)\s*$")


def parse_damage(text: str) -> list[dict[str, Any]]:
    """Free-text damage ("1d8+4 slashing + 1d6 fire") as flat parts, for sheets from before parts."""
    parts = []
    for piece in re.split(r"\s*\+\s*(?=\d+d\d+)", text.strip()):
        m = _DAMAGE_PART.match(piece)
        if piece and m:
            dice, bonus, kind = m.groups()
            parts.append({"dice": dice or "", "bonus": int(bonus.replace(" ", "")) if bonus else 0, "type": kind})
    return parts


# Fields sheets had briefly, for tracking actions in a turn, since dropped: read and ignored.
RETIRED_SHEET_FIELDS = ("attacks_per_action", "grapple_bonus_action")


def without(data: Any, *keys: str) -> Any:
    """`data` without `keys`, if it's a dict: for reading what older sheets saved."""
    return {k: v for k, v in data.items() if k not in keys} if isinstance(data, dict) else data


def _upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """Sheets saved before numbers had parts: keep every number as it was.

    A flat AC becomes a formula with that base and no abilities; HP max becomes
    the hit-dice total less CON x level; a free-text attack becomes one with
    its numbers as flat bonuses. They follow the character once edited.
    """
    data = without(data, *RETIRED_SHEET_FIELDS)
    if isinstance(data.get("ac"), int):
        data["ac"] = {"base": data["ac"], "abilities": []}
    hp = data.get("hp")
    if isinstance(hp, dict) and "max" in hp:
        con = (int(data.get("abilities", {}).get("con", 10)) - 10) // 2
        level = sum(int(c.get("level", 0)) for c in data.get("classes", []) if isinstance(c, dict))
        hp = {k: v for k, v in hp.items() if k != "max"}
        data["hp"] = hp | {"base": int(data["hp"]["max"]) - con * level}
    attacks = []
    for a in data.get("attacks", []):
        if isinstance(a, dict) and (isinstance(a.get("to_hit"), str) or isinstance(a.get("damage"), str)):
            a = dict(a)
            to_hit = str(a.pop("to_hit", "") or "").replace(" ", "")
            a.update(ability="none", proficient=False,
                     hit_bonus=int(to_hit) if re.fullmatch(r"[+-]?\d+", to_hit) else 0)
            if isinstance(a.get("damage"), str):
                a["damage"] = parse_damage(a["damage"])
        attacks.append(a)
    if "attacks" in data:
        data["attacks"] = attacks
    return data


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
    ac: ArmorClass = ArmorClass()
    speed: int = 30
    hp: HitPoints = HitPoints()
    conditions: list[str] = []
    death_saves: dict[str, int] = Field(default_factory=lambda: {"successes": 0, "failures": 0})
    nicknames: list[str] = []  # other names the table uses; the world entry knows them as aliases
    attacks: list[Attack] = []
    bonuses: list[Bonus] = []  # from features and effects; items carry their own
    statuses: list[Status] = []  # switched on and off in play: Rage, Concentration…
    attack_options: list[AttackOption] = []  # chosen per attack: Reckless Attack, Great Weapon Master
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

    @model_validator(mode="before")
    @classmethod
    def _from_older_sheets(cls, data: Any) -> Any:
        return _upgrade(data) if isinstance(data, dict) else data

    @property
    def level(self) -> int:
        return sum(c.level for c in self.classes)

    @property
    def proficiency_bonus(self) -> int:
        return 2 + (max(self.level, 1) - 1) // 4

    def class_level(self, name: str) -> int:
        return sum(c.level for c in self.classes if c.name.strip().lower() == name.strip().lower())

    def score_parts(self, ability: str) -> list[tuple[str, int]]:
        """An ability score: the sheet's, plus bonuses to it, or raised to what an item sets it to."""
        raw = self.abilities.get(ability, 10)
        parts = [("Base", raw)]
        # Amounts here use the unmodified scores, so a score's bonus can't depend on itself.
        for b in self.active_bonuses():
            if b.target == f"score:{ability}":
                parts.append((b.source or b.target, self.amount(b.amount, raw=True)))
        total = sum(v for _, v in parts)
        for b in self.active_bonuses():
            if b.target == f"score_set:{ability}" and self.amount(b.amount, raw=True) > total:
                parts = [(b.source or b.target, self.amount(b.amount, raw=True))]
                total = parts[0][1]
        return parts

    def score(self, ability: str) -> int:
        return sum(v for _, v in self.score_parts(ability))

    def mod(self, ability: str, raw: bool = False) -> int:
        return ((self.abilities.get(ability, 10) if raw else self.score(ability)) - 10) // 2

    def amount(self, amount: int | str, raw: bool = False) -> int:
        """What an amount comes to on this sheet now."""
        if isinstance(amount, int):
            return amount
        m = _AMOUNT.match(amount)
        times, negate, ref, of_class = int(m.group(1) or 1), m.group(2), m.group(3), m.group(4)
        pb = self.proficiency_bonus
        level = self.class_level(of_class) if of_class else self.level
        value = {"prof": pb, "half_prof": pb // 2, "level": level, "half_level": (level + 1) // 2}.get(ref)
        value = self.mod(ref, raw) if value is None else value
        return -times * value if negate else times * value

    def dice(self, dice: str) -> str:
        """Dice with their count worked out: "(half_level:rogue)d6" at rogue 7 is "4d6"."""
        m = _DICE.match(dice)
        if not m or not m.group(2):
            return dice.replace(" ", "")
        return f"{max(0, self.amount(m.group(2).strip().lower()))}d{m.group(3)}"

    @property
    def active_statuses(self) -> set[str]:
        return {st.name.strip().lower() for st in self.statuses if st.active}

    def active_bonuses(self) -> list[Bonus]:
        """Bonuses that count now: switched on, their status (if any) on, and from the sheet
        or from an item that's equipped (and attuned, where it needs to be)."""
        statuses = self.active_statuses

        def on(b: Bonus) -> bool:
            return b.active and (not b.during or b.during.strip().lower() in statuses)

        out = [b for b in self.bonuses if on(b)]
        for item in self.inventory:
            if item.active:
                out += [b.model_copy(update={"source": b.source or item.name}) for b in item.bonuses if on(b)]
        return out

    def _bonuses(self, *targets: str) -> list[tuple[str, int]]:
        return [(b.source or b.target, self.amount(b.amount)) for b in self.active_bonuses() if b.target in targets]

    def save_parts(self, ability: str) -> list[tuple[str, int]]:
        parts = [(ability.upper(), self.mod(ability))]
        if ability in self.save_proficiencies:
            parts.append(("Proficiency", self.proficiency_bonus))
        return parts + self._bonuses("save", f"save:{ability}")

    def skill_parts(self, skill: str) -> list[tuple[str, int]]:
        ability = SKILLS[skill]
        parts = [(ability.upper(), self.mod(ability))]
        if skill in self.skill_expertise:
            parts.append(("Expertise", 2 * self.proficiency_bonus))
        elif skill in self.skill_proficiencies:
            parts.append(("Proficiency", self.proficiency_bonus))
        return parts + self._bonuses("skill", f"skill:{skill}", f"check:{ability}")

    def save(self, ability: str) -> int:
        return sum(v for _, v in self.save_parts(ability))

    def skill(self, skill: str) -> int:
        return sum(v for _, v in self.skill_parts(skill))

    def ac_parts(self) -> list[tuple[str, int]]:
        ac = self.ac
        parts = [(ac.label or "Base", ac.base)]
        for a in ac.abilities:
            m = self.mod(a)
            if a == "dex" and ac.max_dex is not None and m > ac.max_dex:
                parts.append((f"DEX (max {ac.max_dex})", ac.max_dex))
            else:
                parts.append((a.upper(), m))
        return parts + self._bonuses("ac")

    def hp_max_parts(self) -> list[tuple[str, int]]:
        return [("Hit dice", self.hp.base), (f"CON x {self.level}", self.mod("con") * self.level),
                *self._bonuses("hp_max")]

    @property
    def hp_max(self) -> int:
        return max(0, sum(v for _, v in self.hp_max_parts()))

    @staticmethod
    def reach(attack: Attack) -> set[str]:
        """Melee, ranged or both: ammunition is ranged, thrown is both, else by range (over 10 ft is ranged)."""
        props = {p.split()[0].lower() for p in attack.properties if p.strip()}
        feet = re.match(r"\s*(\d+)", attack.range)
        far = "/" in attack.range or (feet is not None and int(feet.group(1)) > 10)
        if "ammunition" in props:
            return {"ranged"}
        if "thrown" in props:
            return {"melee", "ranged"}
        return {"ranged"} if far else {"melee"}

    def option_applies(self, option: AttackOption, attack: Attack) -> bool:
        props = {p.split()[0].lower() for p in attack.properties if p.strip()}
        for want in (w.strip().lower() for w in option.applies_to if w.strip()):
            if want in ABILITIES:
                if self.attack_ability(attack) != want:
                    return False
            elif want in ("melee", "ranged"):
                if want not in self.reach(attack):
                    return False
            elif want in ("weapon", "spell"):
                if (attack.ability == "spell") != (want == "spell"):
                    return False
            elif want not in props:
                return False
        return True

    def attack_ability(self, attack: Attack) -> str | None:
        """The ability an attack uses, resolving finesse and spell; None for "none"."""
        if attack.ability == "finesse":
            return "str" if self.mod("str") >= self.mod("dex") else "dex"
        if attack.ability == "spell":
            return self.spellcasting_ability or None
        return None if attack.ability == "none" else attack.ability

    def attack_parts(self, attack: Attack) -> dict[str, Any]:
        ability = self.attack_ability(attack)
        hit = [(ability.upper(), self.mod(ability))] if ability else []
        if attack.proficient:
            hit.append(("Proficiency", self.proficiency_bonus))
        if attack.bonus:
            hit.append(("Magic", attack.bonus))
        if attack.hit_bonus:
            hit.append(("Bonus", attack.hit_bonus))
        hit += self._bonuses("attack", *([f"attack:{ability}"] if ability else []))
        damage = []
        # Ability, magic and damage bonuses go on the main part: the one adding the ability, else the
        # first that always applies.
        main = next((i for i, d in enumerate(attack.damage) if d.add_ability),
                    next((i for i, d in enumerate(attack.damage) if not d.situational), 0))
        for i, d in enumerate(attack.damage):
            parts = [("Bonus", d.bonus)] if d.bonus else []
            if i == main:
                if d.add_ability and ability:
                    parts.insert(0, (ability.upper(), self.mod(ability)))
                if attack.bonus:
                    parts.append(("Magic", attack.bonus))
                parts += self._bonuses("damage", *([f"damage:{ability}"] if ability else []))
            damage.append({"dice": self.dice(d.dice), "type": d.type, "bonus": _total(parts),
                           "situational": d.situational, "spends": d.spends})
        options = [i for i, o in enumerate(self.attack_options) if self.option_applies(o, attack)]
        return {"to_hit": _total(hit), "damage": damage, "text": _damage_text(damage), "options": options}

    def derived(self) -> dict[str, Any]:
        """Every computed number, as {"total", "parts": [{"label", "value"}]} so it can be explained."""
        perception = self.skill("perception")
        out: dict[str, Any] = {
            "level": self.level,
            "proficiency_bonus": self.proficiency_bonus,
            "scores": {a: _total(self.score_parts(a)) for a in ABILITIES},
            "modifiers": {a: self.mod(a) for a in ABILITIES},
            "saves": {a: _total(self.save_parts(a)) for a in ABILITIES},
            "skills": {k: _total(self.skill_parts(k)) for k in SKILLS},
            "passive_perception": _total([("Base", 10), ("Perception", perception),
                                          *self._bonuses("passive_perception")]),
            "initiative": _total([("DEX", self.mod("dex")), *self._bonuses("initiative", "check:dex")]),
            "ac": _total(self.ac_parts()),
            "speed": _total([("Base", self.speed), *self._bonuses("speed")]),
            "hp_max": _total(self.hp_max_parts()),
            "attacks": [self.attack_parts(a) for a in self.attacks],
            # What each option comes to now; derived.attacks[i].options says which apply to that attack.
            "attack_options": [{"to_hit": self.amount(o.to_hit), "damage": self.amount(o.damage)}
                               for o in self.attack_options],
        }
        if self.spellcasting_ability:
            a = self.spellcasting_ability
            core = [(a.upper(), self.mod(a)), ("Proficiency", self.proficiency_bonus)]
            out["spell_save_dc"] = _total([("Base", 8), *core, *self._bonuses("spell_dc")])
            out["spell_attack"] = _total([*core, *self._bonuses("spell_attack")])
        return out


def _total(parts: list[tuple[str, int]]) -> dict[str, Any]:
    return {"total": sum(v for _, v in parts), "parts": [{"label": k, "value": v} for k, v in parts if v or k == "Base"]}


def _damage_text(damage: list[dict[str, Any]]) -> str:
    """ "1d12+6 slashing (vs giants & titans +3d6 force)": what always applies, then what sometimes does."""

    def one(d: dict[str, Any]) -> str:
        n = d["bonus"]["total"]
        amount = (d["dice"] + (f"{n:+d}" if n else "")) if d["dice"] else str(n)
        return f"{amount} {d['type']}".strip()

    text = " + ".join(one(d) for d in damage if not d.get("situational"))
    extra = [f"{d['situational']} +{one(d)}" for d in damage if d.get("situational")]
    return " ".join([text, *(f"({e})" for e in extra)]).strip()


class PartyStash(Model):
    """What the party holds in common: loot nobody has claimed, the shared purse.

    Items are whole items, bonuses and all, so a magic item put in and taken
    out again still works; nobody wears what's in the stash, so it comes in
    unequipped and unattuned.
    """

    items: list[Item] = []
    currency: dict[str, int] = Field(
        default_factory=lambda: {"cp": 0, "sp": 0, "ep": 0, "gp": 0, "pp": 0}
    )


def move(
    sheet: CharacterSheet, stash: PartyStash, to: str,
    items: dict[str, int] | None = None, currency: dict[str, int] | None = None,
) -> str:
    """Move items (by name) and coin between a character and the party stash.

    `to` is "character" (taking from the stash) or "stash" (putting in).
    Items stack by name, case-insensitively. Raises ValueError, changing
    nothing, if the giver doesn't have enough. Returns what moved, in words.
    """
    if to not in ("character", "stash"):
        raise ValueError("to must be 'character' or 'stash'")
    src_items, dst_items = (stash.items, sheet.inventory) if to == "character" else (sheet.inventory, stash.items)
    src_coin, dst_coin = (stash.currency, sheet.currency) if to == "character" else (sheet.currency, stash.currency)
    src_items = [i.model_copy() for i in src_items]
    dst_items = [i.model_copy() for i in dst_items]
    src_coin, dst_coin = dict(src_coin), dict(dst_coin)
    moved = []

    def find(lst: list[Any], name: str) -> Any:
        return next((i for i in lst if i.name.strip().lower() == name.strip().lower()), None)

    for name, qty in (items or {}).items():
        if qty <= 0:
            continue
        have = find(src_items, name)
        if have is None or have.qty < qty:
            raise ValueError(f"only {have.qty if have else 0} × {name} to give")
        have.qty -= qty
        if have.qty == 0:
            src_items.remove(have)
        if (there := find(dst_items, name)) is not None:
            there.qty += qty
        else:
            stowed = {"equipped": False, "attuned": False} if to == "stash" else {}
            dst_items.append(have.model_copy(update={"qty": qty, **stowed}))
        moved.append(f"{have.name}{f' ×{qty}' if qty != 1 else ''}")
    for coin, n in (currency or {}).items():
        if n <= 0:
            continue
        if src_coin.get(coin, 0) < n:
            raise ValueError(f"only {src_coin.get(coin, 0)} {coin} to give")
        src_coin[coin] -= n
        dst_coin[coin] = dst_coin.get(coin, 0) + n
        moved.append(f"{n} {coin}")
    if not moved:
        raise ValueError("nothing to move")
    if to == "character":
        stash.items, sheet.inventory, stash.currency, sheet.currency = src_items, dst_items, src_coin, dst_coin
    else:
        sheet.inventory, stash.items, sheet.currency, stash.currency = src_items, dst_items, src_coin, dst_coin
    return ", ".join(moved)


def apply_hp(sheet: CharacterSheet, delta: int) -> None:
    """Damage (negative) comes off temp HP first; healing (positive) caps at max."""
    hp = sheet.hp.model_copy()
    if delta < 0:
        absorbed = min(hp.temp, -delta)
        hp.temp -= absorbed
        hp.current = max(0, hp.current + delta + absorbed)
    else:
        hp.current = min(sheet.hp_max, hp.current + delta)
    sheet.hp = hp


def rest(sheet: CharacterSheet, long: bool) -> None:
    """Reset resources that recharge on this rest and end statuses; a long rest also restores HP."""
    kinds = {"short rest"} | ({"long rest", "dawn"} if long else set())
    sheet.resources = [
        r.model_copy(update={"used": 0}) if r.recharge in kinds else r for r in sheet.resources
    ]
    sheet.statuses = [st.model_copy(update={"active": False}) for st in sheet.statuses]  # an hour or more: all over
    if long:
        sheet.hp = sheet.hp.model_copy(update={"current": sheet.hp_max, "temp": 0})
        sheet.death_saves = {"successes": 0, "failures": 0}


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
        f"Level {d['level']}  Prof {sign(d['proficiency_bonus'])}  AC {d['ac']['total']}  "
        f"HP {sheet.hp.current}/{sheet.hp_max}" + (f" (+{sheet.hp.temp} temp)" if sheet.hp.temp else "")
        + f"  Speed {d['speed']['total']}  Init {sign(d['initiative']['total'])}"
        f"  PP {d['passive_perception']['total']}",
        "  ".join(
            f"{a.upper()} {sheet.abilities.get(a, 10)} ({sign(d['modifiers'][a])})" for a in ABILITIES
        ),
        "Saves: "
        + ", ".join(
            f"{a.upper()}{'*' if a in sheet.save_proficiencies else ''} {sign(d['saves'][a]['total'])}"
            for a in ABILITIES
        ),
        "Skills: "
        + ", ".join(
            f"{s} {sign(d['skills'][s]['total'])}"
            for s in SKILLS
            if s in sheet.skill_proficiencies or s in sheet.skill_expertise
        ),
    ]
    if sheet.conditions:
        lines.append("Conditions: " + ", ".join(sheet.conditions))
    if sheet.attacks:
        lines.append("Attacks: " + "; ".join(
            f"{a.name} {sign(x['to_hit']['total'])} {x['text']}".strip()
            + (f" ({', '.join(a.properties)})" if a.properties else "") for a, x in zip(sheet.attacks, d["attacks"])))
    if sheet.resources:
        lines.append(
            "Resources: "
            + "; ".join(f"{r.name} {r.max - r.used}/{r.max}" for r in sheet.resources)
        )
    if sheet.spells:
        if "spell_save_dc" in d:
            lines.append(f"Spell DC {d['spell_save_dc']['total']}  Spell attack {sign(d['spell_attack']['total'])}")
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
