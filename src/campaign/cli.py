"""Command-line interface: `campaign --help`."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from .charsheet import CharacterSheet, apply_hp, merge_patch, render, rest
from .config import db_path
from .db import Campaign, Store

app = typer.Typer(no_args_is_help=True, help="D&D campaign assistant.")
char_app = typer.Typer(no_args_is_help=True, help="Character sheets.")
app.add_typer(char_app, name="char")
users_app = typer.Typer(no_args_is_help=True, help="People who can sign in (when sign-in is on).")
app.add_typer(users_app, name="users")

AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".opus", ".webm"}

CampaignOpt = Annotated[
    Optional[str],
    typer.Option("--campaign", "-c", help="Campaign slug. Defaults to $CAMPAIGN or the only campaign."),
]


def store() -> Store:
    return Store(db_path())


def pick(s: Store, slug: str | None) -> Campaign:
    slug = slug or os.environ.get("CAMPAIGN")
    if slug:
        return s.campaign(slug)
    all_ = s.campaigns()
    if len(all_) == 1:
        return all_[0]
    names = ", ".join(c.slug for c in all_) or "none yet (run `campaign new`)"
    raise typer.BadParameter(f"pick a campaign with -c. Campaigns: {names}")


def show(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False))


@app.command()
def new(
    name: str,
    role: Annotated[str, typer.Option(help="Your role in this campaign: dm or player.")] = "dm",
    notes: str = "",
    owner: Annotated[Optional[str], typer.Option(help="With sign-in on: the user who owns it (else it's unowned until claimed).")] = None,
) -> None:
    """Create a campaign."""
    from .accounts import Accounts

    if role not in ("dm", "player"):
        raise typer.BadParameter("role must be dm or player")
    s = store()
    user = Accounts(s).user_by_email(owner) if owner else None
    if owner and user is None:
        raise typer.BadParameter(f"no user {owner}; add them with `campaign users add`")
    c = s.create_campaign(name, role, notes)
    if user:
        Accounts(s).add_member(c.id, user["id"], role, "owner")
    typer.echo(f"Created {c.name!r} ({c.slug}) as {c.role}" + (f", owned by {user['email']}." if user else "."))


@app.command("list")
def list_() -> None:
    """List campaigns."""
    for c in store().campaigns():
        typer.echo(f"{c.slug:24} {c.role:6} {c.name}")


@app.command("add-source")
def add_source(
    path: Path,
    campaign: CampaignOpt = None,
    title: Annotated[Optional[str], typer.Option(help="Defaults to the file name.")] = None,
    kind: Annotated[str, typer.Option(help="module or notes.")] = "module",
    dm_only: Annotated[
        Optional[bool],
        typer.Option("--dm-only/--party", help="Hide from the assistant in player campaigns. Default: modules are DM-only."),
    ] = None,
    digest_now: Annotated[bool, typer.Option("--digest/--no-digest", help="Digest with Claude right away.")] = False,
) -> None:
    """Load a PDF, Markdown or text file as reference material. The original is kept."""
    from .library import add_reference

    s = store()
    c = pick(s, campaign)
    r = add_reference(s, c, path.read_bytes(), path.name, title, kind, dm_only)
    typer.echo(f"Loaded {r['chunks']} chunks from {path.name} into {c.slug} (source {r['id']}).")
    typer.echo(f"Digest: {r['pending']} sections to digest" + (" (all cached)" if not r["pending"] else ""))
    if digest_now and r["pending"]:
        _digest(s, r["id"])


def _digest(s: Store, source_id: int) -> None:
    from . import digest

    def progress(st: dict) -> None:
        typer.echo(
            f"\r  digested {st['digested']}, cached {st['cached']}, failed {st['failed']} of {st['nodes']}",
            nl=False, err=True,
        )

    stats = digest.run(s.path, source_id, on_progress=progress)
    typer.echo(err=True)
    show(stats | {"entities": s.digest_status(source_id)["entities"]})


@app.command("digest")
def digest_cmd(
    source_id: Annotated[Optional[int], typer.Argument(help="Source id (see `sources`). Default: all.")] = None,
    campaign: CampaignOpt = None,
) -> None:
    """Digest reference material into an outline and an entity catalog. Resumes; skips cached sections."""
    s = store()
    ids = [source_id] if source_id else [
        x["id"] for x in s.sources(pick(s, campaign)) if x["kind"] != "transcript"
    ]
    for sid in ids:
        typer.echo(f"Source {sid}: {s.source(sid)['title']}")
        _digest(s, sid)


@app.command()
def outline(source_id: int, depth: int = 2) -> None:
    """Print a source's digested outline."""
    for n in store().digest_outline(source_id):
        if n["depth"] > depth:
            continue
        pages = f" p{n['page_start']}-{n['page_end']}" if n["page_start"] else ""
        mark = "" if n["summary"] else "  [not digested]"
        typer.secho(f"{'  ' * n['depth']}{n['title']}{pages}{mark}", bold=n["depth"] == 0)
        if n["summary"]:
            typer.echo(f"{'  ' * n['depth']}  {n['summary'][:300]}")


