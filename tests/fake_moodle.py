#!/usr/bin/env python3
"""A tiny fake Moodle site for tests and demos. Every name and file here is made up.

Run it on its own to try the tool without a real account:
    python3 tests/fake_moodle.py
"""
import base64
import hashlib
import json
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "fake-token-0123456789"
SESSION = "fake-session-abc"
OLD = 1_500_000_000  # file timestamp far in the past


class FakeMoodle(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.site = f"http://127.0.0.1:{self.server_address[1]}"
        self.calls = []  # every wsfunction received, so tests can prove nothing writes
        self.downloads = []  # every file path requested
        self.broken = set()  # file paths that die halfway through
        self.modified = {}  # path -> timemodified
        self.extra_events = 0
        self.cookie_name = "MoodleSession"
        self.typeoflogin_blocks = False  # True acts like a school that refuses this sign-in route
        self.siteurl = None  # lets a test send back a different site address
        self.blocked = set()  # functions the school has switched off
        self.key_expired = False  # an expired key answers accessexception at first
        self.event_name = "Homework 3 is due"
        self.uploads = []  # (name, content) of every file received
        self.submission = {"status": "new", "files": []}
        self.canedit = True
        self.drafts_required = True
        self.save_warning = None
        self.statement_required = False
        self.online_text = ""
        self.upload_answer = None  # lets a test replace upload.php's reply
        self.files = {
            "/stats/week1-slides.pdf": b"%PDF-1.7 fake statistics slides, week 1",
            "/stats/week2-slides.pdf": b"%PDF-1.7 fake statistics slides, week 2",
            "/stats/syllabus.pdf": b"%PDF-1.7 fake syllabus posted in the forum",
            "/art/reading-list.pdf": b"%PDF-1.7 fake reading list",
        }
        self.contents = {
            101: [
                {"name": "Week 1", "modules": [self.resource("Slides", "week1-slides.pdf", "/stats/week1-slides.pdf")]},
                {"name": "Week 2", "modules": [self.resource("Slides", "week2-slides.pdf", "/stats/week2-slides.pdf")]},
                {"name": "General", "modules": [{"modname": "forum", "name": "Announcements", "instance": 7}]},
            ],
            102: [
                {"name": "Readings", "modules": [self.resource("List", "reading-list.pdf", "/art/reading-list.pdf")]},
            ],
        }
        self.forums = {7: [{"filename": "syllabus.pdf", "path": "/stats/syllabus.pdf"}]}

    def file_url(self, path):
        return f"{self.site}/webservice/pluginfile.php{path}?forcedownload=1"

    def file_entry(self, name, path, url=None):
        return {
            "type": "file",
            "filename": name,
            "fileurl": url or self.file_url(path),
            "filesize": len(self.files[path]),
            "timemodified": self.modified.get(path, OLD),
        }

    def resource(self, title, name, path):
        return {"modname": "resource", "name": title, "contents": [("file", name, path)]}

    def resolved_contents(self, course_id):
        """Build file entries at request time so tests can change files between syncs."""
        out = []
        for section in self.contents.get(course_id, []):
            modules = []
            for m in section["modules"]:
                m = dict(m)
                m["contents"] = [self.file_entry(*c[1:]) for c in m.get("contents", [])]
                modules.append(m)
            out.append({"name": section["name"], "modules": modules})
        return out

    def events(self):
        now = int(time.time())
        rows = [
            (now + 2 * 86400, 101, "STAT101", self.event_name),
            (now + 5 * 86400, 102, "ART205", "Museum essay is due"),
            (now + 9 * 86400, 101, "STAT101", "Midterm quiz closes"),
        ]
        rows += [(now + 86400 + i, 101, "STAT101", f"Extra task {i}") for i in range(self.extra_events)]
        return [
            {"id": i + 1, "name": n, "timesort": t, "course": {"id": c, "shortname": s}, "url": f"{self.site}/mod/assign/view.php?id={i}"}
            for i, (t, c, s, n) in enumerate(rows)
        ]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/admin/tool/mobile/launch.php":
            return self.launch(query)
        if url.path == "/login/index.php":
            return self.reply(200, b"login page", {"Set-Cookie": f"{self.server.cookie_name}=anonymous; path=/"})
        prefix = "/webservice/pluginfile.php"
        if url.path.startswith(prefix):
            return self.serve_file(url.path[len(prefix):], query)
        self.reply(404, b"not found")

    def launch(self, query):
        """Same contract as Moodle's admin/tool/mobile/launch.php."""
        if self.server.typeoflogin_blocks:
            return self.reply(200, b"<html>This site does not allow this sign-in</html>")
        if f"{self.server.cookie_name}={SESSION}" not in self.headers.get("Cookie", ""):
            return self.reply(303, headers={"Location": f"{self.server.site}/login/index.php"})
        site_id = hashlib.md5((self.server.site + query["passport"][0]).encode()).hexdigest()
        token = base64.b64encode(f"{site_id}:::{TOKEN}:::private".encode()).decode()
        self.reply(303, headers={"Location": f"{query.get('urlscheme', ['moodlemobile'])[0]}://token={token}"})

    def serve_file(self, path, query):
        self.server.downloads.append(path)
        if query.get("token") != [TOKEN] or path not in self.server.files:
            return self.reply(403, b"forbidden")
        body = self.server.files[path]
        if path in self.server.broken:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[: len(body) // 2])
            self.close_connection = True
            return None
        self.reply(200, body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.path == "/webservice/upload.php":
            return self.upload(raw)
        form = dict(urllib.parse.parse_qsl(raw.decode()))
        fn = form.get("wsfunction")
        self.server.calls.append(fn)
        denied = {"exception": "webservice_access_exception", "errorcode": "accessexception", "message": "Access control exception"}
        if form.get("wstoken") != TOKEN:
            out = {"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "Invalid token"}
        elif self.server.key_expired or fn in self.server.blocked:
            out = denied
        else:
            out = self.answer(fn, form)
        self.reply(200, json.dumps(out).encode(), {"Content-Type": "application/json"})

    def upload(self, raw):
        """Same contract as Moodle's webservice/upload.php."""
        if self.server.upload_answer is not None:
            return self.reply(200, self.server.upload_answer)
        if TOKEN.encode() not in raw:
            return self.reply(200, json.dumps({"error": "Invalid token", "errorcode": "invalidtoken"}).encode())
        found = re.search(rb'filename="([^"]*)"\r\nContent-Type: [^\r]*\r\n\r\n(.*?)\r\n--', raw, re.DOTALL)
        self.server.uploads.append((found.group(1).decode(), found.group(2)))
        self.reply(200, json.dumps([{"filename": found.group(1).decode(), "itemid": 555, "filearea": "draft"}]).encode())

    def answer(self, fn, form):
        srv = self.server
        courses = [
            {"id": 101, "fullname": "Introduction to Statistics", "shortname": "STAT101"},
            {"id": 102, "fullname": "Modern Art History", "shortname": "ART205"},
        ]
        if fn == "core_webservice_get_site_info":
            names = [
                "core_course_get_enrolled_courses_by_timeline_classification",
                "core_enrol_get_users_courses",
                "core_course_get_contents",
                "core_calendar_get_action_events_by_timesort",
                "gradereport_overview_get_course_grades",
                "mod_forum_get_forum_discussions",
                "mod_assign_get_assignments",
                "mod_assign_get_submission_status",
            ]
            return {
                "sitename": "Example University",
                "username": "demo.student",
                "userid": 42,
                "siteurl": srv.siteurl or srv.site,
                "functions": [{"name": n} for n in names],
            }
        if fn == "core_course_get_enrolled_courses_by_timeline_classification":
            return {"courses": courses, "nextoffset": 2}
        if fn == "core_enrol_get_users_courses":
            return courses
        if fn == "core_calendar_get_action_events_by_timesort":
            events = sorted(srv.events(), key=lambda e: e["timesort"])
            events = [e for e in events if e["timesort"] <= int(form["timesortto"])]
            after = int(form.get("aftereventid", 0))
            start = next((i + 1 for i, e in enumerate(events) if e["id"] == after), 0)
            page = events[start : start + int(form.get("limitnum", 50))]
            return {"events": page, "firstid": page[0]["id"] if page else None, "lastid": page[-1]["id"] if page else None}
        if fn == "core_course_get_contents":
            if int(form["courseid"]) not in srv.contents:
                return {"exception": "dml_missing_record_exception", "errorcode": "invalidrecord", "message": "Course not found"}
            return srv.resolved_contents(int(form["courseid"]))
        if fn == "mod_forum_get_forum_discussions":
            rows = srv.forums.get(int(form["forumid"]), [])
            return {"discussions": [{"attachments": [srv.file_entry(r["filename"], r["path"]) for r in rows]}]}
        if fn == "mod_assign_get_assignments":
            return {"courses": [{"id": 101, "shortname": "STAT101", "assignments": [
                {"id": 9, "cmid": 90, "name": "Homework 3", "duedate": int(time.time()) + 86400,
                 "requiresubmissionstatement": int(srv.statement_required)},
            ]}]}
        if fn == "mod_assign_get_submission_status":
            files = [{"filename": n} for n in srv.submission["files"]]
            attempt = {
                "submission": {"status": srv.submission["status"], "plugins": [
                    {"type": "file", "fileareas": [{"files": files}]},
                    {"type": "onlinetext", "editorfields": [{"name": "onlinetext", "text": srv.online_text}]},
                ]},
                "canedit": srv.canedit,
                "cansubmit": srv.drafts_required and bool(files) and srv.submission["status"] != "submitted",
            }
            return {"lastattempt": attempt}
        if fn == "mod_assign_save_submission":
            if srv.save_warning:
                return [{"item": "Homework 3", "message": srv.save_warning}]
            assert form["plugindata[files_filemanager]"] == "555"
            srv.submission = {"status": "draft", "files": [srv.uploads[-1][0]]}
            return []
        if fn == "mod_assign_submit_for_grading":
            if srv.statement_required and form["acceptsubmissionstatement"] != "1":
                return [{"item": "Homework 3", "message": "couldnotsubmitforgrading"}]
            srv.submission["status"] = "submitted"
            return []
        if fn == "gradereport_overview_get_course_grades":
            return {"grades": [{"courseid": 101, "grade": "88.00"}, {"courseid": 102, "grade": "-"}]}
        return {"exception": "moodle_exception", "errorcode": "invalidrecord", "message": f"Unknown function {fn}"}


if __name__ == "__main__":
    server = FakeMoodle(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"Fake Moodle running at {server.site}")
    print(f"Cookie value for login: {SESSION}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
