"""Rolling on the book's tables: random encounters, treasure, rumors.

The digest copies each table as printed, rows keyed by their roll ("1-4",
"05", "00", "18+"); this reads those keys and finds the row a roll lands on.
"""

from __future__ import annotations

import random
import re
from typing import Any

_DICE = re.compile(r"^\s*(\d*)\s*d\s*(\d+|%)\s*$", re.IGNORECASE)
_RANGE = re.compile(r"^\s*(\d+)\s*(?:[-–—]|to)\s*(\d+)\s*$")


def dice_range(dice: str) -> tuple[int, int] | None:
    """(count, sides) for "d20", "2d6", "d%"; None if it isn't dice."""
    m = _DICE.match(dice)
    if not m:
        return None
    count = int(m.group(1) or 1)
    sides = 100 if m.group(2) == "%" else int(m.group(2))
    return (count, sides) if count and sides else None


def roll(dice: str, rng: random.Random | None = None) -> int:
    parsed = dice_range(dice)
    if parsed is None:
        raise ValueError(f"can't roll {dice!r}")
    count, sides = parsed
    rng = rng or random.Random()
    return sum(rng.randint(1, sides) for _ in range(count))


def _number(text: str, sides: int) -> int:
    n = int(text)
    return sides if n == 0 and sides in (10, 100) and set(text) == {"0"} else n  # "00" on a d100, "0" on a d10


def covers(key: str, n: int, sides: int) -> bool:
    """Whether a row keyed `key` ("1-4", "05", "00", "18+", "1, 2") is the row for a roll of n."""
    for part in re.split(r"[,/]| or ", key):
        part = part.strip()
        if not part:
            continue
        if m := _RANGE.match(part):
            lo, hi = _number(m.group(1), sides), _number(m.group(2), sides)
            if lo <= n <= hi:
                return True
        elif m := re.match(r"^(\d+)\s*\+$", part):
            if n >= int(m.group(1)):
                return True
        elif m := re.match(r"^(\d+)\s*(?:or (?:less|lower)|-)$", part):
            if n <= int(m.group(1)):
                return True
        elif part.isdigit() and _number(part, sides) == n:
            return True
    return False


def row_for(table: dict[str, Any], n: int) -> dict[str, Any] | None:
    parsed = dice_range(table["dice"])
    sides = parsed[1] if parsed else 0
    return next((r for r in table["rows"] if covers(r["roll"], n, sides)), None)
