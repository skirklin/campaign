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

from .charsheet import CharacterSheet, PartyStash, merge_patch
from .db import Campaign, Store
from .world import World


def _section(n: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": n["id"], "title": n["title"], "pages": [n["page_start"], n["page_end"]],
        "summary": n["summary"] or "(not digested yet)",
    }


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


@dataclass
class ToolContext:
    store: Store
    campaign: Campaign
    session: int | None = None  # the session being ingested; otherwise the latest one
    run_kind: str = "chat"
    run_id: int | None = None
    user_id: int | None = None  # who's asking, when signed in: owns the characters they create

    @property
    def world(self) -> World:
        return World(self.store, self.campaign)

    @property
    def clock(self) -> int:
        """The session that writes are dated to."""
        return self.session if self.session is not None else self.store.current_session(self.campaign)

    def run(self) -> int:
        """This conversation's or ingest's run, started on its first write."""
        if self.run_id is None:
            self.run_id = self.world.start_run(self.run_kind, session=self.clock)
        return self.run_id


def build_tools(ctx: ToolContext, writable: bool = True) -> list[Callable[..., str]]:
    store, campaign, world = ctx.store, ctx.campaign, ctx.world

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

    def _visible(source_id: int) -> bool:
        return any(src["id"] == source_id for src in store.sources(campaign)) and not (
            campaign.spoiler_safe and store.source(source_id)["dm_only"]
        )

    def outline(section_id: int | None = None, source: str | None = None) -> str:
        """Browse the digested outline of the reference material: section summaries with page ranges.

        With no arguments: each book's summary and its top-level sections.
        With section_id: that section's summary and its subsections. A
        section without subsections gives its pages; read them with
        read_pages for exact text.

        Args:
            section_id: A section id from an earlier outline result.
            source: Show one source's top level (title from list_sources).
        """
        if section_id is None:
            books = [o for o in store.source_overviews(campaign) if source in (None, o["source"])]
            if not books:
                return "Nothing has been digested yet; use search_reference instead."
            return _json([
                {"source": b["source"], "summary": b["summary"],
                 "sections": [_section(s) for s in b["sections"]]}
                for b in books
            ])
        source_id = store.digest_node_source(section_id)
        if source_id is None or not _visible(source_id):
            return f"No section {section_id}."
        nodes = store.digest_outline(source_id)
        node = next(n for n in nodes if n["id"] == section_id)
        out = _section(node) | {"source": store.source(source_id)["title"]}
        out["subsections"] = [_section(n) for n in nodes if n["parent_id"] == section_id]
        return _json(out)

    # -- campaign state ------------------------------------------------------

    def find_entities(kind: str | None = None, query: str | None = None, in_play_only: bool = False) -> str:
        """List people, places and things in the campaign with their current status and location.

        Call with no arguments for an overview. Check here before recording
        facts about something new, so you add to the existing entity rather
        than creating a duplicate under a slightly different name.

        Args:
            kind: npc, pc, monster, location, item, quest, faction or lore.
            query: Part of a name, alias or anything said about it.
            in_play_only: Only things the table has interacted with, not everything from the book.
        """
        rows = world.list(kind=kind, q=query, in_play=in_play_only)
        if not rows:
            return "Nothing matches."
        return _json([
            {k: r[k] for k in ("kind", "name", "aliases", "current", "headline")} for r in rows[:200]
        ])

    def lookup(name: str, as_of_session: int | None = None) -> str:
        """Everything known about a person, creature, place, item or quest: true now, true before, and possible.

        Matches names and aliases ("the hag", "the burgomaster"), then partial
        names, then what's been said about things. Each fact shows the
        sessions it held for (0 = before play, e.g. "3-7" or "5-" for still
        true) and its source (a book page, a session, chat). Book material is
        the starting state; facts from play supersede it.

        Args:
            name: A name, title or description, e.g. "Morgantha" or "the windmill hag".
            as_of_session: See the world as it was at the end of this session instead of now.
        """
        hits = world.lookup(name, as_of=as_of_session)
        return _json(hits) if hits else "No match. Try find_entities or search_reference."

    def timeline(name: str) -> str:
        """How one entity changed session by session: facts beginning and ending, and the events it was in.

        Args:
            name: Name or alias.
        """
        e = world.resolve(name)
        if e is None or world.view(e["id"]) is None:
            return f"No entity called {name!r}."
        return _json({"name": e["name"], "timeline": world.timeline(e["id"])})

    def list_sessions() -> str:
        """List played sessions with their recaps, oldest first."""
        return _json(store.sessions(campaign))

    def recent_journal(session: int | None = None, limit: int = 40) -> str:
        """Read the campaign journal: the ordered log of what happened.

        Args:
            session: Only events from this session number.
            limit: Most recent N events.
        """
        rows = world.events(session=session, limit=limit)
        return _json(rows) if rows else "The journal is empty."

    def record_fact(
        entity: str,
        statement: str,
        kind: str | None = None,
        attribute: str | None = None,
        value: str | None = None,
        aliases: list[str] | None = None,
        party_knows: bool = True,
        modality: str = "fact",
    ) -> str:
        """Record something true about a person, place or thing, from the current session on.

        Write `statement` as a standalone sentence ("Ireena is staying at the
        Blue Water Inn"). For a property that has one value at a time, also
        give `attribute` and `value`: recording a new value ends the old one,
        which stays in the history. Common attributes: status (alive, dead,
        missing, active, completed, failed), location, holder (who has an
        item), disposition (toward the party), leader, allegiance, condition.
        Leave `attribute` out for everything else; use end_fact when such a
        statement stops being true.

        Args:
            entity: Name or alias of the entity. Created if new, in which case `kind` is required.
            statement: The fact as a sentence.
            kind: npc, pc, monster, location, item, quest, faction or lore. Needed only for new entities.
            attribute: Single-valued property this sets, if any.
            value: Short value for the attribute, e.g. "dead" or "Vallaki".
            aliases: Other names this entity goes by, to remember.
            party_knows: False if this is true but the party hasn't learned it.
            modality: "fact", or "rumor" for unconfirmed claims, or "possible" for plans and threats that haven't happened.
        """
        e = world.resolve(entity, kind)
        if e is None:
            if not kind:
                return f"No entity called {entity!r}. Check find_entities, or pass kind to create it."
            e = world.ensure_entity(entity, kind)
        if modality not in ("fact", "rumor", "possible"):
            return "modality must be fact, rumor or possible."
        run = ctx.run()
        for alias in aliases or []:
            world.add_alias(e["id"], alias, run)
        r = world.record(e["id"], statement, run, ctx.clock, attribute=attribute, value=value,
                         modality=modality, party_knows=party_knows)
        ended = f"; ended the previous {attribute}" if r["ended"] else ""
        return f"Recorded fact {r['id']} about {e['name']} as of session {ctx.clock}{ended}."

    def end_fact(fact_id: int) -> str:
        """Mark a fact as no longer true as of the current session. It stays in the history.

        Args:
            fact_id: The fact_id shown by lookup.
        """
        ok = world.end_fact(fact_id, ctx.run(), ctx.clock)
        return f"Ended fact {fact_id} at session {ctx.clock}." if ok else f"No open fact {fact_id}."

    def party_learned(fact_id: int) -> str:
        """Note that the party has now learned an existing fact (for example a secret from the book).

        Args:
            fact_id: The fact_id shown by lookup.
        """
        ok = world.party_learned(fact_id, ctx.clock)
        return f"The party knows fact {fact_id} as of session {ctx.clock}." if ok else "Nothing to change."

    def add_journal_entry(text: str, entities: list[str] | None = None) -> str:
        """Append an event to the journal, e.g. "The party killed Morgantha and freed the children."

        One entry per meaningful event, in the order they happened. Record
        the resulting changes (Morgantha's status, the children's location)
        with record_fact as well.

        Args:
            text: What happened, in past tense, naming who was involved.
            entities: Names or aliases of the entities involved.
        """
        ids = [e["id"] for e in (world.resolve(n) for n in entities or []) if e]
        event_id = world.add_event(text, ctx.clock, ctx.run(), ids)
        return f"Logged event {event_id} in session {ctx.clock}."

    # -- characters ----------------------------------------------------------

    def _find_character(name: str) -> dict[str, Any] | None:
        # This campaign's character by its sheet's name, or by any name its world entity answers to.
        if found := store.find_character(name, campaign):
            return found
        if (e := world.resolve(name, "pc")) and (linked := store.character_for_entity(e["id"])):
            return store.character(linked["id"])
        return None

    def get_character(name: str) -> str:
        """Read a player character's sheet, including computed modifiers, saves, skills and spell DC.

        Args:
            name: Character name.
        """
        found = _find_character(name)
        if found is None:
            known = [c["name"] for c in store.characters(campaign_ids=[campaign.id])]
            return f"No character {name!r} in this campaign. Known: {', '.join(known) or 'none'}."
        sheet = CharacterSheet.model_validate(found["sheet"])
        return _json({"sheet": found["sheet"], "derived": sheet.derived()})

    def update_character(name: str, patch: dict[str, Any], reason: str) -> str:
        """Change a character sheet with a JSON merge patch; creates the character if new.

        Objects merge recursively, a null deletes a key, and lists are
        replaced whole, so read the sheet first and send the complete new
        list when changing inventory, spells, attacks or resources.
        Examples: {"hp": {"current": 17}}, {"currency": {"gp": 245}},
        {"conditions": ["poisoned"]}.

        Numbers are built from parts and summed when shown, so record the
        parts, never a total: ability scores (not modifiers); each attack's
        ability ("str", "dex", "finesse", "spell" or "none"), proficient,
        magic bonus and damage parts (dice, type, add_ability for the part
        that adds the ability modifier); AC as a formula (base, abilities,
        max_dex); HP as hp.base, the hit-dice total without Constitution.
        Anything else that adds to a number is a bonus {target, amount,
        source}: on the sheet for features and effects (switch one off with
        active: false, e.g. Rage between fights), or on the item that grants
        it (it counts while equipped, and attuned if requires_attunement).
        An amount can be a number or "prof", "level", an ability ("cha"),
        or a multiple like "2*level".

        Args:
            name: Character name.
            patch: Merge patch against the current sheet.
            reason: What happened, e.g. "took 9 damage from the ghoul".
        """
        found = _find_character(name)
        updated = merge_patch(found["sheet"] if found else {"name": name}, patch)
        try:
            sheet = CharacterSheet.model_validate(updated)
        except ValidationError as err:
            return f"Rejected, sheet would be invalid: {err}"
        if found:
            store.update_character(found["id"], sheet.model_dump(), reason=reason)
        else:
            store.create_character(sheet.model_dump(), campaign_id=campaign.id, owner_id=ctx.user_id, reason=reason)
        return f"Updated {sheet.name}: {reason}"

    def create_character(sheet: CharacterSheet, reason: str = "created") -> str:
        """Add a new player character to this campaign from a complete sheet.

        Use this to enter a character from a paper sheet, a scan, or a
        description: fill in every field the source shows (ability scores,
        not modifiers; proficient saves and skills by their keys, e.g. "wis",
        "sleight of hand"; spell slots and other limited uses as resources).
        The sheet becomes a character in the party, owned by whoever is
        using this tool, and the world model's player character of that name.

        Numbers are built from parts and summed when shown, so record the
        parts, never a total: ability scores (not modifiers); each attack's
        ability ("str", "dex", "finesse", "spell" or "none"), proficient,
        magic bonus and damage parts (dice, type, add_ability for the part
        that adds the ability modifier); AC as a formula (base, abilities,
        max_dex); HP as hp.base, the hit-dice total without Constitution.
        Anything else that adds to a number is a bonus {target, amount,
        source}: on the sheet for features and effects (switch one off with
        active: false, e.g. Rage between fights), or on the item that grants
        it (it counts while equipped, and attuned if requires_attunement).
        An amount can be a number or "prof", "level", an ability ("cha"),
        or a multiple like "2*level".

        Args:
            sheet: The whole character sheet.
            reason: Where it came from, e.g. "entered from a scan of the paper sheet".
        """
        if _find_character(sheet.name):
            return f"{sheet.name} already exists in this campaign; use update_character to change the sheet."
        character_id = store.create_character(sheet.model_dump(), campaign_id=campaign.id, owner_id=ctx.user_id,
                                              reason=reason)
        return _json({"created": character_id, "derived": sheet.derived()})

    def get_party_stash() -> str:
        """Read the party stash: items and coin the party holds in common, not on anyone's sheet."""
        return _json(PartyStash.model_validate(store.party_stash(campaign.id)).model_dump())

    def update_party_stash(patch: dict[str, Any], reason: str) -> str:
        """Change the party stash with a JSON merge patch.

        Use it for loot the party takes but nobody claims, and the shared
        purse. Lists are replaced whole, so read the stash first and send the
        complete new item list. Examples: {"currency": {"gp": 340}},
        {"items": [{"name": "Potion of healing", "qty": 3}]}.

        Args:
            patch: Merge patch against the current stash.
            reason: What happened, e.g. "looted the ghoul nest".
        """
        updated = merge_patch(store.party_stash(campaign.id) or PartyStash().model_dump(), patch)
        try:
            stash = PartyStash.model_validate(updated)
        except ValidationError as err:
            return f"Rejected, stash would be invalid: {err}"
        store.update_party_stash(campaign.id, stash.model_dump(), reason=reason, user_id=ctx.user_id)
        return f"Updated the party stash: {reason}"

    readers = [
        list_sources,
        outline,
        lookup,
        find_entities,
        timeline,
        search_reference,
        read_reference,
        read_pages,
        list_sessions,
        recent_journal,
        get_character,
        get_party_stash,
    ]
    writers = [record_fact, end_fact, party_learned, add_journal_entry, update_character, create_character,
               update_party_stash]
    return readers + (writers if writable else [])
