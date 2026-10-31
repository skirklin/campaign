import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from campaign.accounts import Accounts
from campaign.auth import AuthConfig
from campaign.db import Store
from campaign.web import create_app


@pytest.fixture(autouse=True)
def no_titles(monkeypatch):
    """Writing a conversation's title is its own API call; tests that fake the API script only the chat."""
    import campaign.agent

    monkeypatch.setattr(campaign.agent, "conversation_title", lambda question, answer: None)

BASE = "https://campaign.example"


def auth_config(secret="s" * 40):
    return AuthConfig(client_id="client-123", client_secret="shh", public_url=BASE, session_secret=secret.encode())


class FakeGoogle:
    """Stands in for Google's token endpoint: signs in whoever the test says."""

    def __init__(self):
        self.email = None
        self.claims = {}
        self.calls = []

    def __call__(self, cfg, code, verifier):
        self.calls.append((code, verifier))
        return self.claims


class Site:
    """An app with sign-in on, and a browser per person."""

    def __init__(self, tmp_path):
        self.db = tmp_path / "site.db"
        self.cfg = auth_config()
        self.google = FakeGoogle()
        self.app = create_app(self.db, self.cfg, exchange=self.google)

    def browser(self) -> TestClient:
        return TestClient(self.app, base_url=BASE, follow_redirects=False, headers={"origin": BASE})

    def sign_in(self, browser, email, invite="", next_="/", claims=None):
        """Go through the Google round trip as `email`; returns the callback response."""
        params = {"next": next_} | ({"invite": invite} if invite else {})
        r = browser.get("/auth/login", params=params)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
        self.google.claims = {"iss": "https://accounts.google.com", "aud": "client-123", "exp": time.time() + 600,
                              "nonce": q["nonce"][0], "email": email, "email_verified": True} | (claims or {})
        return browser.get("/auth/callback", params={"code": "abc", "state": q["state"][0]})

    def user(self, email, admin=False):
        """A signed-in browser for a user who exists (made directly, not by invite)."""
        s = Store(self.db)
        Accounts(s).create_user(email, is_admin=admin)
        s.close()
        b = self.browser()
        assert self.sign_in(b, email).status_code == 303
        return b


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path)
