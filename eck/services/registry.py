"""CAP-7 — the single authoritative definition of every answer service.

BR-51 ("define each service in one authoritative place") and BR-64 ("new
services automatically become available in every access mode") are the same
requirement. A decorator-backed registry satisfies both by construction:
the CLI, the HTTP surface and the MCP server all enumerate this registry, so
adding a service here makes it appear in all three with no further wiring
and no chance of the three drifting apart.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from .result import ServiceResult


@dataclass
class ServiceSpec:
    id: str
    category: str
    question: str          # BR-47: the one question category it answers
    when_to_use: str       # BR-52: and how it differs from its neighbours
    inputs: dict[str, str]
    fn: Callable[..., ServiceResult]
    composite: bool = False
    required: list[str] = field(default_factory=list)
    # CAP-7 FAQ layer. 0 = a building block, invoked by id; 1..n = one of the
    # numbered questions a person actually asks, and the order they are
    # offered in. The Ask page lists these; the CLI and MCP see them too,
    # because they are ordinary registry entries (BR-64).
    faq: int = 0
    example: str = ""            # a concrete input, so the question is legible
    answers_with: str = ""       # what the answer contains, in one line

    def signature(self) -> str:
        args = " ".join(f"<{k}>" if k in self.required else f"[{k}]"
                        for k in self.inputs)
        return f"{self.id} {args}".strip()


REGISTRY: dict[str, ServiceSpec] = {}


def service(id: str, category: str, question: str, when_to_use: str,
            inputs: dict[str, str], composite: bool = False,
            faq: int = 0, example: str = "", answers_with: str = ""):
    """Register one service. All services are read-only (BR-53)."""
    def wrap(fn: Callable[..., ServiceResult]) -> Callable[..., ServiceResult]:
        if id in REGISTRY:
            raise ValueError(f"duplicate service id {id!r} — BR-51 requires "
                             f"exactly one definition per service")
        sig = inspect.signature(fn)
        required = [name for name, p in sig.parameters.items()
                    if name != "ctx" and p.default is inspect.Parameter.empty]
        REGISTRY[id] = ServiceSpec(
            id=id, category=category, question=question,
            when_to_use=when_to_use, inputs=inputs, fn=fn,
            composite=composite, required=required,
            faq=faq, example=example, answers_with=answers_with)
        return fn
    return wrap


def get(id: str) -> ServiceSpec:
    if id not in REGISTRY:
        raise KeyError(
            f"no service {id!r}. Available: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[id]


def all_specs() -> list[ServiceSpec]:
    """FAQ services first, in their numbered order; then the building blocks."""
    return sorted(REGISTRY.values(),
                  key=lambda s: (s.faq == 0, s.faq, s.composite, s.category, s.id))


def faq_specs() -> list[ServiceSpec]:
    """The numbered questions, in order — what the Ask page offers."""
    load_all()
    return sorted((s for s in REGISTRY.values() if s.faq), key=lambda s: s.faq)


def load_all() -> None:
    """Import every service module so the decorators run."""
    from . import atomic, composite, faq  # noqa: F401


def catalogue() -> list[dict[str, Any]]:
    """Machine-readable service list — what MCP tool discovery is built on
    (BR-63), and what `eck services` prints."""
    load_all()
    return [{
        "id": s.id, "category": s.category, "question": s.question,
        "when_to_use": s.when_to_use, "inputs": s.inputs,
        "required": s.required, "composite": s.composite,
        "faq": s.faq, "example": s.example, "answers_with": s.answers_with,
        "read_only": True,
    } for s in all_specs()]
