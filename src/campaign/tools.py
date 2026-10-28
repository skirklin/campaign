"""Tools over the campaign store, shared by the built-in agent and the MCP server.

`build_tools` returns plain functions bound to one campaign. Docstrings are
the tool descriptions the model sees, so they say when to use each tool, not
just what it does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from .charsheet import CharacterSheet, merge_patch
from .db import Campaign, Store

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


@dataclass
class ToolContext:
    store: Store
    campaign: Campaign
    session_id: int | None = None  # set while ingesting a session; tags every write


def build_tools(ctx: ToolContext, writable: bool = True) -> list[Callable[..., str]]:
    store, campaign = ctx.store, ctx.campaign

    # -- reference material --------------------------------------------------

    def list_sources() -> str:
        """List the reference documents loaded for this campaign (modules, notes, transcripts) with their page ranges."""
        return _json(store.sources(campaign))

    def search_reference(query: str, source: str | None = None, limit: int = 8) -> str:
        """Full-text search over the campaign's reference material. Returns chunk ids, pages, headings and snippets.

        Use specific terms: creature or NPC names, place names, item names,
        distinctive phrases. Search several phrasings if the first finds
        nothing. Snippets are partial: call read_reference on a hit before
        quoting stats, DCs or rewards.

        Args:
            query: Words to search for, e.g. "Strahd von Zarovich lair actions".
            source: Restrict to one source title from list_sources.
            limit: Maximum hits to return.
        """
        hits = store.search(campaign, query, limit=limit, source=source)
        return _json(hits) if hits else "No matches. Try other names or fewer words."

    def read_reference(chunk_id: int, before: int = 0, after: int = 1) -> str:
        """Read the full text of a reference chunk, plus neighbouring chunks. Stat blocks and room descriptions often continue into the next chunk.

        Args:
            chunk_id: Chunk id from search_reference.
            before: How many preceding chunks to include.
            after: How many following chunks to include.
        """
        rows = store.read_chunks(campaign, chunk_id, before=before, after=after)
        return _json(rows) if rows else f"No chunk {chunk_id}."

    def read_pages(source: str, first_page: int, last_page: int) -> str:
        """Read whole pages of a PDF source, e.g. to see an entire encounter or appendix entry.

        Args:
            source: Source title from list_sources.
            first_page: First page number (as printed in search results).
            last_page: Last page number, inclusive. Keep ranges to a few pages.
        """
        rows = store.read_pages(campaign, source, first_page, min(last_page, first_page + 5))
        return _json(rows) if rows else "Nothing on those pages."

    # -- campaign state ------------------------------------------------------

    def list_sessions() -> str:
        """List played sessions with their summaries, oldest first."""
        return _json(store.sessions(campaign))

    def find_entities(kind: str | None = None, name: str | None = None, status: str | None = None) -> str:
        """Look up tracked campaign state: NPCs, quests, locations, items, factions and so on.

        Call with no arguments for an overview of everything tracked. Check
        here before creating an entity so you update the existing one rather
        than making a duplicate under a slightly different name.

        Args:
            kind: One of npc, pc, location, quest, item, faction, monster, lore.
            name: Substring of the name or summary.
            status: Exact status, e.g. "active", "completed", "dead".
        """
        rows = store.find_entities(campaign, kind=kind, name=name, status=status)
        if not rows:
            return "Nothing tracked matches."
        return _json(
            [{k: r[k] for k in ("id", "kind", "name", "status", "summary")} for r in rows]
        )

    def get_entity(kind: str, name: str) -> str:
        """Full record for one entity: summary, structured data, and how it has changed over sessions.

        Args:
            kind: Entity kind.
            name: Exact entity name.
        """
        e = store.get_entity(campaign, kind, name)
        if e is None:
            return f"No {kind} named {name!r}."
        history = store.entity_history(e["id"])
        e["history"] = [
            {"session": h["session"], "reason": h["reason"], "status": h["snapshot"]["status"]}
            for h in history
            if h["reason"]
        ]
        return _json(e)

    def recent_journal(session: int | None = None, limit: int = 40) -> str:
        """Read the campaign journal: the ordered log of what has happened.

        Args:
            session: Only events from this session number.
            limit: Most recent N events.
        """
        rows = store.events(campaign, session_number=session, limit=limit)
        return _json(rows) if rows else "The journal is empty."

    def record_entity(
        kind: str,
        name: str,
        reason: str,
        status: str | None = None,
        summary: str | None = None,
        data: dict[str, Any] | None = None,
        party_knows: bool | None = None,
    ) -> str:
        """Create or update a tracked entity. Only the fields you pass change.

        Write `summary` as the current truth in 1-3 sentences (rewrite it, don't
        append). Put structured detail in `data`, which merges key by key
        (a null value deletes a key). Useful keys: for quests "giver",
        "objectives", "reward", "location"; for NPCs "disposition", "location",
        "wants", "secrets"; for items "holder", "properties".

        Args:
            kind: One of npc, pc, location, quest, item, faction, monster, lore.
            name: Canonical name. Reuse the existing name when updating.
            reason: Why this changed, e.g. "party accepted the quest from Ismark". Shown in history.
            status: Short state, e.g. "active", "completed", "failed", "alive", "dead", "missing".
            summary: Current 1-3 sentence description.
            data: Structured fields to merge.
            party_knows: False for things the players have not discovered yet.
        """
        e = store.upsert_entity(
            campaign,
            kind,
            name,
            status=status,
            summary=summary,
            data=data,
            party_knows=party_knows,
            session_id=ctx.session_id,
            reason=reason,
        )
        return f"Saved {e['kind']} {e['name']!r} (id {e['id']}, status {e['status'] or '-'})."

    def add_journal_entry(text: str, entities: list[str] | None = None) -> str:
        """Append an event to the campaign journal, e.g. "The party killed the hag Morgantha and freed the children."

        One entry per meaningful event, in the order they happened.

        Args:
            text: What happened, in past tense, naming who was involved.
            entities: Names of tracked entities involved.
        """
        ids = []
        for name in entities or []:
            matches = [e for e in store.find_entities(campaign, name=name) if e["name"].lower() == name.lower()]
            ids += [m["id"] for m in matches]
        event_id = store.add_event(campaign, text, session_id=ctx.session_id, entity_ids=ids)
        return f"Logged event {event_id}."

    # -- characters ----------------------------------------------------------

    def get_character(name: str) -> str:
        """Read a player character's sheet, including computed modifiers, saves, skills and spell DC.

        Args:
            name: Character name.
        """
        raw = store.character(name)
        if raw is None:
            known = store.characters()
            return f"No character {name!r}. Known: {', '.join(known) or 'none'}."
        sheet = CharacterSheet.model_validate(raw)
        return _json({"sheet": raw, "derived": sheet.derived()})

    def update_character(name: str, patch: dict[str, Any], reason: str) -> str:
        """Change a character sheet with a JSON merge patch; creates the character if new.

        Objects merge recursively, a null deletes a key, and lists are
        replaced whole, so read the sheet first and send the complete new
        list when changing inventory, spells, attacks or resources.
        Examples: {"hp": {"current": 17}}, {"currency": {"gp": 245}},
        {"conditions": ["poisoned"]}.

        Args:
            name: Character name.
            patch: Merge patch against the current sheet.
            reason: What happened, e.g. "took 9 damage from the ghoul".
        """
        current = store.character(name) or {"name": name}
        updated = merge_patch(current, patch)
        try:
            sheet = CharacterSheet.model_validate(updated)
        except ValidationError as err:
            return f"Rejected, sheet would be invalid: {err}"
        store.save_character(sheet.name, sheet.model_dump(), campaign=campaign, reason=reason)
        return f"Updated {sheet.name}: {reason}"

    readers = [
        list_sources,
        search_reference,
        read_reference,
        read_pages,
        list_sessions,
        find_entities,
        get_entity,
        recent_journal,
        get_character,
    ]
    writers = [record_entity, add_journal_entry, update_character]
    return readers + (writers if writable else [])

