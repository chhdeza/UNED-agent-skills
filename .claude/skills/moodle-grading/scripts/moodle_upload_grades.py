#!/usr/bin/env python3
"""
moodle_upload_grades.py

Uploads graded rubric and feedback from local grade_<userid>.txt files
back to Moodle using session-based authentication (no token required).

Workflow:
  1. Parse grade_<userid>.txt for rubric scores and feedback paragraph.
  2. Log in to Moodle with username/password.
  3. Fetch the assignment grading page HTML for the student.
  4. Retrieve the rubric definition via AJAX (gradingform_rubric_grader_gradingpanel_fetch).
     Falls back to HTML form parsing if AJAX is unavailable.
  5. Match grade-file items to rubric criteria positionally and require
     exact rubric-level scores by default.
  6. Build the grade form. POST only when --commit is explicitly provided.

Usage (run from workspace root):
    python .claude/skills/moodle-grading/scripts/moodle_upload_grades.py \\
        --assignment CMID --user USERID [--commit]

    python .claude/skills/moodle-grading/scripts/moodle_upload_grades.py \\
        --assignment CMID --users-file Users.md [--commit]

Environment (.env):
    MOODLE_URL       Base URL, e.g. https://aprende.uned.ac.cr
    MOODLE_USERNAME  Login username
    MOODLE_PASSWORD  Login password
"""

import argparse
import json
import os
import re
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

FORMAT_HTML = 1   # Moodle FORMAT_HTML


# ─── CLI ────────────────────────────────────────────────────────────────────

def get_config():
    p = argparse.ArgumentParser(
        description="Upload Moodle assignment grades from local grade_<userid>.txt files."
    )
    p.add_argument("--url", help="Moodle base URL (overrides MOODLE_URL)")
    p.add_argument("--username", help="Moodle username (overrides MOODLE_USERNAME)")
    p.add_argument(
        "--assignment", type=int, required=True, metavar="CMID",
        help="Assignment course-module id from mod/assign/view.php?id=CMID",
    )
    p.add_argument("--user", type=int, metavar="USERID", dest="userid",
                   help="Single student user ID.")
    p.add_argument("--users-file", metavar="PATH",
                   help="Path to Users.md for batch mode.")
    p.add_argument("--course", type=int, metavar="ID",
                   help="Course ID — for locating grade files under submissions/course_<ID>/.")
    p.add_argument("--submissions-dir", default="submissions", metavar="DIR",
                   help="Base submissions directory (default: submissions).")
    write_group = p.add_mutually_exclusive_group()
    write_group.add_argument(
        "--commit", action="store_true",
        help="Submit grades to Moodle. Without this flag the command is a dry run.",
    )
    write_group.add_argument(
        "--dry-run", dest="commit", action="store_false",
        help="Deprecated compatibility flag; dry-run is already the default.",
    )
    p.set_defaults(commit=False)
    p.add_argument(
        "--allow-approximate", action="store_true",
        help="Allow nearest rubric-level matching when no exact score exists.",
    )
    p.add_argument("--delay", type=float, default=2.0, metavar="SEC",
                   help="Seconds to wait between users (default: 2).")
    p.add_argument("--debug", action="store_true",
                   help="Save debug HTML files next to this script.")
    args = p.parse_args()

    url = (args.url or "").strip() or (os.environ.get("MOODLE_URL") or "").strip()
    username = (args.username or "").strip() or (os.environ.get("MOODLE_USERNAME") or "").strip()
    password = (os.environ.get("MOODLE_PASSWORD") or "").strip()
    return url, username, password, args


# ─── GRADE FILE PARSING ─────────────────────────────────────────────────────

