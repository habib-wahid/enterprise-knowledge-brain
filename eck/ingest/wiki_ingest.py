"""CAP-3 source — ingest the GitHub wiki as CURATED knowledge.

A GitHub wiki is a git repository, so its commit history is already the
authored/dated version history BR-19 asks for. We record it per page rather
than inventing a separate curation store.

Nothing here is treated as fact about the code. Wiki statements become
curated MEANING; they only become anchored knowledge in M3, after a human
approves the link to a specific software location (BR-16, BR-21).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..register import Asset
from ..store.db import sha256
from ..store.vocab import Origin

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# Wiki pages are full of embedded images and edit-links; they carry no
# meaning for retrieval and would otherwise dominate short chunks.
IMAGE = re.compile(r"<img[^>]*>|!\[[^\]]*\]\([^)]*\)")
EDIT_LINK = re.compile(r"/_edit#[\w-]*")

MIN_CHUNK_CHARS = 80          # below this a chunk is a heading with no content


@dataclass
class WikiChunk:
    heading_path: str
    text: str
    start_line: int
    end_line: int


def _git_page_meta(repo: Path, rel: str) -> tuple[str | None, str | None, str | None]:
    """Last commit, author and date for one page — BR-19."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H%x1f%an%x1f%aI",
             "--", rel],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None, None, None
        commit, author, date = out.stdout.strip().split("\x1f")
        return commit, author, date
    except Exception:
        return None, None, None


def split_page(markdown: str) -> list[WikiChunk]:
    """Chunk by heading, carrying the heading path as context.

    A chunk's text keeps its heading path prefixed, so a retrieved fragment
    still says what part of what process it belongs to.
    """
    lines = markdown.splitlines()
    stack: list[tuple[int, str]] = []
    chunks: list[WikiChunk] = []
    buf: list[str] = []
    buf_start = 1
    current_path = ""

    def flush(end_line: int) -> None:
        body = "\n".join(buf).strip()
        body = IMAGE.sub("", body)
        body = EDIT_LINK.sub("", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if len(body) >= MIN_CHUNK_CHARS and current_path:
            chunks.append(WikiChunk(current_path, f"{current_path}\n\n{body}",
                                    buf_start, end_line))

    for i, line in enumerate(lines, start=1):
        m = HEADING.match(line)
        if not m:
            buf.append(line)
            continue
        flush(i - 1)
        level, title = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        current_path = " > ".join(t for _, t in stack)
        buf, buf_start = [], i

    flush(len(lines))
    return chunks


def ingest(asset: Asset, run_id: str) -> tuple[list[dict[str, Any]],
                                               list[dict[str, Any]],
                                               list[dict[str, Any]]]:
    """Return (docs, chunks, failures) for one wiki asset."""
    docs: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for page in sorted(asset.abs_path.glob("*.md")):
        rel = page.name
        try:
            markdown = page.read_text(encoding="utf-8")
        except Exception as exc:
            failures.append(dict(asset_id=asset.id, path=rel,
                                 reason="read_error",
                                 detail=f"{type(exc).__name__}: {exc}",
                                 run_id=run_id))
            continue

        title = page.stem.replace("-", " ").replace("‐", "-")
        doc_id = sha256(f"{asset.id}|{rel}".encode())[:16]
        commit, author, date = _git_page_meta(asset.abs_path, rel)

        docs.append(dict(id=doc_id, asset_id=asset.id, title=title, path=rel,
                         last_commit=commit, last_author=author,
                         last_commit_date=date,
                         origin=Origin.CURATED.value, run_id=run_id))

        page_chunks = split_page(markdown)
        if not page_chunks:
            failures.append(dict(asset_id=asset.id, path=rel,
                                 reason="no_extractable_content",
                                 detail="page has no heading with body text",
                                 run_id=run_id))
        for c in page_chunks:
            cid = sha256(f"{doc_id}|{c.heading_path}|{c.start_line}".encode())[:16]
            chunks.append(dict(
                id=cid, asset_id=asset.id, source="wiki", doc_id=doc_id,
                node_id=None, heading=c.heading_path, text=c.text, path=rel,
                start_line=c.start_line, end_line=c.end_line,
                span_sha=sha256(c.text.encode()),
                origin=Origin.CURATED.value, run_id=run_id))

    return docs, chunks, failures
