"""Background loops that converge the database toward what it asks for.

Digests and jobs (session ingests, sheet scans) are declared as targets in
the database and carried out by a loop that keeps no state of its own, so a
restart or a deploy just means the next pass starts from the database
again. While a
rolling deploy has two servers up, a lease makes sure only one runs each
loop; it's handed over on shutdown and expires if a server dies holding it.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from .db import Store


class LeasedLoop:
    """Runs `pass_once` over and over in a thread, while holding the lease `LEASE`.

    Subclasses set LEASE and implement pass_once(), returning True if it did
    work (then it runs again at once) or False (then it waits IDLE_SECONDS,
    or until woken).
    """

    LEASE = ""
    IDLE_SECONDS = 30
    LEASE_SECONDS = 300  # how long a loop that died without letting go blocks the others
    BUSY_RETRY_SECONDS = 10  # how soon to try again when another server holds the lease

    def __init__(self, db: Path | str):
        self.db = db
        # Which copy of the server this is: while two overlap in a deploy, only the lease holder works.
        self.holder = f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def pass_once(self) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.LEASE, daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 20) -> None:
        """Stop, and hand the lease over at once rather than leaving the next server to wait it out.

        Work still in flight after `timeout` is abandoned (the process is
        going away anyway); the next loop just does it again.
        """
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        s = Store(self.db)
        try:
            s.release_lease(self.LEASE, self.holder)
        finally:
            s.close()

    def wake(self) -> None:
        """Look again now (a target changed), rather than at the next idle tick."""
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def holds_lease(self) -> bool:
        """Take or renew the lease; False if another server has it. Call before each costly step.

        Never once stopping: the lease has been (or is about to be) handed to the next server.
        """
        if self._stop.is_set():
            return False
        s = Store(self.db)
        try:
            return s.take_lease(self.LEASE, self.holder, self.LEASE_SECONDS, int(time.time()))
        finally:
            s.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if not self.holds_lease():  # another server is on it; wait our turn
                    self._wake.wait(self.BUSY_RETRY_SECONDS)
                    continue
                busy = self.pass_once()
            except Exception:  # keep the loop alive; the next pass starts from the database again
                traceback.print_exc()
                busy = False
            if not busy:
                self._wake.wait(self.IDLE_SECONDS)
