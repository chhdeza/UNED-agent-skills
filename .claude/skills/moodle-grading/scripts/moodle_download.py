#!/usr/bin/env python3
"""
Moodle submission downloader and course browser.

Authenticates via session login (username/password) or Web Services token.
Can list courses, show course structure, and download student submission files
for a given assignment — single user or batch from a users file.
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


def get_config():
    parser = argparse.ArgumentParser(
        description="Moodle: download submissions, list courses, or view assignment status."
    )
    parser.add_argument("--url", help="Moodle base URL (overrides MOODLE_URL)")
    parser.add_argument("--username", help="Moodle username (overrides MOODLE_USERNAME)")
    parser.add_argument("--in-progress", action="store_true", dest="in_progress", help="Show only courses in progress, then course summary")
    parser.add_argument("--debug", action="store_true", help="Save debug HTML/log")
    parser.add_argument("--course", type=int, metavar="ID", help="Course id (e.g. 8067). Use with --assignment or --in-progress to scope to a specific course.")
    parser.add_argument("--assignment", type=int, metavar="ID", help="Assignment cm id (e.g. 517559). Use with --user or --users-file.")
    parser.add_argument("--user", type=int, metavar="ID", dest="userid", help="Student user id (e.g. 8310). Use with --assignment for a single user.")
    parser.add_argument("--users-file", metavar="PATH", help="Path to file with user ids (e.g. Users.md). Table format | id | name | or one id per line. Use with --assignment to download for all users.")
    parser.add_argument("--group", type=int, metavar="ID", help="Optional group id (e.g. 38525).")
    parser.add_argument("--download-dir", default="submissions", metavar="DIR", help="Directory to save downloaded submission files (default: submissions).")
    args = parser.parse_args()

    url = (args.url or "").strip() or (os.environ.get("MOODLE_URL") or "").strip()
    username = (args.username or "").strip() or (os.environ.get("MOODLE_USERNAME") or "").strip()
    password = (os.environ.get("MOODLE_PASSWORD") or "").strip()
    token = (os.environ.get("MOODLE_TOKEN") or "").strip()

    return url, username, password, args.in_progress, args.debug, args.course, args.assignment, args.userid, args.group, token, args.download_dir, args.users_file


def check_moodle_login(base_url: str, username: str, password: str) -> tuple[bool, str, requests.Session | None]:
    """Attempt web login; return (success, message, session or None)."""
    base_url = base_url.rstrip("/")
    login_url = f"{base_url}/login/index.php"

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "MoodleLoginCheck/1.0"})
        get_resp = session.get(login_url, timeout=15)
        get_resp.raise_for_status()
    except requests.RequestException as e:
        return False, f"Connection error: {e}", None

    # Extract CSRF logintoken from the login form
    token_match = re.search(r'name="logintoken"\s+value="([^"]+)"', get_resp.text)
    if not token_match:
        token_match = re.search(r'name="logintoken"[^>]*value="([^"]+)"', get_resp.text)
    if not token_match:
        return False, "Could not get login form; check MOODLE_URL.", None

    logintoken = token_match.group(1).strip()
    payload = {
        "username": username,
        "password": password,
        "logintoken": logintoken,
        "anchor": "",
        "rememberusername": 1,
    }

    try:
        post_resp = session.post(login_url, data=payload, timeout=15, allow_redirects=True)
        post_resp.raise_for_status()
    except requests.RequestException as e:
        return False, f"Connection error: {e}", None

    # Success: redirected away from login page, or no login error box
    if post_resp.url and "/login/index.php" not in post_resp.url:
        return True, f"Connection successful. Logged in as {username}.", session
    if "loginerrors" in post_resp.text or "invalidlogin" in post_resp.text.lower():
        return False, "Invalid username or password.", None
    # Some Moodle setups may keep same URL on success; check for error div
    if 'id="login"' in post_resp.text and "loginerrors" not in post_resp.text:
        return True, f"Connection successful. Logged in as {username}.", session
    return False, "Connection failed: login rejected or unexpected response.", None


def _fetch_courses_via_ajax(
    session: requests.Session,
    base_url: str,
    sesskey: str,
    debug: bool,
    in_progress_only: bool = False,
) -> list[tuple[int, str]]:
    """Fetch course list via Moodle's AJAX service (used when page loads courses via JS)."""
    base_url = base_url.rstrip("/")
    ajax_url = f"{base_url}/lib/ajax/service.php"
    classifications = ["inprogress"] if in_progress_only else ["inprogress", "future", "past"]
    payload = [
        {
            "index": i,
            "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
            "args": {
                "classification": classification,
                "limit": 0,
                "offset": 0,
                "sort": "fullname",
            },
        }
        for i, classification in enumerate(classifications)
    ]
    headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
    resp = None
    try:
        resp = session.post(
            ajax_url,
            params={"sesskey": sesskey},
            data=json.dumps(payload),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        if debug:
            _write_debug_log("ajax_request", {"error": str(e), "status": resp.status_code if resp else None})
        return []

    if debug:
        _write_debug_log("ajax_response", {"raw": str(data)[:2000]})

    if not isinstance(data, list):
        return []
    seen_ids: set[int] = set()
    result: list[tuple[int, str]] = []
    for item in data:
        if not isinstance(item, dict) or item.get("error"):
            if debug and item.get("error"):
                _write_debug_log("ajax_error", item)
            continue
        courses_data = item.get("data", {}).get("courses") if isinstance(item.get("data"), dict) else item.get("data")
        if not isinstance(courses_data, list):
            continue
        for c in courses_data:
            if isinstance(c, dict):
                cid = c.get("id") or c.get("courseid")
                name = c.get("fullname") or c.get("shortname") or ""
                if cid is not None and cid not in seen_ids and name:
                    seen_ids.add(int(cid))
                    result.append((int(cid), str(name).strip()))
    return result


def _write_debug_log(phase: str, info: dict) -> None:
    """Append a line to moodle_courses_debug.log for later analysis."""
    log_path = os.path.join(os.path.dirname(__file__) or ".", "moodle_courses_debug.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{phase}] {json.dumps(info, default=str)}\n")
    except OSError:
        pass


# --- Web Services API (token-based; no HTML parsing) ---


def moodle_rest(
    base_url: str, token: str, wsfunction: str, **params: str | int
) -> dict | list | None:
    """Call Moodle Web Services REST API. Returns JSON response or None on error."""
    base_url = base_url.rstrip("/")
    url = f"{base_url}/webservice/rest/server.php"
    payload = {
        "wstoken": token,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
        **{k: str(v) for k, v in params.items() if v is not None},
    }
    try:
        resp = requests.post(url, data=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        if isinstance(e, requests.RequestException) and hasattr(e, "response") and e.response is not None:
            try:
                err = e.response.json()
                if isinstance(err, dict) and "exception" in err:
                    raise SystemExit(f"Moodle API: {err.get('message', err)}") from e
            except (ValueError, KeyError):
                pass
        raise SystemExit(f"Moodle API request failed: {e}") from e
    if isinstance(data, dict) and "exception" in data:
        raise SystemExit(f"Moodle API: {data.get('message', data)}")
    return data


def _cmid_to_assign_instance(base_url: str, token: str, cmid: int) -> int | None:
    """Resolve course module id (view.php?id=) to assign instance id. Returns instance id or None."""
    out = moodle_rest(base_url, token, "core_course_get_course_module", cmid=cmid)
    if isinstance(out, dict) and "cm" in out:
        return out.get("cm", {}).get("instance")
    return None


def get_assignment_submission_status(
    base_url: str, token: str, assignment_cmid: int, user_id: int, group_id: int | None = None
) -> dict | None:
    """
    Get submission status for one user via mod_assign_get_submission_status.
    assignment_cmid is the course module id from the URL (mod/assign/view.php?id=517559 -> 517559).
    """
    assignid = _cmid_to_assign_instance(base_url, token, assignment_cmid)
    if assignid is None:
        assignid = assignment_cmid  # fallback: some sites use cmid as assignid
    params: dict = {"assignid": assignid, "userid": user_id}
    if group_id is not None:
        params["groupid"] = group_id
    out = moodle_rest(base_url, token, "mod_assign_get_submission_status", **params)
    return out if isinstance(out, dict) else None


def print_submission_files_and_status(base_url: str, token: str, data: dict, user_id: int) -> None:
    """From get_submission_status response, print status and list submitted files with download URLs."""
    last_attempt = (data or {}).get("lastattempt")
    if not last_attempt:
        print("No submission attempt found for this user.")
        return
    submission = last_attempt.get("submission") or {}
    status = submission.get("status") or "unknown"
    print(f"Submission status: {status}")
    grading = last_attempt.get("gradingstatus") or ""
    if grading:
        print(f"Grading status: {grading}")
    plugins = last_attempt.get("submissionplugins") or []
    printed_any = False
    for plug in plugins:
        file_areas = (plug.get("fileareas") or []) if isinstance(plug.get("fileareas"), list) else []
        for area in file_areas:
            files = area.get("files") or []
            for f in files:
                if not isinstance(f, dict):
                    continue
                printed_any = True
                filename = f.get("filename") or f.get("name") or "file"
                fileurl = f.get("fileurl") or f.get("url") or ""
                if fileurl and "token=" not in fileurl:
                    fileurl = f"{fileurl}{'&' if '?' in fileurl else '?'}token={token}"
                size = f.get("filesize", 0)
                print(f"  File: {filename} ({size} bytes)")
                if fileurl:
                    safe_url = re.sub(
                        r"([?&]token=)[^&]+", r"\1<redacted>", fileurl, flags=re.IGNORECASE
                    )
                    print(f"    Download: {safe_url}")
    if not printed_any:
        print("  (No file list in response; structure may differ. Check Moodle API or use debug.)")


# --- Session-based submission download (no token required) ---


def parse_users_file(path: str) -> list[int]:
    """Parse user IDs from a file. Supports markdown table (| id | name |) or plain numbers (one per line)."""
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    ids: list[int] = []
    seen: set[int] = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("|--") or "---" in line[:10]:
            continue
        if "|" in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if parts and parts[0].isdigit():
                uid = int(parts[0])
                if uid not in seen:
                    seen.add(uid)
                    ids.append(uid)
        else:
            for part in re.split(r"[\s,;]+", line):
                if part.isdigit():
                    uid = int(part)
                    if uid not in seen:
                        seen.add(uid)
                        ids.append(uid)
    return ids


def _get_file_list_for_user(
    session: requests.Session,
    url: str,
    assignment_id: int,
    user_id: int,
    group_id: int | None,
    debug: bool,
) -> list[tuple[str, str]]:
    """Fetch grade page and extract submission file links for one user. Returns list of (url, filename)."""
    try:
        html = fetch_grader_page(session, url, assignment_id, user_id, group_id)
    except requests.RequestException:
        return []
    if debug:
        debug_path = os.path.join(os.path.dirname(__file__) or ".", "grader_page_debug.html")
        with open(debug_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(html)
    file_list = extract_submission_file_links(html, url)
    return file_list


def fetch_grader_page(
    session: requests.Session, base_url: str, assign_cmid: int, user_id: int, group_id: int | None = None
) -> str:
    """Load the assignment grade page for one user (server-rendered). Returns HTML.

    Uses action=grade (traditional page) instead of action=grader (JS SPA)
    because the grader SPA ignores the userid parameter and may render a
    different user's submission data.
    """
    base_url = base_url.rstrip("/")
    url = f"{base_url}/mod/assign/view.php?id={assign_cmid}&action=grade&userid={user_id}&rownum=0&useridlistid="
    if group_id is not None:
        url += f"&group={group_id}"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


def _is_submission_file_url(url: str) -> bool:
    """
    Return True only for user submission files, not assignment intro/description files.
    Moodle: mod_assign/intro = assignment description (same for all users).
    mod_assign/submission or assignsubmission_file = user's submitted files.
    """
    url_lower = url.lower()
    if "/intro" in url_lower or "mod_assign" in url_lower and "intro" in url_lower:
        return False
    if "assignsubmission" in url_lower or ("mod_assign" in url_lower and "submission" in url_lower):
        return True
    return False


def extract_submission_file_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """
    Find submission file links on the grader page. Returns list of (absolute_url, suggested_filename).
    Excludes assignment intro/description files (same for all users).
    """
    base_url = base_url.rstrip("/")
    pattern = re.compile(
        r'href="(https?://[^"]*pluginfile\.php[^"]*)"[^>]*>([^<]*)</a>',
        re.IGNORECASE,
    )
    pattern2 = re.compile(
        r'href="([^"]*pluginfile\.php[^"]*)"[^>]*(?:title="([^"]*)"|download="([^"]*)")?',
        re.IGNORECASE,
    )
    seen_urls: set[str] = set()
    out: list[tuple[str, str]] = []
    def add(url_str: str, filename: str) -> None:
        nonlocal seen_urls, out
        if not url_str or url_str in seen_urls:
            return
        if not url_str.startswith("http"):
            url_str = base_url + ("/" if not url_str.startswith("/") else "") + url_str
        if not _is_submission_file_url(url_str):
            return
        seen_urls.add(url_str)
        filename = re.sub(r'[<>:"/\\|?*]', "_", (filename or "").strip())[:200]
        if not filename or filename == "file":
            filename = url_str.split("/")[-1].split("?")[0] or "download"
        out.append((url_str, filename))

    for m in pattern.finditer(html):
        url_str, link_text = m.group(1).strip(), (m.group(2) or "").strip()
        filename = link_text or url_str.split("/")[-1].split("?")[0] or "file"
        add(url_str, filename)
    for m in pattern2.finditer(html):
        url_str = m.group(1).strip()
        title = (m.group(2) or "").strip() if m.lastindex >= 2 else ""
        download = (m.group(3) or "").strip() if m.lastindex >= 3 else ""
        filename = download or title or url_str.split("/")[-1].split("?")[0] or "file"
        add(url_str, filename)
    return out


def download_submission_files(
    session: requests.Session, file_list: list[tuple[str, str]], out_dir: str
) -> list[str]:
    """Download each URL with the session and save to out_dir. Returns list of saved file paths."""
    os.makedirs(out_dir, exist_ok=True)
    saved: list[str] = []
    for i, (fileurl, suggested_name) in enumerate(file_list):
        try:
            r = session.get(fileurl, timeout=30, stream=True)
            r.raise_for_status()
            # Prefer filename from Content-Disposition
            filename = suggested_name
            cd = r.headers.get("Content-Disposition")
            if cd and "filename=" in cd:
                m = re.search(r'filename[*]?=(?:UTF-8\'\')?["\']?([^"\';]+)', cd, re.IGNORECASE)
                if m:
                    filename = m.group(1).strip().strip('"\'')
            filename = re.sub(r'[<>:"/\\|?*]', "_", filename)[:200]
            if not filename:
                filename = f"file_{i+1}"
            path = os.path.join(out_dir, filename)
            if os.path.exists(path):
                stem, suffix = os.path.splitext(filename)
                duplicate = 2
                while os.path.exists(path):
                    path = os.path.join(out_dir, f"{stem}_{duplicate}{suffix}")
                    duplicate += 1
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            saved.append(path)
        except (requests.RequestException, OSError) as e:
            print(f"  Skip {suggested_name}: {e}", file=sys.stderr)
    return saved


# Human-readable names for Moodle module types
MODULE_TYPE_NAMES = {
    "assign": "Assignment",
    "quiz": "Quiz",
    "resource": "Resource/File",
    "url": "URL",
    "page": "Page",
    "book": "Book",
    "folder": "Folder",
    "forum": "Forum",
    "glossary": "Glossary",
    "lesson": "Lesson",
    "scorm": "SCORM",
    "survey": "Survey",
    "wiki": "Wiki",
    "workshop": "Workshop",
    "chat": "Chat",
    "choice": "Choice",
    "database": "Database",
    "feedback": "Feedback",
    "h5pactivity": "H5P",
    "label": "Label",
    "lti": "External tool",
}


def _parse_course_contents_from_html(html: str) -> list[dict]:
    """Parse course view page HTML into sections and modules (same shape as API)."""
    sections_list: list[dict] = []
    # Section tabs: nav-link with section=N and title="Section name"
    sec_pattern = re.compile(
        r'href="[^"]*?section=(\d+)[^"]*"[^>]*title="([^"]*)"',
        re.IGNORECASE,
    )
    section_titles: dict[int, str] = {}
    for m in sec_pattern.finditer(html):
        idx, title = int(m.group(1)), (m.group(2) or "").strip()
        if idx not in section_titles or title:
            section_titles[idx] = title or f"Section {idx}"

    # Section blocks: li id="section-N" ... content until next section
    section_blocks = list(re.finditer(r'<li[^>]*id="section-(\d+)"[^>]*data-sectionid="(\d+)"', html, re.IGNORECASE))
    # Activity: <li ...> with id="module-XXX" and modtype_YYY (tag may span lines; class/id in any order)
    li_module_start = re.compile(
        r'<li\s[\s\S]*?class="[^"]*modtype_(\w+)[^"]*"[\s\S]*?id="module-\d+"[\s\S]*?>'
        r'|'
        r'<li\s[\s\S]*?id="module-\d+"[\s\S]*?class="[^"]*modtype_(\w+)[^"]*"[\s\S]*?>',
        re.IGNORECASE,
    )
    data_activityname = re.compile(r'data-activityname="([^"]+)"', re.IGNORECASE)

    for i, block in enumerate(section_blocks):
        sec_id = int(block.group(1))
        start = block.start()
        end = section_blocks[i + 1].start() if i + 1 < len(section_blocks) else len(html)
        segment = html[start:end]
        modules = []
        seen_names: set[str] = set()
        for m in li_module_start.finditer(segment):
            modname = m.group(1) or m.group(2)
            frag_start = m.start()
            frag_end = m.end()
            # Find the next </li> that closes this activity (first one after we see our content)
            next_li = segment.find("</li>", frag_end)
            if next_li == -1:
                continue
            frag = segment[frag_start : next_li + 5]
            name_match = data_activityname.search(frag)
            name = (name_match.group(1).strip() if name_match else "").strip()
            if not name:
                # Fallback: instancename text
                inst = re.search(r'class="instancename"[^>]*>([^<]+)', frag, re.IGNORECASE)
                name = (inst.group(1).strip() if inst else "").strip()
            if name and name not in seen_names:
                seen_names.add(name)
                modules.append({"modname": modname, "name": name})
        title = section_titles.get(sec_id, f"Section {sec_id}")
        sections_list.append({"id": sec_id, "name": title, "modules": modules})
    if not sections_list and section_titles:
        for idx in sorted(section_titles):
            sections_list.append({"id": idx, "name": section_titles[idx], "modules": []})
    return sections_list


def get_course_contents(
    session: requests.Session, base_url: str, course_id: int, sesskey: str
) -> list[dict] | None:
    """Fetch course structure (sections and modules). Tries AJAX first, then parses course view HTML."""
    base_url = base_url.rstrip("/")
    ajax_url = f"{base_url}/lib/ajax/service.php"
    payload = [
        {
            "index": 0,
            "methodname": "core_course_get_contents",
            "args": {"courseid": course_id},
        }
    ]
    headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
    try:
        resp = session.post(
            ajax_url,
            params={"sesskey": sesskey},
            data=json.dumps(payload),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        data = None
    if isinstance(data, list) and len(data) > 0 and not data[0].get("error"):
        sections = data[0].get("data")
        if isinstance(sections, list):
            return sections
    # Fallback: fetch course view page and parse HTML
    try:
        view_resp = session.get(f"{base_url}/course/view.php?id={course_id}", timeout=15)
        view_resp.raise_for_status()
        parsed = _parse_course_contents_from_html(view_resp.text)
        if parsed:
            return parsed
    except requests.RequestException:
        pass
    return None


def format_course_summary(sections: list[dict]) -> str:
    """Format sections and modules as a readable summary."""
    lines = []
    for sec in sections:
        name = (sec.get("name") or sec.get("summary") or "Section").strip()
        if not name and sec.get("section") == 0:
            name = "General"
        modules = sec.get("modules") or []
        if not name and not modules:
            continue
        sec_title = name or f"Section {sec.get('section', '?')}"
        lines.append(f"  {sec_title}")
        for mod in modules:
            modname = mod.get("modname") or "activity"
            type_label = MODULE_TYPE_NAMES.get(modname, modname.capitalize())
            mod_name = (mod.get("name") or "Untitled").strip()
            lines.append(f"    - [{type_label}] {mod_name}")
        if not modules:
            lines.append("    (no activities)")
    return "\n".join(lines) if lines else "  (no sections or content)"


def get_my_courses(
    session: requests.Session, base_url: str, debug: bool = False, in_progress_only: bool = False
) -> tuple[list[tuple[int, str]], str]:
    """Fetch My courses page; return (list of (course_id, course_name), sesskey). Prefer 'teaching' section if present."""
    base_url = base_url.rstrip("/")
    my_courses_url = f"{base_url}/my/courses.php"

    try:
        resp = session.get(my_courses_url, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException:
        return [], ""

    if debug:
        debug_path = os.path.join(os.path.dirname(__file__) or ".", "my_courses_debug.html")
        with open(debug_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(html)
        print(f"Debug: saved page to {debug_path}", file=sys.stderr)

    # Extract sesskey for AJAX fallback (courses loaded via JS on this site)
    sesskey_match = re.search(r'"sesskey"\s*:\s*"([^"]+)"', html)
    sesskey = sesskey_match.group(1) if sesskey_match else ""

    # All patterns that extract (course_id, course_name) - order matters for dedup
    patterns = [
        # data-fullname then href (Moodle 4.x cards)
        (re.compile(r'data-fullname="([^"]+)"[\s\S]*?/course/view\.php\?id=(\d+)', re.IGNORECASE), (2, 1)),
        (re.compile(r'/course/view\.php\?id=(\d+)[^"]*"[\s\S]*?data-fullname="([^"]+)"', re.IGNORECASE), (1, 2)),
        # href with direct link text (absolute or relative URL)
        (re.compile(r'href="[^"]*?course/view\.php\?id=(\d+)(?:&[^"]*)?["\'][^>]*>([^<]+)<', re.IGNORECASE), (1, 2)),
        (re.compile(r'href="[^"]*?course/view\.php\?id=(\d+)(?:&[^"]*)?["\'][^>]*>\s*([^<]+)\s*<', re.IGNORECASE | re.DOTALL), (1, 2)),
        # title="Course name" near the link
        (re.compile(r'title="([^"]{1,300})"[^>]*href="[^"]*?course/view\.php\?id=(\d+)', re.IGNORECASE), (2, 1)),
        (re.compile(r'href="[^"]*?course/view\.php\?id=(\d+)[^"]*"[^>]*title="([^"]+)"', re.IGNORECASE), (1, 2)),
        # id= then any text before </a> (nested tags)
        (re.compile(r'course/view\.php\?id=(\d+)"[^>]*>[\s\S]*?([^<\s][^<]{0,300}?)</a>', re.IGNORECASE), (1, 2)),
    ]

    seen_ids: set[int] = set()
    courses: list[tuple[int, str]] = []

    def add(course_id: int, name: str) -> None:
        name = (name or "").strip()
        name = re.sub(r"\s+", " ", name)
        if len(name) > 500:
            return
        if course_id in seen_ids:
            return
        seen_ids.add(course_id)
        if name and name != str(course_id):
            courses.append((course_id, name))

    for pat, (id_grp, name_grp) in patterns:
        for m in pat.finditer(html):
            try:
                cid = int(m.group(id_grp))
                name = m.group(name_grp)
                add(cid, name)
            except (IndexError, ValueError):
                continue

    # Fallback: if no courses found, collect any course IDs from the page (e.g. main content area only)
    if not courses:
        id_only = re.compile(r'course/view\.php\?id=(\d+)', re.IGNORECASE)
        for m in id_only.finditer(html):
            try:
                cid = int(m.group(1))
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    courses.append((cid, f"Course {cid}"))
            except (IndexError, ValueError):
                continue

    # Prefer courses under a "teaching" / "teacher" / "professor" section if present
    teaching_keywords = re.compile(
        r"teaching|teacher|professor|instructor|imparto|enseño|docente",
        re.IGNORECASE,
    )
    teaching_section_start = teaching_keywords.search(html)
    if teaching_section_start:
        start = teaching_section_start.start()
        segment = html[start : start + 15000]
        teaching_ids = set()
        for m in re.finditer(r'course/view\.php\?id=(\d+)', segment, re.IGNORECASE):
            teaching_ids.add(int(m.group(1)))
        if teaching_ids:
            courses = [c for c in courses if c[0] in teaching_ids]

    # Fallback: site loads courses via JavaScript (e.g. block_myoverview). Use AJAX API.
    if not courses and sesskey:
        ajax_courses = _fetch_courses_via_ajax(session, base_url, sesskey, debug, in_progress_only=in_progress_only)
        if ajax_courses:
            courses = ajax_courses
            if debug:
                _write_debug_log("source", {"used": "ajax_api"})
        elif debug:
            _write_debug_log("source", {"used": "html", "sesskey_found": True, "ajax_returned_empty": True})
    elif courses and in_progress_only and sesskey:
        # HTML had courses but user asked for in-progress only; re-fetch via AJAX with inprogress filter
        ajax_courses = _fetch_courses_via_ajax(session, base_url, sesskey, debug, in_progress_only=True)
        if ajax_courses:
            courses = ajax_courses

    return courses, sesskey


def main():
    url, username, password, in_progress_only, debug, course_id, assignment_id, user_id, group_id, token, download_dir, users_file = get_config()
    if not url:
        print("Error: MOODLE_URL is required. Set it in .env or use --url.", file=sys.stderr)
        sys.exit(1)

    # Resolve user list: single --user or --users-file
    user_ids: list[int] = []
    if user_id is not None:
        user_ids = [user_id]
    elif users_file:
        user_ids = parse_users_file(users_file)
        if not user_ids:
            print(f"Error: No user IDs found in {users_file}.", file=sys.stderr)
            sys.exit(1)

    # Assignment submission view: token (API) or session (login + download files)
    if assignment_id is not None and user_ids:
        if token and len(user_ids) == 1:
            parts = [f"course {course_id}" if course_id else "", f"assignment (cmid) {assignment_id}", f"user {user_ids[0]}"]
            if group_id:
                parts.append(f"group {group_id}")
            print(", ".join(p for p in parts if p))
            data = get_assignment_submission_status(url, token, assignment_id, user_ids[0], group_id)
            if data:
                print_submission_files_and_status(url, token, data, user_ids[0])
            else:
                print("No submission data returned.")
            sys.exit(0)
        # Session-based: log in and download for each user
        if not username or not password:
            print("Error: For --assignment without MOODLE_TOKEN, set MOODLE_USERNAME and MOODLE_PASSWORD in .env.", file=sys.stderr)
            sys.exit(1)
        ok, msg, session = check_moodle_login(url, username, password)
        print(msg)
        if not ok or session is None:
            sys.exit(1)
        print(f"Processing {len(user_ids)} user(s) for assignment {assignment_id} (1s delay between users)...")
        for i, uid in enumerate(user_ids):
            file_list = _get_file_list_for_user(session, url, assignment_id, uid, group_id, debug)
            # <download-dir>/assignments/assignment_<cmid>/user_<userid>/ — one folder per student per assignment.
            out_dir = os.path.join(
                download_dir, "assignments", f"assignment_{assignment_id}", f"user_{uid}"
            )
            if not file_list:
                os.makedirs(out_dir, exist_ok=True)
                placeholder = os.path.join(out_dir, "no_submission_found.txt")
                with open(placeholder, "w", encoding="utf-8") as f:
                    f.write(f"No submission file found for user {uid}.\n")
                print(f"  [{i+1}/{len(user_ids)}] User {uid}: skipped (no attached files) - wrote {placeholder}")
                if i < len(user_ids) - 1:
                    time.sleep(1)
                continue
            saved = download_submission_files(session, file_list, out_dir)
            if saved:
                print(f"  [{i+1}/{len(user_ids)}] User {uid}: downloaded {len(saved)} file(s) to {out_dir}")
            else:
                print(f"  [{i+1}/{len(user_ids)}] User {uid}: no files could be downloaded")
            if i < len(user_ids) - 1:
                time.sleep(1)
        sys.exit(0)

    if not username:
        print("Error: MOODLE_USERNAME is required. Set it in .env or use --username.", file=sys.stderr)
        sys.exit(1)
    if not password:
        print("Error: MOODLE_PASSWORD is required. Set it in .env.", file=sys.stderr)
        sys.exit(1)

    ok, msg, session = check_moodle_login(url, username, password)
    print(msg)
    if not ok or session is None:
        sys.exit(1)

    courses, sesskey = get_my_courses(session, url, debug=debug, in_progress_only=in_progress_only)
    if not courses:
        print("No courses found on My courses page (or page structure not recognized).")
        sys.exit(0)
    if course_id is not None:
        courses = [c for c in courses if c[0] == course_id]
        if not courses:
            print(f"No matching course for id {course_id}.", file=sys.stderr)
            sys.exit(1)

    label = "In progress courses" if in_progress_only else "Active courses (from My courses)"
    print(f"\n{label}:")
    for cid, name in sorted(courses, key=lambda c: c[1].lower()):
        print(f"  [{cid}] {name}")

    if in_progress_only and courses and sesskey:
        print("\n--- Course options summary ---")
        for cid, name in sorted(courses, key=lambda c: c[1].lower()):
            print(f"\n{name} (id {cid}):")
            sections = get_course_contents(session, url, cid, sesskey)
            if sections is None:
                print("  (Could not load course structure.)")
            else:
                print(format_course_summary(sections))

    sys.exit(0)


if __name__ == "__main__":
    main()
