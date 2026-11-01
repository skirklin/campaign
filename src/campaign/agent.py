"""Claude-driven Q&A and session ingestion over the campaign tools."""

from __future__ import annotations

import json
import os
import sys
import time
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
- For running the game: encounter to put a whole fight together (every
  creature's stat block, XP and difficulty for the party, in one call; put
  the ```encounter block it returns in your answer and don't reprint the
  stat blocks), stat_block for one creature's full stats, treasure
  for what's at a place or on a creature (and what's been taken), and
  roll_table to roll on the book's random encounter, treasure and other
  tables. Keyed areas answer to their map key ("C3").
- Player character sheets: get_character, update_character.
- The party's inventory: everything the party owns and who has it, the
  party itself (loot nobody has claimed, the shared purse), a character, or
  someone they left it with (inventory). New loot goes in with add_items,
  held by the party unless someone took it; claiming, giving and putting
  back are give_item; coin is change_coin. When the party takes treasure,
  end its fact and add it. What an item does (bonuses, attunement, worth)
  is its definition in the world, shared by every copy: define_item.
  Changes of hands go through the inventory, not holder facts.

Answer from the tools, not from memory of the published adventure: this
table may have changed things, and editions differ. Say "now" vs "before"
clearly when it matters, and point out where the table has diverged from
the book. Before giving a stat block, DC, treasure or reward, or saying
exactly where something is, read the full text rather than a summary, and
cite the source and page: world facts are short notes taken from the book,
and can drop what guards a thing or what it's hidden under. If nothing
covers something, say so plainly, then offer a suggestion labelled as yours.

When the user tells you something happened or changed ("we killed the hag",
"I picked up a +1 shield", "Ireena trusts us now"), record it: record_fact
for what's now true (with an attribute such as status, location, holder or
disposition when it replaces an earlier value), end_fact for statements that
stopped being true, add_journal_entry for the event itself. Say what you
recorded. Writes are dated to the latest session played. Record only what
the user says happened at the table. A question about running something
("the party picks a fight with Neta, what am I running?"), a plan or a
what-if is not an event: answer it, and if it might also have happened,
offer to record it rather than recording it. If the user says something
you recorded didn't happen or was wrong, take it back with retract_fact or
delete_journal_entry.
{spoiler_rules}
Keep answers tight and table-ready: the user is often mid-session. Answer
what was asked; skip background they didn't ask for.

Say only what your lookups showed. Don't add a location, a section or page,
a book title, a number or a rule that you didn't read in this conversation;
a footnote marker or a "see elsewhere" you haven't followed is not
knowledge. When you add something of your own (a tactic, a suggested DC, a
difficulty estimate), label it as your suggestion.

Write in Markdown; the app renders it. Lead with the answer, then the
detail: short paragraphs, **bold** for the things to act on (DCs, damage,
names), lists for steps and options, a table to compare several things.
Use ### headings only when an answer has distinct parts. The app turns
dice like 3d8+5 and "+8 to hit" into buttons that roll, so write dice out
plainly. Put read-aloud text from the book in a > quote. To show a
creature's stat block, call stat_block and paste its text unchanged in a
```statblock fenced block; it's shown as a stat block card.
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
   states the change outright (a level up, HP). Say in `reason` where it
   came from. Loot, purchases and coin go in the party's inventory: read it
   first (inventory), then add_items (held by the party unless someone
   claimed it), give_item when something changes hands, change_coin for
   gold found or spent.
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
    usage: dict[str, Any] = field(default_factory=dict)  # what the last send() cost; see _new_usage

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
        self.usage = _new_usage(self.model)
        began = time.monotonic()
        try:
            return self._loop(start, max_turns)
        finally:
            self.usage["seconds"] = round(time.monotonic() - began, 1)
            self.usage["cost_usd"] = turn_cost(self.usage)

    def _loop(self, start: int, max_turns: int) -> str:
        text = ""
        for _ in range(max_turns):
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                # The system prompt (and the tools before it) is the same for every conversation in a
                # campaign: a cache breakpoint at its end lets a new conversation read it instead of
                # writing it again. The top-level cache_control caches the growing conversation after it.
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
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
            _add_usage(self.usage, getattr(response, "usage", None))

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
            self.usage["tool_calls"] += len(tool_uses)
            self.messages.append(
                {"role": "user", "content": [self._run_tool(b) for b in tool_uses]}
            )
        return text + "\n[stopped: too many tool calls]"


def _new_usage(model: str) -> dict[str, Any]:
    return {"model": model, "api_calls": 0, "tool_calls": 0, "input_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 0, "seconds": 0.0, "cost_usd": None}


def _add_usage(total: dict[str, Any], u: Any) -> None:
    total["api_calls"] += 1
    if u is None:
        return
    total["input_tokens"] += u.input_tokens or 0
    total["cache_read_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
    total["cache_write_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    total["output_tokens"] += u.output_tokens or 0


def turn_cost(usage: dict[str, Any]) -> float | None:
    """Dollars for one answer: cache reads at a tenth of the input price, writes at 1.25 times."""
    from .digest import PRICES

    price = PRICES.get(usage["model"])
    if price is None:
        return None
    inp, out = price
    dollars = (usage["input_tokens"] * inp + usage["cache_read_tokens"] * inp * 0.1
               + usage["cache_write_tokens"] * inp * 1.25 + usage["output_tokens"] * out) / 1_000_000
    return round(dollars, 4)


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


class Answer(str):
    """An answer's text, carrying what it cost (`usage`: tokens, calls, seconds, dollars)."""

    usage: dict[str, Any]

    def __new__(cls, text: str, usage: dict[str, Any]) -> Answer:
        out = super().__new__(cls, text)
        out.usage = usage
        return out


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
    titled = bool(store.conversation(conversation_id)["title"])
    if not history and not titled:
        store.set_conversation_title(conversation_id, short_title(text))  # until a better one is written
    agent = chat_agent(store, campaign, user_id=user_id, messages=list(history), writable=writable, **kw)
    start = len(agent.messages)
    answer = agent.send(text)
    new = [{"role": m["role"], "content": _plain(m["content"])} for m in agent.messages[start:]]
    store.append_conversation(conversation_id, new, usage=agent.usage)
    if not history and not titled and (better := conversation_title(text, answer)):
        store.set_conversation_title(conversation_id, better)
    return Answer(answer, agent.usage)


TITLE_MODEL = os.environ.get("CAMPAIGN_TITLE_MODEL", "claude-haiku-5-5")
TITLE_SYSTEM = """Name this conversation from a D&D campaign assistant for a list of past
conversations: 2 to 6 words naming what it's about, the specific person,
place, fight or rule ("Ferran's fight", "Signet ring locations", "Neta's
price for help"). Reply with the title only: no quotes, no full stop."""


def short_title(text: str, limit: int = 60) -> str:
    """The question, cut at a word boundary: a title until a proper one is written."""
    words = " ".join(text.split())
    if len(words) <= limit:
        return words
    return words[:limit].rsplit(" ", 1)[0].rstrip(",;:.-") + "…"


def conversation_title(question: str, answer: str) -> str | None:
    """A short title for a conversation from its first exchange, or None if one can't be had."""
    try:
        msg = anthropic.Anthropic().messages.create(
            model=TITLE_MODEL, max_tokens=2000, system=TITLE_SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": f"Question: {question[:2000]}\n\nAnswer: {answer[:2000]}"}],
        )
    except Exception:  # a title is a nicety; the conversation keeps its provisional one
        return None
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    title = text.strip().splitlines()[0].strip(" \"'*#.") if text.strip() else ""
    return title if 0 < len(title) <= 80 else None


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


_DESCRIBE: dict[str, Callable[[dict[str, Any]], str]] = {
    "lookup": lambda a: f"Looked up “{a.get('name', '')}”"
              + (f" as of session {a['as_of_session']}" if a.get("as_of_session") is not None else ""),
    "find_entities": lambda a: "Listed " + (f"{a['kind']}s" if a.get("kind") else "the world")
                     + (f" matching “{a['query']}”" if a.get("query") else ""),
    "timeline": lambda a: f"Timeline of “{a.get('name', '')}”",
    "stat_block": lambda a: f"Stat block for “{a.get('name', '')}”",
    "encounter": lambda a: "Put together a fight: " + ", ".join(
        f"{c.get('count', 1)} × {c.get('name', '?')}" for c in a.get("creatures") or []),
    "treasure": lambda a: f"Treasure at “{a.get('place', '')}”",
    "roll_table": lambda a: (f"Rolled on “{a['name']}”" if a.get("name") else "Listed the tables to roll on")
                  + (f" (a {a['roll']})" if a.get("roll") is not None else ""),
    "search_reference": lambda a: f"Searched the books for “{a.get('query', '')}”",
    "read_reference": lambda a: "Read a passage of the book",
    "read_pages": lambda a: f"Read {a.get('source', 'the book')}, p. {a.get('first_page')}"
                  + (f"–{a['last_page']}" if a.get("last_page") not in (None, a.get("first_page")) else ""),
    "outline": lambda a: "Read the outline",
    "list_sources": lambda a: "Listed the reference material",
    "list_sessions": lambda a: "Listed the sessions",
    "recent_journal": lambda a: "Read the journal" + (f" for session {a['session']}" if a.get("session") else ""),
    "record_fact": lambda a: f"Recorded: {a.get('statement', '')}",
    "end_fact": lambda a: "Marked a fact no longer true",
    "retract_fact": lambda a: "Deleted a fact recorded by mistake",
    "delete_journal_entry": lambda a: "Deleted a journal entry recorded by mistake",
    "party_learned": lambda a: "Marked a fact as known to the party",
    "add_journal_entry": lambda a: f"Journal: {a.get('text', '')}",
    "get_character": lambda a: f"Read {a.get('name', '')}'s sheet",
    "update_character": lambda a: f"Updated {a.get('name', '')}'s sheet" + (f": {a['reason']}" if a.get("reason") else ""),
    "create_character": lambda a: "Created a character",
    "inventory": lambda a: "Checked the party's inventory",
    "add_items": lambda a: "Added " + ", ".join(str(i.get("name", "?")) for i in a.get("items") or [])
                 + (f" for {a['holder']}" if a.get("holder") not in (None, "party") else ""),
    "change_item": lambda a: "Changed an item" + (f": {a['reason']}" if a.get("reason") else ""),
    "give_item": lambda a: f"Gave an item to {a.get('to', '?')}",
    "change_coin": lambda a: f"Coin for {a.get('holder', '?')}" + (f" to {a['to']}" if a.get("to") else "")
                   + ": " + ", ".join(f"{v:+d} {k}" if not a.get("to") else f"{v} {k}" for k, v in (a.get("change") or {}).items()),
    "split_party_coin": lambda a: "Shared out the party's coin",
    "link_item": lambda a: f"Said item {a.get('item_id')} is {a['world_item']}" if a.get("world_item")
                 else f"Made item {a.get('item_id')} a plain item",
    "define_item": lambda a: f"Said what {a.get('name', 'an item')} does",
}


def describe_tool_call(name: str, args: dict[str, Any]) -> str:
    """A line saying what a tool call does, for people watching: "  · Looked up “the hag”"."""
    try:
        text = _DESCRIBE[name](args)
    except (KeyError, TypeError):
        brief = json.dumps(args, ensure_ascii=False)
        text = f"{name} {brief}"
    return f"  · {text[:140] + '…' if len(text) > 140 else text}"
