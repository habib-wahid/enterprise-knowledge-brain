"""Contract tests for M6 — CAP-4 Process & Behaviour, CAP-6 Reference Data.

Run:  ./.venv/bin/python tests/test_process_and_refdata.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eck import curated, process_register as preg, refdata_register as rreg  # noqa: E402
from eck import register                                                     # noqa: E402
from eck.curated import Anchor                                               # noqa: E402
from eck.govern import process_check, refresh                                # noqa: E402
from eck.services.registry import REGISTRY, load_all                         # noqa: E402
from eck.services.resolve import Context                                     # noqa: E402
from eck.services.result import UNKNOWN                                      # noqa: E402
from eck.store.db import KnowledgeStore                                      # noqa: E402

DB = ROOT / "build" / "knowledge.db"
passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


# --------------------------------------------------------------- BR-43 proof

def check_br43_no_write_route() -> None:
    """BR-43: reference data has no write path in the API. Checked by
    introspecting the actual FastAPI route table, not by convention."""
    load_all()
    from eck.api.http import create_app
    app = create_app(DB)
    write_verbs = {"POST", "PUT", "PATCH", "DELETE"}
    offenders = [
        (r.path, m) for r in app.routes for m in getattr(r, "methods", set()) or set()
        if "refdata" in r.path.lower() and m in write_verbs
    ]
    check("BR-43: no write route touches refdata", not offenders, str(offenders))

    # The curated store DOES have a write route (anchor review) — confirm
    # that one exists, so the refdata assertion above is a real absence and
    # not an artifact of routes being hard to find in general.
    anchor_writes = [
        (r.path, m) for r in app.routes for m in getattr(r, "methods", set()) or set()
        if "anchor" in r.path.lower() and m in write_verbs
    ]
    check("sanity: anchor review DOES have a write route (contrast case)",
          bool(anchor_writes), str(anchor_writes))


# --------------------------------------------------------------- BR-42 proof

def check_br42_no_excluded_value_captured() -> None:
    store = KnowledgeStore(DB)
    excluded = {r["key_or_table"] for r in
               store.query("SELECT key_or_table FROM refdata_excluded")}
    captured = {r["source_id"] for r in
               store.query("SELECT DISTINCT source_id FROM refdata_value")}
    store.close()
    check("BR-42: no excluded key/table was ever captured as a value",
          not (excluded & captured), str(excluded & captured))
    check("BR-42: at least one exclusion is recorded with a reason",
          bool(excluded))


# --------------------------------------------------------------- BR-45 proof

def check_br45_refdata_service() -> None:
    ctx = Context(DB)
    real_key = "inteacc.payroll.process-batch.application-ready.enabled"
    ok = REGISTRY["configuration.reference_data"].fn(ctx, key=real_key)
    check("BR-46: an allow-listed key answers directly", ok.outcome != UNKNOWN)
    check("BR-44: the answer carries a snapshot date",
          any("snapshot_at" in f for f in ok.findings))

    bogus = REGISTRY["configuration.reference_data"].fn(
        ctx, key="ui.login.defaultPassword")
    check("BR-45: an excluded key returns unknown, never a value",
          bogus.outcome == UNKNOWN and not bogus.evidence)

    nonsense = REGISTRY["configuration.reference_data"].fn(
        ctx, key="totally.made.up.key")
    check("BR-45: an unlisted key returns unknown, not a guess",
          nonsense.outcome == UNKNOWN and not nonsense.evidence)
    ctx.close()


# --------------------------------------------------------------- CAP-4 proof

def check_process_stages_service() -> None:
    ctx = Context(DB)
    r = REGISTRY["flow.process_stages"].fn(ctx, process="salary hold and release")
    ctx.close()

    check("BR-22: the curated process resolves", r.outcome != UNKNOWN)
    stage_findings = [f for f in r.findings if "stage" in f]
    check("BR-22: stages come back in ascending order",
          [f["stage"] for f in stage_findings] ==
          sorted(f["stage"] for f in stage_findings))
    check("BR-24: exactly one stage is marked as the entry point",
          sum(1 for f in stage_findings if f.get("is_entry")) == 1)
    check("BR-24: the entry stage names its trigger",
          any(f.get("is_entry") and f.get("entry_trigger")
              for f in stage_findings))
    check("BR-25: at least one stage reports a derived check",
          any(f.get("checks_found") for f in stage_findings))
    check("BR-29: at least one failure resolves to a real source location",
          any(fl["at"] for f in stage_findings for fl in f.get("failures", [])))
    check("every resolved stage anchor carries real evidence (BR-54)",
          all(e.path and e.start_line > 0 for e in r.evidence))
    check("stage narrative is attributed as curated (BR-20)",
          all(e.origin == "curated" for e in r.evidence
              if e.kind != "failure_path"))

    unknown_process = REGISTRY["flow.process_stages"].fn(
        ctx=Context(DB), process="totally made up business process xyz")
    check("an uncurated process name returns unknown, not empty success",
          unknown_process.outcome == UNKNOWN)
    unknown_process.evidence  # (already closed ctx is fine; no further query)


# --------------------------------------------------------- BR-18-vs-process

def check_broken_stage_anchor_does_not_abort() -> None:
    """The key design distinction from CAP-3: BR-18 aborts refresh on one
    broken anchor; a broken PROCESS stage anchor must not — see the
    schema.sql rationale on process_stage_anchor. Proven end to end on a
    synthetic estate, exactly as the CAP-3 lifecycle test proves the
    opposite for anchors."""
    tmp = Path(tempfile.mkdtemp(prefix="eck-process-test-"))
    curated.CANDIDATES = tmp / "candidates.jsonl"
    curated.ANCHORS = tmp / "anchors.jsonl"

    java_v1 = """package com.demo.pr;
