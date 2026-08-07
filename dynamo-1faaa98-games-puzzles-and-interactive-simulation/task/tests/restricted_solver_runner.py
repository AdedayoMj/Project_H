#!/usr/bin/env python3
"""Run a submitted solver without exposing verifier implementation files."""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


VERIFIER_ROOT = Path(__file__).resolve().parent
BLOCKED_EVENTS = {
    "os.exec",
    "os.fork",
    "os.forkpty",
    "os.posix_spawn",
    "os.system",
    "socket.bind",
    "socket.connect",
    "socket.getaddrinfo",
    "subprocess.Popen",
}


def verifier_path(value: object) -> bool:
    """Return whether an opened path belongs to the mounted verifier tree."""
    if isinstance(value, int):
        return False
    try:
        candidate = Path(os.fsdecode(os.fspath(value))).resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    return candidate == VERIFIER_ROOT or VERIFIER_ROOT in candidate.parents


def restrict_verifier_access(event: str, arguments: tuple[object, ...]) -> None:
    """Deny oracle reads and process/network escapes after this runner starts."""
    if event == "open" and arguments and verifier_path(arguments[0]):
        raise PermissionError("submitted solvers cannot read verifier files")
    if event in BLOCKED_EVENTS:
        raise PermissionError(f"submitted solvers cannot use {event}")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: restricted_solver_runner.py SOLVER [ARG ...]")
    solver = Path(sys.argv[1]).resolve(strict=True)
    arguments = sys.argv[2:]

    # Isolated mode omits PYTHONPATH, user site packages, and the working directory.
    # Remove this script's directory too, then expose only the solver's own directory
    # plus the interpreter's standard and installed package locations.
    sys.path = [
        entry
        for entry in sys.path
        if entry and Path(entry).resolve(strict=False) != VERIFIER_ROOT
    ]
    sys.path.insert(0, str(solver.parent))
    sys.dont_write_bytecode = True
    sys.addaudithook(restrict_verifier_access)
    sys.argv = [str(solver), *arguments]
    runpy.run_path(str(solver), run_name="__main__")


if __name__ == "__main__":
    main()
