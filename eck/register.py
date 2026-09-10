"""CAP-1 — reads the estate register.

Nothing else in the codebase may know a source path. If you find yourself
wanting to hardcode one, that is BR-02 telling you to add a register entry.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .store.db import sha256


@dataclass
class Exclusion:
    path_glob: str
    reason: str


@dataclass
class Asset:
    id: str
    name: str
    role: str
    owner: str
    tech: str
    source_kind: str          # 'code' | 'wiki'
    abs_path: Path
    exclusions: list[Exclusion] = field(default_factory=list)
    source_commit: str | None = None


@dataclass
class Register:
    estate_id: str
    estate_name: str
    platform: str
    assets: list[Asset]
    register_sha: str
    path: Path


def _git_commit(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def load(register_path: Path, project_root: Path) -> Register:
    raw_bytes = Path(register_path).read_bytes()
    doc: dict[str, Any] = yaml.safe_load(raw_bytes)

    estate = doc["estate"]
    defaults = doc.get("defaults", {})
    sources = doc["sources"]

    default_exclusions = [
        Exclusion(e["path_glob"] if "path_glob" in e else e["path"], e["reason"])
        for e in defaults.get("exclusions", [])
    ]

    assets: list[Asset] = []
    for a in doc["assets"]:
        source_kind = a.get("source", "code")
        src = sources[source_kind]
        root = Path(src["root"])
        if not root.is_absolute():
            root = (project_root / root).resolve()
        abs_path = (root / a["path"]).resolve()

        own_exclusions = [
            Exclusion(e.get("path_glob", e.get("path")), e["reason"])
            for e in a.get("exclusions", [])
        ]

        assets.append(Asset(
            id=a["id"],
            name=a["name"],
            role=a["role"],
            owner=a.get("owner", defaults.get("owner", "UNASSIGNED")),
            tech=a.get("tech", defaults.get("tech", "unknown")),
            source_kind=source_kind,
            abs_path=abs_path,
            exclusions=default_exclusions + own_exclusions,
            source_commit=_git_commit(root),
        ))

    return Register(
        estate_id=estate["id"],
        estate_name=estate["name"],
        platform=estate.get("platform", ""),
        assets=assets,
        register_sha=sha256(raw_bytes),
        path=Path(register_path),
    )


def validate(reg: Register) -> list[str]:
    """Return visible problems. BR-70: fail clearly, never silently omit."""
    problems: list[str] = []
    seen: set[str] = set()
    for a in reg.assets:
        if a.id in seen:
            problems.append(f"{a.id}: duplicate asset id in register")
        seen.add(a.id)
        if not a.abs_path.exists():
            problems.append(f"{a.id}: source path does not exist — {a.abs_path}")
    return problems
