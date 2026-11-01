# Chat benchmark: run 3 (Sonnet digest), 2026-11-01

Campaign `benchmark` (DM) on the deployed app. First I re-digested all four reference sources with the configured digest model, `claude-sonnet-5-5`; they had been digested with `claude-haiku-5-5`. Then I put the 23 questions in `bench/questions.md` to the app's assistant with `ask_assistant`, each in a fresh conversation (conversations 49–71), in order, one at a time. Every answer reported the answering model `claude-opus-5-5`, the same as run 2. Only the digest changed between the two runs, along with whatever the app changed since (see §5).

I graded per `bench/README.md` as strictly as run 2 (`2026-10-31-run2.md`): one false or unsupported side claim makes an answer *wrong*, even when every expected fact is there. I checked doubtful claims against the sources with `search_reference`, `read_reference`, `stat_block` and `lookup`. For E1–E3, I re-ran each answer's ```encounter spec through `encounter` and graded the card's stats as part of the answer.

The connector's tools appear in this session as `mcp__3bb4a400-…__digest_status` / `digest_source` / `ask_assistant` and so on, not `mcp__campaign__*`. They are the campaign connector's own tools (same descriptions, same `benchmark` campaign), so I used them.

## 1. The re-digest

`digest_status` before: `configured_model` was `claude-sonnet-5-5`, and every source still had `target_model: claude-haiku-5-5`, all sections done and no failures.

| Source (id) | Sections | Entities before → after | Digested with before → after (tokens) | Cost before (Haiku) | Estimate (Sonnet) | Actual (Sonnet) | Actual vs estimate |
|---|---:|---:|---|---:|---:|---:|---:|
| chateau-rousseau (6) | 10 | 41 → **46** | haiku 83,008 → sonnet 64,313 | $0.0221 | $0.2022 (10 calls, 31,727 in + 13,877 out) | **$0.2697** | +33% |
| guadana (5) | 6 | 23 → **19** | haiku 45,306 → sonnet 35,769 | $0.0118 | $0.1200 (6 calls, 18,869 + 8,230) | **$0.1525** | +27% |
| 030crc-monsters (4) | 22 | 32 → **43** | haiku 172,809 → sonnet 151,956 | $0.0395 | $0.5356 (22 calls, 82,212 + 37,120) | **$0.5836** | +9% |
| srd-5.1-bestiary (3) | 105 | 328 → **351** | haiku 959,314 → sonnet 792,184 | $0.2480 | $2.4279 (105 calls, 376,627 + 167,460) | **$3.4563** | +42% |
| **Total** | 143 | 424 → **459** (+8%) | 1,260,437 → 1,044,222 | **$0.3214** | **$3.2857** (736,122 tokens) | **$4.4621** | **+36%** |

