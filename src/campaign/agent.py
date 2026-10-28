"""Claude-driven Q&A and session ingestion over the campaign tools."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
from anthropic import beta_tool

from .db import Store
from .tools import ToolContext, build_tools

MODEL = os.environ.get("CAMPAIGN_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ASK_SYSTEM = """\
You are the campaign assistant for the D&D campaign "{name}". {role_line}

You have tools over two kinds of knowledge:
- Reference material (published modules, homebrew notes, past session
  transcripts). It has been digested into an outline of section summaries
  and a catalog of NPCs, monsters, places, items and quests with their
  aliases, pages and stat blocks. Use lookup for anything named or
  described ("the hag", "the vampire's castle"), outline to find your way
  into a part of the book, and search_reference, read_reference and
  read_pages for the exact text.
- Campaign state (who is alive, which quests are open, what happened when):
  find_entities, get_entity, recent_journal, list_sessions.
Plus player character sheets: get_character, update_character.

Answer from the tools, not from memory of the published adventure: this
table may have changed things, and editions differ. Before giving a stat
block, DC, treasure or reward, read the full chunk rather than a search
snippet, and cite the source and page. If the material doesn't cover
something, say so plainly and then offer a suggestion, clearly labelled as
yours.

When the user tells you something that happened or changed ("we killed the
hag", "I picked up a +1 shield", "Ireena now trusts us"), record it with the
write tools so the campaign state stays current, and say what you recorded.
{spoiler_rules}
Keep answers tight and table-ready: the user is often mid-session.
{overview}"""

DM_ROLE = "The user is the Dungeon Master and may see everything, including module secrets."
PLAYER_ROLE = "The user is a player in this campaign, not the DM."
PLAYER_SPOILERS = """
Spoiler rules: the user is a player, so only discuss what their party has
learned. Material marked DM-only is already hidden from your tools; don't
fill the gap from your own knowledge of the published adventure, and don't
hint at secrets, upcoming encounters or NPC motives the party hasn't
uncovered. Mark anything the party hasn't discovered with party_knows=false.
"""

INGEST_SYSTEM = """\
You keep the campaign record for the D&D campaign "{name}". {role_line}

You are processing session {number}. The transcript follows in the user
message. Speech-to-text transcripts are noisy: names are often misspelled,
speakers are unlabelled or wrong, and much of the talk is table chatter,
rules lookups and jokes. Record only what happened in the game.

Work in this order:
1. Get your bearings: list_sessions, find_entities (no arguments) and
   recent_journal, so you know the existing names and open threads.
2. Read the whole transcript.
3. Resolve names against what's tracked and the reference material (the
   catalog below, and lookup) so "Strad", "Straud" and "the count" all become the
   canonical "Strahd von Zarovich". Reuse existing entity names exactly.
4. Record what changed: new or updated NPCs, locations, quests (with giver,
   objectives and reward when stated or found in the reference), items and
   who holds them, faction standing. Update status when something is
   resolved. Log the session's key events with add_journal_entry, in order,
   roughly 5-20 entries.
5. Update a character sheet only if it already exists and the transcript
   states the change outright (gold found, items gained, a level up). Say
   in `reason` where it came from.
6. Finish by calling finish_session exactly once with a short title and a
   summary a player could read before next session.

