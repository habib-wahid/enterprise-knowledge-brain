"""The shape every answer service returns (BR-50, BR-54, BR-55).

Services return EVIDENCE, not prose. The narrating is done by whatever
consumes them — a person reading the CLI, or an assistant over MCP. That
separation is why every service here is deterministic, read-only, and
needs no model at all.

Three outcomes, and the middle one carries most of the weight:

  answered   the knowledge base has what was asked for
  partial    some of it, with the missing part named in `gaps`
  unknown    it does not know, and `needed` says what would be required

BR-55 forbids guessing. A service that cannot resolve its subject returns
`unknown` with a concrete next step; it never falls back to a plausible
near-match and presents it as the answer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

ANSWERED, PARTIAL, UNKNOWN = "answered", "partial", "unknown"

# BR-61 — travels with every result, so an assistant consuming a service
# over MCP is told the rule at the point of use rather than only once in a
# server description it may not have read.
GROUNDING = (
    "Answer only from the evidence in this result. Every claim you make must "
    "be traceable to one of the evidence entries below. If the evidence is "
    "insufficient to answer, say so plainly and name what is missing — do not "
    "fill the gap from general knowledge about payroll systems or Java."
)


@dataclass
class Evidence:
    """A verifiable pointer into the estate (BR-54, BR-35)."""
    asset_id: str
    path: str
    start_line: int
    end_line: int
    kind: str
    fqn: str
    origin: str              # derived | curated | inferred  (BR-20)
    confidence: float
    excerpt: str = ""

    def ref(self) -> str:
        return f"{self.asset_id}:{self.path}:{self.start_line}-{self.end_line}"


@dataclass
class ServiceResult:
    service: str
    question: str
    outcome: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    needed: list[str] = field(default_factory=list)
    presentation_guidance: str = ""
    grounding: str = GROUNDING

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [{**asdict(e), "ref": e.ref()} for e in self.evidence]
        return d


def unknown(service: str, question: str, why: str,
            needed: list[str], gaps: list[str] | None = None) -> ServiceResult:
    """The honest answer. Used far more often than it feels like it should be."""
    return ServiceResult(
        service=service, question=question, outcome=UNKNOWN,
        findings=[{"statement": why}], gaps=gaps or [], needed=needed,
        presentation_guidance=(
            "The platform does not know this. Say so directly and relay what "
            "would be needed. Do NOT substitute a near-match or general "
            "knowledge — a confident wrong answer is the failure this system "
            "exists to prevent."))