@app.command("reindex")
def reindex_cmd(force: bool = typer.Option(False, help="Re-chunk everything, not just outdated sources.")) -> None:
    """Re-chunk sources from their stored originals (after a chunker change). No API calls."""
    from .library import reindex

    done = reindex(store(), force=force)
    show(done or "Everything is up to date.")


@app.command()
def info() -> None:
    """Where your data lives and what's in it."""
    s = store()
    show({
        "database": s.path,
        "schema_version": s.schema_version,
        "originals": s.blobs.stats(),
        "backups": str(Path(s.path).parent / "backups"),
        "campaigns": [c.slug for c in s.campaigns()],
    })


@app.command()
def sources(campaign: CampaignOpt = None) -> None:
    """List reference material."""
    s = store()
    show(s.sources(pick(s, campaign)))


@app.command()
def search(query: str, campaign: CampaignOpt = None, limit: int = 8) -> None:
    """Search reference material directly (no AI)."""
    s = store()
    for hit in s.search(pick(s, campaign), query, limit=limit):
        page = f" p{hit['page']}" if hit["page"] else ""
        typer.echo(f"#{hit['id']} {hit['source']}{page} — {hit['heading']}\n    {hit['snippet']}\n")


@app.command()
def read(chunk_id: int, campaign: CampaignOpt = None, after: int = 0) -> None:
    """Print a reference chunk (ids come from `search`)."""
    s = store()
    for row in s.read_chunks(pick(s, campaign), chunk_id, after=after):
        page = f" p{row['page']}" if row["page"] else ""
        typer.echo(f"--- {row['source']}{page} {row['heading']}\n{row['text']}\n")


def _progress(name: str, args: dict) -> None:
    from .agent import describe_tool_call

    typer.secho(describe_tool_call(name, args), fg="bright_black", err=True)


@app.command()
def ask(question: str, campaign: CampaignOpt = None) -> None:
    """Ask one question."""
    from .agent import chat_agent

    s = store()
    agent = chat_agent(s, pick(s, campaign).slug, on_tool=_progress)
    agent.send(question)
    typer.echo()


@app.command()
def chat(campaign: CampaignOpt = None) -> None:
    """Interactive conversation. Tell it what happened and it updates the campaign state."""
    from .agent import chat_agent

    s = store()
    c = pick(s, campaign)
    agent = chat_agent(s, c.slug, on_tool=_progress)
    typer.echo(f"{c.name} ({c.role}). Ctrl-D to exit.")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo()
            return
        if q:
            agent.send(q)
            typer.echo()


@app.command()
def ingest(
    path: Path,
    campaign: CampaignOpt = None,
    number: Annotated[Optional[int], typer.Option(help="Session number. Defaults to the next one.")] = None,
    date: Annotated[Optional[str], typer.Option(help="Date played, YYYY-MM-DD.")] = None,
    whisper_model: str = "small",
) -> None:
    """Fold a session transcript (or audio recording) into the campaign state."""
    from .agent import ingest_session

    s = store()
    c = pick(s, campaign)
    if path.suffix.lower() in AUDIO:
        from .transcribe import transcribe

        typer.echo(f"Transcribing {path.name}…", err=True)
        transcript = transcribe(path, model_size=whisper_model)
        out = path.with_suffix(".transcript.txt")
        out.write_text(transcript, encoding="utf-8")
        typer.echo(f"Saved transcript to {out}", err=True)
    else:
        transcript = path.read_text(encoding="utf-8")
    result = ingest_session(
        s, c.slug, transcript, number=number, played_on=date,
        on_tool=_progress, on_text=lambda t: sys.stderr.write(t),
    )
    typer.echo(f"\nSession {result['session']}: {result.get('title', '(untitled)')}")
    typer.echo(result.get("summary", ""))
    typer.echo(f"\n{result['facts']} facts and {result['journal_entries']} journal entries recorded."
               f" Undo with: campaign revert {result['session']}")


