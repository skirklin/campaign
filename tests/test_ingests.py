"""Session ingests as targets: worked through by a loop that can be interrupted, retried and cancelled."""

import time

import pytest

from campaign import agent as agent_mod
from campaign import ingests
from campaign.db import Store
from campaign.ingests import IngestLoop, Log
from campaign.world import World
from test_agent import FakeClient, text, tool_use

SCRIPT = [
    ([tool_use("t1", "record_fact", entity="Ismark", kind="npc", statement="Ismark is alive",
               attribute="status", value="alive")], "tool_use"),
    ([tool_use("t2", "add_journal_entry", text="Met Ismark.", entities=["Ismark"])], "tool_use"),
    ([tool_use("t3", "finish_session", title="Blood on the Vine", summary="They met Ismark.")], "tool_use"),
    ([text("All recorded.")], "end_turn"),
]


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "c.db"
    s = Store(db)
    camp = s.create_campaign("Strahd", "dm")
    ingest_id = s.add_ingest(camp, 1, s.blobs.put(b"DM: A man in a cloak..."), "s1.txt", None, None)
    yield s, camp, db, ingest_id
    s.close()


def scripted(monkeypatch, *scripts):
    """Each new Agent gets the next script."""
    clients = [FakeClient(sc) for sc in scripts]
    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: clients.pop(0))


def facts(s, camp):
    return s.conn.execute("SELECT count(*) FROM facts WHERE campaign_id = ?", (camp.id,)).fetchone()[0]


def test_an_interrupted_ingest_is_picked_up_and_redone_cleanly(env, monkeypatch):
    s, camp, db, ingest_id = env
    loop = IngestLoop(db)
    # The first attempt is cut off after its first write (say, the server is being replaced).
    calls = []
    real_check = loop._check

    def check(i):
        calls.append(i)
        if len(calls) == 2:
            raise ingests.IngestStopped("handed over")
        real_check(i)

    loop._check = check
    scripted(monkeypatch, SCRIPT, SCRIPT)
    assert loop.pass_once()
    ing = s.ingest(ingest_id)
    assert ing["state"] == "wanted" and ing["attempts"] == 0  # not its fault: it'll be redone
    assert facts(s, camp) == 1  # the first attempt got one fact in

    # The next server (or the next pass) starts over from the transcript.
    loop._check = real_check
    assert loop.pass_once()
    ing = s.ingest(ingest_id)
    assert ing["state"] == "done" and ing["result"]["title"] == "Blood on the Vine"
    assert facts(s, camp) == 1 and len(World(s, camp).events()) == 1  # redone, not doubled
    assert loop.pass_once() is False  # nothing left


def test_failures_back_off_then_wait_for_a_retry(env, monkeypatch):
    s, camp, db, ingest_id = env
    loop = IngestLoop(db)
    clock = [time.time()]
    monkeypatch.setattr(ingests.time, "time", lambda: clock[0])
    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: (_ for _ in ()).throw(RuntimeError("API down")))
    for attempt in range(1, ingests.MAX_ATTEMPTS + 1):
        assert loop.pass_once()
        assert loop.pass_once() is False  # backing off: nothing due yet
        clock[0] += 3600
    ing = s.ingest(ingest_id)
    assert ing["state"] == "failed" and ing["error"].startswith("API down (gave up after 3 tries)")
    assert loop.pass_once() is False  # given up: it waits to be asked again
    s.update_ingest(ingest_id, state="wanted", attempts=0, retry_at=None)  # what Retry does
    scripted(monkeypatch, SCRIPT)
    assert loop.pass_once() and s.ingest(ingest_id)["state"] == "done"


def test_cancelling_midway_undoes_what_it_wrote(env, monkeypatch):
    s, camp, db, ingest_id = env
    loop = IngestLoop(db)
    real_check = loop._check
    calls = []

    def check(i):
        calls.append(i)
        if len(calls) == 2:
            s.update_ingest(i, state="cancelled")  # someone clicked Cancel
        real_check(i)

    loop._check = check
    scripted(monkeypatch, SCRIPT)
    loop.pass_once()
    assert s.ingest(ingest_id)["state"] == "cancelled"
    assert facts(s, camp) == 0  # the fact the first step wrote is gone
    assert any(e["text"] == "Cancelled." for e in s.ingest_log(ingest_id))
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
    assert [(e["kind"], e["text"]) for e in s.ingest_log(ingest_id)] == [
        ("status", "Reading…"), ("text", "The party met Ismark."), ("tool", "record_fact Ismark"), ("text", "Next."),
    ]
