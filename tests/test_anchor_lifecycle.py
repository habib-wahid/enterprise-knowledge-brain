"""End-to-end proof of the BR-18 anchor lifecycle.

Builds a throwaway two-file Java estate in a temp dir, approves an anchor
against it, then mutates that source to drive each transition:

    resolved  -> stale    (body edited under an approved statement)
    stale     -> broken   (method renamed; the anchor points at nothing)
    broken    -> refresh ABORTS, and the live knowledge.db is left untouched

The registered legacy repository is never written to; this is why the test
uses a synthetic estate rather than editing real source.

Run:  ./.venv/bin/python tests/test_anchor_lifecycle.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eck import config, curated, register              # noqa: E402
from eck.curated import Anchor                         # noqa: E402
from eck.govern import anchor_check, refresh           # noqa: E402
from eck.store.db import KnowledgeStore, sha256, utc_now  # noqa: E402

JAVA_V1 = """package com.demo.pay;

import io.jmix.core.DataManager;

public class SalaryHoldService {
    private DataManager dataManager;

    public boolean canRelease(SalaryHold hold) {
        if (hold.getApprovedBy() == null) {
            return false;
        }
        return hold.getAmount() > 0;
    }
}
"""

# Same method, different body -> same fqn, different span hash -> STALE
JAVA_V2 = JAVA_V1.replace("return hold.getAmount() > 0;",
                          "return hold.getAmount() > 0 && !hold.isLocked();")

# Method renamed -> fqn gone -> BROKEN
JAVA_V3 = JAVA_V2.replace("canRelease", "isReleasable")

WIKI = """# Salary hold

## Releasing a hold

A salary hold may only be released when it has an approver recorded and the
held amount is greater than zero.
"""

FQN = "com.demo.pay.SalaryHoldService#canRelease(SalaryHold hold)"

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def write_estate(tmp: Path, java: str) -> Path:
    code = tmp / "src" / "demo"
    code.mkdir(parents=True, exist_ok=True)
    (code / "SalaryHoldService.java").write_text(java)
    wiki = tmp / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Salary-hold.md").write_text(WIKI)

    reg = tmp / "estate.yaml"
    reg.write_text(f"""
version: 1
estate:
  id: TESTESTATE
  name: "Synthetic test estate"
defaults:
  owner: test
  tech: jmix
  exclusions:
    - path: "**/build/**"
      reason: "build output"
sources:
  code: {{root: {tmp / 'src'}, kind: dir}}
  wiki: {{root: {tmp / 'wiki'}, kind: dir}}
assets:
  - id: DEMO
    name: "Demo payroll"
    role: "synthetic"
    path: demo
  - id: WIKI
    name: "Demo wiki"
    role: "synthetic"
    path: "."
    source: wiki
    tech: markdown
""")
    return reg


def build(reg_path: Path, tmp: Path, allow_broken: bool = False) -> Path:
    reg = register.load(reg_path, tmp)
    return refresh.build(reg, tmp / "build", verbose=False, embed=False,
                         allow_broken=allow_broken)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="eck-anchor-test-"))
    # Redirect the curated store so the real project's anchors are untouched.
    curated.CANDIDATES = tmp / "candidates.jsonl"
    curated.ANCHORS = tmp / "anchors.jsonl"
    # This test is about CAP-3 anchors only. Without this, refresh.build()
    # would load the REAL register/refdata.yaml (real assets like HR, FUND)
    # against this synthetic estate's DEMO/WIKI assets and fail on a foreign
    # key — the M6 stages need their own isolation, same as the curated store.
    orig_processes_dir, orig_refdata_path = (config.processes_dir,
                                             config.refdata_register_path)
    config.processes_dir = lambda: tmp / "no-processes"
    config.refdata_register_path = lambda: tmp / "no-refdata.yaml"

    try:
        print("1. build the synthetic estate")
        reg_path = write_estate(tmp, JAVA_V1)
        db = build(reg_path, tmp)
        store = KnowledgeStore(db)
        node = store.query(
            "SELECT id, span_sha FROM node WHERE asset_id='DEMO' AND fqn=?",
            (FQN,))
        check("target method is extracted", bool(node), f"fqn={FQN}")
        if not node:
            return 1
        chunk = store.query(
            "SELECT id, text, path, start_line, end_line FROM chunk"
            " WHERE source='wiki' LIMIT 1")[0]
        store.close()

        print("2. a reviewer approves an anchor")
        a = Anchor(
            id=Anchor.make_id(chunk["id"], "DEMO", FQN),
            chunk_id=chunk["id"], statement=chunk["text"],
            statement_sha=sha256(chunk["text"].encode()),
            doc_path=chunk["path"], doc_start_line=chunk["start_line"],
            doc_end_line=chunk["end_line"],
            target_asset="DEMO", target_kind="method", target_fqn=FQN,
            target_path="SalaryHoldService.java", target_start_line=1,
            target_end_line=1, span_sha_at_approval="", confidence=0.9,
            justification="test", proposer="test")
        store = KnowledgeStore(db)
        stamped = anchor_check.stamp(store, a)
        store.close()
        check("approval stamps the current span hash", stamped)
        a.approve("tester", "looks right")
        curated.save_anchors([a])

        print("3. unchanged source -> RESOLVED")
        db = build(reg_path, tmp)
        store = KnowledgeStore(db)
        rows = anchor_check.validate(store, curated.load_approved(), "r")
        store.close()
        check("state is resolved", rows[0]["state"] == "resolved",
              f"got {rows[0]['state']}")

        print("4. body edited -> STALE")
        write_estate(tmp, JAVA_V2)
        db = build(reg_path, tmp)
        store = KnowledgeStore(db)
        rows = anchor_check.validate(store, curated.load_approved(), "r")
        store.close()
        check("state is stale", rows[0]["state"] == "stale",
              f"got {rows[0]['state']}")
        check("stale does NOT abort a refresh",
              anchor_check.enforce(rows) is None)

        print("5. method renamed -> BROKEN, and refresh must abort")
        write_estate(tmp, JAVA_V3)
        live_before = (tmp / "build" / "knowledge.db").read_bytes()
        aborted = False
        try:
            build(reg_path, tmp)
        except anchor_check.BrokenAnchors as exc:
            aborted = True
            msg = str(exc)
        check("refresh raised BrokenAnchors", aborted)
        if aborted:
            check("the error names the lost target", FQN in msg)
            check("the error names who approved it", "tester" in msg)
        live_after = (tmp / "build" / "knowledge.db").read_bytes()
        check("previously published knowledge.db is untouched",
              live_before == live_after)

        print("6. --allow-broken publishes, but marks the anchor broken")
        db = build(reg_path, tmp, allow_broken=True)
        store = KnowledgeStore(db)
        state = store.scalar("SELECT state FROM anchor LIMIT 1")
        store.close()
        check("override publishes", Path(db).exists())
        check("anchor is recorded as broken, not silently dropped",
              state == "broken", f"got {state!r}")

    finally:
        config.processes_dir = orig_processes_dir
        config.refdata_register_path = orig_refdata_path
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
