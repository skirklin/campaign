"""Claude-driven Q&A and session ingestion over the campaign tools."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from anthropic import beta_tool

from . import budget
from .db import Campaign, Store
from .digest import fallback_params
from .tools import WRITE_TOOLS, ToolContext, build_tools
from .world import World

MODEL = os.environ.get("CAMPAIGN_MODEL", "claude-opus-5-5")

# How anything written into the campaign reads: as the thing itself, never its history. Shared by
# the assistant, the session ingest and the MCP server's instructions.
CLEAN_RECORDS = """\
Write the record as the truth, plainly: a name, note, fact or journal entry
says what is, in the campaign's own words, with the right spelling, name and
number. Never write in where it came from or how it got that way ("on the
sheet as...", "the sheet says...", "previously...", "corrected from...",
"misspelled as..."): that belongs in a change's `reason`, which goes to the
log, and nowhere else. To fix something, replace it; don't annotate it."""

# What players see of the DM's record, and how to keep a secret in it. For whoever writes as the DM.
KEEPING_SECRETS = """Players in this campaign read the same record, filtered to what their party
knows. A fact is hidden from them while party_knows is false; everything
else is shown as written: an entity's name, every alias the party knows
(listed beside the name), journal entries and session recaps. A name gives
a secret away as surely as a fact does:
- Never connect a disguise or secret identity to the truth through names.
  A persona the party has met ("Vasili von Holtz") is its own entity under
  the name the party knows; who it really is ("Vasili is Strahd in
  disguise") is a fact with party_knows false, on the persona. Don't add the
  true name as the persona's alias, or the persona as the true entity's
  alias, and don't merge the two while it's a secret.
- An alias that reveals what someone is ("the windmill hag" for the kindly
  baker) is added with party_knows false: record_fact's aliases go with the
  fact's party_knows; set_alias takes its own. lookup lists such names under
  secret_aliases.
- Journal entries and recaps say what the party saw and did, in the names
  they know, never what's behind it.
When the user, or what you read, shows a secret reaching the players, fix
it at its source rather than writing around it: set_alias with party_knows
false or remove_alias for a name, rename_entity or delete_entity for an
entity that shouldn't be named so or exist, keep_from_party, edit_fact or
retract_fact for a fact, edit_journal_entry or delete_journal_entry for the
journal, edit_session for a recap. Then say what you changed."""

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
  stat blocks), stat_block for one creature's full stats, rules for
  what a 5e magic item, spell, weapon or piece of gear does (the SRD's
  words; the book's own version wins where it differs), treasure
  for what's at a place or on a creature (and what's been taken), and
  roll_table to roll on the book's random encounter, treasure and other
  tables. Keyed areas answer to their map key ("C3").
- Player characters: get_character, update_character. Some keep their sheet
  outside the app (D&D Beyond, paper): then only what they carry is tracked
  here, in the party's inventory, and their stats aren't known. add_party_member
  adds such a character. A sheet on D&D Beyond can be mirrored here
  (refresh_from_dndbeyond, when it's public there); it's then read-only in
  spirit: D&D Beyond is its source, and a refresh replaces it.
- The party's inventory: everything the party owns and who has it, the
  party itself (loot nobody has claimed, the shared purse), a character, or
  someone they left it with (inventory). New loot goes in with add_items,
  held by the party unless someone took it; claiming, giving and putting
  back are give_item; coin is change_coin. When the party takes treasure,
  end its fact and add it. What an item does (bonuses, attunement, worth)
  is its definition in the world, shared by every copy: define_item.
  Changes of hands go through the inventory, not holder facts.
- Recaps: past play with no transcript (the campaign before the app, sessions
  nobody recorded) is written up with add_recap as a session of its own,
  on the same timeline, and what happened in it is dated to that session:
  record_fact, end_fact, party_learned and add_journal_entry take session.
  Write recaps oldest first, and only what the user tells you happened.

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
you recorded didn't happen or was wrong, correct it (edit_fact,
edit_journal_entry, edit_session, rename_entity, remove_alias) or take it
back (retract_fact, delete_journal_entry, delete_entity): anything written
about play can be fixed, whichever session or chat wrote it.

""" + CLEAN_RECORDS + """
{secrets}{spoiler_rules}
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

