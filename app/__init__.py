"""g1-core: minimal, stable speak-on-command service for the Unitree G1."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

__version__ = "0.3.0"


def _git(*args: str) -> Optional[str]:
    """Run a `git -C <repo> ...` command and return stripped stdout, or
    None on any failure (no git, not a checkout, network-less hook, …).
    Two-second hard timeout so a frozen ssh-keychain prompt can't hang
    the boot sequence."""
    repo = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=2.0,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def git_sha() -> str:
    """Short git SHA (8 hex chars), or empty string if unavailable."""
    return _git("rev-parse", "--short=8", "HEAD") or ""


def git_full_sha() -> str:
    """Full 40-char git SHA, or empty string if unavailable. Useful for
    building a stable GitHub permalink to the exact running revision."""
    return _git("rev-parse", "HEAD") or ""


def git_repo_url() -> str:
    """HTTPS URL of the `origin` remote, normalised so it works as a
    browser link. Returns empty string if the repo has no `origin` or
    we can't parse it.

    We strip the trailing `.git` and convert `git@github.com:foo/bar`
    SSH URLs into `https://github.com/foo/bar` so the operator panel
    can just append `/commit/<sha>` to it.
    """
    raw = _git("remote", "get-url", "origin")
    if not raw:
        return ""
    raw = raw.strip()
    if raw.endswith(".git"):
        raw = raw[:-4]
    # git@github.com:owner/repo  →  https://github.com/owner/repo
    m = re.match(r"git@([^:]+):(.+)$", raw)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    if raw.startswith(("http://", "https://")):
        return raw
    return ""


# Backwards-compatible alias used by older call-sites in main.py.
def _git_sha() -> str:
    return git_sha()


def version_string() -> str:
    """`0.3.0 (a1b2c3d4)` if git available, else just the version."""
    sha = git_sha()
    return f"{__version__} ({sha})" if sha else __version__
