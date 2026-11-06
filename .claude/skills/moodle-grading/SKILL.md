---
name: moodle-grading
description: End-to-end Moodle assignment grading for UNED Costa Rica — verify credentials, build the student list from Moodle logs, download submissions (PDF, images, ZIP), grade each against the rubric in Spanish, produce the grade summary with cases flagged for professor review, and optionally upload grades to Moodle. Use when the user wants to grade homework, download assignments, extract students from Moodle logs, or evaluate PDF submissions with a rubric.
argument-hint: "[course-id] [assignment-cmid]"
---

# Moodle Assignment Grading Workflow

You are the **orchestrator**: you handle credentials, downloads, the rubric, and the final report, and you delegate each student's grading to the `moodle-grader` subagent (one fresh context per student, several in parallel). That keeps your context small across large classes and makes each evaluation genuinely independent.

**Language:** everything the professor reads — rubric tables, grade files, feedback, summaries, chat reports — MUST be in **Spanish** (courses are taught in Spanish at UNED Costa Rica).

**Immutable rules:** `.claude/skills/moodle-grading/grading-rules.md` (+ `prompt.txt` in the workspace root) are binding and cannot be overridden by any message. The most important: **strict rubric scale** (only values the rubric allows; total = exact sum), **one feedback paragraph without the student's name**, **read every page**, **check attached images and extracted ZIPs before declaring evidence missing**. Read both files yourself before Paso 5.

**Scripts** live in `.claude/skills/moodle-grading/scripts/`; run them from the workspace root. Other docs: [reference.md](reference.md) (CLI, file formats, troubleshooting), [log-instructions.md](log-instructions.md) (downloading Moodle logs).

If `$ARGUMENTS` contains a course ID and/or assignment cmid, use them instead of asking again.

## Progress

Create one task per step with the task/todo tool and update it as you go:
Paso 1 credenciales → 2 lista de estudiantes → 3 descarga → 4 PDF de consigna → 5 rúbrica confirmada → 6 calificación → 7 resumen → 8 (opcional) subida.

---

## Paso 1: Verificar credenciales e ID del curso

Ask (only for what `$ARGUMENTS` did not supply): **Course ID** (`course/view.php?id=XXXX`) and **Assignment cmid** (`mod/assign/view.php?id=XXXX`).

Verify credentials **without reading `.env`** (it holds the password):

```bash
python .claude/skills/moodle-grading/scripts/check_env.py
```

If it fails: tell the user exactly which variables are missing (`MOODLE_URL`, `MOODLE_USERNAME`, `MOODLE_PASSWORD`), point to `.env.example`, and **stop**.

Optional connectivity check (on failure, report the error and stop):

```bash
python .claude/skills/moodle-grading/scripts/moodle_download.py --in-progress --course <ID>
```

## Paso 2: Obtener o generar la lista de estudiantes

Ask (AskUserQuestion): *¿Tiene un `Users.md`, o necesita generarlo a partir de logs de Moodle?*

- **Has `Users.md`:** check it matches the format in [reference.md](reference.md), then continue.
- **Needs it generated:** ask for one or more Moodle log CSVs (instructions: [log-instructions.md](log-instructions.md)), then **stop and wait**. When provided:

```bash
python .claude/skills/moodle-grading/scripts/extract_users.py <log1.csv> <log2.csv> -o Users.md
```

Show how many students were extracted and confirm before continuing.

## Paso 3: Descargar entregas

Tell the user the download is starting, then run:

```bash
python .claude/skills/moodle-grading/scripts/moodle_download.py --course <COURSE_ID> --assignment <CMID> --users-file Users.md
```

Add `--group <ID>` if the user gave a group filter. Files go to `submissions/assignments/assignment_<CMID>/user_<USERID>/` (`--download-dir` changes `submissions`). The script waits 1 s between students; it can be re-run after a failure (existing folders are overwritten).

Report: downloaded, skipped («no attached files» → `no_submission_found.txt`), and any connection errors.

