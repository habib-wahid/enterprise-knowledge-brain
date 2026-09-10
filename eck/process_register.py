"""CAP-4 — process definitions (BR-22).

processes/*.yaml are engineer-authored, git-tracked files, curated the same
way estate.yaml is: by editing a file, not through a review queue. There
are always few processes and each needs real engineering judgement about
where a stage actually lives — unlike CAP-3 anchors, where hundreds of
candidates need a lightweight approve/reject loop instead.

Order here is the ONLY source of stage order (BR-22). Nothing is inferred
from source layout or call order — see the flow.downstream service's own
disclosed limitation for why that would be dishonest.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class StageAnchor:
    asset_id: str
    fqn: str


@dataclass
class FailurePath:
    stage_key: str
    description: str
    anchor_fqn: str | None
    anchor_asset: str | None


@dataclass
class Stage:
    key: str
    name: str
    description: str
    anchors: list[StageAnchor]
    is_entry: bool = False
    entry_trigger: str | None = None


@dataclass
class Process:
    id: str
    name: str
    description: str
    stages: list[Stage] = field(default_factory=list)
    failures: list[FailurePath] = field(default_factory=list)
    authored_by: str | None = None
    authored_at: str | None = None
    source_path: Path | None = None


def _git_meta(path: Path) -> tuple[str | None, str | None]:
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "log", "-1",
             "--format=%an%x1f%aI", "--", path.name],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None, None
        author, date = out.stdout.strip().split("\x1f")
        return author, date
    except Exception:
        return None, None


def _anchor(entry: Any, default_asset: str) -> StageAnchor:
    if isinstance(entry, str):
        return StageAnchor(asset_id=default_asset, fqn=entry)
    return StageAnchor(asset_id=entry.get("asset", default_asset), fqn=entry["fqn"])


def load_one(path: Path) -> Process:
    doc: dict[str, Any] = yaml.safe_load(path.read_bytes())
    default_asset = doc.get("asset", "")

    stages = []
    for s in doc["stages"]:
        stages.append(Stage(
            key=s["key"], name=s["name"], description=s["description"],
            anchors=[_anchor(a, s.get("asset", default_asset))
                    for a in s.get("anchors", [])],
            is_entry=bool(s.get("is_entry", False)),
            entry_trigger=s.get("entry_trigger")))

    failures = [FailurePath(
        stage_key=f["stage"], description=f["description"],
        anchor_fqn=f.get("anchor"), anchor_asset=f.get("asset", default_asset))
        for f in doc.get("failures", [])]

    author, date = _git_meta(path)
    return Process(id=doc["id"], name=doc["name"], description=doc["description"],
                  stages=stages, failures=failures,
                  authored_by=author, authored_at=date, source_path=path)


def load_all(processes_dir: Path) -> list[Process]:
    if not processes_dir.exists():
        return []
    return [load_one(p) for p in sorted(processes_dir.glob("*.yaml"))]


def validate(processes: list[Process]) -> list[str]:
    problems = []
    seen_ids = set()
    for p in processes:
        if p.id in seen_ids:
            problems.append(f"{p.id}: duplicate process id")
        seen_ids.add(p.id)
        if not p.stages:
            problems.append(f"{p.id}: has no stages")
        keys = [s.key for s in p.stages]
        if len(keys) != len(set(keys)):
            problems.append(f"{p.id}: duplicate stage keys")
        if not any(s.is_entry for s in p.stages):
            problems.append(f"{p.id}: no stage marked is_entry (BR-24)")
        for f in p.failures:
            if f.stage_key not in keys:
                problems.append(f"{p.id}: failure references unknown stage "
                                f"{f.stage_key!r}")
    return problems
