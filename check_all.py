#!/usr/bin/env python3
"""
check_all.py — DREU coordinator script

Reads students.csv, fetches each student's GitHub repo or Google Doc, validates
all weekly logs due so far, prints a status summary, and (optionally) emails
students whose logs are missing or malformed.

Usage:
    python scripts/check_all.py                   # check and print report only
    python scripts/check_all.py --send-email      # check and email students with issues
    python scripts/check_all.py --week 4          # override current week (default: auto)

Supports two log types (auto-detected from the URL in students.csv):
    GitHub repo   — https://github.com/username/repo
    Google Doc    — https://docs.google.com/document/d/DOC_ID/...
                    (must be shared as "Anyone with the link can view")

Requires:
    pip install requests
"""

import argparse
import configparser
import csv
import re
import smtplib
import sys
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests

from check_log import validate_log, validate_readme_names

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE   = Path(__file__).parent / "config.ini"
STUDENTS_FILE = Path(__file__).parent.parent / "students.csv"
NUM_WEEKS     = 10
BRANCH_ORDER  = ["main", "master"]   # tried in order when fetching from GitHub


def load_config() -> configparser.ConfigParser:
    if not CONFIG_FILE.exists():
        print(f"Error: {CONFIG_FILE} not found. Copy config.ini.template and fill it in.")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg


# ── Week calculation ──────────────────────────────────────────────────────────

def current_week_number(start_date: date, today: date = None) -> int:
    """Return the current 1-based week number (capped at NUM_WEEKS)."""
    if today is None:
        today = date.today()
    delta = (today - start_date).days
    if delta < 0:
        return 0
    week = delta // 7 + 1
    return min(week, NUM_WEEKS)


# ── GitHub fetching ───────────────────────────────────────────────────────────

def raw_url(repo_url: str, filepath: str, branch: str) -> str:
    """Convert a GitHub repo URL to a raw content URL."""
    repo_url = repo_url.rstrip("/").removesuffix(".git")
    # https://github.com/user/repo  →  https://raw.githubusercontent.com/user/repo/branch/filepath
    parts = repo_url.replace("https://github.com/", "").split("/")
    if len(parts) < 2:
        return ""
    user, repo = parts[0], parts[1]
    return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{filepath}"


def fetch_file(repo_url: str, filepath: str, token: str = "") -> str | None:
    """
    Fetch a file from a GitHub repo. Returns content string or None if not found.
    Pass a GitHub personal access token for private repos.
    """
    headers = {"Authorization": f"token {token}"} if token else {}
    for branch in BRANCH_ORDER:
        url = raw_url(repo_url, filepath, branch)
        if not url:
            return None
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.text
    return None


# ── Google Docs fetching ─────────────────────────────────────────────────────

