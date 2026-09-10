"""BR-56 — resolve what the user meant into a node in the graph.

Services must accept business identifiers, not only technical ones. Someone
asks about "salary hold release", not
`com.inteacc.hr.view.pr.salaryholdrelease.SalaryHoldReleaseListView`.

Resolution is tried most-precise first, and every path records HOW it
resolved so the caller can judge it. Ambiguity is never broken by picking
the first candidate: an ambiguous subject is reported back as ambiguous with
the options listed, because silently choosing one is the guess BR-55 forbids.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..store.db import KnowledgeStore

# Below this cosine a retrieval fallback is not a resolution, it's a shrug.
RESOLVE_FLOOR = 0.62

# Kinds that name a TYPE rather than a member of one. When someone types a
# bare simple name they almost always mean the type, and a type competing
# with its own constructor is not a real ambiguity.
TYPE_KINDS = {"class", "interface", "enum", "record", "entity", "store",
              "view", "service", "security_role"}


@dataclass
class Subject:
    node_id: str
    asset_id: str
    kind: str
    fqn: str
    name: str
    path: str
    start_line: int
    end_line: int
    how: str                 # fqn | simple_name | route | table | retrieval
    confidence: float


@dataclass
class Ambiguous:
    term: str
    candidates: list[Subject]
    reason: str = "several elements share this name"


class Context:
    """Shared, read-only handle to the knowledge base (BR-53)."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.store = KnowledgeStore(self.db_path)
        self._retriever = None

    @property
    def retriever(self):
        if self._retriever is None:
            from .retrieval import Retriever
            self._retriever = Retriever(self.db_path)
        return self._retriever

    def close(self) -> None:
        self.store.close()


def _subject(row: Any, how: str, confidence: float) -> Subject:
    return Subject(
        node_id=row["id"], asset_id=row["asset_id"], kind=row["kind"],
        fqn=row["fqn"], name=row["name"], path=row["path"],
        start_line=row["start_line"], end_line=row["end_line"],
        how=how, confidence=confidence)


COLS = "id, asset_id, kind, name, fqn, path, start_line, end_line"


def resolve(ctx: Context, term: str, asset: str | None = None
            ) -> Subject | Ambiguous | None:
    term = term.strip()
    scope, params = ("AND asset_id = ?", [asset]) if asset else ("", [])

    # 1. exact fully-qualified name
    rows = ctx.store.query(
        f"SELECT {COLS} FROM node WHERE fqn = ? {scope} LIMIT 5",
        (term, *params))
    if len(rows) == 1:
        return _subject(rows[0], "fqn", 1.0)
    if len(rows) > 1:
        return Ambiguous(term, [_subject(r, "fqn", 1.0) for r in rows])

    # 2. exact simple name — the common case for a class a human names
    rows = ctx.store.query(
        f"SELECT {COLS} FROM node WHERE name = ? {scope}"
        f" ORDER BY CASE kind WHEN 'service' THEN 0 WHEN 'view' THEN 1"
        f" WHEN 'entity' THEN 2 ELSE 3 END LIMIT 12", (term, *params))
    if len(rows) == 1:
        return _subject(rows[0], "simple_name", 0.95)
    if len(rows) > 1:
        # A bare name means the type. Its constructor and same-named members
        # are not competing answers, so do not report a false ambiguity.
        types = [r for r in rows if r["kind"] in TYPE_KINDS]
        if len(types) == 1:
            return _subject(types[0], "simple_name", 0.95)
        pool = types or list(rows)
        return Ambiguous(term, [_subject(r, "simple_name", 0.9) for r in pool])

    # 3. a screen route, or a physical table name
    for how, sql in (
        ("route", f"SELECT {COLS} FROM node WHERE kind='view'"
                  f" AND json_extract(attrs,'$.route') = ? {scope} LIMIT 5"),
        ("table", f"SELECT {COLS} FROM node WHERE kind='store'"
                  f" AND name = ? {scope} LIMIT 5"),
    ):
        rows = ctx.store.query(sql, (term, *params))
        if len(rows) == 1:
            return _subject(rows[0], how, 0.95)
        if len(rows) > 1:
            return Ambiguous(term, [_subject(r, how, 0.9) for r in rows])

    # 4. business language — fall back to meaning-based retrieval
    hits = ctx.retriever.search(term, limit=6, source="code", asset_id=asset)
    strong = [h for h in hits
              if h.similarity is not None and h.similarity >= RESOLVE_FLOOR]
    if not strong:
        return None

    subjects: list[Subject] = []
    for h in strong:
        rows = ctx.store.query(
            f"SELECT {COLS} FROM node WHERE asset_id = ? AND fqn = ? LIMIT 1",
            (h.asset_id, h.heading))
        if rows:
            subjects.append(_subject(rows[0], "retrieval", h.similarity))
    if not subjects:
        return None
    # A retrieval match is a suggestion, never a decision — hand back the
    # shortlist and let the caller (or the human) choose.
    if len(subjects) == 1:
        return subjects[0]
    # These are semantic suggestions, not name collisions — say which, so the
    # caller does not read "ambiguous" as "this name is overloaded".
    return Ambiguous(
        term, subjects,
        reason="no element has this name; these were matched by meaning and "
               "one must be chosen explicitly")
