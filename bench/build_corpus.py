"""Build the benchmark's four reference files from their source repositories.

    uv run python bench/build_corpus.py [out_dir]

Clones the repositories (shallow) into a temporary directory and writes
chateau-rousseau.md, guadana.md, 030crc-monsters.md and srd-5.1-bestiary.md
to out_dir (default bench/corpus), each with its license on top. Upload them
to a DM campaign as "Published module". See README.md.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPOS = {
    "adventures": "https://github.com/WaveBirdDash/dnd5e-adventures",
    "monsters": "https://github.com/WaveBirdDash/dnd5e-030crc",
    "srd": "https://github.com/thejasonvu/dnd-5e-srd",
}
NC = ("© 2021 Mario Panighetti, CC BY-NC-SA 4.0 (https://github.com/WaveBirdDash/dnd5e-adventures)."
      " Includes SRD 5.1 material © Wizards of the Coast, CC BY 4.0.")
NAV = re.compile(r"^\[.*(🏡|⬅️|➡️|⤴️).*\]")


def clean(text: str, demote: int = 0) -> str:
    """Drop the published site's navigation; push headings down `demote` levels to sit under a book title."""
    out = []
    for line in text.splitlines():
        if NAV.match(line.strip()) or line.strip() == "---":
            continue
        if demote and re.match(r"^#{1,5}\s", line):
            line = "#" * demote + line
        out.append(line)
    return "\n".join(out).strip() + "\n"


def fix_srd(name: str, text: str) -> str:
    """Repair the upstream markdown's known faults, so each creature's stat block is whole and in place."""
    if name == "11 monsters":
        # The ice devil's Magic Resistance and actions sit inside the imp's block, after its ability scores.
        imp = text.index("##### Imp\n")
        start = text.index("***Magic Resistance.*** The devil has advantage", imp)
        end = text.index("The frigid air dissipates when the rest of the wall vanishes.\n", start)
        end = text.index("\n", end) + 1
        moved = text[start:end]
        text = text[:start] + text[end:].lstrip("\n")
        text = text[:imp] + moved + "\n" + text[imp:]
    if name == "16 npcs":
        text = text.replace("\n**Archmage**\n", "\n### Archmage\n", 1)  # a bold line, not a heading
    return text


def build(src: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    docs = src / "adventures/docs"
    chateau = docs / "tier-2/chateau-rousseau"
    parts = [f"# Chateau Rousseau\n\n_**By Mario Panighetti**_ · **Level:** 5\n\n_{NC}_\n"]
    for name in ("ch-1-intro", "ch-2-house-rules", "ch-3-house-mechanics", "ch-4-house-layout",
                 "ap-a-magic-items", "ap-b-rousseau-family"):
        parts.append(clean((chateau / f"{name}.md").read_text(), demote=1))
    parts.append(clean((docs / "rules-definitions.md").read_text(), demote=1))
    (out / "chateau-rousseau.md").write_text("\n".join(parts))

    guadana = clean((docs / "tier-2/guadana/index.md").read_text())
    (out / "guadana.md").write_text(guadana.replace("_**By Mario Panighetti**_", f"_**By Mario Panighetti**_\n\n_{NC}_", 1))

    monsters = ["# Zero-to-Thirty CR Challenge: Homebrew Monsters\n\n"
                "_© 2021 Mario Panighetti, CC BY-NC-SA 4.0 (https://github.com/WaveBirdDash/dnd5e-030crc)._\n"]
    monsters += [clean(p.read_text(), demote=1) for p in sorted((src / "monsters/docs").glob("2021-*.md"))]
    (out / "030crc-monsters.md").write_text("\n".join(monsters))

    srd = ["# SRD 5.1 Bestiary\n\n_System Reference Document 5.1 © 2016 Wizards of the Coast LLC, CC BY 4.0"
           " (https://creativecommons.org/licenses/by/4.0/). Markdown from https://github.com/thejasonvu/dnd-5e-srd._\n"]
    srd += [clean(fix_srd(name, (src / f"srd/markdown/{name}.md").read_text()))
            for name in ("11 monsters", "15 creatures", "16 npcs")]
    (out / "srd-5.1-bestiary.md").write_text("\n".join(srd))


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "corpus"
    with tempfile.TemporaryDirectory() as tmp:
        for name, url in REPOS.items():
            subprocess.run(["git", "clone", "--quiet", "--depth", "1", url, f"{tmp}/{name}"], check=True)
        build(Path(tmp), out)
    for f in sorted(out.glob("*.md")):
        print(f"{f}  {f.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
