#!/usr/bin/env python3
"""Extract distinct user IDs and names from Moodle log CSV files.

Usage:
    python extract_users.py logs_*.csv
    python extract_users.py --course-name "TELEMATICA Y REDES" --output Users.md logs_*.csv
    python extract_users.py *.csv -o Users.md
"""

import argparse
import csv
import glob
import os
import re
import sys


def extract_user_id(desc: str) -> str | None:
    if not desc:
        return None
    match = re.search(r"The user with id '(\d+)'", desc)
    return match.group(1) if match else None


def collect_users(file_paths: list[str]) -> dict[str, str]:
    users: dict[str, str] = {}
    for path in file_paths:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    full_name = row.get("Nombre completo del usuario", "").strip()
                    desc = row.get("Descripción", "") or row.get("Descripcion", "")
                    uid = extract_user_id(desc)
                    if full_name and uid:
                        if uid in users and users[uid] != full_name:
                            print(
                                f"Warning: user ID {uid} has conflicting names "
                                f"({users[uid]!r} and {full_name!r}); keeping the first.",
                                file=sys.stderr,
                            )
                        elif uid not in users:
                            users[uid] = full_name
        except (OSError, csv.Error) as e:
            print(f"Warning: could not read {path}: {e}", file=sys.stderr)
    return users


def write_markdown(users: dict[str, str], output_path: str, course_name: str, source_files: list[str]) -> None:
    sorted_users = sorted(users.items(), key=lambda x: int(x[0]))
    source_names = ", ".join(os.path.basename(f) for f in source_files)
    with open(output_path, "w", encoding="utf-8") as md:
        md.write(f"# Users - {course_name}\n\n")
        md.write(f"Distinct users extracted from log files ({source_names})\n\n")
        md.write("| User ID | Nombre completo |\n")
        md.write("|---------|-----------------|\n")
        for uid, name in sorted_users:
            md.write(f"| {uid} | {name} |\n")
    print(f"Created {output_path} with {len(sorted_users)} users")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract distinct Moodle user IDs from log CSV files and produce a markdown table."
    )
    parser.add_argument(
        "logs", nargs="+", metavar="CSV_FILE",
        help="One or more Moodle log CSV files (supports glob patterns).",
    )
    parser.add_argument(
        "-o", "--output", default="Users.md", metavar="PATH",
        help="Output markdown file (default: Users.md).",
    )
    parser.add_argument(
        "--course-name", default="", metavar="NAME",
        help="Course name for the file header. Auto-detected from CSV context column if omitted.",
    )
    args = parser.parse_args()

    resolved: list[str] = []
    for pattern in args.logs:
        matches = glob.glob(pattern)
        if matches:
            resolved.extend(matches)
        elif os.path.isfile(pattern):
            resolved.append(pattern)
        else:
            print(f"Warning: no files matched '{pattern}'", file=sys.stderr)
    if not resolved:
        print("Error: no CSV files found.", file=sys.stderr)
        sys.exit(1)

    course_name = args.course_name
    if not course_name:
        try:
            with open(resolved[0], "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ctx = row.get("Contexto del evento", "")
                    m = re.search(r"Curso:\s*(.+)", ctx)
                    if m:
                        course_name = m.group(1).strip()
                        break
        except (OSError, csv.Error, StopIteration):
            pass
        if not course_name:
            course_name = "Unknown Course"

    users = collect_users(resolved)
    if not users:
        print("Error: no users found in the provided CSV files.", file=sys.stderr)
        sys.exit(1)

    write_markdown(users, args.output, course_name, resolved)


if __name__ == "__main__":
    main()
