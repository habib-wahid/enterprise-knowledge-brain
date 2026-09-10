"""BR-13 / BR-69 / BR-72 — report what was interpreted, and what was not.

The point of this module is that the platform states its own limits. A
coverage number nobody can see is the same as no coverage number.
"""
from __future__ import annotations

from pathlib import Path

from ..store.db import KnowledgeStore


def _bar(pct: float, width: int = 24) -> str:
    filled = round(pct / 100 * width)
    return "#" * filled + "." * (width - filled)


def report(db_path: Path) -> str:
    s = KnowledgeStore(db_path)
    out: list[str] = []
    w = out.append

    run = s.query("SELECT * FROM refresh_run ORDER BY started_at DESC LIMIT 1")[0]
    w(f"ECK coverage report")
    w(f"estate      {run['estate_id']}")
    w(f"run         {run['id']}  {run['status']}  started {run['started_at']}")
    w(f"register    sha256:{run['register_sha'][:16]}")
    w("")

    w("ASSETS IN SCOPE (BR-05)")
    w(f"  {'id':<8} {'name':<28} {'commit':<10} owner")
    for a in s.query("SELECT * FROM asset ORDER BY id"):
        commit = (a["source_commit"] or "-")[:8]
        owner = a["owner"]
        flag = "  <-- BR-73 gap" if owner == "UNASSIGNED" else ""
        w(f"  {a['id']:<8} {a['name']:<28} {commit:<10} {owner}{flag}")
    w("")

    w("STRUCTURE DERIVED (CAP-2)")
    for r in s.query("SELECT kind, COUNT(*) n FROM node GROUP BY kind ORDER BY n DESC"):
        w(f"  {r['kind']:<18} {r['n']:>7,}")
    total_nodes = s.scalar("SELECT COUNT(*) FROM node")
    w(f"  {'TOTAL':<18} {total_nodes:>7,}")
    w("")

    w("RELATIONSHIPS (BR-08)")
    for r in s.query("SELECT kind, COUNT(*) n FROM edge GROUP BY kind ORDER BY n DESC"):
        w(f"  {r['kind']:<18} {r['n']:>7,}")
    total_edges = s.scalar("SELECT COUNT(*) FROM edge")
    w(f"  {'TOTAL':<18} {total_edges:>7,}")
    w("")

    w("FACTS vs INFERENCE (BR-10, BR-20)")
    for r in s.query("SELECT origin, COUNT(*) n FROM edge GROUP BY origin ORDER BY n DESC"):
        pct = 100.0 * r["n"] / total_edges if total_edges else 0.0
        w(f"  {r['origin']:<18} {r['n']:>7,}  {pct:5.1f}%  {_bar(pct)}")
    w("")

    w("WHAT COULD NOT BE INTERPRETED (BR-13)")
    # Only call sites are counted here. belongs_to and declares edges are
    # structural and never pass through resolution, so folding them in would
    # flatter the number — precisely the dishonesty this report exists to stop.
    resolved_calls = s.scalar(
        "SELECT COUNT(*) FROM edge WHERE kind='invokes'") or 0
    unresolved = s.scalar("SELECT COUNT(*) FROM unresolved_ref") or 0
    attempted = resolved_calls + unresolved
    rate = 100.0 * resolved_calls / attempted if attempted else 0.0
    w(f"  call sites attempted   {attempted:>7,}")
    w(f"  resolved               {resolved_calls:>7,}   {rate:5.1f}%  {_bar(rate)}")
    w(f"  unresolved             {unresolved:>7,}")
    for r in s.query("SELECT reason, COUNT(*) n FROM unresolved_ref"
                     " GROUP BY reason ORDER BY n DESC"):
        w(f"    {r['reason']:<26} {r['n']:>7,}")
    w("")

    failures = s.query("SELECT reason, COUNT(*) n FROM parse_failure"
                       " GROUP BY reason ORDER BY n DESC")
    w("PARSE FAILURES")
    if failures:
        for r in failures:
            w(f"  {r['reason']:<26} {r['n']:>7,}")
        w("  (paths listed in the parse_failure table)")
    else:
        w("  none")
    w("")

    w("RETRIEVAL INDEX (CAP-5)")
    docs = s.scalar("SELECT COUNT(*) FROM doc") or 0
    chunks = s.scalar("SELECT COUNT(*) FROM chunk") or 0
    vectors = s.scalar("SELECT COUNT(*) FROM chunk_vec") or 0
    w(f"  wiki pages ingested    {docs:>7,}")
    for r in s.query("SELECT source, origin, COUNT(*) n FROM chunk"
                     " GROUP BY source, origin ORDER BY n DESC"):
        w(f"    {r['source'] + ' (' + r['origin'] + ')':<26} {r['n']:>7,}")
    w(f"  chunks total           {chunks:>7,}")
    embedded = 100.0 * vectors / chunks if chunks else 0.0
    w(f"  embedded               {vectors:>7,}   {embedded:5.1f}%  {_bar(embedded)}")
    if vectors < chunks:
        w("  WARNING: unembedded chunks are keyword-only (BR-33 partially unmet)")
    w("")

    w("CURATED MEANING (CAP-3)")
    total_anchors = s.scalar("SELECT COUNT(*) FROM anchor") or 0
    if total_anchors:
        for r in s.query("SELECT state, COUNT(*) n FROM anchor"
                         " GROUP BY state ORDER BY n DESC"):
            w(f"  {r['state']:<18} {r['n']:>7,}")
        anchored = s.scalar(
            "SELECT COUNT(DISTINCT chunk_id) FROM anchor WHERE state='resolved'") or 0
        wiki_chunks = s.scalar(
            "SELECT COUNT(*) FROM chunk WHERE source='wiki'") or 1
        pct = 100.0 * anchored / wiki_chunks
        w(f"  wiki statements anchored {anchored:>5,} of {wiki_chunks:,}"
          f"  {pct:5.1f}%  {_bar(pct)}")
    else:
        w("  none — no business statement is bound to code yet, so BR-16")
        w("  is unmet. Propose with `eck anchors propose`, review with")
        w("  `eck anchors review`.")
    w("")

    w("PROCESS & BEHAVIOUR (CAP-4)")
    proc_count = s.scalar("SELECT COUNT(*) FROM process") or 0
    if proc_count:
        for r in s.query("SELECT id, name FROM process ORDER BY id"):
            n_stages = s.scalar(
                "SELECT COUNT(*) FROM process_stage WHERE process_id=?",
                (r["id"],)) or 0
            n_broken = s.scalar("""SELECT COUNT(*) FROM process_stage_anchor a
                JOIN process_stage st ON st.id = a.stage_id
                WHERE st.process_id=? AND a.state='broken'""", (r["id"],)) or 0
            flag = f"  <-- {n_broken} broken anchor(s)" if n_broken else ""
            w(f"  {r['id']:<24} {n_stages} stages{flag}")
    else:
        w("  none curated yet")
    w("")

    w("REFERENCE DATA (CAP-6)")
    refdata_count = s.scalar("SELECT COUNT(*) FROM refdata_source") or 0
    if refdata_count:
        values = s.scalar("SELECT COUNT(*) FROM refdata_value") or 0
        excluded = s.scalar("SELECT COUNT(*) FROM refdata_excluded") or 0
        w(f"  allow-listed items   {refdata_count:>4}")
        w(f"  values captured      {values:>4}")
        w(f"  explicitly excluded  {excluded:>4}  (BR-42 — the judgement, not just the result)")
        oldest = s.scalar("SELECT MIN(snapshot_at) FROM refdata_value")
        if oldest:
            w(f"  oldest snapshot      {oldest[:10]}")
    else:
        w("  none allow-listed yet")
    w("")

    w("KNOWN GAPS (BR-72)")
    # CAP-3 proper is the anchor layer, not the wiki text. Ingesting the wiki
    # gives retrievable prose; it does NOT give a business statement tied to a
    # software location, which is what BR-16 actually requires.
    unanchored = (s.scalar("SELECT COUNT(*) FROM chunk WHERE source='wiki'") or 0) \
        - (s.scalar("SELECT COUNT(DISTINCT chunk_id) FROM anchor"
                    " WHERE state='resolved'") or 0)
    if unanchored > 0:
        w(f"  - {unanchored:,} wiki statements have no approved anchor: they are")
        w("    retrievable but not bound to code, so BR-16 is only partly met")
    w("  - XML view descriptors (954 files) not parsed: UI structure is partial")
    w("  - Liquibase changelogs are read only for allow-listed reference "
      "tables (CAP-6);")
    w("    schema history in general is not derived")
    w("  - chained call receivers unresolved: needs return-type tracking")
    if proc_count <= 1:
        w(f"  - only {proc_count} process(es) curated: most end-to-end "
          f"processes have no CAP-4 definition")
    unowned = s.scalar("SELECT COUNT(*) FROM asset WHERE owner='UNASSIGNED'")
    if unowned:
        w(f"  - {unowned} assets have no named owner (BR-73)")

    s.close()
    return "\n".join(out)


