"""Build src/campaign/srd_rules.json, the rules of 5e things, from the SRD 5.1's markdown.

    git clone https://github.com/thejasonvu/dnd-5e-srd
    uv run python scripts/srd_rules.py dnd-5e-srd/markdown

Magic items and spells are taken whole, as the SRD words them; weapons, armor
and adventuring gear from their tables, with the gear's own descriptions and
the weapon properties a weapon has. SRD 5.1 © Wizards of the Coast, CC BY 4.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "campaign" / "srd_rules.json"


def headed(text: str, level: str = "####") -> list[tuple[str, str]]:
    """(heading, body) for each `level` heading in text; deeper headings stay in the body."""
    parts = re.split(rf"^{level} (.+)$", text, flags=re.M)
    return [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def split_subtitle(body: str) -> tuple[str, str]:
    """The italic line under a heading ("*Wondrous item, rare*") and the rest."""
    m = re.match(r"\*([^*\n]+)\*\s*\n", body)
    return (m.group(1).strip(), body[m.end():].strip()) if m else ("", body)


def table_rows(block: str, raw: bool = False) -> tuple[list[str], list[list[str]]]:
    lines = [l for l in block.splitlines() if l.startswith("|")]
    if len(lines) < 3:
        return [], []
    cells = lambda l: [c if raw else c.strip() for c in l.strip().strip("|").split("|")]  # noqa: E731
    return cells(lines[0]), [cells(l) for l in lines[2:]]


def sections(text: str, level: str) -> dict[str, str]:
    return dict(headed(text, level))


def bold_paragraphs(text: str) -> dict[str, str]:
    """***Name.*** paragraphs, by name, each with the paragraphs that follow it up to the next one."""
    out: dict[str, str] = {}
    name = None
    for para in re.split(r"\n\s*\n", text):
        if m := re.match(r"\*\*\*(.+?)\.?\*\*\*\s*(.*)", para.strip(), flags=re.S):
            name = m.group(1).strip().rstrip(".")
            out[name] = m.group(2).strip()
        elif name and para.strip() and not para.lstrip().startswith(("|", "#")):
            out[name] += "\n\n" + para.strip()
        else:
            name = None
    return out


def read(path: Path) -> str:
    return path.read_text().replace("\r\n", "\n")


def build(md: Path) -> dict:
    entries: list[dict] = []
    items = read(md / "10 magic items.md")
    for name, body in headed(items):
        subtitle, text = split_subtitle(body)
        entries.append({"name": name, "kind": "magic item", "subtitle": subtitle, "text": text})

    spells = read(md / "08 spellcasting.md")
    spells = spells[spells.index("Spell Descriptions"):]
    for name, body in headed(spells):
        subtitle, text = split_subtitle(body)
        # Casting time, range, components and duration on one line, so the spell itself comes sooner.
        text = re.sub(r"^((?:\*\*[^*]+:\*\* .+\n\n)+)", lambda m: " · ".join(m.group(1).split("\n\n")).strip(" ·") + "\n\n", text)
        m = re.match(r"(\d)\w\w-level", subtitle)
        entries.append({"name": name, "kind": "spell", "subtitle": subtitle, "text": text,
                        "level": int(m.group(1)) if m else 0})

    gear = read(md / "04 equipment.md")
    props = bold_paragraphs(gear[gear.index("### Weapon Properties"):gear.index("#### Improvised Weapons")])
    specials = bold_paragraphs(gear[gear.index("#### Special Weapons"):gear.index("### Weapons List")])
    for title, block in sections(gear, "#####").items():
        head, rows = table_rows(block)
        if title.endswith("Weapons") and head[:1] == ["Name"]:
            for name, cost, damage, weight, properties in rows:
                if not cost:
                    continue
                said = [p for p in props if p != "Range" and re.search(rf"\b{p.lower()}\b", properties.lower())]
                text = f"**{damage}** · {properties} · {cost}, {weight}"
                if name in specials:
                    text += f"\n\n{specials[name]}"
                text += "".join(f"\n\n***{p}.*** {props[p]}" for p in said)
                entries.append({"name": name, "kind": "weapon", "subtitle": title.rstrip("s").lower().capitalize(), "text": text})
        elif title in ("Light Armor", "Medium Armor", "Heavy Armor", "Shield"):
            for name, cost, ac, strength, stealth, weight in rows:
                bits = [f"**AC {ac}**", f"{cost}, {weight}"]
                if strength != "—":
                    bits.append(f"needs {strength}, or speed drops by 10 feet")
                if stealth != "—":
                    bits.append("disadvantage on Stealth")
                entries.append({"name": name, "kind": "armor", "subtitle": title if title == "Shield" else title.lower().capitalize(),
                                "text": " · ".join(bits)})
    about = bold_paragraphs(gear[gear.index("Adventuring Gear\n---"):gear.index("##### Adventuring Gear")])
    head, rows = table_rows(sections(gear, "#####")["Adventuring Gear"], raw=True)
    group = ""
    for name, cost, weight in rows:
        indented, name, cost, weight = name.startswith("  "), name.strip(), cost.strip(), weight.strip()
        if not cost:  # a heading row inside the table: "*Arcane focus*"
            group = name.strip("* ")
            continue
        if not indented:
            group = ""
        if name.lower().startswith("potion of healing"):
            continue  # a magic item, there
        plain = re.sub(r"\s*\(.*\)$", "", name).strip()
        desc = about.get(plain) or about.get(plain.title()) or next(
            (v for k, v in about.items() if k.lower() == plain.lower() or k.lower().startswith(plain.lower().split(",")[0])), "")
        text = f"{cost}, {weight}" + (f"\n\n{desc}" if desc else "")
        entries.append({"name": name.strip(), "kind": "gear", "subtitle": group or "Adventuring gear", "text": text})
    for e in entries:  # bold italics as bold: the app's markdown doesn't nest them
        e["text"] = re.sub(r"\*\*\*(.+?)\*\*\*", r"**\1**", e["text"])
    return {
        "license": "SRD 5.1 © Wizards of the Coast, CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/), "
                   "via github.com/thejasonvu/dnd-5e-srd.",
        "entries": entries,
    }


if __name__ == "__main__":
    data = build(Path(sys.argv[1]))
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=0) + "\n")
    kinds: dict[str, int] = {}
    for e in data["entries"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(OUT, kinds)
