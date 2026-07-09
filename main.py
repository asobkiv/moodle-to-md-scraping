"""
Moodle course files → a single structured Markdown file.

Downloads pdf/docx/pptx learning materials from a Moodle course entirely in
memory, converts each to Markdown, and assembles one course-level .md file
under output/ — ready to feed to Claude as context. No intermediate binaries
are written to disk, and there's no server: just a local script.

Usage:
    python main.py --list                  # list enrolled courses + ids
    python main.py --course-id 3261         # sync one course
    python main.py --course-id 3261,3508    # sync several courses

Nothing is synced without an explicit --course-id (or MOODLE_COURSE_IDS) —
this never bulk-syncs every enrolled course.

Env vars — see .env.example:
    MOODLE_BASE_URL    default https://teaching.kse.org.ua
    MOODLE_USERNAME
    MOODLE_PASSWORD
    MOODLE_COURSE_IDS  optional comma-separated default filter
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from converters import convert_to_markdown
from moodle_client import MoodleClient

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
ICONS = {"pdf": "📄", "docx": "📝", "pptx": "📊"}


def main():
    load_dotenv()
    args = parse_args()

    base_url = os.environ.get("MOODLE_BASE_URL") or "https://teaching.kse.org.ua"
    username = os.environ["MOODLE_USERNAME"]
    password = os.environ["MOODLE_PASSWORD"]

    client = MoodleClient(base_url, username, password)

    if args.list:
        courses = client.get_enrolled_courses()
        log.info(f"Enrolled in {len(courses)} course(s)")
        for c in courses:
            log.info(f"  {c['id']:>6}  {c['fullname']}")
        return

    course_ids = resolve_course_ids(args)
    if not course_ids:
        log.info("No --course-id given (and no MOODLE_COURSE_IDS set). "
                  "Run with --list to see enrolled courses, or pass --course-id <id>.")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)

    for cid in course_ids:
        try:
            course = client.get_course_by_id(cid)
        except Exception as e:
            log.info(f"Could not fetch course id={cid}: {e}")
            continue
        sync_course(client, course)


def resolve_course_ids(args):
    raw = args.course_id or os.environ.get("MOODLE_COURSE_IDS", "")
    return {int(x) for x in raw.split(",") if x.strip()} if raw.strip() else None


def sync_course(client, course):
    log.info(f"\n=== {course['fullname']} (id={course['id']}) ===")
    contents = client.get_course_contents(course["id"])
    files = client.extract_files(contents)
    log.info(f"Found {len(files)} file(s): pdf/docx/pptx")

    if not files:
        return

    sections = {}
    for f in files:
        sections.setdefault(f["section"], []).append(f)

    parts = [course_header(course, files)]
    for section_name, section_files in sections.items():
        parts.append(f"## {section_name}\n")
        for f in section_files:
            parts.append(render_file(client, f))

    out_path = OUTPUT_DIR / f"{safe_filename(course['fullname'])}.md"
    out_path.write_text("\n".join(parts), encoding="utf-8")
    log.info(f"→ {out_path}")


def render_file(client, f):
    icon = ICONS.get(f["ext"], "📎")
    log.info(f"  {icon} {f['filename']} ({human_size(f['filesize'])})")

    header = f"### {icon} {f['module_name']} (`{f['filename']}`)\n"
    meta = f"**Тип:** {f['ext'].upper()} · **Розмір:** {human_size(f['filesize'])} · [Джерело в Moodle]({f['moodle_url']})\n"

    try:
        data = client.download(f["moodle_url"])
        body = convert_to_markdown(data, f["ext"], f["filename"])
        body = shift_markdown_headings(body, shift=3)
    except Exception as e:
        log.info(f"    ! Не вдалося обробити: {e}")
        body = f"*Не вдалося обробити файл: {e}*\n"

    return f"{header}\n{meta}\n{body}\n---\n"


def course_header(course, files):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    by_ext = {}
    for f in files:
        by_ext[f["ext"]] = by_ext.get(f["ext"], 0) + 1
    stats = " · ".join(f"{ICONS[e]} {e.upper()}: {n}" for e, n in sorted(by_ext.items()))

    return (
        f"# {course['fullname']}\n\n"
        f"> Синхронізовано: {now}  \n"
        f"> Moodle Course ID: {course['id']}  \n"
        f"> Файлів: {len(files)} ({stats})\n"
    )


def shift_markdown_headings(text, shift):
    def repl(m):
        return "#" * min(len(m.group(1)) + shift, 6) + m.group(2)
    return re.sub(r"^(#{1,6})( .*)$", repl, text, flags=re.MULTILINE)


def safe_filename(name):
    name = re.sub(r"[^\w\s\-А-Яа-яІіЇїЄєҐґ]", "", name, flags=re.UNICODE)
    return re.sub(r"\s+", "_", name.strip())[:120]


def human_size(n):
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def parse_args():
    p = argparse.ArgumentParser(description="Moodle course files → single Markdown file")
    p.add_argument("--list", action="store_true", help="List enrolled courses and exit")
    p.add_argument("--course-id", help="Comma-separated Moodle course id(s) to sync")
    return p.parse_args()


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        log.info(f"Missing required env var: {e}. Copy .env.example to .env and fill it in.")
        sys.exit(1)
