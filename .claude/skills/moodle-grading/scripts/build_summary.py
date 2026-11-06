#!/usr/bin/env python3
"""Validate grade_<userid>.txt files against rubric.json and build the Paso 7 summary.

The strict-scale rule (only scores the rubric allows, total = exact sum) is enforced here
mechanically instead of relying on the model's arithmetic.

Usage (from the workspace root):
    # Which students still need grading? (TSV: userid, name, folder)
    python .claude/skills/moodle-grading/scripts/build_summary.py \\
        --assignment-dir submissions/assignments/assignment_517559 \\
        --users-file Users.md --list-pending

    # Validate ONE grade file right after writing it (exit 1 on any error)
    python .claude/skills/moodle-grading/scripts/build_summary.py \\
        --rubric submissions/assignments/assignment_517559/rubric.json \\
        --validate submissions/assignments/assignment_517559/user_3081/grade_3081.txt

    # Build grading_summary.csv + grading_resumen_global.md
    python .claude/skills/moodle-grading/scripts/build_summary.py \\
        --assignment-dir submissions/assignments/assignment_517559 --users-file Users.md

rubric.json:
    {"curso": "NOMBRE DEL CURSO", "nota_aprobacion": 70,
     "items": [{"rubro": "Portada", "max": 3, "valores": [0, 3]},
               {"rubro": "Ejercicios", "max": 5}]}
    "valores" lists the ONLY scores allowed for the item; when omitted it defaults to the
    integers 0..max. "nota_aprobacion" is optional (enables the borderline auto-flag).

review_flags.json (optional, written by the orchestrator from the graders' reports):
    {"3081": {"revisar": "Motivo breve", "estado": "error_ilegible"}}
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

CSV_HEADER = ["User ID", "Nombre Estudiante", "Nota", "Nota Máxima", "Estado"]
OVERRIDE_STATES = {"calificado", "error_pdf_corrupto", "error_ilegible"}
TOTAL_RE = re.compile(r"Nota\s+total[:\s]+(-?[\d.]+)\s*/\s*(-?[\d.]+)", re.IGNORECASE)
DEFAULT_MARGIN_PCT = 5.0


# ─── helpers ────────────────────────────────────────────────────────────────
def _force_utf8() -> None:
    """Windows consoles default to cp1252, which cannot encode some of our messages."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _fold(text: str) -> str:
    """Casefold, strip accents and collapse whitespace (for tolerant comparisons)."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _upload_module():
    """Import lazily so parse_grade_file / parse_users_file stay the single source of truth."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import moodle_upload_grades

    return moodle_upload_grades


# ─── rubric ─────────────────────────────────────────────────────────────────

