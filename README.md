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

### Where your data lives

Everything is stored in one data directory **outside the code checkout**, so
pulling, switching branches, reinstalling or re-cloning never touches it:

```
~/.local/share/campaign/        (or $CAMPAIGN_HOME)
  campaign.db                   campaigns, sources, digests, state, characters
  blobs/ab/ab12…                every uploaded original, named by its SHA-256
  backups/                      a database snapshot before every schema migration
```

- **Originals are kept.** Each upload is stored once by content hash and can
  be downloaded again from the web UI. When the chunker improves,
  `campaign reindex` rebuilds chunks from the originals. The web server does
  this at startup too. Neither makes any API calls.
- **Schema changes are migrations or a clean start.** The schema is
  versioned (`PRAGMA user_version`). Small changes are migrations: opening
  the database with a newer build snapshots it to `backups/`, then upgrades
  it in place. Big changes start clean instead. The old database is moved to
  `backups/`, and the new one keeps everything that isn't world state:
  campaigns, character sheets, reference uploads (re-read from the stored
  originals) and the digest cache. Sessions and world state start empty.
- `campaign info` and the Campaigns page show where the data is.

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
- **Play view**: the same sheet laid out for a phone at the table, and what
  a sheet opens in (the ✎ button switches to the full editor, and the
  choice is remembered on that device). HP, AC, initiative and conditions
  stay pinned at the top; tabs along the bottom hold Main (abilities,
  attacks, resources, saves, rests), Skills, Spells (slots shown with their
  level), Gear and Notes. Damage, healing and coin go in on a keypad, so the
  phone's keyboard doesn't cover the sheet; resources, death saves and
  conditions are taps. Rolls take over the screen. Tapping a check or
  save rolls it, showing the advantage and disadvantage results too.
  Tapping an attack first asks what's chosen: advantage, the attack
  options that apply to it (Reckless Attack, Great Weapon Master) and
  statuses that change it (Rage). Then it rolls to hit, showing both dice
  with advantage and any reminder an option carries. On a hit it rolls
  damage, doubling dice on a crit, with situational damage to add if it
  applies. "Attack again" keeps the choices for a second swing.
  **Start combat** switches Main to a layout for the fight, which decides
  nothing:
  - a round tally with **Next turn**;
  - **Grapple** and **Shove** (also outside combat) roll Athletics, and a
    grapple shows **Grappling** until it's released;
  - statuses, attacks and resources first, with how long each status has
    run ("Rage · round 3 of 10"). A status stays on until it's ended.

  **End** switches back. The round is kept on that device, so a
  reload mid-fight carries on.
  Rolls show their sums one line per part: the die, then each modifier
  (STR, proficiency, Magic, Rage), then the total.
  Every change made in the Play view (HP, pips, statuses, coin, rests)
  shows an **Undo** on its toast. The 🎲 button opens a dice tray: tap dice to build an expression or
  type one (`8d6 fire`, `2d20kh1+5` for advantage, `4d6kh3` for a stat),
  with Adv/Dis toggles. Every roll is remembered on that device, per
  character (the latest 100): the tray shows the recent ones, the Notes
  tab the rest. The screen stays on while it's open. On a phone it can be installed as
  an app (Chrome offers it, and the Characters page shows an Install
  button when it can). Installed, it opens without the browser's bars; in
  a browser tab, the sheet's full-screen button hides them. Either way the
  site's top bar gives way to a ‹ back to the list.
