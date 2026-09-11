"""Turning knowledge-base rows into something two different people can read.

Every FAQ answer has to land with two audiences at once: someone who needs
the business flow and has never opened the source, and someone who needs the
execution path through the code. This module builds both, and it builds them
the same way the rest of the platform works — by reading rows.

The rule that shapes every function here: **no sentence is composed.** A step
of a business flow is the text of a curated wiki chunk or a curated CAP-4
stage description, copied. A step of an execution flow is a node row and an
edge row, labelled. The narration is the ORDERING and the LABELLING; the words
are the estate's own. Where there are no rows, the section says so and names
what is missing, because a readable invention is worse than an honest blank
(BR-55).

Three things follow from that and are worth stating plainly:

  * Business order comes from curated order. CAP-4 stages carry an explicit
    ordinal. Wiki sections carry document order, which in this estate's wiki
    IS the numbered process order ("1.0 LOAN TYPE", "2.0 LOAN APPLICATION").
    Neither is traced execution, and every section says which it is.
  * Technical order comes from the call graph, breadth-first from an entry
    point. Hop distance is not execution sequence and is labelled as depth,
    not as step order.
  * Tables that a given knowledge.db predates (CAP-4, CAP-6) are absent, not
    broken. `has_table` degrades those sections to a stated gap.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .resolve import RESOLVE_FLOOR, Ambiguous, Context, Subject, resolve
from .result import Evidence, Section

# How far the call walk goes. Three hops reaches screen -> service -> helper
# -> entity in this estate; four floods the result with accessors.
WALK_DEPTH = 3
MAX_STEPS = 14
MAX_PER_LAYER = 12
STEP_TEXT = 1600

# Accessors are structurally calls and narratively noise. A flow that lists
# getAmount() beside calculateInterestDailyProduct() reads as if they matter
# equally, and they do not.
ACCESSOR_PREFIXES = ("get", "set", "is", "has")

NODE_COLS = "id, asset_id, kind, name, fqn, signature, path, start_line, end_line, attrs"
# Same columns, qualified — an unqualified `id` is ambiguous once joined.
NODE_COLS_N = ", ".join("n." + c for c in NODE_COLS.split(", "))


# ---------------------------------------------------------------- utilities

def has_table(ctx: Context, name: str) -> bool:
    """Does this knowledge.db carry that table at all?

    A database built before CAP-4 or CAP-6 landed has no `process` and no
    `refdata_source`. That is a gap to report, not an error to raise.
    """
    return bool(ctx.store.query(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)))


def attrs_of(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["attrs"]) if row["attrs"] else {}
    except (KeyError, TypeError, ValueError):
        return {}


def ref(row: Any) -> dict[str, Any]:
    return {"asset": row["asset_id"], "path": row["path"],
            "start_line": row["start_line"], "end_line": row["end_line"]}


def evidence(row: Any, origin: str = "derived", confidence: float = 1.0,
             excerpt: str = "") -> Evidence:
    return Evidence(
        asset_id=row["asset_id"], path=row["path"],
        start_line=row["start_line"], end_line=row["end_line"],
        kind=row["kind"] if "kind" in row.keys() else "chunk",
        fqn=row["fqn"] if "fqn" in row.keys() else (row["heading"] or ""),
        origin=origin, confidence=confidence, excerpt=excerpt)


def is_accessor(row: Any) -> bool:
    if row["kind"] != "method":
        return False
    name = row["name"]
    return (name.startswith(ACCESSOR_PREFIXES)
            and len(name) > 3 and name[3:4].isupper() or
            name.startswith("is") and len(name) > 2 and name[2:3].isupper())


def one_line(text: str | None) -> str:
    """Collapse a signature onto one line, dropping any trailing `//` comment.

    A Java parameter list wraps across lines in source and often carries a
    note per parameter. Both are faithful to the file and both wreck a list
    of call steps, so they are stripped for display only — the file and line
    number beside each entry still lead to the original.
    """
    lines = [ln.split("//", 1)[0] for ln in (text or "").splitlines()]
    return " ".join(" ".join(lines).split())


def short_fqn(fqn: str) -> str:
    """`com.inteacc.lm.view.loanacct.LoanAcctDetailView#calcDailyInterest(..)`
    reads as `LoanAcctDetailView#calcDailyInterest(..)` without losing which
    thing is meant."""
    head, sep, member = one_line(fqn).partition("#")
    simple = head.rsplit(".", 1)[-1]
    return f"{simple}{sep}{member}" if sep else simple


def package_of(fqn: str) -> str:
    head = fqn.partition("#")[0]
    return head.rsplit(".", 1)[0] if "." in head else head


def asset_names(ctx: Context) -> dict[str, str]:
    return {r["id"]: r["name"]
            for r in ctx.store.query("SELECT id, name FROM asset")}


def trim(text: str, limit: int = STEP_TEXT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


# ---------------------------------------------------------------- subject

CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_")


def as_words(name: str) -> str:
    """`calcDailyInterest` -> `calc Daily Interest`.

    Documentation is written in words; code is written in identifiers. When a
    question arrives as an identifier, splitting it is what lets the wiki half
    of the answer find anything at all.
    """
    return " ".join(w for w in CAMEL.split(name or "") if w).strip()



# A flow has to start somewhere that HAS a flow. When several elements match
# a phrase equally well, the one that owns behaviour makes a better entry
# point than whichever leaf method happened to embed nearest the question —
# so relevance order is nudged by kind rather than replaced by it.
ENTRY_PENALTY = {"view": 0, "service": 0, "class": 1, "interface": 2,
                 "event_handler": 2, "enum": 5, "record": 5, "entity": 4,
                 "store": 4, "method": 6, "security_role": 7}


# Words that appear in half the estate and so distinguish nothing.
COMMON = {"view", "detail", "list", "service", "bean", "entity", "impl",
          "manager", "handler", "event", "listener", "job", "util", "helper",
          "data", "base", "abstract", "default", "main", "this", "that",
          "with", "from", "into", "when", "what", "does", "will", "code"}


def query_words(term: str) -> set[str]:
    return {w for w in as_words(term).lower().replace("-", " ").split()
            if len(w) >= 4 and w not in COMMON}


def _name_affinity(row: Any, words: set[str]) -> int:
    """How much of the question is in the element's own name.

    Retrieval matches a chunk, and a chunk is a whole method body — so a
    method that merely MENTIONS loan disbursement in a string ranks beside
    the screen that IS loan disbursement. The name is the tiebreaker: an
    element named for the thing asked about is what the asker meant.
    """
    if not words:
        return 0
    own = set(as_words(row["name"]).lower().split())
    return -3 * min(2, len(words & own))


def _best_entry(candidates: list[Any], term: str = "",
                named: frozenset[str] = frozenset()) -> list[Any]:
    """Re-order candidates by relevance rank, blended with kind and name.

    Three signals, combined rather than ranked against each other: how well
    retrieval matched, whether the kind is worth starting a flow at, and how
    much of the question is in the element's own name. `named` holds the ids
    found by name rather than by meaning — they enter as though they were a
    top-few retrieval hit, so both signals count without either dominating.

    Ties break on kind, so when a screen and a background job score the same
    the screen wins: it is where a person would start reading.
    """
    words = query_words(term)
    scored = []
    for i, r in enumerate(candidates):
        base = min(i, NAME_MATCH_OFFSET) if r["id"] in named else i
        penalty = ENTRY_PENALTY.get(r["kind"], 8)
        scored.append((base + penalty + _name_affinity(r, words), penalty, i, r))
    return [r for *_, r in sorted(scored, key=lambda t: t[:3])]


# Where a name match enters the shortlist. Not at the front: a name match is
# strong evidence, not proof, and `_name_affinity` already rewards it. Entering
# just behind the best retrieval hits lets the two kinds of evidence combine —
# something that is both is what ends up on top.
NAME_MATCH_OFFSET = 2


def _how(ranked: list[Any], named_ids: frozenset[str]) -> str:
    """Report how the WINNER was found, not how the search was run.

    Both signals feed one shortlist, so "retrieval" would be a half-truth
    whenever the element that won did so because its name said what it was.
    """
    if not ranked:
        return "none"
    return "name_tokens" if ranked[0]["id"] in named_ids else "retrieval"


def _merge_candidates(named: list[Any], retrieved: list[Any]) -> list[Any]:
    """Retrieval order is preserved; name matches are appended.

    Their position here does not decide anything — `_best_entry` scores them
    by the NAME_MATCH_OFFSET rule. Appending keeps the retrieval ranking
    exactly as retrieval produced it.
    """
    have = {r["id"] for r in retrieved}
    return list(retrieved) + [r for r in named if r["id"] not in have]


def _promote_owner(ctx: Context, candidates: list[Any]) -> list[Any]:
    """When a search lands on several methods of one class, the class is the answer.

    Asked about "loan account modification", retrieval returns a handful of
    methods on `LoanAcctDetailView`. Any one of them is a poor place to start
    a flow; the screen they all live on is the thing being asked about. Two
    or more matched members is the signal — a single match is just a method.

    Only ever applied on the retrieval path. When someone names a method
    exactly, `resolve` returns it directly and never reaches here.
    """
    if not candidates:
        return candidates
    members = [r for r in candidates if r["kind"] in ("method", "event_handler")]
    if not members:
        return candidates
    owners = _owner(ctx, [r["id"] for r in members])

    tally: dict[str, list[int]] = {}
    for rank, r in enumerate(members):
        owner = owners.get(r["id"])
        if owner and owner["fqn"] != r["fqn"]:
            tally.setdefault(owner["fqn"], []).append(rank)
    if not tally:
        return candidates

    fqn, ranks = min(tally.items(), key=lambda kv: (-len(kv[1]), min(kv[1])))
    if len(ranks) < 2:
        return candidates
    rows = ctx.store.query(
        f"SELECT {NODE_COLS} FROM node WHERE fqn = ? LIMIT 1", (fqn,))
    if not rows:
        return candidates
    owner_row = rows[0]
    # The owner ENTERS the shortlist at its best member's position — it does
    # not jump to the front. A class that owns two weakly-matched methods
    # should not outrank a screen that matched the question directly.
    rest = [r for r in candidates if r["id"] != owner_row["id"]]
    at = min(min(ranks), len(rest))
    return rest[:at] + [owner_row] + rest[at:]


# Kinds worth anchoring a flow on when searching by name — a flow starts at a
# thing that owns behaviour, not at a field or a single accessor.
ANCHOR_KINDS = ("view", "service", "class", "interface", "entity", "store",
                "record", "enum", "event_handler")


def _by_name_tokens(ctx: Context, term: str, asset: str | None,
                    limit: int = 12) -> list[Any]:
    """Elements whose NAME carries every distinctive word of the question.

    `LoanDisbursementServiceBean` is the obvious answer to "loan disbursement"
    and retrieval will not necessarily surface it — embedding similarity is
    computed over method bodies, and the bean's body may say nothing memorable.
    Matching the name directly costs one indexed scan and finds exactly the
    thing a person had in mind when they typed a business name.

    Words are truncated to a six-character stem so "disbursement" still finds
    `...PostDisburse...`; that is deliberately crude, and anything it turns up
    still has to win the ranking against everything else.
    """
    words = sorted(query_words(term))
    if not words:
        return []
    clauses = " AND ".join(["name LIKE ?"] * len(words))
    params: list[Any] = [f"%{w[:6]}%" for w in words]
    scope = ""
    if asset:
        scope = " AND asset_id = ?"
        params.append(asset)
    kinds = ",".join("?" * len(ANCHOR_KINDS))
    params.extend(ANCHOR_KINDS)
    return ctx.store.query(
        f"SELECT {NODE_COLS} FROM node WHERE {clauses}{scope}"
        f" AND kind IN ({kinds}) ORDER BY LENGTH(name), fqn LIMIT {int(limit)}",
        tuple(params))


def code_anchor(ctx: Context, term: str, asset: str | None = None
                ) -> tuple[Any | None, list[Any], str]:
    """Find the code the question is about.

    Returns (primary node row, candidate rows, how). A FAQ takes a business
    phrase as happily as a class name, so name resolution is tried first and
    meaning-based retrieval is the fallback — but a retrieval match is
    reported as such, never dressed up as an exact hit.
    """
    found = resolve(ctx, term, asset)

    def row_for(s: Subject) -> Any | None:
        rows = ctx.store.query(
            f"SELECT {NODE_COLS} FROM node WHERE id = ?", (s.node_id,))
        return rows[0] if rows else None

    if isinstance(found, Subject):
        row = row_for(found)
        return row, ([row] if row else []), found.how
    if isinstance(found, Ambiguous):
        rows = [r for r in (row_for(c) for c in found.candidates) if r]
        # `resolve` reports two different situations as Ambiguous: a real name
        # collision, and a shortlist it matched by meaning. They need different
        # wording, so keep them apart here rather than calling both ambiguous.
        by_meaning = bool(found.candidates) and found.candidates[0].how == "retrieval"
        if not by_meaning:
            return (rows[0] if rows else None), rows, "ambiguous"
        named = _by_name_tokens(ctx, term, asset)
        named_ids = frozenset(r["id"] for r in named)
        ranked = _best_entry(_promote_owner(ctx, _merge_candidates(named, rows)),
                             term, named_ids)
        return (ranked[0] if ranked else None), ranked, _how(ranked, named_ids)

    # Nothing bears this name. Fall back to what the code index MEANS.
    named = _by_name_tokens(ctx, term, asset)
    # Hybrid retrieval fuses keyword and meaning, and the keyword half will
    # match almost anything — "nonsense query" shares tokens with real text.
    # `resolve` applies a cosine floor before it will call a hit a resolution;
    # this path must apply the same one, or a question about nothing at all
    # comes back anchored to whatever happened to share a word with it.
    hits = [h for h in ctx.retriever.search(term, limit=14, source="code",
                                            asset_id=asset)
            if h.similarity is not None and h.similarity >= RESOLVE_FLOOR]
    if not hits:
        if named:
            ranked = _best_entry(named, term)
            return ranked[0], ranked, "name_tokens"
        return None, [], "none"
    chunk_ids = [h.chunk_id for h in hits]
    placeholders = ",".join("?" * len(chunk_ids))
    by_chunk = {r["id"]: r["node_id"] for r in ctx.store.query(
        f"SELECT id, node_id FROM chunk WHERE id IN ({placeholders})",
        tuple(chunk_ids))}
    ordered = [by_chunk[h.chunk_id] for h in hits if by_chunk.get(h.chunk_id)]
    if not ordered:
        if named:
            ranked = _best_entry(named, term)
            return ranked[0], ranked, "name_tokens"
        return None, [], "none"
    placeholders = ",".join("?" * len(ordered))
    rows = {r["id"]: r for r in ctx.store.query(
        f"SELECT {NODE_COLS} FROM node WHERE id IN ({placeholders})",
        tuple(ordered))}
    named_ids = frozenset(r["id"] for r in named)
    pool = _merge_candidates(named, [rows[i] for i in ordered if i in rows])
    candidates = _best_entry(_promote_owner(ctx, pool), term, named_ids)
    return ((candidates[0] if candidates else None), candidates,
            _how(candidates, named_ids))


def subject_dict(row: Any | None, how: str, names: dict[str, str]) -> dict[str, Any]:
    if row is None:
        return {"resolved": False, "how": how}
    a = attrs_of(row)
    return {
        "resolved": True, "how": how, "fqn": row["fqn"], "name": row["name"],
        "kind": row["kind"], "asset": row["asset_id"],
        "application": names.get(row["asset_id"], row["asset_id"]),
        "path": row["path"], "start_line": row["start_line"],
        "end_line": row["end_line"],
        "route": a.get("route"), "table": a.get("table") or a.get("physical_table"),
    }


# ---------------------------------------------------------------- business

def _doc_meta(ctx: Context, path: str) -> dict[str, Any]:
    rows = ctx.store.query(
        "SELECT title, last_author, last_commit_date FROM doc WHERE path = ?",
        (path,))
    if not rows:
        return {"title": path, "last_author": "", "last_commit_date": ""}
    return dict(rows[0])


def _strip_heading(text: str, heading: str) -> str:
    """Chunk text repeats its own heading path as a first line."""
    body = (text or "").split("\n")
    if body and heading and body[0].strip() == heading.strip():
        body = body[1:]
    while body and not body[0].strip():
        body = body[1:]
    return "\n".join(body).strip()


def curated_process(ctx: Context, term: str) -> tuple[Any | None, list[Any]]:
    """A CAP-4 authored process, when one has been written for this name."""
    if not has_table(ctx, "process"):
        return None, []
    rows = ctx.store.query(
        "SELECT * FROM process WHERE id = ? OR name LIKE ? ORDER BY name LIMIT 1",
        (term, f"%{term}%"))
    if not rows:
        return None, []
    stages = ctx.store.query(
        "SELECT * FROM process_stage WHERE process_id = ? ORDER BY ordinal",
        (rows[0]["id"],))
    return rows[0], list(stages)


def business_flow(ctx: Context, term: str, max_steps: int = MAX_STEPS,
                  fallback: str | None = None) -> tuple[Section, list[Evidence]]:
    """The business story, in the estate's own words.

    Preference order is a preference for how much a human vouched for it:
    an authored CAP-4 process (ordered, signed) beats the wiki (written for
    people, but ordered only by the page), and the wiki beats nothing.
    """
    ev: list[Evidence] = []
    section = Section(key="business_flow", audience="business",
                      title="Business flow — what happens, in plain language")

    proc, stages = curated_process(ctx, term)
    if proc is not None and stages:
        section.lead = (
            f"Curated process “{proc['name']}” — {len(stages)} stages, in the "
            f"order an engineer authored and signed"
            + (f" ({proc['authored_by']}, {proc['authored_at'][:10]})"
               if proc["authored_by"] else "") + ".")
        section.steps.append({"n": 0, "title": "About this process",
                              "text": trim(proc["description"]),
                              "origin": "curated"})
        for s in stages:
            anchors = ctx.store.query(
                "SELECT * FROM process_stage_anchor WHERE stage_id = ?",
                (s["id"],))
            failures = ctx.store.query(
                "SELECT * FROM process_failure WHERE stage_id = ?", (s["id"],))
            step = {
                "n": s["ordinal"], "title": s["name"],
                "text": trim(s["description"]), "origin": "curated",
                "meta": [],
            }
            if s["is_entry"]:
                step["meta"].append(f"starts the process — {s['entry_trigger']}"
                                    if s["entry_trigger"] else "starts the process")
            for a in anchors:
                step["meta"].append(
                    f"implemented in {short_fqn(a['target_fqn'])} "
                    f"({a['target_asset']})")
                if a["target_path"]:
                    ev.append(Evidence(
                        asset_id=a["target_asset"], path=a["target_path"],
                        start_line=a["target_start_line"] or 1,
                        end_line=a["target_end_line"] or (a["target_start_line"] or 1),
                        kind=a["target_kind"] or "code", fqn=a["target_fqn"],
                        origin="curated", confidence=1.0))
            for f in failures:
                step["meta"].append(f"can fail: {f['description']}")
            section.steps.append(step)
        section.notes.append(
            "Stage order is curated and authoritative — it was authored by a "
            "named engineer, not inferred from code.")
        return section, ev

    # ---- wiki: curated documentation, read in the order the page sets out
    #
    # The same cosine floor as everywhere else. Quoting a curated page as the
    # business description of something asserts that the page is ABOUT it, and
    # a shared keyword is not that assertion — "recipe for brownies" shares
    # words with payroll documentation and has nothing to do with it.
    def _wiki(q: str) -> list[Any]:
        return [h for h in ctx.retriever.search(q, limit=8, source="wiki")
                if h.similarity is not None and h.similarity >= RESOLVE_FLOOR]

    hits = _wiki(term)
    if not hits and fallback and fallback.strip().lower() != term.strip().lower():
        hits = _wiki(fallback)
        if hits:
            section.notes.append(
                f"Nothing was documented under “{term}”, so the documentation "
                f"was searched for “{fallback}” instead — the words behind the "
                f"resolved element's name.")
    if not hits:
        section.empty = (
            f"No curated business documentation in the knowledge base matches "
            f"“{term}”. The platform will not describe a business flow it "
            f"cannot quote.")
        section.notes.append(
            "Try the wording the documentation itself uses — the Search tab "
            "shows which vocabulary the estate is written in.")
        return section, ev

    best = hits[0]
    doc = _doc_meta(ctx, best.path)
    top = (best.heading or "").split(" > ")[0].strip()

    # Pull the whole curated section, not only the paragraph that matched —
    # a flow made of the two paragraphs nearest a query is not a flow.
    rows = []
    if top:
        rows = ctx.store.query(
            "SELECT * FROM chunk WHERE source='wiki' AND path = ?"
            "   AND (heading = ? OR heading LIKE ?)"
            " ORDER BY start_line LIMIT ?",
            (best.path, top, f"{top} > %", max_steps))
    if len(rows) < 2:
        seen, rows = set(), []
        for h in sorted(hits, key=lambda h: (h.path, h.start_line)):
            if h.chunk_id in seen:
                continue
            seen.add(h.chunk_id)
            rows.extend(ctx.store.query(
                "SELECT * FROM chunk WHERE id = ?", (h.chunk_id,)))
        rows = rows[:max_steps]

    section.lead = (
        f"From the curated documentation “{doc['title']}”"
        + (f", last updated by {doc['last_author']} on "
           f"{(doc['last_commit_date'] or '')[:10]}"
           if doc["last_author"] else "")
        + (f" — section “{top}”." if top else "."))
    for i, r in enumerate(rows, 1):
        heading = r["heading"] or doc["title"]
        title = heading[len(top):].lstrip(" >") if top and heading.startswith(top) else heading
        section.steps.append({
            "n": i, "title": title or heading, "origin": "curated",
            "text": trim(_strip_heading(r["text"], heading)),
            "meta": [f"{r['path']}:{r['start_line']}-{r['end_line']}"],
        })
        ev.append(evidence(r, origin="curated", confidence=1.0))

    others = sorted({h.path for h in hits} - {best.path})
    if others:
        section.notes.append(
            "Other curated pages also mention this: " + ", ".join(
                _doc_meta(ctx, p)["title"] for p in others[:4]) + ".")
    section.notes.append(
        "This is the documentation's own wording and its own order. It is "
        "human-written business knowledge, not behaviour derived from code — "
        "where the two disagree, the code section below is what runs.")
    return section, ev


# ---------------------------------------------------------------- technical

LAYERS = [
    ("entry", "Entry point — where this flow is entered"),
    ("trigger", "Triggers — the user actions and events wired to it"),
    ("logic", "Application logic — the methods that do the work"),
    ("shared", "Shared and cross-application services"),
    ("data", "Data layer — entities and tables reached"),
]


def _owner(ctx: Context, node_ids: Iterable[str]) -> dict[str, Any]:
    ids = list(node_ids)
    if not ids:
        return {}
    out: dict[str, Any] = {}
    for i in range(0, len(ids), 400):
        block = ids[i:i + 400]
        placeholders = ",".join("?" * len(block))
        for r in ctx.store.query(
                f"SELECT e.src_id AS child, n.kind AS kind, n.fqn AS fqn,"
                f"       n.name AS name, n.asset_id AS asset_id"
                f"  FROM edge e JOIN node n ON n.id = e.dst_id"
                f" WHERE e.kind='belongs_to' AND e.src_id IN ({placeholders})",
                tuple(block)):
            out[r["child"]] = {"kind": r["kind"], "fqn": r["fqn"],
                               "name": r["name"], "asset": r["asset_id"]}
    return out


def walk_downstream(ctx: Context, row: Any, depth: int = WALK_DEPTH) -> list[Any]:
    """Everything reachable from this element by `invokes`, with hop distance.

    A type is seeded by its callable members, because callers invoke methods
    rather than classes — but its fields are excluded, since a field is a
    thing the element HAS, not a step it performs.
    """
    return ctx.store.query("""
        WITH RECURSIVE seed(id) AS (
          SELECT ?
          UNION
          SELECT e.src_id FROM edge e JOIN node m ON m.id = e.src_id
          WHERE e.kind = 'belongs_to' AND e.dst_id = ?
            AND m.kind IN ('method', 'event_handler')
        ),
        down(id, hop) AS (
          SELECT id, 0 FROM seed
          UNION
          SELECT e.dst_id, down.hop + 1 FROM edge e JOIN down ON e.src_id = down.id
          WHERE e.kind = 'invokes' AND down.hop < ?
        )
        SELECT n.id, n.asset_id, n.kind, n.name, n.fqn, n.signature, n.path,
               n.start_line, n.end_line, n.attrs, MIN(down.hop) AS hop
        FROM down JOIN node n ON n.id = down.id
        WHERE n.kind <> 'field'
        GROUP BY n.id
        ORDER BY hop, n.asset_id, n.fqn
        LIMIT 400""", (row["id"], row["id"], max(1, min(int(depth), 4))))


def execution_flow(ctx: Context, row: Any, how: str,
                   depth: int = WALK_DEPTH) -> tuple[Section, list[Evidence], dict]:
    """The code path, arranged as layers a developer would draw on a whiteboard."""
    names = asset_names(ctx)
    ev: list[Evidence] = []
    section = Section(key="code_flow", audience="technical",
                      title="Source-code execution flow — how the code runs")

    if row is None:
        section.empty = (
            "No element in the registered estate could be resolved for this "
            "input, so there is no execution path to show.")
        return section, ev, {}

    reached = walk_downstream(ctx, row, depth)
    owners = _owner(ctx, [r["id"] for r in reached])
    subj_attrs = attrs_of(row)

    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in LAYERS}

    # --- entry ------------------------------------------------------------
    entry_meta = [f"{names.get(row['asset_id'], row['asset_id'])} "
                  f"({row['asset_id']}) · {row['kind']}"]
    if subj_attrs.get("route"):
        entry_meta.append(f"screen route /{subj_attrs['route']}")
    if subj_attrs.get("descriptor"):
        entry_meta.append(f"layout {subj_attrs['descriptor']}")
    if subj_attrs.get("annotations"):
        entry_meta.append("annotated " + " ".join(
            "@" + a for a in subj_attrs["annotations"][:6]))
    if subj_attrs.get("transactional"):
        entry_meta.append("runs inside a database transaction")
    buckets["entry"].append({
        "title": short_fqn(row["fqn"]), "text": one_line(row["signature"]) or row["fqn"],
        "meta": entry_meta, "ref": ref(row), "origin": "derived"})
    ev.append(evidence(row))

    # --- the rest ---------------------------------------------------------
    for r in reached:
        if r["id"] == row["id"]:
            continue
        a = attrs_of(r)
        owner = owners.get(r["id"], {})
        meta = [f"{r['asset_id']} · depth {r['hop']}"]
        if r["hop"] == 0:
            meta = [f"{r['asset_id']} · declared on the entry point"]

        if r["kind"] == "event_handler":
            layer = "trigger"
            if a.get("subscribes_to"):
                meta.append(f"fires on “{a['subscribes_to']}”")
        elif r["kind"] in ("entity", "store"):
            layer = "data"
            if a.get("table") or a.get("physical_table"):
                meta.append(f"table {a.get('table') or a.get('physical_table')}")
        elif r["asset_id"] != row["asset_id"]:
            layer = "shared"
            meta.append(f"in {names.get(r['asset_id'], r['asset_id'])}")
        elif r["hop"] == 0 or owner.get("fqn") == row["fqn"]:
            # Declared on the entry point itself — this IS the work, not a
            # delegation to somewhere else.
            layer = "logic"
        elif owner.get("kind") == "service" or r["kind"] == "service":
            layer = "shared"
        elif is_accessor(r) and owner.get("kind") in ("entity", "record"):
            continue                      # a field read, not a step
        else:
            layer = "logic"

        if a.get("transactional"):
            meta.append("transactional")
        if a.get("throws"):
            meta.append("throws " + ", ".join(a["throws"][:4]))
        if owner.get("fqn") and r["kind"] in ("method", "event_handler"):
            meta.append(f"on {short_fqn(owner['fqn'])}")

        buckets[layer].append({
            "title": short_fqn(r["fqn"]), "text": one_line(r["signature"]),
            "meta": meta, "ref": ref(r), "origin": "derived",
            "_hop": r["hop"], "_kind": r["kind"]})

    for key in buckets:
        buckets[key].sort(key=lambda s: (s.get("_hop", 0), s["title"]))

    n = 0
    for key, title in LAYERS:
        items = buckets[key]
        if not items:
            continue
        shown = items[:MAX_PER_LAYER]
        n += 1
        section.steps.append({
            "n": n, "title": title, "layer": key, "origin": "derived",
            "text": "", "items": [
                {k: v for k, v in it.items() if not k.startswith("_")}
                for it in shown],
            "meta": ([f"{len(items)} found, {len(shown)} shown"]
                     if len(items) > len(shown) else
                     [f"{len(items)} found"]),
        })
        for it in shown[:8]:
            rows = ctx.store.query(
                f"SELECT {NODE_COLS} FROM node WHERE asset_id = ? AND path = ?"
                f" AND start_line = ? LIMIT 1",
                (it["ref"]["asset"], it["ref"]["path"], it["ref"]["start_line"]))
            if rows:
                ev.append(evidence(rows[0]))

    section.lead = (
        f"Starting at {short_fqn(row['fqn'])} in "
        f"{names.get(row['asset_id'], row['asset_id'])}, following call "
        f"references up to {depth} hops. "
        + ("Resolved by exact name." if how in ("fqn", "simple_name") else
           "Resolved by screen route." if how == "route" else
           "Resolved by table name." if how == "table" else
           "Matched because its name carries the words you asked about — "
           "confirm it is the element you meant." if how == "name_tokens" else
           "Matched by meaning, not by name — confirm this is the element you "
           "meant before relying on the path." if how == "retrieval" else
           "Several elements share this name; the first is shown."))
    section.notes.append(
        "Layers group calls by role. Depth is how many call hops away a thing "
        "is — it is not execution sequence: branches, loops and conditions are "
        "not modelled, and calls whose receiver the extractor could not resolve "
        "are absent (see Coverage).")
    if not any(buckets[k] for k in ("logic", "shared", "data")):
        section.notes.append(
            "No outbound calls are derived from this element. It may be a leaf, "
            "or its calls may be among the unresolved references.")

    stats = {"reached": len(reached),
             "applications": sorted({r["asset_id"] for r in reached}),
             "layers": {k: len(v) for k, v in buckets.items()}}
    return section, ev, stats


# ---------------------------------------------------------------- fragments

def rules_section(ctx: Context, row: Any) -> tuple[Section, list[Evidence]]:
    """Validation-shaped members, declared throw sites, and security roles."""
    section = Section(key="rules", audience="technical",
                      title="Rules and validations enforced in code")
    ev: list[Evidence] = []
    if row is None:
        section.empty = "No element resolved, so no enforced rule can be shown."
        return section, ev

    checks = ctx.store.query(f"""
        SELECT {NODE_COLS_N} FROM node n
        JOIN edge e ON e.src_id = n.id AND e.kind='belongs_to'
        WHERE e.dst_id = ? AND n.kind IN ('method','event_handler')
          AND (n.name LIKE 'validate%' OR n.name LIKE 'check%'
               OR n.name LIKE 'can%' OR n.name LIKE 'assert%'
               OR n.name LIKE 'ensure%' OR n.name LIKE 'require%'
               OR n.name LIKE 'verify%' OR n.name LIKE '%Valid%')
        ORDER BY n.start_line LIMIT 40""", (row["id"],))

    reached = walk_downstream(ctx, row, 2)
    throwers = [r for r in reached if attrs_of(r).get("throws")]

    n = 0
    for r in checks:
        n += 1
        a = attrs_of(r)
        meta = [f"{r['path']}:{r['start_line']}"]
        if a.get("throws"):
            meta.append("rejects by throwing " + ", ".join(a["throws"][:3]))
        section.steps.append({
            "n": n, "title": short_fqn(r["fqn"]), "origin": "derived",
            "text": one_line(r["signature"]), "meta": meta, "ref": ref(r)})
        ev.append(evidence(r, excerpt=""))

    for r in throwers[:12]:
        if any(s["title"] == short_fqn(r["fqn"]) for s in section.steps):
            continue
        n += 1
        section.steps.append({
            "n": n, "title": short_fqn(r["fqn"]), "origin": "derived",
            "text": one_line(r["signature"]),
            "meta": [f"refuses by throwing "
                     f"{', '.join(attrs_of(r)['throws'][:3])}",
                     f"{r['path']}:{r['start_line']}"],
            "ref": ref(r)})
        ev.append(evidence(r))

    roles = ctx.store.query(
        f"SELECT {NODE_COLS} FROM node WHERE kind='security_role'"
        f" AND asset_id = ? ORDER BY name LIMIT 10", (row["asset_id"],))
    if roles:
        section.notes.append(
            f"{len(roles)} security role(s) are declared in "
            f"{row['asset_id']} and may restrict this declaratively: "
            + ", ".join(r["name"] for r in roles[:6]) + ".")

    if not section.steps:
        section.empty = (
            "No named validation method and no declared throw site was found "
            "on this element.")
    section.notes.append(
        "Checks are identified by naming convention (validate/check/can/"
        "assert/ensure/require/verify) and by where the code throws. Inline "
        "`if` conditions and Jmix declarative constraints are not derived, so "
        "this is a floor — absence here is not proof that no rule exists.")
    return section, ev


def data_section(ctx: Context, row: Any) -> tuple[Section, list[Evidence]]:
    section = Section(key="data", audience="technical",
                      title="Data touched — entities, tables and fields")
    ev: list[Evidence] = []
    if row is None:
        section.empty = "No element resolved, so no data footprint can be shown."
        return section, ev

    reached = walk_downstream(ctx, row, WALK_DEPTH)
    stores = [r for r in reached if r["kind"] in ("entity", "store")]
    direct = ctx.store.query(f"""
        SELECT DISTINCT {NODE_COLS_N} FROM edge e JOIN node n ON n.id = e.dst_id
        WHERE e.src_id = ? AND n.kind IN ('entity','store')
        ORDER BY n.kind, n.fqn LIMIT 40""", (row["id"],))
    seen = {r["fqn"] for r in direct}
    merged = list(direct) + [r for r in stores if r["fqn"] not in seen]

    for i, r in enumerate(merged[:24], 1):
        a = attrs_of(r)
        fields = ctx.store.query(
            "SELECT n.name, json_extract(n.attrs,'$.type') t FROM node n"
            " JOIN edge e ON e.src_id = n.id AND e.kind='belongs_to'"
            " WHERE e.dst_id = ? AND n.kind='field' ORDER BY n.start_line LIMIT 14",
            (r["id"],))
        meta = [f"{r['asset_id']} · {r['kind']}"]
        if a.get("table") or a.get("physical_table"):
            meta.append(f"table {a.get('table') or a.get('physical_table')}")
        if fields:
            meta.append("fields: " + ", ".join(
                f"{f['name']}" + (f" ({(f['t'] or '').rsplit('.', 1)[-1]})"
                                  if f["t"] else "") for f in fields))
        section.steps.append({
            "n": i, "title": short_fqn(r["fqn"]), "origin": "derived",
            "text": "", "meta": meta, "ref": ref(r)})
        ev.append(evidence(r))

    if not section.steps:
        section.empty = (
            "No entity or table is reached from this element by derived calls. "
            "Jmix persists through DataManager, whose receiver the extractor "
            "often cannot resolve, so data access may exist without appearing "
            "here.")
    else:
        section.notes.append(
            "Reads and writes are NOT distinguished: Jmix persistence runs "
            "through DataManager, so direction is not derivable from structure "
            "alone. Treat every entry as “touched”.")
    return section, ev


def callers_section(ctx: Context, row: Any, depth: int = 2
                    ) -> tuple[Section, list[Evidence], dict]:
    section = Section(key="callers", audience="technical",
                      title="What depends on this — blast radius")
    ev: list[Evidence] = []
    if row is None:
        section.empty = "No element resolved, so no dependants can be listed."
        return section, ev, {}

    rows = ctx.store.query("""
        WITH RECURSIVE seed(id) AS (
          SELECT ?
          UNION
          SELECT e.src_id FROM edge e WHERE e.kind='belongs_to' AND e.dst_id = ?
        ),
        up(id, hop) AS (
          SELECT id, 0 FROM seed
          UNION
          SELECT e.src_id, up.hop + 1 FROM edge e JOIN up ON e.dst_id = up.id
          WHERE e.kind='invokes' AND up.hop < ?
        )
        SELECT n.id, n.asset_id, n.kind, n.name, n.fqn, n.signature, n.path,
               n.start_line, n.end_line, n.attrs, MIN(up.hop) AS hop
        FROM up JOIN node n ON n.id = up.id
        WHERE up.hop > 0 AND n.id NOT IN (SELECT id FROM seed) AND n.kind <> 'field'
        GROUP BY n.id ORDER BY hop, n.asset_id, n.fqn LIMIT 120""",
        (row["id"], row["id"], max(1, min(int(depth), 4))))

    names = asset_names(ctx)
    cross = sorted({r["asset_id"] for r in rows} - {row["asset_id"]})
    owners = _owner(ctx, [r["id"] for r in rows])

    direct = [r for r in rows if r["hop"] == 1]
    indirect = [r for r in rows if r["hop"] > 1]
    for label, group in (("Directly calls this", direct),
                         ("Reaches it indirectly", indirect)):
        if not group:
            continue
        shown = group[:MAX_PER_LAYER]
        section.steps.append({
            "n": len(section.steps) + 1, "title": label, "origin": "derived",
            "text": "",
            "meta": [f"{len(group)} found" + (f", {len(shown)} shown"
                                              if len(group) > len(shown) else "")],
            "items": [{
                "title": short_fqn(r["fqn"]),
                "text": one_line(r["signature"]),
                "meta": [f"{names.get(r['asset_id'], r['asset_id'])} "
                         f"({r['asset_id']})"]
                        + ([f"on {short_fqn(owners[r['id']]['fqn'])}"]
                           if owners.get(r["id"]) else []),
                "ref": ref(r), "origin": "derived"} for r in shown]})
        for r in shown[:8]:
            ev.append(evidence(r))

    if not rows:
        section.empty = (
            "Nothing outside this element calls it, as far as derived call "
            "references show.")
    if cross:
        section.notes.append(
            "Crosses application boundaries into: "
            + ", ".join(f"{names.get(a, a)} ({a})" for a in cross)
            + " — changes here are felt outside the owning application.")
    section.notes.append(
        "Call sites the extractor could not resolve are not counted, so this "
        "is a floor for the blast radius, not a ceiling.")
    return section, ev, {"affected": len(rows), "cross_application": cross}


def inventory_section(ctx: Context, row: Any) -> tuple[Section, list[Evidence], dict]:
    """Everything involved, grouped the way a developer would inventory it."""
    section = Section(key="inventory", audience="technical",
                      title="Code inventory — screens, services, methods, data")
    ev: list[Evidence] = []
    if row is None:
        section.empty = "No element resolved, so nothing can be inventoried."
        return section, ev, {}

    reached = walk_downstream(ctx, row, WALK_DEPTH)
    rows = [row] + [r for r in reached if r["id"] != row["id"]]
    names = asset_names(ctx)

    groups: list[tuple[str, str, list[Any]]] = [
        ("screens", "Screens and views (with their routes)",
         [r for r in rows if r["kind"] == "view"]),
        ("services", "Services and beans",
         [r for r in rows if r["kind"] == "service"]),
        ("handlers", "Event handlers",
         [r for r in rows if r["kind"] == "event_handler"]),
        ("types", "Classes, interfaces, records and enums",
         [r for r in rows if r["kind"] in ("class", "interface", "record", "enum")]),
        ("methods", "Methods carrying logic",
         [r for r in rows if r["kind"] == "method" and not is_accessor(r)]),
        ("entities", "Entities and tables",
         [r for r in rows if r["kind"] in ("entity", "store")]),
        ("roles", "Security roles in the owning application",
         list(ctx.store.query(
             f"SELECT {NODE_COLS} FROM node WHERE kind='security_role'"
             f" AND asset_id = ? ORDER BY name LIMIT 10", (row["asset_id"],)))),
    ]

    counts: dict[str, int] = {}
    for key, title, items in groups:
        counts[key] = len(items)
        if not items:
            continue
        shown = items[:MAX_PER_LAYER]
        section.steps.append({
            "n": len(section.steps) + 1, "title": title, "layer": key,
            "origin": "derived", "text": "",
            "meta": [f"{len(items)} found" + (f", {len(shown)} shown"
                                              if len(items) > len(shown) else "")],
            "items": [{
                "title": short_fqn(r["fqn"]),
                "text": one_line(r["signature"]),
                "meta": [f"{names.get(r['asset_id'], r['asset_id'])} "
                         f"({r['asset_id']})"]
                        + ([f"route /{attrs_of(r)['route']}"]
                           if attrs_of(r).get("route") else [])
                        + ([f"table {attrs_of(r).get('table') or attrs_of(r).get('physical_table')}"]
                           if attrs_of(r).get("table") or attrs_of(r).get("physical_table") else []),
                "ref": ref(r), "origin": "derived"} for r in shown]})
        for r in shown[:6]:
            ev.append(evidence(r))

    section.notes.append(
        "This estate exposes its API as Jmix screen routes and Spring service "
        "beans; there are no REST controller nodes in the graph, so “API” here "
        "means routes and service beans, not HTTP endpoints.")
    return section, ev, counts


def mapping_section(ctx: Context, term: str, row: Any
                    ) -> tuple[Section, list[Evidence]]:
    """Business statement ↔ code location, only where a human signed the link."""
    section = Section(key="mapping", audience="business",
                      title="Business ↔ code mapping (human-approved links)")
    ev: list[Evidence] = []

    anchors: list[Any] = []
    if row is not None:
        anchors = list(ctx.store.query(
            "SELECT * FROM anchor WHERE target_asset = ? AND target_fqn = ?"
            " AND state IN ('resolved','stale') ORDER BY confidence DESC",
            (row["asset_id"], row["fqn"])))

    stage_rows: list[Any] = []
    proc, stages = curated_process(ctx, term)
    if proc is not None:
        for s in stages:
            for a in ctx.store.query(
                    "SELECT * FROM process_stage_anchor WHERE stage_id = ?",
                    (s["id"],)):
                stage_rows.append((s, a))

    n = 0
    for s, a in stage_rows:
        n += 1
        section.steps.append({
            "n": n, "title": f"Stage {s['ordinal']} — {s['name']}",
            "origin": "curated", "text": trim(s["description"]),
            "meta": [f"implemented in {short_fqn(a['target_fqn'])} "
                     f"({a['target_asset']})",
                     f"{a['target_path']}:{a['target_start_line']}"
                     if a["target_path"] else "link is broken — fqn not in graph"],
            "ref": ({"asset": a["target_asset"], "path": a["target_path"],
                     "start_line": a["target_start_line"] or 1,
                     "end_line": a["target_end_line"] or 1}
                    if a["target_path"] else None)})

    for a in anchors:
        n += 1
        section.steps.append({
            "n": n, "title": "Approved business statement",
            "origin": "curated", "text": trim(a["statement"]),
            "meta": [f"approved by {a['reviewed_by']} on "
                     f"{(a['reviewed_at'] or '')[:10]}",
                     f"applies to {short_fqn(a['target_fqn'])}",
                     f"source {a['doc_path']}:{a['doc_start_line']}"]
                    + (["STALE — the code changed after this was approved"]
                       if a["state"] == "stale" else []),
            "ref": {"asset": a["target_asset"], "path": a["target_path"],
                    "start_line": a["target_start_line"] or 1,
                    "end_line": a["target_end_line"] or 1}
                   if a["target_path"] else None})
        ev.append(Evidence(
            asset_id="WIKI", path=a["doc_path"],
            start_line=a["doc_start_line"], end_line=a["doc_end_line"],
            kind="statement", fqn=a["target_fqn"], origin="curated",
            confidence=a["confidence"]))

    if not section.steps:
        total = ctx.store.scalar(
            "SELECT COUNT(*) FROM anchor WHERE state='resolved'") or 0
        section.empty = (
            "No human has yet approved a link between a business statement and "
            "this code. The platform will not invent one.")
        section.notes.append(
            f"Only {total} approved anchor(s) exist across the whole estate. "
            f"Use the Review tab to approve candidates — every approved link "
            f"makes this answer stronger for everyone.")
        section.notes.append(
            "The business and code sections of this answer are each sound on "
            "their own; what is missing is the signed statement that this "
            "specific code implements that specific business rule.")
    return section, ev


def failure_section(ctx: Context, term: str) -> tuple[Section, list[Evidence], dict]:
    """Where a named error comes from — throw sites first, then text matches."""
    section = Section(key="failure", audience="technical",
                      title="Failure origin — where this error is raised")
    ev: list[Evidence] = []
    names = asset_names(ctx)

    bare = term.strip().split()[-1] if term.strip() else term
    bare = bare.strip(".:;'\"()")

    thrown = ctx.store.query(
        f"SELECT {NODE_COLS} FROM node"
        f" WHERE attrs LIKE ? ORDER BY asset_id, fqn LIMIT 40",
        (f'%"throws"%{bare}%',))
    thrown = [r for r in thrown
              if any(bare.lower() in t.lower() for t in attrs_of(r).get("throws", []))]

    types = ctx.store.query(
        f"SELECT {NODE_COLS} FROM node"
        f" WHERE (name LIKE '%Exception' OR name LIKE '%Error')"
        f"   AND (name LIKE ? OR fqn LIKE ?)"
        f" ORDER BY name LIMIT 20", (f"%{bare}%", f"%{bare}%"))

    # Text evidence: the actual `throw new ...` lines captured at index time.
    text_hits = ctx.store.query(
        "SELECT c.* FROM chunk c WHERE c.source='code' AND c.text LIKE ?"
        " ORDER BY c.asset_id, c.path LIMIT 20", (f"%{bare}%",))
    text_hits = [r for r in text_hits
                 if "throw" in r["text"] or "catch" in r["text"]][:12]

    n = 0
    for r in types:
        n += 1
        section.steps.append({
            "n": n, "title": f"Exception type — {short_fqn(r['fqn'])}",
            "origin": "derived", "text": one_line(r["signature"]),
            "meta": [f"{names.get(r['asset_id'], r['asset_id'])} ({r['asset_id']})",
                     f"{r['path']}:{r['start_line']}"], "ref": ref(r)})
        ev.append(evidence(r))

    for r in thrown[:MAX_PER_LAYER]:
        n += 1
        section.steps.append({
            "n": n, "title": f"Raised in {short_fqn(r['fqn'])}",
            "origin": "derived", "text": one_line(r["signature"]),
            "meta": ["throws " + ", ".join(attrs_of(r).get("throws", [])[:4]),
                     f"{names.get(r['asset_id'], r['asset_id'])} ({r['asset_id']})",
                     f"{r['path']}:{r['start_line']}"], "ref": ref(r)})
        ev.append(evidence(r))

    for r in text_hits:
        n += 1
        section.steps.append({
            "n": n, "title": f"Referenced in {short_fqn(r['heading'] or r['path'])}",
            "origin": "derived", "text": trim(r["text"], 700),
            "meta": [f"{r['asset_id']} · captured source",
                     f"{r['path']}:{r['start_line']}-{r['end_line']}"],
            "ref": ref(r)})
        ev.append(evidence(r, excerpt=trim(r["text"], 700)))

    if not section.steps:
        section.empty = (
            f"No exception type, declared throw site, or captured source line "
            f"in the estate mentions “{bare}”.")
        section.notes.append(
            "Give the exception class name exactly as it appears in the stack "
            "trace, or paste the literal message text.")
    if not thrown:
        section.notes.append(
            "No declared throw site was found. Derived throw sites come from "
            "`throw new X(...)` statements; a rethrow, a framework-raised "
            "exception, or one thrown through a variable is not derivable.")
    section.notes.append(
        "This locates where a failure ORIGINATES. The runtime conditions that "
        "made it fire are not modelled — the platform cannot tell you why it "
        "fired on a particular record.")
    return section, ev, {"throw_sites": len(thrown), "types": len(types)}
