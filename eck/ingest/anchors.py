"""CAP-3 — propose anchors between wiki statements and code (BR-16, BR-21).

Two proposers behind one interface:

  retrieval  top-k code chunks above a similarity floor. No API key, no cost,
             deterministic. Higher recall, lower precision — it proposes
             plausible neighbours and leaves the judgement to the reviewer.

  llm        the same candidates, then claude-opus-5 decides which ones the
             statement is actually about and says why. Better precision and
             a written justification the reviewer can check.

NEITHER publishes. Both write proposals with status='proposed' for human
review, because BR-21 forbids publishing unreviewed suggestions as fact.
The reviewer is the thing that makes an anchor curated knowledge; the
proposer only reduces how much reading they have to do.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from ..curated import Anchor
from ..services.retrieval import Retriever
from ..store.db import sha256

MODEL = "claude-opus-5"

# A statement is unlikely to be implemented by code we can barely match.
PROPOSE_FLOOR = 0.55
MAX_CANDIDATES_PER_STATEMENT = 6

SYSTEM = """You link business documentation to the code that implements it.

You are given one statement from a product wiki and several candidate code \
locations retrieved from the real codebase. Decide which candidates, if any, \
the statement is ACTUALLY about.

Rules:
- Only select a candidate if the code plainly implements, enforces or \
represents what the statement describes. Topical similarity is not enough.
- Selecting nothing is a correct and common answer. Most statements are \
narrative and have no single implementation site.
- Your justification must cite what in the code supports the link \
(a method name, a field, a table, a condition). If you cannot point at \
something concrete, do not select the candidate.
- Never invent a location that is not in the candidate list."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_index": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "justification": {"type": "string"},
                },
                "required": ["candidate_index", "confidence", "justification"],
                "additionalProperties": False,
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["selections", "reasoning"],
    "additionalProperties": False,
}


def _candidates_for(retriever: Retriever, statement: str, asset: str | None
                    ) -> list[Any]:
    hits = retriever.search(statement, limit=MAX_CANDIDATES_PER_STATEMENT,
                            source="code", asset_id=asset,
                            pool=MAX_CANDIDATES_PER_STATEMENT * 6)
    return [h for h in hits
            if h.similarity is not None and h.similarity >= PROPOSE_FLOOR]


def _anchor_from_hit(chunk, hit, confidence: float, justification: str,
                     proposer: str) -> Anchor:
    return Anchor(
        id=Anchor.make_id(chunk["id"], hit.asset_id, hit.heading),
        chunk_id=chunk["id"],
        statement=chunk["text"],
        statement_sha=sha256(chunk["text"].encode()),
        doc_path=chunk["path"],
        doc_start_line=chunk["start_line"],
        doc_end_line=chunk["end_line"],
        target_asset=hit.asset_id,
        target_kind="",              # filled in by the resolver below
        target_fqn=hit.heading,      # code chunks carry the fqn as heading
        target_path=hit.path,
        target_start_line=hit.start_line,
        target_end_line=hit.end_line,
        span_sha_at_approval="",     # stamped at approval, not at proposal
        confidence=round(float(confidence), 3),
        justification=justification,
        proposer=proposer,
    )


def _fill_target_kind(retriever: Retriever, anchors: list[Anchor]) -> None:
    """Resolve each target's node kind, so the anchor names what it points at."""
    for a in anchors:
        rows = retriever.store.query(
            "SELECT kind FROM node WHERE asset_id = ? AND fqn = ? LIMIT 1",
            (a.target_asset, a.target_fqn))
        a.target_kind = rows[0]["kind"] if rows else "unknown"


# --------------------------------------------------------------- retrieval

def propose_by_retrieval(retriever: Retriever, chunks: Iterable[dict],
                         asset: str | None = None) -> list[Anchor]:
    out: list[Anchor] = []
    for chunk in chunks:
        for hit in _candidates_for(retriever, chunk["text"], asset):
            out.append(_anchor_from_hit(
                chunk, hit, hit.similarity,
                f"Retrieved at cosine {hit.similarity:.3f}. "
                f"NOT judged — a reviewer must confirm this link.",
                proposer="retrieval"))
    _fill_target_kind(retriever, out)
    return out


# --------------------------------------------------------------- llm

def propose_by_llm(retriever: Retriever, chunks: Iterable[dict],
                   asset: str | None = None, model: str = MODEL,
                   progress=None) -> list[Anchor]:
    import anthropic

    client = anthropic.Anthropic()
    out: list[Anchor] = []

    for chunk in chunks:
        candidates = _candidates_for(retriever, chunk["text"], asset)
        if not candidates:
            if progress:
                progress(chunk, 0)
            continue

        listing = "\n\n".join(
            f"[{i}] {h.asset_id} :: {h.heading}\n"
            f"    {h.path}:{h.start_line}-{h.end_line}\n"
            f"{h.text[:1200]}"
            for i, h in enumerate(candidates))

        message = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content":
                       f"STATEMENT (from {chunk['path']} lines "
                       f"{chunk['start_line']}-{chunk['end_line']}):\n"
                       f"{chunk['text'][:3000]}\n\n"
                       f"CANDIDATE CODE LOCATIONS:\n{listing}"}],
        )
        text = next(b.text for b in message.content if b.type == "text")
        result = json.loads(text)

        picked = 0
        for sel in result.get("selections", []):
            idx = sel["candidate_index"]
            if not 0 <= idx < len(candidates):
                continue                      # never trust an invented index
            out.append(_anchor_from_hit(
                chunk, candidates[idx], sel["confidence"],
                sel["justification"], proposer=f"llm:{model}"))
            picked += 1
        if progress:
            progress(chunk, picked)

    _fill_target_kind(retriever, out)
    return out