GDOC_ID_RE      = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")
WEEK_HEADING_RE = re.compile(r"^#\s+Week\s+(\d+)\s*$", re.MULTILINE)
NAME_FIELD_RE   = re.compile(r"^(Student|Mentor):\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def is_gdoc_url(url: str) -> bool:
    return "docs.google.com/document" in url


def is_github_url(url: str) -> bool:
    return "github.com/" in url


def _gdoc_export_url(doc_url: str, fmt: str) -> str:
    m = GDOC_ID_RE.search(doc_url)
    if not m:
        return ""
    return f"https://docs.google.com/document/d/{m.group(1)}/export?format={fmt}"


def _fetch_url(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True)
    except requests.RequestException:
        return None
    return resp.text if resp.status_code == 200 else None


def gdoc_html_to_text(html: str) -> str:
    """
    Convert a Google Docs HTML export to markdown-like plain text.

    Handles two student workflows:
      Heading-styled doc — h1/h2 tags become # / ## markers, so students can
                           use Google Docs heading styles for a formatted doc.
      Plain-text pasted  — literal # and ## inside <p> tags pass through
                           unchanged (backward-compatible with old template).

    Also normalises the Dates line whether the student typed 'Dates: …' or
    the old '**Dates:** …' form.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Error: beautifulsoup4 is required for Google Docs support.")
        print("  pip install beautifulsoup4")
        sys.exit(1)

    soup = BeautifulSoup(html, "html.parser")
    lines: list[str] = []

    # Section names that should be treated as ## headings even if not styled
    SECTION_NAMES = {"Goals", "Approach and Implementation", "Results", "Notes"}
    WEEK_P_RE     = re.compile(r"^Week\s+\d+$", re.IGNORECASE)

    for el in soup.find_all(["h1", "h2", "p", "li"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name == "h1":
            lines.append(f"# {text}")
        elif el.name == "h2":
            lines.append(f"## {text}")
        elif el.name == "li":
            lines.append(f"- {text}")
        else:  # p
            # Promote bare "Week N" paragraphs to # headings (student forgot Heading 1 style)
            if WEEK_P_RE.match(text):
                lines.append(f"# {text}")
            # Promote bare section-name paragraphs to ## headings (student forgot Heading 2 style)
            elif text in SECTION_NAMES:
                lines.append(f"## {text}")
            # Normalise any variant of the Dates line to **Dates:** MM-DD to MM-DD
            # Accepts both dash (06-08) and slash (06/08) separators
            elif re.search(r"Dates:\s*\d{2}[-/]\d{2}", text, re.IGNORECASE):
                date_part = re.sub(r"^.*Dates:\s*", "", text, flags=re.IGNORECASE).strip()
                # Normalise slashes to dashes: 06/08 → 06-08
                date_part = re.sub(r"(\d{2})/(\d{2})", r"\1-\2", date_part)
                lines.append(f"**Dates:** {date_part}")
            else:
                lines.append(text)

    return "\n".join(lines)


def fetch_gdoc(url: str) -> str | None:
    """
    Download a Google Doc as markdown-like plain text for validation.
    Fetches the HTML export (preserving heading structure) and converts it.
    The doc must be shared as 'Anyone with the link can view'.
    """
    html_url = _gdoc_export_url(url, "html")
    if not html_url:
        return None
    html = _fetch_url(html_url)
    if html is None:
        return None
    return gdoc_html_to_text(html)


def extract_gdoc_names(text: str) -> dict[str, str]:
    """
    Extract Student/Mentor name fields from the top of the document.
    Stops at the first '# Week N' heading.
    Returns e.g. {'Student': 'Alex Johnson', 'Mentor': 'Dr. Rivera'}.
    """
    names: dict[str, str] = {}
    for line in text.splitlines():
        if re.match(r"^#\s+Week\s+\d+", line):
            break
        m = NAME_FIELD_RE.match(line)
        if m:
            names[m.group(1).capitalize()] = m.group(2).strip()
    return names


def split_gdoc_by_week(content: str) -> dict[int, str]:
    """
    Split converted Google Doc text into per-week sections on '# Week N' headings.
    Returns {week_number: section_text} for every week found.
    """
    weeks: dict[int, str] = {}
    matches = list(WEEK_HEADING_RE.finditer(content))
    for i, m in enumerate(matches):
        week_num = int(m.group(1))
        start    = m.start()
        end      = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        weeks[week_num] = content[start:end].strip()
    return weeks


def check_student_gdoc(name: str, doc_url: str, weeks_due: int) -> list[dict]:
    """Validate all weeks in a student's Google Doc."""
    content = fetch_gdoc(doc_url)

    if content is None:
        error = (
            "Could not fetch Google Doc — confirm it is shared as "
            "'Anyone with the link can view'"
        )
        return [
            {"week": w,
             "status": STATUS_MISSING if w <= weeks_due else STATUS_FUTURE,
             "errors": [error] if w <= weeks_due else []}
            for w in range(1, NUM_WEEKS + 1)
        ]

    # Document-level: validate Student/Mentor name fields (reported on week 1)
    doc_names   = extract_gdoc_names(content)
    name_errors = [
        f"Missing '{field}:' field at the top of the document"
        for field in ("Student", "Mentor")
        if field not in doc_names or not doc_names[field].strip()
    ]

    week_sections = split_gdoc_by_week(content)

    # If the doc loaded but has no "# Week N" headings at all, it's the wrong
    # format entirely — mark all due weeks INVALID, not MISSING.
    if not week_sections:
        error = (
            "Document is accessible but not in the DREU research log format — "
            "no '# Week N' headings found. Please use the DREU template at "
            "https://github.com/cra-wp/dreu"
        )
        return [
            {"week": w,
             "status": STATUS_INVALID if w <= weeks_due else STATUS_FUTURE,
             "errors": [error] if w <= weeks_due else []}
            for w in range(1, NUM_WEEKS + 1)
        ]

    results: list[dict] = []

    for week in range(1, NUM_WEEKS + 1):
        if week > weeks_due:
            results.append({"week": week, "status": STATUS_FUTURE, "errors": []})
            continue

        week_errors: list[str] = []
        if week == 1:
            week_errors.extend(name_errors)   # surface name issues on week 1

        if week not in week_sections:
            week_errors.append(
                f"Week {week} section not found in Google Doc "
                f"(expected a '# Week {week}' heading)"
            )
            results.append({"week": week, "status": STATUS_MISSING, "errors": week_errors, "warnings": []})
        else:
            passed, errors, warnings = validate_log(week_sections[week], filename=f"Week {week}")
            week_errors.extend(errors)
            results.append({
                "week":     week,
                "status":   STATUS_OK if not week_errors else STATUS_INVALID,
                "errors":   week_errors,
                "warnings": warnings,
            })

    return results


# ── Generic URL fetching ─────────────────────────────────────────────────────

def check_student_generic_url(name: str, url: str, weeks_due: int) -> list[dict]:
    """
    Handle research logs hosted on arbitrary public websites (not GitHub or Google Docs).
    Fetches the page and attempts to parse it as HTML using the same converter as Google Docs.
    If the page is unreachable, all due weeks are MISSING.
    If the page is reachable but not in the expected format, all due weeks are INVALID.
    """
    html = _fetch_url(url)

    if html is None:
        error = (
            f"Could not fetch URL — confirm the page is publicly accessible: {url}"
        )
        return [
            {"week": w,
             "status": STATUS_MISSING if w <= weeks_due else STATUS_FUTURE,
             "errors": [error] if w <= weeks_due else []}
            for w in range(1, NUM_WEEKS + 1)
        ]

    # Try to parse it like a Google Doc HTML export
    try:
        content = gdoc_html_to_text(html)
    except Exception:
        content = ""

    week_sections = split_gdoc_by_week(content)

    if not week_sections:
        # Page loaded but has no recognisable "# Week N" structure
        error = (
            "Page is accessible but not in the expected DREU log format "
            "(no '# Week N' / 'Week N' headings found). "
            "Please use a GitHub repo or Google Doc in the DREU format."
        )
        return [
            {"week": w,
             "status": STATUS_INVALID if w <= weeks_due else STATUS_FUTURE,
             "errors": [error] if w <= weeks_due else []}
            for w in range(1, NUM_WEEKS + 1)
        ]

    # Page has week sections — validate each one
    results: list[dict] = []
    for week in range(1, NUM_WEEKS + 1):
        if week > weeks_due:
            results.append({"week": week, "status": STATUS_FUTURE, "errors": []})
            continue
        if week not in week_sections:
            results.append({"week": week, "status": STATUS_MISSING, "errors": [
                f"Week {week} section not found on page"
            ]})
        else:
            passed, errors, warnings = validate_log(week_sections[week], filename=f"Week {week}")
            results.append({
                "week":     week,
                "status":   STATUS_OK if passed else STATUS_INVALID,
                "errors":   errors,
                "warnings": warnings,
            })
    return results


# ── Per-student check ─────────────────────────────────────────────────────────

STATUS_OK      = "✓"
STATUS_MISSING = "MISSING"
STATUS_INVALID = "INVALID"
STATUS_FUTURE  = "—"       # week not yet due


def check_student(name: str, repo_url: str, weeks_due: int, token: str = "") -> list[dict]:
    """
    Check all logs for one student up to weeks_due.

    Returns a list of result dicts, one per week:
        { week, status, errors, warnings }

    Also checks README.md for unfilled Student/Mentor name placeholders and
    surfaces any warnings on week 1.
    """
    # Check README.md for placeholder names (warn on week 1)
    readme_warnings: list[str] = []
    readme_content = fetch_file(repo_url, "README.md", token=token)
    if readme_content is not None:
        readme_warnings = validate_readme_names(readme_content)

    results = []
    for week in range(1, NUM_WEEKS + 1):
        if week > weeks_due:
            results.append({"week": week, "status": STATUS_FUTURE, "errors": [], "warnings": []})
            continue

        filepath = f"logs/week-{week:02d}.md"
        content  = fetch_file(repo_url, filepath, token=token)

        if content is None:
            results.append({
                "week":     week,
                "status":   STATUS_MISSING,
                "errors":   [f"logs/week-{week:02d}.md not found in repo"],
                "warnings": readme_warnings if week == 1 else [],
            })
        else:
            passed, errors, warnings = validate_log(content, filename=filepath)
            if week == 1:
                warnings = readme_warnings + warnings
            results.append({
                "week":     week,
                "status":   STATUS_OK if passed else STATUS_INVALID,
                "errors":   errors,
                "warnings": warnings,
            })

    return results


# ── Report printing ───────────────────────────────────────────────────────────

def print_report(students_results: list[dict], weeks_due: int):
    """Print a summary table to stdout."""
    col_w = 22
    week_headers = "  ".join(f"W{w:02d}" for w in range(1, NUM_WEEKS + 1))
    print(f"\n{'=' * 80}")
    print(f"DREU Research Log Status Report — Weeks due: 1–{weeks_due}")
    print(f"{'=' * 80}")
    print(f"{'Name':<{col_w}}  {week_headers}")
    print(f"{'-' * col_w}  {'-' * (NUM_WEEKS * 5 - 2)}")

    for sr in students_results:
        row = f"{sr['name']:<{col_w}}  "
        for r in sr["results"]:
            s = r["status"]
            cell = {
                STATUS_OK:      " ✓ ",
                STATUS_MISSING: "MIS",
                STATUS_INVALID: "INV",
                STATUS_FUTURE:  " — ",
            }.get(s, " ? ")
            row += f"{cell}  "
        print(row)

    print(f"\nLegend: ✓=OK  MIS=missing  INV=invalid format  —=not yet due\n")

    # Detail for any errors or warnings
    any_issues = False
    for sr in students_results:
        has_errors   = any(r.get("errors")   for r in sr["results"])
        has_warnings = any(r.get("warnings") for r in sr["results"])
        if has_errors or has_warnings:
            if not any_issues:
                print("Issues:")
                any_issues = True
            print(f"\n  {sr['name']} ({sr['email']})")
            for r in sr["results"]:
                errs  = r.get("errors",   [])
                warns = r.get("warnings", [])
                if errs or warns:
                    print(f"    Week {r['week']}:")
                    for err  in errs:  print(f"      ✗ {err}")
                    for warn in warns: print(f"      ⚠ {warn}")

    if not any_issues:
        print("No issues found — all logs up to date.")


# ── Email notifications ───────────────────────────────────────────────────────

EMAIL_SUBJECT = "DREU Research Log — Action Required"

EMAIL_BODY = """\
Hi {name},

This is an automated reminder from the DREU program about your research log.

The following issues were found with your log at {repo_url}:

{issue_list}

Please address these as soon as possible. Each week's log is due by Sunday at 11:59 PM.

If you have any questions, contact the DREU program staff at dreu_staff@cra.org.

— DREU Program
"""


def send_email(to_name: str, to_email: str, repo_url: str, issues: list[tuple],
               cfg: configparser.ConfigParser):
    """Send a notification email to one student."""
    issue_lines = []
    for week, errors in issues:
        issue_lines.append(f"  Week {week}:")
        for err in errors:
            issue_lines.append(f"    • {err}")
    issue_list = "\n".join(issue_lines)

    body = EMAIL_BODY.format(
        name=to_name,
        repo_url=repo_url,
        issue_list=issue_list,
    )

    msg = EmailMessage()
    msg["Subject"] = EMAIL_SUBJECT
    msg["From"]    = cfg.get("smtp", "sender")
    msg["To"]      = to_email
    msg.set_content(body)

    host     = cfg.get("smtp", "host")
    port     = cfg.getint("smtp", "port")
    username = cfg.get("smtp", "username")
    password = cfg.get("smtp", "password")

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)

    print(f"  Email sent to {to_name} <{to_email}>")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Check all DREU student logs.")
    parser.add_argument("--send-email", action="store_true",
                        help="Email students with missing or invalid logs")
    parser.add_argument("--week", type=int, default=None,
                        help="Override current week number (default: calculated from start_date)")
    args = parser.parse_args()

    cfg = load_config()

    # Determine current week
    if args.week:
        weeks_due = args.week
    else:
        start_date = date.fromisoformat(cfg.get("program", "start_date"))
        weeks_due  = current_week_number(start_date)
        if weeks_due == 0:
            print("Program has not started yet.")
            sys.exit(0)

    print(f"Checking logs for weeks 1–{weeks_due} ...")

    github_token = cfg.get("github", "token", fallback="")

    # Read students
    if not STUDENTS_FILE.exists():
        print(f"Error: {STUDENTS_FILE} not found.")
        sys.exit(1)

    with open(STUDENTS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        students = list(reader)

    if not students:
        print("No students found in students.csv.")
        sys.exit(0)

    # Check each student
    students_results = []
    for s in students:
        name     = s["name"].strip()
        email    = s["email"].strip()
        doc_url  = s["repo_url"].strip()   # column holds either a GitHub or Google Docs URL
        if is_gdoc_url(doc_url):
            doc_type = "Google Doc"
        elif is_github_url(doc_url):
            doc_type = "GitHub"
        else:
            doc_type = "Website"
        print(f"  Checking {name} ({doc_type}) ...")

        if is_gdoc_url(doc_url):
            results = check_student_gdoc(name, doc_url, weeks_due)
        elif is_github_url(doc_url):
            results = check_student(name, doc_url, weeks_due, token=github_token)
        else:
            results = check_student_generic_url(name, doc_url, weeks_due)

        students_results.append({
            "name":     name,
            "email":    email,
            "repo_url": doc_url,
            "results":  results,
        })

    print_report(students_results, weeks_due)

    # Send emails
    if args.send_email:
        print("\nSending email notifications ...")
        for sr in students_results:
            issues = [(r["week"], r["errors"]) for r in sr["results"] if r["errors"]]
            if issues:
                send_email(sr["name"], sr["email"], sr["repo_url"], issues, cfg)
        print("Done.")


if __name__ == "__main__":
    main()