def data(db_path: Path) -> dict:
    """The same facts as report(), as data — for the web UI and any API caller.

    report() stays the authority on wording; this stays the authority on
    numbers. Both read the same tables so they cannot disagree.
    """
    s = KnowledgeStore(db_path)
    run = s.query("SELECT * FROM refresh_run ORDER BY started_at DESC LIMIT 1")[0]

    total_edges = s.scalar("SELECT COUNT(*) FROM edge") or 0
    resolved_calls = s.scalar("SELECT COUNT(*) FROM edge WHERE kind='invokes'") or 0
    unresolved = s.scalar("SELECT COUNT(*) FROM unresolved_ref") or 0
    attempted = resolved_calls + unresolved
    chunks = s.scalar("SELECT COUNT(*) FROM chunk") or 0
    vectors = s.scalar("SELECT COUNT(*) FROM chunk_vec") or 0
    wiki_chunks = s.scalar("SELECT COUNT(*) FROM chunk WHERE source='wiki'") or 0
    anchored = s.scalar("SELECT COUNT(DISTINCT chunk_id) FROM anchor"
                        " WHERE state='resolved'") or 0

    out = {
        "run": {"id": run["id"], "status": run["status"],
                "started_at": run["started_at"], "estate": run["estate_id"],
                "register_sha": run["register_sha"]},
        "assets": [dict(r) for r in s.query(
            "SELECT id, name, role, owner, tech, source_kind, source_commit"
            " FROM asset ORDER BY id")],
        "nodes_by_kind": {r["kind"]: r["n"] for r in s.query(
            "SELECT kind, COUNT(*) n FROM node GROUP BY kind ORDER BY n DESC")},
        "edges_by_kind": {r["kind"]: r["n"] for r in s.query(
            "SELECT kind, COUNT(*) n FROM edge GROUP BY kind ORDER BY n DESC")},
        "edges_by_origin": {r["origin"]: r["n"] for r in s.query(
            "SELECT origin, COUNT(*) n FROM edge GROUP BY origin")},
        "calls": {
            "attempted": attempted, "resolved": resolved_calls,
            "unresolved": unresolved,
            "resolution_pct": round(100.0 * resolved_calls / attempted, 1)
                              if attempted else 0.0,
            "unresolved_by_reason": {r["reason"]: r["n"] for r in s.query(
                "SELECT reason, COUNT(*) n FROM unresolved_ref"
                " GROUP BY reason ORDER BY n DESC")}},
        "parse_failures": {r["reason"]: r["n"] for r in s.query(
            "SELECT reason, COUNT(*) n FROM parse_failure GROUP BY reason")},
        "index": {
            "docs": s.scalar("SELECT COUNT(*) FROM doc") or 0,
            "chunks": chunks, "vectors": vectors,
            "wiki_chunks": wiki_chunks,
            "code_chunks": chunks - wiki_chunks,
            "embedded_pct": round(100.0 * vectors / chunks, 1) if chunks else 0.0},
        "anchors": {
            "by_state": {r["state"]: r["n"] for r in s.query(
                "SELECT state, COUNT(*) n FROM anchor GROUP BY state")},
            "anchored_statements": anchored,
            "wiki_statements": wiki_chunks,
            "anchored_pct": round(100.0 * anchored / wiki_chunks, 1)
                            if wiki_chunks else 0.0},
        "totals": {"nodes": s.scalar("SELECT COUNT(*) FROM node") or 0,
                   "edges": total_edges},
        "processes": [{
            "id": r["id"], "name": r["name"],
            "stages": s.scalar("SELECT COUNT(*) FROM process_stage"
                              " WHERE process_id=?", (r["id"],)) or 0,
            "broken_anchors": s.scalar("""SELECT COUNT(*) FROM
                process_stage_anchor a JOIN process_stage st
                ON st.id = a.stage_id WHERE st.process_id=? AND a.state='broken'""",
                (r["id"],)) or 0,
        } for r in s.query("SELECT id, name FROM process ORDER BY id")],
        "refdata": {
            "items": s.scalar("SELECT COUNT(*) FROM refdata_source") or 0,
            "values": s.scalar("SELECT COUNT(*) FROM refdata_value") or 0,
            "excluded": s.scalar("SELECT COUNT(*) FROM refdata_excluded") or 0,
            "oldest_snapshot": s.scalar("SELECT MIN(snapshot_at) FROM refdata_value"),
        },
        "gaps": [],
    }

    gaps = []
    if out["anchors"]["wiki_statements"] - anchored > 0:
        gaps.append(f"{out['anchors']['wiki_statements'] - anchored:,} wiki "
                    f"statements have no approved anchor — BR-16 only partly met")
    gaps.append("XML view descriptors (954 files) not parsed — UI structure partial")
    gaps.append("Liquibase changelogs are read only for allow-listed reference "
               "tables (CAP-6) — schema history in general is not derived")
    gaps.append("Chained call receivers unresolved — needs return-type tracking")
    if len(out["processes"]) <= 1:
        gaps.append(f"only {len(out['processes'])} process(es) curated — "
                    f"most end-to-end processes have no CAP-4 definition")
    unowned = s.scalar("SELECT COUNT(*) FROM asset WHERE owner='UNASSIGNED'") or 0
    if unowned:
        gaps.append(f"{unowned} assets have no named owner (BR-73)")
    out["gaps"] = gaps

    s.close()
    return out
