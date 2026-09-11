"""Answer synthesis for the web UI's Search view — NOT a CAP-7 service.

Every service in registry.py is deliberately model-free (BR-50 puts the LLM
outside the services; test_services.py asserts none of them writes to the
store or needs a network call). This module is the one deliberate exception
in the whole platform, and it stays out of the registry on purpose: it is
the "someone reads the evidence and writes a sentence" step the MCP surface
already delegates to whatever assistant is connected, offered here for a
human browsing the web UI directly, with no assistant of their own.

It NEVER replaces the evidence — the UI shows the paragraph THEN the same
ranked hit cards it always did. If synthesis fails for any reason (no
credential, no credit, a rate limit), the caller still gets the raw
retrieval result; a summarization failure must never hide the underlying
search (BR-70 — no silent gaps, applied to this layer too).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import answering_credential
from .result import GROUNDING
from .retrieval import Hit

# Sonnet 4.6, per explicit request — verified live against this account
# (claude-sonnet-4-0 / claude-sonnet-4-20250514, i.e. bare "Sonnet 4", are
# both fully retired: 404 on this account). No server-side-fallback beta
# here — that mechanism exists for the "refusal" stop_reason introduced on
# Claude Opus 5 / Claude Fable 5.1, which 4.6 does not produce; the API
# accepts the beta flag as a harmless no-op, but keeping it would be
# leftover machinery for a case this model can't hit.
MODEL = "claude-sonnet-4-6"

SYSTEM = (
    "You answer questions about a Jmix-based payroll and HR platform using "
    "ONLY the numbered evidence excerpts given to you. " + GROUNDING +
    "\n\nWrite two or three SHORT paragraphs (one to three sentences each), "
    "separated by a single blank line — never one dense block. A natural "
    "split is what happens / how it happens / what to watch out for "
    "(exceptions, failure paths, gaps in the evidence), but only use "
    "paragraphs the evidence actually supports; do not pad a short answer "
    "out to three paragraphs. Cite evidence inline with bracket numbers "
    "like [1] or [2, 3] next to each claim that depends on it. Do not add a "
    "references list or a heading — the evidence is already displayed "
    "separately below your answer, so restating it is redundant. If the "
    "evidence does not actually answer the question, say so plainly instead "
    "of stretching a partial match into an answer.")


@dataclass
class Synthesis:
    summary: str | None
    model: str | None
    error: str | None      # human-readable reason synthesis did not run

    @property
    def available(self) -> bool:
        return self.summary is not None


def _format_evidence(hits: list[Hit]) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        origin = "curated wiki" if h.source == "wiki" else "derived code"
        parts.append(
            f"[{i}] ({origin}, {h.asset_id}) {h.heading}\n"
            f"    {h.path}:{h.start_line}-{h.end_line}\n"
            f"    {h.text[:900]}")
    return "\n\n".join(parts)


def synthesize(question: str, hits: list[Hit], verdict: str) -> Synthesis:
    """Best-effort. Never raises — a failure here degrades to no summary,
    never to a broken page."""
    if not hits:
        return Synthesis(None, None, "no search results to summarise")

    have_cred, detail = answering_credential()
    if not have_cred:
        return Synthesis(None, None,
                         "no ANTHROPIC_API_KEY configured — see .env.example")

    try:
        import anthropic
    except ImportError:
        return Synthesis(None, None, "the anthropic package is not installed")

    evidence_block = _format_evidence(hits)
    user_msg = (
        f"QUESTION: {question}\n\n"
        f"MATCH CONFIDENCE: {verdict}"
        f"{' (below the confident threshold — hedge accordingly)' if verdict == 'weak' else ''}"
        f"\n\nEVIDENCE:\n{evidence_block}")

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            return Synthesis(None, None, "the model returned an empty response")
        return Synthesis(text, MODEL, None)

    except anthropic.AuthenticationError:
        return Synthesis(None, None, "the configured API key is invalid")
    except anthropic.PermissionDeniedError:
        return Synthesis(None, None, "the API key lacks permission for this model")
    except anthropic.RateLimitError:
        return Synthesis(None, None, "rate limited — try again shortly")
    except anthropic.BadRequestError as exc:
        msg = str(exc)
        if "credit balance" in msg.lower():
            return Synthesis(None, None, "no API credit available on this "
                                         "account (add credit to enable "
                                         "summaries)")
        return Synthesis(None, None, f"request rejected: {msg[:160]}")
    except anthropic.APIStatusError as exc:
        return Synthesis(None, None, f"API error ({exc.status_code})")
    except Exception as exc:                          # pragma: no cover
        return Synthesis(None, None, f"{type(exc).__name__}: {str(exc)[:160]}")