Prefer fewer, accurate writes over guesses. If something is ambiguous, leave
it out and mention it in your final message.
{spoiler_rules}{catalog}"""


@dataclass
class Agent:
    """A tool-using conversation over one campaign."""

    store: Store
    ctx: ToolContext
    system: str
    effort: str = "medium"
    extra_tools: list[Callable[..., str]] = field(default_factory=list)
    writable: bool = True
    on_text: Callable[[str], None] = lambda s: (sys.stdout.write(s), sys.stdout.flush())
    on_tool: Callable[[str, dict[str, Any]], None] = lambda name, args: None
    messages: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.client = anthropic.Anthropic()
        fns = build_tools(self.ctx, writable=self.writable) + self.extra_tools
        self.tools = {f.__name__: beta_tool(f, eager_input_streaming=True) for f in fns}

    def _run_tool(self, block: Any) -> dict[str, Any]:
        self.on_tool(block.name, block.input)
        tool = self.tools.get(block.name)
        try:
            if tool is None:
                raise ValueError(f"unknown tool {block.name}")
            # call() validates the (possibly eagerly streamed) input against the schema.
            content, is_error = tool.call(block.input), False
        except Exception as err:  # report back so the model can correct itself
            content, is_error = f"Error: {err}", True
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": content,
            "is_error": is_error,
        }

    @staticmethod
    def _echoable(content: list[Any]) -> list[Any]:
        """Drop model-internal blocks from before a mid-output refusal fallback."""
        cut = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
        if cut < 0:
            return content
        drop = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}
        return [b for i, b in enumerate(content) if i > cut or b.type not in drop]

    def send(self, user_content: str | list[dict[str, Any]], max_turns: int = 60) -> str:
        """Add a user message and run the tool loop until Claude answers. Returns the final text."""
        start = len(self.messages)
        self.messages.append({"role": "user", "content": user_content})
        text = ""
        for _ in range(max_turns):
            with self.client.beta.messages.stream(
                model=MODEL,
                max_tokens=64000,
                system=self.system,
                messages=self.messages,
                tools=[t.to_dict() for t in self.tools.values()],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        self.on_text(event.delta.text)
                response = stream.get_final_message()

            if response.stop_reason == "refusal":
                del self.messages[start:]  # leave history valid for the next question
                return "[Claude declined to answer this request.]"

            content = self._echoable(response.content)
            self.messages.append({"role": "assistant", "content": content})
            text = "".join(b.text for b in content if b.type == "text")

            if response.stop_reason == "max_tokens":
                return text + "\n[response truncated: hit max_tokens]"
            tool_uses = [b for b in content if b.type == "tool_use"]
            if not tool_uses:
                return text
            self.on_text("\n")
            self.messages.append(
                {"role": "user", "content": [self._run_tool(b) for b in tool_uses]}
            )
        return text + "\n[stopped: too many tool calls]"


def _role_parts(campaign) -> dict[str, str]:
    return {
        "role_line": PLAYER_ROLE if campaign.spoiler_safe else DM_ROLE,
        "spoiler_rules": PLAYER_SPOILERS if campaign.spoiler_safe else "",
    }


def _span(n: dict[str, Any]) -> str:
    if n["page_start"] is None:
        return ""
    return f" (p. {n['page_start']})" if n["page_end"] in (None, n["page_start"]) else f" (pp. {n['page_start']}-{n['page_end']})"


def overview(store: Store, campaign: Any) -> str:
    """Digest summaries of the book and its top-level sections, for the system prompt.

    Changes only when a digest changes, so it stays in the cached prefix.
    """
    parts = []
    for o in store.source_overviews(campaign):
        parts.append(f"\n### {o['source']}\n{o['summary']}")
        parts += [
            f"- [section {s['id']}] {s['title']}{_span(s)}: {s['summary'] or '(not digested yet)'}"
            for s in o["sections"]
        ]
    if not parts:
        return ""
    return "\n## What the reference material covers\n" + "\n".join(parts) + "\n"


def catalog(store: Store, campaign: Any) -> str:
    """Every name and alias in the reference catalog, so transcripts can be matched to it."""
    rows = [e for e in store.ref_entities(campaign) if e["kind"] != "lore"]
    if not rows:
        return ""
    lines = [f"{e['kind']} | {e['name']} | {'; '.join(e['aliases'])}" for e in rows]
    return "\n## Names in the reference material\nkind | name | aliases\n" + "\n".join(lines) + "\n"


def chat_agent(store: Store, campaign_slug: str, **kw: Any) -> Agent:
    campaign = store.campaign(campaign_slug)
    system = ASK_SYSTEM.format(name=campaign.name, overview=overview(store, campaign), **_role_parts(campaign))
    return Agent(store=store, ctx=ToolContext(store, campaign), system=system, **kw)


def ingest_session(
    store: Store,
    campaign_slug: str,
    transcript: str,
    number: int | None = None,
    played_on: str | None = None,
    source_title: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Store a transcript as a source, then have Claude fold it into the campaign state."""
    from .ingest import parse_markdown

    campaign = store.campaign(campaign_slug)
    number = number or store.next_session_number(campaign)
    title = source_title or f"Session {number} transcript"
    blob = store.blobs.put(transcript.encode("utf-8"))
    source_id = store.add_source(
        campaign, title, "transcript", parse_markdown(transcript),
        blob=blob, filename=f"session-{number}.txt",
    )
    session_id = store.upsert_session(campaign, number, played_on=played_on, source_id=source_id)
    ctx = ToolContext(store, campaign, session_id=session_id)
    result: dict[str, Any] = {"session": number}

    def finish_session(title: str, summary: str) -> str:
        """Save the session's title and recap. Call once, after recording everything else.

        Args:
            title: Short evocative title, e.g. "Death House".
            summary: 1-4 paragraph recap of what the party did, found and decided.
        """
        store.upsert_session(campaign, number, title=title, summary=summary)
        result.update(title=title, summary=summary)
        return "Session saved."

    system = INGEST_SYSTEM.format(
        name=campaign.name, number=number, catalog=catalog(store, campaign), **_role_parts(campaign)
    )
    agent = Agent(
        store=store, ctx=ctx, system=system, effort=kw.pop("effort", "high"),
        extra_tools=[finish_session], **kw,
    )
    result["notes"] = agent.send(
        [
            {
                "type": "text",
                "text": f"<transcript session=\"{number}\">\n{transcript}\n</transcript>",
            }
        ]
    )
    result["journal_entries"] = len(store.events(campaign, session_number=number, limit=1000))
    return result


def describe_tool_call(name: str, args: dict[str, Any]) -> str:
    brief = json.dumps(args, ensure_ascii=False)
    return f"  · {name} {brief[:120] + '…' if len(brief) > 120 else brief}"
