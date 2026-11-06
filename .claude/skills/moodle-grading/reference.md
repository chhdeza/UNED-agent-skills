# Reference — Moodle Grading Skill

All scripts live at `.claude/skills/moodle-grading/scripts/`. Run them from the workspace root.

## Scripts

### check_env.py

Reports which required `.env` variables are set **without printing any secret** (only `MOODLE_URL`, which is not secret, is echoed). Exit code 1 if anything is missing or malformed. Use it instead of reading `.env`.

```bash
python .claude/skills/moodle-grading/scripts/check_env.py
python .claude/skills/moodle-grading/scripts/check_env.py --env path/to/.env
```

---

### build_summary.py

Mechanical enforcement of the strict-scale rule plus the Paso 7 report. Reads `rubric.json` (see [File Formats](#rubricjson)) and reuses the `grade_<userid>.txt` parser of `moodle_upload_grades.py`, so grading and upload agree on the format.

```bash
# Students that still need grading (TSV: userid, name, folder); triage counts on stderr
python .claude/skills/moodle-grading/scripts/build_summary.py --assignment-dir submissions/assignments/assignment_517559 --users-file Users.md --list-pending

# Validate one grade file (exit 1 on any ERROR)
python .claude/skills/moodle-grading/scripts/build_summary.py --rubric submissions/assignments/assignment_517559/rubric.json --validate submissions/assignments/assignment_517559/user_3081/grade_3081.txt

# Build grading_summary.csv + grading_resumen_global.md (exit 1 if anything is ungraded/invalid)
python .claude/skills/moodle-grading/scripts/build_summary.py --assignment-dir submissions/assignments/assignment_517559 --users-file Users.md
```

Validation checks: same number/order/name of rubric rows as `rubric.json`; every score in that row's allowed `valores`; `Nota total` equals the exact sum of the table; declared maximum equals the sum of rubric maxima; exactly one feedback paragraph; the student's name absent from the feedback (error if two or more name tokens appear, warning for one); no mention of weeks/schedules (warning). Options: `--flags FILE` (default `<assignment-dir>/review_flags.json`), `--margin PCT` (borderline band around `nota_aprobacion`, default 5 % of the maximum).

---

### moodle_download.py

Downloads student submission files from Moodle assignments.

#### Environment variables (.env)

| Variable | Required | Description |
|----------|----------|-------------|
| `MOODLE_URL` | Yes | Base URL (e.g. `https://aprende.uned.ac.cr`) |
| `MOODLE_USERNAME` | Yes | Login username |
| `MOODLE_PASSWORD` | Yes | Login password. Wrap in double quotes if it contains special chars: `"'pass\"word"` |
| `MOODLE_TOKEN` | No | Web Services API token (enables REST API mode for single-user queries) |

#### Commands

```bash
# List in-progress courses
python .claude/skills/moodle-grading/scripts/moodle_download.py --in-progress

# Show structure for a specific course
python .claude/skills/moodle-grading/scripts/moodle_download.py --in-progress --course 8067

# Download submissions for all students
python .claude/skills/moodle-grading/scripts/moodle_download.py --course 8067 --assignment 517559 --users-file Users.md

# Download for a single student
python .claude/skills/moodle-grading/scripts/moodle_download.py --course 8067 --assignment 517559 --user 3081

# With group filter and custom output directory
python .claude/skills/moodle-grading/scripts/moodle_download.py --course 8067 --assignment 517559 --users-file Users.md --group 38525 --download-dir hw2

# Debug mode
python .claude/skills/moodle-grading/scripts/moodle_download.py --course 8067 --assignment 517559 --user 655 --debug
```

#### Output structure

(`<download-dir>` defaults to `submissions`.)

```
submissions/
└── assignments/
    └── assignment_517559/           ← Moodle assignment cmid; separate folder per assignment
        ├── user_655/
        │   └── no_submission_found.txt
        ├── user_3081/
        │   ├── Tarea2_EstudianteEjemplo.pdf
        │   ├── captura_cuestionario.png
        │   ├── evidencias.zip
        │   ├── _extracted/
        │   │   └── evidencias/
        │   │       └── extra.png
        │   └── grade_3081.txt       ← created during grading
        ├── consigna.md              ← Paso 5: transcription of the assignment requirements
        ├── rubric.json              ← Paso 5: approved rubric (items, order, allowed scores)
        ├── review_flags.json        ← Paso 6/7: cases flagged for professor review
        ├── grading_summary.csv      ← all students, one row each
        └── grading_resumen_global.md
```

#### Technical notes

- Uses `action=grade` (server-rendered), NOT `action=grader` (JS SPA).
- Filters `pluginfile.php` URLs: only `assignsubmission` paths, excludes `/intro/`.
- 1-second delay between users to avoid server overload.
- Files are saved under `<download-dir>/assignments/assignment_<cmid>/user_<userid>/`. The `--course` flag only scopes course listing / validation; it does not change this path.

---

### extract_users.py

Extracts distinct student user IDs from Moodle log CSV files.

```bash
# Basic usage (auto-detects course name from CSV)
python .claude/skills/moodle-grading/scripts/extract_users.py log1.csv log2.csv

# With explicit course name and output file
python .claude/skills/moodle-grading/scripts/extract_users.py *.csv --course-name "TELEMATICA Y REDES" -o Users.md
```

#### How it works

1. Reads the `Descripción` column for patterns like `The user with id '3081'`.
2. Pairs each user ID with the `Nombre completo del usuario` column.
3. Deduplicates by user ID, sorts numerically, writes markdown table.

---

### extract_submission_zip.py

Extrae archivos `.zip` en la carpeta de entrega de un estudiante hacia `_extracted/<nombre_sanitizado>/` con protección contra zip-slip, enlaces simbólicos y bombas ZIP. Los límites predeterminados son 200 MB descomprimidos y 10 000 entradas.

```bash
python .claude/skills/moodle-grading/scripts/extract_submission_zip.py submissions/assignments/assignment_517559/user_3081

# Ajustar límite (MB)
python .claude/skills/moodle-grading/scripts/extract_submission_zip.py path/to/folder --max-mb 200

# Ajustar también el máximo de entradas
python .claude/skills/moodle-grading/scripts/extract_submission_zip.py path/to/folder --max-files 5000
```

- Si la carpeta `_extracted/<nombre>/` ya tiene contenido, ese ZIP se omite (no sobrescribe).
- **ZIP corrupto o con contraseña:** el script imprime el error; el evaluador debe reflejarlo en la retroalimentación (sin nombre del estudiante en el párrafo de feedback).

---

## File Formats

### Users.md

Markdown table with user IDs and names. Created by `extract_users.py` or manually.

```markdown
| User ID | Nombre completo |
|---------|-----------------|
| 655 | ANA EJEMPLO SOLANO |
| 3081 | JUAN EJEMPLO MORA |
```

Also accepts plain user IDs (one per line).

### Moodle log CSV files

Exported from Moodle: course → Informes → Registros → Descargar como CSV.

Required columns:
- `Nombre completo del usuario` — student full name
- `Descripción` — contains `The user with id 'NNNN'`
- `Contexto del evento` — contains `Curso: COURSE_NAME` (used for auto-detection)

### rubric.json

Written at Paso 5 from the rubric the user approved; the single source of truth for graders and for `build_summary.py`. Row order must match the order of the criteria in Moodle (the upload is positional).

```json
{
  "curso": "CURSO DE EJEMPLO (00000)",
  "nota_aprobacion": 70,
  "items": [
    {"rubro": "Portada", "max": 3, "valores": [0, 3]},
    {"rubro": "Ejercicios de conversión", "max": 10}
  ]
}
```

- `valores` — the **only** scores allowed for that row. Omitted → integers `0..max`.
- `nota_aprobacion` — optional; enables the automatic «zona límite» flag.

### review_flags.json

Optional; keyed by User ID. `revisar` becomes a row in the «Casos que pueden requerir revisión del profesor» section; `estado` may override the status to `error_pdf_corrupto` or `error_ilegible`.

```json
{"3081": {"revisar": "Evidencia ambigua entre el PDF y el ZIP."},
 "7916": {"estado": "error_pdf_corrupto", "revisar": "PDF corrupto; se asignó 0."}}
```

### grading_summary.csv

Generated by `build_summary.py` after grading completes. **One row per student** (everyone in the batch, including skipped or error states). Headers in Spanish. Use as the master table for grades before optional Moodle import.

```csv
User ID,Nombre Estudiante,Nota,Nota Máxima,Estado
655,ANA EJEMPLO SOLANO,0,101,sin_entrega
3081,JUAN EJEMPLO MORA,84,101,calificado
3547,LUIS EJEMPLO CASTRO,0,101,omitido_sin_pdf
7916,MARIA EJEMPLO ROJAS,0,101,error_pdf_corrupto
```

Status values: `calificado`, `sin_entrega`, `omitido_sin_pdf`, `error_pdf_corrupto`, `error_ilegible`; plus `error_formato` (grade file failed validation) and `pendiente` (PDF not graded yet), which make `build_summary.py` exit non-zero.

### grading_resumen_global.md

Informe en español en `<download-dir>/assignments/assignment_<ASSIGNMENT_CMID>/grading_resumen_global.md` que incluye:

1. **Tabla o lista completa** de todos los estudiantes con nota y estado (equivalente legible al CSV).
2. **Sección «Casos que pueden requerir revisión del profesor»:** solo estudiantes donde la evaluación asistida puede ser menos fiable o conviene confirmación humana (notas límite, ambigüedad consigna–entrega, PDF escaneado difícil, problemas con ZIP/adjuntos, rubros muy subjetivos, etc.). Por cada uno: User ID, nombre, motivo breve. El profesor tiene la última palabra.

Este archivo complementa el CSV (retroalimentación agregada y alertas), similar en espíritu a exportaciones de calificaciones con columna de retroalimentación por estudiante (p. ej. exportación CSV del libro de calificaciones de Moodle).

### grade_<userid>.txt

One file per graded student, saved in `<download-dir>/assignments/assignment_<ASSIGNMENT_CMID>/user_<userid>/`. **Written entirely in Spanish.**

El **nombre del estudiante** aparece solo en la identificación inicial. El **párrafo de retroalimentación no debe incluir el nombre**.

```
Estudiante: JUAN EJEMPLO MORA

| Rubro | Puntaje |
|-------|---------|
| Portada | 3 / 3 |
| Índice | 3 / 3 |
| ... | ... |

Nota total: 84 / 101

Retroalimentación:
El trabajo presenta una estructura académica completa...
```

---

## Finding Moodle IDs

| ID | Where to find it |
|----|-----------------|
| Course ID | `course/view.php?id=8067` → `8067`, or run `--in-progress` |
| Assignment cmid | `mod/assign/view.php?id=517559` → `517559` |
| User ID | `&userid=3081` in grader URL, or from logs/Users.md |
| Group ID | `&group=38525` in filtered assignment URL |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| All users get the same file | Using `action=grader` (JS SPA) | Ensure `fetch_grader_page` uses `action=grade` |
| "No courses found" | Dynamic page loading | Script falls back to AJAX via `sesskey` automatically |
| `.env` parse error | Special chars in password | Wrap in double quotes: `MOODLE_PASSWORD="'pass"` |
| Wrong files downloaded | Intro PDF matched | `_is_submission_file_url` excludes `/intro/` paths |
| Script fails mid-batch | Network timeout | Re-run the same command; existing folders get overwritten |
| ZIP no extrae | Contraseña o corrupto | Revisar mensaje del script; reflejar en retroalimentación y rúbrica |
| Evidencia “no vista” | Solo se revisó el PDF | Revisar imágenes en la carpeta y `_extracted/` tras extraer ZIP |
| `build_summary.py` → `ERROR … no permitido` | Puntaje fuera de la escala de `rubric.json` | Corregir el `grade_<id>.txt` a un valor permitido (o corregir `rubric.json` si la escala aprobada era otra) |
| `build_summary.py` → `Nota total ≠ suma` | Suma mal calculada | Recalcular y reescribir la nota total |
| Los agentes calificadores no corren | Subagente no disponible | Calificar en serie siguiendo `.claude/agents/moodle-grader.md` |
