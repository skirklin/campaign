"""Jobs (session ingests, sheet scans): worked through by a loop that can be interrupted, retried and cancelled."""

import json
import time
from dataclasses import replace

import httpx2 as httpx
import pytest

from campaign import agent as agent_mod
from campaign import jobs, sheetscan
from campaign.accounts import Accounts
from campaign.charsheet import ArmorClass, Bonus, ClassLevel
from campaign.charsheet import CharacterSheet
from campaign.db import Store
from campaign.jobs import JobLoop, Log
from campaign.world import World
from types import SimpleNamespace as NS

from test_agent import FakeClient, text, tool_use

SCRIPT = [
    ([tool_use("t1", "record_fact", entity="Ismark", kind="npc", statement="Ismark is alive",
               attribute="status", value="alive")], "tool_use"),
    ([tool_use("t2", "add_journal_entry", text="Met Ismark.", entities=["Ismark"])], "tool_use"),
    ([tool_use("t3", "finish_session", title="Blood on the Vine", summary="They met Ismark.")], "tool_use"),
    ([text("All recorded.")], "end_turn"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "CHECK_SECONDS", 0)  # check at every step, so tests can stop one exactly
    db = tmp_path / "c.db"
    s = Store(db)
    camp = s.create_campaign("Strahd", "dm")
    ingest_id = s.add_job(camp, "ingest", {
        "session": 1, "blob": s.blobs.put(b"DM: A man in a cloak..."), "name": "s1.txt", "played_on": None,
    }, None)
    yield s, camp, db, ingest_id
    s.close()


def interrupt(loop, at, action):
    """Make the job's `at`-th check do `action(job_id)` first (stop it, cancel it)."""
    real = loop._checker
    calls = []

    def checker(job_id):
        check = real(job_id)

        def wrapped():
            calls.append(job_id)
            if len(calls) == at:
                action(job_id)
            check()

        return wrapped

    loop._checker = checker
    return lambda: setattr(loop, "_checker", real)


def scripted(monkeypatch, *scripts):
    """Each new Agent gets the next script."""
    clients = [FakeClient(sc) for sc in scripts]
    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: clients.pop(0))


def facts(s, camp):
    return s.conn.execute("SELECT count(*) FROM facts WHERE campaign_id = ?", (camp.id,)).fetchone()[0]


def test_an_interrupted_ingest_is_picked_up_and_redone_cleanly(env, monkeypatch):
    s, camp, db, ingest_id = env
    loop = JobLoop(db)
    # The first attempt is cut off after its first write (say, the server is being replaced).

    def handed_over(_):
        raise jobs.JobStopped("handed over")

    restore = interrupt(loop, 2, handed_over)
    scripted(monkeypatch, SCRIPT, SCRIPT)
    assert loop.pass_once()
    ing = s.job(ingest_id)
    assert ing["state"] == "wanted" and ing["attempts"] == 0  # not its fault: it'll be redone
    assert facts(s, camp) == 1  # the first attempt got one fact in

    # The next server (or the next pass) starts over from the transcript.
    restore()
    assert loop.pass_once()
    ing = s.job(ingest_id)
    assert ing["state"] == "done" and ing["result"]["title"] == "Blood on the Vine"
    assert facts(s, camp) == 1 and len(World(s, camp).events()) == 1  # redone, not doubled
    assert loop.pass_once() is False  # nothing left


def test_failures_back_off_then_wait_for_a_retry(env, monkeypatch):
    s, camp, db, ingest_id = env
    loop = JobLoop(db)
    clock = [time.time()]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: (_ for _ in ()).throw(RuntimeError("API down")))
    for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
        assert loop.pass_once()
        assert loop.pass_once() is False  # backing off: nothing due yet
        clock[0] += 3600
    ing = s.job(ingest_id)
    assert ing["state"] == "failed" and ing["error"].startswith("API down (gave up after 3 tries)")
    assert loop.pass_once() is False  # given up: it waits to be asked again
    s.update_job(ingest_id, state="wanted", attempts=0, retry_at=None)  # what Retry does
    scripted(monkeypatch, SCRIPT)
    assert loop.pass_once() and s.job(ingest_id)["state"] == "done"


