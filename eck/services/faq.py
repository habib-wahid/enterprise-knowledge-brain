"""The sixteen questions people actually ask (CAP-7, BR-47..BR-52).

The atomic services in `atomic.py` are correct and nobody wants to choose
between them. `checks.enforced_in` is a precise name for a precise query, and
it is the wrong thing to put in front of someone who wants to know what rules
apply to salary adjustment. These sixteen are the question as asked; each one
composes the building blocks and narrates the result.

Two properties every answer here holds to:

  * **Both audiences, every time.** Each result carries at least one
    `business` section and one `technical` section, so the same answer serves
    the person who will never open the source and the person who has to change
    it. A section with nothing behind it says why it is empty rather than
    being dropped.

  * **Nothing is composed.** Sentences come from curated wiki text, curated
    CAP-4 stage descriptions, or captured source; structure comes from node
    and edge rows. No model runs, so there is nothing here to hallucinate
    with. Where the knowledge base is silent the answer is silent and names
    what would fill the gap (BR-55).

They are registered like any other service, so the CLI (`eck ask faq.…`) and
MCP get them for free — BR-64, holding by construction rather than by effort.
"""
from __future__ import annotations

import re
from typing import Any

from . import narrate as N
from .registry import service
from .resolve import Context
from .result import ANSWERED, PARTIAL, UNKNOWN, Evidence, Section, ServiceResult

ELEMENT_INPUT = ("what you are asking about — a business process, a screen, a "
                 "class, a method, a table, or an exception name")


# ------------------------------------------------------------------ shaping

def _dedupe(evidence: list[Evidence]) -> list[Evidence]:
    seen, out = set(), []
    for e in evidence:
        if e.ref() not in seen:
            seen.add(e.ref())
            out.append(e)
    return out[:80]


def _alternatives(ctx: Context, candidates: list[Any], chosen: Any | None,
                  how: str) -> Section | None:
    """When the subject was picked rather than matched exactly, show the rest.

    Silently choosing one of several candidates is the guess BR-55 forbids;
    choosing one and SAYING SO, with the others listed, is not.
    """
    others = [r for r in candidates if chosen is None or r["id"] != chosen["id"]]
    if not others or how in ("fqn", "route", "table"):
        return None
    names = N.asset_names(ctx)
    s = Section(
        key="alternatives", audience="technical",
        title="Other elements this could have meant",
        lead=("Several elements share this name."
              if how == "ambiguous" else
              "These also matched. The answer above uses the first of them."),
        notes=["Pick one to ask the same question about that element instead."])
    for i, r in enumerate(others[:8], 1):
        s.steps.append({
            "n": i, "title": r["fqn"], "origin": "derived", "text": "",
            "meta": [f"{names.get(r['asset_id'], r['asset_id'])} ({r['asset_id']})",
                     r["kind"], f"{r['path']}:{r['start_line']}"],
            # The page turns this into a button that re-asks against this exact
            # name — cheaper than making the ranker guess perfectly.
            "ask": r["fqn"],
            "ref": N.ref(r)})
    return s


def _answer(sid: str, term: str, sections: list[Section],
            evidence: list[Evidence], guidance: str,
            gaps: list[str] | None = None,
            needed: list[str] | None = None,
            subject: dict[str, Any] | None = None,
            findings: list[dict[str, Any]] | None = None) -> ServiceResult:
    """Assemble the result and let the sections decide the outcome.

    Everything empty means the platform does not know. Something empty means
    partial, and the empty sections carry their own reason — which is why the
    gaps list stays short rather than restating them.
    """
    sections = [s for s in sections if s is not None]
    filled = [s for s in sections if s.steps]
    empty = [s for s in sections if not s.steps]

    if not filled:
        outcome = UNKNOWN
    elif empty:
        outcome = PARTIAL
    else:
        outcome = ANSWERED

    return ServiceResult(
        service=sid, question=term, outcome=outcome,
        findings=findings or [{"sections": [s.key for s in sections],
                               "answered_sections": [s.key for s in filled],
                               "empty_sections": [s.key for s in empty]}],
        evidence=_dedupe(evidence),
        gaps=list(gaps or []),
        needed=list(needed or ([] if filled else [
            "a name the estate uses — try the Search tab to find its wording",
            "or a class, screen route, table or exception name that exists in scope"])),
        sections=sections,
        subject=subject or {},
        presentation_guidance=guidance)


