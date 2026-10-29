"""Jobs: background transformations, durable from upload to result.

Anything slow that turns an upload into campaign data (ingesting a
session's transcript, reading a character sheet from a scan) is a job: a
row saying what's wanted, with its input (uploads are kept in the blob
store), its state, and in the end its result. Asking for one only adds the
row; JobLoop works through them oldest first, in the background, on
whichever server is up. Each kind is safe to redo from the start, so the
loop keeps no memory of its own: a job interrupted by a deploy or a crash
is simply run again. Kinds may save what they've worked out into their
input (a transcription, a read sheet) so that a redo doesn't pay for it
twice. Progress goes to job_log, so any server can show it.

Failures retry after 1 and then 4 minutes, then wait to be retried by hand;
a Permanent failure (a file that isn't a sheet, a name already taken) fails
at once. Cancelling stops at the job's next check and undoes what it wrote.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .db import Campaign, Store
from .loops import LeasedLoop

AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".opus", ".webm"}
MAX_ATTEMPTS = 3
RETRY_SECONDS = 60
FLUSH_SECONDS = 0.5
CHECK_SECONDS = 1.0


class JobStopped(Exception):
    """The job was cancelled, or this server is handing its work over."""


class Permanent(Exception):
    """A failure that trying again won't fix: the job fails at once, with this message."""


class Log:
    """Writes a job's progress to the database, merging streamed text into lines."""

    def __init__(self, db: Path | str, job_id: int):
        self.db, self.job_id = db, job_id
        self.pending: list[tuple[str, str]] = []
        self.growing = False  # the last row in the database is text that's still growing
        self.text = ""
        self.last_flush = 0.0

    def emit(self, kind: str, text: str) -> None:
        if kind == "text":
            if self.pending and self.pending[-1][0] == "text":
                self.text += text
                self.pending[-1] = ("text", self.text)
            elif not self.pending and self.growing:
                self.text += text
                self.pending.append(("text", self.text))
            else:
                self.text = text
                self.pending.append(("text", text))
        else:
            self.pending.append((kind, text))
        if kind != "text" or time.monotonic() - self.last_flush > FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        replace_last = self.growing and self.pending[0][0] == "text"
        s = Store(self.db)
        try:
            s.add_job_log(self.job_id, self.pending, replace_last=replace_last)
        finally:
            s.close()
        self.growing = self.pending[-1][0] == "text"
        self.pending = []
        self.last_flush = time.monotonic()


@dataclass
class Run:
    """One attempt at a job, as its kind sees it."""

    store: Store
    job: dict[str, Any]
    campaign: Campaign  # as the person who asked for it sees it, with write access
    log: Log
    check: Callable[[], None]  # raises JobStopped if the job should stop now; call it often

    @property
    def input(self) -> dict[str, Any]:
        return self.job["input"]

    def save_input(self, **fields: Any) -> None:
        """Keep something worked out (a transcription, a read sheet), so a redo doesn't redo it."""
        self.job["input"] = self.input | fields
        self.store.update_job(self.job["id"], input=self.job["input"])


# -- the kinds ------------------------------------------------------------------------


def run_ingest(run: Run) -> dict[str, Any]:
    """Session N should be what this transcript says. Redoing it replaces the session's earlier recording."""
    from .agent import describe_tool_call, ingest_session

    transcript = _transcript(run)
    run.log.emit("status", "Reading the transcript and updating the campaign…")

    def on_tool(name: str, args: dict[str, Any]) -> None:
        run.check()
        run.log.emit("tool", describe_tool_call(name, args).strip(" ·"))

    return ingest_session(
        run.store, run.campaign, transcript, number=run.input["session"], played_on=run.input["played_on"],
        on_text=lambda t: run.log.emit("text", t), on_tool=on_tool,
    )


def undo_ingest(run: Run) -> None:
    from .world import World

    try:
        World(run.store, run.campaign).revert_session(run.input["session"])
    except KeyError:
        pass


def _transcript(run: Run) -> str:
    """The transcript text: the upload itself, or its transcription (made once, then kept)."""
    s = run.store
    if run.input.get("transcript_blob"):
        return s.blobs.get(run.input["transcript_blob"]).decode("utf-8", errors="replace")
    suffix = Path(run.input["name"]).suffix.lower()
    data = s.blobs.get(run.input["blob"])
    if suffix in AUDIO:
        from .transcribe import transcribe

        run.log.emit("status", "Transcribing audio…")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"audio{suffix}"
            path.write_bytes(data)
            text = transcribe(path)
        run.save_input(transcript_blob=s.blobs.put(text.encode("utf-8")))
        return text
    run.save_input(transcript_blob=run.input["blob"])
    return data.decode("utf-8", errors="replace")