public class HoldService {
    public boolean release(String id) { return id != null; }
}
"""
    java_v2 = java_v1.replace("release", "releaseHold")  # fqn now vanishes

    def write(java: str) -> Path:
        code = tmp / "src" / "demo"
        code.mkdir(parents=True, exist_ok=True)
        (code / "HoldService.java").write_text(java)
        wiki = tmp / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "Demo.md").write_text("# Demo\n\n## Hold\n\nSome text.\n")
        procs = tmp / "processes"
        procs.mkdir(parents=True, exist_ok=True)
        (procs / "demo.yaml").write_text("""
id: demo-process
name: "Demo process"
description: "test"
asset: DEMO
stages:
  - key: only-stage
    name: "Only stage"
    is_entry: true
    entry_trigger: "test"
    description: "test"
    anchors:
      - com.demo.pr.HoldService#release(String id)
""")
        reg = tmp / "estate.yaml"
        reg.write_text(f"""
version: 1
estate: {{id: TESTPROC, name: "Synthetic"}}
defaults: {{owner: test, tech: jmix, exclusions: []}}
sources:
  code: {{root: {tmp / 'src'}, kind: dir}}
  wiki: {{root: {tmp / 'wiki'}, kind: dir}}
assets:
  - {{id: DEMO, name: "Demo", role: "test", path: demo}}
  - {{id: WIKI, name: "Demo wiki", role: "test", path: ".", source: wiki, tech: markdown}}