- **After:** every source has done = sections, `failed: 0`, and `digested_with` lists only `claude-sonnet-5-5`. No retries were needed.
- **Time:** I started all four `digest_source` calls at about 14:56:10 UTC, and they ran concurrently. At 14:56:30 Chateau had 6/10 sections done. At 14:57:40, three sources were complete and the SRD was at 104/105, with entities still 0 (entities are written at the end). At 14:58:45 everything was complete. **Wall time: about 2.5 minutes.**
- **Estimate vs actual:** the estimate (`calibrated_on: 0`, so it isn't calibrated yet) came in **36% low** overall: $3.29 estimated, $4.46 spent. It estimated 736k tokens and 1,044k were used (+42%). The error is largest on the SRD (+42%), the source with the most dense stat-block output.
- **Cost:** the Sonnet digest cost **13.9× the Haiku digest** ($4.46 vs $0.32). The SRD bestiary is 77% of it.
- **Entity ids kept:** existing entities kept their ids (Neta 2335, the same id the baseline's journal entry links to). The baseline's leftover journal entry and chat fact survived the re-digest (see §5).

### What the Sonnet digest got wrong (found while grading)
- **An NPC's in-character line recorded as a world fact.** Lynistad Rousseau fact 51224 says "Only she knows how to open the front door". In the source, this is Anni speaking in character ("the front door's downstairs, but only Mother knows how to open it… Neta says she found out how to open the door"). The book contradicts it: Neta knows, and Appendix A explains the rings. This caused N2's wrong answer.
- **A ring location compressed.** Signet Rings fact 51183 is "Emerald Signet in the Conservatory chest". It drops "under the tables" and the violet fungus that guards it. F1's answer repeats exactly this wording.
- **Ring entities merged.** The digest made one "Signet Rings" entity with *Emerald Signet*, *Sapphire Signet*, *Ruby Signet* as aliases, and attached the Ruby's item facts to it. There is *also* a separate Sapphire Signet entity (2346).
- **A stat block missing lines.** `stat_block("Swarm of Trickered Treats")` drops the source's **Resistances** (Bludgeoning, Piercing, Slashing) and **Languages**. The resistances do exist as a separate world fact (51544). F7's answer noticed the gap and recovered the resistances. S4's answer didn't.
- **No stat block for the Imp or the Ice Devil.** `stat_block("Imp")` and `stat_block("Ice Devil")` both return "no stat block in the digested material". These are the two SRD entries `build_corpus.py` splices together (a known corpus defect). Sonnet seems to have dropped both instead of extracting a mixed block. Other SRD blocks I tried are there and correct (Knight, Quasit, Violet Fungus, Skeleton, Magma Mephit, Adult Red Dragon).

## 2. Summary

**Accuracy: 21 correct (91%), 1 partial (4%), 1 wrong (4%).** Run 2: 21 correct (91%), 0 partial, 2 wrong (9%). Baseline: 19 correct (83%), 1 partial (4%), 3 wrong (13%).

- The headline is unchanged, but the errors moved:
  - Run 2's two wrong answers (F3, E1, both unlabelled side claims from the answering model) are now correct.
  - Two new failures (F1 partial, N2 wrong) both trace to the **Sonnet digest's** world facts.
- Counting expected facts only, this run has 22 correct and 1 partial (F1); run 2 had 23 of 23.

**Cost per answer: $0.0789**, 4% below run 2 ($0.0824) and 57% below the baseline ($0.1819). Tokens and latency are essentially flat against run 2.

| | Total run 3 | Mean run 3 | Mean run 2 | Mean baseline | Run 3 vs run 2 (mean) |
|---|---:|---:|---:|---:|---:|
| Input tokens | 218 | 9 | 9 | 9 | 0% |
| Cache-read tokens | 2,015,244 | 87,619 | 89,358 | 64,015 | −2% |
| Cache-write tokens | 105,595 | 4,591 | 5,099 | 26,021 | −10% |
| Output tokens | 24,004 | 1,044 | 1,056 | 1,307 | −1% |
| All tokens | 2,145,061 | 93,264 | 95,522 | 91,352 | −2% |
| Cost (USD) | $1.8151 | $0.0789 | $0.0824 | $0.1819 | −4% |
| Seconds | 282.7 | 12.3 (max 28.1, E1) | 13.0 (max 27.8, F1) | 14.6 (max 36.3, E1) | −5% |
| Lookups (tool calls) | 88 | 3.83 | 4.00 | 4.00 | −4% |
| API calls | 86 | 3.74 | 3.74 | 3.70 | 0% |

Totals for run 2 and the baseline: cache read 2,055,244 / 1,472,344; cache write 117,269 / 598,483; output 24,281 / 30,054; all tokens 2,197,012 / 2,101,097; cost $1.8950 / $4.1833; seconds 299.4 / 336.2; lookups 92 / 92.

F1 hit a cold cache again (29,718 cache-write tokens, $0.218). The last run was a day ago, so the prefix had expired. Without F1, the other 22 answers average **$0.0726** (run 2: $0.074). The run-2 price fit still holds (about $0.40/M cache read, $5/M cache write, $20/M output), and gives $1.814 against $1.815 billed. The split: cache reads $0.81 (44%), cache writes $0.53 (29%), output $0.48 (26%).

### By category

Run 3:

| Category | n | Correct | Partial | Wrong | Mean input | Mean cache read | Mean cache write | Mean output | Total cost | Mean cost | Mean s | Max s | Mean lookups |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Facts | 11 | 10 (91%) | 1 | 0 | 10 | 90,348 | 6,365 | 1,004 | $0.9690 | $0.0881 | 12.0 | 18.3 | 4.00 |
| Tables | 2 | 2 (100%) | 0 | 0 | 7 | 57,132 | 1,588 | 394 | $0.0774 | $0.0387 | 6.8 | 7.8 | 1.50 |
| Stat blocks | 5 | 5 (100%) | 0 | 0 | 8 | 69,635 | 2,065 | 993 | $0.2904 | $0.0581 | 10.3 | 15.6 | 2.20 |
| Whole encounters | 3 | 3 (100%) | 0 | 0 | 13 | 132,389 | 5,788 | 1,818 | $0.3549 | $0.1183 | 21.0 | 28.1 | 8.00 |
| Not in the sources | 2 | 1 (50%) | 0 | 1 | 9 | 80,902 | 2,359 | 874 | $0.1234 | $0.0617 | 11.2 | 14.7 | 3.00 |

Side by side (baseline → run 2 → run 3):

| Category | Correct | Mean cost | Mean s | Mean output | Mean lookups |
|---|---|---|---|---|---|
| Facts | 8 → 10 → 10 of 11 | $0.1847 → $0.0941 → $0.0881 | 13.9 → 13.5 → 12.0 | 1,165 → 1,043 → 1,004 | 4.09 → 4.45 → 4.00 |
| Tables | 2 → 2 → 2 of 2 | $0.1540 → $0.0384 → $0.0387 | 9.0 → 6.2 → 6.8 | 547 → 362 → 394 | 2.50 → 1.50 → 1.50 |
| Stat blocks | 5 → 5 → 5 of 5 | $0.1613 → $0.0605 → $0.0581 | 11.3 → 11.6 → 10.3 | 1,050 → 1,007 → 993 | 2.20 → 2.20 → 2.20 |
| Whole encounters | 3 → 2 → 3 of 3 | $0.2383 → $0.1060 → $0.1183 | 28.1 → 19.1 → 21.0 | 2,992 → 1,693 → 1,818 | 8.33 → 7.00 → 8.00 |
| Not in the sources | 1 → 2 → 1 of 2 | $0.1609 → $0.0811 → $0.0617 | 12.2 → 11.8 → 11.2 | 958 → 982 → 874 | 3.00 → 4.00 → 3.00 |

## 3. Per question

| ID | Grade now | Run 2 | Baseline | Lookups | Total tokens | Cost (USD) | Seconds | conversation_id |
|---|---|---|---|---:|---:|---:|---:|---:|
| F1 | **partial** | correct | correct | 8 | 132,205 | $0.2179 | 14.6 | 49 |
| F2 | correct | correct | correct | 3 | 95,734 | $0.0642 | 9.5 | 50 |
| F3 | correct | wrong | correct | 5 | 105,315 | $0.1005 | 18.3 | 51 |
| F4 | correct | correct | correct | 5 | 98,101 | $0.0832 | 12.7 | 52 |
| F5 | correct | correct | correct | 3 | 100,709 | $0.0771 | 10.3 | 53 |
| F6 | correct | correct | correct | 2 | 72,834 | $0.0536 | 7.3 | 54 |
| F7 | correct | correct | wrong | 5 | 100,829 | $0.0900 | 14.2 | 55 |
| F8 | correct | correct | partial | 5 | 106,112 | $0.1015 | 15.2 | 56 |
| F9 | correct | correct | correct | 2 | 71,806 | $0.0505 | 9.2 | 57 |
| F10 | correct | correct | correct | 2 | 70,539 | $0.0507 | 10.5 | 58 |
| F11 | correct | correct | wrong | 4 | 120,815 | $0.0798 | 10.7 | 59 |
| T1 | correct | correct | correct | 1 | 45,609 | $0.0249 | 5.7 | 60 |
| T2 | correct | correct | correct | 2 | 72,633 | $0.0525 | 7.8 | 61 |
| S1 | correct | correct | correct | 1 | 47,289 | $0.0478 | 13.0 | 62 |
| S2 | correct | correct | correct | 4 | 95,399 | $0.0668 | 10.3 | 63 |
| S3 | correct | correct | correct | 4 | 127,830 | $0.1080 | 15.6 | 64 |
| S4 | correct | correct | correct | 1 | 46,788 | $0.0410 | 8.1 | 65 |
| S5 | correct | correct | correct | 1 | 46,203 | $0.0268 | 4.4 | 66 |
| E1 | correct | wrong | correct | 11 | 217,623 | $0.1696 | 28.1 | 67 |
| E2 | correct | correct | correct | 5 | 103,304 | $0.0996 | 16.2 | 68 |
| E3 | correct | correct | correct | 8 | 99,095 | $0.0857 | 18.6 | 69 |
| N1 | correct | correct | wrong | 4 | 121,245 | $0.0855 | 14.7 | 70 |
| N2 | **wrong** | correct | correct | 2 | 47,044 | $0.0379 | 7.7 | 71 |

## 4. Partial and wrong answers

### F1 (partial): Where are the three Signet Rings, and how do they open the front door?
- **Present:** the Sapphire under frost in the Kitchen (R9) freezer; the Ruby in the jewelry box on the nightstand in Lynistad's Room (R★), reached by the dumbwaiter; and the door rule (attune to all three, press them to the red, green and blue pads). Everything else checks out too: Neta's price, the curses, Wisdom for ring spells, and "killing Goren makes the house fade away" (Chapter 1).
- **Missing:** for the Emerald it says only "Conservatory (R8), F2: in the chest". It doesn't say the chest is **under the tables**, or that the **violet fungus** above it attacks anything within 10 ft. Both are in the expected fact. The run-2 answer gave both. Under a lenient reading ("the Conservatory chest" is enough to find it), this would be correct.
- **Lookups:** looked up "Signet Rings"; read the outline; searched "Signet Rings attune all three press pads front door"; read three passages; searched "Lynistad's Room Ruby Signet"; searched "Conservatory chest Emerald Signet; Kitchen freezer frost Sapphire".
- **Diagnosis: digest, then answering.** The Sonnet digest's fact 51183 reads "Emerald Signet in the Conservatory chest", which is exactly the answer's wording. The Conservatory (R8) passage came up in the last search, but the answer never read it. Its snippet starts mid-sentence ("…on the table directly above it. The flower attacks…") and doesn't say "under the tables". The answer trusted the compressed world fact over the room text.

### N2 (wrong): What's Lynistad Rousseau's stat block?
- **Expected facts:** present. "Chateau Rousseau doesn't give Lynistad Rousseau a stat block… she never appears in the adventure." It invents no stats, and labels the SRD Mage/Archmage stand-in "My suggestion" (Mage CR 6 and Archmage CR 12 are correct for SRD Appendix MM-B).
- **False:** "She made the three Signet Rings, and **only she knows how to open the front door**." The first half is true (Appendix A). The second half is Anni's in-character line ("only Mother knows how to open it… Neta says she found out how to open the door"), not a fact. The book contradicts it: Neta knows how, and Appendix A gives the rule.
- **Lookups:** stat block "Lynistad Rousseau" (none); looked up "Lynistad Rousseau".
- **Diagnosis: digest.** The answer only did a `lookup`, and the Sonnet digest gave it fact 51224, "Only she knows how to open the front door", as a book-sourced fact. The answering model faithfully repeated a false fact it was handed. The other backstory bullets (planar-travel mage, Far Realm gateway, pocket dimension, captures people as food for Goren, shame, searching for a cure) all match facts 51223–51232 and the source.

### Graded correct, with notes
- **F3:** it says the *Remove Curse* scroll "can also undo Neta's life-force curse". The book says only that the HP loss "remains until the curse is removed". This is an unlabelled inference, but it follows from the rules: *remove curse* ends curses. It isn't a claim the source contradicts, unlike run 2's "revivify is how". "Winning a friendly fight against her Magma Mephits" reads "win" into "friendly combat for the information". Ulvaeus, whom Neta is compared to, pays out "if defeated", so I accepted it.
- **F5 and E2:** both report the baseline's leftover journal entry as campaign state, as in run 2 ("the party already fought her once, in session 0"). F5's "if they won that fight, the book suggests she would hand over this information as the prize" is hedged.
- **F7:** it notices the digested Swarm stat block lacks resistances and recovers them from "another entry for this creature". This is correct: the source has them, as does world fact 51544. "Most swarms have that, so I'd use it" is labelled as its own call.
- **S1:** the stat block is exactly right. The comments after it include "psychic-free magic damage such as fire". It's garbled, but not false, since he's immune to psychic and doesn't resist fire. It also notes that only the Str and Con saves look proficient, which is hedged and correct per the save table.
- **S3:** the digest has no Imp stat block, so the assistant read the SRD text. It reported the ice-devil splice again and dropped those lines, which the baseline and run 2 also did. The stats are right (AC 13, HP 10 (3d4 + 3), CR 1), and so is the Spare Bedroom (R6) note with the two *Potions of Speed*.
- **S4:** every expected fact is there. But the stat block, passed through from the digest, has **no Resistances or Languages line**. A DM would run the swarm without its B/P/S resistance. Under the README's rules (only false claims or missing expected facts count), that isn't graded down. It's a digest defect, not an answering error.
- **E1:** the spec re-runs to Skeleton ×3 (AC 13, HP 13 (2d8 + 4), CR 1/4) plus a Grinning Gourd (AC 12, HP 66 (12d6 + 24), CR 3), 850 XP, with no difficulty and no party. The bone knight is correctly cited as *Eberron: Rising from the Last War* and not loaded. The end condition is right. The Gourd details match its stat block (Constrict reach 20 ft., 2d6 + 3, escape DC 13, 4d6 poison; Reel; Bite 1d6 + 3 plus 2d4 fire; Explode DC 14, 3d10 fire, prone). So do the clearing, the pan de muerto (1d6 HP per turn for 1 minute; no food or drink for 1d4 days), Dídac's *dimension door* and "written for 5th level". It now has a separate "**Suggestions (mine, not the module's)**" section, and **no "revivify is how" claim**.
- **N1:** "[Ferran] stays out of it himself. He only fights alongside them if the party is high enough level." This leaves out that he "will strike down hard against any who attack him directly". The "only" is accurate about joining the fight (the high-level option), so I didn't count it, but it is the same kind of compression as run 2's F3. The SRD **knight** stand-in is labelled "my suggestion" (CR 3 is right).

## 5. What the Sonnet digest changed

**Better (2):**
- **F3:** wrong → correct. It read Neta's Appendix B passage this time and listed all three prices.
- **E1:** wrong → correct. No unlabelled "revivify" inference, and suggestions are now in a section marked as its own.

Neither depends on anything the new digest added. Both answers read the source passages, and the difference is in how the answering model wrote them up. This looks like run-to-run variance in the answering model (or prompt changes since run 2), not the digest's doing.

**Worse (2), both from the digest:**
- **F1:** correct → partial. The compressed location fact ("in the Conservatory chest").
- **N2:** correct → wrong. Anni's in-character line stored as "Only she knows how to open the front door".

**Neutral but notable:**
- The Swarm stat block lost its resistances (F7 recovered them, S4 didn't).
- The Imp has no stat block at all (S3 recovered by reading the text). Entities grew 8% (424 → 459), mostly SRD (+23) and homebrew (+11). Guadaña went down (23 → 19).

**Run 2's leftover issues:**

| Issue | Run 3 |
|---|---|
| **F3's unlabelled inference** ("only after a book") | Gone. All three prices are given. One mild unlabelled inference remains ("*Remove Curse* can undo Neta's curse"), but it's rules-true. |
| **E1's unlabelled inference** ("revivify is how he brings back fallen PCs") | Gone. It says only "He revives any fallen PCs", and labels its suggestions. |
| **SRD Imp entry** (ice devil's actions spliced in) | Still in the corpus. With Haiku, the assistant dropped the spliced lines from what it found. With Sonnet, **neither the Imp nor the Ice Devil has a stat block**, so `stat_block` fails and the assistant has to read the raw text (S3: 4 lookups, $0.108, the most expensive stat-block answer). It still got it right. |
| **E2's level-1 default** | Fixed in the app. `encounter` now returns `party: null` with "no_difficulty: … ask for them rather than assume". E1, E2 and E3 all say they can't rate difficulty without the party's level, and mention the module is written for level 5. |
| **Leftover baseline journal entry (id 1) and chat fact 50676** | Still there. The re-digest kept Neta's entity id (2335), so both survived. F5 and E2 again report them as campaign state. |

New since run 2 (app-side, not the digest): answers render stat blocks in a ```statblock fence (F7, S1, S2, S4).

### Writes to the campaign this run: none
`recent_journal` before and after the run holds one entry, id 1, the baseline's E2 entry. I ran `lookup` for Neta Rousseau, Ferran, Lynistad Rousseau, Sapphire Signet, Signet Rings, Play Room, Swarm of Trickered Treats and Animated Pair of Boots. All facts are book-sourced except the baseline's fact 50676 (chat, "Neta is in combat with the party…"), which is still in `past`. Several answers offered to record outcomes ("Tell me how it ends and I'll record the result") without writing anything, which is the intended behaviour.

## 6. Observations and next changes

**The Sonnet digest didn't buy accuracy on this benchmark, and it cost 14× more.**
- Accuracy is unchanged at 21 of 23. Answering cost and latency are flat (−4% and −5%, within noise).
- The two failures it introduced are both digest *fidelity* errors:
  - a fact that lost detail (F1);
  - a character's claim promoted to a fact (N2).
- They matter more than they look. The answering model now trusts `lookup` facts enough to answer from them without reading the passage (N2 made only two lookups). So a wrong world fact passes straight through.
- Stat-block extraction also regressed in two places: a dropped Resistances line, and no block at all for the spliced Imp/Ice Devil entry.
- The digest estimate was 36% low.

**The answering side looks steadier.** Run 2's wrong answers were unlabelled inferences and overstatements. None appeared this time. Suggestions are now consistently set apart ("My suggestion, not from the book", "Suggestions (mine, not the module's)"). The `encounter` level fix works.

**Next changes most likely to help** (most impact first):
1. **Make the digest attribute claims made by characters.** Tell the digest prompt that dialogue and NPC beliefs ("Anni says only Mother knows…") are recorded as "X says/believes …", or skipped, never as plain facts. Optionally, drop any fact the source contradicts elsewhere. This fixes N2, and the same mistake could hit any module with unreliable narrators.
2. **Keep the location detail in item and room facts, and have the assistant read before answering "where".** Digest facts should keep the full placement ("in the chest under the tables, beneath the violet fungus that attacks within 10 ft."), not a summary. The answering prompt could also require reading the room passage before stating where something is. Also split the merged "Signet Rings" entity: it holds all three rings as aliases plus the Ruby's facts, while Sapphire also exists separately. This fixes F1.
3. **Validate digested stat blocks against the source.** After extracting, check that every field the source block prints (Resistances, Immunities, Languages, Senses) is in the stored block, and re-extract or fall back to the raw text when one is missing. Fix the SRD Imp/Ice Devil splice in `build_corpus.py` (open since the baseline), so both get a stat block again.
4. **Choose the digest model per source, and calibrate the estimate.** The SRD bestiary was 77% of the $4.46, and Haiku digested it with no accuracy cost on this benchmark. Consider Haiku (or a cheaper model) for bestiary-style sources and Sonnet for adventures. Calibrate the estimate (`calibrated_on: 0`) on this run's actuals: output ran about 40% over the estimate.
5. **Clean the campaign before the next run.** Delete journal entry 1 and chat fact 50676. They've now been reported as campaign state in two runs (F5, E2), which keeps those answers not quite comparable.

## 7. Appendix: full answers

Each heading gives the grade, the conversation, the API calls, the token counts (input / cache read / cache write / output), the cost and the seconds.

### F1. Where are the three Signet Rings, and how do they open the front door?

Grade: **partial** · conversation 49 · 5 API calls · in 12 / cache read 101,035 / cache write 29,718 / out 1,440 · $0.2179 · 14.6 s

Lookups:
1. Looked up “Signet Rings”
2. Read the outline
3. Searched the books for “Signet Rings attune all three press pads front door”
4. Read a passage of the book
5. Read a passage of the book
6. Read a passage of the book
7. Searched the books for “Lynistad's Room Ruby Signet”
8. Searched the books for “Conservatory chest Emerald Signet; Kitchen freezer frost Sapphire”

<details><summary>Answer</summary>

The rings haven't come up in play yet. All three are still where the book puts them (*Chateau Rousseau*, Chapter 4 and Appendix A):

| Ring | Where | Notes |
|---|---|---|
| **Emerald Signet** | **Conservatory (R8), F2**: in the chest | Same chest holds two *Potions of Greater Healing* |
| **Sapphire Signet** | **Kitchen (R9), F1**: inside the freezer, under a layer of frost | |
| **Ruby Signet** | **Lynistad's Room (R★), F3**: in a small jewelry box on the nightstand | The room is sealed. The way in is the **dumbwaiter in the Kitchen (R9)**, which carries one creature at a time, worked by rope and pulley |

### How the door opens
The front door, in the F1 Hallway, has no handle and no keyhole. It has **three circular pads ringed in red, green and blue**. Here's the rule (Appendix A, Signet Rings):

- **One or more creatures must attune to all three rings** and press them against the matching pads on the inside of the door.
- Once all three are on their pads, **the door opens and anyone inside can leave**.

### Notes for running it
- **Neta (Library, R4)** explains how the rings work, where the Emerald and Sapphire are, and the dumbwaiter route. She never found the Ruby but guesses it's in Lynistad's Room. Her price is **a book she hasn't read** (a journal or spellbook works). Failing that, she takes **life force from a volunteer: their HP maximum drops by 1d6 until the curse is removed**. The party can also choose to fight her for the information.
- **Each ring has a curse.** When someone attunes, give them the ring's description but keep the curse hidden until it triggers. Rings that cast spells use **Wisdom** as the spellcasting ability.
- There's another way out: **killing Goren** makes the house fade away.

</details>

### F2. What happens when someone opens the freezer in the kitchen?

Grade: **correct** · conversation 50 · 4 API calls · in 10 / cache read 92,413 / cache write 2,603 / out 708 · $0.0642 · 9.5 s

Lookups:
1. Searched the books for “kitchen freezer”
2. Read a passage of the book
3. Looked up “Sapphire Signet”

<details><summary>Answer</summary>

Opening it sets off a cold blast. Whoever opens the freezer first makes a **DC 15 Dexterity saving throw** as freezing wind rushes out (Chateau Rousseau, Ch. 4, Kitchen R9):

- **Fail:** **2d6 (7) cold damage** and knocked **prone**.
- **Success:** half damage, and they stay on their feet.

The blast only happens once. After the door is open the wind dies down and anyone can walk in.

**What's inside:** The freezer is a magically chilled walk-in ice box for storing meat. The **Sapphire Signet** lies under a layer of frost, and it's one of the three rings that open the front door. The play records show nobody has taken it yet.

The door is a mirrored metal door on the kitchen's west wall. It's latched shut but not locked.

**About the ring (keep this hidden until it triggers):** It's a bone ring with a sapphire. It needs attunement. It gives:
- advantage on Constitution saves to keep concentration
- unarmed strikes that deal 1d6 slashing
- one self-only *Polymorph* per day

The curse starts after the wearer casts *Polymorph* through the ring. From then on they can't take the ring off. Each time they take damage, they make a **DC 10 Wisdom save** or turn into a sheep for 1 minute, repeating the save at the end of each of their turns. The curse ends at dawn.

</details>

### F3. How can the party get into Lynistad's room?

Grade: **correct** · conversation 51 · 4 API calls · in 10 / cache read 97,108 / cache write 6,820 / out 1,377 · $0.1005 · 18.3 s

Lookups:
1. Looked up “Lynistad's room”
2. Searched the books for “Lynistad's Room dumbwaiter sealed door”
3. Read a passage of the book
4. Read a passage of the book
5. Read a passage of the book

<details><summary>Answer</summary>

The intended way in is **the dumbwaiter in the Kitchen (R9)**. The front door is warded so that only Lynistad can get through. *(Chateau Rousseau, Ch. 4 "Lynistad's Room (R★)" and "Kitchen (R9)"; Appendix B "Neta Rousseau")*

### Route 1: The dumbwaiter (intended)
- A small door in the northwest of the **Kitchen (R9)** opens on a dumbwaiter whose shaft runs straight up to Lynistad's room.
- **One creature at a time** climbs in and rides it by pulling the rope and pulley.
- A pull rope beside the dumbwaiter door upstairs rings a bell above the kitchen door. The party can use it to signal each other between trips.
- The Kitchen is the one room with a door to the room next to it: you can walk to and from the Dining Room without rolling for a random room.
- While they're in the Kitchen, the **Sapphire Signet** is in the freezer. Whoever opens the freezer first makes a **DC 15 Dex save**, taking **2d6 cold damage** and falling prone on a fail, or half damage and staying standing on a success.

### How they find out
**Neta** in the **Library (R4)** knows about the dumbwaiter route. She'll tell them for:
- a book she's never read (a journal, spellbook or other reading material will do), or
- life force from a volunteer, a minor curse that lowers their **HP maximum by 1d6** until it's removed, or
- winning a friendly fight against her **Magma Mephits** (one per party member).

### Route 2: The front door (hard)
- The room can't be reached through the house's random-room doors. Its walls and door can't be passed through, even by ghosts.
- The door has an **Arcane Lock** and, on the inside, a **Glyph of Warding** that **Detect Magic** can find. Both let only Lynistad through.
- If the glyph goes off, a stored **Thunderwave** fires (**DC 19 Con save**) and pushes everyone near the doorway back into the hallway. The **boom is heard all through the house and alerts Goren**. The door also slams shut and a **second Arcane Lock** activates.
- After that the glyph is used up, so the party can get in if they get rid of the second lock.

### Inside
- **Ruby Signet** in the jewelry box on the nightstand.
- In the chest at the foot of the bed: **300 gp** in gold and gems, a "Bag of Holding" that is really a **Bag of Devouring**, a **Potion of Greater Healing**, and a **Spell Scroll of Remove Curse**. The scroll can also undo Neta's life-force curse.

No play has been recorded for this house yet, so everything above is as the book has it. The party hasn't learned about the dumbwaiter or the Ruby Signet's location.

</details>

### F4. How does moving between rooms work in the Chateau?

Grade: **correct** · conversation 52 · 4 API calls · in 10 / cache read 92,246 / cache write 4,712 / out 1,133 · $0.0832 · 12.7 s

Lookups:
1. Read the outline
2. Searched the books for “House Mechanics doors hallway random room d12”
3. Read a passage of the book
4. Read a passage of the book
5. Read a passage of the book

<details><summary>Answer</summary>

Hallways and stairs work normally. **Opening a door from a hallway into a room sends you to a random room.** This is from *Chateau Rousseau*, Chapter 3: House Mechanics.

### The core rule
- Each time a creature opens a room door **from a hallway**, roll a **d12**. Every room title in Chapter 4 has a number (R1–R12), and the roll decides which room is behind the door.
- That link holds **until the door closes**. Then:
  - **Reopened from the hallway:** roll the d12 again.
  - **Reopened from inside the room:** the door opens onto the room's own hallway.
- **Doors close softly on their own within a minute** unless a creature or object holds them open. This makes it easy for the party to get split up.

### Several doors at once
If two or more doors are opened at the same time, every door after the first opens to a room **on the same floor as the first door opened**. This lasts until all the doors are closed. The book includes it to keep things easier for you to track.

### Exceptions
- **Ghosts** (Anni, Neta, Ulvaeus) ignore the randomizing. They pass through doors and walls.
- **Mariner's Bow:** a creature attuned to it who looks through its scope can open a door to the **correct** room.
- **Kitchen (R9) ↔ Dining Room:** the kitchen's east door connects the two rooms directly, with no randomizing.
- **Lynistad's Room (R★):** the d12 can never reach it, and even ghosts can't get through its walls. Its door has an *Arcane Lock* and a *Glyph of Warding*. If the glyph goes off, it releases a *Thunderwave* (**DC 19 Constitution save**) that pushes creatures back into the hallway. The boom alerts Goren, and a second lock activates. The intended way in is the **kitchen dumbwaiter**: creatures ride it one at a time, using a rope and pulley.

### Hard limits
You can't destroy or magically pass through any doors, walls, ceilings or floors. Windows facing outside can't be opened or broken.

</details>

### F5. Neta has information we need. What does she want for it?

Grade: **correct** · conversation 53 · 4 API calls · in 10 / cache read 95,328 / cache write 4,563 / out 808 · $0.0771 · 10.3 s

Lookups:
1. Looked up “Neta”
2. Searched the books for “Neta book never read before life force Hit Point Maximum”
3. Read a passage of the book

<details><summary>Answer</summary>

Neta wants **a book she's never read**. She has already read everything in the house.

**Her price (any one of these):**
1. **A new book.** The book leaves this open. A journal, a spellbook or any other reading material from a character's starting equipment will generally satisfy her.
2. **Life force from one volunteer**, if the party has no books. The volunteer takes a minor curse: **Hit Point Maximum reduced by 1d6** until the curse is removed.
3. **Friendly combat.** She's happy to fight for it. If the party agrees, or attacks her, **Magma Mephits leap from the fireplace, one per party member**. She lets them fight for her and only defends herself if attacked. Her stats: **AC 12, HP 33**, CR 2.

**What she tells them once paid:**
- How to use the **Signet Rings**, and where two of them are.
- She never found the **Ruby Signet**. She assumes it's in **Lynistad's Room (R★)**, the one room she can't visit.
- The front door of Lynistad's Room can't be opened, but the **dumbwaiter in the Kitchen (R9)** leads straight into it.

**Table note:** If Goren comes into the Library, she turns invisible until he leaves.

**Your journal:** The log shows the party already fought her once, in session 0, with four Magma Mephits. That combat is now marked as over. If they won that fight, the book suggests she would hand over this information as the prize. Tell me how it ended and I'll record it.

*Source: Chateau Rousseau, Appendix B: The Rousseau Family, Neta Rousseau.*

</details>

### F6. What happens if a player character dies in the Chateau?

Grade: **correct** · conversation 54 · 3 API calls · in 8 / cache read 69,226 / cache write 3,072 / out 528 · $0.0536 · 7.3 s

Lookups:
1. Searched the books for “character dies return as spirit ghost”
2. Read a passage of the book

<details><summary>Answer</summary>

They come straight back as a spirit, so dying once doesn't end their game. Dying a second time does. This is one of the book's suggested house rules (*Chateau Rousseau*, Chapter 2: Suggested House Rules).

**When a PC dies**, they immediately return as a spirit and:
- regain **all spell slots**, with their other features restored as if after a **Long Rest**;
- have their **Hit Point Maximum halved**;
- become **Undead** (creature type);
- count as being in a **Heavily Obscured** area whenever **Goren** tries to find or attack them, because he has trouble perceiving spirits.

**If a spirit drops to 0 HP**, it is **destroyed for good**. Only divine intervention, *Wish* or a similarly powerful feature can bring it back.

**At the end:** if the party escapes the Chateau or defeats Goren, every spirit that wasn't destroyed comes back to life and leaves with the party.

Anni also tells new arrivals they'll "all have fun together as ghosts," so the rule fits the house's story.

</details>

### F7. What's in the play room?

Grade: **correct** · conversation 55 · 4 API calls · in 10 / cache read 94,921 / cache write 4,401 / out 1,497 · $0.0900 · 14.2 s

Lookups:
1. Looked up “Play Room”
2. Treasure at “Play Room”
3. Searched the books for “Play Room candies bowl toys”
4. Read a passage of the book
5. Stat block for “Swarm of Trickered Treats”

<details><summary>Answer</summary>

The Play Room (**R2**, third floor) has two **Potions of Healing** and a **Diviner's Die** in the toy chests, and a candy bowl that's a monster. Nothing has happened there in play yet, so everything is still as the book has it.

> A thick layer of dust covers all the furniture that would otherwise be brightly colored. Various boxes sit about filled with ancient toys in varying states of deterioration.

### What's in the room
- **Toy chests:** searching them finds a **Diviner's Die** and **two Potions of Healing**. The book doesn't give a DC, so searching just works.
- **Glass bowl of old candies on a table:** if anyone tries to take a candy, the bowl becomes a **Swarm of Trickered Treats** and attacks that person. While the candies sit still, *False Appearance* makes them look like ordinary candy.

(Chateau Rousseau, Chapter 4, F3 › Play Room (R2). The search didn't give a page number.)

### Swarm of Trickered Treats
The adventure points to an outside stat block. This one is from the Zero-to-Thirty CR homebrew monsters:

```statblock
Swarm of Trickered Treats
AC 12 · HP 28 (4d8 + 10) · Speed 0 ft., fly 10 ft. · CR 1/4 (50 XP)
STR 2 (-4)  DEX 15 (+2)  CON 14 (+2)  INT 1 (-5)  WIS 3 (-4)  CHA 1 (-5)
Medium swarm of Tiny constructs, same alignment as its creator
Initiative +2 (12)
Immunities Blinded, Charmed, Deafened, Exhaustion, Frightened, Grappled, Paralyzed, Petrified, Poison, Prone, Restrained, Stunned
Senses Blindsight 10 ft. (blind beyond this radius), Passive Perception 6

Traits:
- Antimagic Susceptibility. Incapacitated in an antimagic field. If targeted by dispel magic, must succeed on a Constitution Saving Throw against the caster's Spell Save DC or fall Unconscious for 1 minute.
- False Appearance. While motionless and not flying, indistinguishable from a normal pile of candy.
- Swarm. Can occupy another creature's space and vice versa, and can move through any opening large enough for a Tiny creature. Can't regain Hit Points or gain Temporary Hit Points.

Actions:
- Cavities. Melee Weapon Attack: +4 to hit, reach 0 ft., one target in the swarm's space. Hit: 5 (2d4) Bludgeoning damage, or 2 (1d4) Bludgeoning damage if the swarm has half of its Hit Points or fewer. If the target has teeth and was hit, they must succeed on a DC 12 Constitution Saving Throw or take 2 (1d4) Poison damage, with Disadvantage if they previously took Poison damage from a trickered treat in the last day.
```

**Note:** the stat block above doesn't list them, but another entry for this creature gives it **resistance to bludgeoning, piercing and slashing**. Most swarms have that, so I'd use it.

When the party loots the room or fights the candies, tell me and I'll record it.

</details>

### F8. Is any treasure in the house not what it seems?

Grade: **correct** · conversation 56 · 4 API calls · in 10 / cache read 97,755 / cache write 6,973 / out 1,374 · $0.1015 · 15.2 s

Lookups:
1. Read the outline
2. Searched the books for “curse hidden appears to be disguised”
3. Read a passage of the book
4. Read a passage of the book
5. Looked up “Bag of Devouring”

<details><summary>Answer</summary>

Yes. In *Chateau Rousseau*, two kinds of treasure aren't what they seem:

### 1. The "Bag of Holding" is a Bag of Devouring
It's in the **chest at the foot of the bed in Lynistad's Room (R★)**. The book says the chest holds "what appears to be a *Bag of Holding* (actually a *Bag of Devouring*)." The same chest holds 300 gp in gold and gems, a *Potion of Greater Healing* and a *Spell Scroll* of *Remove Curse*. According to the world records, the chest hasn't been touched and the party hasn't learned the bag's secret.

### 2. All three Signet Rings carry hidden curses
Appendix A tells you to **give the player only the ring's normal description when they attune, and keep the curse secret until it triggers**. Each curse makes the ring **impossible to remove** and **ends at dawn**.

| Ring | Where | Curse triggers when… | Curse effect |
|---|---|---|---|
| **Emerald** | Conservatory | the wearer spends a charge for the first time | Damage to any ally within 30 ft is **split evenly** with the wearer, before resistance or immunity. The wearer's share can't be reduced. |
| **Ruby** | Jewelry box on the nightstand in Lynistad's Room | the wearer has spent **all 3 charges** | Whenever an ally within 30 ft is hit by an attack the wearer can see, the wearer **automatically swaps places** with the ally. The attack **auto-hits** the wearer and deals **1d4 extra necrotic** damage. |
| **Sapphire** | Kitchen freezer | the wearer casts *Polymorph* through the ring | Whenever the wearer takes damage, they make a **DC 10 Wisdom save** or turn into a **sheep for 1 minute**. They can repeat the save at the end of each of their turns. |

The rings are dangerous because the party needs all three, attuned and pressed to the pads inside the front door, to escape that way. They'll want to use the rings' powers along the way, and that's what sets off the curses.

**My suggestion, not from the book:** the *Remove Curse* scroll in the same chest is a natural fix for whichever ring curse bites hardest. Keep that in mind if the party gets into Lynistad's Room before dawn.

</details>

### F9. In Guadaña's Battle of the Bands, how does the DC work and what's the prize?

Grade: **correct** · conversation 57 · 3 API calls · in 8 / cache read 69,134 / cache write 2,030 / out 634 · $0.0505 · 9.2 s

Lookups:
1. Searched the books for “Battle of the Bands Performance DC prize”
2. Read a passage of the book

<details><summary>Answer</summary>

Battle of the Bands is a **group Charisma (Performance) check** made each round against a rival band, with the crowd as judge. The DC starts at **13** and moves depending on how the party does.

**How the DC works:**
- **Starting DC: 13**
- **Group succeeds:** the DC drops by **2**, because the crowd is warming to them.
- **Group fails:** the DC rises by **1**, because the crowd jeers and backs the other band.
- **Tie:** counts as neither a success nor a failure.
- The game ends when the band reaches **3 successes** (they win) or **3 failures** (they lose).

**Helping out:** on their turn, a band member can take other actions to either give a bandmate advantage on their Performance check or add a different skill to the group check.

**Prize:** if the band wins, each participant gets **una calavera de azúcar (sugar skull)**. It's one of the module's uncommon food items, and it summons an **esqueleto de azúcar** (a sugar-skeleton construct) to fight as an ally. Its full write-up is in the module's Premios section, and the creature's stat block is in Monstruous.

*Source: Guadaña › Juegos de Guadaña › Battle of the Bands. The text I have doesn't include page numbers.*

</details>

### F10. What does Ferran do to the party, and when does he stop?

Grade: **correct** · conversation 58 · 3 API calls · in 8 / cache read 68,019 / cache write 1,785 / out 727 · $0.0507 · 10.5 s

Lookups:
1. Searched the books for “Ferran's Reciprocity”
2. Read a passage of the book

<details><summary>Answer</summary>

**Ferran's Reciprocity** (Guadaña, "Ferran's Reciprocity"): Ferran shows up and **demands a blood payment** from the party. He doesn't fight it out himself. He **summons three skeletons and one Grinning Gourd** to fight for him.

### When he stops
The fight ends as soon as **either** of these happens:
- **All his minions are defeated**, or
- **Two party members fall unconscious.**

Then he is satisfied that the party has paid and his bloodlust is slaked for now. **He revives any fallen party members** and lets them finish the festivities in peace. Afterward, **Dídac apologizes** profusely and gives every guest **pan de muerto** as they're sent home.

### Running notes
- This is the **only planned combat** in Guadaña. The book says to adjust it for party size and level.
- Ferran sends his skeletons in rather than getting his hands dirty, but he **hits back hard at anyone who attacks him directly**.
- **Higher-level option:** Ferran can fight alongside his minions. In that case he **doesn't yield until either he falls or the whole party is down**, so the "two unconscious" stopping point no longer applies.
- His spells (save DC 15, +7 to hit) include *revivify* and *animate dead*, which suit the revive-and-summon story.
- **Dídac** stays out of the fight. He only defends himself, and he'll cast *dimension door* to get away if he's in danger.

If you'd like, I can put the skeletons and Grinning Gourd into an encounter card with the difficulty for your party.

</details>

### F11. What are Dídac's spellcasting stats?

Grade: **correct** · conversation 59 · 5 API calls · in 12 / cache read 116,647 / cache write 3,334 / out 822 · $0.0798 · 10.7 s

Lookups:
1. Stat block for “Dídac”
2. Searched the books for “Dídac spellcasting”
3. Read a passage of the book
4. Searched the books for “warlock of the Archfey”

<details><summary>Answer</summary>

Dídac is an **11th-level spellcaster** who casts with **Charisma**.

- **Spell save DC 17**
- **Spell attack +9**
- He uses the spell list from the *warlock of the Archfey* stat block.
- He needs **no material components**.

*(Guadaña, "Dídac and Ferran › Dídac")*

**His spell list isn't in your loaded material.** Guadaña only points to the *warlock of the Archfey* stat block, and that stat block isn't in the SRD bestiary or any other loaded source, so I can't give you the actual spells or slots. You'll need the book that prints that stat block.

**How the rest of his stats are built:**
- **Base stat block:** *bone knight*, which Guadaña cites from *Eberron: Rising from the Last War*. That isn't loaded here either. Use it with a **+4 proficiency bonus**.
- **Shared with Ferran:**
  - He counts as fey and undead.
  - **Fey Rebirth:** if he dies in Guadaña, he comes back with full HP 1d4 days later where he died.
  - **Legendary Resistance (3/Day).**
- **Dídac only:**
  - Lawful neutral.
  - STR 10 (+0), DEX 14 (+2), CHA 20 (+5).
  - He fights with a **rapier** instead of the greatsword and longbow.

For comparison, Ferran's spellcasting is **DC 15, +7**, with paladin spells.

</details>

### T1. Roll on Guadaña's Random Spicy Pepper table for me.

Grade: **correct** · conversation 60 · 2 API calls · in 6 / cache read 45,045 / cache write 284 / out 274 · $0.0249 · 5.7 s

Lookups:
1. Rolled on “Random Spicy Pepper”

<details><summary>Answer</summary>

**d10 roll: 5. Sweet Heat Pepper (DC 12)**

> The pepper is hot but delicious. You do not have disadvantage on this saving throw if you would normally have it as a result of eating another pepper.

The eater makes the Pepper Challenge's **Constitution save against DC 12**. If earlier peppers would give them disadvantage, they roll this one normally.

</details>

### T2. In the pepper challenge, what happens on a 7?

Grade: **correct** · conversation 61 · 3 API calls · in 8 / cache read 69,219 / cache write 2,892 / out 514 · $0.0525 · 7.8 s

Lookups:
1. Searched the books for “Pepper Challenge pepper table”
2. Read a passage of the book

<details><summary>Answer</summary>

A 7 (or an 8) on the Random Spicy Pepper table means the character got a **Chile Caótico**, which needs a **DC 16 Constitution save**.

- **Before the save**, they roll on the **Wild Magic Surge table** from the *Player's Handbook*.
- **If the surge is a spell**, they cast it using any Spellcasting or Pact Magic feature they have. Metamagic can't change it. It **doesn't need concentration** and lasts its full duration.
- **Characters who can't cast spells** use **Constitution** as their spellcasting ability for it.

**What happens on the save (same for every pepper):**
- **Success:** they eat the pepper.
- **Failure:** they eat it, then choose one:
  - drink their milk and drop out, or
  - take **1d10 fire damage** and have **disadvantage on their next save** in the challenge.
- **At 0 HP:** they're disqualified, and a medic heals them for 30 HP.

*(From Guadaña, Juegos de Guadaña › Pepper Challenge › Random Spicy Pepper. No page number is given.)*

</details>

### S1. Give me Goren's stat block.

Grade: **correct** · conversation 62 · 2 API calls · in 6 / cache read 45,032 / cache write 1,019 / out 1,232 · $0.0478 · 13.0 s

Lookups:
1. Stat block for “Goren”

<details><summary>Answer</summary>

Here is Goren Rousseau's stat block from the Chateau Rousseau material.

```statblock
Goren Rousseau
AC 14 · HP 114 (12d8 + 60) · Speed 30 ft. · CR 5 (1,800 XP; PB +3)
STR 18 (+4)  DEX 15 (+2)  CON 20 (+5)  INT 9 (-1)  WIS 14 (+2)  CHA 15 (+2)
Medium Aberration, Chaotic Evil
Saves: Str +7, Con +8, Int -1, Wis +2, Dex +2, Cha +2
Skills: Athletics +7, Intimidation +5, Perception +5
Resistances: Bludgeoning, Cold, Piercing, Slashing
Immunities: Frightened, Psychic
Senses: Darkvision 60 ft. (unimpeded by magical Darkness), Passive Perception 15
Languages: Common, Deep Speech, Elvish

Traits:
- Negative Self-Image. When Goren can see his own visage, he shields his eyes, giving himself the Blinded condition. He then spends all his actions either trying to flee or trying to cover or destroy the image, at which point he ends the Blinded condition.
- Spider Climb. Goren can climb difficult surfaces, including upside-down on ceilings, without needing to make an ability check.

Actions:
- Multiattack. Goren makes three attacks: one with his Bite, and two with his Claws.
- Bite. Melee Weapon Attack: +7 to hit, reach 5 ft. Hit: 11 (2d6 + 4) Piercing damage, and the creature must make a DC 13 Constitution saving throw. On a failed save, the creature's Hit Point Maximum is reduced by the amount of damage taken from Goren's bite, Goren regains that number of Hit Points, and the creature has the Frightened condition for 1 minute. The creature can make a DC 13 Wisdom saving throw at the end of each of its turns, ending the Frightened condition on a success.
- Claws. Melee Weapon Attack: +7 to hit, reach 5 ft. Hit: 8 (1d8 + 4) Slashing damage.
- Gravity Well (3/Day). Goren redirects gravity in a 60-foot Cone. Any creature or object within the Cone must make a DC 15 Dexterity saving throw. On a failed save, a target has the Prone condition and is dragged up to 30 feet toward Goren, and if this movement brings a creature within Goren's reach, he can attack one such creature with his Claws as part of this action. On a successful save, a target lands Prone as it drops down and grabs the ground to hold its position.
```

He resists bludgeoning, piercing and slashing damage, so weapons do half damage. Two things work against him: **mirrors or anything reflective** (because of Negative Self-Image) and **psychic-free magic damage** such as fire.

He also has a **save** listed for every ability, but only Str and Con look like real proficiencies. The rest are probably just his plain ability modifiers. Note that his Int save says −1, which matches his score.

</details>

### S2. Stat block for the flower in the conservatory.

Grade: **correct** · conversation 63 · 4 API calls · in 10 / cache read 92,424 / cache write 1,968 / out 997 · $0.0668 · 10.3 s

Lookups:
1. Looked up “conservatory”
2. Searched the books for “Conservatory violet fungus flower”
3. Stat block for “violet fungus”
4. Read a passage of the book

<details><summary>Answer</summary>

The flower in the Conservatory (R8) runs as a **Violet Fungus**, but *Chateau Rousseau* changes it in two ways:

- **45 HP (10d8)** instead of 18
- **Vulnerability to fire damage**

It attacks any creature that moves within **10 feet** of it. It sits on the table directly above the chest, and its vines partly hide the chest. The vines across the tables and floor make the room **difficult terrain**.

```statblock
Violet Fungus
AC 5 · HP 18 (4d8) · Speed 5 ft. · CR 1/4 (50 XP)
STR 3 (-4)  DEX 1 (-5)  CON 10 (+0)  INT 1 (-5)  WIS 3 (-4)  CHA 1 (-5)
Condition Immunities blinded, deafened, frightened
Senses blindsight 30 ft. (blind beyond this radius), passive Perception 6

Traits:
- False Appearance. While motionless, indistinguishable from an ordinary fungus.

Actions:
- Multiattack. Makes 1d4 Rotting Touch attacks.
- Rotting Touch. Melee Weapon Attack: +2 to hit, reach 10 ft., one creature. Hit: 4 (1d8) necrotic damage.
```
*(The card above shows the standard SRD stats. Use the module's 45 HP and fire vulnerability instead.)*

**Treasure:** the chest under it holds **two Potions of Greater Healing** and the **Emerald Signet** (Chateau Rousseau, Ch. 4, Conservatory R8). The party hasn't been here yet.

</details>

### S3. What are an imp's stats?

Grade: **correct** · conversation 64 · 5 API calls · in 12 / cache read 120,641 / cache write 5,592 / out 1,585 · $0.1080 · 15.6 s

Lookups:
1. Stat block for “imp”
2. Searched the books for “Imp tiny fiend devil shapechanger sting invisibility”
3. Read a passage of the book
4. Searched the books for “Imp”

<details><summary>Answer</summary>

The **Imp** is a **CR 1 (200 XP)** devil from the SRD 5.1 Bestiary. That entry is printed tangled up with the ice devil's: the ice devil's attacks and Wall of Ice sit in the middle of the imp's text. I took those out and kept only the imp's own lines:

---

**Imp**
*Tiny fiend (devil, shapechanger), lawful evil*

**Armor Class** 13
**Hit Points** 10 (3d4 + 3)
**Speed** 20 ft., fly 40 ft.

| STR | DEX | CON | INT | WIS | CHA |
|---|---|---|---|---|---|
| 6 (−2) | 17 (+3) | 13 (+1) | 11 (+0) | 12 (+1) | 14 (+2) |

**Skills** Deception +4, Insight +3, Persuasion +4, Stealth +5
**Damage Resistances** cold; bludgeoning, piercing, and slashing from nonmagical attacks that aren't silvered
**Damage Immunities** fire, poison
**Condition Immunities** poisoned
**Senses** darkvision 120 ft., passive Perception 11
**Languages** Infernal, Common
**Challenge** 1 (200 XP)

***Shapechanger.*** As an action, the imp can turn into a beast form or back into its true form. The forms are a rat (speed 20 ft.), a raven (20 ft., fly 60 ft.) or a spider (20 ft., climb 20 ft.). Its statistics are the same in every form except speed. Equipment it carries doesn't change with it. It reverts to its true form if it dies.

***Devil's Sight.*** Magical darkness doesn't block its darkvision.

***Magic Resistance.*** It has advantage on saving throws against spells and other magical effects.

**Actions**

***Sting (Bite in Beast Form).*** *Melee Weapon Attack:* **+5 to hit**, reach 5 ft., one target. *Hit:* 5 (1d4 + 3) piercing damage. The target makes a **DC 11 Constitution save**, taking 10 (3d6) poison damage on a failure or half on a success.

***Invisibility.*** It turns invisible until it attacks or its concentration ends, as if concentrating on a spell. Anything it wears or carries is invisible too.

---

**If this is for Chateau Rousseau:** the imp hides under the bed in the **Spare Bedroom (R6)**. If disturbed, it drinks a **Potion of Speed** it found in the nightstand, attacks the party, then darts out to hide somewhere else. The nightstand drawer holds another Potion of Speed. The adventure gives no changed stats for this imp, so use the block above.

</details>

### S4. Stat block for the Swarm of Trickered Treats.

Grade: **correct** · conversation 65 · 2 API calls · in 6 / cache read 45,040 / cache write 789 / out 953 · $0.0410 · 8.1 s

Lookups:
1. Stat block for “Swarm of Trickered Treats”

<details><summary>Answer</summary>

Here's the Swarm of Trickered Treats. It's a homebrew creature from the Zero-to-Thirty CR Challenge monsters. In Chateau Rousseau, the swarm is in the third-floor Play Room.

```statblock
Swarm of Trickered Treats
AC 12 · HP 28 (4d8 + 10) · Speed 0 ft., fly 10 ft. · CR 1/4 (50 XP)
STR 2 (-4)  DEX 15 (+2)  CON 14 (+2)  INT 1 (-5)  WIS 3 (-4)  CHA 1 (-5)
Medium swarm of Tiny constructs, same alignment as its creator
Initiative +2 (12)
Immunities Blinded, Charmed, Deafened, Exhaustion, Frightened, Grappled, Paralyzed, Petrified, Poison, Prone, Restrained, Stunned
Senses Blindsight 10 ft. (blind beyond this radius), Passive Perception 6

Traits:
- Antimagic Susceptibility. Incapacitated in an antimagic field. If targeted by dispel magic, must succeed on a Constitution Saving Throw against the caster's Spell Save DC or fall Unconscious for 1 minute.
- False Appearance. While motionless and not flying, indistinguishable from a normal pile of candy.
- Swarm. Can occupy another creature's space and vice versa, and can move through any opening large enough for a Tiny creature. Can't regain Hit Points or gain Temporary Hit Points.

Actions:
- Cavities. Melee Weapon Attack: +4 to hit, reach 0 ft., one target in the swarm's space. Hit: 5 (2d4) Bludgeoning damage, or 2 (1d4) Bludgeoning damage if the swarm has half of its Hit Points or fewer. If the target has teeth and was hit, they must succeed on a DC 12 Constitution Saving Throw or take 2 (1d4) Poison damage, with Disadvantage if they previously took Poison damage from a trickered treat in the last day.
```

**Things to remember at the table:**
- **Cavities:** after a hit, any target with teeth makes a **DC 12 Constitution save** or takes 1d4 poison damage.
- **Dispel magic:** the swarm makes a Constitution save against the caster's spell DC or falls unconscious for 1 minute.

</details>

### S5. What's an adult red dragon's AC, HP and CR?

Grade: **correct** · conversation 66 · 2 API calls · in 6 / cache read 45,040 / cache write 959 / out 198 · $0.0268 · 4.4 s

Lookups:
1. Stat block for “adult red dragon”

<details><summary>Answer</summary>

These numbers come from the SRD 5.1 Bestiary stat block for the **Adult Red Dragon**:

- **AC 19** (natural armor)
- **HP 256** (19d12+133)
- **CR 17** (18,000 XP)

Want the full stat block or an encounter card for it?

</details>

### E1. Pull together everything I need to run Ferran's fight.

Grade: **correct** · conversation 67 · 8 API calls · in 18 / cache read 207,929 / cache write 7,142 / out 2,534 · $0.1696 · 28.1 s

Lookups:
1. Read the outline
2. Looked up “Ferran”
3. Searched the books for “Ferran's Reciprocity skeletons Grinning Gourd”
4. Read a passage of the book
5. Put together a fight: 3 × Skeleton, 1 × Grinning Gourd
6. Read a passage of the book
7. Stat block for “Grinning Gourd”
8. Stat block for “bone knight”
9. Listed pcs
10. Searched the books for “Pan de Muerto wondrous item”
11. Read a passage of the book

<details><summary>Answer</summary>

The fight is Ferran's **three skeletons and one Grinning Gourd**. Ferran doesn't fight himself unless the party attacks him. It ends when **all his minions are down or two PCs are unconscious**.

### Setup (Guadaña, "Ferran's Reciprocity")
- **Trigger:** Ferran shows up and demands blood as payment for Dídac's hospitality. He summons three skeletons and a Grinning Gourd to fight for him.
- **Where:** a clearing of a few hundred square yards in a dark forest. There's no sun; hundreds of candles and paper lanterns light it.
- **Ending:** once the minions are beaten or two PCs are unconscious, Ferran's bloodlust is satisfied. He **revives any fallen PCs** and lets the party finish celebrating in peace.
- **Afterward:** Dídac apologizes over and over and gives every guest **pan de muerto**. If they use an action to eat it, they regain **1d6 HP at the end of each of their turns for 1 minute**, and they don't need food or drink for 1d4 days.
- **Dídac** stays out of combat. He defends himself if he has to and uses *dimension door* to escape if he's in danger.

```encounter
{"creatures":[{"name":"Skeleton","id":2157,"count":3},{"name":"Grinning Gourd","id":2275}]}
```

The fight is worth **850 XP** (3 skeletons at 50 XP each, plus 700 XP for the gourd). I can't give a difficulty because there are no characters in this campaign yet. Tell me the party's size and level and I'll work it out. The module is written for 5th level and says to adjust this fight for party size and level, since it's the only combat it intends.

### Running the Grinning Gourd
- **False Appearance:** it looks like an ordinary jack-o'-lantern while it isn't moving, which makes it easy to hide among the decorations for an ambush.
- **Constrict** (reach 20 ft.) does **2d6+3** damage. The target is grappled (**escape DC 13**) and restrained, and takes **4d6 poison** at the start of each of its turns. On a later turn the gourd can use **Reel** as a bonus action to pull the target in, then **Bite** it for 1d6+3 piercing plus 2d4 fire.
- **Explode at 0 HP:** everything within 20 ft. makes a **DC 14 Dex save**, taking **3d10 fire** and falling prone on a fail, or half damage on a success. Warn anyone fighting it in melee.

### Ferran, if they attack him (or you scale up)
- He uses the **bone knight** stat block with a **+4 proficiency bonus**. I don't have that stat block; it's in *Eberron: Rising from the Last War*, so you'll need your own copy.
- **Changes from the module:**
  - Lawful evil, and both fey and undead.
  - **Legendary Resistance (3/Day).**
  - **Fey Rebirth:** if he dies in Guadaña, he comes back with full HP **1d4 days later** where he died.
- **Spellcasting:** 11th-level caster using Charisma, **save DC 15**, **+7 to hit** with spells, no material components.
  - 1st level (4 slots): *cause fear, command, compelled duel, hellish rebuke, wrathful smite*
  - 2nd level (3 slots): *branding smite, crown of madness, darkness*
  - 3rd level (3 slots): *animate dead, bestow curse, revivify*
- **How he fights:** he lets his minions do the work but "will strike down hard against any who attack him directly." For a high-level party he can join the fight, and then he **doesn't yield until he or the whole party is defeated**.

### Suggestions (mine, not the module's)
- *compelled duel* and *hellish rebuke* fit a Ferran who only fights back when someone goes after him.
- If the fight is too easy, add skeletons or have him cast *animate dead* during the fight.
- Remind the players of any Guadaña prizes they've won. For example, *calabaza en tacha* gives +3 AC and Con saves, and a *calavera de azúcar* summons a sugar skeleton ally.

Nothing was recorded. Tell me how the fight goes and I'll log it.

</details>

### E2. The party picked a fight with Neta; there are four of them. What am I running?

Grade: **correct** · conversation 68 · 4 API calls · in 10 / cache read 95,576 / cache write 6,203 / out 1,515 · $0.0996 · 16.2 s

Lookups:
1. Looked up “Neta”
2. Listed pcs
3. Searched the books for “Neta library Magma Mephits fireplace fight”
4. Read a passage of the book
5. Put together a fight: 1 × Neta Rousseau, 4 × Magma Mephit

<details><summary>Answer</summary>

You're running **Neta plus four Magma Mephits**, one mephit per party member. When the party attacks or accepts her challenge, the fireplace surges and the mephits leap out. Neta lets them do the fighting and **only fights back herself if someone attacks her** (*Chateau Rousseau*, Appendix B: Neta Rousseau).

```encounter
{"creatures":[{"name":"Neta Rousseau","id":2335,"note":"Lets the mephits fight; only defends herself if attacked"},{"name":"Magma Mephit","id":2123,"count":4}]}
```

**XP:** 850 in total (Neta 450, each mephit 100). I can't give you a difficulty because the campaign has no character sheets and you haven't told me the party's level. The module is written for 5th level, but tell me their actual level and I'll work it out.

### Neta at a glance
- **AC 12, HP 33**. She can only walk 5 ft. but **flies 40 ft. (hover)**, and she can move through creatures and objects.
- **Resists** acid, cold, fire, lightning and thunder, plus **non-silvered** bludgeoning, piercing and slashing. Silvered weapons and radiant, force or psychic damage get through.
- **Paralyzing Touch:** +4 to hit, 1d6 cold damage. On a hit the target makes a **DC 13 Constitution save** or is **paralyzed** for 1 minute, repeating the save at the end of each of its turns.
- **Reading Material (Recharge 6):** a 10-ft. storm of flying books around her that lasts 1 minute. Anyone who starts a turn in it or enters it makes a **DC 13 Dexterity save** or takes 2d6 bludgeoning damage and is knocked prone.
- **Spells (DC 13):** *chill touch* at will. *Dissonant whispers*, *hideous laughter* and *sleep* 3/day each. *Blindness/deafness* and *ray of enfeeblement* 1/day each.

### Worth remembering
- The book calls this a **friendly** challenge for her information, which is how the Signet Rings work, where two of them are, and the dumbwaiter route into Lynistad's Room. If she's beaten, it makes sense for her to pay up.
- If **Goren** comes into the Library, Neta turns invisible until he leaves.

The log already shows this fight starting: the journal says the four-member party took on Neta in the Library (R4) and the four mephits came out. Tell me how it ends and I'll record the result.

</details>

### E3. Stat blocks for everything in the Conservatory and the Closet.

Grade: **correct** · conversation 69 · 4 API calls · in 10 / cache read 93,662 / cache write 4,018 / out 1,405 · $0.0857 · 18.6 s

Lookups:
1. Looked up “Conservatory”
2. Looked up “Closet”
3. Searched the books for “Conservatory Violet Fungus”
4. Searched the books for “Closet Animated Boots”
5. Read a passage of the book
6. Read a passage of the book
7. Put together a fight: 1 × Violet Fungus
8. Put together a fight: 1 × Animated Pair of Boots

<details><summary>Answer</summary>

Each room has one creature. Both cards are below with the full stat blocks.

### Conservatory (R8): Violet Fungus
The large purple flower on the table above the chest **attacks any creature that moves within 10 feet of it**. The book changes the standard stat block in two ways: **45 HP (10d8)** and **Vulnerability to Fire damage**. The vines make the room **difficult terrain**.

- **AC 5, CR 1/4 (50 XP)**
- **Treasure in the chest:** two *Potions of Greater Healing* and the *Emerald Signet*

```encounter
{"creatures":[{"name":"Violet Fungus","id":2071,"hp":"45 (10d8)","note":"Chateau Rousseau R8: Vulnerability to Fire damage; attacks any creature that moves within 10 ft. Vines make the room difficult terrain."}]}
```

### Closet (R11): Animated Pair of Boots
The boots hide among the moth-eaten coats and **attack if the players search the room**. **If they're killed, they become a pair of *Winged Boots***. The stat block comes from the homebrew monster collection (030crc), not the SRD.

- **AC 14, HP 22 (5d8), CR 1/2 (100 XP)**

```encounter
{"creatures":[{"name":"Animated Pair of Boots","id":2287,"note":"Chateau Rousseau R11: hides in the closet, attacks if players search the room. When killed, becomes Winged Boots."}]}
```

The source text for both rooms is in Chateau Rousseau, Chapter 4 (House Layout). It has no page numbers. Neither room has been visited in play, so both are still as the book describes them.

I can't rate the difficulty of these fights because there's no party saved for this campaign. The adventure is written for level 5. Tell me how many characters you have and their level, and I'll work it out.

</details>

### N1. What's the bone knight stat block?

Grade: **correct** · conversation 70 · 5 API calls · in 12 / cache read 116,766 / cache write 3,376 / out 1,091 · $0.0855 · 14.7 s

Lookups:
1. Stat block for “bone knight”
2. Searched the books for “bone knight”
3. Read a passage of the book
4. Read a passage of the book

<details><summary>Answer</summary>

I can't give you the bone knight stat block because it isn't in any of the loaded sources. Guadaña cites it from **Eberron: Rising from the Last War**, and that book isn't loaded. I won't make up its numbers.

### What Guadaña says about it
Dídac and Ferran both use the **bone knight** stat block with these changes:

- **Proficiency bonus +4**
- **Creature Type:** fey and undead.
- **Fey Rebirth:** if he dies in Guadaña, he comes back with all his hit points **1d4 days** later in the spot where he died.
- **Legendary Resistance (3/Day):** if he fails a saving throw, he can choose to succeed instead.

**Dídac** is lawful neutral, with **Str 10, Dex 14, Cha 20**. He fights with a **rapier** instead of a greatsword and longbow. He casts as an 11th-level spellcaster using Charisma (**spell save DC 17, +9 to hit** with spells). He knows the spells from the *warlock of the Archfey* stat block and doesn't need material components. He stays out of fights and uses ***dimension door*** to get away if he's in danger.

**Ferran** is lawful evil. He casts as an 11th-level spellcaster using Charisma (**spell save DC 15, +7 to hit** with spells) and doesn't need material components. His prepared paladin spells:
- 1st level (4 slots): *cause fear, command, compelled duel, hellish rebuke, wrathful smite*
- 2nd level (3 slots): *branding smite, crown of madness, darkness*
- 3rd level (3 slots): *animate dead, bestow curse, revivify*

### For the fight
The only combat the module plans is **Ferran's Reciprocity**: Ferran summons **3 skeletons and 1 Grinning Gourd** and stays out of it himself. He only fights alongside them if the party is high enough level. Both of those stat blocks are in the loaded material, so I can build that encounter for you.

If you need Ferran or Dídac fighting and don't have Eberron: Rising from the Last War, **my suggestion** is to use the SRD **knight** (CR 3) with the changes above. It's a swap, not what the book says. Want me to pull that up?

</details>

### N2. What's Lynistad Rousseau's stat block?

Grade: **wrong** · conversation 71 · 2 API calls · in 6 / cache read 45,038 / cache write 1,342 / out 658 · $0.0379 · 7.7 s

Lookups:
1. Stat block for “Lynistad Rousseau”
2. Looked up “Lynistad Rousseau”

<details><summary>Answer</summary>

**Chateau Rousseau doesn't give Lynistad Rousseau a stat block.** The adventure says she **never appears in the adventure**. She's the absent Rousseau matriarch, and her role is all backstory:

- She's a mage who studied planar travel. Her experiments opened a gateway to the Far Realm, which corrupted Goren.
- She moved the whole house into a pocket dimension to contain him.
- She still captures people from various worlds and sends them to the house as food for Goren.
- She stays away out of shame and searches the planes for a cure.
- She made the three Signet Rings, and only she knows how to open the front door.

None of this has come up in play yet, and the party knows none of it.

**My suggestion, if you want to bring her in:** The SRD Bestiary has generic **Mage** (CR 6) and **Archmage** (CR 12) stat blocks in Appendix MM-B. Either could stand in for her. The Mage is closer to a level-5 party's range. The Archmage fits a mage who can shunt a mansion into a demiplane, but it's far too strong for them to fight. I'd use the Archmage only for a scene where she talks rather than fights. I can pull either stat block or set up an encounter with it if you want.

</details>
