"""Digest reference documents into a tree of summaries and a catalog of entities.

The document's section tree (see ingest.py) is reduced bottom-up:

- A **leaf** is a section's own text. Claude summarizes it and extracts the
  NPCs, monsters, locations, items, quests, factions and lore it mentions,
  with aliases, pages and facts.
- A **branch** combines its children's digests, never raw text. Claude
  writes a summary of the whole section and says which of the children's
  entities are the same thing under different names. The merge itself is
  done in code.

Stat blocks are read on their own, outside the tree. Every stat block in
the source is found in code by its Armor Class line, wherever the outline
put it (a PDF often splits one across sections, and interleaves its columns
with the text around it), and Claude copies each one, word for word, into
its parts (`find_stat_blocks`, STATBLOCK_SYSTEM).

The root's entities and the stat blocks become the source's catalog, which
is applied to the campaign's world model as facts true from session 0 (see
world.py).

Caching works like a Merkle tree. Every node's output is stored under a
hash of exactly what produced it:

    leaf key       = H("leaf",      LEAF_VERSION,      model, title, text)
    branch key     = H("branch",    BRANCH_VERSION,    model, title, H(child output) for each child)
    stat block key = H("statblock", STATBLOCK_VERSION, model, text around it)

so a node is digested again only if its own text changed, or one of its
children's digests changed. Editing one chapter re-digests that chapter's
changed leaves and the branches above them; re-uploading an unchanged file,
re-chunking it, or loading the same module into another campaign costs
nothing. Bumping LEAF_VERSION redoes everything; bumping BRANCH_VERSION
redoes only the branches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

import anthropic
from pydantic import BaseModel

from .db import Store
from .loops import LeasedLoop

LEAF_VERSION = 4  # 2: rollable tables, keyed areas, read-aloud text, treasure; 3: claims, placement, sets; 4: no stat blocks
BRANCH_VERSION = 2  # 2: stricter merge rules (no merging across kinds, members into groups, shared titles)
STATBLOCK_VERSION = 1
MODEL = os.environ.get("CAMPAIGN_DIGEST_MODEL", os.environ.get("CAMPAIGN_MODEL", "claude-opus-5-5"))
EFFORT = os.environ.get("CAMPAIGN_DIGEST_EFFORT", "medium")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Haiku has no server-side refusal fallback; a refused request just fails.
NO_FALLBACK_PREFIXES = ("claude-haiku-",)


def fallback_params(model: str) -> dict[str, Any]:
    """Request parameters that re-run a refused request on a fallback model, where the model supports it."""
    if model.startswith(NO_FALLBACK_PREFIXES):
        return {}
    return {"betas": [FALLBACK_BETA], "fallbacks": "default"}

# Calls in flight at once, across every source being digested. The work is
# waiting on the API, so what bounds it is the account's rate limits, not
# this server: past them the API answers 429, and the client waits as long as
# it says (retry-after) and tries again, so a burst slows down rather than fails.
CONCURRENCY = int(os.environ.get("CAMPAIGN_DIGEST_CONCURRENCY", "32"))
RATE_LIMIT_RETRIES = 10
_IN_FLIGHT = threading.BoundedSemaphore(CONCURRENCY)
_MATERIALIZING = threading.Lock()


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=RATE_LIMIT_RETRIES)

MAX_LEAF_CHARS = 40_000  # ~10k tokens; longer sections are split into parts
MIN_LEAF_CHARS = 6_000  # shorter sibling sections are packed into one call


# -- what Claude returns -------------------------------------------------------


class StatBlock(BaseModel):
    """A creature's stat block in its printed parts. Entries are "Name. text", word for word."""

    name: str
    size_type: str  # "Huge celestial (empyrean), lawful evil"
    armor_class: str
    hit_points: str
    speed: str
    initiative: str | None  # printed only in 2024-style blocks
    strength: int | None
    dexterity: int | None
    constitution: int | None
    intelligence: int | None
    wisdom: int | None
    charisma: int | None
    saving_throws: str | None
    skills: str | None
    damage_vulnerabilities: str | None
    damage_resistances: str | None
    damage_immunities: str | None
    condition_immunities: str | None
    senses: str | None
    languages: str | None
    challenge: str
    traits: list[str]
    actions: list[str]
    bonus_actions: list[str]
    reactions: list[str]
    legendary_intro: str | None  # "Sydon can take 3 legendary actions, …"
    legendary_actions: list[str]
    lair_intro: str | None
    lair_actions: list[str]
    other: list[str]  # anything else under its own heading: "Mythic Actions. …", "Regional Effects. …"


class StatBlockOutput(BaseModel):
    found: bool  # False when the marked line isn't a creature's stat block (a ship, a trap)
    stat_block: StatBlock | None


class Entity(BaseModel):
    name: str
    kind: Literal["npc", "monster", "location", "item", "quest", "faction", "lore"]
    aliases: list[str]
    summary: str
    pages: list[int]
    facts: list[str]
    area_key: str | None  # a keyed area's map key, e.g. "C3"
    read_aloud: str | None  # boxed text to read to the players
    treasure: list[str]  # what can be found here, or what it carries


class TableRow(BaseModel):
    roll: str  # "1-4", "05", "00", "18+"
    result: str


class RollTable(BaseModel):
    name: str
    dice: str  # "d20", "d100", "2d6"
    pages: list[int]
    rows: list[TableRow]


class LeafOutput(BaseModel):
    summary: str
    entities: list[Entity]
    tables: list[RollTable]


class Merge(BaseModel):
    ids: list[str]
    name: str
    summary: str


class BranchOutput(BaseModel):
    summary: str
    merges: list[Merge]


