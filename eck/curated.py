"""The curated store — human-approved knowledge, kept outside knowledge.db.

Why a jsonl file and not a table: `eck refresh` deletes and rebuilds
knowledge.db from scratch. Anything a human authored must survive that
(BR-68), be version-controllable, and carry author/date/reason
(data requirement: "human-curated content is separate, version-controlled
and backed by author/date/reason").

One JSON object per line, sorted by id on write, so a diff in git shows
exactly which anchor a reviewer changed.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .store.db import sha256, utc_now

CURATED_DIR = Path(__file__).resolve().parent.parent / "curated"
CANDIDATES = CURATED_DIR / "candidates.jsonl"
ANCHORS = CURATED_DIR / "anchors.jsonl"

PROPOSED, APPROVED, REJECTED = "proposed", "approved", "rejected"


@dataclass
class Anchor:
    """A claim that one business statement is implemented at one code location."""
    id: str
    chunk_id: str
    statement: str
    statement_sha: str
    doc_path: str
    doc_start_line: int
    doc_end_line: int
    target_asset: str
    target_kind: str
    target_fqn: str
    target_path: str
    target_start_line: int
    target_end_line: int
    span_sha_at_approval: str
    confidence: float
    justification: str
    proposer: str
    status: str = PROPOSED
    reviewed_by: str = ""
    reviewed_at: str = ""
    review_note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def make_id(chunk_id: str, target_asset: str, target_fqn: str) -> str:
        """Deterministic: the same statement/target pair is the same anchor,
        no matter how many times it is proposed."""
        return sha256(f"{chunk_id}|{target_asset}|{target_fqn}".encode())[:16]

    def approve(self, who: str, note: str = "") -> None:
        self.status = APPROVED
        self.reviewed_by = who
        self.reviewed_at = utc_now()
        self.review_note = note

    def reject(self, who: str, note: str = "") -> None:
        self.status = REJECTED
        self.reviewed_by = who
        self.reviewed_at = utc_now()
        self.review_note = note


def _read(path: Path) -> list[Anchor]:
    if not path.exists():
        return []
    out: list[Anchor] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(Anchor(**json.loads(line)))
    return out


def _write(path: Path, anchors: Iterable[Anchor]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(anchors, key=lambda a: a.id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for a in rows:
            fh.write(json.dumps(asdict(a), sort_keys=True,
                                ensure_ascii=False) + "\n")
    tmp.replace(path)


def load_candidates() -> list[Anchor]:
    return _read(CANDIDATES)


def load_approved() -> list[Anchor]:
    return [a for a in _read(ANCHORS) if a.status == APPROVED]


def save_candidates(anchors: Iterable[Anchor]) -> None:
    _write(CANDIDATES, anchors)


def save_anchors(anchors: Iterable[Anchor]) -> None:
    _write(ANCHORS, anchors)


def merge_candidates(new: Iterable[Anchor]) -> tuple[int, int]:
    """Add proposals without clobbering existing review decisions (BR-21).

    A candidate that a human has already judged is never reopened by a
    re-run of the proposer; that would quietly discard review work.
    """
    existing = {a.id: a for a in load_candidates()}
    decided = {a.id for a in _read(ANCHORS)}
    added = skipped = 0
    for a in new:
        if a.id in existing or a.id in decided:
            skipped += 1
            continue
        existing[a.id] = a
        added += 1
    save_candidates(existing.values())
    return added, skipped


def record_decision(anchor: Anchor) -> None:
    """Move a reviewed candidate out of the queue and into the durable store."""
    decided = {a.id: a for a in _read(ANCHORS)}
    decided[anchor.id] = anchor
    save_anchors(decided.values())
    remaining = [a for a in load_candidates() if a.id != anchor.id]
    save_candidates(remaining)
