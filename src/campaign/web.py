"""Web UI: a JSON API plus the static single-page app in `static/`.

    campaign web            # http://127.0.0.1:8765

Session ingests take minutes, so they run as background jobs; the page polls
`/api/jobs/{id}` for progress. There is no authentication: bind to localhost
or put it behind something that authenticates before exposing it.
"""

from __future__ import annotations

import tempfile
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .charsheet import CharacterSheet, apply_hp, rest
from .db import Campaign, Store, slugify
from .ingest import chunk_file

STATIC = Path(__file__).parent / "static"
AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".opus", ".webm"}
REFERENCE = {".pdf", ".md", ".markdown", ".txt"}


@dataclass
class Job:
    id: str
    campaign: str
    status: str = "running"  # running | done | failed
    log: list[dict[str, str]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None

    def emit(self, kind: str, text: str) -> None:
        # Merge consecutive text deltas so the log is lines, not tokens.
        if kind == "text" and self.log and self.log[-1]["kind"] == "text":
            self.log[-1]["text"] += text
        else:
            self.log.append({"kind": kind, "text": text})


class NewCampaign(BaseModel):
    name: str
    role: str = "dm"
    notes: str = ""


class NewCharacter(BaseModel):
    name: str
    campaign: str | None = None


class SheetUpdate(BaseModel):
    sheet: dict[str, Any]
    reason: str = "edited"


class HpChange(BaseModel):
    delta: int


class RestRequest(BaseModel):
    long: bool = True


def create_app(db: Path | str) -> FastAPI:
    app = FastAPI(title="campaign")
    jobs: dict[str, Job] = {}

    def store() -> Iterator[Store]:
        # One connection per request: sqlite connections shouldn't be shared
        # between the request threads and the ingest threads.
        s = Store(db)
        try:
            yield s
        finally:
            s.close()

    def campaign_or_404(s: Store, slug: str) -> Campaign:
        try:
            return s.campaign(slug)
        except KeyError:
            raise HTTPException(404, f"no campaign {slug!r}")

    def sheet_or_404(s: Store, name: str) -> CharacterSheet:
        raw = s.character(name)
        if raw is None:
            raise HTTPException(404, f"no character {name!r}")
        return CharacterSheet.model_validate(raw)

    def sheet_payload(s: Store, sheet: CharacterSheet) -> dict[str, Any]:
        return {
            "sheet": sheet.model_dump(),
            "derived": sheet.derived(),
            "log": s.character_log(sheet.name, limit=30),
        }

    # -- campaigns -----------------------------------------------------------

    @app.get("/api/campaigns")
    def list_campaigns(s: Store = Depends(store)) -> list[dict[str, Any]]:
        return [vars(c) for c in s.campaigns()]

    @app.post("/api/campaigns", status_code=201)
    def new_campaign(body: NewCampaign, s: Store = Depends(store)) -> dict[str, Any]:
        if body.role not in ("dm", "player"):
            raise HTTPException(422, "role must be dm or player")
        if not slugify(body.name):
            raise HTTPException(422, "name is required")
        try:
            s.campaign(slugify(body.name))
            raise HTTPException(409, "a campaign with that name exists")
        except KeyError:
            pass
        return vars(s.create_campaign(body.name, body.role, body.notes))

    @app.get("/api/campaigns/{slug}/sources")
    def sources(slug: str, s: Store = Depends(store)) -> list[dict[str, Any]]:
        return s.sources(campaign_or_404(s, slug))

    @app.post("/api/campaigns/{slug}/sources", status_code=201)
    def add_source(
        slug: str,
        file: UploadFile = File(...),
        title: str = Form(""),
        kind: str = Form("module"),
        dm_only: bool | None = Form(None),
        s: Store = Depends(store),
    ) -> dict[str, Any]:
        c = campaign_or_404(s, slug)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in REFERENCE:
            raise HTTPException(422, "upload a .pdf, .md or .txt file")
        if kind not in ("module", "notes"):
            raise HTTPException(422, "kind must be module or notes")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"upload{suffix}"
            path.write_bytes(file.file.read())
            try:
                chunks = chunk_file(path)
            except Exception as err:
                raise HTTPException(422, f"couldn't read {file.filename}: {err}")
        if not chunks:
            raise HTTPException(422, "no text found (a scanned PDF needs OCR first)")
        hidden = (kind == "module") if dm_only is None else dm_only
        name = title.strip() or Path(file.filename or "upload").stem
        source_id = s.add_source(c, name, kind, chunks, path=file.filename, dm_only=hidden)
        return {"id": source_id, "title": name, "chunks": len(chunks)}

    @app.delete("/api/campaigns/{slug}/sources/{source_id}", status_code=204)
    def delete_source(slug: str, source_id: int, s: Store = Depends(store)) -> None:
        if not s.delete_source(campaign_or_404(s, slug), source_id):
            raise HTTPException(404, "no such source")

    @app.get("/api/campaigns/{slug}/search")
    def search(slug: str, q: str, s: Store = Depends(store)) -> list[dict[str, Any]]:
        return s.search(campaign_or_404(s, slug), q, limit=20)

    @app.get("/api/campaigns/{slug}/chunks/{chunk_id}")
    def chunk(slug: str, chunk_id: int, s: Store = Depends(store)) -> list[dict[str, Any]]:
        return s.read_chunks(campaign_or_404(s, slug), chunk_id, after=1)

    @app.get("/api/campaigns/{slug}/sessions")
    def sessions(slug: str, s: Store = Depends(store)) -> dict[str, Any]:
        c = campaign_or_404(s, slug)
        running = [vars(j) | {"log": None} for j in jobs.values() if j.campaign == slug and j.status == "running"]
        return {"sessions": s.sessions(c), "next": s.next_session_number(c), "running": running}

    @app.post("/api/campaigns/{slug}/sessions/{number}/revert")
    def revert(slug: str, number: int, s: Store = Depends(store)) -> dict[str, int]:
        try:
            return s.revert_session(campaign_or_404(s, slug), number)
        except KeyError:
            raise HTTPException(404, f"no session {number}")

    @app.get("/api/campaigns/{slug}/entities")
    def entities(
        slug: str, kind: str | None = None, status: str | None = None, q: str | None = None,
        s: Store = Depends(store),
    ) -> list[dict[str, Any]]:
        return s.find_entities(campaign_or_404(s, slug), kind=kind or None, status=status or None, name=q or None)

    @app.get("/api/campaigns/{slug}/journal")
    def journal(slug: str, session: int | None = None, s: Store = Depends(store)) -> list[dict[str, Any]]:
        return s.events(campaign_or_404(s, slug), session_number=session, limit=500)

    # -- session ingest ------------------------------------------------------

    @app.post("/api/campaigns/{slug}/ingest", status_code=202)
    def ingest(
        slug: str,
        file: UploadFile | None = File(None),
        text: str = Form(""),
        number: int | None = Form(None),
        played_on: str = Form(""),
        s: Store = Depends(store),
    ) -> dict[str, str]:
        campaign_or_404(s, slug)
        if any(j.campaign == slug and j.status == "running" for j in jobs.values()):
            raise HTTPException(409, "an ingest is already running for this campaign")
        upload: tuple[str, bytes] | None = None
        if file is not None and file.filename:
            upload = (Path(file.filename).suffix.lower(), file.file.read())
        elif not text.strip():
            raise HTTPException(422, "paste a transcript or upload a file")

        job = Job(id=uuid.uuid4().hex[:12], campaign=slug)
        jobs[job.id] = job

        def run() -> None:
            from .agent import describe_tool_call, ingest_session

            js = Store(db)
            try:
                transcript = text
                if upload is not None:
                    suffix, data = upload
                    if suffix in AUDIO:
                        from .transcribe import transcribe

                        job.emit("status", "Transcribing audio…")
                        with tempfile.TemporaryDirectory() as tmp:
                            path = Path(tmp) / f"audio{suffix}"
                            path.write_bytes(data)
                            transcript = transcribe(path)
                    else:
                        transcript = data.decode("utf-8", errors="replace")
                job.emit("status", "Reading the transcript and updating the campaign…")
                job.result = ingest_session(
                    js, slug, transcript, number=number, played_on=played_on or None,
                    on_text=lambda t: job.emit("text", t),
                    on_tool=lambda n, a: job.emit("tool", describe_tool_call(n, a).strip(" ·")),
                )
                job.status = "done"
            except BaseException as err:  # SystemExit from a missing audio extra too
                job.error = str(err) or type(err).__name__
                job.emit("error", traceback.format_exc(limit=3))
                job.status = "failed"
            finally:
                js.close()

        threading.Thread(target=run, name=f"ingest-{job.id}", daemon=True).start()
        return {"job": job.id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, since: int = 0) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job (the server may have restarted)")
        # Resend the last entry: text entries grow in place.
        start = max(0, min(since, len(job.log)) - 1)
        return {
            "status": job.status,
            "result": job.result,
            "error": job.error,
            "log": job.log[start:],
            "from": start,
        }

    # -- characters ----------------------------------------------------------

    @app.get("/api/characters")
    def characters(s: Store = Depends(store)) -> list[dict[str, Any]]:
        out = []
        for name in s.characters():
            sheet = CharacterSheet.model_validate(s.character(name))
            out.append({
                "name": sheet.name,
                "summary": " / ".join(f"{c.name} {c.level}" for c in sheet.classes),
                "species": sheet.species,
                "hp": sheet.hp.model_dump(),
            })
        return out

    @app.post("/api/characters", status_code=201)
    def new_character(body: NewCharacter, s: Store = Depends(store)) -> dict[str, Any]:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "name is required")
        if s.character(name) is not None:
            raise HTTPException(409, f"{name} already exists")
        sheet = CharacterSheet(name=name)
        c = campaign_or_404(s, body.campaign) if body.campaign else None
        s.save_character(name, sheet.model_dump(), campaign=c, reason="created")
        return sheet_payload(s, sheet)

    @app.post("/api/characters/import", status_code=201)
    def import_character(file: UploadFile = File(...), s: Store = Depends(store)) -> dict[str, Any]:
        try:
            sheet = CharacterSheet.model_validate_json(file.file.read())
        except ValidationError as err:
            raise HTTPException(422, str(err))
        s.save_character(sheet.name, sheet.model_dump(), reason=f"imported {file.filename}")
        return sheet_payload(s, sheet)

    @app.get("/api/characters/{name}")
    def get_character(name: str, s: Store = Depends(store)) -> dict[str, Any]:
        return sheet_payload(s, sheet_or_404(s, name))

    @app.put("/api/characters/{name}")
    def put_character(name: str, body: SheetUpdate, s: Store = Depends(store)) -> dict[str, Any]:
        sheet_or_404(s, name)
        try:
            sheet = CharacterSheet.model_validate(body.sheet)
        except ValidationError as err:
            raise HTTPException(422, err.errors(include_url=False, include_context=False))
        if sheet.name != name:
            if s.character(sheet.name) is not None:
                raise HTTPException(409, f"{sheet.name} already exists")
            s.rename_character(name, sheet.name)
        s.save_character(sheet.name, sheet.model_dump(), reason=body.reason)
        return sheet_payload(s, sheet)

    @app.delete("/api/characters/{name}", status_code=204)
    def delete_character(name: str, s: Store = Depends(store)) -> None:
        if not s.delete_character(name):
            raise HTTPException(404, f"no character {name!r}")

    @app.post("/api/characters/{name}/hp")
    def hp(name: str, body: HpChange, s: Store = Depends(store)) -> dict[str, Any]:
        sheet = sheet_or_404(s, name)
        apply_hp(sheet, body.delta)
        s.save_character(sheet.name, sheet.model_dump(), reason=f"hp {body.delta:+d}")
        return sheet_payload(s, sheet)

    @app.post("/api/characters/{name}/rest")
    def take_rest(name: str, body: RestRequest, s: Store = Depends(store)) -> dict[str, Any]:
        sheet = sheet_or_404(s, name)
        rest(sheet, body.long)
        s.save_character(sheet.name, sheet.model_dump(), reason="long rest" if body.long else "short rest")
        return sheet_payload(s, sheet)

    # -- static app ----------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


def serve(db: Path | str, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(create_app(db), host=host, port=port)