def parse_grade_file(path: str) -> dict | None:
    """
    Parse a grade_<userid>.txt file.

    Returns dict with:
        student_name : str
        userid       : int | None
        rubric_items : list[{label: str, score: float}]
        total        : float | None
        feedback     : str  (plain text)

    Returns None if file is missing or has no parseable rubric items.
    """
    if not os.path.isfile(path):
        return None

    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()

    result: dict = {
        "student_name": "",
        "userid": None,
        "rubric_items": [],
        "total": None,
        "feedback": "",
    }

    m = re.search(
        r"^(?:Estudiante|Nombre(?:\s+del\s+estudiante)?)\s*:\s*(.+)$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        result["student_name"] = m.group(1).strip()

    m = re.search(r"User\s*ID[:\s]+(\d+)", text, re.IGNORECASE)
    if m:
        result["userid"] = int(m.group(1))

    # Markdown tables with either | Rubro | Puntaje | or | # | Rubro | Puntaje |.
    score_pattern = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(?:/\s*-?\d+(?:\.\d+)?)?$")
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        score_match = score_pattern.fullmatch(cells[-1])
        if not score_match:
            continue
        label = cells[-2]
        if not label or re.fullmatch(r"[-:]+", label):
            continue
        if re.match(r"^(rubro|criterio|ítem|item|puntaje)$", label, re.IGNORECASE):
            continue
        result["rubric_items"].append(
            {"label": label, "score": float(score_match.group(1))}
        )

    m = re.search(r"Nota\s+total[:\s]+([\d.]+)\s*/\s*[\d.]+", text, re.IGNORECASE)
    if m:
        result["total"] = float(m.group(1))

    # Feedback: everything after "Retroalimentación" section header
    m = re.search(
        r"(?:Retroalimentaci[oó]n|Feedback)[^\n]*\n+(.+)",
        text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        result["feedback"] = m.group(1).strip()

    return result if result["rubric_items"] else None


# ─── USER FILE PARSING ───────────────────────────────────────────────────────

def parse_users_file(path: str) -> list[tuple[int, str]]:
    """Parse Users.md → list of (userid, name)."""
    if not path or not os.path.isfile(path):
        return []
    users: list[tuple[int, str]] = []
    seen: set[int] = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "---" in line[:10]:
                continue
            if "|" in line:
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 2 and parts[0].isdigit():
                    uid = int(parts[0])
                    if uid not in seen:
                        seen.add(uid)
                        users.append((uid, parts[1] if len(parts) > 1 else ""))
    return users


# ─── MOODLE LOGIN ───────────────────────────────────────────────────────────

def moodle_login(
    base_url: str, username: str, password: str
) -> tuple[bool, str, requests.Session | None]:
    """Log in via form submission. Returns (success, message, session|None)."""
    base_url = base_url.rstrip("/")
    login_url = f"{base_url}/login/index.php"
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "MoodleGradeUploader/1.0"})
        resp = session.get(login_url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return False, f"Connection error: {e}", None

    token_m = re.search(r'name="logintoken"[^>]*value="([^"]+)"', resp.text)
    if not token_m:
        token_m = re.search(r'name="logintoken"\s+value="([^"]+)"', resp.text)
    if not token_m:
        return False, "Could not find login token. Check MOODLE_URL.", None

    payload = {
        "username": username,
        "password": password,
        "logintoken": token_m.group(1).strip(),
        "anchor": "",
        "rememberusername": 1,
    }
    try:
        resp = session.post(login_url, data=payload, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        return False, f"Login POST error: {e}", None

    if "/login/index.php" not in resp.url and "loginerrors" not in resp.text:
        return True, f"Logged in as {username}.", session
    if "loginerrors" in resp.text or "invalidlogin" in resp.text.lower():
        return False, "Invalid username or password.", None
    if 'id="login"' in resp.text and "loginerrors" not in resp.text:
        return True, f"Logged in as {username}.", session
    return False, "Login failed: unexpected response.", None


# ─── GRADING PAGE ───────────────────────────────────────────────────────────

def fetch_grade_page(
    session: requests.Session, base_url: str, cmid: int, userid: int
) -> str:
    """Fetch the grading-form HTML for one student."""
    base_url = base_url.rstrip("/")
    url = (
        f"{base_url}/mod/assign/view.php"
        f"?id={cmid}&action=grade&userid={userid}&rownum=0&useridlistid="
    )
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_sesskey(html: str) -> str:
    """Extract sesskey from page JS config or form hidden input."""
    for pat in [
        r'"sesskey"\s*:\s*"([^"]+)"',
        r"sesskey\s*=\s*['\"]([^'\"]+)['\"]",
        r'name="sesskey"[^>]*value="([^"]+)"',
        r'name="sesskey"\s+value="([^"]+)"',
    ]:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""


def extract_contextid(html: str) -> int | None:
    """Extract module context ID from M.cfg JS object in the page."""
    m = re.search(r'"contextid"\s*:\s*(\d+)', html)
    return int(m.group(1)) if m else None


# ─── RUBRIC DISCOVERY ───────────────────────────────────────────────────────

def fetch_rubric_via_ajax(
    session: requests.Session,
    base_url: str,
    sesskey: str,
    contextid: int,
    gradeduserid: int,
    debug: bool = False,
) -> list | None:
    """
    Fetch rubric definition via gradingform_rubric_grader_gradingpanel_fetch AJAX.
    Tries itemname "submissions" then "" as fallback.

    Returns list of:
        {'id': int, 'description': str, 'levels': [{'id': int|None, 'score': float}]}
    or None on failure.
    """
    base_url = base_url.rstrip("/")
    url = f"{base_url}/lib/ajax/service.php"
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    for itemname in ["submissions", ""]:
        payload = [
            {
                "index": 0,
                "methodname": "gradingform_rubric_grader_gradingpanel_fetch",
                "args": {
                    "component": "mod_assign",
                    "contextid": contextid,
                    "itemname": itemname,
                    "gradeduserid": gradeduserid,
                },
            }
        ]
        resp = None
        try:
            resp = session.post(
                url,
                params={"sesskey": sesskey},
                data=json.dumps(payload),
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            if debug:
                print(f"    AJAX fetch error (itemname={itemname!r}): {e}", file=sys.stderr)
            continue

        if not isinstance(data, list) or not data:
            continue
        first = data[0]
        if first.get("error"):
            if debug:
                print(f"    AJAX error (itemname={itemname!r}): {first.get('error')}", file=sys.stderr)
            continue

        grade_data = (first.get("data") or {}).get("grade", {})
        criteria_raw = grade_data.get("criteria", [])
        if not criteria_raw:
            continue

        result = []
        for crit in criteria_raw:
            crit_id = crit.get("id")
            desc = re.sub(r"<[^>]+>", "", crit.get("description") or "").strip()
            levels = []
            for lvl in crit.get("levels", []):
                lvl_id = lvl.get("id")   # may be None for the "not set" pseudo-level
                score_str = str(lvl.get("score") or "0").strip()
                try:
                    score = float(score_str)
                except ValueError:
                    score = 0.0
                # Include real levels only (id is not None and not 0 "not-set" placeholder)
                if lvl_id is not None and lvl_id != 0:
                    levels.append({"id": lvl_id, "score": score})
            if crit_id is not None and levels:
                result.append({"id": crit_id, "description": desc, "levels": levels})

        if result:
            return result

    return None


def parse_rubric_from_html_form(html: str) -> list | None:
    """
    Fallback: extract rubric structure from the grading form HTML.

    Returns list of {'id': int, 'levels': [{'id': int, 'score': float}]}
    ordered as they appear in the HTML, or None if no rubric found.

    Supports two level-score HTML patterns:
      1. Moodle 4.x: <span id="advancedgrading-criteria-CID-levels-LID-score" class="scorevalue">N</span>
      2. Legacy:     id="rubric-level-LID" ... class="score">N</td>
    """
    # ── Step 1: extract all (criterion_id, level_id) pairs from radio inputs ──
    radio_pattern = re.compile(
        r'<input[^>]*name="advancedgrading\[criteria\]\[(\d+)\]\[levelid\]"[^>]*value="(\d+)"'
        r'|<input[^>]*value="(\d+)"[^>]*name="advancedgrading\[criteria\]\[(\d+)\]\[levelid\]"',
        re.IGNORECASE | re.DOTALL,
    )

    criteria_order: list[int] = []
    criteria_map: dict[int, dict] = {}

    for m in radio_pattern.finditer(html):
        if m.group(1) and m.group(2):
            crit_id, lvl_id = int(m.group(1)), int(m.group(2))
        else:
            crit_id, lvl_id = int(m.group(4)), int(m.group(3))

        if crit_id not in criteria_map:
            criteria_map[crit_id] = {"id": crit_id, "levels": {}}
            criteria_order.append(crit_id)
        criteria_map[crit_id]["levels"][lvl_id] = None  # score unknown yet

    if not criteria_map:
        return None

    # ── Step 2: extract scores via Moodle 4.x scorevalue spans ──────────────
    # Pattern: <span id="advancedgrading-criteria-CID-levels-LID-score" class="scorevalue">SCORE</span>
    score_span_pattern = re.compile(
        r'<span\s[^>]*id="advancedgrading-criteria-(\d+)-levels-(\d+)-score"[^>]*'
        r'class="scorevalue"[^>]*>([^<]+)</span>'
        r'|<span\s[^>]*class="scorevalue"[^>]*'
        r'id="advancedgrading-criteria-(\d+)-levels-(\d+)-score"[^>]*>([^<]+)</span>',
        re.IGNORECASE,
    )
    for m in score_span_pattern.finditer(html):
        if m.group(1):
            crit_id, lvl_id, score_str = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        else:
            crit_id, lvl_id, score_str = int(m.group(4)), int(m.group(5)), m.group(6).strip()
        if crit_id in criteria_map and lvl_id in criteria_map[crit_id]["levels"]:
            try:
                criteria_map[crit_id]["levels"][lvl_id] = float(score_str)
            except ValueError:
                pass

    # ── Step 3: legacy fallback — id="rubric-level-LID" ... class="score">N ─
    for crit_id, crit_data in criteria_map.items():
        for lvl_id in list(crit_data["levels"].keys()):
            if crit_data["levels"][lvl_id] is not None:
                continue  # already found above
            block_m = re.search(
                rf'id=["\']rubric-level-{lvl_id}["\'][\s\S]{{0,1500}}?'
                r'class=["\'][^"\']*score[^"\']*["\'][^>]*>\s*([\d.]+)',
                html, re.IGNORECASE,
            )
            if block_m:
                try:
                    crit_data["levels"][lvl_id] = float(block_m.group(1))
                except ValueError:
                    pass

    # ── Step 4: build result list ────────────────────────────────────────────
    result = []
    for crit_id in criteria_order:
        crit_data = criteria_map[crit_id]
        levels = [
            {"id": lid, "score": score if score is not None else 0.0}
            for lid, score in crit_data["levels"].items()
        ]
        result.append({"id": crit_id, "levels": levels})

    return result if result else None


# ─── LEVEL MATCHING ─────────────────────────────────────────────────────────

def find_exact_level(
    levels: list[dict], target: float, tolerance: float = 1e-9
) -> dict | None:
    """Return the rubric level whose score exactly matches target."""
    return next(
        (level for level in levels if abs(level["score"] - target) <= tolerance),
        None,
    )


def find_closest_level(levels: list[dict], target: float) -> dict | None:
    """Return the closest rubric level for explicit override mode."""
    if not levels:
        return None
    return min(levels, key=lambda lvl: abs(lvl["score"] - target))


def match_items_to_levels(
    rubric_items: list[dict],
    rubric_def: list[dict],
    verbose: bool = True,
    allow_approximate: bool = False,
) -> dict[int, int]:
    """
    Match grade-file items (by position) to Moodle rubric criteria.
    Returns {criterion_id: level_id}.
    """
    n_items = len(rubric_items)
    n_crit = len(rubric_def)
    if n_items != n_crit:
        raise ValueError(
            f"grade file has {n_items} items but Moodle rubric has {n_crit} criteria"
        )

    choices: dict[int, int] = {}
    for i, crit in enumerate(rubric_def):
        crit_id = crit["id"]
        target = rubric_items[i]["score"]

        levels = crit.get("levels", [])
        lvl = find_exact_level(levels, target)
        approximate = False
        if lvl is None and allow_approximate:
            lvl = find_closest_level(levels, target)
            approximate = lvl is not None
        if lvl is None:
            available = ", ".join(str(level["score"]) for level in levels) or "none"
            raise ValueError(
                f"criterion {i + 1} ({crit_id}) has no exact level for score "
                f"{target}; available scores: {available}"
            )

        chosen_score = lvl["score"]
        if verbose:
            desc = (crit.get("description") or f"ID {crit_id}")[:45]
            approx = "  *** APPROXIMATE OVERRIDE ***" if approximate else ""
            print(
                f"    [{i+1:>2}] {desc:<45}  "
                f"score={target:<6}-> level {lvl['id']} (score={chosen_score}){approx}"
            )
        choices[crit_id] = lvl["id"]

    return choices


# ─── FORM DATA BUILDING ─────────────────────────────────────────────────────

def extract_form_fields(html: str) -> dict[str, str]:
    """Extract all hidden form input fields from the grading page."""
    fields: dict[str, str] = {}

    # Various attribute orderings for hidden inputs
    for pat in [
        r'<input[^>]+type=["\']hidden["\'][^>]+>',
        r'<input[^>]+name=["\'][^"\']+["\'][^>]+type=["\']hidden["\'][^>]*>',
    ]:
        for m in re.finditer(pat, html, re.IGNORECASE):
            tag = m.group(0)
            name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
            value_m = re.search(r'value=["\']([^"\']*)["\']', tag)
            if name_m:
                fields[name_m.group(1)] = value_m.group(1) if value_m else ""

    return fields


def build_post_data(
    base_fields: dict[str, str],
    cmid: int,
    userid: int,
    level_choices: dict[int, int],
    feedback_text: str,
) -> dict[str, str]:
    """
    Build the complete POST payload for submitting the grade form.
    Starts from all existing hidden fields and overrides only the
    rubric levels and feedback comment.
    """
    data = dict(base_fields)

    # Required form fields
    data["id"] = str(cmid)
    data["action"] = "submitgrade"
    data["savegrade"] = "1"
    data[f"_qf__mod_assign_grade_form_{userid}"] = "1"

    # Rubric level selections
    for crit_id, level_id in level_choices.items():
        data[f"advancedgrading[criteria][{crit_id}][levelid]"] = str(level_id)
        data[f"advancedgrading[criteria][{crit_id}][remark]"] = ""
        data[f"advancedgrading[criteria][{crit_id}][remarkformat]"] = "0"

    # Feedback comment (wrapped in a paragraph for HTML format)
    safe_feedback = feedback_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    data["assignfeedbackcomments_editor[text]"] = f"<p>{safe_feedback}</p>"
    data["assignfeedbackcomments_editor[format]"] = str(FORMAT_HTML)
    if "assignfeedbackcomments_editor[itemid]" not in data:
        data["assignfeedbackcomments_editor[itemid]"] = "0"

    return data


# ─── FORM SUBMISSION ────────────────────────────────────────────────────────

def submit_grade_form(
    session: requests.Session,
    base_url: str,
    cmid: int,
    post_data: dict[str, str],
    dry_run: bool,
    debug: bool = False,
) -> bool:
    """POST the grade form to Moodle. Returns True on success."""
    base_url = base_url.rstrip("/")
    url = f"{base_url}/mod/assign/view.php"

    if dry_run:
        print(f"  [DRY RUN] Would POST to: {url}")
        for key in sorted(post_data.keys()):
            if (
                "advancedgrading" in key
                or "feedback" in key
                or key in ("action", "id", "savegrade", "sesskey", "rownum", "attemptnumber")
            ):
                val = post_data[key]
                print(f"    {key} = {val[:90]!r}{'...' if len(val) > 90 else ''}")
        return True

    try:
        resp = session.post(url, data=post_data, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Error: {e}", file=sys.stderr)
        return False

    if debug:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        debug_path = os.path.join(script_dir, f"grade_submit_debug_{post_data.get('userid', 'user')}.html")
        with open(debug_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(resp.text)

    # Check for Moodle error indicators
    if "errorbox" in resp.text.lower() or (
        "/login/index.php" in resp.url and "/assign/" not in resp.url
    ):
        err_m = re.search(
            r'class="errorbox[^"]*"[\s\S]{0,800}?</div>', resp.text, re.IGNORECASE
        )
        if err_m:
            err_text = re.sub(r"<[^>]+>", " ", err_m.group(0)).strip()[:250]
            print(f"  Moodle error: {err_text}", file=sys.stderr)
        else:
            print("  Submission may have failed (unexpected response).", file=sys.stderr)
        return False

    return True


# ─── GRADE FILE LOCATOR ─────────────────────────────────────────────────────

def find_grade_file(
    submissions_dir: str, course_id: int | None, cmid: int, userid: int
) -> str | None:
    """Return the path to grade_<userid>.txt for this student, or None."""
    candidates = [
        # Current layout: submissions/assignments/assignment_CMID/user_UID/
        os.path.join(
            submissions_dir, "assignments",
            f"assignment_{cmid}", f"user_{userid}", f"grade_{userid}.txt",
        ),
        # Legacy layout: submissions/course_CID/assignment_CMID_user_UID/
        *(
            [os.path.join(
                submissions_dir, f"course_{course_id}",
                f"assignment_{cmid}_user_{userid}", f"grade_{userid}.txt",
            )]
            if course_id else []
        ),
        # Flat layout
        os.path.join(submissions_dir, f"grade_{userid}.txt"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# ─── MAIN ───────────────────────────────────────────────────────────────────

def main():
    url, username, password, args = get_config()

    if not url:
        print("Error: MOODLE_URL is required in .env or via --url.", file=sys.stderr)
        sys.exit(1)
    if not username or not password:
        print("Error: MOODLE_USERNAME and MOODLE_PASSWORD are required in .env.", file=sys.stderr)
        sys.exit(1)

    # Resolve user list
    user_ids: list[int] = []
    if args.userid is not None:
        user_ids = [args.userid]
    elif args.users_file:
        users = parse_users_file(args.users_file)
        user_ids = [uid for uid, _ in users]
        if not user_ids:
            print(f"Error: no user IDs found in {args.users_file}.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: provide --user USERID or --users-file PATH.", file=sys.stderr)
        sys.exit(1)

    dry = not args.commit
    label = " [DRY RUN]" if dry else ""
    print(f"Moodle grade uploader{label}")
    print(f"  URL:        {url}")
    print(f"  Assignment: {args.assignment}")
    print(f"  Users:      {user_ids}")
    print()

    # Login
    ok, msg, session = moodle_login(url, username, password)
    print(msg)
    if not ok or session is None:
        sys.exit(1)

    results = {"ok": 0, "skip": 0, "error": 0}

    for i, userid in enumerate(user_ids):
        print(f"\n{'-'*60}")
        print(f"[{i+1}/{len(user_ids)}] User {userid}")

        # Locate grade file
        grade_path = find_grade_file(args.submissions_dir, args.course, args.assignment, userid)
        if not grade_path:
            print("  No grade file found - skipping.")
            results["skip"] += 1
            continue

        # Parse grade file
        grade = parse_grade_file(grade_path)
        if not grade:
            print(f"  Could not parse {grade_path}.", file=sys.stderr)
            results["error"] += 1
            continue
        if grade["userid"] is not None and grade["userid"] != userid:
            print(
                f"  Grade file User ID {grade['userid']} does not match requested user {userid}.",
                file=sys.stderr,
            )
            results["error"] += 1
            continue

        print(f"  File:    {grade_path}")
        print(f"  Student: {grade['student_name']}")
        print(f"  Total:   {grade['total']}")
        print(f"  Items:   {len(grade['rubric_items'])}")
        print(f"  Feedback: {grade['feedback'][:80]}{'...' if len(grade['feedback']) > 80 else ''}")

        # Fetch grading page
        try:
            html = fetch_grade_page(session, url, args.assignment, userid)
        except requests.RequestException as e:
            print(f"  Error fetching grading page: {e}", file=sys.stderr)
            results["error"] += 1
            if i < len(user_ids) - 1:
                time.sleep(args.delay)
            continue

        if args.debug:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(script_dir, f"grade_page_{userid}.html"), "w", encoding="utf-8", errors="replace") as f:
                f.write(html)

        sesskey = extract_sesskey(html)
        contextid = extract_contextid(html)

        if not sesskey:
            print("  Warning: sesskey not found in page HTML.", file=sys.stderr)

        # Fetch rubric definition
        rubric_def = None
        if contextid and sesskey:
            print(f"  Fetching rubric via AJAX (contextid={contextid})...")
            rubric_def = fetch_rubric_via_ajax(
                session, url, sesskey, contextid, userid, debug=args.debug
            )

        if rubric_def:
            print(f"  Rubric:  {len(rubric_def)} criteria (AJAX)")
        else:
            print("  AJAX rubric fetch failed - parsing from HTML form...")
            rubric_def = parse_rubric_from_html_form(html)
            if rubric_def:
                print(f"  Rubric:  {len(rubric_def)} criteria (HTML fallback)")
            else:
                print("  Could not determine rubric structure - skipping.", file=sys.stderr)
                results["error"] += 1
                if i < len(user_ids) - 1:
                    time.sleep(args.delay)
                continue

        # Match grade items to rubric levels
        print("  Matching criteria:")
        try:
            level_choices = match_items_to_levels(
                grade["rubric_items"],
                rubric_def,
                allow_approximate=args.allow_approximate,
            )
        except ValueError as exc:
            print(f"  Rubric match failed: {exc}", file=sys.stderr)
            results["error"] += 1
            if i < len(user_ids) - 1:
                time.sleep(args.delay)
            continue
        if not level_choices:
            print("  No level choices resolved - skipping.", file=sys.stderr)
            results["error"] += 1
            if i < len(user_ids) - 1:
                time.sleep(args.delay)
            continue

        # Build POST data
        base_fields = extract_form_fields(html)
        post_data = build_post_data(
            base_fields, args.assignment, userid, level_choices, grade["feedback"]
        )

        # Submit
        ok_submit = submit_grade_form(
            session, url, args.assignment, post_data, dry, args.debug
        )
        if ok_submit:
            action = "DRY RUN OK" if dry else "SUBMITTED"
            print(f"  {action}: {len(level_choices)} criteria, total {grade['total']}")
            results["ok"] += 1
        else:
            print("  FAILED.", file=sys.stderr)
            results["error"] += 1

        if i < len(user_ids) - 1:
            time.sleep(args.delay)

    # Summary
    print(f"\n{'='*60}")
    print(f"Results:  OK={results['ok']}  Skip={results['skip']}  Error={results['error']}")
    if dry:
        print("(Dry run — nothing was submitted to Moodle)")
    if results["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
