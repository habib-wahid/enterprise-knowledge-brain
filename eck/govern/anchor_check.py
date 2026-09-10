"""BR-18 / BR-70 — validate curated anchors against freshly derived structure.

The rule the BRD states plainly: if an anchor no longer resolves, the refresh
must FAIL rather than serve stale meaning. Not warn. Not skip. Fail.

Four outcomes:
  resolved  target exists and its source span is byte-identical to what the
            reviewer approved
  stale     target exists but its span changed — the code was edited under a
            statement a human signed off on, so it needs re-review
  broken    the target is gone (renamed, moved, deleted) — the anchor points
            at nothing and the knowledge base must not be published
  orphaned  the wiki statement itself is gone or was rewritten
"""
from __future__ import annotations

from typing import Any

from ..curated import Anchor
from ..store.db import KnowledgeStore

RESOLVED, STALE, BROKEN, ORPHANED = "resolved", "stale", "broken", "orphaned"


class BrokenAnchors(SystemExit):
    """Refresh aborted because curated meaning points at code that is gone."""


def validate(store: KnowledgeStore, anchors: list[Anchor], run_id: str
             ) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for a in anchors:
        node = store.query(
            "SELECT id, kind, path, start_line, end_line, span_sha FROM node"
            " WHERE asset_id = ? AND fqn = ? LIMIT 1",
            (a.target_asset, a.target_fqn))

        chunk_present = bool(store.query(
            "SELECT 1 FROM chunk WHERE id = ? LIMIT 1", (a.chunk_id,)))

        if not node:
            state, n = BROKEN, None
        else:
            n = node[0]
            if n["span_sha"] == a.span_sha_at_approval:
                state = RESOLVED
            else:
                state = STALE

        # A missing statement is reported even when the code side is fine:
        # the anchor's two ends are independent.
        if state in (RESOLVED, STALE) and not chunk_present:
            state = ORPHANED

        rows.append(dict(
            id=a.id, chunk_id=a.chunk_id, statement=a.statement[:2000],
            statement_sha=a.statement_sha, doc_path=a.doc_path,
            doc_start_line=a.doc_start_line, doc_end_line=a.doc_end_line,
            target_asset=a.target_asset,
            target_kind=(n["kind"] if n else a.target_kind),
            target_fqn=a.target_fqn,
            target_node_id=(n["id"] if n else None),
            target_path=(n["path"] if n else a.target_path),
            target_start_line=(n["start_line"] if n else a.target_start_line),
            target_end_line=(n["end_line"] if n else a.target_end_line),
            span_sha_at_approval=a.span_sha_at_approval,
            span_sha_now=(n["span_sha"] if n else None),
            state=state, confidence=a.confidence,
            justification=a.justification, proposer=a.proposer,
            reviewed_by=a.reviewed_by, reviewed_at=a.reviewed_at,
            origin="curated", run_id=run_id))

    return rows


def enforce(rows: list[dict[str, Any]], allow_broken: bool = False) -> None:
    """BR-18: refuse to publish knowledge whose curated meaning has come loose."""
    broken = [r for r in rows if r["state"] == BROKEN]
    if not broken or allow_broken:
        return

    lines = [
        "",
        "REFRESH ABORTED — curated meaning points at code that no longer exists.",
        "",
        f"{len(broken)} anchor(s) could not be resolved (BR-18):",
        "",
    ]
    for r in broken[:20]:
        lines.append(f"  {r['target_asset']}  {r['target_fqn']}")
        lines.append(f"    approved by {r['reviewed_by']} on {r['reviewed_at']}")
        lines.append(f"    statement:  {r['doc_path']}:"
                     f"{r['doc_start_line']}-{r['doc_end_line']}")
        lines.append("")
    if len(broken) > 20:
        lines.append(f"  ... and {len(broken) - 20} more")
        lines.append("")
    lines += [
        "The knowledge base was NOT published. The previous one is untouched.",
        "",
        "Fix by re-pointing or retiring each anchor:",
        "    ./eck-cli anchors review        # re-review in the UI",
        "    ./eck-cli anchors list --state broken",
        "",
        "To publish anyway and mark this meaning unresolved, re-run with",
        "--allow-broken. Doing so serves knowledge the BRD says must not be",
        "served, so it should be a deliberate, temporary choice.",
    ]
    raise BrokenAnchors("\n".join(lines))


def summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    out = {RESOLVED: 0, STALE: 0, BROKEN: 0, ORPHANED: 0}
    for r in rows:
        out[r["state"]] = out.get(r["state"], 0) + 1
    return out


def stamp(store: KnowledgeStore, anchor: Anchor) -> bool:
    """Record the target's CURRENT span hash at the moment a human approves.

    This is what BR-18 later compares against. An anchor approved without a
    stamp can never go stale, which would quietly defeat the whole check —
    so approval fails rather than proceeding unstamped.
    """
    rows = store.query(
        "SELECT kind, path, start_line, end_line, span_sha FROM node"
        " WHERE asset_id = ? AND fqn = ? LIMIT 1",
        (anchor.target_asset, anchor.target_fqn))
    if not rows:
        return False
    n = rows[0]
    anchor.target_kind = n["kind"]
    anchor.target_path = n["path"]
    anchor.target_start_line = n["start_line"]
    anchor.target_end_line = n["end_line"]
    anchor.span_sha_at_approval = n["span_sha"]
    return True