LEAF_SYSTEM = """\
You are indexing a tabletop RPG adventure (a published module or a game
master's notes) so a campaign assistant can find things later without
rereading the book.

You get the text of one section. `[p. N]` marks where page N begins.

Return:
- summary: 2-5 sentences a GM would want before running this section: the
  situation, who and what is here, threats, treasure, and where it leads.
  Name names. Don't retell it.
- entities: every distinct thing a GM or player might later ask about by name.
  - npc: named characters
  - monster: creature types, and notable encounters
  - location: named places, areas, and keyed rooms with distinctive content
  - item: magic items and notable treasure
  - quest: hooks, missions and goals, with who gives them and the reward
  - faction: groups and organizations
  - lore: important secrets, history, prophecies
  For each give the name as the book gives it; aliases (titles, epithets,
  nicknames, short forms, and descriptions players are likely to use, such
  as "the hag" or "the burgomaster"); a 1-2 sentence summary; the pages it
  appears on; facts, short standalone statements with numbers copied
  exactly ("Reward: 50 gp each from Ismark", "DC 15 Wisdom save or be
  charmed"). Stat blocks are read separately: leave their numbers out of
  facts.
  For a keyed area (a numbered or lettered room or site on a map), also give
  area_key, the key as printed ("C3", "12", "K20a"), and put its traps,
  hazards, locks and checks with their DCs in facts. For anything, give
  read_aloud, the boxed or read-aloud text exactly as printed, if there is
  some, and treasure: each thing of value found there or carried, with
  amounts copied exactly ("3 potions of healing", "a pouch with 25 gp",
  "+1 longsword in the sarcophagus"). Otherwise leave these empty.
- tables: every table meant to be rolled on (random encounters, treasure,
  rumors, wild magic and the like), with its name, the dice to roll ("d20",
  "d100", "2d6"), its pages, and every row: the roll exactly as printed
  ("1-4", "05", "00", "18+") and the result in full. Not other tables
  (price lists, stat summaries).

Facts are what the book says is true. What a character says, believes,
claims or lies about is not: if it matters, write it as theirs ("Anni says
only her mother knows how to open the front door"), never as a plain fact,
since characters are often wrong or lying and the book may say otherwise.

Keep where things are exactly as the book places them: the room, the
container, what it's under or behind, and what guards or hides it ("Emerald
Signet: in a chest under the tables in the Conservatory, beneath a violet
fungus that attacks anything within 10 ft."), not a shortened "in the
Conservatory chest". Put that placement in the facts of the item and of the
place.

Each separately named thing is its own entity. A set of named things (three
named rings, four named siblings) is one entity each; if the set also has a
name of its own ("the Signet Rings"), it can be an entity too, but never
with its members' names as aliases, and its facts are about the set, not
about one member.

Skip mundane things (doors, furniture, unnamed guards) unless the text makes
them matter. Copy numbers exactly and never invent them.
"""

BRANCH_SYSTEM = """\
You are building an index of a tabletop RPG adventure one level of its
outline at a time. You get the summaries of one section's subsections and the
entities found in them, each with an id.

Return:
- summary: an overview of the whole section built from the subsection
  summaries: what it covers, the main NPCs and threats, important treasure,
  and how it connects to the rest of the adventure. 3-6 sentences; for a
  whole book, up to three short paragraphs.
- merges: groups of entity ids that are the same single thing under
  different names (for example "Morgantha" and "the hag of Old
  Bonegrinder", or one item described in two rooms). For each group give the
  best canonical name (the book's proper name) and a 1-2 sentence summary
  combining what's known. Only merge when the text makes clear they are one
  and the same. Never merge:
  - different individuals who share a title, role or epithet ("the Titan",
    "the king", "her father" can each be several people);
  - a member, leader or servant into the group they belong to, or a group
    into its leader;
  - a creature with the place it lives in, the treasure it guards, or the
    kind of creature it is (a named dragon is not "dragons");
  - things of different kinds (a person and an item, a place and a faction).
  Leave everything else out.
"""

MARK = "▶ "  # starts the Armor Class line of the stat block a call is about

STATBLOCK_SYSTEM = f"""\
You transcribe one creature's stat block from a tabletop RPG book into its
parts, for a game master to run at the table from what you return.

The block to transcribe is the one whose Armor Class line starts with
"{MARK.strip()}". The text around it may hold other stat blocks or prose; ignore
them. If it came from a PDF, the columns may be interleaved: the block's
parts can be separated by other text, appear before its Armor Class line,
or carry on after a page footer. Gather every part that belongs to this
creature, and only those.

Copy every entry word for word as printed: every number, DC, die, range,
duration, condition and rider, and every spell in a spell list. Join lines
the PDF broke and undo hyphenation across lines, but never summarize,
shorten, reword or leave anything out. If a sentence is cut off where the
text ends, keep what's there.

- name: the creature's name, as the book titles this stat block.
- size_type: the size, type and alignment line, as printed.
- armor_class, hit_points, speed, initiative (2024-style blocks only),
  saving_throws, skills, damage_vulnerabilities, damage_resistances,
  damage_immunities, condition_immunities, senses, languages, challenge:
  each value as printed, without its label ("22 (natural armor)",
  "313 (19d12 + 190)", "23 (50,000 XP)"). A 2024-style "Immunities" line
  is split: damage types to damage_immunities, conditions to
  condition_immunities. Leave out what the block doesn't print.
- strength … charisma: the ability scores (the score, not the modifier).
- traits, actions, bonus_actions, reactions, legendary_actions,
  lair_actions: each entry as one string, its name, a period and its text
  ("Bite. Melee Weapon Attack: +14 to hit, reach 10 ft., one target. Hit:
  19 (2d10 + 8) piercing damage."), in the book's order and under the
  heading the book puts it under. Traits are the entries before Actions.
  A spellcasting entry is one string with all its spell lines.
- legendary_intro, lair_intro: the paragraph that opens those sections
  ("Sydon can take 3 legendary actions, …").
- other: anything else printed under a heading of its own (mythic actions,
  regional effects), each as "Heading. text".

The text opens with the heading path of the section the block is in, which
names the creature when the block's own heading doesn't ("Attributes").

If the marked line isn't a creature's stat block (a ship, a trap, an
object), return found false and no stat_block.
"""


