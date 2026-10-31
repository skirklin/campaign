"""Drive the agent loop with a scripted stand-in for the Messages API."""

from types import SimpleNamespace as NS

import pytest

from campaign import agent as agent_mod
from campaign.db import Store
from campaign.world import World


def text(t):
    return NS(type="text", text=t)


def tool_use(id, tool, **input):
    return NS(type="tool_use", id=id, name=tool, input=input)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for b in self.message.content:
            if b.type == "text":
                yield NS(type="content_block_delta", delta=NS(type="text_delta", text=b.text))

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        content, stop = self.script.pop(0)
        usage = NS(input_tokens=50, output_tokens=20, cache_read_input_tokens=1000, cache_creation_input_tokens=200)
        return FakeStream(NS(content=content, stop_reason=stop, usage=usage))


@pytest.fixture
def store():
    s = Store(":memory:")
    s.create_campaign("Strahd", "dm")
    yield s
    s.close()


def install(monkeypatch, script):
    client = FakeClient(script)
    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: client)
    return client


def test_ingest_records_state_and_finishes(store, monkeypatch):
    client = install(
        monkeypatch,
        [
            ([text("Checking state."), tool_use("t1", "find_entities")], "tool_use"),
            (
                [
                    tool_use("t2", "record_fact", entity="Ireena Kolyana", kind="npc",
                             statement="Ireena is alive and in Barovia village", attribute="status", value="alive"),
                    tool_use("t3", "add_journal_entry", text="The party met Ireena.",
                             entities=["Ireena Kolyana"]),
                    tool_use("t4", "record_fact", entity="Bad", statement=["not", "a", "string"]),
                ],
                "tool_use",
            ),
            ([tool_use("t5", "finish_session", title="Into the Mists", summary="They arrived.")], "tool_use"),
            ([text("Done. Unsure whether Ismark joined the party.")], "end_turn"),
        ],
    )
    result = agent_mod.ingest_session(store, "strahd", "DM: You see a girl...", on_text=lambda t: None)
    c = store.campaign("strahd")
    assert result["session"] == 1 and result["title"] == "Into the Mists"
    assert result["journal_entries"] == 1
    assert store.sessions(c)[0]["summary"] == "They arrived."
    world = World(store, c)
    assert world.lookup("Ireena Kolyana")[0]["current"] == {"status": "alive"}
    assert result["facts"] == 1
    assert world.revert_session(1)["entities_removed"] == 1
    assert world.lookup("Ireena Kolyana") == []

    # all three results go back in one user message; the bad input is an error result
    results = client.requests[2]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t2", "t3", "t4"]
    assert [r["is_error"] for r in results] == [False, False, True]
    assert client.requests[0]["fallbacks"] == "default"
    assert "<transcript" in client.requests[0]["messages"][0]["content"][0]["text"]


def test_chat_keeps_history_and_recovers_from_refusal(store, monkeypatch):
    client = install(
        monkeypatch,
        [
            ([text("Ireena is in Vallaki.")], "end_turn"),
            ([tool_use("t1", "list_sources")], "tool_use"),
            ([], "refusal"),
            ([text("Sure.")], "end_turn"),
        ],
    )
    a = agent_mod.chat_agent(store, "strahd", on_text=lambda t: None)
    assert a.send("Where is Ireena?") == "Ireena is in Vallaki."
    assert "declined" in a.send("something odd")
    assert len(a.messages) == 2  # the refused exchange is rolled back entirely
    a.send("ok")
    assert [m["role"] for m in client.requests[-1]["messages"]] == ["user", "assistant", "user"]


def test_conversations_share_a_cacheable_prefix(store, monkeypatch):
    """Two new conversations in one campaign send the same tools and system prompt, with a
    cache breakpoint at the end of the system prompt, so the second reads what the first wrote."""
    from campaign.agent import chat_agent

    sent = []
    for question in ("Who is Ireena?", "What's in the windmill?"):
        client = install(monkeypatch, [([text("ok")], "end_turn")])
        chat_agent(store, "strahd", on_text=lambda t: None).send(question)
        sent.append(client.requests[0])
    a, b = sent
    assert a["system"] == b["system"] and a["tools"] == b["tools"]
    assert a["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert a["messages"] != b["messages"]
