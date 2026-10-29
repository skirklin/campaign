"""Sign in with Google, for running the web UI on the open internet.

Enabled with CAMPAIGN_AUTH=google. Sign-in is invite-only: a Google account
gets in if it's already a user (see accounts.py), or if it arrives with a
valid invite link (/invite/<token>), which makes it one. The first admin is
added from the command line (`campaign users add EMAIL --admin`). Users are
looked up on every request, so removing someone signs them out at once.

The flow is OpenID Connect's authorization code flow with PKCE, a state
value and a nonce. The ID token is fetched directly from Google's token
endpoint over TLS, which the OIDC spec (Core 3.1.3.7) accepts in place of
verifying its signature, so no JWT library is needed. The session is an
HMAC-signed cookie: HttpOnly, Secure, SameSite=Lax. Changes also require
a same-origin Origin header when the browser sends one.

Settings (all required when enabled):
    CAMPAIGN_AUTH=google
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   from a Google Cloud OAuth client (Web application)
    CAMPAIGN_PUBLIC_URL      e.g. https://campaign.kirkl.in; the redirect URI is <this>/auth/callback
    CAMPAIGN_SESSION_SECRET  32+ random characters (e.g. `openssl rand -hex 32`)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import posixpath
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .accounts import Accounts, InviteError
from .db import Store

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
SESSION_DAYS = 30
FLOW_MINUTES = 10
# The app's manifest and icons are open too: browsers fetch the manifest without the
# session cookie, and can only offer to install the app if they can read it.
OPEN_PATHS = ("/auth/", "/invite/", "/healthz", "/static/manifest.webmanifest", "/static/icons/")
# The remote MCP endpoint and its OAuth endpoints check bearer tokens (or a
# client's own credentials) themselves, and are called by apps from other
# origins, so they skip both the session check and the same-origin check.
# The consent page in between is an ordinary signed-in page.
BEARER_PATHS = {"/mcp", "/mcp/", "/authorize", "/token", "/register", "/revoke"}


@dataclass(frozen=True)
class AuthConfig:
    client_id: str
    client_secret: str
    public_url: str
    session_secret: bytes

    @classmethod
    def from_env(cls) -> AuthConfig | None:
        mode = os.environ.get("CAMPAIGN_AUTH", "").strip().lower()
        if not mode or mode == "none":
            return None
        if mode != "google":
            raise SystemExit(f"CAMPAIGN_AUTH={mode!r} isn't supported; use 'google' or leave it unset.")
        env = {k: os.environ.get(k, "").strip() for k in (
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "CAMPAIGN_PUBLIC_URL", "CAMPAIGN_SESSION_SECRET",
        )}
        if missing := [k for k, v in env.items() if not v]:
            raise SystemExit(f"Google sign-in needs: {', '.join(missing)}")
        if len(env["CAMPAIGN_SESSION_SECRET"]) < 32:
            raise SystemExit("CAMPAIGN_SESSION_SECRET must be at least 32 characters")
        return cls(
            client_id=env["GOOGLE_CLIENT_ID"],
            client_secret=env["GOOGLE_CLIENT_SECRET"],
            public_url=env["CAMPAIGN_PUBLIC_URL"].rstrip("/"),
            session_secret=env["CAMPAIGN_SESSION_SECRET"].encode(),
        )

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origin(self) -> str:
        u = urllib.parse.urlsplit(self.public_url)
        return f"{u.scheme}://{u.netloc}"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def session_cookie(self) -> str:
        # __Host- cookies must be Secure, host-only and Path=/, so no other
        # subdomain can set or read them.
        return "__Host-campaign_session" if self.secure else "campaign_session"

    @property
    def flow_cookie(self) -> str:
        return "__Host-campaign_oauth" if self.secure else "campaign_oauth"


# -- signed values -----------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(cfg: AuthConfig, payload: dict[str, Any], purpose: str) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(cfg.session_secret, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(cfg: AuthConfig, token: str | None, purpose: str) -> dict[str, Any] | None:
    """The payload if the signature is valid and it hasn't expired, else None."""
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    expected = hmac.new(cfg.session_secret, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_unb64(mac), expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if payload.get("exp", 0) > time.time() else None


def current_user(cfg: AuthConfig, request: Request, accounts: Accounts) -> dict[str, Any] | None:
    """The signed-in user, if the session is valid and the user still exists."""
    session = unsign(cfg, request.cookies.get(cfg.session_cookie), "session")
    user = accounts.user(session["uid"]) if session and "uid" in session else None
    return user if user and user["email"] == session.get("email") else None


# -- Google ----------------------------------------------------------------------


def exchange_code(cfg: AuthConfig, code: str, verifier: str) -> dict[str, Any]:
    """Trade the authorization code for tokens and return the ID token's claims."""
    data = urllib.parse.urlencode({
        "code": code, "client_id": cfg.client_id, "client_secret": cfg.client_secret,
        "redirect_uri": cfg.redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        tokens = json.load(resp)
    parts = tokens["id_token"].split(".")
    return json.loads(_unb64(parts[1]))


def check_claims(cfg: AuthConfig, claims: dict[str, Any], nonce: str) -> str:
    """Validate ID token claims and return the email, or raise PermissionError."""
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise PermissionError("token wasn't issued by Google")
    aud = claims.get("aud")
    if aud != cfg.client_id and not (isinstance(aud, list) and cfg.client_id in aud):
        raise PermissionError("token is for a different client")
    if claims.get("exp", 0) < time.time():
        raise PermissionError("token has expired")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise PermissionError("nonce mismatch")
    email = str(claims.get("email", "")).lower()
    if not email or claims.get("email_verified") is not True:
        raise PermissionError("Google account has no verified email")
    return email


def _safe_next(target: str | None) -> str:
    # Only same-site relative paths, so the login can't be used as an open redirect.
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _page(title: str, message: str, status: int) -> HTMLResponse:
    from html import escape

    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{escape(title)}</title><body style='font:16px system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p><p><a href='/auth/login'>Try again</a></p>",
        status_code=status,
    )


def install(
    app: FastAPI, cfg: AuthConfig, db: Path | str,
    exchange: Callable[[AuthConfig, str, str], dict[str, Any]] = exchange_code,
) -> None:
    """Add sign-in, invite links, and a check that every other request is from a signed-in user."""

    def set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(name, value, max_age=max_age, path="/", secure=cfg.secure, httponly=True, samesite="lax")

    def start_session(resp: Response, user: dict[str, Any]) -> None:
        session = {"uid": user["id"], "email": user["email"], "exp": time.time() + SESSION_DAYS * 86400}
        set_cookie(resp, cfg.session_cookie, sign(cfg, session, "session"), SESSION_DAYS * 86400)

    def landing(invite: dict[str, Any]) -> str:
        return f"/#/campaigns/{invite['slug']}" if invite.get("slug") else "/"

    @app.get("/auth/login", include_in_schema=False)
    def login(next: str = "/", invite: str = "") -> RedirectResponse:
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        params = {
            "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri, "response_type": "code",
            "scope": "openid email", "state": state, "nonce": nonce, "prompt": "select_account",
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        resp = RedirectResponse(f"{GOOGLE_AUTH}?{urllib.parse.urlencode(params)}", status_code=303)
        flow = {"state": state, "nonce": nonce, "verifier": verifier, "next": _safe_next(next),
                "invite": invite[:100], "exp": time.time() + FLOW_MINUTES * 60}
        set_cookie(resp, cfg.flow_cookie, sign(cfg, flow, "flow"), FLOW_MINUTES * 60)
        return resp

    @app.get("/auth/callback", include_in_schema=False)
    def callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
        flow = unsign(cfg, request.cookies.get(cfg.flow_cookie), "flow")
        if error:
            return _page("Sign-in cancelled", f"Google said: {error}", 400)
        if not flow or not code or not hmac.compare_digest(state, flow["state"]):
            return _page("Sign-in expired", "That sign-in link is stale or was started elsewhere.", 400)
        try:
            email = check_claims(cfg, exchange(cfg, code, flow["verifier"]), flow["nonce"])
        except PermissionError as err:
            return _page("Not allowed", str(err), 403)
        except Exception:
            return _page("Sign-in failed", "Couldn't complete sign-in with Google.", 502)
        store = Store(db)
        try:
            accounts = Accounts(store)
            user, target = accounts.user_by_email(email), flow["next"]
            if flow.get("invite"):
                try:
                    user, invite = accounts.redeem(flow["invite"], email)
                    target = landing(invite)
                except InviteError as err:
                    if user is None:
                        return _page("Invite not accepted", str(err), 403)
            if user is None:
                return _page(
                    "Invite needed",
                    f"{email} doesn't have access. This app is invite-only: ask a campaign's owner for a link.",
                    403,
                )
        finally:
            store.close()
        resp = RedirectResponse(target, status_code=303)
        resp.delete_cookie(cfg.flow_cookie, path="/", secure=cfg.secure, httponly=True, samesite="lax")
        start_session(resp, user)
        return resp

    @app.get("/invite/{token}", include_in_schema=False)
    def accept_invite(token: str, request: Request) -> Response:
        store = Store(db)
        try:
            accounts = Accounts(store)
            user = current_user(cfg, request, accounts)
            if user is None:
                try:
                    accounts.check_invite(token)
                except InviteError as err:
                    return _page("Invite not accepted", str(err), 400)
                return RedirectResponse(f"/auth/login?{urllib.parse.urlencode({'invite': token})}", status_code=303)
            try:
                _, invite = accounts.redeem(token, user["email"])
            except InviteError as err:
                return _page("Invite not accepted", str(err), 400)
            return RedirectResponse(landing(invite), status_code=303)
        finally:
            store.close()

    @app.post("/auth/logout", include_in_schema=False)
    def logout() -> Response:
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(cfg.session_cookie, path="/", secure=cfg.secure, httponly=True, samesite="lax")
        return resp

    @app.middleware("http")
    async def require_login(request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in BEARER_PATHS or path.startswith("/.well-known/oauth-"):
            return await call_next(request)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != cfg.origin:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        # Normalised, so ".." can't climb out of an open folder into a closed one.
        if posixpath.normpath(path).startswith(OPEN_PATHS):
            return await call_next(request)
        store = Store(db)
        try:
            accounts = Accounts(store)
            user = current_user(cfg, request, accounts)
            if user and (not user["last_seen_at"] or _older_than(user["last_seen_at"], hours=1)):
                accounts.seen(user["id"])
        finally:
            store.close()
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "sign in required"}, status_code=401)
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/auth/login?next={urllib.parse.quote(target)}", status_code=303)
        request.state.user = user
        return await call_next(request)


def _older_than(iso: str, hours: int) -> bool:
    return datetime.fromisoformat(iso) < datetime.now(timezone.utc) - timedelta(hours=hours)
