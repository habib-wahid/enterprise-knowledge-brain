"""Contract tests for the CAP-7 answer services.

These assert the properties the BRD makes promises about, rather than any
particular answer — answers change as the estate changes, the contract
must not.

Run:  ./.venv/bin/python tests/test_services.py
"""
from __future__ import annotations

import hashlib
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eck.services.registry import REGISTRY, catalogue, load_all  # noqa: E402
from eck.services.resolve import Context                          # noqa: E402
from eck.services.result import UNKNOWN                           # noqa: E402

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


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    if not DB.exists():
        print("no build/knowledge.db — run ./eck-cli refresh first")
        return 1
    load_all()
    cat = catalogue()

    print("registry contract")
    check("BR-51 every service defined exactly once",
          len(cat) == len({c['id'] for c in cat}))
    check("BR-47 every service states its question",
          all(c["question"].strip() for c in cat))
    check("BR-52 every service says when to use it and how it differs",
          all(len(c["when_to_use"]) > 40 for c in cat))
    check("BR-53 every service is declared read-only",
          all(c["read_only"] for c in cat))
    check("BR-48 all eleven answer categories are covered",
          {c["category"] for c in cat} >= {
              "navigation", "impact", "flow", "checks", "effects",
              "explanation", "placement", "search", "detail",
              "configuration", "status"},
          str(sorted({c["category"] for c in cat})))
    check("BR-49 composite services exist",
          len([c for c in cat if c["composite"]]) >= 4)

    print("\ninvocation contract")
    before = digest(DB)
    ctx = Context(DB)
    results = {}
    probes = {
        "navigation.find": {"element": "SalaryPaymentSendBackServiceBean"},
        "impact.of_change": {"element": "SalaryPaymentSendBackServiceBean"},
        "flow.downstream": {"element": "SalaryPaymentSendBackServiceBean"},
        "checks.enforced_in": {"element": "SalaryPaymentSendBackServiceBean"},
        "effects.of": {"element": "SalaryPaymentSendBackServiceBean"},
        "explanation.of": {"element": "SalaryPaymentSendBackServiceBean"},
        "placement.for_check": {"element": "SalaryPaymentSendBackServiceBean"},
        "search.knowledge": {"question": "how is a salary hold released"},
        "detail.source": {"element": "SalaryPaymentSendBackServiceBean"},
        "configuration.affecting": {"element": "SalaryPaymentSendBackServiceBean"},
        "status.platform": {},
        "composite.change_impact": {"element": "SalaryPaymentSendBackServiceBean"},
        "composite.failure_trace": {"symptom": "PayrollTaxValidationException"},
        "composite.input_acceptance": {"element": "SalaryPaymentSendBackServiceBean"},
        "composite.process_description": {"process": "salary hold and release"},
    }
    check("every registered service has a probe",
          set(probes) == set(REGISTRY), str(set(REGISTRY) ^ set(probes)))

    errors = []
    for sid, kwargs in probes.items():
        try:
            results[sid] = REGISTRY[sid].fn(ctx, **kwargs)
        except Exception as exc:
            errors.append(f"{sid}: {type(exc).__name__}: {exc}")
    ctx.close()
    check("every service invokes without raising", not errors,
          "; ".join(errors[:3]))
    check("BR-53 invoking every service does not modify the store",
          digest(DB) == before)

    print("\nanswer contract")
    check("BR-50 every result carries presentation guidance",
          all(r.presentation_guidance.strip() for r in results.values()))
    check("BR-61 every result carries the grounding instruction",
          all("only from the evidence" in r.grounding for r in results.values()))
    check("BR-55 every unknown says what would be needed",
          all(r.needed for r in results.values() if r.outcome == UNKNOWN),
          str([s for s, r in results.items()
               if r.outcome == UNKNOWN and not r.needed]))
    answered = {s: r for s, r in results.items()
                if r.outcome != UNKNOWN and s != "status.platform"}
    check("BR-54 every answered result carries source evidence",
          all(r.evidence for r in answered.values()),
          str([s for s, r in answered.items() if not r.evidence]))
    check("BR-35 every evidence entry has a resolvable location",
          all(e.path and e.start_line > 0
              for r in results.values() for e in r.evidence))
    check("BR-20 evidence records derived vs curated",
          all(e.origin in ("derived", "curated", "inferred")
              for r in results.values() for e in r.evidence))

    print("\nrefusal contract (BR-55)")
    ctx = Context(DB)
    junk = REGISTRY["navigation.find"].fn(ctx, element="kubernetes helm chart")
    unanchored = REGISTRY["explanation.of"].fn(
        ctx, element="SalaryPaymentStatusServiceBean")
    ctx.close()
    check("out-of-scope subject returns unknown, not a near match",
          junk.outcome == UNKNOWN, junk.outcome)
    check("unknown carries no fabricated evidence", not junk.evidence)
    check("missing curated meaning returns unknown rather than inventing one",
          unanchored.outcome == UNKNOWN, unanchored.outcome)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
