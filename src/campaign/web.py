"""Web UI: a JSON API plus the static single-page app in `static/`.

    campaign web            # http://127.0.0.1:8765

Digests, session ingests and reading sheets from scans take minutes. Asking
for one records a target in the database, and a background loop
(digest.Reconciler for digests, jobs.JobLoop for the rest) carries it out, wherever and whenever a server is running; the page reads
progress back from the database.

Without sign-in (local use), whoever runs the server owns everything. With
it (auth.py), every campaign route checks the user's membership:
- reading needs any membership, and players see the spoiler-filtered world;
- changing content needs editor access;
- managing members and invites needs owner access.
A campaign you don't belong to answers 404, as if it didn't exist.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import digest, encounters, sheetscan
from .accounts import Accounts, at_least
from .auth import AuthConfig
from .auth import install as install_auth
from .mcp_server import remote_server
from .oauth import Provider
from .oauth import install as install_oauth
from .charsheet import Bonus, CharacterFile, CharacterSheet, ItemMechanics, apply_hp, rest
from .db import Campaign, Store
from .inventory import Inventory, InventoryError
from .jobs import JobLoop
from .library import add_reference, reindex
from .world import World

STATIC = Path(__file__).parent / "static"


class NewCampaign(BaseModel):
    name: str
    role: str = "dm"
    notes: str = ""


class NewCharacter(BaseModel):
    name: str
    campaign: str


class CopyCharacter(BaseModel):
    campaign: str


class ChatMessage(BaseModel):
    text: str


class SourceUpdate(BaseModel):
    dm_only: bool | None = None


class ChatTitle(BaseModel):
    title: str = Field(min_length=1)


class SheetUpdate(BaseModel):
    sheet: dict[str, Any]
    reason: str = "edited"


class HpChange(BaseModel):
    delta: int


class RestRequest(BaseModel):
    long: bool = True


# The party's inventory. A holder is a world entity's id (a character's, or an NPC's); None is the party.

class NewHolding(BaseModel):
    name: str
    qty: int = Field(1, ge=1)
    notes: str = ""
    value_gp: float = Field(0, ge=0)
    holder: int | None = None


class HoldingChange(BaseModel):
    name: str | None = None
    qty: int | None = Field(None, ge=0)  # 0 removes it
    notes: str | None = None
    value_gp: float | None = Field(None, ge=0)
    equipped: bool | None = None
    attuned: bool | None = None


class HoldingMove(BaseModel):
    to: int | None = None
    to_name: str | None = None  # someone else in the world, by name (added as an NPC if new)
    qty: int | None = None  # all of it if not given


class CoinChange(BaseModel):
    holder: int | None = None
    currency: dict[str, int]  # gains positive, spending negative


class CoinGive(BaseModel):
    holder: int | None = None
    to: int | None = None
    to_name: str | None = None
    currency: dict[str, int]


class Definition(BaseModel):
    name: str | None = None  # the world item's name, for a holding that has none yet; default its own
    requires_attunement: bool = False
    bonuses: list[Bonus] = []
    value_gp: float = Field(0, ge=0)
    party_knows: bool = True  # False: the DM's secret until the party identifies it


class NewFact(BaseModel):
    statement: str
    attribute: str | None = None
    value: str | None = None
    party_knows: bool = True
    modality: str = "fact"


class MergeRequest(BaseModel):
    other_id: int


class MemberUpdate(BaseModel):
    role: str | None = None
    access: str | None = None


class NewInvite(BaseModel):
    role: str = "player"
    access: str = "viewer"
    email: str | None = None
    days: int = 7
    max_uses: int = 1


class AdminUpdate(BaseModel):
    is_admin: bool


def create_app(
    db: Path | str, auth: AuthConfig | None = None, exchange: Callable | None = None,
    digest_client: Callable[[], Any] | None = None, shutting_down: threading.Event | None = None,
) -> FastAPI:
    """`exchange` replaces the call to Google's token endpoint, `digest_client` the Claude client (for tests).

    `shutting_down` is set when the server has been told to stop, so live
    streams end at once (and browsers reconnect to the next server) instead
    of holding a deploy up.
    """
    shutting_down = shutting_down or threading.Event()
    # Migrate now, once, before anything else (the digest loop, the first
    # requests) opens the database and races to create the schema.
    Store(db).close()
    # With sign-in on, the remote MCP endpoint (and its OAuth) is served too.
    mcp = remote_server(db, auth, wake_digest=lambda: reconciler.wake()) if auth is not None else None
    # Digests run in the background for as long as the server does, toward
    # whatever the sources' targets say (see digest.Reconciler).
    reconciler = digest.Reconciler(db, **({"client_factory": digest_client} if digest_client else {}))
    job_loop = JobLoop(db)  # likewise session ingests and sheet scans (jobs.py)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        reconciler.start()
        job_loop.start()
        try:
            if mcp is None:
                yield
            else:
                async with mcp[0].session_manager.run():
                    yield
        finally:
            shutting_down.set()
            reconciler.stop()
            job_loop.stop()

    # No interactive API docs: they'd be one more surface to protect.
    app = FastAPI(title="campaign", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    if auth is not None:
        install_auth(app, auth, db, **({"exchange": exchange} if exchange else {}))
        install_oauth(app, Provider(db, auth))
    local = auth is None
    # Cost estimates for sources not yet fully digested. Planning re-reads the
    # whole source, so it's done once, not on every poll; asking for a digest
    # (or removing the source) forgets it.
    estimates: dict[int, dict[str, Any]] = {}

    def store() -> Iterator[Store]:
        # One connection per request: sqlite connections shouldn't be shared
        # between the request threads and the background job threads.
        s = Store(db)
        try:
            yield s
        finally:
            s.close()

    def user_of(request: Request) -> dict[str, Any] | None:
        return getattr(request.state, "user", None)

    # -- who may do what -------------------------------------------------------

    def access(needed: str) -> Callable[..., Campaign]:
        """A dependency giving the campaign as the user sees it, if they have `needed` access."""

        def dependency(slug: str, request: Request, s: Store = Depends(store)) -> Campaign:
            try:
                campaign = s.campaign(slug)
            except KeyError:
                raise HTTPException(404, f"no campaign {slug!r}")
            if local:
                return campaign
            m = Accounts(s).membership(campaign.id, user_of(request)["id"])
            if m is None:
                raise HTTPException(404, f"no campaign {slug!r}")
            if not at_least(m["access"], needed):
                raise HTTPException(403, f"this needs {needed} access to {campaign.name}")
            return replace(campaign, role=m["role"], access=m["access"])

        return dependency

    reader, editor, owner = access("viewer"), access("editor"), access("owner")

    def signed_in(request: Request) -> dict[str, Any]:
        if local:
            raise HTTPException(404, "sign-in is off, so there are no members or invites")
        return user_of(request)

    def admin(request: Request) -> dict[str, Any] | None:
        if local:
            return None
        user = user_of(request)
        if not user["is_admin"]:
            raise HTTPException(403, "admins only")
        return user

    # -- account ---------------------------------------------------------------

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        if local:
            return {"local": True}
        user = user_of(request)
        return {"id": user["id"], "email": user["email"], "is_admin": bool(user["is_admin"])}

    @app.get("/api/me/apps")
    def connected_apps(request: Request, s: Store = Depends(store)) -> list[dict[str, Any]]:
        """Apps (like the Claude app) you've connected to your account over MCP."""
        return s.oauth_sessions(signed_in(request)["id"])

    @app.delete("/api/me/apps/{client_id}", status_code=204)
    def disconnect_app(client_id: str, request: Request, s: Store = Depends(store)) -> None:
        if not s.revoke_oauth_client(signed_in(request)["id"], client_id):
            raise HTTPException(404, "no such connected app")

    @app.get("/api/info")
    def info(s: Store = Depends(store), _: Any = Depends(admin)) -> dict[str, Any]:
        return {
            "data_dir": str(Path(s.path).parent),
            "database": s.path,
            "schema_version": s.schema_version,
            "originals": s.blobs.stats(),
            "digest_model": digest.MODEL,
        }

    # -- campaigns -------------------------------------------------------------

    @app.get("/api/campaigns")
    def list_campaigns(request: Request, s: Store = Depends(store)) -> list[dict[str, Any]]:
        if local:
            return [vars(c) for c in s.campaigns()]
        return [vars(c) for c, _ in Accounts(s).campaigns_for(user_of(request)["id"])]

    @app.post("/api/campaigns", status_code=201)
    def new_campaign(body: NewCampaign, request: Request, s: Store = Depends(store)) -> dict[str, Any]:
        if body.role not in ("dm", "player"):
            raise HTTPException(422, "role must be dm or player")
        if not body.name.strip():
            raise HTTPException(422, "name is required")
        c = s.create_campaign(body.name.strip(), body.role, body.notes)
        if not local:
            Accounts(s).add_member(c.id, user_of(request)["id"], body.role, "owner")
        return vars(c)

    # -- members and invites (sign-in only) ---------------------------------------

    @app.get("/api/campaigns/{slug}/members")
    def members(
        c: Campaign = Depends(reader), user: dict = Depends(signed_in), s: Store = Depends(store),
    ) -> dict[str, Any]:
        mine = Accounts(s).membership(c.id, user["id"])
        return {"members": Accounts(s).members(c.id), "me": user["id"], "can_manage": mine["access"] == "owner"}

    @app.patch("/api/campaigns/{slug}/members/{user_id}")
    def update_member(
        user_id: int, body: MemberUpdate, c: Campaign = Depends(owner), _: dict = Depends(signed_in),
        s: Store = Depends(store),
    ) -> list[dict[str, Any]]:
        if body.role not in (None, "dm", "player") or body.access not in (None, "owner", "editor", "viewer"):
            raise HTTPException(422, "role is dm or player; access is owner, editor or viewer")
        try:
            Accounts(s).update_member(c.id, user_id, body.role, body.access)
        except KeyError:
            raise HTTPException(404, "not a member")
        except ValueError as err:
            raise HTTPException(409, str(err))
        return Accounts(s).members(c.id)

    @app.delete("/api/campaigns/{slug}/members/{user_id}", status_code=204)
    def remove_member(
        user_id: int, c: Campaign = Depends(reader), user: dict = Depends(signed_in), s: Store = Depends(store),
    ) -> None:
        acc = Accounts(s)
        # Anyone may leave; only owners may remove others.
        if user_id != user["id"] and acc.membership(c.id, user["id"])["access"] != "owner":
            raise HTTPException(403, "only owners can remove members")
        try:
            if not acc.remove_member(c.id, user_id):
                raise HTTPException(404, "not a member")
        except ValueError as err:
            raise HTTPException(409, str(err))

    def invite_url(token: str) -> str:
        return f"{auth.public_url}/invite/{token}"

    @app.get("/api/campaigns/{slug}/invites")
    def invites(c: Campaign = Depends(owner), _: dict = Depends(signed_in), s: Store = Depends(store)) -> list[dict]:
        return Accounts(s).pending_invites(c.id)

    @app.post("/api/campaigns/{slug}/invites", status_code=201)
    def create_invite(
        body: NewInvite, c: Campaign = Depends(owner), user: dict = Depends(signed_in), s: Store = Depends(store),
    ) -> dict[str, Any]:
        try:
            invite, token = Accounts(s).create_invite(
                c.id, body.role, body.access, email=body.email or None, created_by=user["id"],
                days=body.days, max_uses=body.max_uses,
            )
        except ValueError as err:
            raise HTTPException(422, str(err))
        return {"invite": invite, "url": invite_url(token)}

    @app.delete("/api/campaigns/{slug}/invites/{invite_id}", status_code=204)
    def revoke_invite(
        invite_id: int, c: Campaign = Depends(owner), _: dict = Depends(signed_in), s: Store = Depends(store),
    ) -> None:
        if not Accounts(s).revoke_invite(invite_id, c.id):
            raise HTTPException(404, "no such invite")

    # -- admin (sign-in only) -------------------------------------------------------

    @app.get("/api/admin/users")
    def admin_users(_: dict = Depends(signed_in), me: Any = Depends(admin), s: Store = Depends(store)) -> list[dict]:
        return Accounts(s).users()

    @app.patch("/api/admin/users/{user_id}")
    def admin_set(
        user_id: int, body: AdminUpdate, _: dict = Depends(signed_in), me: Any = Depends(admin),
        s: Store = Depends(store),
    ) -> list[dict]:
        if user_id == me["id"] and not body.is_admin:
            raise HTTPException(409, "you can't remove your own admin rights")
        Accounts(s).set_admin(user_id, body.is_admin)
        return Accounts(s).users()

    @app.delete("/api/admin/users/{user_id}", status_code=204)
    def admin_remove(user_id: int, _: dict = Depends(signed_in), me: Any = Depends(admin), s: Store = Depends(store)) -> None:
        if user_id == me["id"]:
            raise HTTPException(409, "you can't remove yourself")
        if not Accounts(s).delete_user(user_id):
            raise HTTPException(404, "no such user")

    @app.get("/api/admin/invites")
    def admin_invites(_: dict = Depends(signed_in), me: Any = Depends(admin), s: Store = Depends(store)) -> list[dict]:
        return Accounts(s).pending_invites(None)

    @app.post("/api/admin/invites", status_code=201)
    def admin_invite(
        body: NewInvite, _: dict = Depends(signed_in), me: Any = Depends(admin), s: Store = Depends(store),
    ) -> dict[str, Any]:
        try:
            invite, token = Accounts(s).create_invite(
                None, email=body.email or None, created_by=me["id"], days=body.days, max_uses=body.max_uses,
            )
        except ValueError as err:
            raise HTTPException(422, str(err))
        return {"invite": invite, "url": invite_url(token)}

    @app.delete("/api/admin/invites/{invite_id}", status_code=204)
    def admin_revoke(invite_id: int, _: dict = Depends(signed_in), me: Any = Depends(admin), s: Store = Depends(store)) -> None:
        if not Accounts(s).revoke_invite(invite_id, None):
            raise HTTPException(404, "no such invite")

    # -- reference material ------------------------------------------------------

    def want_digest(s: Store, source_id: int) -> None:
        """Set the source's target to the configured model, give failed sections another go, and nudge the loop.

        Idempotent: asking twice is the same as asking once.
        """
        digest.request(s, source_id)
        estimates.pop(source_id, None)
        reconciler.wake()

    def visible_source(s: Store, c: Campaign, source_id: int, manage: bool = False) -> dict[str, Any]:
        """A source of this campaign. Players can't read DM-only ones, but their editors can still manage them."""
        src = s.source(source_id)
        hidden = c.spoiler_safe and src and src["dm_only"] and not (manage and at_least(c.access, "editor"))
        if src is None or src["campaign_id"] != c.id or hidden:
            raise HTTPException(404, "no such source")
        return src

    @app.get("/api/campaigns/{slug}/sources")
    def sources(c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict[str, Any]]:
        return list_sources(s, c)

    def list_sources(s: Store, c: Campaign) -> list[dict[str, Any]]:
        """The campaign's sources with their digest status, as this viewer may see them."""
        out = []
        for src in s.sources(c):
            # Players see DM-only material listed (so editors can manage it) but never its contents.
            if c.spoiler_safe and src["dm_only"] and not at_least(c.access, "editor"):
                continue
            target = src["digest_model"]
            status = s.digest_status(src["id"])
            remaining = status["total"] - status["done"]
            # Everything left has failed (or waits on something that has): nothing more will happen on its own.
            stuck = bool(target) and remaining > 0 and status["failed"] >= remaining
            running = bool(target) and (remaining > 0 or status["total"] == 0) and not stuck
            model = target or digest.MODEL
            stale = src["id"] not in estimates or estimates[src["id"]].get("model") != model
            if src["kind"] != "transcript" and not running and stale:
                # Planned against the model a digest would use (without saving), so it shows what it would do.
                estimates[src["id"]] = digest.estimate(
                    s, digest.plan(s, src["id"], model=model, save=False), model=model)
            status |= {"running": running, "stuck": stuck, "target": target, "model": target or digest.MODEL,
                       "spent_usd": digest.spent(s.digest_usage(src["id"]))}
            if not running and remaining > 0 and src["id"] in estimates:
                status["estimate"] = estimates[src["id"]]
            out.append(src | {"digest": status})
        return out

    @app.patch("/api/campaigns/{slug}/sources/{source_id}")
    def update_source(source_id: int, body: SourceUpdate, c: Campaign = Depends(editor),
                      s: Store = Depends(store)) -> dict[str, Any]:
        """Change who can see a source: `dm_only` hides it, and what the world took from it, from players."""
        src = s.source(source_id)
        if src is None or src["campaign_id"] != c.id or (c.spoiler_safe and src["dm_only"]):
            raise HTTPException(404, "no such source")
        if body.dm_only is not None:
            s.set_source_dm_only(source_id, body.dm_only)
        return {"id": source_id, "dm_only": bool(s.source(source_id)["dm_only"])}

    @app.post("/api/campaigns/{slug}/sources", status_code=201)
    def add_source(
        file: UploadFile = File(...),
        title: str = Form(""),
        kind: str = Form("module"),
        dm_only: bool | None = Form(None),
        digest_now: bool = Form(False),
        c: Campaign = Depends(editor),
        s: Store = Depends(store),
    ) -> dict[str, Any]:
        if kind not in ("module", "notes"):
            raise HTTPException(422, "kind must be module or notes")
        try:
            result = add_reference(
                s, c, file.file.read(), file.filename or "upload", title.strip() or None, kind, dm_only
            )
        except ValueError as err:
            raise HTTPException(422, str(err))
        except Exception as err:
            raise HTTPException(422, f"couldn't read {file.filename}: {err}")
        estimates[result["id"]] = result["estimate"]
        result["digesting"] = bool(digest_now and result["pending"])
        if result["digesting"]:
            want_digest(s, result["id"])
        return result

    @app.post("/api/campaigns/{slug}/sources/{source_id}/digest", status_code=202)
    def run_digest(source_id: int, c: Campaign = Depends(editor), s: Store = Depends(store)) -> dict[str, Any]:
        """Ask for the source to be digested (and failed sections retried). Safe to repeat."""
        visible_source(s, c, source_id, manage=True)
        want_digest(s, source_id)
        return {"digesting": True}

    @app.delete("/api/campaigns/{slug}/sources/{source_id}/digest", status_code=204)
    def stop_digest(source_id: int, c: Campaign = Depends(editor), s: Store = Depends(store)) -> None:
        """Stop asking: the loop finishes the calls in flight (they're kept) and starts no more."""
        visible_source(s, c, source_id, manage=True)
        s.set_digest_target(source_id, None)
        estimates.pop(source_id, None)

    @app.get("/api/campaigns/{slug}/sources/{source_id}/outline")
    def source_outline(source_id: int, c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict]:
        visible_source(s, c, source_id)
        return s.digest_outline(source_id)

    @app.get("/api/campaigns/{slug}/sources/{source_id}/outline/{node_id}")
    def outline_node(
        source_id: int, node_id: int, c: Campaign = Depends(reader), s: Store = Depends(store),
    ) -> dict[str, Any]:
        """One section's digest: its summary and the entities found in it, before any merging above."""
        visible_source(s, c, source_id)
        found = s.digest_node_output(node_id)
        if found is None or found[0] != source_id:
            raise HTTPException(404, "no such section")
        return found[1] or {"summary": None, "entities": []}

    @app.get("/api/campaigns/{slug}/sources/{source_id}/entities")
    def source_entities(
        source_id: int, q: str | None = None, kind: str | None = None,
        c: Campaign = Depends(reader), s: Store = Depends(store),
    ) -> list[dict[str, Any]]:
        visible_source(s, c, source_id)
        needle = (q or "").lower()
        return [
            e for e in s.source_catalog(source_id)
            if (not kind or e["kind"] == kind)
            and (not needle or any(needle in x.lower() for x in [e["name"], *e["aliases"], e["summary"]]))
        ]

    @app.get("/api/campaigns/{slug}/sources/{source_id}/original")
    def source_original(source_id: int, c: Campaign = Depends(reader), s: Store = Depends(store)) -> FileResponse:
        src = visible_source(s, c, source_id)
        if not src["blob"] or not s.blobs.exists(src["blob"]):
            raise HTTPException(404, "the original file wasn't kept for this source; upload it again")
        return FileResponse(s.blobs.path(src["blob"]), filename=src["filename"] or f"{src['title']}")

    @app.delete("/api/campaigns/{slug}/sources/{source_id}", status_code=204)
    def delete_source(source_id: int, c: Campaign = Depends(editor), s: Store = Depends(store)) -> None:
        visible_source(s, c, source_id, manage=True)
        s.delete_source(c, source_id)
        estimates.pop(source_id, None)

    @app.get("/api/campaigns/{slug}/search")
    def search(q: str, c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict[str, Any]]:
        return s.search(c, q, limit=20)

    @app.get("/api/campaigns/{slug}/chunks/{chunk_id}")
    def chunk(chunk_id: int, c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict[str, Any]]:
        return s.read_chunks(c, chunk_id, after=1)

    # -- sessions and the world ------------------------------------------------------

    @app.get("/api/campaigns/{slug}/sessions")
    def sessions(c: Campaign = Depends(reader), s: Store = Depends(store)) -> dict[str, Any]:
        # Recent ingests that are waiting, running or need attention (finished ones show as sessions).
        active = [job_view(j) for j in s.jobs(c, "ingest") if j["state"] in ("wanted", "running", "failed")]
        return {"sessions": s.sessions(c), "next": s.next_ingest_session(c), "ingests": active}

    @app.post("/api/campaigns/{slug}/sessions/{number}/revert")
    def revert(number: int, c: Campaign = Depends(editor), s: Store = Depends(store)) -> dict[str, int]:
        try:
            return World(s, c).revert_session(number)
        except KeyError:
            raise HTTPException(404, f"no session {number}")

    @app.get("/api/campaigns/{slug}/entities")
    def entities(
        kind: str | None = None, q: str | None = None, in_play: bool = False,
        c: Campaign = Depends(reader), s: Store = Depends(store),
    ) -> dict[str, Any]:
        w = World(s, c)
        return {"now": w.now, "entities": w.list(kind=kind or None, q=q or None, in_play=in_play)}

    def visible_entity(w: World, entity_id: int) -> dict[str, Any]:
        view = w.view(entity_id)
        if view is None:
            raise HTTPException(404, "no such entity")
        return view

    @app.get("/api/campaigns/{slug}/entities/{entity_id}")
    def entity(entity_id: int, c: Campaign = Depends(reader), s: Store = Depends(store)) -> dict[str, Any]:
        w = World(s, c)
        view = visible_entity(w, entity_id)
        # All facts, so the page can show the world as of any session without asking again.
        view["all_facts"] = w.facts(entity_id)
        view["timeline"] = w.timeline(entity_id)
        view["all_events"] = w.events(entity_id=entity_id)
        view["character"] = s.character_for_entity(entity_id)
        return view

    @app.post("/api/campaigns/{slug}/entities/{entity_id}/facts", status_code=201)
    def add_fact(entity_id: int, body: NewFact, c: Campaign = Depends(editor), s: Store = Depends(store)) -> dict:
        w = World(s, c)
        visible_entity(w, entity_id)
        if not body.statement.strip() or body.modality not in ("fact", "rumor", "possible"):
            raise HTTPException(422, "a statement is required; modality is fact, rumor or possible")
        run = w.start_run("manual", session=w.now, label="edited in the web UI")
        return w.record(entity_id, body.statement, run, w.now, attribute=body.attribute or None,
                        value=body.value or None, modality=body.modality, party_knows=body.party_knows)

    @app.post("/api/campaigns/{slug}/facts/{fact_id}/end")
    def end_fact(fact_id: int, c: Campaign = Depends(editor), s: Store = Depends(store)) -> dict[str, bool]:
        w = World(s, c)
        fact = s.conn.execute("SELECT entity_id FROM facts WHERE id = ? AND campaign_id = ?", (fact_id, c.id)).fetchone()
        if fact is None or not any(f["id"] == fact_id for f in w.facts(fact["entity_id"])):
            raise HTTPException(404, "no open fact with that id")
        run = w.start_run("manual", session=w.now, label="edited in the web UI")
        if not w.end_fact(fact_id, run, w.now):
            raise HTTPException(404, "no open fact with that id")
        return {"ended": True}

    @app.post("/api/campaigns/{slug}/entities/{entity_id}/merge")
    def merge(entity_id: int, body: MergeRequest, c: Campaign = Depends(editor), s: Store = Depends(store)) -> dict:
        w = World(s, c)
        visible_entity(w, entity_id)
        visible_entity(w, body.other_id)
        return w.merge(entity_id, body.other_id)

    @app.post("/api/campaigns/{slug}/entities/{entity_id}/summary")
    def summarize(entity_id: int, c: Campaign = Depends(editor), s: Store = Depends(store)) -> dict[str, str]:
        w = World(s, c)
        visible_entity(w, entity_id)
        try:
            return {"summary": w.synthesize(entity_id)}
        except Exception as err:
            raise HTTPException(502, f"couldn't write a summary: {err}")

    @app.post("/api/campaigns/{slug}/encounter")
    def encounter(spec: encounters.Spec, c: Campaign = Depends(reader), s: Store = Depends(store)) -> dict[str, Any]:
        """A fight as the encounter card shows it: each creature's stat block, XP and the difficulty."""
        return encounters.build(s, c, spec)

    @app.get("/api/campaigns/{slug}/entities/{entity_id}/passage")
    def entity_passage(entity_id: int, c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict[str, Any]]:
        """The book text an entity's stat block was read from, to check it against."""
        return encounters.book_passage(s, c, entity_id)

    @app.get("/api/campaigns/{slug}/journal")
    def journal(session: int | None = None, c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict]:
        return World(s, c).events(session=session)

    # -- jobs ----------------------------------------------------------------------
    # Session ingests and sheet scans are jobs: a request recorded in the
    # database, worked through by the job loop in the background (jobs.py).

    def job_view(job: dict[str, Any]) -> dict[str, Any]:
        keep = ("id", "kind", "state", "attempts", "retry_at", "error", "result", "created_at")
        return {k: job[k] for k in keep} | {"name": job["input"].get("name"), "session": job["input"].get("session")}

    def campaign_job(s: Store, c: Campaign, job_id: int) -> dict[str, Any]:
        job = s.job(job_id)
        if job is None or job["campaign_id"] != c.id:
            raise HTTPException(404, "no such job")
        return job

    def job_to_change(request: Request, s: Store, c: Campaign, job_id: int) -> dict[str, Any]:
        """A job you may retry or cancel: any, as an editor; else only the ones you asked for."""
        job = campaign_job(s, c, job_id)
        if not at_least(c.access, "editor") and not (local or job["requested_by"] == user_of(request)["id"]):
            raise HTTPException(403, "only editors can change other people's jobs")
        return job

    def add_job(request: Request, s: Store, c: Campaign, kind: str, input: dict[str, Any]) -> int:
        job_id = s.add_job(c, kind, input, None if local else user_of(request)["id"])
        job_loop.wake()
        return job_id

    @app.post("/api/campaigns/{slug}/ingest", status_code=202)
    def ingest(
        request: Request,
        file: UploadFile | None = File(None),
        text: str = Form(""),
        number: int | None = Form(None),
        played_on: str = Form(""),
        c: Campaign = Depends(editor),
        s: Store = Depends(store),
    ) -> dict[str, Any]:
        """Session N should be what this transcript (or recording) says."""
        if file is not None and file.filename:
            name, data = file.filename, file.file.read()
        elif text.strip():
            name, data = "pasted.txt", text.encode("utf-8")
        else:
            raise HTTPException(422, "paste a transcript or upload a file")
        session = number or s.next_ingest_session(c)
        job_id = add_job(request, s, c, "ingest", {
            "session": session, "blob": s.blobs.put(data), "name": name, "played_on": played_on or None,
        })
        return {"job": job_id, "session": session}

    @app.post("/api/campaigns/{slug}/characters/from-scan", status_code=202)
    def character_from_scan(
        request: Request, file: UploadFile = File(...), c: Campaign = Depends(reader), s: Store = Depends(store),
    ) -> dict[str, Any]:
        """Make a character from a scan or PDF of a paper sheet. Claude reads it in the background."""
        data, name = file.file.read(sheetscan.MAX_BYTES + 1), file.filename or ""
        try:
            sheetscan.check_upload(data, name)
        except sheetscan.ScanError as err:
            raise HTTPException(422, str(err))
        return {"job": add_job(request, s, c, "scan", {"blob": s.blobs.put(data), "name": name})}

    @app.get("/api/campaigns/{slug}/jobs")
    def list_jobs(kind: str | None = None, c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict]:
        return [job_view(j) for j in s.jobs(c, kind)]

    @app.get("/api/campaigns/{slug}/jobs/{job_id}")
    def job_status(job_id: int, since: int = 0, c: Campaign = Depends(reader), s: Store = Depends(store)) -> dict:
        """A job's state and its progress log from entry `since` (the last entry is resent: text lines grow)."""
        job = campaign_job(s, c, job_id)
        start = max(0, since - 1)
        return job_view(job) | {"log": s.job_log(job_id, since=start), "from": start}

    @app.post("/api/campaigns/{slug}/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: int, request: Request, c: Campaign = Depends(reader), s: Store = Depends(store)) -> dict:
        job = job_to_change(request, s, c, job_id)
        if job["state"] not in ("failed", "cancelled", "wanted"):
            raise HTTPException(409, f"this job is {job['state']}")
        s.update_job(job_id, state="wanted", attempts=0, retry_at=None, error=None)
        job_loop.wake()
        return job_view(s.job(job_id))

    @app.delete("/api/campaigns/{slug}/jobs/{job_id}", status_code=204)
    def cancel_job(job_id: int, request: Request, c: Campaign = Depends(reader), s: Store = Depends(store)) -> None:
        """Stop wanting it. A running one stops at its next step and undoes what it wrote."""
        job = job_to_change(request, s, c, job_id)
        if job["state"] in ("wanted", "running"):
            s.update_job(job_id, state="cancelled")

    # -- live updates ----------------------------------------------------------------
    # One Server-Sent Events stream per open campaign page. It reads the
    # database (the only state there is) every second and sends what changed:
    # the sources' digest status, and each recent job's state and new log
    # lines. A browser's EventSource reconnects by itself, so when a deploy
    # ends a stream the page carries on against the next server.

    def sse(event: str, data: Any) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    @app.get("/api/campaigns/{slug}/events")
    async def events(request: Request, c: Campaign = Depends(reader), seconds: float = 300) -> StreamingResponse:
        """Live campaign updates. Each stream ends after `seconds` (the browser then reconnects)."""
        def snapshot(sent: dict[int, tuple[str, int, int, str]]) -> list[str]:
            """Messages for what changed since the last snapshot. `sent` remembers, per job,
            the state, the attempt, and the last log line (seq and text) this stream has sent."""
            s = Store(db)
            try:
                out = [sse("sources", list_sources(s, c))]
                for job in s.jobs(c, limit=10):
                    state, attempt, seq, text = sent.get(job["id"], (None, -1, -1, ""))
                    if job["attempts"] != attempt:  # a new attempt starts its log over
                        seq, text = -1, ""
                    # From the last line sent: streamed text grows in place.
                    fresh = [e for e in s.job_log(job["id"], since=max(seq, 0))
                             if e["seq"] > seq or e["text"] != text]
                    if job["state"] == state and job["attempts"] == attempt and not fresh:
                        continue
                    out.append(sse("job", job_view(job) | {"log": fresh}))
                    last = fresh[-1] if fresh else {"seq": seq, "text": text}
                    sent[job["id"]] = (job["state"], job["attempts"], last["seq"], last["text"])
                return out
            finally:
                s.close()

        async def stream() -> AsyncIterator[str]:
            sent: dict[int, tuple[str, int, int, str]] = {}
            last_sources = None
            deadline = time.monotonic() + seconds
            yield "retry: 2000\n\n"  # how soon the browser reconnects after a stream ends
            while True:
                for message in await asyncio.to_thread(snapshot, sent):
                    if message.startswith("event: sources"):
                        if message == last_sources:
                            continue
                        last_sources = message
                    yield message
                if time.monotonic() >= deadline or shutting_down.is_set() or await request.is_disconnected():
                    return
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    # -- chat ------------------------------------------------------------------------
    # Conversations with the campaign assistant, stored so any server can carry
    # one on. Each is private to the person having it.

    def my_conversation(s: Store, request: Request, c: Campaign, conversation_id: int) -> dict[str, Any]:
        conv = s.conversation(conversation_id)
        user = None if local else user_of(request)["id"]
        if conv is None or conv["campaign_id"] != c.id or conv["user_id"] != user:
            raise HTTPException(404, "no such conversation")
        return conv

    @app.get("/api/campaigns/{slug}/chats")
    def chats(request: Request, c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict[str, Any]]:
        return s.conversations(c, None if local else user_of(request)["id"])

    @app.post("/api/campaigns/{slug}/chats", status_code=201)
    def new_chat(request: Request, c: Campaign = Depends(reader), s: Store = Depends(store)) -> dict[str, Any]:
        return s.conversation(s.create_conversation(c, None if local else user_of(request)["id"]))

    @app.get("/api/campaigns/{slug}/chats/{conversation_id}")
    def chat_history(conversation_id: int, request: Request, c: Campaign = Depends(reader),
                     s: Store = Depends(store)) -> dict[str, Any]:
        """The conversation as the page shows it: what each side said, and the tools the assistant used."""
        from .agent import describe_tool_call

        conv = my_conversation(s, request, c, conversation_id)
        turns: list[dict[str, Any]] = []
        for m in s.conversation_messages(conversation_id, with_usage=True):
            blocks = [{"type": "text", "text": m["content"]}] if isinstance(m["content"], str) else m["content"]
            for b in blocks:
                if b.get("type") == "text" and b.get("text", "").strip():
                    turns.append({"role": m["role"], "kind": "text", "text": b["text"]})
                elif b.get("type") == "tool_use":
                    turns.append({"role": "assistant", "kind": "tool",
                                  "text": describe_tool_call(b["name"], b.get("input", {})).strip(" ·")})
            if m.get("usage") and turns and turns[-1]["role"] == "assistant":
                turns[-1]["usage"] = m["usage"]  # what the answer this message ends cost
        return conv | {"turns": turns}

    @app.patch("/api/campaigns/{slug}/chats/{conversation_id}")
    def rename_chat(conversation_id: int, body: ChatTitle, request: Request, c: Campaign = Depends(reader),
                    s: Store = Depends(store)) -> dict[str, Any]:
        my_conversation(s, request, c, conversation_id)
        s.set_conversation_title(conversation_id, body.title.strip()[:120])
        return s.conversation(conversation_id)

    @app.delete("/api/campaigns/{slug}/chats/{conversation_id}", status_code=204)
    def delete_chat(conversation_id: int, request: Request, c: Campaign = Depends(reader), s: Store = Depends(store)) -> None:
        my_conversation(s, request, c, conversation_id)
        s.delete_conversation(conversation_id)

    @app.post("/api/campaigns/{slug}/chats/{conversation_id}/messages")
    async def chat_message(conversation_id: int, body: ChatMessage, request: Request,
                           c: Campaign = Depends(reader)) -> StreamingResponse:
        """Ask the assistant; the answer streams back as Server-Sent Events.

        Events: `text` (a piece of the answer), `tool` (a tool it's using),
        `done` (with what the answer cost: tokens, calls, seconds, dollars), `error`. The turn runs to the end and is saved even if the
        page goes away mid-answer.
        """
        from .agent import chat_turn, describe_tool_call

        question = body.text.strip()
        if not question:
            raise HTTPException(422, "say something")
        s = Store(db)
        try:
            my_conversation(s, request, c, conversation_id)
        finally:
            s.close()
        user = None if local else user_of(request)["id"]
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        put = lambda item: loop.call_soon_threadsafe(queue.put_nowait, item)  # noqa: E731

        def run() -> None:
            ts = Store(db)
            try:
                answer = chat_turn(
                    ts, c, conversation_id, question, user_id=user, writable=at_least(c.access, "editor"),
                    on_text=lambda t: put(sse("text", t)),
                    on_tool=lambda n, a: put(sse("tool", describe_tool_call(n, a).strip(" ·"))),
                )
                put(sse("done", {"usage": answer.usage}))
            except Exception as err:
                put(sse("error", str(err) or type(err).__name__))
            finally:
                ts.close()
                put(None)

        threading.Thread(target=run, name=f"chat-{conversation_id}", daemon=True).start()

        async def stream() -> AsyncIterator[str]:
            while (item := await queue.get()) is not None:
                yield item

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    # -- characters ----------------------------------------------------------------
    # A sheet belongs to one campaign and to the person who made it. Its owner
    # can edit it; so can the campaign's DMs who have editor access. Anyone in
    # the campaign can read it. Taking a character to another campaign copies it.

    def character_rights(s: Store, request: Request, ch: dict[str, Any]) -> tuple[bool, bool]:
        """(can read, can edit) for the current user."""
        if local:
            return True, True
        user = user_of(request)
        m = Accounts(s).membership(ch["campaign_id"], user["id"])
        mine = ch["owner_id"] == user["id"]
        return mine or m is not None, mine or bool(m and m["role"] == "dm" and at_least(m["access"], "editor"))

    def character_or_404(s: Store, request: Request, character_id: int, write: bool = False) -> dict[str, Any]:
        ch = s.character(character_id)
        can_read, can_edit = character_rights(s, request, ch) if ch else (False, False)
        if not can_read:
            raise HTTPException(404, "no such character")
        if write and not can_edit:
            raise HTTPException(403, "only the character's owner or the campaign's DM can change it")
        return ch | {"can_edit": can_edit}

    def campaign_label(s: Store, campaign_id: int) -> dict[str, str]:
        c = s.campaign_by_id(campaign_id)
        return {"slug": c.slug, "name": c.name}

    def viewer(s: Store, request: Request, campaign_id: int) -> Campaign:
        """A campaign as the current user sees it; someone no longer in it sees it as a player."""
        c = s.campaign_by_id(campaign_id)
        if local:
            return c
        m = Accounts(s).membership(campaign_id, user_of(request)["id"])
        return replace(c, role=m["role"] if m else "player", access=m["access"] if m else "viewer")

    def inventory_of(s: Store, request: Request, campaign_id: int) -> Inventory:
        return Inventory(s, viewer(s, request, campaign_id), user_id(request))

    def sheet_payload(s: Store, request: Request, ch: dict[str, Any]) -> dict[str, Any]:
        inv = inventory_of(s, request, ch["campaign_id"])
        sheet = inv.sheet(ch)
        holder = inv.character_holder(ch["id"])
        origin = s.character(ch["copied_from"]) if ch["copied_from"] else None
        copied_from = None
        if origin:
            copied_from = {"name": origin["name"], "campaign": campaign_label(s, origin["campaign_id"]),
                           # Linked only for someone who can still open the original.
                           "id": origin["id"] if character_rights(s, request, origin)[0] else None}
        return {
            "id": ch["id"], "campaign": campaign_label(s, ch["campaign_id"]), "can_edit": ch["can_edit"],
            "entity_id": s.link_character(ch["id"]), "copied_from": copied_from,
            "sheet": sheet.model_dump(), "derived": sheet.derived(), "log": s.character_log(ch["id"], limit=30),
            "inventory": {"holder": holder, "items": inv.holdings(holder), "purse": inv.purse(holder),
                          "log": inv.log(holder, everyone=False, limit=30)},
            "files": [{"id": f["id"], "name": f["name"], "created_at": f["created_at"],
                       "url": f"/api/characters/{ch['id']}/files/{f['id']}"} for f in s.character_files(ch["id"])],
        }

    def save(s: Store, request: Request, ch: dict[str, Any], sheet: CharacterSheet, reason: str) -> dict[str, Any]:
        try:
            s.update_character(ch["id"], sheet.model_dump(), reason=reason)
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"this campaign already has a character called {sheet.name}")
        return sheet_payload(s, request, character_or_404(s, request, ch["id"]))

    def character_card(s: Store, request: Request, ch: dict[str, Any]) -> dict[str, Any]:
        sheet = inventory_of(s, request, ch["campaign_id"]).sheet(ch)
        return {
            "id": ch["id"],
            "name": sheet.name,
            "campaign": campaign_label(s, ch["campaign_id"]),
            "mine": local or ch["owner_id"] == user_of(request)["id"],
            "summary": " / ".join(f"{c.name} {c.level}" for c in sheet.classes),
            "species": sheet.species,
            "hp": sheet.hp.model_dump() | {"max": sheet.hp_max},
        }

    @app.get("/api/characters")
    def characters(request: Request, s: Store = Depends(store)) -> list[dict[str, Any]]:
        """Characters across the campaigns you're in: yours, and the rest of each party."""
        if local:
            rows = s.characters()
        else:
            uid = user_of(request)["id"]
            # Your own stay listed even after you leave their campaign.
            rows = s.characters(campaign_ids=[c.id for c, _ in Accounts(s).campaigns_for(uid)], owner_id=uid)
        return [character_card(s, request, ch) for ch in rows]

    @app.get("/api/campaigns/{slug}/characters")
    def party(request: Request, c: Campaign = Depends(reader), s: Store = Depends(store)) -> list[dict[str, Any]]:
        return [character_card(s, request, ch) for ch in s.characters(campaign_ids=[c.id])]

    def campaign_for_new_character(s: Store, request: Request, slug: str | None) -> int:
        if not slug:
            raise HTTPException(422, "a character belongs to a campaign: pick one")
        try:
            c = s.campaign(slug)
        except KeyError:
            raise HTTPException(404, f"no campaign {slug!r}")
        if not local and Accounts(s).membership(c.id, user_of(request)["id"]) is None:
            raise HTTPException(404, f"no campaign {slug!r}")
        return c.id

    def create(
        s: Store, request: Request, file: CharacterFile, campaign_id: int, reason: str, copied_from: int | None = None,
    ) -> dict:
        """A new character from a sheet file: the sheet, and what they carry into the party's inventory."""
        owner_id = None if local else user_of(request)["id"]
        try:
            character_id = s.create_character(
                file.sheet().model_dump(), campaign_id, owner_id, reason=reason, copied_from=copied_from,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"this campaign already has a character called {file.name}")
        inv = inventory_of(s, request, campaign_id)
        inv.bring_in(inv.character_holder(character_id), file.inventory, file.currency, reason=f"{file.name}'s, {reason}")
        return sheet_payload(s, request, character_or_404(s, request, character_id))

    def character_file(s: Store, request: Request, ch: dict[str, Any]) -> CharacterFile:
        """A character as a file, carrying what they have."""
        inv = inventory_of(s, request, ch["campaign_id"])
        holder = inv.character_holder(ch["id"])
        return CharacterFile.model_validate(ch["sheet"] | {
            "inventory": [i.model_dump() for i in inv.carried(holder)], "currency": inv.purse(holder)})

    @app.post("/api/characters", status_code=201)
    def new_character(body: NewCharacter, request: Request, s: Store = Depends(store)) -> dict[str, Any]:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "name is required")
        campaign_id = campaign_for_new_character(s, request, body.campaign)
        return create(s, request, CharacterFile(name=name), campaign_id, "created")

    @app.post("/api/characters/import", status_code=201)
    def import_character(
        request: Request, file: UploadFile = File(...), campaign: str = Form(""), s: Store = Depends(store),
    ) -> dict[str, Any]:
        try:
            sheet = CharacterFile.model_validate_json(file.file.read())
        except ValidationError as err:
            raise HTTPException(422, str(err))
        campaign_id = campaign_for_new_character(s, request, campaign or None)
        return create(s, request, sheet, campaign_id, f"imported {file.filename}")

    @app.post("/api/characters/{character_id}/copy", status_code=201)
    def copy_character(character_id: int, body: CopyCharacter, request: Request, s: Store = Depends(store)) -> dict:
        """Take a character to another campaign: a new sheet there, owned by you, that changes on its own."""
        ch = character_or_404(s, request, character_id, write=True)
        campaign_id = campaign_for_new_character(s, request, body.campaign)
        if campaign_id == ch["campaign_id"]:
            raise HTTPException(422, "the character is already in that campaign")
        origin = s.campaign_by_id(ch["campaign_id"]).name
        made = create(s, request, character_file(s, request, ch), campaign_id, f"copied from {origin}", copied_from=ch["id"])
        for f in s.character_files(ch["id"]):  # the scan goes with it
            s.add_character_file(made["id"], f["blob"], f["name"])
        return sheet_payload(s, request, character_or_404(s, request, made["id"]))

    @app.get("/api/characters/{character_id}/export")
    def export_character(character_id: int, request: Request, s: Store = Depends(store)) -> dict[str, Any]:
        """The character as a file: the sheet and what they carry, to import elsewhere."""
        return character_file(s, request, character_or_404(s, request, character_id)).model_dump()

    # Files kept with a character: the scan it was read from, or anything its owner attaches.
    @app.get("/api/characters/{character_id}/files/{file_id}")
    def get_character_file(character_id: int, file_id: int, request: Request, s: Store = Depends(store)) -> FileResponse:
        character_or_404(s, request, character_id)
        f = s.character_file(character_id, file_id)
        if f is None or not s.blobs.exists(f["blob"]):
            raise HTTPException(404, "no such file")
        # Inline, so a PDF or photo opens in the browser to check the sheet against.
        return FileResponse(s.blobs.path(f["blob"]), filename=f["name"], content_disposition_type="inline",
                            media_type=sheetscan.MEDIA_TYPES.get(Path(f["name"]).suffix.lower()))

    @app.post("/api/characters/{character_id}/files", status_code=201)
    def attach_file(character_id: int, request: Request, file: UploadFile = File(...),
                    s: Store = Depends(store)) -> dict[str, Any]:
        ch = character_or_404(s, request, character_id, write=True)
        data, name = file.file.read(sheetscan.MAX_BYTES + 1), file.filename or "file"
        try:
            sheetscan.check_upload(data, name)
        except sheetscan.ScanError as err:
            raise HTTPException(422, str(err))
        s.add_character_file(character_id, s.blobs.put(data), name)
        return sheet_payload(s, request, ch)

    @app.delete("/api/characters/{character_id}/files/{file_id}", status_code=204)
    def remove_file(character_id: int, file_id: int, request: Request, s: Store = Depends(store)) -> None:
        character_or_404(s, request, character_id, write=True)
        if not s.delete_character_file(character_id, file_id):
            raise HTTPException(404, "no such file")

    @app.get("/api/characters/{character_id}")
    def get_character(character_id: int, request: Request, s: Store = Depends(store)) -> dict[str, Any]:
        return sheet_payload(s, request, character_or_404(s, request, character_id))

    @app.put("/api/characters/{character_id}")
    def put_character(character_id: int, body: SheetUpdate, request: Request, s: Store = Depends(store)) -> dict:
        ch = character_or_404(s, request, character_id, write=True)
        try:
            sheet = CharacterSheet.model_validate(body.sheet)
        except ValidationError as err:
            raise HTTPException(422, err.errors(include_url=False, include_context=False))
        return save(s, request, ch, sheet, body.reason)

    @app.delete("/api/characters/{character_id}", status_code=204)
    def delete_character(character_id: int, request: Request, s: Store = Depends(store)) -> None:
        ch = character_or_404(s, request, character_id, write=True)
        inv = inventory_of(s, request, ch["campaign_id"])
        inv.release(inv.character_holder(character_id), reason=f"{ch['name']} was deleted")
        s.delete_character(character_id)

    @app.post("/api/characters/{character_id}/hp")
    def hp(character_id: int, body: HpChange, request: Request, s: Store = Depends(store)) -> dict[str, Any]:
        ch = character_or_404(s, request, character_id, write=True)
        sheet = inventory_of(s, request, ch["campaign_id"]).sheet(ch)
        apply_hp(sheet, body.delta)
        return save(s, request, ch, sheet, f"hp {body.delta:+d}")

    @app.post("/api/characters/{character_id}/rest")
    def take_rest(character_id: int, body: RestRequest, request: Request, s: Store = Depends(store)) -> dict:
        ch = character_or_404(s, request, character_id, write=True)
        sheet = inventory_of(s, request, ch["campaign_id"]).sheet(ch)
        rest(sheet, body.long)
        return save(s, request, ch, sheet, "long rest" if body.long else "short rest")

    # -- the party's inventory --------------------------------------------------------
    # Everything the party owns, by who has it (inventory.py). What the party
    # holds in common, and anything left with someone outside the party, anyone
    # in the campaign can change. A character's things need the right to change
    # that character. Taking from the party's things is acting for whoever gets
    # them; giving away your own needs only yours. Saying what an item does
    # (its definition, in the world) needs editor access, like the rest of the world.

    def user_id(request: Request) -> int | None:
        return None if local else user_of(request)["id"]

    def holder_rights(s: Store, request: Request, inv: Inventory, holder: int | None) -> bool:
        ch = inv.character_of(holder)
        return ch is None or character_rights(s, request, s.character(ch["id"]))[1]

    def must_hold(s: Store, request: Request, inv: Inventory, holder: int | None) -> None:
        if not holder_rights(s, request, inv, holder):
            raise HTTPException(403, f"only {inv.holder_name(holder)}'s owner or the DM can change what they have")

    def holder_in(inv: Inventory, holder: int | None) -> int | None:
        if holder is not None and inv.world.entity(holder) is None:
            raise HTTPException(404, "nobody like that in this campaign")
        return holder

    def inventory_payload(s: Store, request: Request, inv: Inventory) -> dict[str, Any]:
        out = inv.overview()
        for h in out["holders"]:
            h["can_edit"] = holder_rights(s, request, inv, h["id"])
        out["log"] = inv.log()
        out["can_define"] = at_least(inv.campaign.access, "editor")
        out["dm"] = not inv.campaign.spoiler_safe
        return out

    def inventory_dep(request: Request, c: Campaign = Depends(reader), s: Store = Depends(store)) -> Inventory:
        return Inventory(s, c, user_id(request))

    def inventory_call(s: Store, request: Request, inv: Inventory, fn: Callable[[], Any]) -> dict[str, Any]:
        try:
            fn()
        except InventoryError as err:
            raise HTTPException(422, str(err))
        return inventory_payload(s, request, inv)

    def holding_of(inv: Inventory, holding_id: int) -> dict[str, Any]:
        try:
            return inv.holding(holding_id)
        except InventoryError as err:
            raise HTTPException(404, str(err))

    @app.get("/api/campaigns/{slug}/inventory")
    def get_inventory(request: Request, inv: Inventory = Depends(inventory_dep), s: Store = Depends(store)) -> dict:
        return inventory_payload(s, request, inv)

    @app.post("/api/campaigns/{slug}/inventory/items")
    def add_item(body: NewHolding, request: Request, inv: Inventory = Depends(inventory_dep),
                 s: Store = Depends(store)) -> dict:
        must_hold(s, request, inv, holder_in(inv, body.holder))
        return inventory_call(s, request, inv, lambda: inv.add(body.name, body.qty, body.holder, body.notes, body.value_gp))

    @app.patch("/api/campaigns/{slug}/inventory/items/{holding_id}")
    def change_item(holding_id: int, body: HoldingChange, request: Request, inv: Inventory = Depends(inventory_dep),
                    s: Store = Depends(store)) -> dict:
        must_hold(s, request, inv, holding_of(inv, holding_id)["holder_id"])
        return inventory_call(s, request, inv, lambda: inv.change(holding_id, **body.model_dump(exclude_none=True)))

    @app.delete("/api/campaigns/{slug}/inventory/items/{holding_id}")
    def remove_item(holding_id: int, request: Request, inv: Inventory = Depends(inventory_dep),
                    s: Store = Depends(store)) -> dict:
        must_hold(s, request, inv, holding_of(inv, holding_id)["holder_id"])
        return inventory_call(s, request, inv, lambda: inv.remove(holding_id))

    def recipient(inv: Inventory, to: int | None, to_name: str | None) -> int | None:
        if to_name and to_name.strip():
            try:
                return inv.resolve_holder(to_name)
            except InventoryError as err:
                raise HTTPException(422, str(err))
        return holder_in(inv, to)

    @app.post("/api/campaigns/{slug}/inventory/items/{holding_id}/move")
    def move_item(holding_id: int, body: HoldingMove, request: Request, inv: Inventory = Depends(inventory_dep),
                  s: Store = Depends(store)) -> dict:
        """Claim it, give it, or put it back."""
        src = holding_of(inv, holding_id)["holder_id"]
        to = recipient(inv, body.to, body.to_name)
        must_hold(s, request, inv, src)
        if src is None:
            must_hold(s, request, inv, to)
        return inventory_call(s, request, inv, lambda: inv.move(holding_id, to, body.qty))

    @app.put("/api/campaigns/{slug}/inventory/items/{holding_id}/definition")
    def define_item(holding_id: int, body: Definition, request: Request, inv: Inventory = Depends(inventory_dep),
                    s: Store = Depends(store)) -> dict:
        """Say what an item does: its world item's definition, shared by every holding of it."""
        if not at_least(inv.campaign.access, "editor"):
            raise HTTPException(403, "saying what an item does needs editor access to the campaign")
        h = holding_of(inv, holding_id)
        mechanics = ItemMechanics(requires_attunement=body.requires_attunement, bonuses=body.bonuses, value_gp=body.value_gp)

        def run() -> None:
            linked = h["item"]["id"] if h["item"] else None
            if h["item"] and linked is None:  # linked to something this viewer can't see
                raise InventoryError("the DM has said what this item does; ask them to change it")
            item_id = inv.define((body.name or h["name"]).strip(), mechanics, body.party_knows, item_id=linked)
            if item_id != linked:
                inv.link(holding_id, item_id)

        return inventory_call(s, request, inv, run)

    @app.delete("/api/campaigns/{slug}/inventory/items/{holding_id}/definition")
    def unlink_item(holding_id: int, request: Request, inv: Inventory = Depends(inventory_dep),
                    s: Store = Depends(store)) -> dict:
        """Make it a plain item again; the world item and its definition stay."""
        if not at_least(inv.campaign.access, "editor"):
            raise HTTPException(403, "this needs editor access to the campaign")
        holding_of(inv, holding_id)
        return inventory_call(s, request, inv, lambda: inv.unlink(holding_id))

    @app.post("/api/campaigns/{slug}/inventory/coin")
    def change_coin(body: CoinChange, request: Request, inv: Inventory = Depends(inventory_dep),
                    s: Store = Depends(store)) -> dict:
        """Gain or spend coin: a change, not a total, so two at once both count."""
        must_hold(s, request, inv, holder_in(inv, body.holder))
        return inventory_call(s, request, inv, lambda: inv.coin(body.holder, body.currency))

    @app.post("/api/campaigns/{slug}/inventory/coin/give")
    def give_coin(body: CoinGive, request: Request, inv: Inventory = Depends(inventory_dep),
                  s: Store = Depends(store)) -> dict:
        src, to = holder_in(inv, body.holder), recipient(inv, body.to, body.to_name)
        must_hold(s, request, inv, src)
        if src is None:
            must_hold(s, request, inv, to)
        return inventory_call(s, request, inv, lambda: inv.give_coin(src, to, body.currency))

    @app.post("/api/campaigns/{slug}/inventory/coin/split")
    def split_coin(request: Request, inv: Inventory = Depends(inventory_dep), s: Store = Depends(store)) -> dict:
        """Share the party's purse evenly among the characters."""
        return inventory_call(s, request, inv, lambda: inv.split())

    # -- static app ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz(s: Store = Depends(store)) -> dict[str, Any]:
        return {"ok": True, "schema_version": s.schema_version}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    if mcp is not None:
        # Last, so it only gets what nothing above matched: /mcp and the OAuth endpoints.
        app.mount("/", mcp[1])
    return app


