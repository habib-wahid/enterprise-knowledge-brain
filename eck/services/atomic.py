"""CAP-7 atomic services — one clear question category each (BR-47, BR-48).

Every one of these is a read-only query over derived structure and curated
anchors. None calls a model. Each returns evidence with exact source
locations (BR-54) and says plainly when it does not know (BR-55).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..store import source as src
from .registry import service
from .resolve import Ambiguous, Context, Subject, resolve
from .result import ANSWERED, PARTIAL, Evidence, ServiceResult, unknown

MAX_ROWS = 60


# ------------------------------------------------------------------ helpers

def _excerpt(ctx: Context, asset_id: str, path: str, start: int,
             end: int, limit: int = 6) -> str:
    """Source text for evidence, from the filesystem or from the index.

    A serving deployment often has no checked-out source, so this degrades to
    the text captured at index time rather than returning nothing.
    """
    rows = ctx.store.query(
        "SELECT abs_path, rel_path FROM asset WHERE id = ?", (asset_id,))
    if not rows:
        return ""
    asset_dir = src.resolve_asset_dir(rows[0]["abs_path"], rows[0]["rel_path"])
    lines, how = src.read_span(asset_dir, path, start, end)
    if how == "filesystem":
        return "\n".join(lines[:limit])
    text = src.indexed_text(ctx.store, asset_id, path, start)
    return "\n".join((text or "").splitlines()[:limit])


def _ev(ctx: Context, row: Any, excerpt: bool = False) -> Evidence:
    e = Evidence(
        asset_id=row["asset_id"], path=row["path"],
        start_line=row["start_line"], end_line=row["end_line"],
        kind=row["kind"], fqn=row["fqn"],
        origin=row["origin"] if "origin" in row.keys() else "derived",
        confidence=row["confidence"] if "confidence" in row.keys() else 1.0)
    if excerpt:
        e.excerpt = _excerpt(ctx, e.asset_id, e.path, e.start_line, e.end_line)
    return e


def _subject_or_unknown(ctx: Context, sid: str, element: str,
                        asset: str | None) -> tuple[Subject | None, ServiceResult | None]:
    """Shared front door. Turns an unresolvable or ambiguous subject into a
    proper `unknown` result rather than a guess."""
    found = resolve(ctx, element, asset)
    if found is None:
        return None, unknown(
            sid, element,
            f"Nothing in the registered estate matches {element!r}.",
            needed=[
                "a class, method, screen route or table name that exists in scope",
                "or run `eck estate list` to see what is registered",
                "or `eck search` to find the right name in business language",
            ])
    if isinstance(found, Ambiguous):
        return None, unknown(
            sid, element,
            f"{element!r} did not resolve to one element: {found.reason} "
            f"({len(found.candidates)} candidates).",
            needed=["re-ask using one of the candidate names below, exactly"],
            gaps=[f"{c.asset_id} · {c.kind} · {c.fqn}" for c in found.candidates])
    return found, None


def _header(s: Subject) -> dict[str, Any]:
    return {"subject": s.fqn, "kind": s.kind, "asset": s.asset_id,
            "resolved_by": s.how, "resolution_confidence": round(s.confidence, 3)}


# ------------------------------------------------------------------ 1 navigation

@service(
    id="navigation.find", category="navigation",
    question="Where does this thing live in the estate?",
    when_to_use="Use to locate an element by business or technical name before "
                "asking anything else about it. For finding knowledge by topic "
                "rather than by name, use search.knowledge instead.",
    inputs={"element": "class, method, screen route, table, or business phrase",
            "asset": "optional asset id to narrow the search"})
def navigation_find(ctx: Context, element: str, asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "navigation.find", element, asset)
    if failure:
        return failure
    row = ctx.store.query(
        "SELECT * FROM node WHERE id = ?", (subject.node_id,))[0]
    owner = ctx.store.query(
        "SELECT name, role, owner FROM asset WHERE id = ?", (subject.asset_id,))[0]
    return ServiceResult(
        service="navigation.find", question=element, outcome=ANSWERED,
        findings=[{**_header(subject),
                   "owning_asset": owner["name"], "asset_role": owner["role"],
                   "asset_owner": owner["owner"]}],
        evidence=[_ev(ctx, row, excerpt=True)],
        presentation_guidance="State what it is, which registered application "
                              "owns it, and cite the file and line range.")


# ------------------------------------------------------------------ 2 impact

@service(
    id="impact.of_change", category="impact",
    question="What is affected if I change this?",
    when_to_use="Use for blast radius BEFORE editing code. This walks callers "
                "(who depends on this). For the opposite direction — what this "
                "code itself calls — use flow.downstream.",
    inputs={"element": "the thing you intend to change",
            "depth": "how many call hops to walk (default 2)",
            "asset": "optional asset id"})
def impact_of_change(ctx: Context, element: str, depth: int = 2,
                     asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "impact.of_change", element, asset)
    if failure:
        return failure

    depth = max(1, min(int(depth), 4))
    # Blast radius means CALLERS, so the walk follows `invokes` upward only.
    # `belongs_to` points member -> type, so following it here would descend
    # into the subject and report its own members as "affected" — they are
    # not affected by the change, they ARE the change.
    #
    # When the subject is a type, its members seed the walk (callers invoke
    # methods, not classes) and are then excluded from the results, so what
    # comes back is what lies OUTSIDE the subject.
    rows = ctx.store.query(f"""
        WITH RECURSIVE seed(id) AS (
          SELECT ?
          UNION
          SELECT e.src_id FROM edge e
          WHERE e.kind = 'belongs_to' AND e.dst_id = ?
        ),
        up(id, hop) AS (
          SELECT id, 0 FROM seed
          UNION
          SELECT e.src_id, up.hop + 1 FROM edge e JOIN up ON e.dst_id = up.id
          WHERE e.kind = 'invokes' AND up.hop < ?
        )
        SELECT n.*, MIN(up.hop) AS hop,
               (SELECT e2.origin FROM edge e2
                WHERE e2.src_id = n.id AND e2.kind = 'invokes'
                ORDER BY e2.confidence LIMIT 1) AS edge_origin
        FROM up JOIN node n ON n.id = up.id
        WHERE up.hop > 0 AND n.id NOT IN (SELECT id FROM seed)
        GROUP BY n.id ORDER BY hop, n.asset_id, n.fqn LIMIT ?""",
        (subject.node_id, subject.node_id, depth, MAX_ROWS))

    if not rows:
        return ServiceResult(
            service="impact.of_change", question=element, outcome=ANSWERED,
            findings=[{**_header(subject), "affected_count": 0,
                       "statement": "Nothing outside this element calls it, as "
                                    "far as derived structure shows."}],
            evidence=[],
            gaps=["Unresolved call sites mean callers may exist but not be "
                  "derivable — see `eck coverage`."],
            presentation_guidance="Report zero known callers, and state the "
                                  "coverage caveat rather than implying "
                                  "certainty that nothing calls it.")

    cross = {r["asset_id"] for r in rows} - {subject.asset_id}
    inferred = [r for r in rows if (r["edge_origin"] or "derived") == "inferred"]
    findings = [{**_header(subject),
                 "affected_count": len(rows),
                 "hops_walked": depth,
                 "cross_application": sorted(cross),
                 "inferred_links": len(inferred)}]
    for r in rows[:MAX_ROWS]:
        findings.append({
            "affected": r["fqn"], "kind": r["kind"], "asset": r["asset_id"],
            "hops": r["hop"],
            "link_origin": r["edge_origin"] or "derived"})

    return ServiceResult(
        service="impact.of_change", question=element,
        outcome=PARTIAL if inferred else ANSWERED,
        findings=findings, evidence=[_ev(ctx, r) for r in rows[:MAX_ROWS]],
        gaps=([f"{len(inferred)} link(s) are inferred, not derived — verify "
               f"before relying on them (BR-10)"] if inferred else []) +
             ["Call sites the extractor could not resolve are excluded; run "
              "`eck coverage` for the resolution rate."],
        presentation_guidance=(
            "Lead with directly affected callers (hops=1), then indirect. "
            "Call out cross-application entries separately — those are the "
            "expensive surprises. Mark inferred links explicitly as "
            "unverified. Note that members of the subject itself are excluded: "
            "this is what lies OUTSIDE the thing being changed."))


# ------------------------------------------------------------------ 3 flow

@service(
    id="flow.downstream", category="flow",
    question="What does this call, and in what order?",
    when_to_use="Use to understand what a piece of code does by seeing what it "
                "invokes, in source order. For who calls IT, use impact.of_change.",
    inputs={"element": "the starting element", "asset": "optional asset id"})
def flow_downstream(ctx: Context, element: str, asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "flow.downstream", element, asset)
    if failure:
        return failure

    rows = ctx.store.query("""
        SELECT n.*, e.start_line AS call_line, e.origin AS edge_origin,
               e.confidence AS edge_conf
        FROM edge e JOIN node n ON n.id = e.dst_id
        WHERE e.src_id = ? AND e.kind = 'invokes'
        ORDER BY e.start_line LIMIT ?""", (subject.node_id, MAX_ROWS))

    if not rows:
        return unknown(
            "flow.downstream", element,
            f"No outbound calls are derived from {subject.fqn}.",
            needed=["this may be a leaf method, or its calls may be among the "
                    "unresolved receivers — check `eck coverage`"])

    return ServiceResult(
        service="flow.downstream", question=element, outcome=ANSWERED,
        findings=[{**_header(subject), "call_count": len(rows)}] +
                 [{"step": i + 1, "at_line": r["call_line"], "calls": r["fqn"],
                   "kind": r["kind"], "asset": r["asset_id"],
                   "link_origin": r["edge_origin"]}
                  for i, r in enumerate(rows)],
        evidence=[_ev(ctx, r) for r in rows],
        gaps=["Source order is not execution order: branches and loops are not "
              "modelled. Ordered process stages need CAP-4 (not yet built)."],
        presentation_guidance=(
            "Present as a numbered sequence in source order, and state the "
            "caveat that this is lexical order, not a traced execution path."))


# ------------------------------------------------------------------ 4 checks

@service(
    id="checks.enforced_in", category="checks",
    question="What validations or rules are enforced here?",
    when_to_use="Use to find where a rule is actually enforced in code. For "
                "where a NEW check should go, use placement.for_check.",
    inputs={"element": "class, service or view to inspect",
            "asset": "optional asset id"})
def checks_enforced_in(ctx: Context, element: str, asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "checks.enforced_in", element, asset)
    if failure:
        return failure

    rows = ctx.store.query("""
        SELECT n.* FROM node n
        JOIN edge e ON e.src_id = n.id AND e.kind = 'belongs_to'
        WHERE e.dst_id = ? AND n.kind IN ('method','event_handler')
          AND (n.name LIKE 'validate%' OR n.name LIKE 'check%'
               OR n.name LIKE 'is%' OR n.name LIKE 'can%'
               OR n.name LIKE 'assert%' OR n.name LIKE '%Validation%')
        ORDER BY n.start_line LIMIT ?""", (subject.node_id, MAX_ROWS))

    roles = ctx.store.query("""
        SELECT * FROM node WHERE kind='security_role' AND asset_id = ?
        ORDER BY name LIMIT 10""", (subject.asset_id,))

    if not rows:
        return unknown(
            "checks.enforced_in", element,
            f"No validation-shaped members were found on {subject.fqn}.",
            needed=["checks may be inline conditionals, which structural "
                    "extraction does not identify as checks",
                    "CAP-4 behaviour modelling (M6) is what would list checks "
                    "in process order"],
            gaps=[f"{len(roles)} security roles exist in {subject.asset_id} and "
                  f"may enforce rules declaratively"])

    return ServiceResult(
        service="checks.enforced_in", question=element, outcome=PARTIAL,
        findings=[{**_header(subject), "check_count": len(rows)}] +
                 [{"check": r["name"], "signature": r["signature"],
                   "at": f"{r['path']}:{r['start_line']}"} for r in rows],
        evidence=[_ev(ctx, r, excerpt=True) for r in rows[:12]],
        gaps=["Identified by naming convention (validate*/check*/is*/can*). "
              "Inline conditional checks are NOT included, so this is a floor, "
              "not a complete list."],
        presentation_guidance=(
            "List each check with its location. State clearly that this finds "
            "named validation methods only and may miss inline conditions."))


# ------------------------------------------------------------------ 5 effects

@service(
    id="effects.of", category="effects",
    question="What data does this read or write?",
    when_to_use="Use to find the persistence footprint of a change. For the "
                "physical table behind an entity, resolve the entity directly "
                "with navigation.find.",
    inputs={"element": "the element to inspect", "asset": "optional asset id"})
def effects_of(ctx: Context, element: str, asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "effects.of", element, asset)
    if failure:
        return failure

    rows = ctx.store.query("""
        SELECT DISTINCT n.*, e.kind AS edge_kind FROM edge e
        JOIN node n ON n.id = e.dst_id
        WHERE e.src_id = ? AND n.kind IN ('entity','store')
        ORDER BY n.kind, n.fqn LIMIT ?""", (subject.node_id, MAX_ROWS))

    tx = ctx.store.query(
        "SELECT json_extract(attrs,'$.transactional') t FROM node WHERE id = ?",
        (subject.node_id,))
    transactional = bool(tx and tx[0]["t"])

    if not rows:
        return unknown(
            "effects.of", element,
            f"No entity or table access is derived directly from {subject.fqn}.",
            needed=["persistence may happen through DataManager or a repository "
                    "whose receiver the extractor could not resolve",
                    "check `flow.downstream` for what it calls instead"],
            gaps=[f"transactional boundary present: {transactional}"])

    return ServiceResult(
        service="effects.of", question=element, outcome=PARTIAL,
        findings=[{**_header(subject), "transactional": transactional,
                   "touched": len(rows)}] +
                 [{"touches": r["fqn"], "kind": r["kind"],
                   "relationship": r["edge_kind"]} for r in rows],
        evidence=[_ev(ctx, r) for r in rows],
        gaps=["Reads and writes are not distinguished: Jmix persistence goes "
              "through DataManager, so direction is not derivable from "
              "structure alone. Treat these as 'touches'."],
        presentation_guidance=(
            "Name the entities and tables touched. Be explicit that read vs "
            "write is NOT known, and say whether a transaction wraps it."))


# ------------------------------------------------------------------ 6 explanation

@service(
    id="explanation.of", category="explanation",
    question="What does this mean in business terms?",
    when_to_use="Use to get human-curated meaning for a code element. Returns "
                "only statements a reviewer approved and anchored (BR-16) — "
                "never an inferred description.",
    inputs={"element": "the element to explain", "asset": "optional asset id"})
def explanation_of(ctx: Context, element: str, asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "explanation.of", element, asset)
    if failure:
        return failure

    rows = ctx.store.query("""
        SELECT * FROM anchor WHERE target_asset = ? AND target_fqn = ?
        AND state IN ('resolved','stale') ORDER BY confidence DESC""",
        (subject.asset_id, subject.fqn))

    if not rows:
        anchored_total = ctx.store.scalar(
            "SELECT COUNT(*) FROM anchor WHERE state='resolved'") or 0
        return unknown(
            "explanation.of", element,
            f"No curated business meaning is anchored to {subject.fqn}.",
            needed=["a reviewer must approve an anchor linking a wiki statement "
                    "to this element: `eck anchors propose` then `eck anchors review`"],
            gaps=[f"only {anchored_total} anchor(s) exist estate-wide, so most "
                  f"elements have no curated meaning yet"])

    stale = [r for r in rows if r["state"] == "stale"]
    findings = [{**_header(subject), "statements": len(rows)}]
    ev: list[Evidence] = []
    for r in rows:
        findings.append({
            "statement": r["statement"][:1200],
            "state": r["state"],
            "approved_by": r["reviewed_by"], "approved_at": r["reviewed_at"],
            "confidence": r["confidence"],
            "source": f"{r['doc_path']}:{r['doc_start_line']}-{r['doc_end_line']}"})
        ev.append(Evidence(
            asset_id="WIKI", path=r["doc_path"],
            start_line=r["doc_start_line"], end_line=r["doc_end_line"],
            kind="statement", fqn=r["target_fqn"], origin="curated",
            confidence=r["confidence"]))

    return ServiceResult(
        service="explanation.of", question=element,
        outcome=PARTIAL if stale else ANSWERED,
        findings=findings, evidence=ev,
        gaps=([f"{len(stale)} anchor(s) are STALE: the code changed after a "
               f"human approved the statement, so the meaning may no longer "
               f"hold (BR-18)"] if stale else []),
        presentation_guidance=(
            "This is CURATED interpretation, not derived fact — attribute it to "
            "the named reviewer and date (BR-20). If any anchor is stale, warn "
            "prominently that the code moved underneath the statement."))


# ------------------------------------------------------------------ 7 placement

@service(
    id="placement.for_check", category="placement",
    question="Where should a new check go?",
    when_to_use="Use when adding a new validation and you need the right place "
                "for it. Suggests existing validation sites as precedent; it "
                "does not decide for you.",
    inputs={"element": "the process or element the check concerns",
            "asset": "optional asset id"})
def placement_for_check(ctx: Context, element: str, asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "placement.for_check", element, asset)
    if failure:
        return failure

    peers = ctx.store.query("""
        SELECT n.* FROM node n
        WHERE n.asset_id = ? AND n.kind IN ('method','event_handler')
          AND (n.name LIKE 'validate%' OR n.name LIKE 'can%')
        ORDER BY n.fqn LIMIT 15""", (subject.asset_id,))

    return ServiceResult(
        service="placement.for_check", question=element,
        outcome=PARTIAL if peers else ANSWERED,
        findings=[{**_header(subject),
                   "statement": "Existing validation sites in the same "
                                "application, offered as precedent."}] +
                 [{"precedent": p["fqn"], "at": f"{p['path']}:{p['start_line']}"}
                  for p in peers],
        evidence=[_ev(ctx, p) for p in peers[:10]],
        gaps=["This is precedent, not a recommendation. Correct placement "
              "depends on process order, which needs CAP-4 (M6)."],
        presentation_guidance=(
            "Offer these as precedent and say explicitly that the platform is "
            "NOT recommending a location — it lacks process-order knowledge."))


# ------------------------------------------------------------------ 8 search

@service(
    id="search.knowledge", category="search",
    question="What do we know about this topic?",
    when_to_use="Use for open questions in business language when you do not "
                "know an element name. To locate a NAMED thing, use "
                "navigation.find instead.",
    inputs={"question": "an ordinary-language question",
            "source": "'wiki' or 'code' to target one kind (BR-34)",
            "asset": "optional asset id", "limit": "max results"})
def search_knowledge(ctx: Context, question: str, source: str = None,
                     asset: str = None, limit: int = 8) -> ServiceResult:
    from .retrieval import verdict

    hits = ctx.retriever.search(question, limit=int(limit), source=source,
                                asset_id=asset)
    v = verdict(hits)
    if v == "none":
        return unknown(
            "search.knowledge", question,
            "Nothing in the indexed estate matches closely enough to be useful.",
            needed=["rephrase using terms the system would use",
                    "or check `eck coverage` — the material may not be in scope"])

    return ServiceResult(
        service="search.knowledge", question=question,
        outcome=ANSWERED if v == "strong" else PARTIAL,
        findings=[{"match_quality": v, "results": len(hits)}] +
                 [{"heading": h.heading, "source": h.source, "asset": h.asset_id,
                   "matched_by": h.matched_by,
                   "similarity": round(h.similarity, 3) if h.similarity else None,
                   "at": f"{h.path}:{h.start_line}-{h.end_line}"} for h in hits],
        evidence=[Evidence(
            asset_id=h.asset_id, path=h.path, start_line=h.start_line,
            end_line=h.end_line, kind=h.source, fqn=h.heading,
            origin=h.origin, confidence=h.similarity or 0.5,
            excerpt=h.text[:600]) for h in hits],
        gaps=([] if v == "strong" else
              ["No result cleared the confidence threshold — treat these as "
               "leads, not as an answer (BR-55)."]),
        presentation_guidance=(
            "Distinguish curated wiki statements from derived code. If "
            "match_quality is 'weak', say up front that the platform is not "
            "confident these answer the question."))


# ------------------------------------------------------------------ 9 detail

@service(
    id="detail.source", category="detail",
    question="Show me the actual source at this location.",
    when_to_use="Use to read the full text behind any evidence reference "
                "returned by another service (BR-38).",
    inputs={"element": "element whose source you want",
            "asset": "optional asset id"})
def detail_source(ctx: Context, element: str, asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "detail.source", element, asset)
    if failure:
        return failure
    row = ctx.store.query("SELECT * FROM node WHERE id = ?", (subject.node_id,))[0]
    text = _excerpt(ctx, subject.asset_id, subject.path, subject.start_line,
                    subject.end_line, limit=400)
    return ServiceResult(
        service="detail.source", question=element, outcome=ANSWERED,
        findings=[{**_header(subject),
                   "lines": f"{subject.start_line}-{subject.end_line}"}],
        evidence=[Evidence(
            asset_id=subject.asset_id, path=subject.path,
            start_line=subject.start_line, end_line=subject.end_line,
            kind=subject.kind, fqn=subject.fqn, origin=row["origin"],
            confidence=1.0, excerpt=text)],
        presentation_guidance="Show the source verbatim with its file and line "
                              "range. Do not paraphrase code as behaviour.")


# ------------------------------------------------------------------ 10 configuration

@service(
    id="configuration.affecting", category="configuration",
    question="What configuration changes this behaviour?",
    when_to_use="Use when behaviour seems environment-dependent. Reports "
                "config-bearing fields on the element, with current values "
                "for any that are on the CAP-6 reference-data allow-list.",
    inputs={"element": "the element to inspect", "asset": "optional asset id"})
def configuration_affecting(ctx: Context, element: str,
                            asset: str = None) -> ServiceResult:
    subject, failure = _subject_or_unknown(ctx, "configuration.affecting",
                                           element, asset)
    if failure:
        return failure

    rows = ctx.store.query("""
        SELECT n.* FROM node n
        JOIN edge e ON e.src_id = n.id AND e.kind='belongs_to'
        WHERE e.dst_id = ? AND n.kind='field'
          AND (n.attrs LIKE '%Value%' OR n.attrs LIKE '%ConfigurationProperties%'
               OR n.attrs LIKE '%DependsOnProperties%')
        ORDER BY n.name LIMIT ?""", (subject.node_id, MAX_ROWS))

    if not rows:
        return unknown(
            "configuration.affecting", element,
            f"No configuration-bearing fields are derived on {subject.fqn}.",
            needed=["a param-level @Value is not derivable yet — only field-"
                    "level annotations are (a disclosed extractor gap)",
                    "or this element genuinely has no configuration switch"])

    import json as _json

    fields, ev = [], []
    unlisted = 0
    for r in rows:
        attrs = _json.loads(r["attrs"])
        key = attrs.get("config_key")
        entry: dict[str, Any] = {"field": r["name"], "type": r["signature"],
                                 "at": f"{r['path']}:{r['start_line']}"}
        if key:
            entry["config_key"] = key
            values = ctx.store.query(
                "SELECT rv.*, rs.reason FROM refdata_value rv"
                " JOIN refdata_source rs ON rs.id = rv.source_id"
                " WHERE rv.source_id = ? ORDER BY rv.start_line", (key,))
            if values:
                entry["current_value"] = _json.loads(values[0]["value"])
                entry["snapshot_at"] = values[0]["snapshot_at"]        # BR-44
                entry["allow_list_reason"] = values[0]["reason"]       # BR-41
                for v in values:
                    ev.append(Evidence(
                        asset_id=v["asset_id"], path=v["path"],
                        start_line=v["start_line"], end_line=v["start_line"],
                        kind="config_value", fqn=key, origin="derived",
                        confidence=1.0))
            else:
                unlisted += 1
        fields.append(entry)

    gaps = []
    if unlisted:
        gaps.append(f"{unlisted} config key(s) found in code are not on the "
                    f"CAP-6 allow-list (register/refdata.yaml), so no current "
                    f"value is shown for them — BR-45 forbids reading a key "
                    f"that was not explicitly approved.")

    return ServiceResult(
        service="configuration.affecting", question=element,
        outcome=ANSWERED if any("current_value" in f for f in fields) else PARTIAL,
        findings=[{**_header(subject), "config_fields": len(fields)}] + fields,
        evidence=[_ev(ctx, r) for r in rows] + ev,
        gaps=gaps,
        presentation_guidance="Name each setting. Where a current value is "
                              "present, state its snapshot age (BR-44) — it "
                              "is a git-tracked default, not a live read.")


@service(
    id="configuration.reference_data", category="configuration",
    question="What is the approved value of this reference-data key or table?",
    when_to_use="Use for a specific config key or table name you already "
                "know. Only ever answers from the CAP-6 allow-list — a key "
                "or table not on it returns unknown, never a guess (BR-45).",
    inputs={"key": "a config property key or a reference table name"})
def configuration_reference_data(ctx: Context, key: str) -> ServiceResult:
    src_row = ctx.store.query(
        "SELECT * FROM refdata_source WHERE id = ?", (key,))
    if not src_row:
        excluded = ctx.store.query(
            "SELECT reason FROM refdata_excluded WHERE key_or_table = ?", (key,))
        if excluded:
            return unknown(
                "configuration.reference_data", key,
                f"{key!r} was explicitly excluded from reference data.",
                needed=[f"nothing — this is deliberate: {excluded[0]['reason']}"])
        return unknown(
            "configuration.reference_data", key,
            f"{key!r} is not on the CAP-6 allow-list "
            f"(register/refdata.yaml).",
            needed=["only allow-listed keys/tables can be read (BR-45) — "
                    "add it to register/refdata.yaml with a reason, if it "
                    "should be tracked"])

    source = src_row[0]
    values = ctx.store.query(
        "SELECT * FROM refdata_value WHERE source_id = ? ORDER BY label",
        (key,))
    if not values:
        return ServiceResult(
            service="configuration.reference_data", question=key,
            outcome=PARTIAL,
            findings=[{"key": key, "kind": source["kind"],
                      "reason": source["reason"], "values_found": 0}],
            gaps=[f"{key!r} is allow-listed but nothing was captured for it "
                 f"at the last refresh — the key or table name may not "
                 f"match what is actually in source"],
            presentation_guidance="State that this is approved for tracking "
                                  "but currently empty.")

    import json as _json
    return ServiceResult(
        service="configuration.reference_data", question=key, outcome=ANSWERED,
        findings=[{"key": key, "kind": source["kind"],
                  "reason": source["reason"], "values_found": len(values)}] +
                 [{"label": v["label"], "value": _json.loads(v["value"]),
                   "snapshot_at": v["snapshot_at"]} for v in values],
        evidence=[Evidence(
            asset_id=v["asset_id"], path=v["path"], start_line=v["start_line"],
            end_line=v["start_line"], kind=source["kind"], fqn=key,
            origin="derived", confidence=1.0) for v in values],
        gaps=[f"snapshot taken at refresh time from git-tracked source "
             f"(BR-40) — not a live read; oldest snapshot here is "
             f"{min(v['snapshot_at'] for v in values)[:10]}"],
        presentation_guidance="State every value with its snapshot date "
                              "(BR-44) and the reason it is tracked (BR-41). "
                              "Never present this as a live production read.")


# ------------------------------------------------------------------ 12 process (CAP-4)

@service(
    id="flow.process_stages", category="flow",
    question="What are the ordered stages of this business process?",
    when_to_use="Use for a named end-to-end process (e.g. 'salary hold and "
                "release'). Returns curated, human-authored stage order with "
                "derived evidence per stage — checks, effects and failure "
                "sites. For what one piece of code calls, use flow.downstream "
                "instead; source order is not process order.",
    inputs={"process": "the process name or id"})
def flow_process_stages(ctx: Context, process: str) -> ServiceResult:
    rows = ctx.store.query(
        "SELECT * FROM process WHERE id = ? OR name LIKE ?",
        (process, f"%{process}%"))
    if not rows:
        known = ctx.store.query("SELECT id, name FROM process")
        return unknown(
            "flow.process_stages", process,
            f"No curated process matches {process!r}.",
            needed=["a process name from the list below — CAP-4 only covers "
                    "processes an engineer has explicitly authored"],
            gaps=[f"curated: {r['id']} ({r['name']})" for r in known] or
                 ["no process has been curated yet"])

    proc = rows[0]
    stages = ctx.store.query(
        "SELECT * FROM process_stage WHERE process_id = ? ORDER BY ordinal",
        (proc["id"],))

    findings: list[dict[str, Any]] = [{
        "process": proc["name"], "description": proc["description"],
        "stage_count": len(stages),
        "authored_by": proc["authored_by"], "authored_at": proc["authored_at"]}]
    evidence: list[Evidence] = []
    gaps: list[str] = []
    prior_assets: set[str] = set()

    for stage in stages:
        anchors = ctx.store.query(
            "SELECT * FROM process_stage_anchor WHERE stage_id = ?",
            (stage["id"],))
        resolved = [a for a in anchors if a["state"] == "resolved"]
        broken = [a for a in anchors if a["state"] == "broken"]

        checks, effects = [], []
        for a in resolved:
            if a["target_node_id"]:
                checks += ctx.store.query("""
                    SELECT n.name, n.path, n.start_line FROM node n
                    JOIN edge e ON e.src_id = n.id AND e.kind='belongs_to'
                    WHERE e.dst_id = ? AND n.kind IN ('method','event_handler')
                      AND (n.name LIKE 'validate%' OR n.name LIKE 'has%'
                           OR n.name LIKE 'can%')
                    LIMIT 6""", (a["target_node_id"],))
                effects += ctx.store.query("""
                    SELECT DISTINCT tn.fqn, tn.kind FROM edge e
                    JOIN node tn ON tn.id = e.dst_id
                    WHERE e.src_id = ? AND tn.kind IN ('entity','store')
                    LIMIT 6""", (a["target_node_id"],))

        # BR-23 — a handover is a stage introducing an asset the process
        # has not touched yet, derived from the anchors, not asserted.
        stage_assets = {a["target_asset"] for a in resolved}
        handover_from = sorted(stage_assets - prior_assets) if prior_assets \
            else []
        prior_assets |= stage_assets

        failures = ctx.store.query(
            "SELECT * FROM process_failure WHERE stage_id = ?", (stage["id"],))

        findings.append({
            "stage": stage["ordinal"] + 1, "key": stage["stage_key"],
            "name": stage["name"], "description": stage["description"],
            "is_entry": bool(stage["is_entry"]),           # BR-24
            "entry_trigger": stage["entry_trigger"],
            "implemented_by": [a["target_fqn"] for a in resolved],
            "broken_anchors": [a["target_fqn"] for a in broken],
            "cross_application_handover": handover_from or None,  # BR-23
            "checks_found": sorted({c["name"] for c in checks}),   # BR-25
            "data_touched": sorted({e["fqn"] for e in effects}),   # BR-26
            "failures": [{"description": f["description"],
                         "at": (f"{f['target_path']}:{f['target_start_line']}"
                                if f["target_path"] else None),
                         "state": f["state"]} for f in failures],  # BR-29
        })

        for a in resolved:
            evidence.append(Evidence(
                asset_id=a["target_asset"], path=a["target_path"],
                start_line=a["target_start_line"], end_line=a["target_end_line"],
                kind=a["target_kind"] or "unknown", fqn=a["target_fqn"],
                origin="curated", confidence=1.0))
        for f in failures:
            if f["state"] == "resolved":
                evidence.append(Evidence(
                    asset_id=proc["id"], path=f["target_path"],
                    start_line=f["target_start_line"],
                    end_line=f["target_start_line"], kind="failure_path",
                    fqn=f["target_fqn"] or "", origin="curated", confidence=1.0))
        if broken:
            gaps.append(f"stage {stage['stage_key']!r}: {len(broken)} anchor(s) "
                        f"no longer resolve — the code they pointed at was "
                        f"renamed or removed since this process was authored")

    gaps.append("Checks and data-touched are found by naming convention on "
               "each stage's anchored elements — a floor, not a complete list "
               "(same limitation as checks.enforced_in and effects.of).")
    gaps.append("Stage ORDER is curated by an engineer, not derived — call "
               "order in the code does not reliably reflect business process "
               "order, especially across applications.")

    return ServiceResult(
        service="flow.process_stages", question=process,
        outcome=PARTIAL if any("broken_anchors" in f and f.get("broken_anchors")
                               for f in findings[1:]) else ANSWERED,
        findings=findings, evidence=evidence, gaps=gaps,
        presentation_guidance=(
            "Present stages in order with their entry trigger on stage one. "
            "Call out cross-application handovers explicitly — they are "
            "where hidden coordination requirements live. Attribute the "
            "stage narrative to its curated origin (BR-20); checks, data and "
            "failure locations are derived evidence supporting it."))


# ------------------------------------------------------------------ 11 status

@service(
    id="status.platform", category="status",
    question="How current and how complete is this knowledge?",
    when_to_use="Use before trusting any other answer, and whenever asked how "
                "fresh or how complete the knowledge base is (BR-69, BR-72).",
    inputs={})
def status_platform(ctx: Context) -> ServiceResult:
    run = ctx.store.query(
        "SELECT * FROM refresh_run ORDER BY started_at DESC LIMIT 1")[0]
    counts = {k: ctx.store.scalar(f"SELECT COUNT(*) FROM {t}") for k, t in (
        ("assets", "asset"), ("nodes", "node"), ("edges", "edge"),
        ("chunks", "chunk"), ("anchors", "anchor"),
        ("processes", "process"), ("refdata_items", "refdata_source"))}
    resolved_calls = ctx.store.scalar(
        "SELECT COUNT(*) FROM edge WHERE kind='invokes'") or 0
    unresolved = ctx.store.scalar("SELECT COUNT(*) FROM unresolved_ref") or 0
    rate = 100.0 * resolved_calls / max(1, resolved_calls + unresolved)
    broken_stages = ctx.store.scalar(
        "SELECT COUNT(*) FROM process_stage_anchor WHERE state='broken'") or 0

    gaps = [f"{100 - rate:.1f}% of call sites are unresolved"]
    gaps.append(f"{counts['processes']} process(es) curated — most business "
               f"processes have no CAP-4 definition yet" if counts["processes"]
               else "no process is curated yet (CAP-4)")
    if broken_stages:
        gaps.append(f"{broken_stages} process stage anchor(s) are broken — "
                    f"see flow.process_stages for which")

    return ServiceResult(
        service="status.platform", question="platform status", outcome=ANSWERED,
        findings=[{
            "estate": run["estate_id"], "last_refresh": run["started_at"],
            "refresh_status": run["status"], **counts,
            "call_resolution_pct": round(rate, 1),
            "anchored_statements": ctx.store.scalar(
                "SELECT COUNT(DISTINCT chunk_id) FROM anchor WHERE state='resolved'") or 0,
            "broken_process_stage_anchors": broken_stages,
        }],
        evidence=[],
        gaps=gaps,
        presentation_guidance="Give the freshness date and the coverage numbers "
                              "plainly. Do not soften the gaps.")
