"""The remote MCP endpoint: OAuth for the Claude app, then tools acting as the signed-in user."""

import base64
import hashlib
import json
import secrets
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from conftest import BASE
from test_access import join, setup_campaign

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def api(site):
    # The MCP session manager runs in the app's lifespan, which TestClient starts as a context manager.
    with TestClient(site.app, base_url=BASE) as client:
        yield client


def register(api, name="Claude"):
    r = api.post("/register", json={
        "client_name": name, "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": "campaign",
    })
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def connect(site, api, browser, client_id, decision="allow"):
    """The whole OAuth dance for a signed-in browser; returns the token response (or the denial redirect)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = browser.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "xyz",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "campaign", "resource": f"{BASE}/mcp",
    })
    assert r.status_code == 302, r.text
    consent = r.headers["location"]
    assert consent.startswith(f"{BASE}/oauth/consent?request=")
    page = browser.get(consent)
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    request_token = urllib.parse.parse_qs(urllib.parse.urlsplit(consent).query)["request"][0]
    back = browser.post("/oauth/consent", data={"request": request_token, "decision": decision})
    assert back.status_code == 302 and back.headers["location"].startswith(REDIRECT)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(back.headers["location"]).query)
    assert q["state"] == ["xyz"]
    if decision != "allow":
        return q
    r = api.post("/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier, "resource": f"{BASE}/mcp",
    })
    assert r.status_code == 200, r.text
    return r.json()


def rpc(api, token, method, params=None):
    r = api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers={
        "authorization": f"Bearer {token}", "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
    })
    return r


def call(api, token, tool, **arguments):
    r = rpc(api, token, "tools/call", {"name": tool, "arguments": arguments})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    return result["content"][0]["text"], result.get("isError", False)


def test_claude_app_connects_and_acts_as_the_user(site, api):
    dm, _ = setup_campaign(site)
    meta = api.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"] == f"{BASE}/register" and "S256" in meta["code_challenge_methods_supported"]
    assert api.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == f"{BASE}/mcp"

    # Without a token the endpoint says where to sign in.
    r = rpc(api, "nope", "tools/list")
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]

    client_id = register(api)
    # Someone not signed in is sent to sign in first, and comes back to the same consent request.
    stranger = site.browser()
    consent = stranger.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "s",
        "code_challenge": "x" * 43, "code_challenge_method": "S256",
    }).headers["location"]
    login = stranger.get(consent)
    assert login.status_code == 303 and urllib.parse.unquote(login.headers["location"]).endswith(
        urllib.parse.urlsplit(consent).path + "?" + urllib.parse.urlsplit(consent).query)

    tokens = connect(site, api, dm, client_id)
    token = tokens["access_token"]
    names = {t["name"] for t in rpc(api, token, "tools/list").json()["result"]["tools"]}
    assert {"list_campaigns", "campaign_overview", "lookup", "create_character", "record_fact"} <= names

    text, _ = call(api, token, "list_campaigns")
    assert json.loads(text) == [{"slug": "strahd", "name": "Strahd", "role": "dm", "access": "owner"}]
    text, _ = call(api, token, "lookup", campaign="strahd", name="the windmill hag")
    assert "Morgantha" in text
    assert "No campaign" in call(api, token, "lookup", campaign="avernus", name="x")[0]

    sheet = {"name": "Bram", "classes": [{"name": "Paladin", "level": 3}], "abilities": {
        "str": 16, "dex": 10, "con": 14, "int": 8, "wis": 12, "cha": 15}, "hp": {"max": 28, "current": 28}}
    text, _ = call(api, token, "create_character", campaign="strahd", sheet=sheet, reason="from a scan")
    assert json.loads(text)["derived"]["level"] == 3
    chars = dm.get("/api/campaigns/strahd/characters").json()
    assert [c["name"] for c in chars] == ["Bram"] and chars[0]["mine"]  # owned by the connected user

    # Refresh tokens rotate: the old one stops working.
    r = api.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                 "client_id": client_id})
    assert r.status_code == 200
    again = api.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                     "client_id": client_id})
    assert again.status_code == 400

    # Disconnecting the app revokes it.
    apps = dm.get("/api/me/apps").json()
    assert [a["client_name"] for a in apps] == ["Claude"]
    assert dm.delete(f"/api/me/apps/{client_id}").status_code == 204
    assert rpc(api, r.json()["access_token"], "tools/list").status_code == 401


def test_a_player_gets_their_own_view_and_access(site, api):
    dm, _ = setup_campaign(site)
    player, _, _ = join(site, dm, "pc@example.com")  # player, viewer
    client_id = register(api)
    token = connect(site, api, player, client_id)["access_token"]
    # The player knows Morgantha from play, but not what the DM-only book says about her.
    seen = call(api, token, "lookup", campaign="strahd", name="Morgantha")[0]
    assert "bakes pies" in seen and "the windmill hag" not in seen
    dm_token = connect(site, api, dm, client_id)["access_token"]
    assert "the windmill hag" in call(api, dm_token, "lookup", campaign="strahd", name="Morgantha")[0]
    # Viewers can't change the world, but can make their own character.
    assert "can't change" in call(api, token, "record_fact", campaign="strahd", entity="Ireena", statement="x")[0]
    text, _ = call(api, token, "create_character", campaign="strahd", sheet={"name": "Ireth"})
    assert "created" in text
    assert player.get("/api/campaigns/strahd/characters").json()[0]["mine"]


def test_denying_consent_and_tokens_for_other_resources(site, api):
    dm, _ = setup_campaign(site)
    client_id = register(api)
    assert connect(site, api, dm, client_id, decision="deny")["error"] == ["access_denied"]
    # A consent request can't be replayed by someone else's browser with a forged decision either:
    # it's signed, and the code goes to whoever is signed in, which is checked at the consent page.
    other = site.browser()
    assert other.post("/oauth/consent", data={"request": "forged.sig", "decision": "allow"}).status_code in (303, 401)


def test_ask_assistant_runs_the_apps_chat_and_keeps_it(site, api, monkeypatch):
    from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock

    import campaign.agent as agent_mod
    from test_agent import FakeClient

    dm, _ = setup_campaign(site)
    clients = [
        FakeClient([
            ([BetaToolUseBlock(type="tool_use", id="t1", name="lookup", input={"name": "Morgantha"})], "tool_use"),
            ([BetaTextBlock(type="text", text="A **night hag**.")], "end_turn"),
        ]),
        FakeClient([([BetaTextBlock(type="text", text="She bakes pies.")], "end_turn")]),
    ]
    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: clients.pop(0))
    token = connect(site, api, dm, register(api))["access_token"]

    got = json.loads(call(api, token, "ask_assistant", campaign="strahd", question="Who is Morgantha?")[0])
    assert got["answer"] == "A **night hag**." and got["lookups"] == ["Looked up “Morgantha”"]
    assert (got["usage"]["api_calls"], got["usage"]["tool_calls"]) == (2, 1) and got["usage"]["cost_usd"] > 0
    conv = got["conversation_id"]
    again = json.loads(call(api, token, "ask_assistant", campaign="strahd", question="What does she do?",
                            conversation_id=conv)[0])
    assert again["conversation_id"] == conv and again["answer"] == "She bakes pies."
    # It's an ordinary conversation in the app, with what each answer cost.
    turns = dm.get(f"/api/campaigns/strahd/chats/{conv}").json()["turns"]
    assert [t["text"] for t in turns if t["kind"] == "text"] == [
        "Who is Morgantha?", "A **night hag**.", "What does she do?", "She bakes pies."]
    assert turns[2]["usage"]["tool_calls"] == 1

    # Someone else's conversation isn't yours to continue.
    player, _, _ = join(site, dm, "pc@example.com")
    ptoken = connect(site, api, player, register(api))["access_token"]
    assert "No conversation" in call(api, ptoken, "ask_assistant", campaign="strahd", question="hi",
                                     conversation_id=conv)[0]


def test_digest_tools_redigest_a_book_with_a_new_model(site, api, monkeypatch):
    import time

    from campaign import digest
    from test_digest import BOOK, FakeDigest

    monkeypatch.setattr(digest, "MIN_LEAF_CHARS", 1)
    fake = FakeDigest()
    monkeypatch.setattr(digest.anthropic, "Anthropic", lambda **kw: fake)
    dm, _ = setup_campaign(site)
    book = BOOK.format(intro="", church="Donavich prays.").encode()
    src = dm.post("/api/campaigns/strahd/sources", files={"file": ("book.md", book)},
                  data={"kind": "module", "digest_now": "true"}).json()
    token = connect(site, api, dm, register(api))["access_token"]

    def settled():
        for _ in range(200):
            rows = json.loads(call(api, token, "digest_status", campaign="strahd")[0])["sources"]
            row = next(r for r in rows if r["id"] == src["id"])
            if row["done"] == row["sections"] == 8:
                return row
            time.sleep(0.05)
        raise AssertionError(row)

    first = settled()
    first_model = digest.MODEL
    assert set(first["digested_with"]) == {first_model} and len(fake.calls) == 8

    # Each source keeps its own model: a new default doesn't redo it, asking for a model does.
    monkeypatch.setattr(digest, "MODEL", "claude-sonnet-5-5")
    same = json.loads(call(api, token, "digest_source", campaign="strahd", source_id=src["id"])[0])
    assert same["model"] == first_model and same["estimate"]["calls"] == 0
    assert "Unknown model" in call(api, token, "digest_source", campaign="strahd", source_id=src["id"],
                                   model="gpt-4")[0]
    got = json.loads(call(api, token, "digest_source", campaign="strahd", source_id=src["id"],
                          model="claude-sonnet-5-5")[0])
    assert got["digesting"] and got["model"] == "claude-sonnet-5-5" and got["estimate"]["calls"] == 8
    assert got["status"]["done"] == 0 and got["status"]["target_model"] == "claude-sonnet-5-5"
    again = settled()
    assert again["digested_with"].keys() == {"claude-sonnet-5-5"} and len(fake.calls) == 16
    # Asking again with nothing left to do costs nothing.
    assert json.loads(call(api, token, "digest_source", campaign="strahd", source_id=src["id"])[0])["estimate"]["calls"] == 0
    assert "No source" in call(api, token, "digest_source", campaign="strahd", source_id=src["id"] + 99)[0]

    player, _, _ = join(site, dm, "pc@example.com")
    ptoken = connect(site, api, player, register(api))["access_token"]
    assert "editor access" in call(api, ptoken, "digest_status", campaign="strahd")[0]
    assert "editor access" in call(api, ptoken, "digest_source", campaign="strahd", source_id=src["id"])[0]
