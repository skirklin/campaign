"""Digest reference documents into a tree of summaries and a catalog of entities.

The document's section tree (see ingest.py) is reduced bottom-up:

- A **leaf** is a section's own text. Claude summarizes it and extracts the
  NPCs, monsters, locations, items, quests, factions and lore it mentions,
  with aliases, pages, facts and stat blocks.
- A **branch** combines its children's digests, never raw text. Claude
  writes a summary of the whole section and says which of the children's
  entities are the same thing under different names. The merge itself is
  done in code.

The root's entities become the source's catalog, which is applied to the
campaign's world model as facts true from session 0 (see world.py).

Caching works like a Merkle tree. Every node's output is stored under a
hash of exactly what produced it:

    leaf key   = H("leaf",   LEAF_VERSION,   model, title, text)
    branch key = H("branch", BRANCH_VERSION, model, title, H(child output) for each child)

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

import anthropic
from pydantic import BaseModel

from .db import Store

LEAF_VERSION = 1
BRANCH_VERSION = 1
MODEL = os.environ.get("CAMPAIGN_DIGEST_MODEL", os.environ.get("CAMPAIGN_MODEL", "claude-opus-5-5"))
EFFORT = os.environ.get("CAMPAIGN_DIGEST_EFFORT", "medium")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Haiku has no server-side refusal fallback; a refused section just fails.
NO_FALLBACK_PREFIXES = ("claude-haiku-",)

MAX_LEAF_CHARS = 40_000  # ~10k tokens; longer sections are split into parts
MIN_LEAF_CHARS = 6_000  # shorter sibling sections are packed into one call


# -- what Claude returns -------------------------------------------------------


class StatBlock(BaseModel):
    armor_class: str
    hit_points: str
    speed: str
    strength: int | None
    dexterity: int | None
    constitution: int | None
    intelligence: int | None
    wisdom: int | None
    charisma: int | None
    challenge: str
    traits: list[str]
    actions: list[str]
    other: list[str]  # saves, skills, senses, languages, reactions, legendary actions


class Entity(BaseModel):
    name: str
    kind: Literal["npc", "monster", "location", "item", "quest", "faction", "lore"]
    aliases: list[str]
    summary: str
    pages: list[int]
    facts: list[str]
    stat_block: StatBlock | None


class LeafOutput(BaseModel):
    summary: str
    entities: list[Entity]


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
  - monster: creature types with a stat block here, or a notable encounter
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
  charmed"); and stat_block only when the full stat block is in this text.

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
- merges: groups of entity ids that are the same thing under different names
  or kinds (for example "Morgantha" and "the hag of Old Bonegrinder", or one
  item described in two rooms). For each group give the best canonical name
  (the book's proper name) and a 1-2 sentence summary combining what's known.
  Only merge when you're confident. Leave everything else out.
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

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def walk(self) -> Iterator[DNode]:
        yield self
        for c in self.children:
            yield from c.walk()


def _pages(items: list[Any]) -> tuple[int | None, int | None]:
    starts = [i.page_start if isinstance(i, DNode) else i["page"] for i in items]
    ends = [i.page_end if isinstance(i, DNode) else i["page"] for i in items]
    starts = [p for p in starts if p is not None]
    ends = [p for p in ends if p is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _render(chunks: list[dict[str, Any]]) -> str:
    """Section text for Claude, with page markers and the local headings."""
    parts: list[str] = []
    page = heading = None
    for c in chunks:
        if c["page"] is not None and c["page"] != page:
            page = c["page"]
            parts.append(f"[p. {page}]")
        local = c["heading"].split(" > ")[-1] if c["heading"] else ""
        if local and local != heading:
            heading = local
            parts.append(f"### {local}")
        parts.append(c["text"])
    return "\n\n".join(parts)


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


# -- cache keys ------------------------------------------------------------------


def _h(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def output_hash(output: dict[str, Any]) -> str:
    return _h(json.dumps(output, sort_keys=True, ensure_ascii=False))


def leaf_key(node: DNode, model: str) -> str:
    return _h("leaf", str(LEAF_VERSION), model, node.title, node.text)


def branch_key(node: DNode, model: str) -> str:
    assert all(c.output is not None for c in node.children)
    return _h("branch", str(BRANCH_VERSION), model, node.title, *(output_hash(c.output) for c in node.children))


def _resolve(store: Store, node: DNode, model: str) -> None:
    """Fill in keys and cached outputs bottom-up, as far as the cache reaches."""
    for c in node.children:
        _resolve(store, c, model)
    if node.is_leaf:
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
    return {
        "name": name,
        "kind": group[0]["kind"],
        "aliases": [a for a in aliases if _norm(a) != _norm(name)],
        "summary": summary or max((e["summary"] for e in group), key=len),
        "pages": sorted({p for e in group for p in e["pages"]}),
        "facts": _dedupe([f for e in group for f in e["facts"]]),
        "stat_block": next((e["stat_block"] for e in group if e.get("stat_block")), None),
    }


def premerge(children: list[DNode]) -> list[dict[str, Any]]:
    """Children's entities with exact (kind, name) duplicates already combined."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in children:
        for e in c.output["entities"]:
            groups.setdefault((e["kind"], _norm(e["name"])), []).append(e)
    return [combine(g) if len(g) > 1 else g[0] for g in groups.values()]


def apply_merges(candidates: list[dict[str, Any]], merges: list[Merge]) -> list[dict[str, Any]]:
    index = {f"e{i}": i for i in range(len(candidates))}
    merged_into: dict[int, int] = {}  # candidate -> first candidate of its group
    groups: dict[int, tuple[list[int], Merge]] = {}
    for m in merges:
        ids = [index[i] for i in dict.fromkeys(m.ids) if i in index and index[i] not in merged_into]
        if len(ids) < 2:
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
    fallback: dict[str, Any] = {}
    if not model.startswith(NO_FALLBACK_PREFIXES):
        fallback = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=fmt,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        **fallback,
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


