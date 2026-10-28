"""Google sign-in: the flow, claim checks, the allowlist, and request gating."""

import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from campaign.auth import AuthConfig, sign
from campaign.web import create_app, serve

BASE = "https://campaign.example"


def config(emails=("dm@example.com",), secret="s" * 40):
    return AuthConfig(client_id="client-123", client_secret="shh", public_url=BASE,
                      allowed_emails=frozenset(emails), session_secret=secret.encode())


class FakeGoogle:
    """Stands in for the token endpoint: returns whatever claims the test sets."""

    def __init__(self):
        self.claims = {}
        self.calls = []

    def __call__(self, cfg, code, verifier):
        self.calls.append((code, verifier))
        return self.claims


def make(tmp_path, cfg=None):
    from campaign import auth

    cfg = cfg or config()
    google = FakeGoogle()
    app = create_app(tmp_path / "a.db")
    # create_app installs auth itself in production; here we inject the fake exchange.
    auth.install(app, cfg, exchange=google)
    return TestClient(app, base_url=BASE, follow_redirects=False), google, cfg


def sign_in(client, google, email="dm@example.com", next_="/"):
    r = client.get("/auth/login", params={"next": next_})
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    google.claims = {"iss": "https://accounts.google.com", "aud": "client-123", "exp": time.time() + 600,
                     "nonce": q["nonce"][0], "email": email, "email_verified": True}
    return client.get("/auth/callback", params={"code": "abc", "state": q["state"][0]}), q


def test_everything_but_sign_in_and_health_is_closed(tmp_path):
    client, _, _ = make(tmp_path)
    assert client.get("/api/campaigns").status_code == 401
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?next=/"
    assert client.get("/static/app.js").status_code == 303
    assert client.get("/healthz").json()["ok"] is True


def test_sign_in_flow(tmp_path):
    client, google, _ = make(tmp_path)
    r, q = sign_in(client, google, next_="/somewhere")
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email"]
    assert q["redirect_uri"] == [f"{BASE}/auth/callback"]
    assert r.status_code == 303 and r.headers["location"] == "/somewhere"
    cookie = r.headers["set-cookie"]
    assert "__Host-campaign_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "samesite=lax" in cookie.lower()
    assert google.calls[0][0] == "abc" and len(google.calls[0][1]) > 40  # PKCE verifier sent
    assert client.get("/api/campaigns").status_code == 200
    assert client.get("/auth/me").json() == {"email": "dm@example.com"}
    client.post("/auth/logout", headers={"origin": BASE})
    assert client.get("/api/campaigns").status_code == 401


@pytest.mark.parametrize("change, reason", [
    ({"email": "stranger@example.com"}, "allowed to use this app"),
    ({"email_verified": False}, "verified email"),
    ({"aud": "someone-else"}, "different client"),
    ({"iss": "https://evil.example"}, "issued by Google"),
    ({"exp": 1}, "expired"),
    ({"nonce": "replayed"}, "nonce"),
])
def test_bad_tokens_are_refused(tmp_path, change, reason):
    client, google, _ = make(tmp_path)
    r = client.get("/auth/login")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    google.claims = {"iss": "accounts.google.com", "aud": "client-123", "exp": time.time() + 600,
                     "nonce": q["nonce"][0], "email": "dm@example.com", "email_verified": True} | change
    r = client.get("/auth/callback", params={"code": "abc", "state": q["state"][0]})
    assert r.status_code == 403 and reason in r.text
    assert "campaign_session" not in r.headers.get("set-cookie", "")
    assert client.get("/api/campaigns").status_code == 401


def test_state_must_match_this_browsers_flow(tmp_path):
    client, google, _ = make(tmp_path)
    client.get("/auth/login")
    assert client.get("/auth/callback", params={"code": "abc", "state": "forged"}).status_code == 400
    fresh = TestClient(client.app, base_url=BASE, follow_redirects=False)  # no flow cookie at all
    assert fresh.get("/auth/callback", params={"code": "abc", "state": "x"}).status_code == 400
    assert google.calls == []


def test_open_redirects_are_neutralized(tmp_path):
    client, google, _ = make(tmp_path)
    for target in ("//evil.example", "https://evil.example", "/\\evil.example"):
        r, _ = sign_in(client, google, next_=target)
        assert r.headers["location"] == "/"


def test_cross_origin_writes_are_refused(tmp_path):
    client, google, _ = make(tmp_path)
    sign_in(client, google)
    body = {"name": "Strahd", "role": "dm"}
    assert client.post("/api/campaigns", json=body, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/campaigns", json=body, headers={"origin": BASE}).status_code == 201


def test_removed_or_forged_sessions_stop_working(tmp_path):
    client, google, cfg = make(tmp_path)
    sign_in(client, google)
    cookie = client.cookies.get("__Host-campaign_session")
    # Tampered payload
    body, mac = cookie.rsplit(".", 1)
    forged = sign(config(secret="x" * 40), {"email": "dm@example.com", "exp": time.time() + 60}, "session")
    for bad in (forged, body[:-2] + "AA." + mac, ""):
        c = TestClient(client.app, base_url=BASE, cookies={"__Host-campaign_session": bad})
        assert c.get("/api/campaigns").status_code == 401
    # Same secret, but the address was taken off the allowlist
    client2, _, _ = make(tmp_path, config(emails=("someone-else@example.com",)))
    client2.cookies.set("__Host-campaign_session", cookie)
    assert client2.get("/api/campaigns").status_code == 401


def test_config_from_env(monkeypatch):
    monkeypatch.delenv("CAMPAIGN_AUTH", raising=False)
    assert AuthConfig.from_env() is None
    monkeypatch.setenv("CAMPAIGN_AUTH", "google")
    with pytest.raises(SystemExit, match="GOOGLE_CLIENT_ID"):
        AuthConfig.from_env()
    for k, v in {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret", "CAMPAIGN_PUBLIC_URL": BASE + "/",
                 "CAMPAIGN_ALLOWED_EMAILS": " DM@Example.com, , pal@example.com", "CAMPAIGN_SESSION_SECRET": "short"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match="32"):
        AuthConfig.from_env()
    monkeypatch.setenv("CAMPAIGN_SESSION_SECRET", "z" * 32)
    cfg = AuthConfig.from_env()
    assert cfg.allowed_emails == {"dm@example.com", "pal@example.com"} and cfg.public_url == BASE


def test_refuses_public_interface_without_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_AUTH", raising=False)
    with pytest.raises(SystemExit, match="Refusing"):
        serve(tmp_path / "x.db", host="0.0.0.0")


def test_create_app_installs_sign_in(tmp_path):
    client = TestClient(create_app(tmp_path / "p.db", config()), base_url=BASE, follow_redirects=False)
    assert client.get("/api/campaigns").status_code == 401
    assert client.get("/auth/login").headers["location"].startswith("https://accounts.google.com/")
    assert client.get("/docs").status_code in (303, 404)  # no API explorer either way