- **Campaigns**: create DM or player campaigns, then use the tabs:
  - **Ask**: chat with the campaign assistant. Answers stream in as
    they're written, with the lookups it makes folded into one line
    above them. Answers are Markdown, rendered as it arrives (headings,
    lists, tables; the book's read-aloud text as a boxed quote); a stat
    block comes as a card laid out like the books', and dice in any answer
    or stat block ("3d8+5", "+8 to hit") are buttons that roll. Under each
    answer is what it cost: tokens, lookups, seconds and dollars (hover
    for the breakdown into cached and uncached input).
    Conversations are kept (per person), so you can come back to one; in
    a player campaign it only knows what the party knows. With editor
    access it can also record what you tell it happened.
  - **Sessions**: paste a transcript, or upload a text file or a recording,
    to run an ingest. A live log shows what Claude is reading and
    recording. Each past session has its recap and a **Revert** button.
    An ingest is a [job](#background-jobs): it continues with the page
    closed and survives restarts and deploys.
  - **Reference**: upload module PDFs and notes, mark them DM-only, and
    search the text directly.
  - **World state**: every person, place, item and quest, from the book and
    from play, with their current status and location. Filter it to what
    the table has touched. Each entry opens to a page with:
    - what's true now, what's no longer true, and rumors, each with its
      session range and source;
    - an "as of session N" slider;
    - its events;
    - a synthesized summary;
    - forms to add or end facts by hand, and to merge duplicates.
  - **Journal**: events by session, linked to the entities involved.

Locally there's no sign-in, the server listens only on this machine, and
you own everything. To put it on the internet, turn on Google sign-in
(`CAMPAIGN_AUTH=google`). The server refuses to listen anywhere but
localhost without it, unless you pass `--no-auth`. With sign-in on:

- **Campaigns are shared by invite link.** Each member is a DM (sees
  everything) or a player (sees only what the party knows), with owner,
  editor or viewer access. A campaign's **Members** tab manages this.
- **Character sheets belong to their players.** Everyone in the campaign
  can read them; their owner and the campaign's DMs can change them.
- **A character lives in one campaign**, on its **Party** tab. The sheet is
  the same character as the player-character entry in that campaign's
  world: the sheet shows its timeline, the world entry links to the sheet,
  and the assistant finds the sheet by any name the world knows it by.
  Renaming the sheet renames the entry and keeps the old name as an alias.
  Taking a character to another campaign (a sequel, a one-shot) copies it
  with **Copy to campaign…**: the copy changes on its own and links back
  to the original. The top-level **Characters** page lists yours across
  campaigns.
- **Sheet numbers are worked out from their parts**, so they follow the
  character: raise Strength and every Strength attack, check and save
  goes up; level up and everything proficient does. An attack is its
  ability, proficiency, magic bonus and damage dice; AC is a formula
  (base plus ability modifiers, with a Dexterity cap for medium armor);
  max HP is the hit-dice total plus Constitution per level. Anything
  else is a bonus with a source: on the item that grants it (it counts
  while equipped, and attuned if it needs to be), or on the sheet for
  features and effects, which can be switched off (Rage, between fights).
  Click any number to see what it adds up from. **Statuses** are what's
  switched on in play: Rage, Concentration (on a named spell), Bless. A
  bonus can count only during one (Rage's damage), starting one can spend
  a resource (a use of Rage), and a rest ends them. Damage that only
  applies sometimes (Sneak Attack, "vs giants", a centaur's Charge) is
  **situational**: it's left out of the attack's damage and offered as a
  button on the roll, to add when it applies; adding one can spend a
  resource (a smite's slot). A status can say how many rounds it lasts
  (Rage: 10), which combat mode shows. **Attack options** are choices made before
  an attack roll: advantage or disadvantage, a change to hit and damage,
  and a reminder (Reckless Attack: "attacks against you have advantage").
  Each applies to the attacks it names, by ability, melee or ranged,
  weapon or spell, and weapon property (Great Weapon Master: heavy,
  melee). The editor has presets for the common ones. Dice can grow with a class
  (`(half_level:rogue)d6`), and items can raise an ability score or set
  it (Gauntlets of Ogre Power). Nicknames become the world entry's
  aliases. Sheets saved before this
  keep their numbers as flat amounts until edited.
- **The party stash** is on the Party tab and on the Play view's Gear tab:
  the shared purse, plus loot nobody has claimed yet. It's the party's
  shared bag, so anyone in the campaign can change it, whatever their
  access, and its history shows who did what. **Take** moves an item or
  coin into a character's inventory, and an item's **Put in the party
  stash** moves it back. Both need the right to change that character, and
  both are logged on the stash and the sheet. Coin is added and spent as
  changes, so two people at once both count. Other edits are refused if
  someone changed the stash in the meantime, rather than overwriting it.
  Session ingests and chat record unclaimed loot there
  (`get_party_stash`, `update_party_stash`).
- **Paper sheets can be scanned in.** On the Party tab, **From a scan or
  PDF…** takes a photo or PDF of a filled-in sheet; Claude reads it into a
  new character (`CAMPAIGN_SHEET_MODEL`, default `CAMPAIGN_MODEL`) and lists
  anything it couldn't make out in the sheet's notes, so you can correct
  those by hand. It records numbers as parts, and lists anywhere the
  parts don't add up to what the sheet prints. While reading, it can search the campaign's reference
  material (as the uploader sees it, so a player's scan doesn't see
  DM-only books): it looks up abbreviated or unfamiliar classes,
  subclasses, species, features and spells, such as a setting's own
  subclasses, and fills in what named magic items do. Reading takes a minute or two and is a
  [job](#background-jobs), with its progress shown on the Party tab.
- **An admin page** manages users and app-wide invites.
[docs/deploy.md](docs/deploy.md) covers running it on the homelab cluster:
the container image, Kubernetes manifests, the Caddy route, and the
CI/release workflows.

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

# character sheets (each belongs to a campaign: -c picks it)
uv run campaign char import examples/character.json -c curse-of-strahd
uv run campaign char show "Brannoc Ashvale"
uv run campaign char hp "Brannoc Ashvale" -- -9
uv run campaign char rest "Brannoc Ashvale" --long
uv run campaign char set "Brannoc Ashvale" '{"currency": {"gp": 120}}' --reason "loot from the crypt"

# people (only with sign-in on; see docs/deploy.md)
uv run campaign users add you@example.com --admin --claim-unowned   # the first admin
uv run campaign invite -c curse-of-strahd --role player             # prints an invite link
```

With more than one campaign, pass `-c <slug>` or set `CAMPAIGN=<slug>`.

### Digesting reference material

```bash
uv run campaign add-source curse-of-strahd.pdf --digest   # or the checkbox in the web UI
uv run campaign digest            # digest (or resume) everything not yet digested
uv run campaign outline 1         # print the outline with section summaries
```

The digest turns a module into two things the assistant can use:
- **An outline**: a summary for the book, each chapter, and each section,
  with page ranges. The book and chapter summaries are part of every chat's
  system prompt. The `outline` tool drills into deeper sections.
- **A catalog** of NPCs, monsters, locations, items, quests, factions and
  lore. Each entry has aliases ("the hag", "the burgomaster"), pages, key
  facts with exact numbers, and a stat block where there is one. Keyed
  areas carry their map key ("C3", which works as a name), read-aloud text,
  and traps and checks with their DCs; anything of value is listed as
  treasure where it lies. The catalog becomes the world model's starting
  state (session 0; see [The world model](#the-world-model)).
- **The tables** to roll on: random encounters, treasure, rumors, each row
  as printed.

For a fight, the `encounter` tool takes the creatures and how many of
each (with the adventure's changes to any of them, like "45 HP, vulnerable
to fire") and gives back their stat blocks, their XP and the encounter's
difficulty for the party by the DMG's rules (the party defaults to the
campaign's characters). In the Ask tab the answer shows it as an encounter
card: the difficulty, every stat block, an HP box per monster and a button
that rolls the monsters' initiative.

For running a session, the chat and MCP tools `stat_block`, `treasure`
(what's at a place, and what the party has already taken) and `roll_table`
work from these.

See [How the digest works](#how-the-digest-works) for the caching rules.

### Background jobs

Anything slow that turns an upload into campaign data is a **job**: a
session ingest, or reading a character sheet from a scan. Asking for one
stores the upload and adds a row to `jobs` saying what's wanted; a loop in
the server (`jobs.JobLoop`) works through them oldest first. The row is
the whole truth, so it doesn't matter whether the page stays open, and a
job interrupted by a restart or a deploy is simply run again from its
input: each kind is safe to redo (an ingest replaces the session's earlier
recording; a scan recognises the character its own earlier try made), and
keeps what it has already paid for (a transcription, the read sheet) in
its input so a redo doesn't buy it twice. Progress is written to
`job_log`, which the page shows live. Failures retry after 1 and 4
minutes, then show **Retry**; a failure that retrying can't fix (a file
that isn't a sheet, a character name already taken) fails at once.
**Cancel** stops a job at its next step and undoes what it wrote. A new
kind is a function in `jobs.KINDS` plus an endpoint that adds the row.

**Digesting is a target, not a job.** Clicking **Digest** records on the
source which model it should be digested with; a loop in the server
(`digest.Reconciler`) compares that with the digest cache and runs
whatever's missing. It keeps no state of its own, so it doesn't matter
whether the page is open, the server restarts or a deploy replaces it: the
next pass picks up from the database, and nothing finished is paid for
twice. **Stop** clears the target (calls already in flight finish and are
kept). A section that fails is retried after 1 and then 4 minutes, then
waits until someone clicks **Retry failed**, so a broken section can't run
up a bill.

**In the web UI**, uploading doesn't digest unless you tick the box. The
source's line shows how many calls are left and a rough dollar estimate,
and **Digest** asks before spending it. The first estimate is a guess; once
a few sections have been digested with the same model, it uses their
measured token counts. While a digest runs, the source's page refreshes on
its own: sections fill in as they finish, each one's entities can be opened
before the whole book is done, and the status line shows what's been spent.

### Using it from the Claude app (remote MCP)

When the web app runs with sign-in on, it also serves the tools over MCP at
`<public url>/mcp`, for every campaign you belong to. In claude.ai (desktop
or mobile), add it under Settings → Connectors as a custom connector; the
**Account** page (click your email) shows the URL. Claude sends you to the
app to sign in with Google and asks you to allow it, then acts as you:

- **Your view of each campaign.** Every tool takes the campaign's slug
  (`list_campaigns`); in a campaign where you're a player it gets the
  spoiler-filtered world, and changing the world needs editor access.
  Anyone in a campaign can add their own character (`create_character`),
  which is how a photographed or scanned paper sheet gets entered.
- **The app's own assistant** (`ask_assistant`): asks the Ask tab's chat a
  question, as you, and returns its answer, the lookups it made and what
  it cost. The exchange is saved as one of your conversations, so it can
  be read and continued in the app. Useful for testing how the app answers.
- **OAuth 2.1** (`oauth.py`): dynamic client registration, PKCE, codes and
  tokens stored as hashes, access tokens for an hour and rotating refresh
  tokens for 30 days, all tied to your user. The Account page lists
  connected apps and disconnects them; removing a user revokes theirs.

### Using it locally from Claude Code or Claude Desktop (MCP over stdio)

The same tools are also available over local MCP, against the database on
this machine. Claude Code or Claude Desktop can then be the assistant, and
no API key is needed:

```bash
claude mcp add strahd -- uv --directory /path/to/campaign run campaign mcp -c curse-of-strahd
```

Add `--read-only` to expose only the lookup tools.

## How it works

```
 PDF / notes ──chunk by heading──▶ chunks + FTS5 index ◀── search_reference / read_reference
 digest ─────apply as session 0──▶ world model: entities, aliases, facts over sessions, events
 transcript ──Claude + tools────▶ the same world model, dated to that session; session recap
 chat ────────Claude + tools────▶ same tools; reads and writes
 character sheet (JSON, validated) ◀── update_character / `campaign char …`; linked to its pc entity
```

- **Storage** (`db.py`): one SQLite file. Reference text is split into
  chunks at headings, so a stat block or a room stays together. The chunks
  are indexed with FTS5 (BM25). Campaign state is the world model
  (`world.py`, below).
- **Retrieval** is agentic and doesn't use embeddings. Claude searches, then
  reads the full chunk or page before quoting numbers. Proper nouns and game
  terms dominate D&D questions, and keyword search handles those well.
  Embeddings can be added later if fuzzy questions turn out to miss.
- **Ingest** (`agent.py`): the transcript is stored as a searchable source.
  Claude then reads the current state, matches mangled speech-to-text names
  to known entities, records what changed, and writes a recap. Sheets are
  updated only when the transcript states a change outright.
- **Spoiler safety** is enforced in the database layer, not just in the
  prompt. In player campaigns, `dm_only` sources are hidden, and so is every
  fact the party hadn't learned by the session in question.
- **Model**: Claude Opus 5.5 with adaptive thinking (`CAMPAIGN_MODEL` to
  override), with server-side refusal fallback enabled. Prompt caching keeps
  the repeated transcript cheap across the ingest tool loop.

## The world model

All sources feed one model of the world (`world.py`):

- **Entities** are identity only: a kind, a canonical name and aliases. The
  book, transcripts and chat all attach to the same entity, so "Morgantha",
  "the windmill hag" and "the baker" are one record.
- **Facts** are true over a range of sessions, according to a source.
  Session numbers are the clock, and session 0 is the world before play,
  which is what the book describes.
  - A fact with an **attribute** (status, location, holder, disposition,
    leader, allegiance, condition…) fills a single-valued slot. Recording a
    new value ends the old one at that session.
  - A fact without one is a statement that holds until it's ended.
  - Nothing is overwritten, so the assistant, the UI and the CLI
    (`campaign entity NAME --as-of 4`) can show the world as it was at any
    session, and where play diverged from the book.
  - Facts can also be **rumors** or **possibilities** rather than
    established facts.
- **Knowledge is per fact.** Each fact records since when the party has
  known it, so a player campaign can know Morgantha runs a bakery long
  before learning she's a hag.
- **Events** are the journal: what happened in which session, linked to the
  entities involved.
- **Runs** record who wrote what: a digest being applied, a session ingest,
  a chat, a manual edit. Undo works per run. Reverting a session removes
  exactly what its ingest recorded and reopens the facts it ended.
  Re-ingesting a session replaces the previous ingest of it.
- **Re-applying the book is safe.** It happens when a digest changes and is
  skipped when nothing did. Entities are matched by name and alias, so facts
  from play stay attached, and a book fact the table ended or learned keeps
  that history.
- **Summaries are synthesized** from all of an entity's facts and events,
  and cached under a hash of them, so they're rewritten only when something
  changed.

## How the digest works

The document's section tree comes from the PDF's outline (bookmarks),
Markdown headings, or 12-page groups when there's neither. Claude reduces
that tree bottom-up (`digest.py`):

- **Leaves** are sections' own text, split into parts above ~40k characters.
  Runs of small sibling sections are packed into one call. Claude returns a
  summary, the entities in that text, and its tables.
- **Branches** see only their children's digests, never raw text. Claude
  returns a summary of the section and says which children's entities are
  the same thing under different names. The merge itself is done in code,
  which refuses groups that mix kinds of thing (a named creature may be an
  npc or a monster) or are too big to be one thing. Exact duplicates are
  merged without asking.
- **Applying the catalog** to the world matches entries to entities by their
  own names, and by naming aliases only: a title ("the Titan"), a
  description ("the old boar") or a map key ("C3") can belong to several
  things, so it never joins two entries into one. A new version of these
  rules (`world.APPLY_VERSION`), or of the digest, takes effect when a
  server starts: it plans every source again, digests what changed, and
  applies cached catalogs again.
- **The root's** entities become the source's catalog. Tables are passed up
  unchanged.

Each node's output is cached under a hash of exactly what produced it:

```
leaf key   = H("leaf",   LEAF_VERSION,   model, title, text)
branch key = H("branch", BRANCH_VERSION, model, title, H(output of child 1), H(output of child 2), …)
```

So:

- **Editing one section** re-digests that leaf and the branches above it.
  Nothing else is redone.
- **A text change that doesn't change a leaf's digest** stops there. Parents
  hash their children's *outputs*, not their text.
- **Re-uploading, re-chunking**, or loading the same book into another
  campaign costs nothing.
- **Interrupted or failed runs resume** where they stopped. A failed section
  doesn't block its siblings.
- **It runs wide.** Every source waiting for a digest runs at once, with
  up to `CAMPAIGN_DIGEST_CONCURRENCY` calls in flight between them
  (default 32), and a section starts as soon as its own subsections are
  done, so a slow one holds up only the sections above it. The calls are
  just waiting on the API, so the limit is the account's rate limits, not
  the server: when the API says to slow down (429), the client waits as
  long as it asks and tries again, up to 10 times, before a section counts
  as failed.
- **Bumping `BRANCH_VERSION`** after changing the reduce prompt redoes only
  the branches. **Bumping `LEAF_VERSION`** redoes everything. Changing the
  model (`CAMPAIGN_DIGEST_MODEL`) also gives new keys.

The cache isn't tied to a source, so deleting and re-uploading a book is
also free.

## Status and next steps

Working and tested (`uv run pytest`): storage, PDF/Markdown chunking and
section trees, search, the world model (supersession, as-of queries,
per-fact spoilers, run-based revert, merges, book re-application), character
sheets, CLI, MCP tool registration, the web API including background ingest
and digest jobs, the clean-start path for old databases, and the agent loop
and digest cache rules against a scripted fake API. The web UI was also exercised in
headless Chromium at desktop and phone widths.

Not yet exercised: live API calls end to end against a real module (the
digest's request shape is checked against the real SDK, but it hasn't run
against the API), and
audio transcription (`transcribe.py` uses faster-whisper but hasn't been run
here). Likely next steps:

- **Speaker diarization** for recordings, so the transcript says who spoke.
- **Ask from the web UI**: a chat box on the campaign page, using the same
  agent as `campaign chat`.
- **In-game dates** as a finer clock than session numbers.
- **Review before commit** for ingest: show proposed changes and approve
  them, as an alternative to ingest-then-revert.
- **Scanned PDFs** need OCR. Text-layer PDFs work as they are.
- **Batch API for digests**: leaves don't need to finish quickly, and the
  Message Batches API costs half as much.
- **Embedding search** as a second retriever, if lookup and keyword search
  still miss things.
