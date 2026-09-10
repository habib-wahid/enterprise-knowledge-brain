"""Acquire registered source from its remote (CAP-1 support for BR-01/BR-02).

The register names a remote; this module makes a local working copy of it.
Nothing else in the codebase knows a URL, exactly as nothing else knows a path.

Safety, in order of how badly it would go wrong:

  1. We only ever `git reset --hard` inside the project's own `sources/`
     cache. A root that resolves outside it is refused outright — a hard
     reset in someone's working repository would destroy uncommitted work.
  2. Before touching an existing checkout we verify its `origin` matches the
     register. A directory that happens to share a name is not our cache.
  3. We fetch and check out. We never push, never commit, never write to a
     remote. The estate is read-only (BR-53) and that includes its history.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import config


class SyncRefused(SystemExit):
    """Raised rather than run a destructive git command in the wrong place."""


@dataclass
class SyncResult:
    name: str
    action: str            # cloned | updated | unchanged | skipped
    root: Path
    ref: str | None = None
    before: str | None = None
    after: str | None = None
    detail: str = ""

    @property
    def moved(self) -> bool:
        return bool(self.before and self.after and self.before != self.after)


_ASKPASS = """#!/bin/sh
# Supplies credentials to git from the environment. Used only when
# ECK_GIT_TOKEN is set — a server or CI runner with no keychain.
case "$1" in
  Username*) echo "${ECK_GIT_USER:-oauth2}" ;;
  *)         echo "$ECK_GIT_TOKEN" ;;
esac
"""


def _askpass_script() -> Path:
    """Write the helper once per process, mode 0700.

    The token is passed via the environment, never as a command-line
    argument (visible in `ps`) and never written into .git/config.
    """
    path = Path(tempfile.gettempdir()) / f"eck-askpass-{os.getpid()}.sh"
    if not path.exists():
        path.write_text(_ASKPASS)
        path.chmod(0o700)
    return path


def _git(args: list[str], cwd: Path | None = None, timeout: int = 1800
         ) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Never let git block on an interactive credential prompt; fail visibly
    # instead so the caller can print something useful.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")

    token = config.git_token()
    if token:
        env["GIT_ASKPASS"] = str(_askpass_script())
        env["ECK_GIT_TOKEN"] = token

    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def head(root: Path) -> str | None:
    r = _git(["rev-parse", "HEAD"], cwd=root, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else None


def origin_of(root: Path) -> str | None:
    r = _git(["remote", "get-url", "origin"], cwd=root, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else None


def _same_remote(a: str, b: str) -> bool:
    """Compare remotes ignoring the cosmetic differences git tolerates."""
    def norm(u: str) -> str:
        u = u.strip().rstrip("/")
        if u.endswith(".git"):
            u = u[:-4]
        return u.replace("git@", "").replace(":", "/").lower()
    return norm(a) == norm(b)


def _assert_managed(root: Path, project_root: Path) -> None:
    """Refuse to manage anything outside the project's own sources cache."""
    cache = (project_root / "sources").resolve()
    resolved = root.resolve()
    if resolved != cache and cache not in resolved.parents:
        raise SyncRefused(
            f"\nREFUSING to sync {resolved}\n\n"
            f"Only paths inside {cache} are managed by `eck sources sync`,\n"
            f"because syncing runs `git reset --hard` and would discard any\n"
            f"uncommitted work in a repository you are actually using.\n\n"
            f"To fetch this source, point its `root` at a path under sources/\n"
            f"in register/estate.yaml. To keep using a local checkout instead,\n"
            f"give it `kind: dir` and it will be left alone.\n")


def sync(name: str, spec: dict, project_root: Path, verbose: bool = True,
         depth: int = 1) -> SyncResult:
    """Bring one registered source up to date with its remote."""
    kind = spec.get("kind", "dir")
    origin = spec.get("origin")
    ref = spec.get("ref")

    root = Path(spec["root"])
    if not root.is_absolute():
        root = (project_root / root).resolve()

    if kind != "git" or not origin:
        return SyncResult(name, "skipped", root, ref,
                          detail="no git origin in the register — local source")

    _assert_managed(root, project_root)

    say = (lambda m: print(m)) if verbose else (lambda m: None)
    git_dir = root / ".git"

    # ---------------------------------------------------------------- clone
    if not git_dir.exists():
        if root.exists() and any(root.iterdir()):
            raise SyncRefused(
                f"\n{root} exists and is not a git checkout.\n"
                f"Move or delete it, then run `eck sources sync` again.\n")
        root.parent.mkdir(parents=True, exist_ok=True)
        say(f"  {name}: cloning {origin}")
        args = ["clone", "--depth", str(depth), "--single-branch"]
        if ref:
            args += ["--branch", ref]
        args += [origin, str(root)]
        r = _git(args)
        if r.returncode != 0:
            shutil.rmtree(root, ignore_errors=True)
            raise SyncRefused(_auth_hint(name, origin, r.stderr))
        return SyncResult(name, "cloned", root, ref, after=head(root))

    # ---------------------------------------------------------------- update
    existing = origin_of(root)
    if existing and not _same_remote(existing, origin):
        raise SyncRefused(
            f"\n{root} is a checkout of\n    {existing}\n"
            f"but the register says\n    {origin}\n\n"
            f"Refusing to reset a repository that is not the one registered.\n")

    before = head(root)
    say(f"  {name}: fetching {ref or 'default branch'}")
    fetch_args = ["fetch", "--depth", str(depth), "origin"]
    if ref:
        fetch_args.append(ref)
    r = _git(fetch_args, cwd=root)
    if r.returncode != 0:
        raise SyncRefused(_auth_hint(name, origin, r.stderr))

    r = _git(["reset", "--hard", "FETCH_HEAD"], cwd=root)
    if r.returncode != 0:
        raise SyncRefused(f"{name}: could not check out FETCH_HEAD\n{r.stderr}")

    after = head(root)
    return SyncResult(name, "updated" if before != after else "unchanged",
                      root, ref, before=before, after=after)


def _auth_hint(name: str, origin: str, stderr: str) -> str:
    tail = "\n".join(l for l in stderr.strip().splitlines()[-6:])
    hint = ""
    low = stderr.lower()
    if "authentication" in low or "could not read" in low or "403" in low:
        hint = ("\nThis looks like an authentication failure. The register "
                "names a private\nremote, so git needs credentials for it:\n"
                "  · a token (servers and CI) — set ECK_GIT_TOKEN in .env\n"
                "    optionally ECK_GIT_USER if the host wants a real username\n"
                "  · macOS keychain (laptops) — clone once by hand\n"
                "  · SSH — change origin in estate.yaml to the git@ form and "
                "mount a key\n")
    elif "could not resolve host" in low or "timed out" in low:
        hint = ("\nThe host could not be reached. If this remote is on an "
                "internal network,\nconnect to the VPN and try again.\n")
    return f"\n{name}: git failed for {origin}\n\n{tail}\n{hint}"