def _prep(ctx: Context, term: str):
    row, candidates, how = N.code_anchor(ctx, term)
    names = N.asset_names(ctx)
    return row, candidates, how, N.subject_dict(row, how, names)


def _business(ctx: Context, term: str, row: Any | None, max_steps: int = N.MAX_STEPS):
    """The business half, with the resolved element's name as a second try.

    Someone who types `LoanAcctDetailView` is asking a business question in
    technical words. Splitting the identifier is what lets the documentation
    answer it.
    """
    fallback = N.as_words(row["name"]) if row is not None else None
    return N.business_flow(ctx, term, max_steps=max_steps, fallback=fallback)


def _business_and_code(ctx: Context, term: str, depth: int = N.WALK_DEPTH):
    """The pair every answer is built around."""
    row, candidates, how, subj = _prep(ctx, term)
    biz, ev_b = _business(ctx, term, row)
    code, ev_c, stats = N.execution_flow(ctx, row, how, depth)
    return row, candidates, how, subj, biz, code, ev_b + ev_c, stats


# ================================================================== 1

@service(
    id="faq.process_steps", category="process", composite=True, faq=1,
    question="What are the steps of this business process?",
    example="Loan interest calculation",
    answers_with="Human-readable end-to-end business flow, then the code that "
                 "carries it out.",
    when_to_use="Use when you want the business narrative first — what happens, "
                "in what order, in plain language. For the code path in detail "
                "use faq.source_execution_flow; for both weighted equally use "
                "faq.end_to_end.",
    inputs={"element": ELEMENT_INPUT})