def load_rubric(path: Path) -> dict:
    """Load and normalise rubric.json; raise ValueError on any schema problem."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer la rúbrica {path}: {exc}") from exc
    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError("rubric.json: 'items' debe ser una lista no vacía")
    items = []
    for index, entry in enumerate(items_raw, start=1):
        name = str(entry.get("rubro", "")).strip()
        if not name:
            raise ValueError(f"rubric.json: el ítem {index} no tiene 'rubro'")
        if "valores" in entry:
            values = sorted({float(v) for v in entry["valores"]})
        elif "max" in entry:
            values = [float(v) for v in range(int(float(entry["max"])) + 1)]
        else:
            raise ValueError(f"rubric.json: el ítem '{name}' necesita 'valores' o 'max'")
        if not values:
            raise ValueError(f"rubric.json: el ítem '{name}' no tiene valores permitidos")
        items.append({"rubro": name, "valores": values, "max": max(values)})
    rubric = {
        "curso": str(raw.get("curso", "")).strip(),
        "nota_aprobacion": raw.get("nota_aprobacion"),
        "items": items,
    }
    rubric["max_total"] = sum(i["max"] for i in items)
    return rubric


# ─── grade file validation ──────────────────────────────────────────────────

def validate_grade_file(path: Path, rubric: dict) -> tuple[dict | None, list[str], list[str]]:
    """Return (parsed, errors, warnings) for one grade_<userid>.txt."""
    uploader = _upload_module()
    parsed = uploader.parse_grade_file(str(path))
    if parsed is None:
        return None, [f"{path.name}: no se pudo interpretar (¿falta la tabla de rubros?)"], []

    errors: list[str] = []
    warnings: list[str] = []
    rows = parsed["rubric_items"]
    items = rubric["items"]

    if len(rows) != len(items):
        errors.append(f"la tabla tiene {len(rows)} rubros; la rúbrica define {len(items)}")
    for index, (row, item) in enumerate(zip(rows, items), start=1):
        if _fold(row["label"]) != _fold(item["rubro"]):
            errors.append(f"rubro {index}: '{row['label']}' ≠ '{item['rubro']}' (orden/nombre)")
        if row["score"] not in item["valores"]:
            allowed = ", ".join(_fmt(v) for v in item["valores"])
            errors.append(
                f"rubro '{item['rubro']}': puntaje {_fmt(row['score'])} no permitido "
                f"(permitidos: {allowed})"
            )

    text = path.read_text(encoding="utf-8", errors="replace")
    total_match = TOTAL_RE.search(text)
    expected_total = sum(row["score"] for row in rows)
    if not total_match:
        errors.append("falta la línea 'Nota total: X / Y'")
    else:
        stated, maximum = float(total_match.group(1)), float(total_match.group(2))
        if abs(stated - expected_total) > 1e-9:
            errors.append(
                f"Nota total {_fmt(stated)} ≠ suma de la tabla {_fmt(expected_total)}"
            )
        if abs(maximum - rubric["max_total"]) > 1e-9:
            errors.append(
                f"máximo declarado {_fmt(maximum)} ≠ suma de máximos {_fmt(rubric['max_total'])}"
            )

    feedback = parsed["feedback"]
    if not feedback:
        errors.append("falta el párrafo de Retroalimentación")
    elif re.search(r"\n\s*\n", feedback):
        errors.append("la Retroalimentación debe ser un solo párrafo (hay líneas en blanco)")
    errors_name, warnings_name = _name_leak(parsed["student_name"], feedback)
    errors += errors_name
    warnings += warnings_name
    if re.search(r"\bsemanas?\b|\bcronograma", _fold(feedback)):
        warnings.append("la Retroalimentación menciona semanas/cronograma (prohibido)")

    parsed["total_calc"] = expected_total
    return parsed, errors, warnings


def _name_leak(student_name: str, feedback: str) -> tuple[list[str], list[str]]:
    """The student's name must never appear in the feedback paragraph."""
    tokens = [t for t in re.split(r"\s+", _fold(student_name)) if len(t) >= 3]
    if not tokens or not feedback:
        return [], []
    folded = _fold(feedback)
    hits = [t for t in tokens if re.search(rf"\b{re.escape(t)}\b", folded)]
    if len(hits) >= min(2, len(tokens)):
        return ["la Retroalimentación contiene el nombre del estudiante (prohibido)"], []
    if hits:
        return [], [f"la Retroalimentación coincide con parte del nombre ('{hits[0]}'); revisar"]
    return [], []


# ─── folder inspection ──────────────────────────────────────────────────────

def has_pdf(folder: Path) -> bool:
    return any(
        p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith("grade_")
        for p in folder.iterdir()
    )


def pre_status(folder: Path, uid: int) -> str:
    """Deterministic triage (Paso 6, steps 2–4). Returns an estado or 'pendiente'."""
    if (folder / f"grade_{uid}.txt").is_file():
        return "calificado"
    if not folder.is_dir():
        return "pendiente_sin_carpeta"
    if has_pdf(folder):
        return "pendiente"
    if (folder / "no_submission_found.txt").is_file():
        return "sin_entrega"
    return "omitido_sin_pdf"