# -- the tree ------------------------------------------------------------------


@dataclass
class DNode:
    title: str
    page_start: int | None = None
    page_end: int | None = None
    text: str = ""  # leaves only
    children: list[DNode] = field(default_factory=list)
    row_id: int | None = None
    key: str | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    statblock: bool = False  # one stat block's text, read on its own (not part of the tree)
    stat_blocks: list[DNode] = field(default_factory=list)  # the root's: every stat block in the source

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def kind(self) -> str:
        return "statblock" if self.statblock else "leaf" if self.is_leaf else "branch"

    def walk(self) -> Iterator[DNode]:
        yield self
        for c in self.children:
            yield from c.walk()

    def calls(self) -> list[DNode]:
        """Every node of a root's plan: the tree, then the stat blocks."""
        return [*self.walk(), *self.stat_blocks]


def _pages(items: list[Any]) -> tuple[int | None, int | None]:
    starts = [i.page_start if isinstance(i, DNode) else i["page"] for i in items]
    ends = [i.page_end if isinstance(i, DNode) else i["page"] for i in items]
    starts = [p for p in starts if p is not None]
    ends = [p for p in ends if p is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _render(chunks: list[dict[str, Any]]) -> str:
    """Section text for Claude, with page markers and the local headings."""
    return _render_spans(chunks)[0]


def _render_spans(chunks: list[dict[str, Any]]) -> tuple[str, list[tuple[int, str]]]:
    """The rendered text, and where each chunk's part of it starts with the chunk's heading path."""
    parts: list[str] = []
    spans: list[tuple[int, str]] = []
    size = 0
    page = heading = None

    def add(text: str) -> None:
        nonlocal size
        size += len(text) + (2 if parts else 0)
        parts.append(text)

    for c in chunks:
        spans.append((size + (2 if parts else 0), c["heading"] or ""))
        if c["page"] is not None and c["page"] != page:
            page = c["page"]
            add(f"[p. {page}]")
        local = c["heading"].split(" > ")[-1] if c["heading"] else ""
        if local and local != heading:
            heading = local
            add(f"### {local}")
        add(c["text"])
    return "\n\n".join(parts), spans


def _leaf(title: str, chunks: list[dict[str, Any]]) -> DNode | None:
    if not chunks:
        return None
    start, end = _pages(chunks)
    text = _render(chunks)
    if len(text) <= MAX_LEAF_CHARS:
        return DNode(title, start, end, text=text)
    # Too long for one call: split on chunk boundaries into roughly equal parts.
    target = len(text) / -(-len(text) // MAX_LEAF_CHARS)
    groups: list[list[dict[str, Any]]] = [[]]
    size = 0
    for c in chunks:
        if groups[-1] and (size >= target or size + len(c["text"]) > MAX_LEAF_CHARS):
            groups.append([])
            size = 0
        groups[-1].append(c)
        size += len(c["text"])
    if len(groups) == 1:  # one oversized chunk; can't split further
        return DNode(title, start, end, text=text)
    parts = [_leaf(f"{title} (part {i}/{len(groups)})", g) for i, g in enumerate(groups, 1)]
    return DNode(title, start, end, children=[p for p in parts if p])


def _pack(children: list[DNode]) -> list[DNode]:
    """Merge runs of small sibling leaves so tiny rooms don't each cost a call.

    Packing depends only on the leaves' sizes, so it is stable: editing one
    room changes at most the pack it's in (and maybe the next).
    """
    out: list[DNode] = []
    group: list[DNode] = []

    def flush() -> None:
        if len(group) == 1:
            out.append(group[0])
        elif group:
            titles = [g.title for g in group]
            title = "; ".join(titles) if len(titles) <= 3 else f"{titles[0]} … {titles[-1]} ({len(titles)} sections)"
            start, end = _pages(group)
            text = "\n\n".join(f"## {g.title}\n\n{g.text}" for g in group)
            out.append(DNode(title, start, end, text=text))
        group.clear()

    for c in children:
        if c.is_leaf and len(c.text) < MIN_LEAF_CHARS:
            group.append(c)
            if sum(len(g.text) for g in group) >= MIN_LEAF_CHARS:
                flush()
        else:
            flush()
            out.append(c)
    flush()
    return out


def build_tree(title: str, nodes: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> DNode | None:
    by_node: dict[int | None, list[dict[str, Any]]] = {}
    for c in chunks:
        by_node.setdefault(c["node_id"], []).append(c)
    kids: dict[int | None, list[dict[str, Any]]] = {}
    for n in nodes:
        kids.setdefault(n["parent_id"], []).append(n)

    def branch(name: str, own: list[dict[str, Any]], child_nodes: list[dict[str, Any]], own_title: str) -> DNode | None:
        children = [t for t in (build(n) for n in child_nodes) if t]
        intro = _leaf(own_title if not children else f"{name} (introduction)", own)
        if not children:
            return intro
        children = _pack(([intro] if intro else []) + children)
        if len(children) == 1:
            only = children[0]
            if not only.title.startswith(name):
                only.title = f"{name} › {only.title}"
            return only
        start, end = _pages(children)
        return DNode(name, start, end, children=children)

    def build(n: dict[str, Any]) -> DNode | None:
        tree = branch(n["title"], by_node.get(n["id"], []), kids.get(n["id"], []), n["title"])
        if tree and n["page_start"] is not None and tree.page_start is None:
            tree.page_start, tree.page_end = n["page_start"], n["page_end"]
        return tree

    return branch(title, by_node.get(None, []), kids.get(None, []), title)


# -- finding stat blocks -------------------------------------------------------------

# A stat block's Armor Class line: "Armor Class 22 (natural)", "**Armor Class** 15", "**AC** 12 **Initiative** +2".
_AC_LINE = re.compile(r"^[ \t*_#>]*(?:Armor Class|AC)\b[*_:\s]*\d", re.IGNORECASE | re.MULTILINE)
# What can sit between a stat block's title and its Armor Class line: its type line ("Huge celestial,
# lawful evil", often in italics), page marks and blank lines.
_TITLE = re.compile(r"^\s*(#{1,6}\s+\S.*|\*\*[^*]+\*\*|__[^_]+__)\s*$")
_BETWEEN = re.compile(
    r"^\s*(\[p\. \d+\]|[*_][^*_].*[*_]|(Tiny|Small|Medium|Large|Huge|Gargantuan)\b.*|)\s*$", re.IGNORECASE)
# Headings that name a part of a stat block, not the creature (homebrew often puts the block under "Attributes").
_GENERIC = re.compile(
    r"^(attributes|statistics|stats|stat ?block|actions|bonus actions|reactions|legendary actions|lair actions"
    r"|traits|description|abilities|combat|variants?)$", re.IGNORECASE)
STATBLOCK_REACH = 8_000  # how far past its Armor Class line a block may run, for PDFs that interleave columns
STATBLOCK_LEAD = 1_500  # how far before it to start, when no title shows where it starts


def _title_start(text: str, ac: int) -> int | None:
    """Where the block whose Armor Class line starts at `ac` begins: its title (a heading or a bold
    name) just above, past its type line and page marks. None if nothing marks it."""
    found, i = None, ac
    for _ in range(10):
        if i <= 0:
            break
        j = text.rfind("\n", 0, i - 1) + 1
        line = text[j:i].rstrip("\n")
        if _TITLE.match(line):
            found = j
        elif not _BETWEEN.match(line):
            break
        i = j
    return found


def _title_text(text: str, start: int | None) -> str:
    if start is None:
        return ""
    return re.sub(r"^[#*_\s]+|[*_\s]+$", "", text[start:].split("\n", 1)[0])


def _creature(heading: str) -> str:
    """The creature a heading path names: its last part that isn't a part of a stat block."""
    names = [p for p in (re.sub(r"[#*_]", "", x).strip() for x in heading.split(" > ")) if p and not _GENERIC.match(p)]
    return names[-1] if names else ""


def find_stat_blocks(chunks: list[dict[str, Any]]) -> list[DNode]:
    """Every stat block in a source, each as the text around it with its Armor Class line marked,
    after the heading path of the section that line is in.

    A block runs from its title to the next block's title, so a clean book (Markdown,
    a well-made PDF) gives each call one block. Where nothing marks where blocks
    start, the window reaches further either way and the call is told which one it's
    about, so a block a PDF split apart or interleaved with other text still arrives whole.
    """
    text, spans = _render_spans(chunks)
    acs = [m.start() for m in _AC_LINE.finditer(text)]
    titles = [_title_start(text, a) for a in acs]
    pages = [(m.start(), int(m.group(1))) for m in re.finditer(r"^\[p\. (\d+)\]$", text, re.MULTILINE)]
    out = []
    for i, a in enumerate(acs):
        start = titles[i] if titles[i] is not None else max(0, a - STATBLOCK_LEAD)
        end = min(len(text), a + STATBLOCK_REACH)
        if i + 1 < len(acs) and titles[i + 1] is not None:
            end = min(end, titles[i + 1])
        page = next((p for pos, p in reversed(pages) if pos < a), None)
        heading = next((hd for pos, hd in reversed(spans) if pos <= a), "")
        name = _title_text(text, titles[i])
        name = name if name and not _GENERIC.match(name) else _creature(heading)
        title = f"Stat block: {name}" if name else f"Stat block, p. {page}" if page else "Stat block"
        window = text[start:a] + MARK + text[a:end]
        out.append(DNode(title, page, page, text=f"Section: {heading}\n\n{window}" if heading else window,
                         statblock=True))
    return out


# -- cache keys ------------------------------------------------------------------


def _h(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def output_hash(output: dict[str, Any]) -> str:
    return _h(json.dumps(output, sort_keys=True, ensure_ascii=False))


def leaf_key(node: DNode, model: str) -> str:
    return _h("leaf", str(LEAF_VERSION), model, node.title, node.text)


def statblock_key(node: DNode, model: str) -> str:
    return _h("statblock", str(STATBLOCK_VERSION), model, node.text)


def branch_key(node: DNode, model: str) -> str:
    assert all(c.output is not None for c in node.children)
    return _h("branch", str(BRANCH_VERSION), model, node.title, *(output_hash(c.output) for c in node.children))


def _resolve(store: Store, node: DNode, model: str) -> None:
    """Fill in keys and cached outputs bottom-up, as far as the cache reaches."""
    for c in node.children:
        _resolve(store, c, model)
    if node.statblock:
        node.key = statblock_key(node, model)
    elif node.is_leaf:
        node.key = leaf_key(node, model)
    elif all(c.output is not None for c in node.children):
        node.key = branch_key(node, model)
    if node.key:
        node.output = store.get_digest(node.key)


# -- merging entities --------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for i in items:
        k = _norm(i)
        if k and k not in seen:
            seen.add(k)
            out.append(i)
    return out


def combine(group: list[dict[str, Any]], name: str | None = None, summary: str | None = None) -> dict[str, Any]:
    name = name or group[0]["name"]
    aliases = _dedupe([e["name"] for e in group] + [a for e in group for a in e["aliases"]])

    def first(field: str) -> Any:
        return next((e[field] for e in group if e.get(field)), None)

    return {
        "name": name,
        "kind": group[0]["kind"],
        "aliases": [a for a in aliases if _norm(a) != _norm(name)],
        "summary": summary or max((e["summary"] for e in group), key=len),
        "pages": sorted({p for e in group for p in e["pages"]}),
        "facts": _dedupe([f for e in group for f in e["facts"]]),
        "area_key": first("area_key"),
        "read_aloud": first("read_aloud"),
        "treasure": _dedupe([t for e in group for t in e.get("treasure") or []]),
    }


def premerge(children: list[DNode]) -> list[dict[str, Any]]:
    """Children's entities with exact (kind, name) duplicates already combined."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in children:
        for e in c.output["entities"]:
            groups.setdefault((e["kind"], _norm(e["name"])), []).append(e)
    return [combine(g) if len(g) > 1 else g[0] for g in groups.values()]


def _family(kind: str) -> str:
    return "creature" if kind in ("npc", "monster") else kind


MAX_MERGE = 6  # more than this many entries being "one thing" is a sign of lumping, not merging


def apply_merges(candidates: list[dict[str, Any]], merges: list[Merge]) -> list[dict[str, Any]]:
    """Combine the groups Claude says are one thing, within limits checked here: only
    entries of the same kind of thing (a named creature may be an npc or a monster),
    and only small groups. A group that breaks them is left unmerged."""
    index = {f"e{i}": i for i in range(len(candidates))}
    merged_into: dict[int, int] = {}  # candidate -> first candidate of its group
    groups: dict[int, tuple[list[int], Merge]] = {}
    for m in merges:
        ids = [index[i] for i in dict.fromkeys(m.ids) if i in index and index[i] not in merged_into]
        if len(ids) < 2 or len(ids) > MAX_MERGE or len({_family(candidates[i]["kind"]) for i in ids}) > 1:
            continue
        for i in ids:
            merged_into[i] = ids[0]
        groups[ids[0]] = (ids, m)
    out = []
    for i, e in enumerate(candidates):
        if i in groups:
            ids, m = groups[i]
            out.append(combine([candidates[j] for j in ids], name=m.name, summary=m.summary))
        elif i not in merged_into:
            out.append(e)
    return out


# -- calling Claude ------------------------------------------------------------------


class DigestError(Exception):
    pass


def _call(client: Any, system: str, user: str, fmt: type[BaseModel], model: str, effort: str) -> tuple[Any, int, int]:
    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=fmt,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        **fallback_params(model),
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise DigestError("Claude declined to digest this section")
    if msg.stop_reason == "max_tokens":
        raise DigestError("digest output was cut off (max_tokens)")
    if msg.parsed_output is None:
        raise DigestError("no structured output in response")
    u = msg.usage
    input_tokens = u.input_tokens + (getattr(u, "cache_read_input_tokens", 0) or 0) + (
        getattr(u, "cache_creation_input_tokens", 0) or 0
    )
    return msg.parsed_output, input_tokens, u.output_tokens


def _span(node: DNode) -> str:
    if node.page_start is None:
        return ""
    if node.page_end in (None, node.page_start):
        return f" (p. {node.page_start})"
    return f" (pp. {node.page_start}-{node.page_end})"


def leaf_prompt(node: DNode) -> str:
    return f"Section: {node.title}{_span(node)}\n\n{node.text}"


def branch_prompt(node: DNode, candidates: list[dict[str, Any]]) -> str:
    lines = [f"Section: {node.title}{_span(node)}", "", "## Subsections"]
    for c in node.children:
        lines += [f"### {c.title}{_span(c)}", c.output["summary"], ""]
    lines += ["## Entities", "id | kind | name | aliases | summary"]
    for i, e in enumerate(candidates):
        lines.append(f"e{i} | {e['kind']} | {e['name']} | {'; '.join(e['aliases'])} | {e['summary']}")
    return "\n".join(lines)


# The labelled lines of a stat block between its armor class and its challenge, as books label them
# (2014 and 2024 styles), and the field each goes in. Checked against the source after a block is read.
STAT_LINES = {
    "saving throws": "saving_throws", "skills": "skills",
    "damage vulnerabilities": "damage_vulnerabilities", "vulnerabilities": "damage_vulnerabilities",
    "damage resistances": "damage_resistances", "resistances": "damage_resistances",
    "damage immunities": "damage_immunities", "condition immunities": "condition_immunities",
    "senses": "senses", "languages": "languages",
}
STAT_LINES |= {k.removesuffix("ies") + "y" if k.endswith("ies") else k.removesuffix("s"): v
               for k, v in STAT_LINES.items() if k not in ("senses", "skills", "languages", "saving throws")}
_STAT_LINE = re.compile(
    r"^\W*(" + "|".join(sorted(map(re.escape, STAT_LINES), key=len, reverse=True)) + r")\b\W*\s+(.+?)\s*$",
    re.IGNORECASE)
_CHALLENGE = re.compile(r"^\W*(challenge|CR)\b", re.IGNORECASE | re.MULTILINE)


def stat_lines(text: str) -> dict[str, str]:
    """The labelled lines (saves, skills, resistances, senses, languages…) of the marked stat block in
    `text`, by field: from its marked Armor Class line to its challenge."""
    start = text.find(MARK)
    end = _CHALLENGE.search(text, start) if start >= 0 else None
    if end is None:
        return {}
    out: dict[str, str] = {}
    for line in text[start:end.start()].splitlines():
        line = re.sub(r"<br\s*/?>", "", line)
        if hit := _STAT_LINE.match(line):
            out.setdefault(STAT_LINES[hit.group(1).lower()], re.sub(r"[*_]", "", hit.group(2)).strip())
    return out


def fill_missing_lines(sb: dict[str, Any], text: str) -> dict[str, Any]:
    """Add the labelled lines the source prints for this stat block but the reading left out."""
    missing = {f: v for f, v in stat_lines(text).items() if not sb.get(f)}
    return {**sb, **missing}


def stat_block_entry(sb: dict[str, Any], page: int | None) -> dict[str, Any]:
    """A stat block as an entry in the source's catalog, for the creature of that name."""
    return {"name": sb["name"], "kind": "monster", "aliases": [], "summary": "", "pages": [page] if page else [],
            "facts": [], "stat_block": sb, "area_key": None, "read_aloud": None, "treasure": []}


STATBLOCK_EFFORT = "low"  # copying, not judging


def digest_node(client: Any, node: DNode, model: str, effort: str) -> tuple[dict[str, Any], int, int]:
    if node.statblock:
        parsed, i, o = _call(client, STATBLOCK_SYSTEM, node.text, StatBlockOutput, model, STATBLOCK_EFFORT)
        sb = parsed.stat_block.model_dump() if parsed.found and parsed.stat_block else None
        entities = [stat_block_entry(fill_missing_lines(sb, node.text), node.page_start)] if sb else []
        return {"entities": entities}, i, o
    if node.is_leaf:
        parsed, i, o = _call(client, LEAF_SYSTEM, leaf_prompt(node), LeafOutput, model, effort)
        entities = [e.model_dump() for e in parsed.entities]
        # The same name twice within one section is the same thing.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for e in entities:
            groups.setdefault((e["kind"], _norm(e["name"])), []).append(e)
        entities = [combine(g) if len(g) > 1 else {**g[0], "pages": sorted(set(g[0]["pages"]))} for g in groups.values()]
        tables = [t.model_dump() for t in parsed.tables if t.rows]
        return {"summary": parsed.summary, "entities": entities, "tables": tables}, i, o
    candidates = premerge(node.children)
    parsed, i, o = _call(client, BRANCH_SYSTEM, branch_prompt(node, candidates), BranchOutput, model, effort)
    tables = [t for c in node.children for t in c.output.get("tables", [])]  # passed up as they are
    return {"summary": parsed.summary, "entities": apply_merges(candidates, parsed.merges), "tables": tables}, i, o


# -- planning and running ------------------------------------------------------------


def catalog(root: DNode) -> tuple[list[dict[str, Any]], str]:
    """A digested source's catalog: the tree's entities, then a stat block entry for each stat block
    read so far. With the key it's applied under, which changes whenever one of them does."""
    read = [n for n in root.stat_blocks if n.output is not None]
    entities = [*root.output["entities"], *(e for n in read for e in n.output["entities"])]
    return entities, _h(root.key, *(n.key for n in read))


def _materialize(store: Store, source_id: int, root: DNode) -> None:
    """Apply the book's catalog to its campaign's world model as the state before play."""
    from .world import World

    campaign = store.campaign_by_id(store.source(source_id)["campaign_id"])
    entities, key = catalog(root)
    World(store, campaign).apply_book(source_id, entities, key=key)


def plan(store: Store, source_id: int, model: str = MODEL, save: bool = True) -> DNode | None:
    """Build the digest tree for a source and match it against the cache. No API calls.

    With `save`, records the tree so the UI can show progress, and if the
    root is already cached (an unchanged re-upload) refreshes the entity
    catalog right away. A tree with the same shape as the saved one keeps
    its rows (and their ids), only updating which are done or failed, so
    planning again and again, as the reconciler does, doesn't churn them.
    Without `save` it only reads (for cost estimates).
    """
    src = store.source(source_id)
    nodes, chunks = store.source_structure(source_id)
    root = build_tree(src["title"], nodes, chunks) if src else None
    if root is None:
        if save:
            store.save_digest_plan(source_id, [])
        return None
    root.stat_blocks = find_stat_blocks(chunks)
    _resolve(store, root, model)
    for n in root.stat_blocks:
        _resolve(store, n, model)
    if not save:
        return root

    rows: list[dict[str, Any]] = []
    order: list[DNode] = []

    def flatten(n: DNode, parent: int | None, depth: int) -> None:
        idx = len(rows)
        rows.append({
            "parent": parent, "depth": depth, "title": n.title, "page_start": n.page_start,
            "page_end": n.page_end, "kind": n.kind, "chars": len(n.text), "key": n.key,
        })
        order.append(n)
        for c in n.children:
            flatten(c, idx, depth + 1)

    flatten(root, None, 0)
    for n in root.stat_blocks:  # beside the tree, not in it
        flatten(n, None, 0)
    for n in order:  # a section that failed for good stays failed until asked again
        if n.output is None and n.key and (f := store.digest_failure(n.key)) and f["retry_at"] is None:
            n.error = f["error"]
    shape = lambda r: (r["parent"], r["title"], r["kind"], r["chars"])  # noqa: E731
    existing = store.existing_digest_plan(source_id)
    if [shape(r) for r in existing] == [shape(r) for r in rows]:
        updates = []
        for n, old in zip(order, existing):
            n.row_id = old["id"]
            if (n.key, n.error) != (old["digest_key"], old["error"]):
                updates.append((n.key, n.error, old["id"]))
        if updates:
            store.update_digest_nodes(updates)
    else:
        for n, row_id in zip(order, store.save_digest_plan(source_id, rows)):
            n.row_id = row_id
        store.update_digest_nodes([(None, n.error, n.row_id) for n in order if n.error])
    if root.output is not None:
        _materialize(store, source_id, root)
    return root


def pending_calls(root: DNode | None) -> int:
    return 0 if root is None else sum(1 for n in root.calls() if n.output is None)


# -- cost ------------------------------------------------------------------------------

# Dollars per million tokens (input, output). A model not listed gets no dollar figure.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

# Guesses for estimating before anything has been digested with a model. Once
# enough sections have been, their measured ratios replace these.
CHARS_PER_TOKEN = 3.5
LEAF_OVERHEAD = 2000  # system prompt + output schema, in tokens
LEAF_OUT_PER_IN = 0.8  # thinking + summary + entities, per input token (0.5 came out 36-42% low on Sonnet)
BRANCH_OVERHEAD = 1200
BRANCH_OUT_PER_IN = 0.3
STATBLOCK_OVERHEAD = 1500
STATBLOCK_OUT_PER_IN = 0.6  # the block copied out, and some thinking to sort out interleaved columns
VISIBLE_SHARE = 0.5  # of a node's output tokens, how much is the summary/entities its parent reads
MIN_SAMPLES = 5


def cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def spent(usage: list[dict[str, Any]]) -> float | None:
    """Dollars for a source's digested nodes, from Store.digest_usage."""
    costs = [cost(u["model"], u["input_tokens"], u["output_tokens"]) for u in usage]
    return None if None in costs else sum(c for c in costs if c is not None)


def estimate(store: Store, root: DNode | None, model: str = MODEL) -> dict[str, Any]:
    """Roughly what digesting the uncached part of a planned tree will cost.

    A leaf's input is its text; a branch's input is its children's summaries
    and entities, which for children not yet digested are themselves guesses.
    Ratios come from earlier digests with the same model when there are
    enough of them, else from the defaults above. Effort level and how much
    a section mentions move the real number either way.
    """
    history = store.digest_history(model)
    leaves = [h for h in history if h["kind"] == "leaf" and h["chars"]]
    branches = [h for h in history if h["kind"] == "branch"]
    blocks = [h for h in history if h["kind"] == "statblock" and h["chars"]]
    if len(leaves) >= MIN_SAMPLES:
        leaf_in = lambda chars: chars * sum(h["input_tokens"] for h in leaves) / sum(h["chars"] for h in leaves)
        leaf_ratio = sum(h["output_tokens"] for h in leaves) / sum(h["input_tokens"] for h in leaves)
    else:
        leaf_in = lambda chars: LEAF_OVERHEAD + chars / CHARS_PER_TOKEN
        leaf_ratio = LEAF_OUT_PER_IN
    branch_ratio = (
        sum(h["output_tokens"] for h in branches) / sum(h["input_tokens"] for h in branches)
        if len(branches) >= MIN_SAMPLES else BRANCH_OUT_PER_IN
    )
    if len(blocks) >= MIN_SAMPLES:
        block_in = lambda chars: chars * sum(h["input_tokens"] for h in blocks) / sum(h["chars"] for h in blocks)
        block_ratio = sum(h["output_tokens"] for h in blocks) / sum(h["input_tokens"] for h in blocks)
    else:
        block_in = lambda chars: STATBLOCK_OVERHEAD + chars / CHARS_PER_TOKEN
        block_ratio = STATBLOCK_OUT_PER_IN

    totals = {"calls": 0, "input_tokens": 0.0, "output_tokens": 0.0}

    def visible(n: DNode) -> float:
        """Tokens of n's output that its parent reads; adds n's own call to the totals if pending."""
        if n.output is not None:
            return len(json.dumps(n.output, ensure_ascii=False)) / CHARS_PER_TOKEN
        i = leaf_in(len(n.text)) if n.is_leaf else BRANCH_OVERHEAD + sum(visible(c) for c in n.children)
        o = i * (leaf_ratio if n.is_leaf else branch_ratio)
        totals["calls"] += 1
        totals["input_tokens"] += i
        totals["output_tokens"] += o
        return o * VISIBLE_SHARE

    if root is not None:
        visible(root)
        for n in root.stat_blocks:
            if n.output is None:
                i = block_in(len(n.text))
                totals["calls"] += 1
                totals["input_tokens"] += i
                totals["output_tokens"] += i * block_ratio
    i, o = round(totals["input_tokens"]), round(totals["output_tokens"])
    return {
        "model": model, "calls": totals["calls"], "input_tokens": i, "output_tokens": o,
        "usd": cost(model, i, o) if totals["calls"] else 0.0,
        "calibrated_on": len(leaves) if len(leaves) >= MIN_SAMPLES else 0,
    }


class _Stopped(Exception):
    """A queued call that found, when its turn came, that it's no longer wanted."""


MAX_ATTEMPTS = 3  # automatic tries per section before it waits to be asked again
RETRY_SECONDS = 60  # then 4 min, then give up


def run(
    db: Path | str,
    source_id: int,
    client: Any = None,
    workers: int = CONCURRENCY,
    model: str = MODEL,
    effort: str = EFFORT,
    on_progress: Callable[[dict[str, int]], None] | None = None,
    keep_going: Callable[[], bool] = lambda: True,
) -> dict[str, int]:
    """Digest whatever of a source isn't in the cache yet, bottom-up, then refresh its catalog.

    Idempotent: everything finished is in the cache, so running it again only
    does what's missing. A section that fails is retried on later runs with
    backoff, and after MAX_ATTEMPTS waits until someone asks again (see
    retry_failed). `keep_going` is checked between batches, so a caller can
    stop it early; calls already made still land in the cache.
    """
    store = Store(db)
    try:
        root = plan(store, source_id, model)
        stats = {"nodes": 0, "cached": 0, "digested": 0, "failed": 0, "waiting": 0,
                 "input_tokens": 0, "output_tokens": 0}
        if root is None:
            return stats
        nodes = root.calls()
        stats["nodes"] = len(nodes)
        stats["cached"] = sum(1 for n in nodes if n.output is not None)
        stats["waiting"] = sum(1 for n in nodes if n.error)  # gave up earlier; plan() marked them
        now = int(time.time())

        def backing_off(n: DNode) -> bool:
            f = store.digest_failure(n.key) if n.key else None
            if f is None:
                return False
            if f["retry_at"] is None or f["retry_at"] > now:
                n.error = n.error or f["error"]
                return True
            return False

        client = client or _client()

        def work(n: DNode) -> tuple[dict[str, Any], int, int]:
            with _IN_FLIGHT:  # shared by every source, so together they stay under the limit
                if not keep_going():  # checked when the call's turn comes, not just when it was queued
                    raise _Stopped
                return digest_node(client, n, model, effort)

        # Each section starts as soon as its subsections are done, not when the
        # whole level is: one slow call holds up only the sections above it.
        in_flight: dict[Any, DNode] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                found_cached = False
                for n in nodes:
                    if (n.output is not None or n.error is not None or n in in_flight.values()
                            or any(c.output is None for c in n.children)):
                        continue
                    if not n.is_leaf:
                        # Children just finished, so the branch's key is knowable now.
                        n.key = branch_key(n, model)
                        n.output = store.get_digest(n.key)
                        if n.output is not None:
                            store.set_digest_node(n.row_id, n.key)
                            stats["cached"] += 1
                            found_cached = True
                            continue
                    if backing_off(n):
                        stats["waiting"] += 1
                        continue
                    if not keep_going():
                        break
                    in_flight[pool.submit(work, n)] = n
                if not in_flight:
                    if found_cached:
                        continue  # a cached section may have made its parent ready; look again
                    break
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in done:
                    n = in_flight.pop(fut)
                    try:
                        output, i, o = fut.result()
                    except _Stopped:
                        n.error = "stopped"  # not scheduled again this run; still to do next time
                        continue
                    except Exception as err:  # keep going; the rest of the tree is still useful
                        n.error = str(err) or type(err).__name__
                        f = store.record_digest_failure(n.key, n.error, int(time.time()), MAX_ATTEMPTS, RETRY_SECONDS)
                        if f["retry_at"] is None:
                            n.error = f"{n.error} (gave up after {f['attempts']} tries)"
                        store.set_digest_node(n.row_id, n.key, n.error)
                        stats["failed"] += 1
                    else:
                        store.put_digest(n.key, n.kind, output, model, i, o)
                        store.clear_digest_failures([n.key])
                        store.set_digest_node(n.row_id, n.key)
                        n.output = output
                        stats["digested"] += 1
                        stats["input_tokens"] += i
                        stats["output_tokens"] += o
                    if on_progress:
                        on_progress(dict(stats))
        for n in nodes:
            if n.output is None and n.error is None and any(c.output is None for c in n.children):
                store.set_digest_node(n.row_id, None, "waiting on a failed subsection")
        if root.output is not None:
            with _MATERIALIZING:  # sources digested side by side apply one at a time
                _materialize(store, source_id, root)
        return stats
    finally:
        store.close()


def request(store: Store, source_id: int, model: str | None = None) -> str:
    """Ask for a source to be digested, giving failed sections another go; returns the model.

    With `model`, digests with that one (a source can have its own: a cheap
    model for a bestiary, a stronger one for an adventure); without, keeps
    the model the source is already set to, or else the configured one.
    Idempotent. A source digested with another model is planned against
    this one now (no API calls), so its sections show as still to do at
    once rather than when the loop gets to it. The caller wakes the loop.
    """
    src = store.source(source_id)
    model = model or (src["digest_model"] if src else None) or MODEL
    store.set_digest_target(source_id, model, EFFORT)
    plan(store, source_id, model=model)
    retry_failed(store, source_id, model=model)
    return model


def retry_failed(store: Store, source_id: int, model: str = MODEL) -> int:
    """Forget the failures of a source's sections, so the next run tries them again."""
    root = plan(store, source_id, model, save=False)
    keys = [n.key for n in root.calls() if n.key] if root else []
    store.clear_digest_failures(keys)
    return len(keys)


# -- the reconciliation loop ---------------------------------------------------------


class Reconciler(LeasedLoop):
    """Keeps every source's digest where its target says it should be.

    The desired state is on the source (digest_model, set by clicking
    Digest); the actual state is the digest cache. Each pass reads both
    from the database and runs whatever's missing, so the loop itself
    remembers nothing: a restart, a deploy, or a second loop (say, the CLI)
    just picks up where things stand. Clearing a source's target stops it
    after the calls in flight, which still land in the cache.
    """

    LEASE = "digest-loop"

    def __init__(self, db: Path | str, client_factory: Callable[[], Any] | None = None, workers: int = CONCURRENCY):
        super().__init__(db)
        self.client_factory = client_factory or _client
        self.workers = workers

    def _wanted(self, source_id: int, model: str) -> bool:
        s = Store(self.db)
        try:
            return any(t["id"] == source_id and t["model"] == model for t in s.digest_targets())
        finally:
            s.close()

    def pass_once(self) -> bool:
        if not self._replanned:
            self.replan()
        return self.reconcile_once()

    _replanned = False

    def replan(self) -> None:
        """Once per start: plan every source with a target again (no API calls).

        A new version of the digest (LEAF_VERSION, BRANCH_VERSION) or of how a
        catalog is applied to the world (world.APPLY_VERSION) changes the
        keys; planning saves the new ones, so changed sections get digested,
        and a cached catalog is applied again under the new rules.
        """
        s = Store(self.db)
        try:
            for t in s.digest_targets():
                if self.stopping:
                    return
                try:
                    plan(s, t["id"], t["model"])
                except Exception:  # one bad source shouldn't stop the rest
                    traceback.print_exc()
        finally:
            s.close()
        self._replanned = True

    def reconcile_once(self) -> bool:
        """One pass over every source with a target. True if it digested anything."""
        s = Store(self.db)
        try:
            pending = [t for t in s.digest_targets() if _unfinished(s, t["id"], t["model"])]
        finally:
            s.close()
        if not pending:
            return False

        def one(t: dict[str, Any]) -> bool:
            stats = run(
                self.db, t["id"], client=self.client_factory(), workers=self.workers,
                model=t["model"], effort=t["effort"] or EFFORT,
                # Before each call: still running, still wanted, and still ours (renewing the lease).
                keep_going=lambda: not self.stopping and self._wanted(t["id"], t["model"]) and self.holds_lease(),
            )
            return stats["digested"] > 0

        # All the sources that want work at once, so a big one doesn't hold up the rest.
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            return any(list(pool.map(one, pending)))


def _unfinished(store: Store, source_id: int, model: str) -> bool:
    """Whether a source may still have work: not every saved section is done with this model."""
    row = store.conn.execute(
        "SELECT count(n.id) AS total, count(d.key) AS done, sum(d.model != ?) AS other"
        " FROM digest_nodes n LEFT JOIN digests d ON d.key = n.digest_key WHERE n.source_id = ?",
        (model, source_id),
    ).fetchone()
    return row["total"] == 0 or row["done"] < row["total"] or bool(row["other"])
