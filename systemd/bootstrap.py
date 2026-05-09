#!/usr/bin/env python3
"""
Bootstrap = "make the install bullet-proof to Windows-edited files".

Why this exists
---------------
Editing shell scripts on Windows or copying them via scp / WinSCP / a
network drive often turns LF into CRLF. bash then explodes with the
classic `$'\r': command not found` and `set: pipefail: invalid option
name`, and `install.sh` can't even reach the line where it would have
fixed the line endings itself.

Python's tokenizer reads source files in universal-newline mode, so
this script runs identically whether it arrived as LF or CRLF. That
makes it the safest possible entry point.

What it does
------------
  1. Strips CRLF from every .sh / .service / .py file in the repo.
  2. Marks the shell scripts executable.
  3. Re-execs `bash systemd/install.sh` with the now-clean files.

Usage on the robot
------------------
    cd ~/g1-core
    python3 systemd/bootstrap.py            # full install
    python3 systemd/bootstrap.py --fix-only # just sanitize, don't install
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Patterns we care about. .py is included so a CRLF main.py doesn't
# crash the service in a different way (Python tolerates it but `#!`
# shebangs do not when the file is invoked directly).
PATTERNS = ("*.sh", "*.service", "*.py")

# Subset that should be executable.
EXEC_GLOBS = ("systemd/*.sh", "scripts/*.sh", "systemd/bootstrap.py")


def strip_crlf(path: Path) -> bool:
    """Return True if the file actually had CRLF and we rewrote it."""
    try:
        data = path.read_bytes()
    except Exception as e:
        print(f"[bootstrap] skip {path} ({e})")
        return False
    if b"\r\n" not in data:
        return False
    fixed = data.replace(b"\r\n", b"\n")
    path.write_bytes(fixed)
    return True


def collect_targets() -> list[Path]:
    seen: set[Path] = set()
    for pat in PATTERNS:
        for p in REPO.rglob(pat):
            # Skip virtualenvs and caches.
            if any(part in {"__pycache__", ".venv", ".git"} for part in p.parts):
                continue
            seen.add(p.resolve())
    return sorted(seen)


def make_executable() -> None:
    for pattern in EXEC_GLOBS:
        for p in REPO.glob(pattern):
            mode = p.stat().st_mode
            p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main(argv: list[str]) -> int:
    fix_only = "--fix-only" in argv

    print(f"[bootstrap] repo = {REPO}")
    fixed = 0
    for path in collect_targets():
        if strip_crlf(path):
            print(f"[bootstrap] CRLF → LF: {path.relative_to(REPO)}")
            fixed += 1
    print(f"[bootstrap] cleaned {fixed} file(s) of CRLF")

    make_executable()
    print("[bootstrap] chmod +x on systemd/*.sh + scripts/*.sh")

    if fix_only:
        return 0

    install = REPO / "systemd" / "install.sh"
    if not install.is_file():
        print(f"[bootstrap] ERROR: {install} missing — nothing to install", file=sys.stderr)
        return 2

    print(f"[bootstrap] exec bash {install.relative_to(REPO)}")
    os.chdir(REPO)
    return subprocess.call(["bash", str(install)])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