def serve(db: Path | str, host: str = "127.0.0.1", port: int = 8765, allow_no_auth: bool = False) -> None:
    import ipaddress

    import uvicorn

    auth = AuthConfig.from_env()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if auth is None and not loopback and not allow_no_auth:
        raise SystemExit(
            f"Refusing to listen on {host} without sign-in: anyone who can reach it could read your\n"
            "campaigns and spend your API credits. Set CAMPAIGN_AUTH=google (see docs/deploy.md),\n"
            "or pass --no-auth if this network is trusted."
        )
    # Opening the store migrates the schema; then bring chunking up to date.
    s = Store(db)
    for r in reindex(s):
        print(f"Re-chunked {r['title']}: {r['chunks']} chunks, {r['pending']} digest calls pending")
    print(f"Data: {Path(s.path).parent} (schema v{s.schema_version})")
    if auth:
        users = Accounts(s).users()
        print(f"Sign-in: Google, invite-only, {len(users)} user(s)")
        if not any(u["is_admin"] for u in users):
            print("No admin yet. Add yourself: campaign users add YOU@gmail.com --admin --claim-unowned")
    else:
        print("Sign-in: off")
    s.close()
    shutting_down = threading.Event()

    class Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            shutting_down.set()  # end live streams now; uvicorn then waits for the rest
            super().handle_exit(sig, frame)

    # Behind a reverse proxy (Caddy) only the proxy's X-Forwarded-* headers are trusted.
    # On SIGTERM (a deploy), finish requests in flight for up to 20 s, then stop the background loops.
    Server(uvicorn.Config(
        create_app(db, auth, shutting_down=shutting_down), host=host, port=port,
        proxy_headers=True, timeout_graceful_shutdown=20,
    )).run()
