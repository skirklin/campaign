"""Adding reference documents, and rebuilding them from their stored originals."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import digest
from .db import Campaign, Store
from .ingest import CHUNKER_VERSION, parse_bytes

REFERENCE_SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}


def add_reference(
    store: Store,
    campaign: Campaign,
    data: bytes,
    filename: str,
    title: str | None = None,
    kind: str = "module",
    dm_only: bool | None = None,
) -> dict[str, Any]:
    """Keep the original, chunk it, and plan its digest (no API calls).

    Returns the source id, chunk count and how many digest calls are pending,
    which is zero when the same text has been digested before.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in REFERENCE_SUFFIXES:
        raise ValueError("upload a .pdf, .md or .txt file")
    blob = store.blobs.put(data)
    doc = parse_bytes(data, filename)
    if not doc.chunks:
        raise ValueError("no text found (a scanned PDF needs OCR first)")
    source_id = store.add_source(
        campaign, title or Path(filename).stem, kind, doc,
        dm_only=(kind == "module") if dm_only is None else dm_only,
        blob=blob, filename=filename,
    )
    root = digest.plan(store, source_id)
    return {"id": source_id, "chunks": len(doc.chunks), "pending": digest.pending_calls(root),
            "estimate": digest.estimate(store, root)}


def reindex(store: Store, force: bool = False) -> list[dict[str, Any]]:
    """Re-chunk reference sources whose chunker version is out of date.

    Uses the stored original, keeps the source's id, and re-plans the digest,
    which reuses every section whose text didn't change.
    """
    done = []
    for src in store.all_sources():
        if src["kind"] == "transcript" or not src["blob"] or not store.blobs.exists(src["blob"]):
            continue
        if src["chunker_version"] >= CHUNKER_VERSION and not force:
            continue
        doc = parse_bytes(store.blobs.get(src["blob"]), src["filename"] or "upload.pdf")
        store.replace_content(src["id"], doc, CHUNKER_VERSION)
        root = digest.plan(store, src["id"])
        done.append({"id": src["id"], "title": src["title"], "chunks": len(doc.chunks),
                     "pending": digest.pending_calls(root)})
    return done
