"""CAP-5 — hybrid retrieval (BR-32..BR-38).

Two retrievers, fused:
  * FTS5 BM25    — exact terms, identifiers, table names
  * dense vector — meaning, so a business-language question works without
                   knowing a single technical name (BR-32, BR-33)

Fusion is Reciprocal Rank Fusion, which needs no score calibration between
the two very differently-scaled retrievers.

Every result carries asset, path and line range (BR-35), and whether it is
derived structure or curated documentation (BR-20).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..ingest.embed import LocalEmbedder, unpack
from ..store.db import KnowledgeStore

RRF_K = 60          # standard damping constant
TOKEN = re.compile(r"[A-Za-z0-9_]+")

# Cosine floors, chosen by measuring this index rather than by taste.
# Observed on bge-small-en-v1.5 over 22,040 chunks:
#   genuine estate questions   0.679 - 0.814
#   out-of-domain questions    0.486 - 0.682
# The bands OVERLAP, so no single threshold separates them. MIN_COSINE is
# set below the weakest genuine question, which removes only the clearly
# absurd ("recipe for brownies" at 0.486). Anything between the floor and
# STRONG is reported as weak rather than presented as an answer.
#
# This is deliberately NOT a solution to BR-55. A similarity number cannot
# tell you whether a chunk answers a question; only reading it can. The real
# gate is the M4 answer service, which must be able to return "the evidence
# I found does not answer this".
# STRONG is set ABOVE the overlap band on purpose. The consequence is
# measured and accepted: "how do we stop paying someone who has left the
# company" (0.679) is reported weak even though it retrieves correctly.
# Erring this way costs a caveat on a good answer; erring the other way
# costs trust in every answer. For this platform that is not a close call.
MIN_COSINE = 0.62
STRONG_COSINE = 0.70


@dataclass
class Hit:
    chunk_id: str
    asset_id: str
    source: str          # 'wiki' | 'code'
    origin: str          # 'curated' | 'derived'
    heading: str
    path: str
    start_line: int
    end_line: int
    text: str
    score: float
    matched_by: str      # 'both' | 'keyword' | 'meaning'
    similarity: float | None = None   # cosine; None for keyword-only hits


def verdict(hits: list["Hit"]) -> str:
    """How much weight the caller may put on this result set.

    'strong'  at least one hit is a confident semantic match
    'weak'    hits exist but none is confident — may not answer the question
    'none'    nothing cleared the floor
    """
    if not hits:
        return "none"
    best = max((h.similarity or 0.0) for h in hits)
    return "strong" if best >= STRONG_COSINE else "weak"


def fts_query(question: str) -> str:
    """FTS5 MATCH is a query language, not free text — a bare question with
    punctuation is a syntax error. Quote every token and OR them."""
    tokens = [t for t in TOKEN.findall(question) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in tokens) if tokens else '""'


class Retriever:
    def __init__(self, db_path: Path, embedder: LocalEmbedder | None = None):
        self.store = KnowledgeStore(db_path)
        self._embedder = embedder
        self._matrix: np.ndarray | None = None
        self._ids: list[str] = []

    @property
    def embedder(self) -> LocalEmbedder:
        if self._embedder is None:
            self._embedder = LocalEmbedder(self.model_name())
        return self._embedder

    def model_name(self) -> str:
        row = self.store.query(
            "SELECT name FROM embedding_model ORDER BY rowid DESC LIMIT 1")
        return row[0]["name"] if row else "BAAI/bge-small-en-v1.5"

    def _load_vectors(self) -> None:
        if self._matrix is not None:
            return
        rows = self.store.query(
            "SELECT chunk_id, dim, vec FROM chunk_vec ORDER BY chunk_id")
        if not rows:
            self._matrix = np.zeros((0, 1), dtype=np.float32)
            return
        dim = rows[0]["dim"]
        self._ids = [r["chunk_id"] for r in rows]
        self._matrix = np.array(
            [unpack(r["vec"], dim) for r in rows], dtype=np.float32)

    # ------------------------------------------------------------------

    def search(self, question: str, limit: int = 10,
               source: str | None = None, asset_id: str | None = None,
               pool: int = 60) -> list[Hit]:
        """source: 'wiki' | 'code' | None for both (BR-34).
        asset_id: narrow to one registered asset (BR-36)."""
        where, params = [], []
        if source:
            where.append("c.source = ?")
            params.append(source)
        if asset_id:
            where.append("c.asset_id = ?")
            params.append(asset_id)
        clause = (" AND " + " AND ".join(where)) if where else ""

        # --- keyword half ---------------------------------------------
        kw_rows = self.store.query(
            f"SELECT c.id FROM chunk_fts f JOIN chunk c ON c.id = f.chunk_id"
            f" WHERE chunk_fts MATCH ?{clause} ORDER BY bm25(chunk_fts)"
            f" LIMIT ?", (fts_query(question), *params, pool))
        kw_rank = {r["id"]: i for i, r in enumerate(kw_rows)}

        # --- meaning half ---------------------------------------------
        self._load_vectors()
        vec_rank: dict[str, int] = {}
        vec_sim: dict[str, float] = {}
        if len(self._ids):
            q = self.embedder.encode_query(question).astype(np.float32)
            sims = self._matrix @ q
            allowed: set[str] | None = None
            if where:
                allowed = {r["id"] for r in self.store.query(
                    f"SELECT c.id FROM chunk c WHERE 1=1{clause}", tuple(params))}
            order = np.argsort(-sims)
            taken = 0
            for idx in order:
                cid = self._ids[int(idx)]
                sim = float(sims[int(idx)])
                if sim < MIN_COSINE:
                    break            # sorted, so everything after is worse
                if allowed is not None and cid not in allowed:
                    continue
                vec_rank[cid] = taken
                vec_sim[cid] = sim
                taken += 1
                if taken >= pool:
                    break

        # --- fuse -------------------------------------------------------
        fused: dict[str, float] = {}
        for cid, r in kw_rank.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + r)
        for cid, r in vec_rank.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + r)
        if not fused:
            return []

        top = sorted(fused.items(), key=lambda kv: -kv[1])[:limit]
        ids = [cid for cid, _ in top]
        placeholders = ",".join("?" * len(ids))
        by_id = {r["id"]: r for r in self.store.query(
            f"SELECT * FROM chunk WHERE id IN ({placeholders})", tuple(ids))}

        hits: list[Hit] = []
        for cid, score in top:
            r = by_id.get(cid)
            if r is None:
                continue
            in_kw, in_vec = cid in kw_rank, cid in vec_rank
            hits.append(Hit(
                chunk_id=cid, asset_id=r["asset_id"], source=r["source"],
                origin=r["origin"], heading=r["heading"] or "", path=r["path"],
                start_line=r["start_line"], end_line=r["end_line"],
                text=r["text"], score=score,
                matched_by="both" if in_kw and in_vec
                           else ("keyword" if in_kw else "meaning"),
                similarity=vec_sim.get(cid)))
        return hits

    def get_span(self, chunk_id: str) -> dict[str, Any] | None:
        """BR-38 — return the full content of a specific source location."""
        rows = self.store.query("SELECT * FROM chunk WHERE id = ?", (chunk_id,))
        return dict(rows[0]) if rows else None

    def close(self) -> None:
        self.store.close()