def process_steps(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj, biz, code, ev, _ = _business_and_code(ctx, element, 2)
    return _answer(
        "faq.process_steps", element,
        [biz, code, _alternatives(ctx, cand, row, how)], ev,
        subject=subj,
        guidance=(
            "Read the business flow top to bottom — it is the documentation's "
            "own wording, in its own order, and needs no technical background. "
            "The code section below shows where each part is carried out."),
        gaps=["Documentation order is the order of the curated page, which in "
              "this estate's wiki is its numbered process order. It is not a "
              "traced execution sequence."]
        if not N.has_table(ctx, "process") else [])


# ================================================================== 2

@service(
    id="faq.end_to_end", category="process", composite=True, faq=2,
    question="How does this business functionality work from start to finish?",
    example="Loan account modification",
    answers_with="The business story and the system behaviour behind it, side "
                 "by side.",
    when_to_use="The broadest question available: business narrative, entry "
                "points, the full call path and the data touched. Use "
                "faq.process_steps if you only want the business half.",
    inputs={"element": ELEMENT_INPUT})
def end_to_end(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj, biz, code, ev, _ = _business_and_code(ctx, element)
    data, ev_d = N.data_section(ctx, row)
    rules, ev_r = N.rules_section(ctx, row)
    return _answer(
        "faq.end_to_end", element,
        [biz, code, rules, data, _alternatives(ctx, cand, row, how)],
        ev + ev_d + ev_r, subject=subj,
        guidance=(
            "Present business flow first for a non-technical reader, then the "
            "execution flow, the rules enforced along it, and the data it "
            "touches. Keep curated business wording and derived code structure "
            "visibly separate — they are different kinds of claim."))


# ================================================================== 3

@service(
    id="faq.rules_and_validations", category="checks", composite=True, faq=3,
    question="What business rules and validations apply to this functionality?",
    example="Salary adjustment",
    answers_with="The documented rules, and the checks actually enforced in "
                 "code, with locations.",
    when_to_use="Use to compare what the business says must be true against "
                "what the code enforces. For where to ADD a new check, use the "
                "placement.for_check building block.",
    inputs={"element": ELEMENT_INPUT})
def rules_and_validations(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    biz, ev_b = _business(ctx, element, row)
    biz.title = "Documented rules and conditions — the business view"
    rules, ev_r = N.rules_section(ctx, row)
    code, ev_c, _ = N.execution_flow(ctx, row, how, 2)
    return _answer(
        "faq.rules_and_validations", element,
        [biz, rules, code, _alternatives(ctx, cand, row, how)],
        ev_b + ev_r + ev_c, subject=subj,
        guidance=(
            "Set the documented rules against the enforced checks and say "
            "plainly where the two do not line up. A rule present in the "
            "documentation with no matching check in code is a finding worth "
            "raising, not an omission to smooth over."),
        gaps=["A documented rule with no enforced check found here may still "
              "be enforced by an inline condition or a Jmix constraint, "
              "neither of which is derived."])


# ================================================================== 4

@service(
    id="faq.source_execution_flow", category="flow", composite=True, faq=4,
    question="What is the complete source-code execution flow for this functionality?",
    example="Loan disbursement",
    answers_with="Entry point → handlers → application logic → shared services "
                 "→ entities and tables.",
    when_to_use="The developer's question. Use when you need the call path "
                "itself. For the same path with the business story attached, "
                "use faq.end_to_end.",
    inputs={"element": ELEMENT_INPUT,
            "depth": "how many call hops to follow (1-4, default 3)"})
def source_execution_flow(ctx: Context, element: str, depth: int = 3) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    code, ev_c, stats = N.execution_flow(ctx, row, how, int(depth))
    data, ev_d = N.data_section(ctx, row)
    biz, ev_b = _business(ctx, element, row, max_steps=6)
    biz.title = "What this does in business terms — context for the code below"
    return _answer(
        "faq.source_execution_flow", element,
        [code, data, biz, _alternatives(ctx, cand, row, how)],
        ev_c + ev_d + ev_b, subject=subj,
        findings=[{"reached": stats.get("reached", 0),
                   "applications": stats.get("applications", []),
                   "layers": stats.get("layers", {})}],
        guidance=(
            "Walk the layers in order: where it starts, what triggers it, what "
            "runs, what it delegates to, what it persists. State clearly that "
            "the layering is structural — depth is call distance, not the "
            "sequence statements execute in."))


# ================================================================== 5

@service(
    id="faq.code_inventory", category="navigation", composite=True, faq=5,
    question="Which APIs, classes, methods, and services are involved in this functionality?",
    example="Customer registration",
    answers_with="A grouped inventory of every element the functionality "
                 "reaches, with file locations.",
    when_to_use="Use to scope work or a review — the checklist of what exists. "
                "For how they call each other, use faq.source_execution_flow.",
    inputs={"element": ELEMENT_INPUT})
def code_inventory(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    inv, ev_i, counts = N.inventory_section(ctx, row)
    biz, ev_b = _business(ctx, element, row, max_steps=6)
    biz.title = "What this functionality is, in business terms"
    return _answer(
        "faq.code_inventory", element, [inv, biz, _alternatives(ctx, cand, row, how)],
        ev_i + ev_b, subject=subj, findings=[counts],
        guidance=(
            "Give the inventory by group with counts, and name the file for "
            "each entry. Say that the list covers what derived call references "
            "reach — anything invoked through an unresolved receiver is not "
            "counted."))


# ================================================================== 6

@service(
    id="faq.business_to_code_map", category="explanation", composite=True, faq=6,
    question="How does this business process map to the source code?",
    example="Loan repayment",
    answers_with="Business step ↔ code location, separating human-approved "
                 "links from structural proximity.",
    when_to_use="Use when you need to know which code implements which "
                "documented step, and how much that link is worth. For the "
                "business story alone use faq.process_steps.",
    inputs={"element": ELEMENT_INPUT})
def business_to_code_map(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    mapping, ev_m = N.mapping_section(ctx, element, row)
    biz, ev_b = _business(ctx, element, row)
    code, ev_c, _ = N.execution_flow(ctx, row, how, 2)
    code.title = "Where the code for this lives — structural view"
    return _answer(
        "faq.business_to_code_map", element,
        [mapping, biz, code, _alternatives(ctx, cand, row, how)],
        ev_m + ev_b + ev_c, subject=subj,
        guidance=(
            "Lead with the approved mappings — those are links a named person "
            "signed. Present the business flow and the code flow as two "
            "halves that have NOT been formally connected unless an approved "
            "link says so. Do not close that gap by inference; that is exactly "
            "what the approval step exists for."),
        gaps=["Where no approved anchor exists, business steps and code "
              "locations here are two independent findings about the same "
              "subject, not a verified mapping between them."])


# ================================================================== 7

@service(
    id="faq.data_involved", category="effects", composite=True, faq=7,
    question="What data is involved in this business process, and how does it change?",
    example="Account closure",
    answers_with="Entities, physical tables and fields reached, with the "
                 "documented meaning of the data.",
    when_to_use="Use for the persistence footprint before a data migration or "
                "an impact assessment. For the call path that touches it, use "
                "faq.source_execution_flow.",
    inputs={"element": ELEMENT_INPUT})
def data_involved(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    data, ev_d = N.data_section(ctx, row)
    biz, ev_b = _business(ctx, element, row)
    biz.title = "What this data means to the business"
    code, ev_c, _ = N.execution_flow(ctx, row, how, 2)
    return _answer(
        "faq.data_involved", element,
        [data, biz, code, _alternatives(ctx, cand, row, how)],
        ev_d + ev_b + ev_c, subject=subj,
        guidance=(
            "Name each entity and its physical table with the fields carried. "
            "Be explicit about the limit: the platform knows WHICH data is "
            "touched, not whether a touch is a read or a write, and not what "
            "value it ends up with. Do not describe state transitions the "
            "evidence does not contain."),
        gaps=["How a value CHANGES is not derivable from structure. Direction "
              "(read vs write) and resulting state need behaviour modelling "
              "that structural extraction does not provide."])


# ================================================================== 8

@service(
    id="faq.trigger_and_aftermath", category="flow", composite=True, faq=8,
    question="What happens when this functionality is triggered, and what happens afterward?",
    example="Fund transfer",
    answers_with="What starts it, what runs, and what it leaves behind.",
    when_to_use="Use to understand side effects — what else moves when this "
                "runs. For who calls it in the first place, use "
                "faq.change_blast_radius.",
    inputs={"element": ELEMENT_INPUT})
def trigger_and_aftermath(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    code, ev_c, _ = N.execution_flow(ctx, row, how)
    code.title = "Trigger and processing — what starts it and what runs"
    data, ev_d = N.data_section(ctx, row)
    data.title = "Afterwards — the data left changed"
    biz, ev_b = _business(ctx, element, row)
    biz.title = "What this means for the business when it runs"
    return _answer(
        "faq.trigger_and_aftermath", element,
        [biz, code, data, _alternatives(ctx, cand, row, how)],
        ev_b + ev_c + ev_d, subject=subj,
        guidance=(
            "Structure it as trigger, then processing, then what is left "
            "behind. Note which steps sit inside a transaction, since that "
            "decides whether a failure half-completes. Event handlers listed "
            "as triggers are wired declaratively — say so."),
        gaps=["Asynchronous follow-on work (scheduled jobs, listeners reached "
              "through the event bus rather than by a direct call) is not "
              "traced by call references and may not appear."])


# ================================================================== 9

@service(
    id="faq.conditions_and_decisions", category="checks", composite=True, faq=9,
    question="What conditions or decisions can change the outcome of this business process?",
    example="Loan approval",
    answers_with="The decision points: documented conditions, enforced checks, "
                 "and the enumerations outcomes are drawn from.",
    when_to_use="Use when an outcome differs between two cases and you need to "
                "know what could have decided it. For the checks alone, use "
                "faq.rules_and_validations.",
    inputs={"element": ELEMENT_INPUT})
def conditions_and_decisions(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    biz, ev_b = _business(ctx, element, row)
    biz.title = "Documented conditions — what the business says decides this"
    rules, ev_r = N.rules_section(ctx, row)
    rules.title = "Decision points enforced in code"

    states = Section(key="states", audience="technical",
                     title="Outcomes and states this can take")
    ev_s: list[Evidence] = []
    if row is not None:
        reached = N.walk_downstream(ctx, row, 2)
        enums = [r for r in reached if r["kind"] == "enum"]
        for i, r in enumerate(enums[:12], 1):
            values = ctx.store.query(
                "SELECT name FROM node n JOIN edge e ON e.src_id = n.id"
                " AND e.kind='belongs_to' WHERE e.dst_id = ? AND n.kind='field'"
                " ORDER BY n.start_line LIMIT 24", (r["id"],))
            states.steps.append({
                "n": i, "title": N.short_fqn(r["fqn"]), "origin": "derived",
                "text": "", "ref": N.ref(r),
                "meta": [f"{r['asset_id']} · enumeration"]
                        + ([", ".join(v["name"] for v in values)] if values else [])})
            ev_s.append(N.evidence(r))
    if not states.steps:
        states.empty = ("No enumeration is reached from this element, so the "
                        "set of possible outcomes is not derivable here.")

    return _answer(
        "faq.conditions_and_decisions", element,
        [biz, rules, states, _alternatives(ctx, cand, row, how)],
        ev_b + ev_r + ev_s, subject=subj,
        guidance=(
            "Present each decision point with what it decides and where it "
            "lives. State the hard limit up front: the platform does not model "
            "branch conditions, so it can show WHERE a decision is made and "
            "WHICH outcomes exist, but not the rule that picks between them "
            "unless the documentation states it."),
        gaps=["Inline `if`/`switch` conditions are not modelled. A decision "
              "tree cannot be produced from this evidence — what is offered is "
              "the set of places a decision is taken."])


# ================================================================== 10

@service(
    id="faq.where_implemented", category="navigation", composite=True, faq=10,
    question="Where is this business functionality implemented in the codebase?",
    example="Interest calculation",
    answers_with="Applications, packages, files, classes and methods — the "
                 "places to open.",
    when_to_use="The first question when you are new to an area. For what "
                "those places contain and call, use faq.code_inventory or "
                "faq.source_execution_flow.",
    inputs={"element": ELEMENT_INPUT})
def where_implemented(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    names = N.asset_names(ctx)

    loc = Section(key="locations", audience="technical",
                  title="Where to look — applications, packages and files")
    ev: list[Evidence] = []
    pool = ([row] if row is not None else []) + [
        r for r in cand if row is None or r["id"] != row["id"]]
    if pool:
        by_asset: dict[str, list[Any]] = {}
        for r in pool[:24]:
            by_asset.setdefault(r["asset_id"], []).append(r)
        for i, (asset, rows) in enumerate(sorted(by_asset.items()), 1):
            packages = sorted({N.package_of(r["fqn"]) for r in rows})
            loc.steps.append({
                "n": i, "title": f"{names.get(asset, asset)} ({asset})",
                "origin": "derived", "text": "",
                "meta": [f"{len(rows)} element(s)",
                         "packages: " + ", ".join(packages[:4])],
                "items": [{
                    "title": N.short_fqn(r["fqn"]),
                    "text": N.one_line(r["signature"]) or r["fqn"],
                    "meta": [r["kind"],
                             f"{r['path']}:{r['start_line']}-{r['end_line']}"]
                            + ([f"route /{N.attrs_of(r)['route']}"]
                               if N.attrs_of(r).get("route") else []),
                    "ref": N.ref(r), "origin": "derived"} for r in rows]})
            for r in rows[:6]:
                ev.append(N.evidence(r))
        loc.lead = ("Matched by name." if how in ("fqn", "simple_name")
                    else "Matched by screen route." if how == "route"
                    else "Matched by table name." if how == "table"
                    else "Matched because these names carry the words you "
                         "asked about." if how == "name_tokens"
                    else "Matched by meaning — these are the closest elements "
                         "in the indexed estate, ordered by relevance.")
    else:
        loc.empty = ("Nothing in the registered estate matches this name or "
                     "this description closely enough to point at a file.")

    biz, ev_b = _business(ctx, element, row, max_steps=8)
    biz.title = "What this functionality is — the business description"
    return _answer(
        "faq.where_implemented", element, [loc, biz], ev + ev_b, subject=subj,
        guidance=(
            "Give the owning application first, then package, then file and "
            "line. If the match was by meaning rather than by name, say so "
            "before listing anything."))


# ================================================================== 11

@service(
    id="faq.error_root_cause", category="flow", composite=True, faq=11,
    question="Why did this error or exception occur, and what is its root cause?",
    example="NullPointerException in loan processing",
    answers_with="The code that raises it, what guards it, and an explicit "
                 "statement of what the platform cannot know.",
    when_to_use="Use with an exception class name or an error message. For the "
                "full call path leading into it, use faq.error_technical_flow.",
    inputs={"element": "the exception class, error message, or symptom"})
def error_root_cause(ctx: Context, element: str) -> ServiceResult:
    fail, ev_f, stats = N.failure_section(ctx, element)
    row, cand, how, subj = _prep(ctx, element)
    rules, ev_r = N.rules_section(ctx, row)
    rules.title = "Guards around it — what is checked before this point"
    biz, ev_b = _business(ctx, element, row, max_steps=6)
    biz.title = "What the business expects here — documented behaviour"
    return _answer(
        "faq.error_root_cause", element, [fail, rules, biz],
        ev_f + ev_r + ev_b, subject=subj, findings=[stats],
        guidance=(
            "Lead with the throw sites — that is where the failure originates. "
            "Then say, without hedging, that the platform locates the origin "
            "but does not know the runtime cause: it has no logs, no stack "
            "trace and no record state. Offer the guards as the places a "
            "missing precondition would have been caught."),
        gaps=["A root cause needs runtime evidence — the stack trace, the "
              "input, the record. The knowledge base holds neither runtime "
              "state nor logs, so what is offered is the origin and the "
              "guards, not the cause."],
        needed=["the stack trace, to name the exact frame",
                "the record or input the failure occurred on"])


# ================================================================== 12

@service(
    id="faq.error_technical_flow", category="flow", composite=True, faq=12,
    question="What is the complete technical flow leading to this error?",
    example="Payment processing timeout",
    answers_with="The failure path: the throw site, everything that reaches it, "
                 "and the data in play.",
    when_to_use="Use after faq.error_root_cause has identified where a failure "
                "originates, to see the route into it.",
    inputs={"element": "the exception class, error message, or failing area"})
def error_technical_flow(ctx: Context, element: str) -> ServiceResult:
    fail, ev_f, stats = N.failure_section(ctx, element)
    row, cand, how, subj = _prep(ctx, element)
    callers, ev_ca, _ = N.callers_section(ctx, row, 3)
    callers.title = "The path into it — what reaches this code"
    code, ev_c, _ = N.execution_flow(ctx, row, how)
    code.title = "The path out of it — what this code goes on to call"
    biz, ev_b = _business(ctx, element, row, max_steps=5)
    biz.title = "The business activity this failure sits inside"
    return _answer(
        "faq.error_technical_flow", element,
        [fail, callers, code, biz, _alternatives(ctx, cand, row, how)],
        ev_f + ev_ca + ev_c + ev_b, subject=subj, findings=[stats],
        guidance=(
            "Present the failure path as: what can reach this code, where it "
            "raises, and what it had already called by then. This is the set "
            "of possible routes derived from call references — not the route "
            "a particular failure actually took. Say which it is."),
        gaps=["This is every derivable route into the failing code, not the "
              "one that ran. Only a stack trace identifies the actual path."])


# ================================================================== 13

@service(
    id="faq.change_blast_radius", category="impact", composite=True, faq=13,
    question="What will be affected if I change this functionality?",
    example="Change loan interest rate calculation",
    answers_with="Dependants, direct then indirect, with cross-application "
                 "calls separated out.",
    when_to_use="The question before editing. For the business consequences "
                "alongside the technical ones, use faq.change_impact.",
    inputs={"element": ELEMENT_INPUT,
            "depth": "how many call hops of dependants to walk (1-4, default 2)"})
def change_blast_radius(ctx: Context, element: str, depth: int = 2) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    callers, ev_ca, stats = N.callers_section(ctx, row, int(depth))
    data, ev_d = N.data_section(ctx, row)
    biz, ev_b = _business(ctx, element, row, max_steps=6)
    biz.title = "What this functionality does for the business"
    return _answer(
        "faq.change_blast_radius", element,
        [callers, data, biz, _alternatives(ctx, cand, row, how)],
        ev_ca + ev_d + ev_b, subject=subj, findings=[stats],
        guidance=(
            "Direct dependants first, then indirect, and pull cross-"
            "application entries out on their own — those are the expensive "
            "surprises. Close with the coverage caveat rather than implying "
            "the list is exhaustive."))


# ================================================================== 14

@service(
    id="faq.change_impact", category="impact", composite=True, faq=14,
    question="What is the technical and business impact of changing this code?",
    example="Modify repayment calculation",
    answers_with="Both consequences: who breaks technically, and which business "
                 "activity is affected.",
    when_to_use="Use when a change needs to be explained to people who do not "
                "read code as well as to those who do. For the dependant list "
                "alone, use faq.change_blast_radius.",
    inputs={"element": ELEMENT_INPUT,
            "depth": "how many call hops of dependants to walk (1-4, default 2)"})
def change_impact(ctx: Context, element: str, depth: int = 2) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    biz, ev_b = _business(ctx, element, row)
    biz.title = "Business impact — the activity this change lands on"
    callers, ev_ca, stats = N.callers_section(ctx, row, int(depth))
    callers.title = "Technical impact — what depends on this code"
    data, ev_d = N.data_section(ctx, row)
    rules, ev_r = N.rules_section(ctx, row)
    rules.title = "Rules that would need re-testing"
    mapping, ev_m = N.mapping_section(ctx, element, row)
    return _answer(
        "faq.change_impact", element,
        [biz, callers, rules, data, mapping, _alternatives(ctx, cand, row, how)],
        ev_b + ev_ca + ev_r + ev_d + ev_m, subject=subj, findings=[stats],
        guidance=(
            "Give the business consequence in language a non-engineer can act "
            "on, then the technical dependants, then the checks to re-test. "
            "Where no approved anchor links the business activity to this "
            "code, say that the business half is inferred from documentation "
            "about the same subject — do not present it as a confirmed link."))


# ================================================================== 15

@service(
    id="faq.before_you_change", category="impact", composite=True, faq=15,
    question="What should I know before changing this functionality?",
    example="Loan repayment",
    answers_with="Preconditions, dependencies, risks and what test coverage "
                 "exists — with the unknowns named.",
    when_to_use="The briefing before starting work. Broader than "
                "faq.change_blast_radius: it adds documented behaviour, "
                "enforced rules, transaction boundaries and testing.",
    inputs={"element": ELEMENT_INPUT})
def before_you_change(ctx: Context, element: str) -> ServiceResult:
    row, cand, how, subj = _prep(ctx, element)
    biz, ev_b = _business(ctx, element, row)
    biz.title = "What it is meant to do — read this first"
    rules, ev_r = N.rules_section(ctx, row)
    rules.title = "Behaviour you must preserve"
    callers, ev_ca, stats = N.callers_section(ctx, row, 2)
    callers.title = "Who will feel it"
    data, ev_d = N.data_section(ctx, row)
    data.title = "Data you could damage"

    risk = Section(key="risks", audience="technical",
                   title="Risks and testing")
    names = N.asset_names(ctx)
    if row is not None:
        a = N.attrs_of(row)
        if a.get("transactional"):
            risk.steps.append({
                "n": len(risk.steps) + 1, "origin": "derived",
                "title": "Runs inside a database transaction",
                "text": "A failure part-way through rolls back; a change that "
                        "moves work outside this boundary changes that.",
                "meta": [f"{row['path']}:{row['start_line']}"], "ref": N.ref(row)})
        cross = stats.get("cross_application", [])
        if cross:
            risk.steps.append({
                "n": len(risk.steps) + 1, "origin": "derived",
                "title": "Other applications call this",
                "text": "A change here is a cross-application change: "
                        + ", ".join(f"{names.get(c, c)} ({c})" for c in cross),
                "meta": ["coordinate with the owning teams"]})
        stale = ctx.store.query(
            "SELECT COUNT(*) n FROM anchor WHERE target_asset = ?"
            " AND target_fqn = ? AND state='stale'", (row["asset_id"], row["fqn"]))
        if stale and stale[0]["n"]:
            risk.steps.append({
                "n": len(risk.steps) + 1, "origin": "curated",
                "title": "Documented meaning is already out of date",
                "text": f"{stale[0]['n']} approved statement(s) about this code "
                        f"are marked stale — the code moved after a human "
                        f"approved the description.",
                "meta": ["re-approve before relying on the documentation"]})

    tests = ctx.store.scalar(
        "SELECT COUNT(*) FROM node WHERE path LIKE '%src/test/%'") or 0
    risk.steps.append({
        "n": len(risk.steps) + 1, "origin": "derived",
        "title": "Test coverage",
        "text": (f"{tests} elements under a test source root are registered."
                 if tests else
                 "No test sources are registered in the estate, so the platform "
                 "cannot tell you what is covered. Treat every behaviour above "
                 "as untested until you confirm otherwise."),
        "meta": ["from the CAP-1 asset register"]})

    return _answer(
        "faq.before_you_change", element,
        [biz, rules, callers, data, risk, _alternatives(ctx, cand, row, how)],
        ev_b + ev_r + ev_ca + ev_d, subject=subj, findings=[stats],
        guidance=(
            "Read as a pre-flight briefing: what it is for, what must keep "
            "working, who is affected, what data is at stake, and what is not "
            "known. The unknowns are the most useful part — lead the risk "
            "section with them rather than burying them."))


# ================================================================== 16

IDENT = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")
CALL = re.compile(r"\b([a-z][A-Za-z0-9_]{2,})\s*\(")


def _mentioned(ctx: Context, text: str, limit: int = 24) -> list[Any]:
    """Elements of the registered estate that a pasted snippet names.

    Only exact identifier matches count. A snippet is not indexed knowledge,
    so nothing about it is interpreted — the question asked of the knowledge
    base is only "does the estate contain something by this name, and what
    depends on it".
    """
    tokens: list[str] = []
    for m in IDENT.findall(text):
        if m not in tokens:
            tokens.append(m)
    for m in CALL.findall(text):
        if m not in tokens:
            tokens.append(m)
    if not tokens:
        return []
    placeholders = ",".join("?" * len(tokens[:40]))
    rank = {"view": 0, "service": 1, "class": 2, "interface": 3, "entity": 4,
            "record": 5, "enum": 6, "event_handler": 7, "method": 8}
    rows = ctx.store.query(
        f"SELECT {N.NODE_COLS} FROM node WHERE name IN ({placeholders})"
        f" AND kind <> 'field' LIMIT 200", tuple(tokens[:40]))
    ordered = sorted(rows, key=lambda r: (rank.get(r["kind"], 9), r["fqn"]))
    return ordered[:limit]


@service(
    id="faq.replacement_analysis", category="impact", composite=True, faq=16,
    question="What will be affected if I replace this implementation with the proposed code?",
    example="Paste the new code, or name the element it would replace",
    answers_with="The integration points the replacement must honour, and what "
                 "the current implementation does that could be lost.",
    when_to_use="Use with a proposed snippet pasted in, or with the name of the "
                "element being replaced. It analyses the ESTATE around the "
                "change; it does not review the proposed code itself.",
    inputs={"element": "the element being replaced — or paste the proposed code"})
def replacement_analysis(ctx: Context, element: str) -> ServiceResult:
    names = N.asset_names(ctx)
    mentioned = _mentioned(ctx, element)
    looks_like_code = bool(mentioned) and len(element.strip()) > 60

    row, cand, how, subj = _prep(ctx, element)
    if row is None and mentioned:
        row, cand, how = mentioned[0], mentioned, "snippet"
        subj = N.subject_dict(row, how, names)

    touch = Section(
        key="integration_points", audience="technical",
        title="Integration points — what the replacement must keep honouring")
    ev_t: list[Evidence] = []
    if mentioned:
        touch.lead = (
            f"{len(mentioned)} name(s) in this input exist in the registered "
            f"estate. Each is a contract the replacement inherits.")
        for i, r in enumerate(mentioned, 1):
            callers = ctx.store.scalar(
                "SELECT COUNT(*) FROM edge WHERE kind='invokes' AND dst_id = ?",
                (r["id"],)) or 0
            touch.steps.append({
                "n": i, "title": N.short_fqn(r["fqn"]), "origin": "derived",
                "text": N.one_line(r["signature"]),
                "meta": [f"{names.get(r['asset_id'], r['asset_id'])} "
                         f"({r['asset_id']}) · {r['kind']}",
                         f"{callers} call site(s) in the estate",
                         f"{r['path']}:{r['start_line']}"],
                "ref": N.ref(r)})
            ev_t.append(N.evidence(r))
    else:
        touch.empty = (
            "No name in this input matches an element of the registered "
            "estate, so no integration point can be identified.")
        touch.notes.append(
            "Paste the proposed code, or name the class or method it would "
            "replace — the analysis works from names the estate already knows.")

    callers, ev_ca, stats = N.callers_section(ctx, row, 2)
    callers.title = "Callers that would have to keep working"
    rules, ev_r = N.rules_section(ctx, row)
    rules.title = "Behaviour the current implementation enforces"
    data, ev_d = N.data_section(ctx, row)
    data.title = "Data the current implementation touches"
    biz, ev_b = _business(ctx, element if not looks_like_code else
                          N.as_words(row["name"]) if row is not None else element,
                          row)
    biz.title = "What this is for — the business obligation being replaced"

    return _answer(
        "faq.replacement_analysis", element,
        [touch, callers, rules, data, biz, _alternatives(ctx, cand, row, how)],
        ev_t + ev_ca + ev_r + ev_d + ev_b, subject=subj, findings=[stats],
        guidance=(
            "Frame this as a checklist the replacement must satisfy: these "
            "callers keep working, these rules keep being enforced, this data "
            "keeps being written. State clearly that the proposed code has NOT "
            "been analysed — the platform knows only the registered estate, "
            "so it reports what the replacement must honour, never whether the "
            "new code honours it."),
        gaps=["The proposed code is not part of the knowledge base and has not "
              "been parsed or judged. Everything here describes the estate the "
              "replacement lands in, not the replacement itself."])