def test_cancelling_midway_undoes_what_it_wrote(env, monkeypatch):
    s, camp, db, ingest_id = env
    loop = JobLoop(db)
    interrupt(loop, 2, lambda i: s.update_job(i, state="cancelled"))  # someone clicked Cancel
    scripted(monkeypatch, SCRIPT)
    loop.pass_once()
    assert s.job(ingest_id)["state"] == "cancelled"
    assert facts(s, camp) == 0  # the fact the first step wrote is gone
    assert any(e["text"] == "Cancelled." for e in s.job_log(ingest_id))
    assert loop.pass_once() is False


def test_log_merges_streamed_text_into_lines(env):
    s, _, db, ingest_id = env
    log = Log(db, ingest_id)
    log.emit("status", "Reading…")
    for word in ["The ", "party ", "met "]:
        log.emit("text", word)
    log.flush()
    log.emit("text", "Ismark.")  # continues the same line after a flush
    log.emit("tool", "record_fact Ismark")
    log.emit("text", "Next.")
    log.flush()
    assert [(e["kind"], e["text"]) for e in s.job_log(ingest_id)] == [
        ("status", "Reading…"), ("text", "The party met Ismark."), ("tool", "record_fact Ismark"), ("text", "Next."),
    ]


# -- sheet scans -----------------------------------------------------------------------

SCAN = sheetscan.ScannedSheet(
    name="Ireth", nicknames=[], statuses=[], player="Sam", species="Elf", background="Sage", alignment="NG",
    classes=[ClassLevel(name="Wizard", level=3)], xp=900,
    abilities=sheetscan.Abilities(strength=8, dexterity=14, constitution=12, intelligence=17, wisdom=12, charisma=10),
    save_proficiencies=["Intelligence", "wis"], skill_proficiencies=["Arcana", "History", "Basket weaving"],
    skill_expertise=[], ac=ArmorClass(base=10, abilities=["dex"]), printed_ac=12, speed=30,
    hp=sheetscan.ScannedHitPoints(max=17, current=17, temp=0), attacks=[], bonuses=[], resources=[],
    spellcasting_ability="Intelligence", spells=[], inventory=[], currency=sheetscan.Coins(gp=12),
    features=["Arcane Recovery"], proficiencies=["Daggers"], personality="", notes="Has a cat.",
    unreadable=["Wisdom score"],
)


def scan_client(monkeypatch, *script, events=("Looking at the sheet. ", "Wizard 3.")):
    """A fake Claude for sheet scans: each request streams some thinking, then gets the next of `script`
    (a reply string, or (content, stop_reason)); the last one repeats. By default it answers with SCAN
    as JSON, wrapped in a few words. Returns the list of requests made."""
    script = [r if isinstance(r, tuple) else ([text(r)], "end_turn") for r in script] or [
        ([text(f"Here it is:\n```json\n{SCAN.model_dump_json()}\n```")], "end_turn")]
    calls = []

    class Stream:
        def __init__(self, content, stop):
            self.message = NS(content=content, stop_reason=stop)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for t in events:
                yield NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking=t))

        def get_final_message(self):
            return self.message

    def stream(**kw):
        calls.append({**kw, "messages": list(kw["messages"])})
        return Stream(*script[min(len(calls), len(script)) - 1])

    client = NS(beta=NS(messages=NS(stream=stream)))
    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: client)
    return calls


@pytest.fixture
def scan_job(env):
    s, camp, db, ingest_id = env
    s.update_job(ingest_id, state="done")  # out of the way
    owner = Accounts(s).create_user("sam@example.com")["id"]
    job_id = s.add_job(camp, "scan", {"blob": s.blobs.put(b"%PDF-1.7 fake"), "name": "ireth.pdf"}, owner)
    return s, camp, db, job_id, owner


def test_a_scan_becomes_a_character(scan_job, monkeypatch):
    s, camp, db, job_id, owner = scan_job
    calls = scan_client(monkeypatch)
    assert JobLoop(db).pass_once()
    job = s.job(job_id)
    assert job["state"] == "done" and job["result"]["name"] == "Ireth"
    assert job["result"]["unreadable"] == ["Wisdom score", "skill: Basket weaving"]
    ch = s.character(job["result"]["character"])
    sheet = ch["sheet"]
    assert ch["owner_id"] == owner and ch["campaign_id"] == camp.id
    assert sheet["abilities"]["int"] == 17 and sheet["save_proficiencies"] == ["int", "wis"]
    assert sheet["skill_proficiencies"] == ["arcana", "history"] and sheet["currency"]["gp"] == 12
    assert sheet["notes"].startswith("Has a cat.") and "Wisdom score; skill: Basket weaving" in sheet["notes"]
    assert calls[0]["messages"][0]["content"][0]["type"] == "document"
    assert calls[0]["thinking"]["display"] == "summarized"  # else the thinking shown as progress is empty
    assert "output_format" not in calls[0] and '"unreadable"' in calls[0]["system"]  # plain JSON, schema in the prompt
    assert 'campaign "Strahd"' in calls[0]["system"]
    tools = {t["name"] for t in calls[0]["tools"]}
    assert {"search_reference", "read_reference", "lookup"} <= tools and "record_fact" not in tools  # read-only
    assert [(e["kind"], e["text"]) for e in s.job_log(job_id)] == [
        ("status", "Claude is reading ireth.pdf…"), ("text", "Looking at the sheet. Wizard 3."), ("status", "Made Ireth."),
    ]


