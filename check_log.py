#!/usr/bin/env python3
"""
check_log.py — Validate a single DREU weekly research log file.

Usage (student-facing):
    python scripts/check_log.py logs/week-01.md

Exit codes:
    0 — log is valid (may still have warnings)
    1 — log has errors that must be fixed

Requires: no third-party packages
"""

import re
import sys
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

REQUIRED_SECTIONS = ["Goals", "Approach and Implementation", "Results"]

# Matches:  # Week 1  /  # Week 10  (leading # required)
WEEK_TITLE_RE  = re.compile(r'^#\s+Week\s+\d+\s*$', re.MULTILINE)

# Valid date line:  **Dates:** 06-02 to 06-06
DATES_LINE_RE  = re.compile(
    r'^\*\*Dates:\*\*\s+(\d{2}-\d{2})\s+to\s+(\d{2}-\d{2})\s*$',
    re.MULTILINE,
)

# Slash date line (common student mistake):  **Dates:** 06/02 to 06/06
DATES_SLASH_RE = re.compile(
    r'^\*\*Dates:\*\*\s+(\d{2}/\d{2})\s+to\s+(\d{2}/\d{2})\s*$',
    re.MULTILINE,
)

# Unfilled placeholder in dates
PLACEHOLDER_RE = re.compile(r'MM-DD|MM/DD')

# README name placeholders
NAME_PLACEHOLDERS = {"Your Full Name", "Mentor Full Name"}
README_NAME_RE    = re.compile(
    r'^\*\*(?P<field>Student|Mentor):\*\*\s+(?P<value>.+)',
    re.MULTILINE,
)


# ── Core validation ────────────────────────────────────────────────────────────

def validate_log(content: str, filename: str = "") -> tuple[bool, list[str], list[str]]:
    """
    Validate the content of a weekly log file.

    Returns (passed, errors, warnings).
      - errors  → the log fails; student must fix these
      - warnings → informational only; log still passes
    """
    errors   = []
    warnings = []

    # Skip files that still have the unfilled date placeholder
    if PLACEHOLDER_RE.search(content):
        return True, [], ["Log contains unfilled date placeholder (MM-DD) — skipping validation."]

    # 1. Week title
    if not WEEK_TITLE_RE.search(content):
        errors.append("Missing week title line (e.g. '# Week 1').")

    # 2. Dates line
    if DATES_LINE_RE.search(content):
        pass  # correct format
    elif DATES_SLASH_RE.search(content):
        warnings.append(
            "Dates use slashes (e.g. 06/08) — please use dashes instead (e.g. 06-08)."
        )
    else:
        errors.append(
            "Missing or invalid Dates line. "
            "Expected format: **Dates:** MM-DD to MM-DD  (e.g. **Dates:** 06-02 to 06-06)"
        )

    # 3. Required sections
    # Split on ## headings to find section blocks
    section_pattern = re.compile(r'^##\s+(.+)$', re.MULTILINE)
    found_sections  = {}
    for m in section_pattern.finditer(content):
        found_sections[m.group(1).strip()] = m.start()

    for section in REQUIRED_SECTIONS:
        if section not in found_sections:
            errors.append(f"Missing required section: '## {section}'")
        else:
            # Check the section has non-blank content after the heading
            start = found_sections[section]
            # Find end of section (next ## heading or end of file)
            next_heading = re.search(r'^##', content[start + 1:], re.MULTILINE)
            end = start + 1 + next_heading.start() if next_heading else len(content)
            # Skip the heading line itself
            body_start = content.index('\n', start) + 1
            body = content[body_start:end].strip()
            if not body:
                errors.append(f"Section '## {section}' is empty — please add content.")

    passed = len(errors) == 0
    return passed, errors, warnings


def validate_readme_names(content: str) -> list[str]:
    """
    Check README.md for unfilled Student/Mentor name placeholders.
    Returns a list of warning strings (empty if names look real).
    """
    warnings = []
    for m in README_NAME_RE.finditer(content):
        field = m.group("field")
        value = m.group("value").strip()
        if value in NAME_PLACEHOLDERS:
            warnings.append(
                f"README still has placeholder {field} name '{value}' — "
                f"please replace it with the real name."
            )
    return warnings


def validate_file(filepath) -> tuple[bool, list[str], list[str]]:
    """Read a file from disk and validate it."""
    path = Path(filepath)
    if not path.exists():
        return False, [f"File not found: {filepath}"], []
    content = path.read_text(encoding="utf-8")
    return validate_log(content, filename=path.name)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/check_log.py <log_file>")
        print("Example: python scripts/check_log.py logs/week-01.md")
        sys.exit(1)

    filepath = sys.argv[1]
    passed, errors, warnings = validate_file(filepath)

    if not errors and not warnings:
        print(f"✓  {filepath} — looks good!")
        sys.exit(0)

    print(f"{'✓' if passed else '✗'}  {filepath}")
    for err in errors:
        print(f"   ✗ {err}")
    for warn in warnings:
        print(f"   ⚠ {warn}")

    if not passed:
        print("\nPlease fix the errors above, then re-run this script.")
        print("Contact the DREU program staff <dreu_staff@cra.org> if you need help.")
        sys.exit(1)
    else:
        print("\nLog passes — warnings above are suggestions, not required fixes.")
        sys.exit(0)


if __name__ == "__main__":
    main()
