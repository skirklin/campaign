"""Expose the campaign tools over MCP, so Claude can be the assistant.

Two ways in:

- **Local, stdio** (`campaign mcp --campaign curse-of-strahd`): one
  campaign from the local database, for Claude Code or Claude Desktop on
  the same machine.
- **Remote, HTTP** (`<public url>/mcp`, mounted by the web app when
  sign-in is on): every campaign you belong to, for the Claude app as a
  custom connector. You sign in with OAuth (oauth.py), and each call acts
  as you: a player's campaign is spoiler-filtered, and writes need the
  same access they need in the web UI.

The MCP client's own model does the reasoning; this server only reads and
writes the campaign database.
"""

from __future__ import annotations

import inspect
import json
import typing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .accounts import Accounts, at_least
from .agent import ASK_SYSTEM, _role_parts, overview
from .auth import AuthConfig
from .db import Campaign, Store
from .oauth import SCOPE, Provider, user_id_of
from .tools import ToolContext, build_tools


def serve(store: Store, campaign_slug: str, read_only: bool = False) -> None:
    campaign = store.campaign(campaign_slug)
    server = MCPServer(
        name=f"campaign-{campaign.slug}",
        instructions=ASK_SYSTEM.format(
            name=campaign.name, overview=overview(store, campaign), **_role_parts(campaign)
        ),
    )
    for fn in build_tools(ToolContext(store, campaign), writable=not read_only):
        server.add_tool(fn)
    server.run("stdio")


# -- remote ------------------------------------------------------------------------

REMOTE_INSTRUCTIONS = """\
Tools over the user's D&D campaigns: reference material (published modules
and notes, with digested outlines), each campaign's world (people, places,
items, quests and what's true about them session by session), the session
journal, and the party's character sheets.

Start with list_campaigns. Every other tool takes `campaign`, the slug it
returns. Before answering about a campaign, call campaign_overview once: it
says how that campaign's material is organized and, where the user is a
player, what must not be revealed. Spoilers are also filtered on the
server, so in a player's campaign the tools only return what the party
knows.

To enter a character from a paper sheet, a scan or a photo: read the whole
document, then call create_character with every field it shows, and tell
the user anything you couldn't read so they can fix it on the sheet.
"""

CAMPAIGN_ARG = "campaign: The campaign's slug, from list_campaigns."
# Writes anyone in the campaign may make, like making their own sheet in the web UI.
MEMBER_WRITES = {"create_character"}


def _template() -> tuple[list[Callable[..., str]], set[str]]:
    """The campaign tools' names, signatures and docs, from a throwaway in-memory campaign."""
    s = Store(":memory:")
    ctx = ToolContext(s, s.create_campaign("Template", "dm"))
    every = build_tools(ctx, writable=True)
    readers = {f.__name__ for f in build_tools(ctx, writable=False)}
    return every, {f.__name__ for f in every} - readers


def _campaign_for(s: Store, user_id: int | None, slug: str) -> Campaign | None:
    """The campaign as this user sees it (their role and access), if they're in it."""
    if user_id is None:
        return None
    return next((c for c, _ in Accounts(s).campaigns_for(user_id) if c.slug == slug), None)


def campaign_tools(db: Path | str) -> list[tuple[Callable[..., str], bool]]:
    """Each campaign tool as a remote one: a leading `campaign` argument, run as the signed-in user.

    Returns (tool, writes) pairs.
    """
    template, writers = _template()
    out = []
    for tmpl in template:
        name = tmpl.__name__
        writes = name in writers
        hints = typing.get_type_hints(tmpl)
        params = [inspect.Parameter("campaign", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)] + [
            p.replace(annotation=hints.get(p.name, p.annotation)) for p in inspect.signature(tmpl).parameters.values()
        ]

        def tool(campaign: str, __name: str = name, __writes: bool = writes, **kwargs: Any) -> str:
            uid = user_id_of(get_access_token())
            s = Store(db)
            try:
                c = _campaign_for(s, uid, campaign)
                if c is None:
                    return f"No campaign {campaign!r} for you. Call list_campaigns for the slugs."
                if __writes and __name not in MEMBER_WRITES and not at_least(c.access, "editor"):
                    return f"You have {c.access} access to {c.name}, which can't change it."
                fns = {f.__name__: f for f in build_tools(ToolContext(s, c, user_id=uid), writable=True)}
                return fns[__name](**kwargs)
            finally:
                s.close()

        tool.__name__ = name
        tool.__doc__ = (inspect.getdoc(tmpl) or "").rstrip() + f"\n\n{CAMPAIGN_ARG}"
        tool.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
        tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
        out.append((tool, writes))
    return out


