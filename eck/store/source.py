"""Reading estate source, wherever it happens to live — or not at all.

The database records the absolute path each asset was built from. That path
belongs to the BUILD machine and will not exist on a server that was shipped
only `knowledge.db`. Resolution therefore tries, in order:

  1. the recorded absolute path (the developer case — source is right there)
  2. the recorded project-relative path under ECK_SOURCES_ROOT (the case
     where source is present but mounted somewhere else)
  3. nothing — and callers fall back to the text captured in the index

Step 3 is the important one. A serving deployment with no source at all is a
supported, normal configuration: it is what makes "build inside the boundary,
serve the file anywhere" true rather than aspirational.
"""
from __future__ import annotations

from pathlib import Path

from ..config import project_root, sources_root


def resolve_asset_dir(abs_path: str | None, rel_path: str | None) -> Path | None:
    """Where this asset's source is on THIS machine, or None."""
    if abs_path:
        p = Path(abs_path)
        if p.exists():
            return p
    if rel_path:
        rel = Path(rel_path)
        if rel.is_absolute():
            return rel if rel.exists() else None
        # Recorded relative to the project root, e.g. sources/repo/module.
        for base in (project_root(), sources_root().parent):
            candidate = (base / rel).resolve()
            if candidate.exists():
                return candidate
    return None


def read_span(asset_dir: Path | None, path: str, start: int, end: int
              ) -> tuple[list[str], str]:
    """Return (lines, provenance). Provenance is 'filesystem' or 'unavailable'."""
    if asset_dir is None:
        return [], "unavailable"
    full = asset_dir / path
    try:
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return [], "unavailable"
    lo = max(1, start)
    hi = min(len(lines), max(start, end))
    return lines[lo - 1:hi], "filesystem"


def indexed_text(store, asset_id: str, path: str, start: int) -> str | None:
    """The span as captured at index time — the fallback when source is absent.

    Not a substitute for the file: chunks are capped and cover only methods
    and type declarations. It is enough to show what a piece of evidence says,
    which is what an answer needs.
    """
    rows = store.query(
        "SELECT text FROM chunk WHERE asset_id = ? AND path = ?"
        " AND start_line = ? LIMIT 1", (asset_id, path, start))
    if rows:
        return rows[0]["text"]
    rows = store.query(
        "SELECT text FROM chunk WHERE asset_id = ? AND path = ?"
        " ORDER BY ABS(start_line - ?) LIMIT 1", (asset_id, path, start))
    return rows[0]["text"] if rows else None
