"""CAP-5 — turn derived structure into retrievable code chunks (BR-34).

Chunks are cut at semantic boundaries (a method, a type declaration) rather
than at a fixed character window, so a retrieval hit is always a thing a
person can be pointed at, with an exact line range (BR-35).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..store.db import sha256
from ..store.vocab import Origin

# Types whose declaration carries meaning worth indexing on its own.
TYPE_KINDS = ("entity", "view", "service", "security_role", "class", "interface")
MEMBER_KINDS = ("method", "event_handler")

MAX_CHARS = 2400          # keeps embedding fast; long bodies are truncated
MIN_CHARS = 40


def _header(kind: str, fqn: str, signature: str | None,
            attrs: dict[str, Any]) -> str:
    """A short natural-language-ish preamble so semantic search has purchase.

    Raw Java embeds poorly against a business-language question. Saying
    'view EmplSalaryListView route hr_EmplSalary.list' in words gives the
    embedding something to match 'where do I see employee salaries'.
    """
    bits = [f"{kind} {fqn}"]
    if signature:
        bits.append(signature)
    if attrs.get("table"):
        bits.append(f"database table {attrs['table']}")
    if attrs.get("route"):
        bits.append(f"screen route {attrs['route']}")
    if attrs.get("edits"):
        bits.append(f"edits entity {attrs['edits']}")
    if attrs.get("role_code"):
        bits.append(f"security role {attrs['role_code']}")
    if attrs.get("subscribes_to"):
        bits.append(f"handles event on {attrs['subscribes_to']}")
    if attrs.get("transactional"):
        bits.append("transactional")
    ann = attrs.get("annotations") or []
    if ann:
        bits.append("annotated " + " ".join(f"@{a}" for a in ann[:6]))
    return " | ".join(bits)


def chunk_code(store, asset_paths: dict[str, Path], run_id: str
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build code chunks from nodes already in the store."""
    import json

    kinds = TYPE_KINDS + MEMBER_KINDS
    placeholders = ",".join("?" * len(kinds))
    rows = store.query(
        f"SELECT id, asset_id, kind, name, fqn, signature, path, start_line,"
        f" end_line, attrs FROM node WHERE kind IN ({placeholders})"
        f" ORDER BY id", kinds)

    chunks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    file_cache: dict[tuple[str, str], list[str]] = {}

    for r in rows:
        key = (r["asset_id"], r["path"])
        if key not in file_cache:
            full = asset_paths[r["asset_id"]] / r["path"]
            try:
                file_cache[key] = full.read_text(encoding="utf-8",
                                                 errors="replace").splitlines()
            except Exception as exc:
                file_cache[key] = []
                failures.append(dict(
                    asset_id=r["asset_id"], path=r["path"],
                    reason="chunk_read_error",
                    detail=f"{type(exc).__name__}: {exc}", run_id=run_id))
        lines = file_cache[key]
        if not lines:
            continue

        body = "\n".join(lines[r["start_line"] - 1:r["end_line"]])
        if r["kind"] in TYPE_KINDS:
            # For a type, index the declaration and its annotations, not the
            # entire class — members are indexed separately.
            body = "\n".join(body.splitlines()[:40])
        body = body[:MAX_CHARS]

        attrs = json.loads(r["attrs"])
        text = _header(r["kind"], r["fqn"], r["signature"], attrs) + "\n\n" + body
        if len(text) < MIN_CHARS:
            continue

        chunks.append(dict(
            id=sha256(f"code|{r['id']}".encode())[:16],
            asset_id=r["asset_id"], source="code", doc_id=None,
            node_id=r["id"], heading=r["fqn"], text=text, path=r["path"],
            start_line=r["start_line"], end_line=r["end_line"],
            span_sha=sha256(text.encode()),
            origin=Origin.DERIVED.value, run_id=run_id))

    return chunks, failures
