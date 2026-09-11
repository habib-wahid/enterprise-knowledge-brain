"""The ECK web service (CAP-7 + CAP-8 over HTTP, BR-64).

One FastAPI app serving:
  /                 the web interface
  /api/services/*   every registered answer service, generated from the registry
  /api/...          estate, coverage, status, search, anchors, source, audit

There is no per-service code below: service routes are produced by walking
the registry, so a service added there appears here automatically and cannot
drift from the CLI. That is BR-51 and BR-64 holding by construction.

Read-only except for one endpoint: anchor review decisions, which write to
the curated store (never to the estate). BR-53 concerns the software being
described — the platform still never writes to registered source.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import curated, register
from ..govern import anchor_check, coverage
from ..services.registry import REGISTRY, catalogue, get, load_all
from ..store import source as src
from ..services.resolve import Context
from ..store.db import KnowledgeStore, utc_now

STATIC = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parent.parent.parent


def create_app(db_path: Path) -> FastAPI:
    load_all()
    app = FastAPI(title="Enterprise Code Knowledge Platform", version="0.5.0")
    audit_log: list[dict[str, Any]] = []

    @app.middleware("http")
    async def audit(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):          # BR-71
            audit_log.append({
                "at": utc_now(),
                "caller": request.client.host if request.client else "unknown",
                "client": request.headers.get("user-agent", "")[:100],
                "path": request.url.path,
                "query": dict(request.query_params),
                "status": response.status_code})
        return response

    # ---------------------------------------------------------------- UI

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---------------------------------------------------------------- meta

    @app.get("/api/catalogue")
    def api_catalogue():
        """BR-63 — service discovery."""
        return catalogue()

    @app.get("/api/coverage")
    def api_coverage():
        """BR-13, BR-69, BR-72 — what is known and what is not."""
        return coverage.data(db_path)

    @app.get("/api/estate")
    def api_estate():
        """CAP-1 — what is in scope and why (BR-05)."""
        reg = register.load(ROOT / "register" / "estate.yaml", ROOT)
        store = KnowledgeStore(db_path)
        counts = {r["asset_id"]: r["n"] for r in store.query(
            "SELECT asset_id, COUNT(*) n FROM node GROUP BY asset_id")}
        kinds = {}
        for r in store.query("SELECT asset_id, kind, COUNT(*) n FROM node"
                             " GROUP BY asset_id, kind"):
            kinds.setdefault(r["asset_id"], {})[r["kind"]] = r["n"]
        store.close()
        return {
            "estate": {"id": reg.estate_id, "name": reg.estate_name,
                       "platform": reg.platform,
                       "register_sha": reg.register_sha},
            "assets": [{
                "id": a.id, "name": a.name, "role": a.role, "owner": a.owner,
                "tech": a.tech, "source_kind": a.source_kind,
                "path": str(a.abs_path), "commit": a.source_commit,
                "nodes": counts.get(a.id, 0), "kinds": kinds.get(a.id, {}),
            } for a in reg.assets],
            "exclusions": [{"glob": e.path_glob, "reason": e.reason}
                           for e in (reg.assets[0].exclusions if reg.assets else [])],
        }

    @app.get("/api/audit")
    def api_audit():
        """BR-71 — the access log, visible rather than hidden."""
        return audit_log[-300:][::-1]

    # ---------------------------------------------------------------- search

    @app.get("/api/search")
    def api_search(q: str, source: str = None, asset: str = None,
                   limit: int = 10):
        """CAP-5 (BR-32..BR-36)."""
        from ..services.retrieval import verdict
        ctx = Context(db_path)
        try:
            hits = ctx.retriever.search(q, limit=int(limit),
                                        source=source or None,
                                        asset_id=asset or None)
            return {"question": q, "verdict": verdict(hits), "hits": [{
                "chunk_id": h.chunk_id, "asset": h.asset_id, "source": h.source,
                "origin": h.origin, "heading": h.heading, "path": h.path,
                "start_line": h.start_line, "end_line": h.end_line,
                "matched_by": h.matched_by,
                "similarity": round(h.similarity, 3) if h.similarity else None,
                "excerpt": h.text[:700]} for h in hits]}
        finally:
            ctx.close()

    @app.get("/api/answer")
    def api_answer(q: str, source: str = None, asset: str = None,
                   limit: int = 8):
        """Retrieval PLUS a synthesised paragraph — for the web UI's Search
        view. Not a CAP-7 service (see services/synthesize.py): this is the
        one place in the platform that calls a model directly, and it is
        additive — the raw hits are always returned alongside the summary,
        never replaced by it, so a synthesis failure degrades to exactly
        today's search experience rather than to a broken page.
        """
        from ..services.retrieval import verdict
        from ..services.synthesize import synthesize
        ctx = Context(db_path)
        try:
            hits = ctx.retriever.search(q, limit=int(limit),
                                        source=source or None,
                                        asset_id=asset or None)
            v = verdict(hits)
            result = synthesize(q, hits, v)
            return {
                "question": q, "verdict": v,
                "summary": result.summary, "summary_model": result.model,
                "summary_unavailable": result.error,
                "hits": [{
                    "chunk_id": h.chunk_id, "asset": h.asset_id,
                    "source": h.source, "origin": h.origin,
                    "heading": h.heading, "path": h.path,
                    "start_line": h.start_line, "end_line": h.end_line,
                    "matched_by": h.matched_by,
                    "similarity": round(h.similarity, 3) if h.similarity else None,
                    "excerpt": h.text[:700]} for h in hits]}
        finally:
            ctx.close()

    # ---------------------------------------------------------------- services

    @app.get("/api/services/{service_id}")
    def api_service(service_id: str, request: Request):
        try:
            spec = get(service_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        kwargs = {k: v for k, v in request.query_params.items()
                  if k in spec.inputs and v != ""}
        missing = [r for r in spec.required if r not in kwargs]
        if missing:
            raise HTTPException(422, {"error": "missing required argument(s)",
                                      "missing": missing,
                                      "usage": spec.signature(),
                                      "inputs": spec.inputs})
        ctx = Context(db_path)
        try:
            result = spec.fn(ctx, **kwargs)
        finally:
            ctx.close()
        return JSONResponse(json.loads(json.dumps(result.to_dict(), default=str)))

    # ---------------------------------------------------------------- source

    @app.get("/api/source")
    def api_source(asset: str, path: str, start: int = 1, end: int = 0):
        """BR-38 — the full content of a specific source location."""
        store = KnowledgeStore(db_path)
        rows = store.query(
            "SELECT abs_path, rel_path FROM asset WHERE id = ?", (asset,))
        if not rows:
            store.close()
            raise HTTPException(404, f"no registered asset {asset!r}")

        asset_dir = src.resolve_asset_dir(rows[0]["abs_path"], rows[0]["rel_path"])
        end = end or 10 ** 9
        lines, how = src.read_span(asset_dir, path, start, end)

        if how == "filesystem":
            store.close()
            lo = max(1, start)
            return {"asset": asset, "path": path, "start_line": lo,
                    "end_line": lo + len(lines) - 1, "from": "filesystem",
                    "lines": [{"n": lo + i, "text": t}
                              for i, t in enumerate(lines)]}

        # No source on this machine — serve what the index captured, and say so.
        text = src.indexed_text(store, asset, path, start)
        store.close()
        if text is None:
            raise HTTPException(404, {
                "error": "source not available on this deployment",
                "detail": f"{path} is not on disk and no indexed text covers "
                          f"line {start}",
                "fix": "mount the estate checkout and set ECK_SOURCES_ROOT, "
                       "or rebuild the knowledge base where the source lives"})
        body = text.splitlines()
        return {"asset": asset, "path": path, "start_line": start,
                "end_line": start + len(body) - 1, "from": "index",
                "note": "Source is not checked out on this deployment; this is "
                        "the text captured when the knowledge base was built.",
                "lines": [{"n": start + i, "text": t}
                          for i, t in enumerate(body)]}

    # ---------------------------------------------------------------- anchors

    @app.get("/api/anchors")
    def api_anchors(state: str = "proposed", limit: int = 40):
        if state == "proposed":
            rows = sorted(curated.load_candidates(),
                          key=lambda a: -a.confidence)[:int(limit)]
        else:
            rows = [a for a in curated._read(curated.ANCHORS)
                    if state == "all" or a.status == state][:int(limit)]
        store = KnowledgeStore(db_path)
        live = {r["id"]: r["state"] for r in store.query(
            "SELECT id, state FROM anchor")}
        store.close()
        return {
            "counts": {
                "proposed": len(curated.load_candidates()),
                "approved": len(curated.load_approved()),
                "rejected": len([a for a in curated._read(curated.ANCHORS)
                                 if a.status == "rejected"])},
            "anchors": [{
                "id": a.id, "status": a.status, "confidence": a.confidence,
                "statement": a.statement, "doc_path": a.doc_path,
                "doc_start_line": a.doc_start_line, "doc_end_line": a.doc_end_line,
                "target_asset": a.target_asset, "target_kind": a.target_kind,
                "target_fqn": a.target_fqn, "target_path": a.target_path,
                "target_start_line": a.target_start_line,
                "target_end_line": a.target_end_line,
                "justification": a.justification, "proposer": a.proposer,
                "reviewed_by": a.reviewed_by, "reviewed_at": a.reviewed_at,
                "review_note": a.review_note,
                "live_state": live.get(a.id)} for a in rows]}

    @app.post("/api/anchors/decide")
    def api_decide(payload: dict = Body(...)):
        """BR-19/BR-21 — a human decision, signed and dated."""
        anchor_id = payload.get("anchor_id", "")
        reviewer = (payload.get("reviewer") or "").strip()
        decision = payload.get("decision")
        note = payload.get("note", "")
        if not reviewer:
            raise HTTPException(422, "a reviewer name is required — curated "
                                     "knowledge must be attributable (BR-19)")
        pending = {a.id: a for a in curated.load_candidates()}
        anchor = pending.get(anchor_id)
        if anchor is None:
            raise HTTPException(404, "no such candidate awaiting review")

        if decision == "approve":
            store = KnowledgeStore(db_path)
            stamped = anchor_check.stamp(store, anchor)
            store.close()
            if not stamped:
                anchor.reject(reviewer, "auto-rejected: target is not in the "
                                        "graph, so no span hash could be "
                                        "recorded (BR-18 would be defeated)")
                curated.record_decision(anchor)
                return {"ok": False, "status": anchor.status,
                        "reason": anchor.review_note}
            anchor.approve(reviewer, note)
        elif decision == "reject":
            anchor.reject(reviewer, note)
        else:
            raise HTTPException(422, "decision must be 'approve' or 'reject'")

        curated.record_decision(anchor)
        return {"ok": True, "status": anchor.status,
                "span_sha": anchor.span_sha_at_approval[:16],
                "remaining": len(curated.load_candidates()),
                "note": "Rebuild (`eck refresh`) to project this into "
                        "knowledge.db and re-validate it."}

    return app