def collect_students(assignment_dir: Path, users_file: str) -> list[tuple[int, str]]:
    """Union of Users.md and discovered user_<id> folders, sorted by User ID."""
    uploader = _upload_module()
    users = dict(uploader.parse_users_file(users_file)) if users_file else {}
    for folder in assignment_dir.glob("user_*"):
        suffix = folder.name.removeprefix("user_")
        if suffix.isdigit() and int(suffix) not in users:
            users[int(suffix)] = ""
    return sorted(users.items())


# ─── commands ───────────────────────────────────────────────────────────────

def cmd_list_pending(assignment_dir: Path, users_file: str) -> int:
    counts: dict[str, int] = {}
    for uid, name in collect_students(assignment_dir, users_file):
        folder = assignment_dir / f"user_{uid}"
        status = pre_status(folder, uid)
        counts[status] = counts.get(status, 0) + 1
        if status == "pendiente":
            print(f"{uid}\t{name or '(sin nombre)'}\t{folder.as_posix()}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "sin estudiantes"
    print(f"# triage: {summary}", file=sys.stderr)
    return 0


def cmd_validate(grade_file: Path, rubric: dict) -> int:
    _, errors, warnings = validate_grade_file(grade_file, rubric)
    for warning in warnings:
        print(f"AVISO  {grade_file.name}: {warning}")
    for error in errors:
        print(f"ERROR  {grade_file.name}: {error}")
    if errors:
        return 1
    print(f"OK     {grade_file.name}")
    return 0


def cmd_build(
    assignment_dir: Path, users_file: str, rubric: dict, flags_path: Path | None, margin: float
) -> int:
    flags: dict[str, dict] = {}
    if flags_path and flags_path.is_file():
        flags = json.loads(flags_path.read_text(encoding="utf-8"))

    rows: list[dict] = []
    review: list[tuple[int, str, str]] = []
    failures = 0
    for uid, name in collect_students(assignment_dir, users_file):
        folder = assignment_dir / f"user_{uid}"
        state = pre_status(folder, uid)
        score = 0.0
        reasons: list[str] = []
        if state == "calificado":
            parsed, errors, warnings = validate_grade_file(folder / f"grade_{uid}.txt", rubric)
            name = name or (parsed or {}).get("student_name", "")
            if errors:
                failures += 1
                state = "error_formato"
                reasons += [f"Archivo de nota inválido: {e}" for e in errors]
            else:
                score = parsed["total_calc"]
            reasons += [f"Aviso: {w}" for w in warnings]
        elif state.startswith("pendiente"):
            failures += 1
            reasons.append("Entrega con PDF todavía sin calificar")

        flag = flags.get(str(uid), {})
        if flag.get("estado") in OVERRIDE_STATES and state in {"calificado", "error_formato"}:
            if flag["estado"] != "calificado":
                state = flag["estado"]
        if flag.get("revisar"):
            reasons.append(str(flag["revisar"]))

        pass_mark = rubric.get("nota_aprobacion")
        if state == "calificado" and pass_mark is not None:
            band = rubric["max_total"] * margin / 100
            if abs(score - float(pass_mark)) <= band:
                reasons.append(
                    f"Nota {_fmt(score)} en zona límite respecto a la aprobación ({_fmt(float(pass_mark))})"
                )

        rows.append({"uid": uid, "name": name, "score": score, "state": state})
        if reasons:
            review.append((uid, name, " ".join(reasons)))

    csv_path = assignment_dir / "grading_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for r in rows:
            writer.writerow(
                [r["uid"], r["name"], _fmt(r["score"]), _fmt(rubric["max_total"]), r["state"]]
            )

    markdown = render_markdown(rows, review, rubric)
    (assignment_dir / "grading_resumen_global.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\nEscritos: {csv_path} y grading_resumen_global.md", file=sys.stderr)
    if failures:
        print(
            f"ATENCIÓN: {failures} entrega(s) sin calificar o con archivo inválido.",
            file=sys.stderr,
        )
    return 1 if failures else 0


def render_markdown(rows: list[dict], review: list[tuple[int, str, str]], rubric: dict) -> str:
    graded = [r for r in rows if r["state"] == "calificado"]
    max_total = _fmt(rubric["max_total"])
    average = sum(r["score"] for r in graded) / len(graded) if graded else 0.0
    count = {s: sum(1 for r in rows if r["state"] == s) for s in {r["state"] for r in rows}}
    errors = sum(n for s, n in count.items() if s.startswith("error"))
    pending = sum(n for s, n in count.items() if s.startswith("pendiente"))

    lines = [
        f"# Resumen global de calificaciones{' — ' + rubric['curso'] if rubric['curso'] else ''}",
        "",
        "| User ID | Nombre Estudiante | Nota | Nota Máxima | Estado |",
        "|---------|-------------------|------|-------------|--------|",
    ]
    lines += [
        f"| {r['uid']} | {r['name']} | {_fmt(r['score'])} | {max_total} | {r['state']} |"
        for r in rows
    ]
    lines += [
        "",
        "## Totales",
        "",
        f"- Total de estudiantes procesados: {len(rows)}",
        f"- Calificados: {len(graded)} (promedio {average:.1f} / {max_total})",
        f"- Omitidos: sin entrega {count.get('sin_entrega', 0)}, "
        f"sin PDF {count.get('omitido_sin_pdf', 0)}",
        f"- Errores (archivos corruptos, ilegibles, ZIP inválido, nota inválida): {errors}",
        f"- Pendientes de calificar: {pending}",
        f"- **Casos marcados para revisión: {len(review)}** "
        "(la decisión final es siempre del profesor)",
        "",
        "## Casos que pueden requerir revisión del profesor",
        "",
    ]
    if review:
        lines += ["| User ID | Nombre | Motivo |", "|---------|--------|--------|"]
        lines += [f"| {uid} | {name} | {reason} |" for uid, name, reason in review]
    else:
        lines.append(
            "_Ningún caso marcado. Aun así, la calificación asistida es un borrador: "
            "verifique un muestreo de notas contra los PDF originales._"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate grade files and build grading_summary.csv / grading_resumen_global.md"
    )
    parser.add_argument("--assignment-dir", type=Path, help="submissions/assignments/assignment_<cmid>")
    parser.add_argument("--users-file", default="Users.md")
    parser.add_argument("--rubric", type=Path, help="rubric.json (default: <assignment-dir>/rubric.json)")
    parser.add_argument("--flags", type=Path, help="review_flags.json (default: <assignment-dir>/review_flags.json)")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN_PCT,
                        help="Borderline band, %% of max total, around nota_aprobacion (default 5)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-pending", action="store_true", help="Print students that still need grading")
    mode.add_argument("--validate", type=Path, metavar="GRADE_FILE", help="Validate one grade file")
    args = parser.parse_args(argv)
    _force_utf8()

    if args.list_pending:
        if not args.assignment_dir:
            parser.error("--list-pending requiere --assignment-dir")
        return cmd_list_pending(args.assignment_dir, args.users_file)

    rubric_path = args.rubric or (args.assignment_dir / "rubric.json" if args.assignment_dir else None)
    if rubric_path is None:
        parser.error("indique --rubric o --assignment-dir")
    try:
        rubric = load_rubric(rubric_path)
    except ValueError as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 2

    if args.validate:
        return cmd_validate(args.validate, rubric)
    if not args.assignment_dir:
        parser.error("--assignment-dir es obligatorio para generar el resumen")
    flags_path = args.flags or args.assignment_dir / "review_flags.json"
    return cmd_build(args.assignment_dir, args.users_file, rubric, flags_path, args.margin)


if __name__ == "__main__":
    sys.exit(main())
