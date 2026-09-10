"""CAP-4 — resolve a curated process against freshly derived structure.

Each stage anchor is looked up by (asset, fqn) in the node table just
extracted. A stage anchor asserts "this stage lives here" — not a claim
about the body's content — so unlike CAP-3 anchors there is no span_sha to
compare and no 'stale' state: only 'resolved' or 'broken'.

Deliberately does NOT abort refresh the way BR-18 aborts on a broken CAP-3
anchor (see the schema.sql comment on process_stage_anchor for the reasoning
this rests on). A broken stage link is reported — in the process answer
itself, and in coverage — never silently dropped (BR-70).
"""
from __future__ import annotations

from typing import Any

from .. import process_register as preg
from ..store.db import KnowledgeStore, sha256

RESOLVED, BROKEN, UNANCHORED = "resolved", "broken", "unanchored"


def _find(store: KnowledgeStore, asset_id: str, fqn: str):
    rows = store.query(
        "SELECT id, kind, path, start_line, end_line FROM node"
        " WHERE asset_id = ? AND fqn = ? LIMIT 1", (asset_id, fqn))
    return rows[0] if rows else None


def validate_process(store: KnowledgeStore, process: preg.Process, run_id: str
                     ) -> dict[str, list[dict[str, Any]]]:
    """Returns rows ready for store.add_process(...): process, stages,
    stage_anchors, failures."""
    proc_row = dict(
        id=process.id, name=process.name, description=process.description,
        origin="curated", authored_by=process.authored_by,
        authored_at=process.authored_at, run_id=run_id)

    stage_rows, anchor_rows, failure_rows = [], [], []

    for ordinal, stage in enumerate(process.stages):
        stage_id = f"{process.id}::{stage.key}"
        stage_rows.append(dict(
            id=stage_id, process_id=process.id, ordinal=ordinal,
            stage_key=stage.key, name=stage.name, description=stage.description,
            is_entry=1 if stage.is_entry else 0,
            entry_trigger=stage.entry_trigger, run_id=run_id))

        for a in stage.anchors:
            node = _find(store, a.asset_id, a.fqn)
            aid = sha256(f"{stage_id}|{a.asset_id}|{a.fqn}".encode())[:16]
            if node:
                anchor_rows.append(dict(
                    id=aid, stage_id=stage_id, target_asset=a.asset_id,
                    target_fqn=a.fqn, target_node_id=node["id"],
                    target_kind=node["kind"], target_path=node["path"],
                    target_start_line=node["start_line"],
                    target_end_line=node["end_line"],
                    state=RESOLVED, run_id=run_id))
            else:
                anchor_rows.append(dict(
                    id=aid, stage_id=stage_id, target_asset=a.asset_id,
                    target_fqn=a.fqn, target_node_id=None, target_kind=None,
                    target_path=None, target_start_line=None,
                    target_end_line=None, state=BROKEN, run_id=run_id))

    stage_ids = {s.key: f"{process.id}::{s.key}" for s in process.stages}
    for f in process.failures:
        stage_id = stage_ids[f.stage_key]
        fid = sha256(f"{stage_id}|{f.description}".encode())[:16]
        if not f.anchor_fqn:
            failure_rows.append(dict(
                id=fid, stage_id=stage_id, description=f.description,
                target_fqn=None, target_node_id=None, target_path=None,
                target_start_line=None, state=UNANCHORED, run_id=run_id))
            continue
        node = _find(store, f.anchor_asset, f.anchor_fqn)
        if node:
            failure_rows.append(dict(
                id=fid, stage_id=stage_id, description=f.description,
                target_fqn=f.anchor_fqn, target_node_id=node["id"],
                target_path=node["path"], target_start_line=node["start_line"],
                state=RESOLVED, run_id=run_id))
        else:
            failure_rows.append(dict(
                id=fid, stage_id=stage_id, description=f.description,
                target_fqn=f.anchor_fqn, target_node_id=None,
                target_path=None, target_start_line=None,
                state=BROKEN, run_id=run_id))

    return dict(process=[proc_row], stages=stage_rows,
               anchors=anchor_rows, failures=failure_rows)


def summary(anchor_rows: list[dict[str, Any]]) -> dict[str, int]:
    out = {RESOLVED: 0, BROKEN: 0}
    for r in anchor_rows:
        out[r["state"]] = out.get(r["state"], 0) + 1
    return out
