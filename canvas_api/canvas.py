"""Typed helpers over the Canvas REST API, plus HTML -> text and quiz-review parsing.

Everything here returns compact, agent-friendly dicts: absolute URLs, plain-text bodies,
and the ids needed to call the next endpoint.
"""

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString

from .browser import CanvasBrowser

BLOCK_TAGS = ("p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
              "table", "section", "article", "blockquote", "pre", "hr")


class CanvasError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"Canvas returned {status}: {detail}")
        self.status = status
        self.detail = detail


class SessionExpired(CanvasError):
    def __init__(self):
        super().__init__(401, "The Canvas session is not logged in")


class Canvas:
    def __init__(self, browser: CanvasBrowser):
        self.browser = browser
        self.base_url = browser.settings.base_url
        self._courses: tuple[float, dict[int, dict]] | None = None

    # ---------------------------------------------------------------- transport

    async def request(self, method: str, path: str, params: list[tuple[str, str]] | None = None,
                      body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        result = await self.browser.fetch(method, path, params=params, body=data, headers=headers)
        if result.status == 401 and b"unauthenticated" in result.body:
            raise SessionExpired()
        try:
            payload = result.json()
        except ValueError:
            payload = result.body.decode(errors="replace")[:500]
        if result.status >= 400:
            raise CanvasError(result.status, payload)
        return payload

    async def get(self, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        return await self.request("GET", path, params)

    async def get_all(self, path: str, params: list[tuple[str, str]] | None = None,
                      max_pages: int = 50) -> list:
        params = list(params or [])
        if not any(k == "per_page" for k, _ in params):
            params.append(("per_page", "100"))
        result = await self.browser.fetch("GET", path, params=params)
        items: list = []
        for _ in range(max_pages):
            if result.status == 401 and b"unauthenticated" in result.body:
                raise SessionExpired()
            if result.status >= 400:
                raise CanvasError(result.status, result.body.decode(errors="replace")[:500])
            page = result.json()
            if not isinstance(page, list):
                return page
            items.extend(page)
            next_url = _next_link(result.headers.get("link", ""))
            if not next_url:
                break
            result = await self.browser.fetch("GET", next_url)
        return items

    async def web(self, path: str) -> str:
        result = await self.browser.fetch("GET", path, headers={"Accept": "text/html"}, xhr=False)
        if result.status >= 400:
            raise CanvasError(result.status, "page not available")
        if not result.url.startswith(self.base_url) or "/login" in result.url:
            raise SessionExpired()
        return result.body.decode(errors="replace")

    def absolute(self, url: str | None) -> str | None:
        return urljoin(self.base_url + "/", url) if url else None

    # ------------------------------------------------------------------ courses

    async def courses(self, include_all: bool = False) -> list[dict]:
        params = [("include[]", "term"), ("include[]", "total_scores")]
        if not include_all:
            params.append(("enrollment_state", "active"))
        raw = await self.get_all("/api/v1/courses", params)
        out = [_course(c) for c in raw if c.get("name")]
        if not include_all:
            self._courses = (time.monotonic(), {c["id"]: c for c in out})
        return out

    async def course_names(self) -> dict[int, str]:
        if self._courses is None or time.monotonic() - self._courses[0] > 600:
            await self.courses()
        return {cid: c["name"] for cid, c in self._courses[1].items()}

    async def active_course_ids(self) -> list[int]:
        return list((await self.course_names()).keys())

    # ------------------------------------------------------------- assignments

    async def assignments(self, course_id: int, bucket: str | None = None) -> list[dict]:
        params = [("include[]", "submission"), ("order_by", "due_at")]
        if bucket:
            params.append(("bucket", bucket))
        raw = await self.get_all(f"/api/v1/courses/{course_id}/assignments", params)
        return [self.assignment_summary(a) for a in raw]

    def assignment_summary(self, a: dict) -> dict:
        sub = a.get("submission") or {}
        kind = ("new_quiz" if a.get("is_quiz_lti_assignment") else
                "quiz" if a.get("quiz_id") else
                "discussion" if a.get("discussion_topic") else
                "external_tool" if "external_tool" in (a.get("submission_types") or []) else
                "assignment")
        return {
            "id": a["id"],
            "course_id": a.get("course_id"),
            "name": a.get("name"),
            "kind": kind,
            "quiz_id": a.get("quiz_id"),
            "due_at": a.get("due_at"),
            "lock_at": a.get("lock_at"),
            "points_possible": a.get("points_possible"),
            "submission_types": a.get("submission_types"),
            "status": _submission_status(sub),
            "score": sub.get("score"),
            "grade": sub.get("grade"),
            "submitted_at": sub.get("submitted_at"),
            "late": sub.get("late"),
            "missing": sub.get("missing"),
            "attempt": sub.get("attempt"),
            "locked": a.get("locked_for_user"),
            "html_url": a.get("html_url"),
        }

    async def assignment_detail(self, course_id: int, assignment_id: int) -> dict:
        a, sub = await asyncio.gather(
            self.get(f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
                     [("include[]", "submission"), ("include[]", "score_statistics")]),
            self.get(f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self",
                     [("include[]", "submission_comments"), ("include[]", "rubric_assessment"),
                      ("include[]", "submission_history")]),
        )
        out = self.assignment_summary(a)
        out.update({
            "description": html_to_text(a.get("description"), self.base_url),
            "unlock_at": a.get("unlock_at"),
            "lock_explanation": a.get("lock_explanation"),
            "allowed_extensions": a.get("allowed_extensions"),
            "allowed_attempts": a.get("allowed_attempts"),
            "grading_type": a.get("grading_type"),
            "rubric": [
                {"id": r.get("id"), "description": r.get("description"), "points": r.get("points"),
                 "long_description": r.get("long_description"),
                 "ratings": [{"description": x.get("description"), "points": x.get("points")}
                             for x in r.get("ratings", [])]}
                for r in a.get("rubric") or []
            ],
            "score_statistics": a.get("score_statistics"),
            "submission": self.submission_detail(sub),
        })
        return out

    def submission_detail(self, s: dict) -> dict:
        return {
            "status": _submission_status(s),
            "submission_type": s.get("submission_type"),
            "attempt": s.get("attempt"),
            "submitted_at": s.get("submitted_at"),
            "score": s.get("score"),
            "grade": s.get("grade"),
            "late": s.get("late"),
            "missing": s.get("missing"),
            "excused": s.get("excused"),
            "body": html_to_text(s.get("body"), self.base_url) or None,
            "url": s.get("url"),
            "attachments": [_file(f) for f in s.get("attachments") or []],
            "comments": [
                {"author": c.get("author_name"), "created_at": c.get("created_at"), "text": c.get("comment"),
                 "attachments": [_file(f) for f in c.get("attachments") or []]}
                for c in s.get("submission_comments") or []
            ],
            "rubric_assessment": s.get("rubric_assessment"),
            "history": [
                {"attempt": h.get("attempt"), "submitted_at": h.get("submitted_at"),
                 "submission_type": h.get("submission_type"), "score": h.get("score")}
                for h in s.get("submission_history") or [] if h.get("submitted_at")
            ],
            "preview_url": s.get("preview_url"),
        }

    # -------------------------------------------------------------------- todo

    async def todo(self, days: int, include_done: bool) -> dict:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        names, planner, missing = await asyncio.gather(
            self.course_names(),
            self.get_all("/api/v1/planner/items", [("start_date", start), ("end_date", end)]),
            self.get_all("/api/v1/users/self/missing_submissions",
                         [("include[]", "planner_overrides"), ("filter[]", "submittable")]),
        )
        items, announcements = [], []
        for p in planner:
            item = self.planner_item(p, names)
            if item["type"] == "announcement":
                announcements.append(item)
            elif include_done or not item["done"]:
                items.append(item)
        missing_items = [
            {**self.assignment_summary(a), "course": names.get(a.get("course_id"))}
            for a in missing
        ]
        return {"window_days": days, "due": items, "missing": missing_items,
                "announcements": announcements}

    def planner_item(self, p: dict, names: dict[int, str]) -> dict:
        plannable = p.get("plannable") or {}
        subs = p.get("submissions") or {}
        override = p.get("planner_override") or {}
        kind = p.get("plannable_type")
        done = bool(override.get("marked_complete")) or bool(subs and (subs.get("submitted") or subs.get("excused")))
        item = {
            "type": kind,
            "id": p.get("plannable_id"),
            "course_id": p.get("course_id"),
            "course": names.get(p.get("course_id")) or p.get("context_name"),
            "title": plannable.get("title"),
            "due_at": plannable.get("due_at") or p.get("plannable_date"),
            "points_possible": plannable.get("points_possible"),
            "done": done,
            "submitted": subs.get("submitted") if subs else None,
            "graded": subs.get("graded") if subs else None,
            "missing": subs.get("missing") if subs else None,
            "late": subs.get("late") if subs else None,
            "html_url": self.absolute(p.get("html_url")),
        }
        url = p.get("html_url") or ""
        if m := re.search(r"/assignments/(\d+)", url):
            item["assignment_id"] = int(m.group(1))
        if m := re.search(r"/quizzes/(\d+)", url):
            item["quiz_id"] = int(m.group(1))
        if kind == "announcement":
            item["unread"] = plannable.get("read_state") == "unread"
        return item

    # ----------------------------------------------------------------- modules

    async def modules(self, course_id: int) -> list[dict]:
        raw = await self.get_all(f"/api/v1/courses/{course_id}/modules",
                                 [("include[]", "items"), ("include[]", "content_details")])
        out = []
        for m in raw:
            items = m.get("items")
            if items is None:  # large modules omit items; fetch them separately
                items = await self.get_all(f"/api/v1/courses/{course_id}/modules/{m['id']}/items",
                                           [("include[]", "content_details")])
            out.append({
                "id": m["id"],
                "name": m.get("name"),
                "state": m.get("state"),
                "unlock_at": m.get("unlock_at"),
                "items": [self.module_item(course_id, i) for i in items],
            })
        return out

    def module_item(self, course_id: int, i: dict) -> dict:
        details = i.get("content_details") or {}
        kind = i.get("type")
        cid = i.get("content_id")
        api = {
            "Page": f"/courses/{course_id}/pages/{i.get('page_url')}" if i.get("page_url") else None,
            "Assignment": f"/courses/{course_id}/assignments/{cid}",
            "Quiz": f"/courses/{course_id}/quizzes/{cid}/review",
            "Discussion": f"/courses/{course_id}/discussions/{cid}",
            "File": f"/files/{cid}",
        }.get(kind)
        requirement = i.get("completion_requirement") or {}
        return {
            "id": i.get("id"),
            "type": kind,
            "title": i.get("title"),
            "indent": i.get("indent"),
            "content_id": cid,
            "page_url": i.get("page_url"),
            "external_url": i.get("external_url"),
            "html_url": i.get("html_url"),
            "due_at": details.get("due_at"),
            "points_possible": details.get("points_possible"),
            "locked": details.get("locked_for_user"),
            "completion": ({"type": requirement.get("type"), "completed": requirement.get("completed")}
                           if requirement else None),
            "content": api,  # this service's endpoint for the item's content
        }

    async def page(self, course_id: int, slug: str) -> dict:
        p = await self.get(f"/api/v1/courses/{course_id}/pages/{slug}")
        return {
            "title": p.get("title"),
            "url": p.get("url"),
            "updated_at": p.get("updated_at"),
            "html_url": p.get("html_url"),
            "text": html_to_text(p.get("body"), self.base_url),
            "links": html_links(p.get("body"), self.base_url),
        }

    async def discussion(self, course_id: int, topic_id: int) -> dict:
        topic, view = await asyncio.gather(
            self.get(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}"),
            self.get(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view"),
        )
        people = {p["id"]: p.get("display_name") for p in view.get("participants", [])}

        def entry(e: dict) -> dict:
            return {"id": e.get("id"), "author": people.get(e.get("user_id")),
                    "created_at": e.get("created_at"),
                    "text": html_to_text(e.get("message"), self.base_url),
                    "replies": [entry(r) for r in e.get("replies", []) if not r.get("deleted")]}

        return {
            "id": topic.get("id"),
            "title": topic.get("title"),
            "author": (topic.get("author") or {}).get("display_name"),
            "posted_at": topic.get("posted_at"),
            "assignment_id": topic.get("assignment_id"),
            "require_initial_post": topic.get("require_initial_post"),
            "text": html_to_text(topic.get("message"), self.base_url),
            "html_url": topic.get("html_url"),
            "entries": [entry(e) for e in view.get("view", []) if not e.get("deleted")],
        }

    async def file_info(self, file_id: int) -> dict:
        return _file(await self.get(f"/api/v1/files/{file_id}"))

    # ------------------------------------------------------------------ quizzes

    async def quizzes(self, course_id: int) -> list[dict]:
        assignments = await self.get_all(f"/api/v1/courses/{course_id}/assignments",
                                         [("include[]", "submission")])
        quizzes = [self.assignment_summary(a) for a in assignments
                   if a.get("quiz_id") or a.get("is_quiz_lti_assignment")]
        for q in quizzes:
            q["review"] = (f"/courses/{course_id}/quizzes/{q['quiz_id']}/review"
                           if q["kind"] == "quiz" else None)
        # Practice quizzes and surveys aren't assignments; add them when the Quizzes API is enabled.
        try:
            known = {q["quiz_id"] for q in quizzes}
            for z in await self.get_all(f"/api/v1/courses/{course_id}/quizzes"):
                if z["id"] not in known:
                    quizzes.append({"id": None, "quiz_id": z["id"], "course_id": course_id,
                                    "name": z.get("title"), "kind": z.get("quiz_type"),
                                    "due_at": z.get("due_at"), "points_possible": z.get("points_possible"),
                                    "html_url": z.get("html_url"),
                                    "review": f"/courses/{course_id}/quizzes/{z['id']}/review"})
        except CanvasError:
            pass
        return quizzes

    async def quiz_review(self, course_id: int, quiz_id: int, version: int | None = None) -> dict:
        path = f"/courses/{course_id}/quizzes/{quiz_id}/history"
        if version:
            path += f"?version={version}"
        review = parse_quiz_history(await self.web(path), self.base_url)
        review.update({"course_id": course_id, "quiz_id": quiz_id,
                       "html_url": self.absolute(path)})
        return review

    # ------------------------------------------------------------ grades & news

    async def grades(self) -> list[dict]:
        names = await self.course_names()
        enrollments = await self.get_all("/api/v1/users/self/enrollments",
                                         [("state[]", "active"), ("type[]", "StudentEnrollment"),
                                          ("include[]", "current_points")])
        return [
            {"course_id": e.get("course_id"), "course": names.get(e.get("course_id")),
             "current_score": (e.get("grades") or {}).get("current_score"),
             "current_grade": (e.get("grades") or {}).get("current_grade"),
             "final_score": (e.get("grades") or {}).get("final_score"),
             "current_points": e.get("current_points"),
             "html_url": (e.get("grades") or {}).get("html_url")}
            for e in enrollments if e.get("course_id") in names
        ]

    async def announcements(self, days: int, course_id: int | None = None) -> list[dict]:
        from datetime import datetime, timedelta, timezone

        names = await self.course_names()
        ids = [course_id] if course_id else list(names)
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        params = [("context_codes[]", f"course_{c}") for c in ids] + [("start_date", start)]
        raw = await self.get_all("/api/v1/announcements", params)
        out = []
        for a in raw:
            cid = int(a.get("context_code", "course_0").split("_")[1])
            out.append({"id": a.get("id"), "course_id": cid, "course": names.get(cid),
                        "title": a.get("title"), "posted_at": a.get("posted_at"),
                        "author": (a.get("author") or {}).get("display_name"),
                        "unread": a.get("read_state") == "unread",
                        "text": html_to_text(a.get("message"), self.base_url),
                        "html_url": a.get("html_url")})
        return sorted(out, key=lambda a: a["posted_at"] or "", reverse=True)

    # --------------------------------------------------------------- submitting

    async def upload_for_submission(self, course_id: int, assignment_id: int, name: str,
                                    data: bytes, content_type: str) -> dict:
        return await self._upload(f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self/files",
                                  name, data, content_type)

    async def _upload(self, init_path: str, name: str, data: bytes, content_type: str) -> dict:
        """Canvas's three-step upload: ask for a slot, POST the bytes, confirm."""
        slot = await self.request("POST", init_path, body={
            "name": name, "size": len(data), "content_type": content_type, "on_duplicate": "rename"})
        response = await self.browser.context.request.post(
            slot["upload_url"],
            multipart={**{k: str(v) for k, v in (slot.get("upload_params") or {}).items()},
                       "file": {"name": name, "mimeType": content_type, "buffer": data}},
            max_redirects=0, fail_on_status_code=False, timeout=300_000)
        try:
            status, headers, body = response.status, response.headers, await response.body()
        finally:
            await response.dispose()
        if status in (301, 302, 303) and headers.get("location"):
            confirmed = await self.browser.fetch("POST" if status == 307 else "GET", headers["location"])
            status, body = confirmed.status, confirmed.body
        if status >= 400:
            raise CanvasError(status, body.decode(errors="replace")[:300])
        info = json.loads(body)
        if "id" not in info and info.get("location"):
            info = await self.get(info["location"])
        return _file(info)

    async def submit(self, course_id: int, assignment_id: int, submission: dict,
                     comment: str | None) -> dict:
        body: dict[str, Any] = {"submission": submission}
        if comment:
            body["comment"] = {"text_comment": comment}
        s = await self.request("POST", f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
                               body=body)
        return self.submission_detail(s)

    async def comment(self, course_id: int, assignment_id: int, text: str) -> dict:
        s = await self.request("PUT", f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self",
                               body={"comment": {"text_comment": text}})
        return self.submission_detail(s)


# ---------------------------------------------------------------------- helpers

def _next_link(link_header: str) -> str | None:
    for url, rel in re.findall(r'<([^>]+)>;\s*rel="([^"]+)"', link_header):
        if rel == "next":
            return url
    return None


def _course(c: dict) -> dict:
    enrollment = next((e for e in c.get("enrollments") or [] if e.get("type") == "student"), {})
    return {
        "id": c["id"],
        "name": c.get("name"),
        "code": c.get("course_code"),
        "term": (c.get("term") or {}).get("name"),
        "current_score": enrollment.get("computed_current_score"),
        "current_grade": enrollment.get("computed_current_grade"),
        "start_at": c.get("start_at"),
        "end_at": c.get("end_at"),
    }


def _file(f: dict) -> dict:
    return {"id": f.get("id"), "name": f.get("display_name") or f.get("filename"),
            "content_type": f.get("content-type") or f.get("content_type"), "size": f.get("size"),
            "updated_at": f.get("updated_at"),
            "download": f"/files/{f['id']}/download" if f.get("id") else None}


def _submission_status(s: dict) -> str:
    if not s:
        return "unknown"
    if s.get("excused"):
        return "excused"
    if s.get("workflow_state") == "graded" and s.get("score") is not None:
        return "graded"
    if s.get("submitted_at") or s.get("workflow_state") in ("submitted", "pending_review"):
        return "submitted"
    if s.get("missing"):
        return "missing"
    return "not_submitted"


def html_to_text(html: str | None, base_url: str = "") -> str:
    """Readable plain text: block elements become lines, links keep their URLs."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for img in soup.find_all("img"):
        alt = img.get("alt") or img.get("title")
        img.replace_with(f"[image: {alt}]" if alt else "")
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin(base_url + "/", a["href"]) if base_url else a["href"]
        a.replace_with(f"{text} ({href})" if text and href not in text else (text or href))
    for li in soup.find_all("li"):
        li.insert(0, NavigableString("- "))
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before(NavigableString("\n"))
        tag.insert_after(NavigableString("\n"))
    text = soup.get_text("")
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_links(html: str | None, base_url: str) -> list[dict]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url + "/", a["href"])
        entry = {"text": a.get_text(" ", strip=True), "url": href}
        if m := re.search(r"/files/(\d+)", href):
            entry["file_id"] = int(m.group(1))
            entry["download"] = f"/files/{m.group(1)}/download"
        links.append(entry)
    return links


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def parse_quiz_history(html: str, base_url: str = "") -> dict:
    """Parse a Classic Quiz history (results) page into questions and the student's answers."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#content") or soup
    page_text = _text(content)
    title = _text(soup.title).split(": ", 1)[-1] if soup.title else None
    if not title and (heading := content.select_one("#quiz_title, .quiz-header h1, h1, h2")):
        title = _text(heading)

    score = possible = None
    if m := re.search(r"Score for this (?:attempt|quiz):\s*([\d.]+)\s*out of\s*([\d.]+)", page_text):
        score, possible = float(m.group(1)), float(m.group(2))
    notices = [n for n in ("Correct answers are hidden.",
                           "Quiz results are protected for this quiz and are not visible to students.",
                           "Correct answers will be available")
               if n.split(".")[0] in page_text]

    questions = []
    for q in content.select("div.display_question"):
        classes = set(q.get("class") or [])
        qtype = _text(q.select_one(".question_type")) or None
        if qtype == "text_only_question":
            continue
        result = next((r for r in ("partial_correct", "incorrect", "correct") if r in classes), None)
        earned = total = None
        if m := re.search(r"([\d.]+)?\s*/\s*([\d.]+)\s*pts", _text(q.select_one(".question_points_holder"))):
            earned = float(m.group(1)) if m.group(1) else None
            total = float(m.group(2))

        answers = []
        for a in q.select(".answers .answer"):
            a_classes = set(a.get("class") or [])
            text = _text(a.select_one(".answer_text")) or _text(a.select_one(".answer_match_left"))
            inp = a.select_one("input.question_input")
            selected = "selected_answer" in a_classes or bool(inp and inp.has_attr("checked"))
            if not text and not selected:
                continue
            entry = {"text": text, "selected": selected}
            if "correct_answer" in a_classes:
                entry["correct"] = True
            elif "wrong_answer" in a_classes:
                entry["correct"] = False
            answers.append(entry)

        responses = [i.get("value") for i in q.select("input.question_input[type=text]") if i.get("value")]
        responses += [_text(o) for o in q.select("select.question_input option[selected]") if _text(o)]
        responses += [_text(r) for r in q.select(".quiz_response_text") if _text(r)]
        comments = [_text(c) for c in q.select(".quiz_comment:not(.empty), .question_comment")
                    if _text(c)]
        question_node = q.select_one(".question_text")
        questions.append({
            "name": _text(q.select_one(".question_name")),
            "type": qtype,
            "result": result,
            "points": earned,
            "points_possible": total,
            "question": html_to_text(question_node.decode_contents() if question_node else "", base_url),
            "answers": answers,
            "responses": responses,
            "comments": comments,
        })

    attempts = []
    for li in content.select("li.quiz_version, .quiz_version"):
        link = li.select_one("a")
        if link and (m := re.search(r"version=(\d+)", link.get("href", ""))):
            attempts.append({"version": int(m.group(1)), "label": _text(li),
                             "selected": "selected" in (li.get("class") or [])})
    return {
        "title": title,
        "score": score,
        "points_possible": possible,
        "notices": notices,
        "results_visible": bool(questions),
        "questions": questions,
        "attempts": attempts,
    }