def digest_node(client: Any, node: DNode, model: str, effort: str) -> tuple[dict[str, Any], int, int]:
    if node.is_leaf:
        parsed, i, o = _call(client, LEAF_SYSTEM, leaf_prompt(node), LeafOutput, model, effort)
        entities = [e.model_dump() for e in parsed.entities]
        # The same name twice within one section is the same thing.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for e in entities:
            groups.setdefault((e["kind"], _norm(e["name"])), []).append(e)
        entities = [combine(g) if len(g) > 1 else {**g[0], "pages": sorted(set(g[0]["pages"]))} for g in groups.values()]
        return {"summary": parsed.summary, "entities": entities}, i, o
    candidates = premerge(node.children)
    parsed, i, o = _call(client, BRANCH_SYSTEM, branch_prompt(node, candidates), BranchOutput, model, effort)
    return {"summary": parsed.summary, "entities": apply_merges(candidates, parsed.merges)}, i, o


# -- planning and running ------------------------------------------------------------


def _materialize(store: Store, source_id: int, root: DNode) -> None:
    """Apply the book's catalog to its campaign's world model as the state before play."""
    from .world import World

    campaign = store.campaign_by_id(store.source(source_id)["campaign_id"])
    World(store, campaign).apply_book(source_id, root.output["entities"], key=root.key)


def plan(store: Store, source_id: int, model: str = MODEL) -> DNode | None:
    """Build the digest tree for a source and match it against the cache. No API calls.

    Saves the tree so the UI can show progress, and if the root is already
    cached (an unchanged re-upload) refreshes the entity catalog right away.
    """
    src = store.source(source_id)
    nodes, chunks = store.source_structure(source_id)
    root = build_tree(src["title"], nodes, chunks) if src else None
    if root is None:
        store.save_digest_plan(source_id, [])
        return None
    _resolve(store, root, model)

    rows: list[dict[str, Any]] = []
    order: list[DNode] = []

    def flatten(n: DNode, parent: int | None, depth: int) -> None:
        idx = len(rows)
        rows.append({
            "parent": parent, "depth": depth, "title": n.title, "page_start": n.page_start,
            "page_end": n.page_end, "is_leaf": n.is_leaf, "chars": len(n.text), "key": n.key,
        })
        order.append(n)
        for c in n.children:
            flatten(c, idx, depth + 1)

    flatten(root, None, 0)
    for n, row_id in zip(order, store.save_digest_plan(source_id, rows)):
        n.row_id = row_id
    if root.output is not None:
        _materialize(store, source_id, root)
    return root


def pending_calls(root: DNode | None) -> int:
    return 0 if root is None else sum(1 for n in root.walk() if n.output is None)


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
LEAF_OVERHEAD = 1500  # system prompt + output schema, in tokens
LEAF_OUT_PER_IN = 0.5  # thinking + summary + entities, per input token
BRANCH_OVERHEAD = 1200
BRANCH_OUT_PER_IN = 0.3
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
    i, o = round(totals["input_tokens"]), round(totals["output_tokens"])
    return {
        "model": model, "calls": totals["calls"], "input_tokens": i, "output_tokens": o,
        "usd": cost(model, i, o) if totals["calls"] else 0.0,
        "calibrated_on": len(leaves) if len(leaves) >= MIN_SAMPLES else 0,
    }


def run(
    db: Path | str,
    source_id: int,
    client: Any = None,
    workers: int = 4,
    model: str = MODEL,
    effort: str = EFFORT,
    on_progress: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, int]:
    """Digest everything not already cached, bottom-up, and refresh the entity catalog.

    Safe to interrupt and rerun: finished nodes are in the cache, so a rerun
    resumes where the last one stopped. Opens its own database connection so
    it can run on a background thread.
    """
    store = Store(db)
    try:
        root = plan(store, source_id, model)
        stats = {"nodes": 0, "cached": 0, "digested": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0}
        if root is None:
            return stats
        nodes = list(root.walk())
        stats["nodes"] = len(nodes)
        stats["cached"] = sum(1 for n in nodes if n.output is not None)
        client = client or anthropic.Anthropic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                ready = [
                    n for n in nodes
                    if n.output is None and n.error is None and all(c.output is not None for c in n.children)
                ]
                if not ready:
                    break
                futures = {}
                for n in ready:
                    if not n.is_leaf:
                        # Children just finished, so the branch's key is knowable now.
                        n.key = branch_key(n, model)
                        n.output = store.get_digest(n.key)
                        if n.output is not None:
                            store.set_digest_node(n.row_id, n.key)
                            stats["cached"] += 1
                            continue
                    futures[pool.submit(digest_node, client, n, model, effort)] = n
                for fut in as_completed(futures):
                    n = futures[fut]
                    try:
                        output, i, o = fut.result()
                    except Exception as err:  # keep going; the rest of the tree is still useful
                        n.error = str(err) or type(err).__name__
                        store.set_digest_node(n.row_id, None, n.error)
                        stats["failed"] += 1
                    else:
                        store.put_digest(n.key, "leaf" if n.is_leaf else "branch", output, model, i, o)
                        store.set_digest_node(n.row_id, n.key)
                        n.output = output
                        stats["digested"] += 1
                        stats["input_tokens"] += i
                        stats["output_tokens"] += o
                    if on_progress:
                        on_progress(dict(stats))
        for n in nodes:
            if n.output is None and n.error is None:
                store.set_digest_node(n.row_id, None, "waiting on a failed subsection")
        if root.output is not None:
            _materialize(store, source_id, root)
        return stats
    finally:
        store.close()