def run_scan(run: Run) -> dict[str, Any]:
    """A character sheet read from a scan or PDF becomes a new character, owned by whoever uploaded it."""
    from . import sheetscan
    from .charsheet import CharacterSheet

    s, name = run.store, run.input["name"]
    if run.input.get("sheet") is None:
        run.log.emit("status", f"Claude is reading {name}…")
        try:
            sheet, unreadable = sheetscan.read_sheet(
                s.blobs.get(run.input["blob"]), name, check=run.check, on_thinking=lambda t: run.log.emit("text", t),
            )
        except sheetscan.ScanError as err:
            raise Permanent(str(err)) from err
        except sheetscan.anthropic.BadRequestError as err:  # the same request would be refused again
            raise Permanent(f"Claude's API refused the request: {err.message}") from err
        run.save_input(sheet=sheet.model_dump(), unreadable=unreadable)
    sheet = CharacterSheet.model_validate(run.input["sheet"])
    unreadable = run.input["unreadable"]
    if unreadable:  # on the sheet itself, where they get corrected
        note = "Not clear on the scan, check these: " + "; ".join(unreadable)
        sheet.notes = f"{sheet.notes}\n\n{note}".strip()
    run.check()  # last chance to stop before anything is written
    reason = f"read from {name} (job {run.job['id']})"
    try:
        character_id = s.create_character(sheet.model_dump(), run.campaign.id, run.job["requested_by"], reason=reason)
    except sqlite3.IntegrityError:
        same = s.find_character(sheet.name, run.campaign)
        if same is None or not any(e["reason"] == reason for e in s.character_log(same["id"], limit=1000)):
            raise Permanent(
                f"this campaign already has a character called {sheet.name}: rename or remove that one, then retry"
            ) from None
        character_id = same["id"]  # made by an earlier try of this job, which stopped before it could say so
    run.log.emit("status", f"Made {sheet.name}.")
    return {"character": character_id, "name": sheet.name, "unreadable": unreadable}


@dataclass(frozen=True)
class Kind:
    run: Callable[[Run], dict[str, Any]]
    undo: Callable[[Run], None] | None = None  # on cancel, take back what an attempt wrote


KINDS = {"ingest": Kind(run_ingest, undo_ingest), "scan": Kind(run_scan)}


# -- the loop ---------------------------------------------------------------------------


class JobLoop(LeasedLoop):
    # The name the ingest loop had before it ran other jobs: while a deploy has
    # both versions up, they share one lease and never run a job twice at once.
    LEASE = "ingest-loop"

    def pass_once(self) -> bool:
        s = Store(self.db)
        try:
            due = s.jobs_due(int(time.time()))
        finally:
            s.close()
        for job in due:
            if self.stopping or not self.holds_lease():
                return False
            self.process(job["id"])
            return True  # one at a time; the next pass looks again
        return False

    def _checker(self, job_id: int) -> Callable[[], None]:
        """Stop if cancelled, or if this server is shutting down or lost the lease. Cheap to call often."""
        last = 0.0

        def check() -> None:
            nonlocal last
            if self.stopping:
                raise JobStopped("handed over")
            if time.monotonic() - last < CHECK_SECONDS:
                return
            last = time.monotonic()
            if not self.holds_lease():
                raise JobStopped("handed over")
            s = Store(self.db)
            try:
                if s.job(job_id)["state"] != "running":
                    raise JobStopped("cancelled")
            finally:
                s.close()

        return check

    def process(self, job_id: int) -> None:
        s = Store(self.db)
        log = Log(self.db, job_id)
        try:
            job = s.job(job_id)
            attempt = job["attempts"] + 1
            s.update_job(job_id, state="running", attempts=attempt, error=None, retry_at=None)
            s.clear_job_log(job_id)
            if attempt > 1:
                log.emit("status", f"Attempt {attempt}: starting over.")
            kind = KINDS.get(job["kind"])
            campaign = replace(s.campaign_by_id(job["campaign_id"]), role=job["role"], access="editor")
            run = Run(s, job, campaign, log, self._checker(job_id))
            try:
                if kind is None:
                    raise Permanent(f"this server doesn't know how to do a {job['kind']} job")
                result = kind.run(run)
            except JobStopped as stop:
                cancelled = str(stop) == "cancelled"
                log.emit("status", "Cancelled." if cancelled else "Paused: picking up after a restart.")
                log.flush()
                if cancelled:
                    if kind and kind.undo:
                        kind.undo(run)
                else:
                    s.update_job(job_id, state="wanted", attempts=job["attempts"])  # not this attempt's fault
                return
            except BaseException as err:  # SystemExit from a missing audio extra too
                message = str(err) or type(err).__name__
                log.emit("error", message if isinstance(err, Permanent) else traceback.format_exc(limit=3))
                log.flush()
                if isinstance(err, Permanent):
                    s.update_job(job_id, state="failed", error=message)
                elif attempt < MAX_ATTEMPTS:
                    s.update_job(job_id, state="wanted", error=message,
                                 retry_at=int(time.time()) + RETRY_SECONDS * 4 ** (attempt - 1))
                else:
                    s.update_job(job_id, state="failed", error=f"{message} (gave up after {attempt} tries)")
                return
            log.flush()
            s.update_job(job_id, state="done", result=result)
        finally:
            s.close()
