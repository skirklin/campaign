"""Session ingests as targets: "session N should be what this transcript says".

Asking for an ingest adds a row (Store.add_ingest); IngestLoop works through
them oldest first, in the background, whichever server is up. An ingest is
safe to redo from the start (it replaces the session's earlier recording,
and the transcript's source by title), so the loop needs no memory of its
own: one interrupted by a deploy or a crash is simply run again. Progress is
written to ingest_log, so any server can show it.

Failures retry after 1 and then 4 minutes, then wait to be retried by hand.
Cancelling stops at the next tool call and undoes what that attempt wrote.
"""

from __future__ import annotations

import tempfile
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

from .db import Store
from .loops import LeasedLoop

AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".opus", ".webm"}
MAX_ATTEMPTS = 3
RETRY_SECONDS = 60
FLUSH_SECONDS = 0.5


class IngestStopped(Exception):
    """The ingest was cancelled, or this server is handing its work over."""


class Log:
    """Writes an ingest's progress to the database, merging streamed text into lines."""

    def __init__(self, db: Path | str, ingest_id: int):
        self.db, self.ingest_id = db, ingest_id
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
            s.add_ingest_log(self.ingest_id, self.pending, replace_last=replace_last)
        finally:
            s.close()
        self.growing = self.pending[-1][0] == "text"
        self.pending = []
        self.last_flush = time.monotonic()


class IngestLoop(LeasedLoop):
    LEASE = "ingest-loop"

    def pass_once(self) -> bool:
        s = Store(self.db)
        try:
            due = s.ingests_due(int(time.time()))
        finally:
            s.close()
        for ing in due:
            if self.stopping or not self.holds_lease():
                return False
            self.process(ing["id"])
            return True  # one at a time; the next pass looks again
        return False

    def _check(self, ingest_id: int) -> None:
        """Between tool calls: stop if cancelled, or if this server is shutting down or lost the lease."""
        if self.stopping or not self.holds_lease():
            raise IngestStopped("handed over")
        s = Store(self.db)
        try:
            if s.ingest(ingest_id)["state"] != "running":
                raise IngestStopped("cancelled")
        finally:
            s.close()

    def process(self, ingest_id: int) -> None:
        from .agent import describe_tool_call, ingest_session
        from .world import World

        s = Store(self.db)
        log = Log(self.db, ingest_id)
        try:
            ing = s.ingest(ingest_id)
            attempt = ing["attempts"] + 1
            s.update_ingest(ingest_id, state="running", attempts=attempt, error=None, retry_at=None)
            s.clear_ingest_log(ingest_id)
            if attempt > 1:
                log.emit("status", f"Attempt {attempt}: starting over from the transcript.")
            campaign = replace(s.campaign_by_id(ing["campaign_id"]), role=ing["role"], access="editor")
            try:
                transcript = self._transcript(s, ing, log)
                log.emit("status", "Reading the transcript and updating the campaign…")

                def on_tool(name: str, args: dict[str, Any]) -> None:
                    self._check(ingest_id)
                    log.emit("tool", describe_tool_call(name, args).strip(" ·"))

                result = ingest_session(
                    s, campaign, transcript, number=ing["session"], played_on=ing["played_on"],
                    on_text=lambda t: log.emit("text", t), on_tool=on_tool,
                )
            except IngestStopped as stop:
                log.emit("status", "Cancelled." if str(stop) == "cancelled" else "Paused: picking up after a restart.")
                log.flush()
                if str(stop) == "cancelled":
                    try:  # undo what this attempt wrote
                        World(s, campaign).revert_session(ing["session"])
                    except KeyError:
                        pass
                else:
                    s.update_ingest(ingest_id, state="wanted", attempts=ing["attempts"])  # not this attempt's fault
                return
            except BaseException as err:  # SystemExit from a missing audio extra too
                message = str(err) or type(err).__name__
                log.emit("error", traceback.format_exc(limit=3))
                log.flush()
                if attempt < MAX_ATTEMPTS:
                    s.update_ingest(ingest_id, state="wanted", error=message,
                                    retry_at=int(time.time()) + RETRY_SECONDS * 4 ** (attempt - 1))
                else:
                    s.update_ingest(ingest_id, state="failed", error=f"{message} (gave up after {attempt} tries)")
                return
            log.flush()
            s.update_ingest(ingest_id, state="done", result=result)
        finally:
            s.close()

    @staticmethod
    def _transcript(s: Store, ing: dict[str, Any], log: Log) -> str:
        """The transcript text: the upload itself, or its transcription (made once, then kept)."""
        if ing["transcript_blob"]:
            return s.blobs.get(ing["transcript_blob"]).decode("utf-8", errors="replace")
        suffix = Path(ing["input_name"]).suffix.lower()
        data = s.blobs.get(ing["input_blob"])
        if suffix in AUDIO:
            from .transcribe import transcribe

            log.emit("status", "Transcribing audio…")
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"audio{suffix}"
                path.write_bytes(data)
                text = transcribe(path)
            s.update_ingest(ing["id"], transcript_blob=s.blobs.put(text.encode("utf-8")))
            return text
        s.update_ingest(ing["id"], transcript_blob=ing["input_blob"])
        return data.decode("utf-8", errors="replace")