def test_an_interrupted_scan_is_redone_without_doubling_anything(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    calls = scan_client(monkeypatch)
    loop = JobLoop(db)

    # Cut off while Claude is still reading: nothing is made, and it'll be read again.
    def handed_over(_):
        raise jobs.JobStopped("handed over")

    restore = interrupt(loop, 1, handed_over)
    assert loop.pass_once()
    assert s.job(job_id)["state"] == "wanted" and s.characters([camp.id]) == []
    restore()

    # This time the read finishes, but the server dies before recording that the job is done.
    real = Store.update_job

    def die_when_done(self, job_id, **fields):
        if fields.get("state") == "done":
            raise KeyboardInterrupt("killed")
        real(self, job_id, **fields)

    monkeypatch.setattr(Store, "update_job", die_when_done)
    with pytest.raises(KeyboardInterrupt):
        loop.pass_once()
    monkeypatch.setattr(Store, "update_job", real)
    assert s.job(job_id)["state"] == "running" and [c["name"] for c in s.characters([camp.id])] == ["Ireth"]
    assert len(calls) == 2 and s.job(job_id)["input"]["sheet"]["name"] == "Ireth"  # the read is kept

    # The next server finds it running, so redoes it: without reading the sheet again or making a second Ireth.
    assert loop.pass_once()
    job = s.job(job_id)
    assert job["state"] == "done" and len(calls) == 2
    assert [c["name"] for c in s.characters([camp.id])] == ["Ireth"] == [job["result"]["name"]]


def test_an_answer_that_does_not_fit_gets_one_chance_to_be_corrected(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    bad = SCAN.model_dump()
    del bad["abilities"]
    calls = scan_client(monkeypatch, json.dumps(bad), SCAN.model_dump_json())
    loop = JobLoop(db)
    assert loop.pass_once() and s.job(job_id)["state"] == "done"
    assert len(calls) == 2
    second = calls[1]["messages"]
    assert second[1]["role"] == "assistant" and "abilities" in second[2]["content"]

    # Wrong twice: that's not a sheet Claude can read, and asking again won't change it.
    job_id = s.add_job(camp, "scan", {"blob": s.blobs.put(b"%PDF-1.7"), "name": "doodle.pdf"}, None)
    scan_client(monkeypatch, "I see a drawing of a dragon.")
    assert loop.pass_once()
    job = s.job(job_id)
    assert job["state"] == "failed" and job["attempts"] == 1 and "couldn't read a character sheet" in job["error"]


def test_a_request_the_api_refuses_fails_at_once(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

    def stream(**kw):
        raise agent_mod.anthropic.BadRequestError("The compiled grammar is too large", response=response, body=None)

    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: NS(beta=NS(messages=NS(stream=stream))))
    assert JobLoop(db).pass_once()
    job = s.job(job_id)
    assert job["state"] == "failed" and job["attempts"] == 1 and job["retry_at"] is None
    assert "compiled grammar is too large" in job["error"]


def test_scan_failures_that_retrying_cannot_fix_fail_at_once(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    s.create_character({"name": "Ireth"}, camp.id)  # someone already made one by hand
    scan_client(monkeypatch)
    assert JobLoop(db).pass_once()
    job = s.job(job_id)
    assert job["state"] == "failed" and "already has a character called Ireth" in job["error"]
    assert job["attempts"] == 1 and job["retry_at"] is None


def test_cancelling_a_scan_makes_nothing(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    scan_client(monkeypatch)
    loop = JobLoop(db)
    interrupt(loop, 2, lambda i: s.update_job(i, state="cancelled"))
    loop.pass_once()
    assert s.job(job_id)["state"] == "cancelled" and s.characters([camp.id]) == []


def test_ingests_move_over_to_jobs_keeping_their_ids(tmp_path):
    db = tmp_path / "old.db"
    s = Store(db)
    camp = s.create_campaign("Strahd", "dm")
    # The database as the v10 server left it: an ingest in the ingests table, no jobs.
    s.conn.executescript("DROP TABLE character_files; DROP TABLE job_log; DROP TABLE jobs;"
                         " DROP TABLE party_stash_log; DROP TABLE party_stash; PRAGMA user_version = 10;")
    s.conn.execute(
        "INSERT INTO ingests (id, campaign_id, session, input_blob, input_name, transcript_blob, played_on, role,"
        " state, attempts, error, created_at, updated_at)"
        " VALUES (7, ?, 3, 'b1', 's3.m4a', 'b2', '2026-10-25', 'dm', 'failed', 3, 'boom', 'then', 'then')", (camp.id,))
    s.conn.execute("INSERT INTO ingest_log (ingest_id, seq, kind, text) VALUES (7, 0, 'status', 'Transcribing…')")
    s.conn.commit()
    s.close()
    s = Store(db)  # migrates
    job = s.job(7)
    assert job["kind"] == "ingest" and job["state"] == "failed" and job["error"] == "boom" and job["attempts"] == 3
    assert job["input"] == {"session": 3, "blob": "b1", "name": "s3.m4a", "transcript_blob": "b2",
                            "played_on": "2026-10-25"}
    assert s.job_log(7) == [{"seq": 0, "kind": "status", "text": "Transcribing…"}]
    s.close()


def test_a_scan_looks_up_what_it_cannot_place_in_the_campaign_books(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    from campaign.library import add_reference

    add_reference(s, camp, b"# Barbarian Paths\n## Path of the Herculean\nYou draw on the strength of a hero.\n",
                  "paths.md", kind="notes")
    herculean = SCAN.model_copy(update={"classes": [ClassLevel(name="Barbarian", level=3, subclass="Path of the Herculean")]})
    calls = scan_client(
        monkeypatch,
        ([tool_use("t1", "search_reference", query="barbarian path HP")], "tool_use"),
        herculean.model_dump_json(),
    )
    assert JobLoop(db).pass_once()
    job = s.job(job_id)
    assert job["state"] == "done"
    assert s.character(job["result"]["character"])["sheet"]["classes"][0]["subclass"] == "Path of the Herculean"
    # The lookup ran against the campaign's books, and the job's log shows it.
    result = calls[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "Herculean" in result["content"]
    assert any(e["kind"] == "tool" and "Searched the books" in e["text"] for e in s.job_log(job_id))


def test_a_players_scan_cannot_see_dm_only_material(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    from campaign.library import add_reference

    add_reference(s, camp, b"# Secrets\nThe Herculean Path was founded by the villain.\n", "secrets.md",
                  kind="notes", dm_only=True)
    s.update_job(job_id, state="done")
    player = replace(camp, role="player")
    job_id = s.add_job(player, "scan", {"blob": s.blobs.put(b"%PDF-1.7"), "name": "cid.pdf"}, None)
    calls = scan_client(
        monkeypatch,
        ([tool_use("t1", "search_reference", query="Herculean")], "tool_use"),
        SCAN.model_dump_json(),
    )
    assert JobLoop(db).pass_once() and s.job(job_id)["state"] == "done"
    assert "villain" not in calls[1]["messages"][-1]["content"][0]["content"]


def test_a_scan_records_parts_and_flags_totals_that_do_not_add_up(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    axe = sheetscan.ScannedAttack(
        name="Greataxe", ability="str", proficient=True, printed_to_hit="+6", printed_damage="1d12 + 3 slashing",
        damage=[{"dice": "1d12", "type": "slashing", "add_ability": True}],
    )
    bow = sheetscan.ScannedAttack(  # printed +9: more than its parts (DEX +2, proficiency +3)
        name="Longbow", ability="dex", printed_to_hit="+9", printed_damage="1d8+2 piercing",
        damage=[{"dice": "1d8", "type": "piercing", "add_ability": True}],
    )
    barbarian = SCAN.model_copy(update={
        "abilities": sheetscan.Abilities(strength=16, dexterity=14, constitution=14, intelligence=8, wisdom=10, charisma=10),
        "classes": [ClassLevel(name="Barbarian", level=5)],
        "ac": ArmorClass(label="Unarmored defense", base=10, abilities=["dex", "con"]), "printed_ac": 15,
        "hp": sheetscan.ScannedHitPoints(max=55, current=55, temp=0), "attacks": [axe, bow],
        "bonuses": [Bonus(target="damage:str", amount=2, source="Rage", active=False)],
        "spellcasting_ability": "",
    })
    scan_client(monkeypatch, barbarian.model_dump_json())
    assert JobLoop(db).pass_once()
    job = s.job(job_id)
    sheet = CharacterSheet.model_validate(s.character(job["result"]["character"])["sheet"])
    d = sheet.derived()
    assert sheet.hp.base == 45 and d["hp_max"]["total"] == 55  # 55 printed = 45 from hit dice + CON 2 x 5
    assert d["attacks"][0]["to_hit"]["total"] == 6 and d["attacks"][0]["text"] == "1d12+3 slashing"
    assert job["result"]["unreadable"] == [
        "Wisdom score", "skill: Basket weaving",
        "AC: the sheet says 15, its parts add up to 14",
        "Longbow: the sheet says +9 to hit, its parts add up to +5",
    ]
    # Rage is recorded but off until switched on; then Strength attacks gain it.
    sheet.bonuses[0].active = True
    assert sheet.derived()["attacks"][0]["text"] == "1d12+5 slashing"


def test_explanations_and_prose_in_damage_get_sent_back_once(scan_job, monkeypatch):
    s, camp, db, job_id, _ = scan_job
    wordy = SCAN.model_copy(update={
        "attacks": [sheetscan.ScannedAttack(
            name="Greataxe", printed_to_hit="", printed_damage="",
            damage=[{"dice": "1d12", "type": "slashing, or 1d10 when used two-handed", "add_ability": True}])],
        "unreadable": ["The Wisdom score was hard to make out because the scan is blurry in that corner, "
                       "so I used 10 as instructed"],
    })
    tidy = SCAN.model_copy(update={
        "attacks": [sheetscan.ScannedAttack(
            name="Greataxe", printed_to_hit="", printed_damage="", properties=["heavy", "two-handed"],
            damage=[{"dice": "1d12", "type": "slashing", "add_ability": True}])],
        "unreadable": ["Wisdom score blurry"],
    })
    calls = scan_client(monkeypatch, wordy.model_dump_json(), tidy.model_dump_json())
    loop = JobLoop(db)
    assert loop.pass_once()
    job = s.job(job_id)
    assert job["state"] == "done" and len(calls) == 2
    feedback = calls[1]["messages"][-1]["content"]
    assert "damage type" in feedback and "unreadable entry" in feedback
    sheet = s.character(job["result"]["character"])["sheet"]
    assert sheet["attacks"][0]["damage"][0]["type"] == "slashing" and sheet["attacks"][0]["properties"] == ["heavy", "two-handed"]

    # Still untidy after its one chance: taken as it is rather than failing the scan.
    job_id = s.add_job(camp, "scan", {"blob": s.blobs.put(b"%PDF-1.7"), "name": "again.pdf"}, None)
    s.conn.execute("DELETE FROM characters")
    s.conn.commit()
    scan_client(monkeypatch, wordy.model_dump_json())
    assert loop.pass_once() and s.job(job_id)["state"] == "done"


def test_scans_read_before_files_were_kept_get_attached(tmp_path):
    db = tmp_path / "old.db"
    s = Store(db)
    camp = s.create_campaign("Odyssey", "dm")
    character_id = s.create_character({"name": "Cid"}, camp.id)
    blob = s.blobs.put(b"%PDF-1.7 cid")
    job_id = s.add_job(camp, "scan", {"blob": blob, "name": "cid character sheet.pdf"}, None)
    s.update_job(job_id, state="done", result={"character": character_id, "name": "Cid", "unreadable": []})
    failed = s.add_job(camp, "scan", {"blob": blob, "name": "other.pdf"}, None)
    s.update_job(failed, state="failed")
    # The database as the v12 server left it: no files table.
    s.conn.executescript("DROP TABLE character_files; PRAGMA user_version = 12;")
    s.close()
    s = Store(db)  # migrates
    assert [(f["name"], f["blob"]) for f in s.character_files(character_id)] == [("cid character sheet.pdf", blob)]
    s.close()


def test_a_scan_stored_with_retired_fields_still_reads():
    stored = SCAN.model_dump() | {"attacks_per_action": 2, "grapple_bonus_action": True}
    stored["attacks"] = [a | {"uses": "action"} for a in stored["attacks"]]
    assert sheetscan.ScannedSheet.model_validate(stored).model_dump() == SCAN.model_dump()
