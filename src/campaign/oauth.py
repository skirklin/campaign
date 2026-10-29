"""OAuth 2.1 for the remote MCP endpoint, so the Claude app can connect as you.

The MCP SDK serves the protocol endpoints (metadata, dynamic client
registration, /authorize, /token, /revoke) and checks PKCE; this module is
the storage behind them and the one step the SDK leaves to us: deciding
who the user is. That's the app's own sign-in. /authorize sends you to a
consent page that requires your Google session, names the app and where
it will send the code, and asks you to allow it.

Codes and tokens are random strings stored only as SHA-256 hashes, tied
to a user. Access tokens last an hour, refresh tokens 30 days and rotate
on use. Removing a user, or disconnecting an app from the Admin page,
revokes its tokens at once.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .auth import AuthConfig, sign, unsign
from .db import Store

SCOPE = "campaign"
CODE_SECONDS = 5 * 60
ACCESS_SECONDS = 60 * 60
REFRESH_SECONDS = 30 * 24 * 60 * 60
CONSENT_SECONDS = 10 * 60


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def user_id_of(token: AccessToken | None) -> int | None:
    return int(token.subject) if token and token.subject else None


class Provider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, db: Path | str, cfg: AuthConfig):
        self.db = db
        self.cfg = cfg

    def _store(self) -> Store:
        return Store(self.db)

    # -- clients ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        s = self._store()
        try:
            info = s.oauth_client(client_id)
        finally:
            s.close()
        return None if info is None else OAuthClientInformationFull.model_validate_json(info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        s = self._store()
        try:
            s.add_oauth_client(client_info.client_id, client_info.model_dump_json())
        finally:
            s.close()

    # -- authorization ---------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Hand the request to the consent page, signed so it can't be altered on the way."""
        request = sign(self.cfg, {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "explicit": params.redirect_uri_provided_explicitly,
            "challenge": params.code_challenge,
            "scopes": params.scopes or [SCOPE],
            "state": params.state,
            # The one resource this server issues tokens for, when the client doesn't say.
            "resource": params.resource or f"{self.cfg.public_url}/mcp",
            "exp": int(time.time()) + CONSENT_SECONDS,
        }, "mcp-consent")
        return f"{self.cfg.public_url}/oauth/consent?request={request}"

    def issue_code(self, request: dict[str, Any], user_id: int) -> str:
        code = secrets.token_urlsafe(32)
        s = self._store()
        try:
            s.prune_oauth(int(time.time()))
            s.add_oauth_grant(
                _hash(code), "code", request["client_id"], user_id, request["scopes"], request["resource"],
                int(time.time()) + CODE_SECONDS,
                {"redirect_uri": request["redirect_uri"], "explicit": request["explicit"],
                 "challenge": request["challenge"]},
            )
        finally:
            s.close()
        return code

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        s = self._store()
        try:
            g = s.oauth_grant(_hash(authorization_code), "code", int(time.time()))
        finally:
            s.close()
        if g is None or g["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=g["scopes"], expires_at=g["expires_at"], client_id=g["client_id"],
            code_challenge=g["data"]["challenge"], redirect_uri=g["data"]["redirect_uri"],
            redirect_uri_provided_explicitly=g["data"]["explicit"], resource=g["resource"], subject=str(g["user_id"]),
        )

    def _tokens(self, s: Store, client_id: str, user_id: int, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        s.add_oauth_grant(_hash(access), "access", client_id, user_id, scopes, resource, now + ACCESS_SECONDS)
        s.add_oauth_grant(_hash(refresh), "refresh", client_id, user_id, scopes, resource, now + REFRESH_SECONDS)
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_SECONDS,
            refresh_token=refresh, scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        s = self._store()
        try:
            s.delete_oauth_grants(_hash(authorization_code.code))  # single use
            return self._tokens(s, client.client_id, int(authorization_code.subject), authorization_code.scopes,
                                authorization_code.resource)
        finally:
            s.close()

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        s = self._store()
        try:
            g = s.oauth_grant(_hash(refresh_token), "refresh", int(time.time()))
        finally:
            s.close()
        if g is None or g["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=g["client_id"], scopes=g["scopes"],
                            expires_at=g["expires_at"], resource=g["resource"], subject=str(g["user_id"]))

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "can't widen the scope on refresh")
        s = self._store()
        try:
            s.delete_oauth_grants(_hash(refresh_token.token))  # rotate
            return self._tokens(s, client.client_id, int(refresh_token.subject), scopes or refresh_token.scopes,
                                refresh_token.resource)
        finally:
            s.close()

    # -- checking tokens -----------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        s = self._store()
        try:
            g = s.oauth_grant(_hash(token), "access", int(time.time()))
        finally:
            s.close()
        if g is None:
            return None
        return AccessToken(token=token, client_id=g["client_id"], scopes=g["scopes"], expires_at=g["expires_at"],
                           resource=g["resource"], subject=str(g["user_id"]))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        s = self._store()
        try:
            s.delete_oauth_grants(_hash(token.token))
        finally:
            s.close()


