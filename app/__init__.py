"""g1-core: minimal, stable speak-on-command service for the Unitree G1."""

from __future__ import annotations

import subprocess
from pathlib import Path

# Bump on every meaningful release. We surface this in /health and at
# every boot so the operator knows what's actually running on the robot
# (the deploy story is "scp + restart" — a stale repo is easy to miss).
__version__ = "0.3.0"


def _git_sha() -> str:
    """Best-effort short git SHA. Empty string if not a git checkout."""
    repo = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short=8", "HEAD"],
            capture_output=True, text=True, timeout=2.0,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return ""


def version_string() -> str:
    """`0.3.0 (a1b2c3d4)` if git available, else just the version."""
    sha = _git_sha()
    return f"{__version__} ({sha})" if sha else __version__
