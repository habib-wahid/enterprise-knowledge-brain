"""Project-local configuration.

Reads a .env file from the project root into the environment so credentials
live in one known place instead of a shell someone forgot to export.

The data requirements are explicit that credentials are not stored with
knowledge or in source control, so:
  * .env is gitignored, and .env.example (no secret) is what gets committed
  * an already-exported environment variable always wins, so CI and the
    shell can override the file without editing it
  * nothing here ever logs or prints a key's value
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def load_env(path: Path | None = None) -> list[str]:
    """Populate os.environ from .env. Returns the names of keys it set."""
    path = path or ENV_FILE
    if not path.exists():
        return []

    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:   # an exported var wins
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def answering_credential() -> tuple[bool, str]:
    """Is a credential present for the answer/proposal models? Never returns it."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        raw = os.environ.get(var, "")
        if raw:
            return True, f"{var} (…{raw[-4:]})"
    return False, "not configured"