The user message is material about session {number}: whatever the DM had,
such as a recording's transcript, their own notes, a player's recap, or a
few lines about what happened. Read it for what it is. A speech-to-text
transcript is noisy: names are often misspelled, and much of the talk is
table chatter, rules lookups and jokes. Speakers may be unlabelled, labelled
"Speaker 1", "Speaker 2"…, or labelled with the real person's name, and the
labels can be wrong. A speaker is a person at the table, not a character:
work out from what they say who is the DM and which character each player
plays. Notes are terse and assume you know the campaign. Record only what
happened in the game, and only as much as the material says: short notes
make a short record.
{existing}
The record is a world model of facts over time. Everything you write is
dated to session {number}. Recording a new value for an attribute (status,
location, holder, disposition, leader, allegiance, condition) automatically
ends the old value, which stays in the history; don't try to delete or
rewrite the past. The book's facts are the world before play (session 0).

Work in this order:
1. Get your bearings: list_sessions, recent_journal, and find_entities with
   in_play_only for what the table has already touched.
2. Read all of the material.
3. Resolve every name to an existing entity: the names list below, lookup
   and find_entities. "Strad", "Straud" and "the count" are all "Strahd von
   Zarovich". Use the existing name, and add new nicknames with `aliases`
   (a nickname that gives a secret away only with party_knows false, below).
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
5. Log the session's key events with add_journal_entry, in order, naming the
   entities involved: as many as the material has, up to about 20.
6. Update a character sheet only if it already exists and the material
   states the change outright (a level up, HP). Say in `reason` where it
   came from. Loot, purchases and coin go in the party's inventory: read it
   first (inventory), then add_items (held by the party unless someone
   claimed it), give_item when something changes hands, change_coin for
   gold found or spent.
7. Finish by calling finish_session exactly once with a short title and a
   recap of the whole session a player could read before the next one.

Prefer fewer, accurate writes over guesses. If something is ambiguous, leave
it out and mention it in your final message.

""" + CLEAN_RECORDS + """
{secrets}{spoiler_rules}{catalog}"""

NEW_SESSION = ""

ADDING_TO_SESSION = """
Session {number} is already recorded, from {uploads}. This adds to it; what's
recorded stays. First read what the session has: its recap is below, and
recent_journal with session={number} gives its journal. Then record only
what this material adds or corrects: don't repeat a fact or journal entry
that's already there. When it contradicts the record, the record was
probably misheard: record the correction (a new attribute value, or end_fact
on a statement that was wrong). Finish with the recap rewritten to cover the
whole session, what was there and what this adds, keeping the title unless
this material shows it's wrong.