def remote_server(db: Path | str, cfg: AuthConfig) -> tuple[MCPServer, Any]:
    """The MCP server and its ASGI app, serving /mcp plus the OAuth endpoints at the site root."""
    provider = Provider(db, cfg)
    server = MCPServer(
        name="campaign",
        instructions=REMOTE_INSTRUCTIONS,
        auth=AuthSettings(
            issuer_url=cfg.public_url,
            resource_server_url=f"{cfg.public_url}/mcp",
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
            validate_token_resource=True,  # refuse tokens issued for anything else
        ),
        auth_server_provider=provider,
    )

    def list_campaigns() -> str:
        """The campaigns you belong to: slug, name, your role (dm or player) and access (owner, editor, viewer)."""
        s = Store(db)
        try:
            uid = user_id_of(get_access_token())
            return json.dumps([{"slug": c.slug, "name": c.name, "role": c.role, "access": access}
                               for c, access in Accounts(s).campaigns_for(uid)] if uid else [])
        finally:
            s.close()

    def campaign_overview(campaign: str) -> str:
        """How to work with one campaign: what's in it, how its material is organized, and its spoiler rules.

        Args:
            campaign: The campaign's slug, from list_campaigns.
        """
        s = Store(db)
        try:
            c = _campaign_for(s, user_id_of(get_access_token()), campaign)
            if c is None:
                return f"No campaign {campaign!r} for you. Call list_campaigns for the slugs."
            return ASK_SYSTEM.format(name=c.name, overview=overview(s, c), **_role_parts(c))
        finally:
            s.close()

    async def ask_assistant(campaign: str, question: str, conversation_id: int | None = None) -> str:
        """Ask the campaign's own assistant, the one in the app's Ask tab, and get its answer.

        It runs the app's chat as you, with the app's prompt and tools, and
        the exchange is saved as one of your conversations there, so you can
        open it in the app. Use this to see how the app answers (testing it,
        comparing answers); to just look something up, the other tools are
        quicker and cheaper. Takes as long as an answer takes in the app,
        often a minute.

        Returns JSON: the conversation_id (pass it back to follow up in the
        same conversation), the answer (Markdown), the lookups it made, and
        usage: model, api_calls, tool_calls, input/cache-read/cache-write/
        output tokens, seconds and cost_usd.

        Args:
            campaign: The campaign's slug, from list_campaigns.
            question: What to ask, as you'd type it in the app.
            conversation_id: Continue this conversation; leave out to start a new one.
        """
        import anyio

        from .agent import chat_turn, describe_tool_call

        uid = user_id_of(get_access_token())

        def run() -> str:
            s = Store(db)
            try:
                c = _campaign_for(s, uid, campaign)
                if c is None:
                    return f"No campaign {campaign!r} for you. Call list_campaigns for the slugs."
                if conversation_id is None:
                    conv = s.create_conversation(c, uid)
                else:
                    found = s.conversation(conversation_id)
                    if found is None or found["campaign_id"] != c.id or found["user_id"] != uid:
                        return f"No conversation {conversation_id} of yours in {c.name}."
                    conv = conversation_id
                lookups: list[str] = []
                answer = chat_turn(
                    s, c, conv, question, user_id=uid, writable=at_least(c.access, "editor"),
                    on_text=lambda t: None,
                    on_tool=lambda n, a: lookups.append(describe_tool_call(n, a).strip(" ·")),
                )
                return json.dumps({"conversation_id": conv, "answer": str(answer), "lookups": lookups,
                                   "usage": answer.usage}, ensure_ascii=False)
            finally:
                s.close()

        return await anyio.to_thread.run_sync(run)

    read_only = ToolAnnotations(readOnlyHint=True)
    server.add_tool(list_campaigns, annotations=read_only)
    server.add_tool(campaign_overview, annotations=read_only)
    # Not read-only: the conversation is saved, and the assistant may record what it's told happened.
    server.add_tool(ask_assistant, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    for tool, writes in campaign_tools(db):
        server.add_tool(tool, annotations=ToolAnnotations(readOnlyHint=not writes, destructiveHint=False))

    host = urlsplit(cfg.public_url).netloc
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[host], allowed_origins=[cfg.origin, "https://claude.ai"],
        ),
    )
    return server, app
