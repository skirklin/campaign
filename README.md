# campaign

A D&D campaign assistant that:

1. **Answers questions from your reference material**: stat blocks, rewards,
   quest goals, room contents. It searches the module PDF and your notes and
   cites the page.
2. **Remembers the campaign.** Session transcripts (or audio recordings) and
   things you tell it in chat update a record of NPCs, quests, locations,
   items and a session-by-session journal.
3. **Keeps character sheets** if you want it to. They're optional, and they
   track HP, resources, inventory and spells, with a change log.

It supports both DM and player campaigns. In a **player** campaign the
assistant can't see module text, and it can't see anything the party hasn't
discovered, so it won't spoil the adventure.

## Setup

```bash
uv sync                      # add `--extra audio` for local speech-to-text
export ANTHROPIC_API_KEY=... # or `ant auth login`
```

Data lives in `~/.local/share/campaign/campaign.db`. Set `CAMPAIGN_DB` to put
it somewhere else.

## Web UI

```bash
uv run campaign web            # http://127.0.0.1:8765
```

- **Characters**: the full sheet, editable in place and saved automatically.
  It has ability scores with live modifiers, save and skill proficiency
  checkboxes, and HP with damage and heal buttons (damage comes off temp HP
  first). Resources like spell slots and hit dice are clickable pips. It
  also covers attacks, spells by level, inventory and coin, conditions,
  death saves, short and long rests, export to JSON, and a change history.
  It works at phone width for use at the table.
- **Campaigns**: create DM or player campaigns, then use the tabs:
  - **Sessions**: paste a transcript, or upload a text file or a recording,
    to run an ingest. A live log shows what Claude is reading and
    recording. Each past session has its recap and a **Revert** button.
  - **Reference**: upload module PDFs and notes, mark them DM-only, and
    search the text directly.
  - **World state**: tracked NPCs, quests, locations and items, filterable
    by kind and status.
  - **Journal**: events grouped by session.

There's no login. By default it listens only on this machine. Pass
`--host 0.0.0.0` to reach it from a phone on your network, and put it
behind something that authenticates before exposing it any further.

## Command line

```bash
# create a campaign: --role dm or --role player
uv run campaign new "Curse of Strahd" --role dm

# load reference material (PDF, Markdown or text)
uv run campaign add-source ~/books/curse-of-strahd.pdf --title "Curse of Strahd"
uv run campaign add-source notes/homebrew.md --kind notes

# ask questions
uv run campaign ask "What's the stat block for the Death House grick, and what treasure is in its lair?"
uv run campaign chat          # conversation; tell it what happened and it records it

# after a session
uv run campaign ingest session-07.txt --date 2026-10-25   # or a .m4a/.mp3 with the audio extra
uv run campaign journal
uv run campaign state --kind quest --status active
uv run campaign revert 7      # undo what that ingest wrote, if it got things wrong

# character sheets
uv run campaign char import examples/character.json
uv run campaign char show "Brannoc Ashvale"
uv run campaign char hp "Brannoc Ashvale" -- -9
uv run campaign char rest "Brannoc Ashvale" --long
uv run campaign char set "Brannoc Ashvale" '{"currency": {"gp": 120}}' --reason "loot from the crypt"
```

With more than one campaign, pass `-c <slug>` or set `CAMPAIGN=<slug>`.

### Using it from Claude Code or Claude Desktop (MCP)

The same tools are also available over MCP. Claude Code or Claude Desktop
can then be the assistant, and no API key is needed:

```bash
claude mcp add strahd -- uv --directory /path/to/campaign run campaign mcp -c curse-of-strahd
```

Add `--read-only` to expose only the lookup tools.

## How it works

```
 PDF / notes ──chunk by heading──▶ chunks + FTS5 index ◀── search_reference / read_reference
 transcript ──Claude + tools────▶ entities (+ revisions), journal, session summary
 chat ────────Claude + tools────▶ same tools; reads and writes
 character sheet (JSON, validated) ◀── update_character / `campaign char …`
```

- **Storage** (`db.py`): one SQLite file. Reference text is split into
  chunks at headings, so a stat block or a room stays together. The chunks
  are indexed with FTS5 (BM25). Campaign state is held as *entities* (npc,
  quest, location, item, faction, …), each with a status, a short summary
  and free-form JSON `data`, plus an append-only *journal*. Every entity
  write is saved as a revision tagged with its session, which is what makes
  `revert` possible.
- **Retrieval** is agentic and doesn't use embeddings. Claude searches, then
  reads the full chunk or page before quoting numbers. Proper nouns and game
  terms dominate D&D questions, and keyword search handles those well.
  Embeddings can be added later if fuzzy questions turn out to miss.
- **Ingest** (`agent.py`): the transcript is stored as a searchable source.
  Claude then reads the current state, matches mangled speech-to-text names
  to known entities, records what changed, and writes a recap. Sheets are
  updated only when the transcript states a change outright.
- **Spoiler safety** is enforced in the database layer, not just in the
  prompt. In player campaigns, `dm_only` sources and entities with
  `party_knows=false` are filtered out of every query.
- **Model**: Claude Opus 5.5 with adaptive thinking (`CAMPAIGN_MODEL` to
  override), with server-side refusal fallback enabled. Prompt caching keeps
  the repeated transcript cheap across the ingest tool loop.

## Status and next steps

Working and tested (`uv run pytest`): storage, PDF/Markdown chunking,
search, spoiler filtering, revisions and revert, character sheets, CLI, MCP
tool registration, the web API including background ingest jobs, and the
agent loop against a scripted fake API. The web UI was also exercised in
headless Chromium at desktop and phone widths.

Not yet exercised: live API calls end to end against a real module, and
audio transcription (`transcribe.py` uses faster-whisper but hasn't been run
here). Likely next steps:

- **Speaker diarization** for recordings, so the transcript says who spoke.
- **Ask from the web UI**: a chat box on the campaign page, using the same
  agent as `campaign chat`.
- **Editing world state by hand** in the web UI. Today it's read-only there;
  corrections go through chat or a revert.
- **Review before commit** for ingest: show proposed changes and approve
  them, as an alternative to ingest-then-revert.
- **Scanned PDFs** need OCR. Text-layer PDFs work as they are.
- **Embedding search** as a second retriever, if keyword search misses.
