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


# --------------------------------------------------------------- deployment
# Every path the platform uses is resolvable from an environment variable so
# a server can mount things wherever it likes. Defaults keep a developer
# checkout working with no configuration at all.

def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


def project_root() -> Path:
    return _env_path("ECK_PROJECT_ROOT", ROOT)


def db_path() -> Path:
    return _env_path("ECK_DB_PATH", project_root() / "build" / "knowledge.db")


def register_path() -> Path:
    return _env_path("ECK_REGISTER", project_root() / "register" / "estate.yaml")


def curated_dir() -> Path:
    return _env_path("ECK_CURATED_DIR", project_root() / "curated")


def sources_root() -> Path:
    """Where checked-out estate source lives, when it is present at all.

    A serving deployment usually has no source: the knowledge base is built
    elsewhere and only the database is shipped. Code that reads source must
    therefore tolerate its absence rather than assume it (see
    `store.source.resolve_asset_dir`).
    """
    return _env_path("ECK_SOURCES_ROOT", project_root() / "sources")


def embed_model() -> str:
    return os.environ.get("ECK_EMBED_MODEL", "").strip() or "BAAI/bge-small-en-v1.5"


def git_token() -> str:
    """Token for fetching a private remote on a machine with no keychain."""
    return os.environ.get("ECK_GIT_TOKEN", "").strip()
