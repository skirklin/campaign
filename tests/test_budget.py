"""Out of API budget: the app says so plainly, waits, and notices when it's back."""

import httpx2
import pytest
from fastapi.testclient import TestClient

from campaign import budget, digest, jobs
from campaign.web import create_app

LIMIT = {"type": "error", "error": {"type": "invalid_request_error",
                                    "message": "You have reached your specified API usage limits. You will regain access on 2026-12-01."}}
BILLING = {"type": "error", "error": {"type": "billing_error", "message": "Your credit balance is too low."}}
OK = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
      "content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 1, "output_tokens": 1}}


@pytest.fixture(autouse=True)
def fresh():
    budget.reset()
    yield
    budget.reset()


def api(*replies):
    """A client whose API answers with these (status, body) pairs in turn."""
    queue = list(replies)

    def answer(request):
        status, body = queue.pop(0)
        return httpx2.Response(status, json=body)

    return budget.client(transport=httpx2.MockTransport(answer), api_key="test", max_retries=0)


def ask(client):
    return client.messages.create(model="claude-opus-5-5", max_tokens=5, messages=[{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("status, body", [(400, LIMIT), (402, BILLING)])
def test_the_spending_limit_pauses_claude_until_a_call_goes_through(status, body):
    client = api((status, body), (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}),
                 (200, OK))
    with pytest.raises(Exception) as caught:
        ask(client)
    assert budget.is_budget_error(caught.value) and budget.explain(caught.value) == budget.MESSAGE
    assert budget.paused() and budget.should_wait()
    assert budget.status()["message"] == budget.MESSAGE and budget.status()["api"] == body["error"]["message"]
    with pytest.raises(Exception) as other:  # another kind of refusal isn't the budget, and doesn't clear it
        ask(client)
    assert not budget.is_budget_error(other.value) and budget.explain(other.value) != budget.MESSAGE and budget.paused()
    ask(client)  # it's back
    assert not budget.paused() and budget.status() is None


def test_wrapped_errors_are_recognized():
    client = api((400, LIMIT))
    try:
        try:
            ask(client)
        except Exception as err:
            raise RuntimeError("the agent failed") from err
    except RuntimeError as wrapped:
        assert budget.is_budget_error(wrapped)
    assert not budget.is_budget_error(ValueError("nope"))


def test_background_work_waits(tmp_path, monkeypatch):
    client = api((400, LIMIT))
    with pytest.raises(Exception):
        ask(client)
    # The digest doesn't ask again for a while, and a job hit by it waits instead of failing.
    assert digest.Reconciler(tmp_path / "c.db").reconcile_once() is False
    monkeypatch.setattr(budget, "RECHECK_SECONDS", 0)
    assert not budget.should_wait()  # and after a while it tries again, to find out it's back
    assert jobs.budget is budget


def test_the_banner_endpoint(tmp_path):
    client = TestClient(create_app(tmp_path / "c.db"))
    assert client.get("/api/claude").json() == {"paused": None}
    with pytest.raises(Exception):
        ask(api((402, BILLING)))
    assert client.get("/api/claude").json()["paused"]["message"] == budget.MESSAGE
