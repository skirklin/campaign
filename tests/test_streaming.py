"""Live updates and streaming chat over Server-Sent Events."""

import json
import threading

import pytest
from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock
from fastapi.testclient import TestClient

from campaign import agent as agent_mod
from campaign.db import Store
from campaign.web import create_app
from test_agent import FakeClient


def events(response):
    """Parse a text/event-stream body into (event, data) pairs."""
    out, event = [], None
    for line in response.iter_lines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            out.append((event, json.loads(line[6:])))
            event = None
    return out


@pytest.fixture
def app(tmp_path):
    db = tmp_path / "s.db"
    with TestClient(create_app(db)) as client:
        client.post("/api/campaigns", json={"name": "Strahd", "role": "dm"})
        yield client, db


def test_events_stream_sources_and_ingest_progress(app):
    client, db = app
    client.post("/api/campaigns/strahd/sources", files={"file": ("notes.md", b"# Vallaki\nUrwin runs the inn.\n")},
                data={"kind": "notes"})
    s = Store(db)
    camp = s.campaign("strahd")
    ingest_id = s.add_ingest(camp, 1, s.blobs.put(b"x"), "s1.txt", None, None)
    s.update_ingest(ingest_id, state="failed", error="boom")  # so the loop leaves it alone
    s.add_ingest_log(ingest_id, [("status", "Reading…"), ("text", "The party")])
    s.close()
    with client.stream("GET", "/api/campaigns/strahd/events?seconds=0") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        got = events(r)
    kinds = [e for e, _ in got]
    assert kinds[0] == "sources" and got[0][1][0]["title"] == "notes"
    ingest = next(d for e, d in got if e == "ingest")
    assert ingest["state"] == "failed" and [x["text"] for x in ingest["log"]] == ["Reading…", "The party"]


def test_events_send_only_what_changed(tmp_path, monkeypatch):
    """Within one stream: unchanged state isn't resent; a growing line is."""
    db = tmp_path / "s.db"
    stop = threading.Event()  # ends the stream after the third snapshot, not on a wall-clock cap
    client = TestClient(create_app(db, shutting_down=stop)).__enter__()
    client.post("/api/campaigns", json={"name": "Strahd", "role": "dm"})
    s = Store(db)
    camp = s.campaign("strahd")
    ingest_id = s.add_ingest(camp, 1, s.blobs.put(b"x"), "s1.txt", None, None)
    s.update_ingest(ingest_id, state="failed")
    s.add_ingest_log(ingest_id, [("text", "The party")])
    s.close()
    # Grow the line between the stream's first and second snapshot.
    import campaign.web as web

    ticks = []
    real_sleep = web.asyncio.sleep

    async def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 1:
            st = Store(db)
            st.add_ingest_log(ingest_id, [("text", "The party met Ismark.")], replace_last=True)
            st.close()
        if len(ticks) == 2:
            st = Store(db)
            st.add_ingest_log(ingest_id, [("tool", "lookup Ismark")])
            st.close()
            stop.set()
        await real_sleep(0)

    monkeypatch.setattr(web.asyncio, "sleep", sleep)
    try:
        with client.stream("GET", "/api/campaigns/strahd/events?seconds=60") as r:
            got = [d for e, d in events(r) if e == "ingest"]
    finally:
        client.__exit__(None, None, None)
    # First the line, then the same line grown, then only the new tool line.
    assert [[x["text"] for x in d["log"]] for d in got] == [
        ["The party"], ["The party met Ismark."], ["lookup Ismark"]]


def test_chat_streams_and_is_kept(app, monkeypatch):
    client, _ = app
    clients = [
        FakeClient([
            ([BetaToolUseBlock(type="tool_use", id="t1", name="list_sessions", input={})], "tool_use"),
            ([BetaTextBlock(type="text", text="No sessions yet.")], "end_turn"),
        ]),
        FakeClient([([BetaTextBlock(type="text", text="Still none.")], "end_turn")]),
    ]
    seen = []

    def make():
        c = clients.pop(0)
        seen.append(c)
        return c

    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", make)
    chat = client.post("/api/campaigns/strahd/chats").json()
    with client.stream("POST", f"/api/campaigns/strahd/chats/{chat['id']}/messages", json={"text": "What happened?"}) as r:
        got = events(r)
    got = [(e, d) for e, d in got if not (e == "text" and not d.strip())]  # the newline after a tool round
    assert [e for e, _ in got] == ["tool", "text", "done"]
    assert "list_sessions" in got[0][1] and got[1][1] == "No sessions yet."

    # A second turn sends the whole stored history back, exactly as it was.
    with client.stream("POST", f"/api/campaigns/strahd/chats/{chat['id']}/messages", json={"text": "And now?"}) as r:
        assert [e for e, _ in events(r)] == ["text", "done"]
    sent = seen[1].requests[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user", "assistant", "user"]
    assert sent[1]["content"][0] == {"id": "t1", "input": {}, "name": "list_sessions", "type": "tool_use"}

    history = client.get(f"/api/campaigns/strahd/chats/{chat['id']}").json()
    assert history["title"] == "What happened?"
    assert [(t["role"], t["kind"]) for t in history["turns"]] == [
        ("user", "text"), ("assistant", "tool"), ("assistant", "text"), ("user", "text"), ("assistant", "text")]
    assert [c["id"] for c in client.get("/api/campaigns/strahd/chats").json()] == [chat["id"]]
    assert client.post(f"/api/campaigns/strahd/chats/{chat['id']}/messages", json={"text": " "}).status_code == 422
    assert client.delete(f"/api/campaigns/strahd/chats/{chat['id']}").status_code == 204
    assert client.get(f"/api/campaigns/strahd/chats/{chat['id']}").status_code == 404