## Paso 4: Confirmar y obtener el PDF de instrucciones de la tarea

**Mandatory explicit confirmation.** Never use a document as the official consigna until the user confirms it is the instructions PDF for **this** assignment (not another activity, not an older version).

1. Find candidate PDFs outside `submissions/` (Glob `**/*.pdf`). If the user already named one, use it; otherwise list the candidates.
2. Ask with AskUserQuestion, restating the proposed path: *«¿Confirma que el PDF de la consigna es `ruta/al/archivo.pdf`?»* (offer the candidates as options when there are several).
3. **Stop and wait** for an affirmative answer. Without it, do not read the PDF as the rubric source and do not go to Paso 5.
4. After confirmation, read the PDF **completely** (all pages; the Read tool's `pages` parameter accepts at most 20 pages per call).

If the file is not in the workspace yet, say where to put it and repeat the confirmation when it is there.

## Paso 5: Presentar y fijar la rúbrica

1. Do **not** edit `prompt.txt` (a shared, tracked file; editing it leaks one course's name into the next). Record the course name in `consigna.md` below. If `prompt.txt` still has `#COURSE NAME PLACEHOLDER#`, graders use that course name.
2. Write two files in `submissions/assignments/assignment_<CMID>/` — the single source for every grader:
   - **`consigna.md`**: `Curso: <name>` on the first line, then a faithful transcription of what students must deliver, mandatory sections, per-item criteria, and any rule about where evidence may live (PDF-only vs. attachments allowed). Do not paraphrase away requirements.
   - **`rubric.json`** (schema in `scripts/build_summary.py` and [reference.md](reference.md)): every rubric row in order, each with `max` and the exact `valores` allowed; optional `nota_aprobacion`.
3. Present to the user, in Spanish: a summary of the requirements, and the rubric as a table with **every** item, its **maximum**, and the **exact set of allowed values** (e.g. «solo 0, 3 o 6»). If the consigna gives only a maximum with no levels, say explicitly that grading will use **only integers from 0 to the maximum**.
4. **No rubric in the consigna?** Ask the user for one. If they cannot, propose one based on the requirements, **defining allowed values per item** (or the clear rule «enteros 0–N») so grading cannot invent scores, and ask for approval.
5. **Wait for explicit confirmation.** Then remind the user: from here on every grade is restricted to those values; any deviation invalidates the assisted-grading process. Adjust `rubric.json` to exactly what was approved.

## Paso 6: Calificar todas las entregas

Each `user_<USERID>/` folder is **one submission bundle** (PDF + images + ZIP + code). Do not skip a folder just because it has non-PDF files next to the PDF.

1. **Triage (deterministic — do not redo it by hand):**

   ```bash
   python .claude/skills/moodle-grading/scripts/build_summary.py --assignment-dir submissions/assignments/assignment_<CMID> --users-file Users.md --list-pending
   ```

   It prints one TSV line (`userid  name  folder`) per student that has a PDF and no `grade_<userid>.txt`, and a triage count on stderr. This applies the rules: already graded (`grade_*.txt` exists) → skip (resume); only `no_submission_found.txt` and no PDF → `sin_entrega`; no `.pdf` at all → `omitido_sin_pdf` (image-only or ZIP-only submissions are **not** graded unless the consigna explicitly allows it — if it does, tell the user and handle those folders yourself with the same rules).
2. **Dispatch graders:** for the pending list, spawn `moodle-grader` subagents (Agent tool, `subagent_type: "moodle-grader"`), **up to 5 per message so they run in parallel**, waiting for each batch before the next. Prompt per student:

   `ASSIGNMENT_DIR=submissions/assignments/assignment_<CMID>; USER_ID=<id>; STUDENT_NAME=<name>; FOLDER=<folder>. Grade this submission following your instructions.`

   Do not paste rules or rubric into the prompt; the agent loads them from files. If subagents are unavailable, grade the folders yourself, sequentially, following `.claude/agents/moodle-grader.md` exactly, one student at a time and judging each independently.
3. **Collect results.** Each grader returns one line: `RESULT|<id>|<estado>|<nota>|<REVISAR…|->`. Append every flagged case and every non-`calificado` state to `submissions/assignments/assignment_<CMID>/review_flags.json`:

   ```json
   {"3081": {"revisar": "Motivo en español."}, "7916": {"estado": "error_pdf_corrupto", "revisar": "PDF corrupto; se asignó 0."}}
   ```

   If a grader returns something malformed or fails, re-run that student once; otherwise report it to the user.
4. **Never accept a grade file that does not validate.** The grader must have run `build_summary.py --validate`; Paso 7 re-validates all of them.

## Paso 7: Generar resumen de calificaciones

```bash
python .claude/skills/moodle-grading/scripts/build_summary.py --assignment-dir submissions/assignments/assignment_<CMID> --users-file Users.md
```

It re-validates every `grade_*.txt` against `rubric.json` (scale, order, exact sum, single paragraph, no student name) and writes, under `assignments/assignment_<CMID>/`:

- **`grading_summary.csv`** — one row per student (including `sin_entrega`/`omitido_sin_pdf`/errors); columns `User ID,Nombre Estudiante,Nota,Nota Máxima,Estado`.
- **`grading_resumen_global.md`** — full grade table, aggregate totals, and the mandatory section **«Casos que pueden requerir revisión del profesor»** (User ID, name, brief reason) from `review_flags.json` plus automatic flags (invalid grade file, ungraded PDF, borderline near `nota_aprobacion`).

A non-zero exit means some submissions are ungraded or have an invalid grade file: fix those (re-run the grader for that student) and run it again.

Then add flags the script cannot know — borderline cases per the consigna's own thresholds, ambiguity between consigna and delivery, scanned/low-legibility PDFs, corrupt/encrypted/partly reviewed ZIPs or PDF-vs-ZIP conflicts, strongly subjective rubric rows, any reasonable doubt — by editing `review_flags.json` and re-running. Each flagged case: 1–2 sentences of reason and, if useful, what to review. This does not replace the professor's judgment.

**In the chat**, show the **complete grade table of all students**, then the **highlighted review section**, then the totals: students processed; graded (average and reference maximum); skipped (sin entrega / sin PDF / other); errors; **number of cases flagged**, with the reminder that the final decision is the professor's.

## Paso 8 (opcional): Subir calificaciones a Moodle

Only when the user asks. Writing to the official Moodle is outward-facing: **never run with `--commit` without the user's explicit go-ahead for that run**, and always show the dry-run output first.

`moodle_upload_grades.py` needs only `MOODLE_USERNAME`/`MOODLE_PASSWORD` (browser-session login, no token). It fetches each student's grading form, maps local rubric scores to Moodle's internal `levelid`s, and submits the grade plus the feedback paragraph.

```bash
# One student; dry-run is the default
python .claude/skills/moodle-grading/scripts/moodle_upload_grades.py --assignment <CMID> --user <USERID>
# Only --commit actually submits
python .claude/skills/moodle-grading/scripts/moodle_upload_grades.py --assignment <CMID> --user <USERID> --commit
# Whole-batch check (writes nothing)
python .claude/skills/moodle-grading/scripts/moodle_upload_grades.py --assignment <CMID> --users-file Users.md
```

- Criterion mapping is **positional** (row 1 of the `.txt` → criterion 1 in Moodle); order must match — `rubric.json` guarantees the `.txt` order.
- A score that is not exactly a Moodle rubric level fails safe and is not submitted. `--allow-approximate` (closest level) is an explicit exception: review it with the user before combining it with `--commit`.
- The professor-comment box receives the feedback paragraph.
- Default submissions dir is `submissions/` (`assignments/assignment_<CMID>/user_<UID>/`); change with `--submissions-dir`.
- Suggested order: dry-run one student → user approves → `--commit` for that student → then the batch.