# -- the consent page ------------------------------------------------------------------


def _consent_page(client: OAuthClientInformationFull, email: str, request_token: str, redirect_uri: str) -> HTMLResponse:
    name = client.client_name or "An app"
    host = urlsplit(redirect_uri).netloc or redirect_uri
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>Connect to Campaign</title>"
        "<body style='font:16px system-ui;max-width:32rem;margin:12vh auto;padding:0 1rem;line-height:1.5'>"
        f"<h1>Connect {escape(name)}?</h1>"
        f"<p><strong>{escape(name)}</strong> wants to use your campaigns as <strong>{escape(email)}</strong>: "
        "read what you can read in each campaign, and change what you can change (your character sheets, "
        "and the world state where you're an editor).</p>"
        f"<p>It will be signed in through <code>{escape(host)}</code>. Only allow this if you just asked to "
        "connect an app there.</p>"
        "<form method=post action='/oauth/consent' style='display:flex;gap:.6rem'>"
        f"<input type=hidden name=request value='{escape(request_token, quote=True)}'>"
        "<button name=decision value=allow style='font:inherit;padding:.4rem 1rem'>Allow</button>"
        "<button name=decision value=deny style='font:inherit;padding:.4rem 1rem'>Deny</button>"
        "</form>",
        # Never inside someone else's frame, where the Allow button could be clicked unseen.
        headers={"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"},
    )


def install(app: FastAPI, provider: Provider) -> None:
    cfg = provider.cfg

    async def pending(request_token: str) -> tuple[dict[str, Any], OAuthClientInformationFull] | None:
        req = unsign(cfg, request_token, "mcp-consent")
        client = await provider.get_client(req["client_id"]) if req else None
        return (req, client) if client else None

    def expired() -> HTMLResponse:
        return HTMLResponse("<p>This request has expired. Start connecting again from the app.</p>", status_code=400)

    # Both need the app's own sign-in: the login middleware sends you to Google first.
    @app.get("/oauth/consent", include_in_schema=False)
    async def consent(request: Request, request_token: str = Query("", alias="request")) -> Response:
        found = await pending(request_token)
        if found is None:
            return expired()
        req, client = found
        return _consent_page(client, request.state.user["email"], request_token, req["redirect_uri"])

    @app.post("/oauth/consent", include_in_schema=False)
    async def decide(request: Request, request_token: str = Form(alias="request"), decision: str = Form("deny")) -> Response:
        found = await pending(request_token)
        if found is None:
            return expired()
        req, _ = found
        if decision != "allow":
            return RedirectResponse(construct_redirect_uri(req["redirect_uri"], error="access_denied", state=req["state"]),
                                    status_code=302)
        code = provider.issue_code(req, request.state.user["id"])
        return RedirectResponse(construct_redirect_uri(req["redirect_uri"], code=code, state=req["state"]), status_code=302)
