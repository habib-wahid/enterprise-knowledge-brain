"""Contract tests for the web UI's answer-synthesis layer.

synthesize.py is deliberately outside the CAP-7 registry (it is the one
place in the platform that calls a model) — these tests assert its failure
modes degrade cleanly, since a summarization failure must never break the
underlying search it sits on top of.

Run:  ./.venv/bin/python tests/test_synthesize.py
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest import mock

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eck.services.retrieval import Hit           # noqa: E402
from eck.services import synthesize as syn        # noqa: E402

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


SAMPLE_HITS = [Hit(
    chunk_id="c1", asset_id="HR", source="wiki", origin="curated",
    heading="Salary on-hold", path="Payroll-management.md",
    start_line=158, end_line=181, text="Some real wiki text about holds.",
    score=1.0, matched_by="both", similarity=0.8)]


def check_no_evidence() -> None:
    r = syn.synthesize("anything", [], "none")
    check("no hits -> no summary, clear reason, never raises",
          not r.available and "no search results" in (r.error or ""))


def check_no_credential() -> None:
    with mock.patch("eck.services.synthesize.answering_credential",
                    return_value=(False, "not configured")):
        r = syn.synthesize("q", SAMPLE_HITS, "strong")
    check("no credential configured -> clear reason, no crash",
          not r.available and "ANTHROPIC_API_KEY" in (r.error or ""))


def _fake_client_raising(exc: Exception):
    client = mock.MagicMock()
    client.messages.create.side_effect = exc
    return client


def check_error_paths() -> None:
    import anthropic

    cases = [
        (anthropic.AuthenticationError(
            message="bad key", response=mock.MagicMock(status_code=401),
            body=None), "invalid"),
        (anthropic.RateLimitError(
            message="slow down", response=mock.MagicMock(status_code=429),
            body=None), "rate limited"),
        (anthropic.BadRequestError(
            message="Your credit balance is too low to access the API",
            response=mock.MagicMock(status_code=400), body=None),
         "no API credit"),
    ]
    for exc, expect_substr in cases:
        with mock.patch("eck.services.synthesize.answering_credential",
                        return_value=(True, "ANTHROPIC_API_KEY (…test)")), \
             mock.patch("anthropic.Anthropic",
                        return_value=_fake_client_raising(exc)):
            r = syn.synthesize("q", SAMPLE_HITS, "strong")
        check(f"{type(exc).__name__} degrades to a clear, specific reason "
              f"(not a crash)",
              not r.available and expect_substr.lower() in (r.error or "").lower(),
              repr(r.error))


def check_success_path() -> None:
    text_block = mock.MagicMock(type="text", text="A grounded paragraph [1].")
    fake_response = mock.MagicMock(content=[text_block], stop_reason="end_turn")
    client = mock.MagicMock()
    client.messages.create.return_value = fake_response
    with mock.patch("eck.services.synthesize.answering_credential",
                    return_value=(True, "x")), \
         mock.patch("anthropic.Anthropic", return_value=client):
        r = syn.synthesize("q", SAMPLE_HITS, "strong")
    check("a normal response is returned as an available summary",
          r.available and r.summary == "A grounded paragraph [1]."
          and r.model == syn.MODEL)

    call_kwargs = client.messages.create.call_args.kwargs
    check("BR-61: the grounding instruction is in the system prompt sent "
          "to the model", "only from" in call_kwargs["system"].lower()
          or "ONLY" in call_kwargs["system"])
    check("the evidence sent to the model includes a real source location",
          "Payroll-management.md:158" in call_kwargs["messages"][0]["content"])
    check("effort is set for a fast, cheap synthesis call",
          call_kwargs.get("output_config", {}).get("effort") == "low")
    check("no leftover Opus-5-only beta/fallback params sent for this model",
          "fallbacks" not in call_kwargs and "betas" not in call_kwargs)
    check("model is the requested Sonnet 4.6, not the prior claude-opus-5 default",
          call_kwargs.get("model") == "claude-sonnet-4-6")


def main() -> int:
    print("no-evidence and no-credential short-circuits")
    check_no_evidence()
    check_no_credential()

    print("\nAPI error paths degrade to a specific, honest reason")
    check_error_paths()

    print("\nsuccess path — prompt shape and grounding")
    check_success_path()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
