#!/usr/bin/env python3
"""Report which Moodle credentials are configured in .env — never prints secrets.

Usage (from the workspace root):
    python .claude/skills/moodle-grading/scripts/check_env.py
    python .claude/skills/moodle-grading/scripts/check_env.py --env path/to/.env

Exit code: 0 if every required variable is set, 1 otherwise.
Only MOODLE_URL (not a secret) is echoed; the other values are reported as set/missing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values

REQUIRED = ("MOODLE_URL", "MOODLE_USERNAME", "MOODLE_PASSWORD")
OPTIONAL = ("MOODLE_TOKEN",)


def check_env(env_path: Path) -> tuple[list[str], list[str]]:
    """Return (missing_required, problems) for the given .env file."""
    if not env_path.is_file():
        return list(REQUIRED), [f"{env_path} no existe (copie .env.example a .env)"]
    values = dotenv_values(env_path)
    missing = [key for key in REQUIRED if not (values.get(key) or "").strip()]
    problems: list[str] = []
    url = (values.get("MOODLE_URL") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        problems.append("MOODLE_URL debe comenzar con http:// o https://")
    return missing, problems


def _force_utf8() -> None:
    """Windows consoles default to cp1252, which cannot encode some of our messages."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", default=".env", help="Path to the .env file (default: .env)")
    args = parser.parse_args(argv)
    _force_utf8()

    env_path = Path(args.env)
    missing, problems = check_env(env_path)
    values = dotenv_values(env_path) if env_path.is_file() else {}

    for key in REQUIRED + OPTIONAL:
        is_set = bool((values.get(key) or "").strip())
        shown = values.get(key) if key == "MOODLE_URL" and is_set else ""
        label = "OK     " if is_set else ("FALTA  " if key in REQUIRED else "(vacío)")
        print(f"{label} {key} {shown}".rstrip())
    for problem in problems:
        print(f"PROBLEMA: {problem}", file=sys.stderr)

    if missing or problems:
        print(f"\nFaltan o son inválidas: {', '.join(missing) or 'ver problemas'}", file=sys.stderr)
        return 1
    print("\nCredenciales completas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
