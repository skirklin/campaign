"""Google sign-in: the flow, claim checks, invite-only access, and request gating."""

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from campaign.accounts import Accounts
from campaign.auth import AuthConfig, sign
from campaign.db import Store
from campaign.web import create_app, serve

from conftest import BASE, auth_config


def test_everything_but_sign_in_and_health_is_closed(site):
    b = site.browser()
    assert b.get("/api/campaigns").status_code == 401
    r = b.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?next=/"
    assert b.get("/static/app.js").status_code == 303
    assert b.get("/healthz").json()["ok"] is True
    # The manifest and icons are open, so a phone can offer to install the app before signing in.
    manifest = b.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.json()["display"] == "standalone"
    for icon in manifest.json()["icons"]:
        assert b.get(icon["src"]).status_code == 200, icon["src"]
    assert b.get("/static/icons/%2e%2e/app.js").status_code != 200  # no way out of the open folders


def test_sign_in_is_invite_only(site):
    r = site.sign_in(site.browser(), "stranger@example.com")
    assert r.status_code == 403 and "invite-only" in r.text
    assert "campaign_session" not in r.headers.get("set-cookie", "")


def test_sign_in_flow(site):
    s = Store(site.db)
    Accounts(s).create_user("dm@example.com")
    s.close()
    b = site.browser()
    r = site.sign_in(b, "DM@example.com", next_="/somewhere")  # Google may vary case
    assert r.status_code == 303 and r.headers["location"] == "/somewhere"
    cookie = r.headers["set-cookie"]
    assert "__Host-campaign_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "samesite=lax" in cookie.lower()
    assert site.google.calls[0][0] == "abc" and len(site.google.calls[0][1]) > 40  # PKCE verifier sent
    assert b.get("/api/campaigns").status_code == 200
    assert b.get("/api/me").json() == {"id": 1, "email": "dm@example.com", "is_admin": False}
    b.post("/auth/logout")
    assert b.get("/api/campaigns").status_code == 401


def test_login_parameters(site):
    r = site.browser().get("/auth/login")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email"]
    assert q["redirect_uri"] == [f"{BASE}/auth/callback"] and q["client_id"] == ["client-123"]


@pytest.mark.parametrize("change, reason", [
    ({"email_verified": False}, "verified email"),
    ({"aud": "someone-else"}, "different client"),
    ({"iss": "https://evil.example"}, "issued by Google"),
    ({"exp": 1}, "expired"),
    ({"nonce": "replayed"}, "nonce"),
])
def test_bad_tokens_are_refused(site, change, reason):
    s = Store(site.db)
    Accounts(s).create_user("dm@example.com")
    s.close()
    b = site.browser()
    r = site.sign_in(b, "dm@example.com", claims=change)
    assert r.status_code == 403 and reason in r.text
    assert b.get("/api/campaigns").status_code == 401


def test_state_must_match_this_browsers_flow(site):
    b = site.browser()
    b.get("/auth/login")
    assert b.get("/auth/callback", params={"code": "abc", "state": "forged"}).status_code == 400
    assert site.browser().get("/auth/callback", params={"code": "abc", "state": "x"}).status_code == 400
    assert site.google.calls == []


def test_open_redirects_are_neutralized(site):
    b = site.user("dm@example.com")
    for target in ("//evil.example", "https://evil.example", "/\\evil.example"):
        assert site.sign_in(b, "dm@example.com", next_=target).headers["location"] == "/"


def test_cross_origin_writes_are_refused(site):
    b = site.user("dm@example.com")
    body = {"name": "Strahd", "role": "dm"}
    assert b.post("/api/campaigns", json=body, headers={"origin": "https://evil.example"}).status_code == 403
    assert b.post("/api/campaigns", json=body).status_code == 201


def test_forged_and_removed_sessions_stop_working(site):
    b = site.user("dm@example.com")
    cookie = b.cookies.get("__Host-campaign_session")
    body, mac = cookie.rsplit(".", 1)
    forged = sign(auth_config(secret="x" * 40), {"uid": 1, "email": "dm@example.com", "exp": 9e9}, "session")
    for bad in (forged, body[:-2] + "AA." + mac, ""):
        c = TestClient(site.app, base_url=BASE, cookies={"__Host-campaign_session": bad})
        assert c.get("/api/campaigns").status_code == 401
    s = Store(site.db)
    Accounts(s).delete_user(1)
    s.close()
    assert b.get("/api/campaigns").status_code == 401  # removed users are out at once


def test_config_from_env(monkeypatch):
    monkeypatch.delenv("CAMPAIGN_AUTH", raising=False)
    assert AuthConfig.from_env() is None
    monkeypatch.setenv("CAMPAIGN_AUTH", "google")
    with pytest.raises(SystemExit, match="GOOGLE_CLIENT_ID"):
        AuthConfig.from_env()
    for k, v in {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret", "CAMPAIGN_PUBLIC_URL": BASE + "/",
                 "CAMPAIGN_SESSION_SECRET": "short"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match="32"):
        AuthConfig.from_env()
    monkeypatch.setenv("CAMPAIGN_SESSION_SECRET", "z" * 32)
    assert AuthConfig.from_env().public_url == BASE


def test_create_app_installs_sign_in(tmp_path):
    client = TestClient(create_app(tmp_path / "p.db", auth_config()), base_url=BASE, follow_redirects=False)
    assert client.get("/api/campaigns").status_code == 401
    assert client.get("/auth/login").headers["location"].startswith("https://accounts.google.com/")
    assert client.get("/docs").status_code in (303, 404)


def test_refuses_public_interface_without_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_AUTH", raising=False)
    with pytest.raises(SystemExit, match="Refusing"):
        serve(tmp_path / "x.db", host="0.0.0.0")