@app.command()
def revert(number: int, campaign: CampaignOpt = None) -> None:
    """Undo what the ingest of one session recorded."""
    from .world import World

    s = store()
    show(World(s, pick(s, campaign)).revert_session(number))


@app.command()
def state(
    campaign: CampaignOpt = None,
    kind: Optional[str] = None,
    query: Annotated[Optional[str], typer.Option("--query", "-q")] = None,
    in_play: Annotated[bool, typer.Option(help="Only what the table has interacted with.")] = False,
) -> None:
    """Show people, places and things with their current status and location."""
    from .world import World

    s = store()
    w = World(s, pick(s, campaign))
    for e in w.list(kind=kind, q=query, in_play=in_play):
        attrs = ", ".join(f"{k}: {v}" for k, v in e["current"].items())
        typer.echo(f"[{e['kind']}] {e['name']}" + (f" ({attrs})" if attrs else ""))
        if e["headline"]:
            typer.echo(f"    {e['headline']}")


@app.command()
def entity(name: str, campaign: CampaignOpt = None, as_of: Optional[int] = None) -> None:
    """Show everything known about one entity, optionally as of a past session."""
    from .world import World

    s = store()
    hits = World(s, pick(s, campaign)).lookup(name, as_of=as_of)
    show(hits[0] if hits else f"No entity called {name!r}.")


@app.command()
def journal(campaign: CampaignOpt = None, session: Optional[int] = None) -> None:
    """Show the campaign journal."""
    from .world import World

    s = store()
    c = pick(s, campaign)
    titles = {x["number"]: x["title"] for x in s.sessions(c)}
    current = None
    for e in World(s, c).events(session=session):
        if e["session"] != current:
            current = e["session"]
            typer.secho(f"Session {current}: {titles.get(current, '')}", bold=True)
        typer.echo(f"  {e['text']}")


@app.command()
def mcp(
    campaign: CampaignOpt = None,
    read_only: Annotated[bool, typer.Option(help="Expose only the read tools.")] = False,
) -> None:
    """Run an MCP server (stdio) so Claude Code / Claude Desktop can use this campaign."""
    from .mcp_server import serve

    s = store()
    serve(s, pick(s, campaign).slug, read_only=read_only)


@app.command()
def web(
    host: Annotated[str, typer.Option(help="Interface to listen on. Anything but localhost requires sign-in (CAMPAIGN_AUTH=google) or --no-auth.")] = "127.0.0.1",
    port: int = 8765,
    no_auth: Annotated[bool, typer.Option("--no-auth", help="Allow listening on a network interface without sign-in.")] = False,
) -> None:
    """Run the web UI for character sheets and campaign ingest."""
    from .web import serve

    serve(db_path(), host=host, port=port, allow_no_auth=no_auth)


# -- characters --------------------------------------------------------------


def _load(s: Store, name: str) -> tuple[int, CharacterSheet]:
    found = s.find_character(name)
    if found is None:
        raise typer.BadParameter(f"no character {name!r}")
    return found["id"], CharacterSheet.model_validate(found["sheet"])


@char_app.command("list")
def char_list() -> None:
    """List characters."""
    s = store()
    names = {c.id: c.slug for c in s.campaigns()}
    for c in s.characters():
        typer.echo(f"{c['name']:24} {names.get(c['campaign_id'], '-')}")


