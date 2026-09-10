"""CAP-7 composite services — answer a whole question in one call (BR-49).

Each fans out to atomic services and returns their combined evidence plus
guidance for presenting it (BR-50). Composites do NOT summarise: they never
collapse evidence into a claim, because the claim is the consumer's job and
collapsing it is where traceability gets lost.

A composite whose subject cannot be resolved returns `unknown` exactly as an
atomic service does — it does not partially answer around a missing subject.
"""
from __future__ import annotations

from typing import Any

from . import atomic
from .registry import service
from .resolve import Context
from .result import ANSWERED, PARTIAL, UNKNOWN, ServiceResult, unknown


def _merge(sid: str, question: str, parts: dict[str, ServiceResult],
           guidance: str, extra_gaps: list[str] | None = None) -> ServiceResult:
    evidence = []
    seen = set()
    for r in parts.values():
        for e in r.evidence:
            key = e.ref()
            if key not in seen:
                seen.add(key)
                evidence.append(e)

    findings: list[dict[str, Any]] = []
    gaps: list[str] = list(extra_gaps or [])
    for name, r in parts.items():
        findings.append({"section": name, "outcome": r.outcome,
                         "detail": r.findings})
        gaps.extend(f"[{name}] {g}" for g in r.gaps)

    outcomes = {r.outcome for r in parts.values()}
    outcome = (ANSWERED if outcomes == {ANSWERED}
               else UNKNOWN if outcomes == {UNKNOWN} else PARTIAL)

    return ServiceResult(service=sid, question=question, outcome=outcome,
                         findings=findings, evidence=evidence,
                         gaps=sorted(set(gaps)),
                         presentation_guidance=guidance)


# ------------------------------------------------------------------ 1

@service(
    id="composite.change_impact", category="impact", composite=True,
    question="If I change this, what breaks and what do I need to know?",
    when_to_use="The default question before making a change. Combines blast "
                "radius, enforced checks, data touched, curated meaning and "
                "configuration. Use impact.of_change alone if you only want "
                "the caller list.",
    inputs={"element": "the thing you intend to change",
            "depth": "call hops to walk (default 2)",
            "asset": "optional asset id"})
def change_impact(ctx: Context, element: str, depth: int = 2,
                  asset: str = None) -> ServiceResult:
    probe = atomic.navigation_find(ctx, element, asset)
    if probe.outcome == UNKNOWN:
        return probe

    parts = {
        "location": probe,
        "blast_radius": atomic.impact_of_change(ctx, element, depth, asset),
        "checks_enforced": atomic.checks_enforced_in(ctx, element, asset),
        "data_touched": atomic.effects_of(ctx, element, asset),
        "business_meaning": atomic.explanation_of(ctx, element, asset),
        "configuration": atomic.configuration_affecting(ctx, element, asset),
    }
    return _merge(
        "composite.change_impact", element, parts,
        guidance=(
            "Structure the answer as: (1) what this element is; (2) directly "
            "affected components, then indirect, with cross-application "
            "entries called out separately; (3) checks that would need "
            "re-testing; (4) data touched; (5) curated business meaning, "
            "attributed to its reviewer and clearly separated from derived "
            "fact (BR-20). End with the gaps verbatim — an impact answer that "
            "hides its blind spots is worse than none."))


# ------------------------------------------------------------------ 2

@service(
    id="composite.failure_trace", category="flow", composite=True,
    question="Where does this failure come from?",
    when_to_use="Use with an exception name, an error message, or a symptom "
                "to find the code that raises it and what guards it.",
    inputs={"symptom": "exception class, error text, or description",
            "asset": "optional asset id"})
