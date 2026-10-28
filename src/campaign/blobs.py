"""Content-addressed storage for original uploads.

Files are stored once under their SHA-256, so the same PDF uploaded to two
campaigns takes the space of one, and a source can always be re-chunked
from its original when the chunker improves.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        digest = sha256(data)
        dest = self.path(digest)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename so a crash never leaves a truncated blob under a valid name.
            fd, tmp = tempfile.mkstemp(dir=dest.parent)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        return digest

    def get(self, digest: str) -> bytes:
        return self.path(digest).read_bytes()

    def exists(self, digest: str) -> bool:
        return self.path(digest).exists()

    def stats(self) -> dict[str, int]:
        files = [p for p in self.root.glob("*/*") if p.is_file()] if self.root.exists() else []
        return {"files": len(files), "bytes": sum(p.stat().st_size for p in files)}
