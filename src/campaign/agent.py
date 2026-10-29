"""Claude-driven Q&A and session ingestion over the campaign tools."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
from anthropic import beta_tool

from .db import Campaign, Store
from .digest import fallback_params
from .tools import ToolContext, build_tools
from .world import World

MODEL = os.environ.get("CAMPAIGN_MODEL", "claude-opus-5-5")

ASK_SYSTEM = """\
You are the campaign assistant for the D&D campaign "{name}". {role_line}

What you know comes from tools over one world model, built from several
sources:
- The reference material (published modules, homebrew notes, transcripts),
  digested into an outline of section summaries (outline) and searchable
  as text (search_reference, read_reference, read_pages).
- The world: every person, place, item, quest and faction, with facts from
  the book and from play. Each fact holds over a range of sessions and
  names its source. Session 0 is the world before play, which is what the
  book describes; facts from play supersede it. Nothing is overwritten, so
  you can see what was true before as well as now. Use lookup for anything
  named or described ("the hag", "the vampire's castle"), find_entities for
  overviews, timeline for how something changed, and recent_journal and
  list_sessions for what happened when.
- Player character sheets: get_character, update_character.
- The party stash, what the party holds in common (loot nobody has
  claimed, the shared purse): get_party_stash, update_party_stash.

Answer from the tools, not from memory of the published adventure: this
table may have changed things, and editions differ. Say "now" vs "before"
clearly when it matters, and point out where the table has diverged from
the book. Before giving a stat block, DC, treasure or reward, read the
full text rather than a summary, and cite the source and page. If nothing
covers something, say so plainly, then offer a suggestion labelled as yours.

When the user tells you something happened or changed ("we killed the hag",
"I picked up a +1 shield", "Ireena trusts us now"), record it: record_fact
for what's now true (with an attribute such as status, location, holder or
disposition when it replaces an earlier value), end_fact for statements that
stopped being true, add_journal_entry for the event itself. Say what you
recorded. Writes are dated to the latest session played.
{spoiler_rules}
Keep answers tight and table-ready: the user is often mid-session.
{overview}"""

DM_ROLE = "The user is the Dungeon Master and may see everything, including module secrets."
PLAYER_ROLE = "The user is a player in this campaign, not the DM."
PLAYER_SPOILERS = """
Spoiler rules: the user is a player, so only discuss what their party has
learned. Your tools already hide DM-only material and facts the party
doesn't know. Don't fill gaps from your own knowledge of the published
adventure, and don't hint at secrets, upcoming encounters or NPC motives
the party hasn't uncovered.
"""

INGEST_SYSTEM = """\
You keep the campaign record for the D&D campaign "{name}". {role_line}

You are processing session {number}. The transcript follows in the user
message. Speech-to-text transcripts are noisy: names are often misspelled,
speakers are unlabelled or wrong, and much of the talk is table chatter,
rules lookups and jokes. Record only what happened in the game.

The record is a world model of facts over time. Everything you write is
dated to session {number}. Recording a new value for an attribute (status,
location, holder, disposition, leader, allegiance, condition) automatically
ends the old value, which stays in the history; don't try to delete or
rewrite the past. The book's facts are the world before play (session 0).

Work in this order:
1. Get your bearings: list_sessions, recent_journal, and find_entities with
   in_play_only for what the table has already touched.
2. Read the whole transcript.
3. Resolve every name to an existing entity: the names list below, lookup
   and find_entities. "Strad", "Straud" and "the count" are all "Strahd von
   Zarovich". Use the existing name, and add new nicknames with `aliases`.
   Create an entity (by passing `kind`) only for something genuinely new.
4. Record what changed, as facts:
   - attributes for state that replaces earlier state: someone dies, moves,
     changes sides; an item changes hands; a quest is taken, completed or
     failed (quests use status: active, completed, failed);
   - statements for new information: what an NPC wants, what the party
     learned, a quest's objectives, giver and reward;
   - end_fact for a statement that stopped being true;
   - party_learned when the party uncovers something the book already says
     (look up the fact id first);
   - modality "rumor" for claims the party heard but hasn't confirmed.
5. Log the session's key events with add_journal_entry, in order, roughly
   5-20 entries, naming the entities involved.
6. Update a character sheet only if it already exists and the transcript
   states the change outright (gold found, items gained, a level up). Say
   in `reason` where it came from. Loot the party takes without anyone
   claiming it goes in the party stash (update_party_stash) instead.