Session {number} so far: "{title}"
{summary}
"""


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
    # Given, the tools that change the campaign aren't run: each call is kept here, as a change for
    # the DM to accept or not (an ingest the DM reviews). The other tools run as ever.
    proposals: list[dict[str, Any]] | None = None
    stop_reason: str | None = None  # of the last response
    usage: dict[str, Any] = field(default_factory=dict)  # what the last send() cost; see _new_usage

    def __post_init__(self) -> None:
        self.client = budget.client()
        fns = build_tools(self.ctx, writable=self.writable) + self.extra_tools
        self.tools = {f.__name__: beta_tool(f, eager_input_streaming=True) for f in fns}

    def _run_tool(self, block: Any) -> dict[str, Any]:
        self.on_tool(block.name, block.input)
        tool = self.tools.get(block.name)
        if self.proposals is not None and block.name in WRITE_TOOLS and tool is not None:
            self.proposals.append({"tool": block.name, "args": dict(block.input),
                                   "summary": describe_tool_call(block.name, block.input).strip(" ·")})
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": False, "content": (
                "Noted for the DM to review: it's made only if they accept it, so nothing has changed yet and it "
                "has no id to refer to. Carry on as if it were done.")}
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
        "secrets": "" if campaign.spoiler_safe else "\n" + KEEPING_SECRETS + "\n",
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


def chat_agent(store: Store, campaign_slug: str | Campaign, user_id: int | None = None,
               conversation_id: int | None = None, **kw: Any) -> Agent:
    """A conversation over one campaign. Pass a Campaign to talk as a particular viewer (their role)."""
    campaign = store.campaign(campaign_slug) if isinstance(campaign_slug, str) else campaign_slug
    system = ASK_SYSTEM.format(name=campaign.name, overview=overview(store, campaign), **_role_parts(campaign))
    ctx = ToolContext(store, campaign, user_id=user_id, conversation_id=conversation_id)
    return Agent(store=store, ctx=ctx, system=system, **kw)


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
    agent = chat_agent(store, campaign, user_id=user_id, conversation_id=conversation_id, messages=list(history),
                       writable=writable, **kw)
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
        msg = budget.client().messages.create(
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
    text: str,
    number: int | None = None,
    played_on: str | None = None,
    name: str = "",
    review: bool = False,
    **kw: Any,
) -> dict[str, Any]:
    """Have Claude read material about a session into the campaign: a new session, or more about
    one already recorded (`number`), which it adds to.

    Pass a Campaign rather than a slug to ingest as a particular viewer: a
    player's ingest only sees what players may see.

    With `review`, nothing changes at all: what the assistant would record
    comes back as `proposals` (and the recap as `title` and `summary`), for the
    DM to accept, edit or leave out, and `apply_ingest` makes the upload and
    the changes they accept. Without it (the command line), the upload is made
    first and the changes as it goes.
    """
    campaign = store.campaign(campaign_slug) if isinstance(campaign_slug, str) else campaign_slug
    number = number or store.next_session_number(campaign)
    name = (name or "notes").replace('"', "'")
    blob = store.blobs.put(text.encode("utf-8"))
    session = next((x for x in store.sessions(campaign) if x["number"] == number), None)
    ctx = ToolContext(store, campaign, session=number, run_kind="ingest")
    if not review:
        ctx.run_id = _record_upload(store, campaign, number, blob, name, played_on)
    result: dict[str, Any] = {"session": number, "adding": session is not None}
    proposals: list[dict[str, Any]] | None = [] if review else None

    def finish_session(title: str, summary: str) -> str:
        """Save the session's title and recap. Call once, after recording everything else.

        Args:
            title: Short evocative title, e.g. "Death House".
            summary: 1-4 paragraph recap of the whole session: what the party did, found and decided.
        """
        if not review:
            store.upsert_session(campaign, number, title=title, summary=summary)
        result.update(title=title, summary=summary)
        return "Session saved." if not review else "Recap noted for the DM to review."

    existing = NEW_SESSION if session is None else ADDING_TO_SESSION.format(
        number=number, title=session["title"] or "(untitled)", summary=session["summary"] or "(no recap yet)",
        uploads=", ".join(u["name"] for u in session["uploads"]) or "material entered by hand",
    )
    system = INGEST_SYSTEM.format(
        name=campaign.name, number=number, existing=existing, catalog=catalog(store, campaign),
        **_role_parts(campaign),
    )
    agent = Agent(
        store=store, ctx=ctx, system=system, effort=kw.pop("effort", "high"),
        extra_tools=[finish_session], proposals=proposals, **kw,
    )
    result["notes"] = agent.send(
        [{"type": "text", "text": f"<material session=\"{number}\" name=\"{name}\">\n{text}\n</material>"}]
    )
    if review:
        return result | {"review": "pending", "proposals": proposals, "blob": blob, "name": name,
                         "played_on": played_on}
    result["upload"] = ctx.run_id
    result["journal_entries"] = len(World(store, campaign).events(session=number))
    result["facts"] = store.conn.execute("SELECT count(*) FROM facts WHERE run_id = ?", (ctx.run_id,)).fetchone()[0]
    return result


def _record_upload(store: Store, campaign: Campaign, number: int, blob: str, name: str, played_on: str | None) -> int:
    """Make the upload: the session if it's new, the text as a searchable source, and the run that
    what it records belongs to (which remembers the recap it may rewrite)."""
    from .ingest import parse_markdown

    session = next((x for x in store.sessions(campaign) if x["number"] == number), None)
    store.upsert_session(campaign, number, played_on=played_on if session is None or not session["played_on"] else None)
    taken = {x["title"] for x in store.sources(campaign)}
    title = f"Session {number}: {name}"
    title = next(t for n in range(1, len(taken) + 2) if (t := title if n == 1 else f"{title} ({n})") not in taken)
    text = store.blobs.get(blob).decode("utf-8", errors="replace")
    source_id = store.add_source(campaign, title, "transcript", parse_markdown(text), blob=blob, filename=name)
    run_id = World(store, campaign).start_run("ingest", session=number, source_id=source_id, label=name)
    before = {"title": session["title"], "summary": session["summary"]} if session else {"title": "", "summary": ""}
    with store.conn:
        store.conn.execute("UPDATE runs SET recap_before = ? WHERE id = ?", (json.dumps(before), run_id))
    return run_id


def apply_ingest(
    store: Store, campaign: Campaign, review: dict[str, Any], accepted: list[int],
    edits: dict[int, dict[str, Any]] | None = None, title: str | None = None, summary: str | None = None,
) -> dict[str, Any]:
    """Make the upload and the changes the DM accepted from its review, in the order the assistant
    proposed them, with their edits, and the recap as they left it. What the session already had stays."""
    from anthropic import beta_tool

    number = review["session"]
    run_id = _record_upload(store, campaign, number, review["blob"], review["name"], review.get("played_on"))
    ctx = ToolContext(store, campaign, session=number, run_kind="ingest", run_id=run_id)
    tools = {f.__name__: beta_tool(f) for f in build_tools(ctx)}
    proposals = review["proposals"]
    made, problems = 0, []
    for i in sorted(set(accepted)):
        if not 0 <= i < len(proposals):
            continue
        p = proposals[i]
        args = p["args"] | (edits or {}).get(i, {})
        try:
            said = str(tools[p["tool"]].call(args))
        except Exception as err:  # what's left still goes in; the DM hears what didn't
            said = f"Error: {err}"
        if said.startswith("Error"):
            problems.append(f"{p['summary']}: {said.removeprefix('Error: ')}")
        else:
            made += 1
    store.upsert_session(campaign, number, title=(title if title is not None else review.get("title")) or "",
                         summary=(summary if summary is not None else review.get("summary")) or "")
    world = World(store, campaign)
    facts = store.conn.execute("SELECT count(*) FROM facts WHERE run_id = ?", (run_id,)).fetchone()[0]
    return {"made": made, "problems": problems, "facts": facts, "upload": run_id,
            "journal_entries": len(world.events(session=number))}


_DESCRIBE: dict[str, Callable[[dict[str, Any]], str]] = {
    "lookup": lambda a: f"Looked up “{a.get('name', '')}”"
              + (f" as of session {a['as_of_session']}" if a.get("as_of_session") is not None else ""),
    "find_entities": lambda a: "Listed " + (f"{a['kind']}s" if a.get("kind") else "the world")
                     + (f" matching “{a['query']}”" if a.get("query") else ""),
    "timeline": lambda a: f"Timeline of “{a.get('name', '')}”",
    "stat_block": lambda a: f"Stat block for “{a.get('name', '')}”",
    "rules": lambda a: f"Read the rules for “{a.get('name', '')}”",
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
    "edit_fact": lambda a: "Rewrote a fact" + (f": {a['statement']}" if a.get("statement") else ""),
    "keep_from_party": lambda a: "Hid a fact from the players",
    "delete_journal_entry": lambda a: "Deleted a journal entry recorded by mistake",
    "edit_journal_entry": lambda a: f"Rewrote a journal entry: {a.get('text', '')}",
    "party_learned": lambda a: "Marked a fact as known to the party",
    "set_alias": lambda a: f"{a.get('entity', '?')} goes by “{a.get('alias', '')}”"
                 + ("" if a.get("party_knows", True) else " (secret from the party)"),
    "remove_alias": lambda a: f"{a.get('entity', '?')} no longer goes by “{a.get('alias', '')}”",
    "rename_entity": lambda a: f"Renamed {a.get('entity', '?')} to {a.get('name', '?')}",
    "delete_entity": lambda a: f"Deleted {a.get('entity', '?')}",
    "edit_session": lambda a: f"Rewrote session {a.get('number', '?')}"
                    + (f": {a['title']}" if a.get("title") else ""),
    "add_journal_entry": lambda a: f"Journal: {a.get('text', '')}",
    "add_recap": lambda a: f"Recap: {a.get('title', '')}",
    "get_character": lambda a: f"Read {a.get('name', '')}'s sheet",
    "update_character": lambda a: f"Updated {a.get('name', '')}'s sheet" + (f": {a['reason']}" if a.get("reason") else ""),
    "create_character": lambda a: "Created a character",
    "add_party_member": lambda a: f"Added {a.get('name', 'a character')} to the party",
    "refresh_from_dndbeyond": lambda a: f"Read {a.get('name', 'a character')} from D&D Beyond",
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