""")
        return reg

    import eck.config as cfg
    orig_processes_dir = cfg.processes_dir
    orig_refdata_path = cfg.refdata_register_path
    cfg.processes_dir = lambda: tmp / "processes"
    cfg.refdata_register_path = lambda: tmp / "no-refdata.yaml"
    import eck.govern.refresh as refresh_mod
    refresh_mod.config.processes_dir = cfg.processes_dir
    refresh_mod.config.refdata_register_path = cfg.refdata_register_path

    try:
        reg_path = write(java_v1)
        reg = register.load(reg_path, tmp)
        db1 = refresh.build(reg, tmp / "build", verbose=False, embed=False)
        store = KnowledgeStore(db1)
        counts = process_check.summary(store.query(
            "SELECT state FROM process_stage_anchor"))
        store.close()
        check("stage anchor resolves against the initial source",
              counts.get("resolved") == 1 and not counts.get("broken"))

        # Rename the method: the fqn the process points at now vanishes.
        write(java_v2)
        db2 = refresh.build(reg, tmp / "build", verbose=False, embed=False)
        check("refresh with a broken stage anchor still PUBLISHES",
              db2.exists())
        store = KnowledgeStore(db2)
        state = store.scalar(
            "SELECT state FROM process_stage_anchor LIMIT 1")
        run_status = store.scalar(
            "SELECT status FROM refresh_run ORDER BY started_at DESC LIMIT 1")
        store.close()
        check("the broken anchor is recorded as broken, not silently dropped",
              state == "broken", f"got {state!r}")
        check("the refresh_run itself is still marked ok",
              run_status == "ok", f"got {run_status!r}")
    finally:
        cfg.processes_dir = orig_processes_dir
        cfg.refdata_register_path = orig_refdata_path
        refresh_mod.config.processes_dir = orig_processes_dir
        refresh_mod.config.refdata_register_path = orig_refdata_path
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------- refdata accuracy

def check_refdata_capture_accuracy() -> None:
    """The captured values must be the REAL ones from source, not placeholders."""
    store = KnowledgeStore(DB)
    row = store.query("""
        SELECT value FROM refdata_value
        WHERE source_id = 'inteacc.payroll.process-batch.application-ready.enabled'
        LIMIT 1""")
    check("config default captured matches the real @Value default",
          bool(row) and json.loads(row[0]["value"]) == {"default": "true"},
          str(row))

    rows = store.query(
        "SELECT label, value FROM refdata_value WHERE source_id='FUND_ACCOUNTING_SOURCE'")
    labels = {r["label"] for r in rows}
    check("reference-table rows use the real SOURCE_CODE as label, not a "
          "row index", "PF_FINAL_SETTLEMENT_APPROVAL" in labels, str(labels))
    if rows:
        cols = json.loads(rows[0]["value"])
        check("captured row carries real seeded columns",
              "ACTIVE" in cols and "CREDIT_CONTROL_ACCT_CATEGORY" in cols,
              str(cols))
    store.close()


def main() -> int:
    if not DB.exists():
        print("no build/knowledge.db — run ./eck-cli refresh first")
        return 1

    print("BR-43 — reference data has no write path")
    check_br43_no_write_route()

    # Everything below reads CAP-4 and CAP-6 tables. A knowledge.db built
    # before those capabilities landed simply does not have them, and the
    # honest report is "not exercised", not a stack trace that hides whether
    # the rest of the suite passed.
    store = KnowledgeStore(DB)
    have = store.has_table("process") and store.has_table("refdata_source")
    store.close()
    if not have:
        print("\nCAP-4 / CAP-6 assertions NOT EXERCISED")
        print("  build/knowledge.db predates these capabilities and carries no")
        print("  process or refdata tables. Run `./eck-cli refresh` to rebuild")
        print("  it, then re-run this suite to check them.")
        print(f"\n{passed} passed, {failed} failed, CAP-4/CAP-6 skipped")
        return 1 if failed else 0

    print("\nBR-42 — no excluded key/table is ever captured")
    check_br42_no_excluded_value_captured()

    print("\nBR-45/BR-46 — reference-data service never guesses")
    check_br45_refdata_service()

    print("\nCAP-4 — curated process stages, ordered and evidenced")
    check_process_stages_service()

    print("\nrefdata capture is accurate, not placeholder")
    check_refdata_capture_accuracy()

    print("\nCAP-4 vs BR-18 — a broken stage anchor degrades, never aborts")
    check_broken_stage_anchor_does_not_abort()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