7. Finish by calling finish_session exactly once with a short title and a
   recap a player could read before the next session.

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
    # Given, Claude's thinking is asked for in summary and passed here as it streams (to show progress).
    on_thinking: Callable[[str], None] | None = None
    model: str = MODEL
    messages: list[Any] = field(default_factory=list)
    stop_reason: str | None = None  # of the last response

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
                model=self.model,
                max_tokens=64000,
                system=self.system,
                messages=self.messages,
                tools=[t.to_dict() for t in self.tools.values()],
                thinking={"type": "adaptive", **({"display": "summarized"} if self.on_thinking else {})},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                **fallback_params(self.model),
            ) as stream:
                for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    if event.delta.type == "text_delta":
                        self.on_text(event.delta.text)
                    elif event.delta.type == "thinking_delta" and self.on_thinking and event.delta.thinking:
                        self.on_thinking(event.delta.thinking)
                response = stream.get_final_message()
            self.stop_reason = response.stop_reason

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
    """Every entity name and alias, so transcripts can be matched to them."""
    rows = [e for e in World(store, campaign).list() if e["kind"] != "lore"]
    if not rows:
        return ""
    lines = [f"{e['kind']} | {e['name']} | {'; '.join(e['aliases'])}" for e in rows]
    return "\n## Names in the reference material\nkind | name | aliases\n" + "\n".join(lines) + "\n"


def chat_agent(store: Store, campaign_slug: str | Campaign, user_id: int | None = None, **kw: Any) -> Agent:
    """A conversation over one campaign. Pass a Campaign to talk as a particular viewer (their role)."""
    campaign = store.campaign(campaign_slug) if isinstance(campaign_slug, str) else campaign_slug
    system = ASK_SYSTEM.format(name=campaign.name, overview=overview(store, campaign), **_role_parts(campaign))
    return Agent(store=store, ctx=ToolContext(store, campaign, user_id=user_id), system=system, **kw)


def _plain(content: Any) -> Any:
    """A message's content as JSON-ready data, exactly as the API returned it (thinking blocks included)."""
    if isinstance(content, str):
        return content
    return [b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b for b in content]


def chat_turn(
    store: Store, campaign: Campaign, conversation_id: int, text: str, user_id: int | None = None,
    writable: bool = True, **kw: Any,
) -> str:
    """One turn of a stored conversation: load its history, answer, and append what was said.

    Only what this turn added is written, after the turn finishes, so the
    stored history is always a whole number of turns and only ever grows,
    as replaying thinking blocks requires.
    """
    history = store.conversation_messages(conversation_id)
    first = not history
    agent = chat_agent(store, campaign, user_id=user_id, messages=list(history), writable=writable, **kw)
    start = len(agent.messages)
    answer = agent.send(text)
    new = [{"role": m["role"], "content": _plain(m["content"])} for m in agent.messages[start:]]
    store.append_conversation(conversation_id, new, title=text.strip()[:80] if first else None)
    return answer


def ingest_session(
    store: Store,
    campaign_slug: str | Campaign,
    transcript: str,
    number: int | None = None,
    played_on: str | None = None,
    source_title: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Store a transcript as a source, then have Claude fold it into the campaign state.

    Pass a Campaign rather than a slug to ingest as a particular viewer: a
    player's ingest only sees what players may see.
    """
    from .ingest import parse_markdown

    campaign = store.campaign(campaign_slug) if isinstance(campaign_slug, str) else campaign_slug
    number = number or store.next_session_number(campaign)
    title = source_title or f"Session {number} transcript"
    blob = store.blobs.put(transcript.encode("utf-8"))
    source_id = store.add_source(
        campaign, title, "transcript", parse_markdown(transcript),
        blob=blob, filename=f"session-{number}.txt",
    )
    # Re-ingesting a session replaces what the previous ingest of it recorded.
    world = World(store, campaign)
    try:
        world.revert_session(number)
    except KeyError:
        pass
    store.upsert_session(campaign, number, played_on=played_on, source_id=source_id)
    ctx = ToolContext(store, campaign, session=number, run_kind="ingest")
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
    result["journal_entries"] = len(world.events(session=number))
    result["facts"] = store.conn.execute(
        "SELECT count(*) FROM facts WHERE run_id = ?", (ctx.run_id,)
    ).fetchone()[0] if ctx.run_id else 0
    return result


def describe_tool_call(name: str, args: dict[str, Any]) -> str:
    brief = json.dumps(args, ensure_ascii=False)
    return f"  · {name} {brief[:120] + '…' if len(brief) > 120 else brief}"
