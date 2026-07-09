"""
Moodle API client — auth, course listing, and learning-file discovery.

Auth flow adapted from kse-notion-sync/moodle-to-notion/main.py: tries the
mobile token endpoint first, falls back to session-cookie + AJAX API login.
"""

import logging
import re
import time

import requests

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"pdf", "docx", "pptx"}

MOODLE_MAX_RETRIES = 3
MOODLE_RETRY_BACKOFF = 5  # seconds; doubles each retry


class MoodleClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.auth = self._authenticate(username, password)
        log.info(f"Auth method: {self.auth['type']}")

    # ── AUTH ─────────────────────────────────────────────────

    def _authenticate(self, username, password):
        try:
            resp = requests.post(
                f"{self.base_url}/login/token.php",
                data={"username": username, "password": password, "service": "moodle_mobile_app"},
                timeout=30,
            )
            data = resp.json()
            if "token" in data:
                log.info("Using Moodle mobile token API")
                return {"type": "token", "value": data["token"]}
            log.info(f"Mobile token not available ({data.get('error', 'unknown')}), falling back to session")
        except Exception as e:
            log.info(f"Token attempt failed: {e}")
        return self._session_login(username, password)

    def _session_login(self, username, password):
        session = requests.Session()
        r = session.get(f"{self.base_url}/login/index.php", timeout=30)
        m = re.search(r'name="logintoken"\s+value="([^"]+)"', r.text)
        logintoken = m.group(1) if m else ""

        session.post(
            f"{self.base_url}/login/index.php",
            data={"username": username, "password": password, "logintoken": logintoken},
            allow_redirects=True,
            timeout=30,
        )
        sesskey = self._extract_sesskey(session.get(f"{self.base_url}/my/", timeout=30).text)
        if not sesskey:
            raise RuntimeError("Could not extract sesskey — login may have failed (check credentials)")

        log.info("Using session cookie + AJAX API")
        return {"type": "session", "session": session, "sesskey": sesskey}

    @staticmethod
    def _extract_sesskey(html):
        m = re.search(r"""["']sesskey["']\s*[=:]\s*["']([^"']{10,})["']""", html)
        return m.group(1) if m else None

    # ── RAW API CALLS ────────────────────────────────────────

    def call(self, wsfunction, params):
        for attempt in range(1, MOODLE_MAX_RETRIES + 1):
            try:
                return self._call_once(wsfunction, params)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt == MOODLE_MAX_RETRIES:
                    raise
                wait = MOODLE_RETRY_BACKOFF * (2 ** (attempt - 1))
                log.info(f"  {wsfunction} failed ({e}), retrying in {wait}s (attempt {attempt}/{MOODLE_MAX_RETRIES})")
                time.sleep(wait)

    def _call_once(self, wsfunction, params):
        if self.auth["type"] == "token":
            resp = requests.post(
                f"{self.base_url}/webservice/rest/server.php",
                data={"wstoken": self.auth["value"], "wsfunction": wsfunction, "moodlewsrestformat": "json", **params},
                timeout=60,
            )
            data = resp.json()
            if isinstance(data, dict) and "errorcode" in data:
                raise RuntimeError(f"Moodle API error: {data.get('message')}")
            return data

        resp = self.auth["session"].post(
            f"{self.base_url}/lib/ajax/service.php?sesskey={self.auth['sesskey']}&info={wsfunction}",
            json=[{"index": 0, "methodname": wsfunction, "args": params}],
            timeout=60,
        )
        results = resp.json()
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"Unexpected AJAX response: {resp.text[:200]}")
        if results[0].get("error"):
            msg = (results[0].get("data") or {}).get("message") or str(results[0])[:200]
            raise RuntimeError(f"AJAX API error: {msg}")
        return results[0]["data"]

    # ── COURSES & CONTENTS ───────────────────────────────────

    def get_enrolled_courses(self):
        info = self.call("core_webservice_get_site_info", {})
        user_id = str(info["userid"])
        courses = self.call("core_enrol_get_users_courses", {"userid": user_id})
        if not isinstance(courses, list):
            raise RuntimeError("Unexpected courses response")
        return courses

    def get_course_contents(self, course_id):
        return self.call("core_course_get_contents", {"courseid": str(course_id)})

    def get_course_by_id(self, course_id):
        """Fetch a single course's metadata directly by id — no need to list
        every enrolled course first."""
        result = self.call("core_course_get_courses_by_field", {"field": "id", "value": str(course_id)})
        courses = result.get("courses") if isinstance(result, dict) else None
        if not courses:
            raise RuntimeError(f"Course id={course_id} not found or not accessible")
        return courses[0]

    # ── LEARNING FILES ───────────────────────────────────────

    def extract_files(self, sections):
        """Walk course sections/modules, returning files whose extension is
        pdf/docx/pptx from `resource` and `folder` modules."""
        files = []
        for section in sections:
            section_name = section.get("name") or "Загальне"
            for mod in section.get("modules", []):
                if mod.get("modname") not in ("resource", "folder"):
                    continue
                for f in mod.get("contents", []):
                    if f.get("type") != "file":
                        continue
                    filename = f.get("filename") or ""
                    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                    if ext not in ALLOWED_EXTENSIONS:
                        continue
                    files.append({
                        "section":     section_name,
                        "module_name": mod.get("name") or filename,
                        "filename":    filename,
                        "ext":         ext,
                        "filesize":    f.get("filesize", 0),
                        "moodle_url":  self._append_token(f.get("fileurl", "")),
                    })
        return files

    def _append_token(self, url):
        if not url or self.auth["type"] != "token":
            return url
        if "token=" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}token={self.auth['value']}"

    # ── DOWNLOAD ─────────────────────────────────────────────

    def download(self, url):
        """Fetch a Moodle file's bytes into memory. Never written to disk —
        callers convert straight from bytes."""
        requester = requests if self.auth["type"] == "token" else self.auth["session"]
        resp = requester.get(url, timeout=90)
        resp.raise_for_status()
        return resp.content
