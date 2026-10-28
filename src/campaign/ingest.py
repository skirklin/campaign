"""Turn reference documents into searchable chunks.

Chunks follow the document's own structure where possible: a new chunk
starts at every heading (detected from font size in PDFs, `#` lines in
Markdown), and long sections are split on paragraph boundaries. Keeping a
stat block or a room description in one chunk matters more than uniform
chunk size.
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path
from typing import Any, Iterator

MAX_CHARS = 2500


def _split_long(text: str, limit: int = MAX_CHARS) -> Iterator[str]:
    if len(text) <= limit:
        yield text
        return
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        if buf and len(buf) + len(para) > limit:
            yield buf.strip()
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        yield buf.strip()


def _emit(heading: str, text: str, page: int | None) -> Iterator[dict[str, Any]]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return
    for part in _split_long(text):
        yield {"heading": heading, "text": part, "page": page}


def chunk_markdown(text: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    trail: list[str] = []  # heading path, e.g. ["Chapter 2", "Death House"]
    buf: list[str] = []

    def flush() -> None:
        chunks.extend(_emit(" > ".join(trail), "\n".join(buf), None))
        buf.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            level = len(m.group(1))
            trail[:] = trail[: level - 1] + [m.group(2).strip()]
        else:
            buf.append(line)
    flush()
    return chunks


def chunk_pdf(path: Path) -> list[dict[str, Any]]:
    import pymupdf

    doc = pymupdf.open(path)

    # Section names from the PDF outline give every chunk on a page context
    # ("Chapter 4: Castle Ravenloft") even when the page itself has no heading.
    section_by_page: dict[int, str] = {}
    current = ""
    toc = sorted(doc.get_toc(simple=True), key=lambda t: t[2])
    toc_i = 0
    for pno in range(1, doc.page_count + 1):
        while toc_i < len(toc) and toc[toc_i][2] <= pno:
            current = toc[toc_i][1].strip()
            toc_i += 1
        section_by_page[pno] = current

    pages = [page.get_text("dict", sort=True) for page in doc]
    sizes = [
        round(span["size"], 1)
        for p in pages
        for block in p["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if span["text"].strip()
    ]
    body = statistics.median(sizes) if sizes else 10.0

    chunks: list[dict[str, Any]] = []
    for pno, p in enumerate(pages, start=1):
        section = section_by_page.get(pno, "")
        heading = ""
        buf: list[str] = []

        def flush() -> None:
            label = " > ".join(x for x in (section, heading) if x)
            chunks.extend(_emit(label, "\n".join(buf), pno))
            buf.clear()

        for block in p["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                if not text:
                    continue
                size = max(s["size"] for s in line["spans"])
                bold = all(s["flags"] & 16 for s in line["spans"] if s["text"].strip())
                is_heading = len(text) < 80 and (size >= body * 1.2 or (bold and size >= body * 1.05))
                if is_heading:
                    if buf:
                        flush()
                    heading = text
                else:
                    buf.append(text)
            buf.append("")  # block boundary -> paragraph break
        flush()
    return chunks


def chunk_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return chunk_pdf(path)
    if suffix in {".md", ".markdown", ".txt"}:
        return chunk_markdown(path.read_text(encoding="utf-8"))
    raise ValueError(f"don't know how to read {suffix} files (expected .pdf, .md or .txt)")
