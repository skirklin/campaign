# Chat benchmark

How well the Ask tab's assistant answers questions about a campaign, and
what each answer costs. It runs against a real campaign on the deployed
app, so the answers are the ones you'd get, the digest cache is the
app's, and every exchange stays in the app as a conversation to read.

## The corpus

A campaign called **Benchmark** (DM) with four reference sources, all
uploaded as "Published module" and digested:

| File | What | License |
|---|---|---|
| `chateau-rousseau.md` | Level 5 haunted-mansion adventure: keyed rooms, traps with DCs, ghosts, cursed rings | © 2021 Mario Panighetti, [CC BY-NC-SA 4.0](https://github.com/WaveBirdDash/dnd5e-adventures) |
| `guadana.md` | Level 5 Domain of Dread one-shot: carnival games, rollable tables, one fight | same |
| `030crc-monsters.md` | The author's homebrew monsters the adventures link to | © 2021 Mario Panighetti, [CC BY-NC-SA 4.0](https://github.com/WaveBirdDash/dnd5e-030crc) |
| `srd-5.1-bestiary.md` | Monsters, creatures and NPCs from the SRD 5.1 | © 2016 Wizards of the Coast, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), via [thejasonvu/dnd-5e-srd](https://github.com/thejasonvu/dnd-5e-srd) |

`build_corpus.py` makes the four files from those repositories (the
adventures' chapters joined into one file each, navigation links dropped,
the license on top). They aren't committed here; build and upload them.

## Running it

Each question in `questions.md` is put to the app's own assistant through
the MCP tool `ask_assistant` (a fresh conversation per question unless a
question says it follows another). The tool returns the answer, the
lookups it made and its usage. Each answer is graded against the
question's expected facts:

- **correct**: every expected fact, nothing false;
- **partial**: some expected facts missing, nothing false;
- **wrong**: a false claim (a number, a location, a rule the source
  doesn't say), or the question not answered.

A question marked *not in the sources* is correct only if the answer says
the sources don't have it rather than inventing it.

Results go in `results/<date>.md`: the grade and usage per question, then
totals (accuracy, tokens, dollars, seconds) and what went wrong.
