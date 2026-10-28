"""Command-line interface: `campaign --help`."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from .charsheet import CharacterSheet, merge_patch, render
from .config import db_path
from .db import Campaign, Store
from .ingest import chunk_file

app = typer.Typer(no_args_is_help=True, help="D&D campaign assistant.")
char_app = typer.Typer(no_args_is_help=True, help="Character sheets.")
app.add_typer(char_app, name="char")

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
) -> None:
    """Create a campaign."""
    if role not in ("dm", "player"):
        raise typer.BadParameter("role must be dm or player")
    c = store().create_campaign(name, role, notes)
    typer.echo(f"Created {c.name!r} ({c.slug}) as {c.role}.")


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
) -> None:
    """Load a PDF, Markdown or text file as reference material."""
    s = store()
    c = pick(s, campaign)
    chunks = chunk_file(path)
    hidden = (kind == "module") if dm_only is None else dm_only
    s.add_source(c, title or path.stem, kind, chunks, path=str(path.resolve()), dm_only=hidden)
    typer.echo(f"Loaded {len(chunks)} chunks from {path.name} into {c.slug}.")


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
    typer.echo(f"\n{result['journal_entries']} journal entries recorded. Undo with: campaign revert {result['session']}")


@app.command()
def revert(number: int, campaign: CampaignOpt = None) -> None:
    """Undo the state changes an ingest made for one session."""
    s = store()
    show(s.revert_session(pick(s, campaign), number))


@app.command()
def state(
    campaign: CampaignOpt = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    """Show tracked NPCs, quests, locations, items…"""
    s = store()
    for e in s.find_entities(pick(s, campaign), kind=kind, status=status):
        typer.echo(f"[{e['kind']}] {e['name']} ({e['status'] or '-'})\n    {e['summary']}")
        if e["data"]:
            typer.echo("    " + json.dumps(e["data"], ensure_ascii=False))


@app.command()
def journal(campaign: CampaignOpt = None, session: Optional[int] = None, limit: int = 50) -> None:
    """Show the campaign journal."""
    s = store()
    c = pick(s, campaign)
    if session is None:
        for sess in s.sessions(c):
            typer.secho(f"Session {sess['number']}: {sess['title']}", bold=True)
    for e in s.events(c, session_number=session, limit=limit):
        typer.echo(f"  S{e['session'] or '-'}  {e['text']}")


@app.command()
def mcp(
    campaign: CampaignOpt = None,
    read_only: Annotated[bool, typer.Option(help="Expose only the read tools.")] = False,
) -> None:
    """Run an MCP server (stdio) so Claude Code / Claude Desktop can use this campaign."""
    from .mcp_server import serve

    s = store()
    serve(s, pick(s, campaign).slug, read_only=read_only)


# -- characters --------------------------------------------------------------


def _load(s: Store, name: str) -> CharacterSheet:
    raw = s.character(name)
    if raw is None:
        raise typer.BadParameter(f"no character {name!r}")
    return CharacterSheet.model_validate(raw)


@char_app.command("list")
def char_list() -> None:
    """List characters."""
    for n in store().characters():
        typer.echo(n)


@char_app.command("show")
def char_show(name: str, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Print a character sheet."""
    sheet = _load(store(), name)
    if as_json:
        show(sheet.model_dump())
    else:
        typer.echo(render(sheet))


@char_app.command("import")
def char_import(path: Path, campaign: CampaignOpt = None) -> None:
    """Create or replace a character from a JSON sheet (see examples/character.json)."""
    s = store()
    sheet = CharacterSheet.model_validate_json(path.read_text(encoding="utf-8"))
    c = pick(s, campaign) if (campaign or s.campaigns()) else None
    s.save_character(sheet.name, sheet.model_dump(), campaign=c, reason=f"imported from {path.name}")
    typer.echo(f"Saved {sheet.name}.")


@char_app.command("set")
def char_set(
    name: str,
    patch: Annotated[str, typer.Argument(help='JSON merge patch, e.g. \'{"hp": {"current": 12}}\'')],
    reason: str = "manual edit",
) -> None:
    """Apply a JSON merge patch to a sheet."""
    s = store()
    current = _load(s, name)
    updated = CharacterSheet.model_validate(merge_patch(current.model_dump(), json.loads(patch)))
    s.save_character(updated.name, updated.model_dump(), reason=reason)
    typer.echo(render(updated))


@char_app.command("hp")
def char_hp(
    name: str,
    delta: Annotated[int, typer.Argument(help="Negative for damage, positive for healing.")],
) -> None:
    """Apply damage or healing. Damage comes off temp HP first."""
    s = store()
    sheet = _load(s, name)
    hp = sheet.hp.model_copy()
    if delta < 0:
        absorbed = min(hp.temp, -delta)
        hp.temp -= absorbed
        hp.current = max(0, hp.current + delta + absorbed)
    else:
        hp.current = min(hp.max, hp.current + delta)
    sheet.hp = hp
    s.save_character(sheet.name, sheet.model_dump(), reason=f"hp {delta:+d}")
    typer.echo(f"{sheet.name}: {hp.current}/{hp.max}" + (f" (+{hp.temp} temp)" if hp.temp else ""))


@char_app.command("rest")
def char_rest(name: str, long: Annotated[bool, typer.Option("--long/--short")] = True) -> None:
    """Reset resources that recharge on a short or long rest (long also restores HP)."""
    s = store()
    sheet = _load(s, name)
    kinds = {"short rest"} | ({"long rest", "dawn"} if long else set())
    for r in sheet.resources:
        if r.recharge in kinds:
            r.used = 0
    if long:
        sheet.hp = sheet.hp.model_copy(update={"current": sheet.hp.max, "temp": 0})
        sheet.death_saves = {"successes": 0, "failures": 0}
    s.save_character(sheet.name, sheet.model_dump(), reason="long rest" if long else "short rest")
    typer.echo(render(sheet))


@char_app.command("log")
def char_log(name: str, limit: int = 20) -> None:
    """Show recent changes to a sheet."""
    for row in store().character_log(name, limit=limit):
        typer.echo(f"{row['created_at']}  {row['reason']}")


def main() -> None:
    app()