@char_app.command("show")
def char_show(name: str, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Print a character sheet."""
    _, sheet = _load(store(), name)
    if as_json:
        show(sheet.model_dump())
    else:
        typer.echo(render(sheet))


@char_app.command("import")
def char_import(path: Path, campaign: CampaignOpt = None) -> None:
    """Create or replace a character from a JSON sheet (see examples/character.json)."""
    s = store()
    sheet = CharacterSheet.model_validate_json(path.read_text(encoding="utf-8"))
    c = pick(s, campaign)  # a character belongs to a campaign
    reason = f"imported from {path.name}"
    if existing := s.find_character(sheet.name, c):
        s.update_character(existing["id"], sheet.model_dump(), reason=reason)
    else:
        s.create_character(sheet.model_dump(), campaign_id=c.id, reason=reason)
    typer.echo(f"Saved {sheet.name} in {c.name}.")


@char_app.command("set")
def char_set(
    name: str,
    patch: Annotated[str, typer.Argument(help='JSON merge patch, e.g. \'{"hp": {"current": 12}}\'')],
    reason: str = "manual edit",
) -> None:
    """Apply a JSON merge patch to a sheet."""
    s = store()
    character_id, current = _load(s, name)
    updated = CharacterSheet.model_validate(merge_patch(current.model_dump(), json.loads(patch)))
    s.update_character(character_id, updated.model_dump(), reason=reason)
    typer.echo(render(updated))


@char_app.command("hp")
def char_hp(
    name: str,
    delta: Annotated[int, typer.Argument(help="Negative for damage, positive for healing.")],
) -> None:
    """Apply damage or healing. Damage comes off temp HP first."""
    s = store()
    character_id, sheet = _load(s, name)
    apply_hp(sheet, delta)
    hp = sheet.hp
    s.update_character(character_id, sheet.model_dump(), reason=f"hp {delta:+d}")
    typer.echo(f"{sheet.name}: {hp.current}/{hp.max}" + (f" (+{hp.temp} temp)" if hp.temp else ""))


@char_app.command("rest")
def char_rest(name: str, long: Annotated[bool, typer.Option("--long/--short")] = True) -> None:
    """Reset resources that recharge on a short or long rest (long also restores HP)."""
    s = store()
    character_id, sheet = _load(s, name)
    rest(sheet, long)
    s.update_character(character_id, sheet.model_dump(), reason="long rest" if long else "short rest")
    typer.echo(render(sheet))


@char_app.command("log")
def char_log(name: str, limit: int = 20) -> None:
    """Show recent changes to a sheet."""
    s = store()
    character_id, _ = _load(s, name)
    for row in s.character_log(character_id, limit=limit):
        typer.echo(f"{row['created_at']}  {row['reason']}")


# -- people (only matters with sign-in on) ---------------------------------------


@users_app.command("list")
def users_list() -> None:
    """List users."""
    from .accounts import Accounts

    for u in Accounts(store()).users():
        flags = " admin" if u["is_admin"] else ""
        typer.echo(f"{u['email']:36} {u['campaigns']} campaign(s){flags}  last seen {u['last_seen_at'] or 'never'}")


@users_app.command("add")
def users_add(
    email: str,
    admin: Annotated[bool, typer.Option(help="Can manage users and invite people to the app.")] = False,
    claim_unowned: Annotated[bool, typer.Option(help="Become owner of every campaign nobody owns yet.")] = False,
) -> None:
    """Add a user directly, without an invite. Use this for the first admin."""
    from .accounts import Accounts

    acc = Accounts(store())
    user = acc.create_user(email, is_admin=admin)
    typer.echo(f"{user['email']}{' (admin)' if user['is_admin'] else ''} can sign in.")
    if claim_unowned:
        claimed = acc.claim_unowned(user["id"])
        typer.echo(f"Now owner of: {', '.join(claimed) or 'nothing (every campaign has an owner)'}")


@users_app.command("remove")
def users_remove(email: str) -> None:
    """Remove a user. They're signed out at once; their character sheets stay, ownerless."""
    from .accounts import Accounts

    acc = Accounts(store())
    user = acc.user_by_email(email)
    if user is None:
        raise typer.BadParameter(f"no user {email}")
    acc.delete_user(user["id"])
    typer.echo(f"Removed {email}.")


@app.command()
def invite(
    campaign: CampaignOpt = None,
    role: Annotated[str, typer.Option(help="dm or player")] = "player",
    access: Annotated[str, typer.Option(help="editor or viewer")] = "viewer",
    email: Annotated[Optional[str], typer.Option(help="Only this Google account can use the link.")] = None,
    days: int = 7,
    uses: int = 1,
    app_only: Annotated[bool, typer.Option("--app-only", help="Access to the app, not a campaign.")] = False,
) -> None:
    """Make an invite link. Prints the link; it isn't stored and can't be shown again."""
    from .accounts import Accounts

    s = store()
    c = None if app_only else pick(s, campaign)
    inv, token = Accounts(s).create_invite(
        c.id if c else None, role, access, email=email, days=days, max_uses=uses
    )
    base = os.environ.get("CAMPAIGN_PUBLIC_URL", "http://127.0.0.1:8765").rstrip("/")
    what = f"{c.name} as {role} ({access})" if c else "the app"
    typer.echo(f"Invite to {what}, expires {inv['expires_at']}:\n{base}/invite/{token}")


def main() -> None:
    app()
