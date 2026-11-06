"""When the Claude API's spending limit runs out, say so plainly, everywhere.

Everything the app asks Claude goes through `client()`, whose HTTP client
watches every response. One refused for billing (a 402 billing_error, or a
400 about usage limits or credit balance, as the API has also said it)
marks the app as out of budget, and the next one that goes through clears
it. While it's marked:

- the chat, jobs and summaries fail with MESSAGE instead of an API error;
- every page shows it in a banner (`status()`, served by /api/status);
- the digest loop stops asking, and looks again every RECHECK_SECONDS.

The mark lives in this process: the web server runs the chat, jobs and
digests in one, and a restart just finds out again on the first call.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import anthropic

MESSAGE = (
    "Claude is paused: this app's Claude API spending limit has been reached, so the assistant, "
    "session notes and reading books are on hold. Nothing is lost. It picks up again on its own when "
    "the limit resets, or as soon as whoever runs the app raises it (Anthropic Console, Limits)."
)
RECHECK_SECONDS = 30 * 60
SIGNS = ("usage limit", "spend limit", "spending limit", "credit balance", "billing")

_lock = threading.Lock()
_since: float | None = None  # when the API first refused for billing
_last = 0.0  # when it last did
_said = ""


def _billing(status: int, body: Any) -> str | None:
    """The API's message if this response is a refusal for billing, else None."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "")
    if status == 402 or error.get("type") == "billing_error":
        return message or "billing error"
    if status in (400, 403) and any(s in message.lower() for s in SIGNS):
        return message
    return None


def _watch(response: Any) -> None:
    """Every response from the API: a billing refusal marks the app out of budget; anything that worked clears it."""
    global _since, _last, _said
    if response.status_code < 400:
        if _since is not None:
            with _lock:
                _since, _said = None, ""
        return
    if response.status_code not in (400, 402, 403):
        return
    try:
        response.read()
        said = _billing(response.status_code, response.json())
    except Exception:
        return
    if said is not None:
        with _lock:
            _last = time.time()
            _since = _since or _last
            _said = said


def client(transport: Any = None, **kwargs: Any) -> anthropic.Anthropic:
    """An API client whose every response is watched for the spending limit (`transport` for tests)."""
    http = anthropic.DefaultHttpxClient(event_hooks={"response": [_watch]}, **({"transport": transport} if transport else {}))
    return anthropic.Anthropic(http_client=http, **kwargs)


def paused() -> bool:
    return _since is not None


def reset() -> None:
    """Forget the mark (tests)."""
    global _since, _last, _said
    with _lock:
        _since, _last, _said = None, 0.0, ""


def status() -> dict[str, Any] | None:
    """For the banner: since when, what to tell people, and what the API said."""
    with _lock:
        if _since is None:
            return None
        return {"since": int(_since), "message": MESSAGE, "api": _said}


def should_wait() -> bool:
    """For background work: skip this pass while out of budget, but try again now and then to find out it's back."""
    return _since is not None and time.time() - _last < RECHECK_SECONDS


def is_budget_error(err: BaseException | None) -> bool:
    """Whether an exception (or what caused it) is the API refusing for billing."""
    seen = 0
    while err is not None and seen < 8:
        if isinstance(err, anthropic.APIStatusError):
            body = err.body if isinstance(err.body, dict) else {"error": {"type": getattr(err, "type", None),
                                                                          "message": err.message}}
            if "error" not in body:
                body = {"error": body}
            if _billing(err.status_code, body) is not None:
                return True
        err = err.__cause__ or err.__context__
        seen += 1
    return False


def explain(err: BaseException) -> str:
    """What to tell a person about a failure: MESSAGE for the spending limit, else the error itself."""
    return MESSAGE if is_budget_error(err) else (str(err) or type(err).__name__)
