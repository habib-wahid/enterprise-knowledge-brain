"""CAP-6 — the reference-data allow-list (BR-41).

register/refdata.yaml is the ONLY place that says which configuration keys
and reference tables get snapshotted. Nothing downstream may add an entry
that is not listed here — that is what makes BR-42 ("no customer, personal
or transactional data") an enforceable property of the file rather than a
promise about the code that reads it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RefdataItem:
    id: str                 # a config key, or a table name
    kind: str                # 'config_property' | 'reference_table'
    asset_id: str
    reason: str


@dataclass
class RefdataExclusion:
    key_or_table: str
    reason: str


@dataclass
class RefdataRegister:
    items: list[RefdataItem] = field(default_factory=list)
    excluded: list[RefdataExclusion] = field(default_factory=list)
    path: Path | None = None


def load(path: Path) -> RefdataRegister:
    if not path.exists():
        return RefdataRegister(path=path)
    doc: dict[str, Any] = yaml.safe_load(path.read_bytes()) or {}

    items = []
    for entry in doc.get("items", []):
        key = entry.get("key") or entry.get("table")
        if not key:
            raise ValueError(f"refdata item missing key/table: {entry}")
        kind = "config_property" if "key" in entry else "reference_table"
        items.append(RefdataItem(
            id=key, kind=kind, asset_id=entry["asset"], reason=entry["reason"]))

    excluded = [RefdataExclusion(e["key"], e["reason"])
               for e in doc.get("excluded", [])]

    return RefdataRegister(items=items, excluded=excluded, path=path)


def validate(reg: RefdataRegister) -> list[str]:
    """BR-70 — surface problems rather than silently skip a bad entry."""
    problems = []
    seen = set()
    for it in reg.items:
        if it.id in seen:
            problems.append(f"{it.id}: duplicate refdata entry")
        seen.add(it.id)
        if not it.reason.strip():
            problems.append(f"{it.id}: missing reason (BR-41 requires one)")
    return problems
