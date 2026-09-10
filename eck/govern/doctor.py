"""Preflight — what this machine can and cannot do (deployment support).

Two profiles need different things, and conflating them is what makes
deployment fail:

  builder   fetches source and rebuilds the knowledge base. Needs git,
            network to the remotes, credentials, and the embedding model.
  server    serves answers from a prebuilt knowledge base. Needs the
            database file and the embedding model. Needs NO source, NO git,
            NO credentials.

`eck doctor` reports each check as ok / warn / fail against a profile, so a
server is never marked broken for lacking things it should not have.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from pathlib import Path

from .. import config

OK, WARN, FAIL = "ok", "warn", "fail"


def _check(name, status, detail, fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def run(profile: str = "auto") -> tuple[list[dict], str]:
    checks: list[dict] = []
    db = config.db_path()

    if profile == "auto":
        profile = "server" if db.exists() and not config.sources_root().exists() \
            else "builder"

    # ---------------------------------------------------------- both profiles
    for mod, why in (("tree_sitter", "Java extraction"),
                     ("yaml", "reading the register"),
                     ("numpy", "vector search"),
                     ("fastapi", "the web service"),
                     ("uvicorn", "serving")):
        try:
            importlib.import_module(mod)
            checks.append(_check(f"python: {mod}", OK, "importable"))
        except ImportError:
            checks.append(_check(f"python: {mod}", FAIL, f"missing — needed for {why}",
                                 "pip install -r requirements.txt"))

    try:
        importlib.import_module("sentence_transformers")
        checks.append(_check("python: sentence-transformers", OK, "importable"))
    except ImportError:
        checks.append(_check(
            "python: sentence-transformers", FAIL,
            "missing — meaning-based retrieval cannot run",
            "pip install -r requirements.txt"))

    reg = config.register_path()
    checks.append(_check("register", OK if reg.exists() else FAIL,
                         str(reg) if reg.exists() else f"not found at {reg}",
                         "set ECK_REGISTER to the estate.yaml location"))

    if db.exists():
        size = db.stat().st_size / 1_048_576
        checks.append(_check("knowledge.db", OK, f"{db} ({size:.0f} MB)"))
        try:
            from ..store.db import KnowledgeStore
            s = KnowledgeStore(db)
            n = s.scalar("SELECT COUNT(*) FROM node") or 0
            v = s.scalar("SELECT COUNT(*) FROM chunk_vec") or 0
            run_row = s.query("SELECT started_at, status FROM refresh_run"
                              " ORDER BY started_at DESC LIMIT 1")
            s.close()
            checks.append(_check("knowledge content", OK if n else FAIL,
                                 f"{n:,} nodes, {v:,} vectors"))
            if run_row:
                checks.append(_check("freshness", OK,
                                     f"built {run_row[0]['started_at']}"
                                     f" ({run_row[0]['status']})"))
            if not v:
                checks.append(_check("vectors", WARN,
                                     "no embeddings — meaning search disabled",
                                     "rebuild without --no-embed"))
        except Exception as exc:
            checks.append(_check("knowledge content", FAIL,
                                 f"cannot read: {type(exc).__name__}: {exc}"))
    else:
        checks.append(_check(
            "knowledge.db", FAIL if profile == "server" else WARN,
            f"not found at {db}",
            "ship the file from a builder and set ECK_DB_PATH, "
            "or run `eck refresh`"))

    # embedding model — the usual silent first-run surprise
    model = config.embed_model()
    hf = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    cached = hf.exists() and any(hf.rglob("*bge-small*"))
    checks.append(_check(
        "embedding model", OK if cached else WARN,
        f"{model} — {'cached at ' + str(hf) if cached else 'NOT cached'}",
        "" if cached else
        "first use downloads ~130 MB from huggingface.co; pre-warm it in the "
        "image or mount HF_HOME, or the first query will hang or fail offline"))

    # ---------------------------------------------------------- builder only
    if profile == "builder":
        git = shutil.which("git")
        checks.append(_check("git", OK if git else FAIL,
                             git or "not on PATH", "install git"))
        try:
            from .. import register as reg_mod
            r = reg_mod.load(reg, config.project_root())
            for name, spec in r.sources.items():
                origin = spec.get("origin")
                if not origin:
                    checks.append(_check(f"source: {name}", OK,
                                         "local, no remote configured"))
                    continue
                res = subprocess.run(
                    ["git", "ls-remote", "--heads", origin],
                    capture_output=True, text=True, timeout=25,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
                if res.returncode == 0:
                    checks.append(_check(f"source: {name}", OK,
                                         f"reachable — {origin}"))
                else:
                    err = (res.stderr or "").strip().splitlines()
                    checks.append(_check(
                        f"source: {name}", FAIL,
                        f"unreachable — {err[-1] if err else 'git failed'}",
                        "set ECK_GIT_TOKEN, configure a credential helper, "
                        "or connect to the network that hosts it"))
        except Exception as exc:
            checks.append(_check("sources", WARN,
                                 f"could not check: {type(exc).__name__}: {exc}"))
    else:
        checks.append(_check("source checkout", OK,
                             "not required — this profile serves a prebuilt "
                             "knowledge base"))

    # ---------------------------------------------------------- optional
    have_key, detail = config.answering_credential()
    checks.append(_check("anthropic credential", OK if have_key else WARN,
                         detail,
                         "" if have_key else
                         "only the LLM anchor proposer needs this; every other "
                         "feature runs without it"))

    curated = config.curated_dir()
    if curated.exists():
        n = len((curated / "anchors.jsonl").read_text().splitlines()) \
            if (curated / "anchors.jsonl").exists() else 0
        checks.append(_check("curated store", OK, f"{curated} — {n} decision(s)"))
    else:
        checks.append(_check("curated store", WARN, f"absent at {curated}",
                             "ship curated/ alongside the database: it is the "
                             "one thing a rebuild cannot regenerate"))

    worst = FAIL if any(c["status"] == FAIL for c in checks) else \
        WARN if any(c["status"] == WARN for c in checks) else OK
    return checks, profile if worst != FAIL else profile


def report(checks: list[dict], profile: str) -> str:
    mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
    out = [f"ECK preflight — profile: {profile}", ""]
    for c in checks:
        out.append(f"[{mark[c['status']]}] {c['name']:<28} {c['detail']}")
        if c["fix"] and c["status"] != OK:
            out.append(f"{'':<10}   -> {c['fix']}")
    fails = [c for c in checks if c["status"] == FAIL]
    warns = [c for c in checks if c["status"] == WARN]
    out += ["", f"{len(checks)} checks — {len(fails)} failing, {len(warns)} warning"]
    if fails:
        out.append("This deployment will NOT work until the failures are fixed.")
    elif warns:
        out.append("Usable, but read the warnings before relying on it.")
    else:
        out.append("Ready.")
    return "\n".join(out)