def failure_trace(ctx: Context, symptom: str, asset: str = None) -> ServiceResult:
    search = atomic.search_knowledge(ctx, symptom, source="code",
                                     asset=asset, limit=10)
    if search.outcome == UNKNOWN:
        return unknown(
            "composite.failure_trace", symptom,
            f"Nothing in the estate matches {symptom!r} closely enough to "
            f"identify an origin.",
            needed=["the exception class name as it appears in the stack trace",
                    "or the exact error message text"])

    # Exception types are the strongest signal available from structure alone.
    exceptions = ctx.store.query("""
        SELECT * FROM node
        WHERE (name LIKE '%Exception' OR name LIKE '%Error')
          AND (name LIKE ? OR fqn LIKE ?)
        ORDER BY name LIMIT 20""", (f"%{symptom}%", f"%{symptom}%"))

    parts = {"candidate_locations": search}
    if exceptions:
        first = exceptions[0]
        parts["exception_type"] = atomic.navigation_find(ctx, first["fqn"], None)
        parts["raised_by"] = atomic.impact_of_change(ctx, first["fqn"], 1, None)

    return _merge(
        "composite.failure_trace", symptom, parts,
        guidance=(
            "Lead with the exception type if one was identified, then the code "
            "that references it — those are the throw sites. Present other "
            "matches as candidates, not conclusions. State explicitly that "
            "trigger CONDITIONS are not modelled (that needs CAP-4), so the "
            "answer locates where a failure originates, not why it fired."),
        extra_gaps=(
            [] if exceptions else
            [f"No exception type matching {symptom!r} exists in the estate; "
             f"results are text matches only"]))


# ------------------------------------------------------------------ 3

@service(
    id="composite.input_acceptance", category="checks", composite=True,
    question="What has to be true for this input to be accepted?",
    when_to_use="Use to understand why something was rejected, or what a "
                "screen or service demands before it will proceed.",
    inputs={"element": "the entry point, screen or service",
            "asset": "optional asset id"})
def input_acceptance(ctx: Context, element: str, asset: str = None) -> ServiceResult:
    probe = atomic.navigation_find(ctx, element, asset)
    if probe.outcome == UNKNOWN:
        return probe

    parts = {
        "entry_point": probe,
        "checks_enforced": atomic.checks_enforced_in(ctx, element, asset),
        "what_it_calls": atomic.flow_downstream(ctx, element, asset),
        "business_meaning": atomic.explanation_of(ctx, element, asset),
    }
    return _merge(
        "composite.input_acceptance", element, parts,
        guidance=(
            "List the conditions found, each with its source location. Be "
            "explicit that this is a FLOOR, not a complete acceptance "
            "contract: inline conditionals and declarative Jmix validation "
            "are not derived, so absence of a check here is not evidence that "
            "no check exists."))


# ------------------------------------------------------------------ 4

@service(
    id="composite.process_description", category="flow", composite=True,
    question="How does this business process work end to end?",
    when_to_use="Use for onboarding-style questions about a whole process. "
                "Returns curated documentation plus the code it is anchored "
                "to. Ordered stages need CAP-4, which is not built yet.",
    inputs={"process": "the process, in business language",
            "asset": "optional asset id"})
def process_description(ctx: Context, process: str,
                        asset: str = None) -> ServiceResult:
    docs = atomic.search_knowledge(ctx, process, source="wiki", limit=10)
    code = atomic.search_knowledge(ctx, process, source="code", asset=asset,
                                   limit=10)
    if docs.outcome == UNKNOWN and code.outcome == UNKNOWN:
        return unknown(
            "composite.process_description", process,
            f"No documentation or code matches {process!r} well enough to "
            f"describe a process.",
            needed=["a process name closer to the wiki's own wording",
                    "`eck search` will show what vocabulary the estate uses"])

    anchored = ctx.store.query("""
        SELECT * FROM anchor WHERE state IN ('resolved','stale')
        ORDER BY confidence DESC LIMIT 20""")

    parts = {"documentation": docs, "implementation": code}
    gaps = ["Ordered stages, handovers, failure paths and effects require "
            "CAP-4 process modelling, which is NOT built (M6). This returns "
            "relevant material, not a verified end-to-end sequence."]
    if not anchored:
        gaps.append("No approved anchors exist, so nothing here is a verified "
                    "link between documentation and code (BR-16 unmet).")

    return _merge(
        "composite.process_description", process, parts,
        guidance=(
            "Present the curated documentation first as the business view, "
            "attributed as curated, then the implementation as derived fact — "
            "keeping the two visibly separate (BR-20). State up front that "
            "the platform cannot yet confirm stage ORDER, so this is "
            "supporting material for understanding the process, not an "
            "authoritative description of it."),
        extra_gaps=gaps)
