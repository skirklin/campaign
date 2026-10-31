"""Turn reference documents into a section tree of searchable chunks.

Chunks follow the document's own structure: a new chunk starts at every
heading (detected from font size in PDFs, `#` lines in Markdown), and long
sections are split on paragraph boundaries. Keeping a stat block or a room
description in one chunk matters more than uniform chunk size.

Each chunk also belongs to a node in the section tree: the PDF outline
(bookmarks) when there is one, Markdown headings, or fixed page ranges as a
last resort. The digest walks this tree.

Bump CHUNKER_VERSION when chunking output changes; `campaign reindex`
re-chunks stored originals that were made by an older version.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

CHUNKER_VERSION = 3  # 3: Markdown footnotes inlined where they're cited
MAX_CHARS = 2500
PAGES_PER_GROUP = 12  # section size for PDFs without an outline


@dataclass
class ParsedDoc:
    # nodes: {"title", "level", "parent" (index or None), "page_start", "page_end"}
    nodes: list[dict[str, Any]] = field(default_factory=list)
    # chunks: {"heading", "text", "page", "node" (index or None)}
    chunks: list[dict[str, Any]] = field(default_factory=list)


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


def _emit(heading: str, text: str, page: int | None, node: int | None) -> Iterator[dict[str, Any]]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return
    for part in _split_long(text):
        yield {"heading": heading, "text": part, "page": page, "node": node}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _parent_for(nodes: list[dict[str, Any]], level: int) -> int | None:
    for i in range(len(nodes) - 1, -1, -1):
        if nodes[i]["level"] < level:
            return i
    return None


_FOOTNOTE_DEF = re.compile(r"^\[\^([^\]]+)\]:\s?(.*)$")


def inline_footnotes(text: str) -> str:
    """Put each Markdown footnote's text where it's cited, and drop the definitions.

    A footnote's definition usually sits at the end of the document, far from
    the passage that cites it ("the bone knight[^1] stat block"), so a chunk
    with the citation would only carry an unexplained marker. Inlined, the
    chunk says "the bone knight (Source: Eberron: Rising from the Last War)".
    """
    notes: dict[str, str] = {}
    kept: list[str] = []
    last: str | None = None
    for line in text.splitlines():
        if m := _FOOTNOTE_DEF.match(line):
            last = m.group(1)
            notes[last] = m.group(2).strip()
            continue
        if last is not None and line.startswith(("    ", "\t")) and line.strip():
            notes[last] += " " + line.strip()  # a definition's continuation line
            continue
        last = None
        kept.append(line)
    if not notes:
        return text
    cite = re.compile(r"\[\^(" + "|".join(re.escape(k) for k in notes) + r")\]")
    return "\n".join(cite.sub(lambda m: f" ({notes[m.group(1)]})", line) for line in kept)


def parse_markdown(text: str) -> ParsedDoc:
    doc = ParsedDoc()
    text = inline_footnotes(text)
    trail: list[str] = []  # heading path, e.g. ["Chapter 2", "Death House"]
    buf: list[str] = []
    current: int | None = None

    def flush() -> None:
        doc.chunks.extend(_emit(" > ".join(trail), "\n".join(buf), None, current))
        buf.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            trail[:] = trail[: level - 1] + [title]
            doc.nodes.append({
                "title": title, "level": level, "parent": _parent_for(doc.nodes, level),
                "page_start": None, "page_end": None,
            })
            current = len(doc.nodes) - 1
        else:
            buf.append(line)
    flush()
    return doc


def chunk_markdown(text: str) -> list[dict[str, Any]]:
    return parse_markdown(text).chunks


def _pdf_chunks(doc: Any) -> list[dict[str, Any]]:
    """Chunks with page and local heading, before section assignment."""
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
        heading = ""
        buf: list[str] = []

        def flush() -> None:
            chunks.extend(_emit(heading, "\n".join(buf), pno, None))
            buf.clear()

        for block in p["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                if not text:
                    continue
                size = max(s["size"] for s in line["spans"])
                bold = all(s["flags"] & 16 for s in line["spans"] if s["text"].strip())
                if len(text) < 80 and (size >= body * 1.2 or (bold and size >= body * 1.05)):
                    if buf:
                        flush()
                    heading = text
                else:
                    buf.append(text)
            buf.append("")  # block boundary -> paragraph break
        flush()
    return chunks


def _assign_sections(chunks: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> None:
    """Attach each chunk to the outline entry it falls under.

    A section usually starts partway down a page, so on a section's first
    page the switch happens at the chunk whose heading matches the outline
    title. If no heading on that page matches, the whole page goes to the
    new section.
    """
    def matches(chunk: dict[str, Any], node: dict[str, Any]) -> bool:
        a, b = _norm(chunk["heading"]), _norm(node["title"])
        return bool(a and b) and (a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)))

    switch_at: list[int] = []  # chunk index where each node starts
    for node in nodes:
        on_page = [i for i, c in enumerate(chunks) if c["page"] == node["page_start"]]
        later = [i for i, c in enumerate(chunks) if c["page"] > node["page_start"]]
        hit = next((i for i in on_page if matches(chunks[i], node)), None)
        start = hit if hit is not None else (on_page[0] if on_page else (later[0] if later else len(chunks)))
        # Outline order must stay monotonic even if a heading matched oddly.
        switch_at.append(max(start, switch_at[-1]) if switch_at else start)

    n = 0
    current: int | None = None
    for i, chunk in enumerate(chunks):
        while n < len(nodes) and switch_at[n] <= i:
            current = n
            n += 1
        chunk["node"] = current
        section = nodes[current]["title"] if current is not None else ""
        chunk["heading"] = " > ".join(x for x in (section, chunk["heading"]) if x)


def parse_pdf(path: Path) -> ParsedDoc:
    import pymupdf

    pdf = pymupdf.open(path)
    chunks = _pdf_chunks(pdf)
    nodes: list[dict[str, Any]] = []
    toc = [(lvl, title.strip(), page) for lvl, title, page in pdf.get_toc(simple=True) if page >= 1]
    toc.sort(key=lambda t: t[2])  # stable: keeps outline order within a page
    if toc:
        for lvl, title, page in toc:
            nodes.append({"title": title, "level": lvl, "parent": _parent_for(nodes, lvl), "page_start": page})
    else:
        for start in range(1, pdf.page_count + 1, PAGES_PER_GROUP):
            end = min(start + PAGES_PER_GROUP - 1, pdf.page_count)
            nodes.append({"title": f"Pages {start}–{end}", "level": 1, "parent": None, "page_start": start})
    # A node ends where the next node at the same or a higher level begins.
    for i, node in enumerate(nodes):
        nxt = next((m for m in nodes[i + 1:] if m["level"] <= node["level"]), None)
        node["page_end"] = max(node["page_start"], nxt["page_start"] - 1 if nxt else pdf.page_count)
    _assign_sections(chunks, nodes)
    return ParsedDoc(nodes=nodes, chunks=chunks)


def parse_file(path: Path) -> ParsedDoc:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix in {".md", ".markdown", ".txt"}:
        return parse_markdown(path.read_text(encoding="utf-8"))
    raise ValueError(f"don't know how to read {suffix} files (expected .pdf, .md or .txt)")


def parse_bytes(data: bytes, filename: str) -> ParsedDoc:
    import tempfile

    suffix = Path(filename).suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        return parse_markdown(data.decode("utf-8", errors="replace"))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"upload{suffix}"
        path.write_bytes(data)
        return parse_file(path)


def chunk_file(path: Path) -> list[dict[str, Any]]:
    return parse_file(path).chunks
