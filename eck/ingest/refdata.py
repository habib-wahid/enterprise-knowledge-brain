"""CAP-6 — capture the values behind an allow-listed key or table.

Two kinds of reference data exist in this estate, and neither is a Spring
`application.properties` (there isn't one — see the estate-specific note in
the README). Both are read from git-tracked source, so both are exactly the
"approved, reviewed" values BR-39/BR-40 ask for: nobody edits them without a
merge request.

  config_property   a `@Value("${key:default}")` field. The literal default
                     is the value; no live config server is consulted
                     (BR-40 — no live connection, by construction).
  reference_table    rows seeded into a table via Liquibase <insert>. These
                     are parsed with a small line-tracking scanner rather
                     than a full XML library, so BR-35 evidence can point at
                     an exact line, and so the platform gains no new
                     dependency for a handful of small files.

A table later touched by a Liquibase <update> changeset would make an
insert-only snapshot quietly wrong — so that case is detected and reported
as a gap rather than presented as current (BR-70).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ..refdata_register import RefdataItem
from ..register import Asset
from ..store.db import sha256

VALUE_ANNOTATION = re.compile(
    r'@Value\(\s*"\$\{\s*([\w.\-]+)\s*:\s*([^}]*)\}"\s*\)')

INSERT_BLOCK = re.compile(
    r'<insert\s+tableName="([A-Za-z0-9_]+)"[^>]*>(.*?)</insert>', re.S)
UPDATE_BLOCK = re.compile(r'<update\s+tableName="([A-Za-z0-9_]+)"')
COLUMN = re.compile(
    r'<column\s+name="([A-Za-z0-9_]+)"\s+value(?:Numeric|Boolean)?="([^"]*)"')


def _git_commit_date(repo_root: Path, rel: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%aI", "--", rel],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def capture_config_property(item: RefdataItem, asset: Asset
                            ) -> list[dict[str, Any]]:
    """Find every @Value site for this key and its literal default."""
    rows: list[dict[str, Any]] = []
    for path in asset.abs_path.rglob("*.java"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in VALUE_ANNOTATION.finditer(text):
            key, default = m.group(1), m.group(2)
            if key != item.id:
                continue
            rel = str(path.relative_to(asset.abs_path))
            line = _line_of(text, m.start())
            rows.append(dict(
                id=sha256(f"{item.id}|{rel}|{line}".encode())[:16],
                source_id=item.id, asset_id=asset.id, label=item.id,
                value=json.dumps({"default": default}),
                path=rel, start_line=line,
                snapshot_at=_git_commit_date(asset.abs_path, rel)
                            or "unknown", origin="derived"))
    return rows


def capture_reference_table(item: RefdataItem, asset: Asset
                            ) -> tuple[list[dict[str, Any]], list[str]]:
    """Every seeded row for this table, plus any staleness caveats."""
    rows: list[dict[str, Any]] = []
    caveats: list[str] = []
    seen_update = False

    changelog_files = list(asset.abs_path.rglob("*.xml"))
    for path in changelog_files:
        if "liquibase" not in path.as_posix().lower():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if not seen_update and any(m.group(1) == item.id
                                   for m in UPDATE_BLOCK.finditer(text)):
            seen_update = True

        for m in INSERT_BLOCK.finditer(text):
            table, body = m.group(1), m.group(2)
            if table != item.id:
                continue
            cols = {c: v for c, v in COLUMN.findall(body)}
            rel = str(path.relative_to(asset.abs_path))
            line = _line_of(text, m.start())
            label = cols.get("CODE") or cols.get("SOURCE_CODE") \
                or cols.get("NAME") or cols.get("ID") or f"row@{line}"
            rows.append(dict(
                id=sha256(f"{item.id}|{rel}|{line}".encode())[:16],
                source_id=item.id, asset_id=asset.id, label=label,
                value=json.dumps(cols, sort_keys=True),
                path=rel, start_line=line,
                snapshot_at=_git_commit_date(asset.abs_path, rel)
                            or "unknown", origin="derived"))

    if seen_update:
        caveats.append(
            f"{item.id}: at least one Liquibase <update> changeset touches "
            f"this table. This snapshot lists insert-time values only and "
            f"may not reflect later updates (BR-40 — no live connection "
            f"means this can go stale silently unless said so).")
    return rows, caveats
