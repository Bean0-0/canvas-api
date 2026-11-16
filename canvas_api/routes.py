"""High-level, agent-friendly endpoints: to-do, courses, modules, assignments, quizzes, submitting."""

import asyncio
import base64
import binascii
import mimetypes
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .canvas import Canvas, CanvasError

SUBMIT_TYPES = ("online_text_entry", "online_url", "online_upload")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class FileIn(BaseModel):
    name: str
    content_base64: str
    content_type: str | None = None


class SubmitRequest(BaseModel):
    type: Literal["online_text_entry", "online_url", "online_upload"] | None = Field(
        None, description="Inferred from body/url/files when omitted")
    body: str | None = Field(None, description="HTML or plain text for online_text_entry")
    url: str | None = Field(None, description="URL for online_url")
    files: list[FileIn] = Field(default_factory=list, description="Files for online_upload (base64)")
    comment: str | None = Field(None, description="Optional comment attached to the submission")
    confirm: bool = Field(False, description="false = preview only; true = actually submit")


class CommentRequest(BaseModel):
    text: str = Field(min_length=1)
    confirm: bool = False


class ReplyRequest(BaseModel):
    message: str = Field(min_length=1, description="HTML or plain text")
    confirm: bool = False


def create_router(canvas: Canvas) -> APIRouter:
    router = APIRouter()

    # ------------------------------------------------------------ overview

    @router.get("/todo", tags=["overview"])
    async def todo(days: int = Query(14, ge=1, le=120),
                   include_done: bool = Query(False, description="Include submitted / marked-done items")):
        """Upcoming work from the Canvas planner, plus missing submissions and recent announcements."""
        return await canvas.todo(days, include_done)

    @router.get("/courses", tags=["overview"])
    async def courses(all: bool = Query(False, description="Include past/concluded courses")):
        return await canvas.courses(include_all=all)

    @router.get("/grades", tags=["overview"])
    async def grades():
        return await canvas.grades()

    @router.get("/announcements", tags=["overview"])
    async def announcements(days: int = Query(14, ge=1, le=365), course_id: int | None = None):
        return await canvas.announcements(days, course_id)

    @router.get("/assignments", tags=["assignments"])
    async def all_assignments(
        course_id: int | None = Query(None, description="Limit to one course; default: all active courses"),
        bucket: Literal["past", "overdue", "undated", "ungraded", "unsubmitted", "upcoming", "future"] | None = None,
    ):
        """Assignments (including quizzes) with your submission status, across courses."""
        names = await canvas.course_names()
        ids = [course_id] if course_id else list(names)
        results = await asyncio.gather(*(canvas.assignments(c, bucket) for c in ids), return_exceptions=True)
        out = []
        for cid, result in zip(ids, results):
            if isinstance(result, CanvasError) and result.status in (401, 403, 404):
                continue
            if isinstance(result, BaseException):
                raise result
            out += [{**a, "course": names.get(cid)} for a in result]
        return sorted(out, key=lambda a: (a["due_at"] is None, a["due_at"] or ""))

    # -------------------------------------------------------------- courses

    @router.get("/courses/{course_id}/modules", tags=["content"])
    async def modules(course_id: int):
        """Modules and their items. Each item's `content` is this API's endpoint for reading it."""
        return await canvas.modules(course_id)

    @router.get("/courses/{course_id}/pages/{slug}", tags=["content"])
    async def page(course_id: int, slug: str):
        return await canvas.page(course_id, slug)

    @router.get("/courses/{course_id}/discussions/{topic_id}", tags=["content"])
    async def discussion(course_id: int, topic_id: int):
        return await canvas.discussion(course_id, topic_id)

    @router.post("/courses/{course_id}/discussions/{topic_id}/reply", tags=["submit"])
    async def discussion_reply(course_id: int, topic_id: int, body: ReplyRequest):
        """Post a reply to a discussion. Previews unless `confirm` is true."""
        topic = await canvas.discussion(course_id, topic_id)
        if not body.confirm:
            return {"posted": False, "discussion": topic["title"], "message": body.message,
                    "hint": "Re-send with confirm=true to post"}
        entry = await canvas.request("POST", f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries",
                                     body={"message": body.message})
        return {"posted": True, "entry_id": entry.get("id"), "created_at": entry.get("created_at")}

    @router.get("/files/{file_id}", tags=["content"])
    async def file_info(file_id: int):
        return await canvas.file_info(file_id)

    @router.get("/files/{file_id}/download", tags=["content"], response_class=Response)
    async def file_download(file_id: int):
        info = await canvas.get(f"/api/v1/files/{file_id}")
        result = await canvas.browser.fetch("GET", info["url"], headers={"Accept": "*/*"})
        if result.status >= 400:
            raise HTTPException(result.status, "Download failed")
        name = (info.get("display_name") or "file").replace('"', "")
        return Response(result.body, media_type=info.get("content-type") or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ---------------------------------------------------------- assignments

    @router.get("/courses/{course_id}/assignments", tags=["assignments"])
    async def course_assignments(course_id: int, bucket: str | None = None):
        return await canvas.assignments(course_id, bucket)

    @router.get("/courses/{course_id}/assignments/{assignment_id}", tags=["assignments"])
    async def assignment(course_id: int, assignment_id: int):
        """Full assignment: description text, rubric, allowed types/extensions, your submission + comments."""
        return await canvas.assignment_detail(course_id, assignment_id)

    # ---------------------------------------------------------------- quizzes

    @router.get("/quizzes", tags=["quizzes"])
    async def all_quizzes(course_id: int | None = None):
        """Quizzes with scores and attempts. `review` links to the parsed results page (Classic Quizzes)."""
        names = await canvas.course_names()
        ids = [course_id] if course_id else list(names)
        results = await asyncio.gather(*(canvas.quizzes(c) for c in ids), return_exceptions=True)
        out = []
        for cid, result in zip(ids, results):
            if isinstance(result, CanvasError) and result.status in (401, 403, 404):
                continue
            if isinstance(result, BaseException):
                raise result
            out += [{**q, "course": names.get(cid)} for q in result]
        return sorted(out, key=lambda q: (q.get("due_at") is None, q.get("due_at") or ""))

    @router.get("/courses/{course_id}/quizzes/{quiz_id}/review", tags=["quizzes"])
    async def quiz_review(course_id: int, quiz_id: int,
                          version: int | None = Query(None, description="Attempt number; default latest")):
        """Your submitted answers for a Classic Quiz, with correctness where the instructor shows it."""
        return await canvas.quiz_review(course_id, quiz_id, version)

    # ------------------------------------------------------------- submitting

    @router.post("/courses/{course_id}/assignments/{assignment_id}/submit", tags=["submit"])
    async def submit(course_id: int, assignment_id: int, request: Request):
        """Submit an assignment. **Previews unless `confirm` is true.**

        JSON body (`SubmitRequest`), or multipart form with fields `type`, `body`, `url`, `comment`,
        `confirm` and one or more `files`.
        """
        req, files = await _read_submit_request(request)
        a = await canvas.get(f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
                             [("include[]", "submission")])
        kind = req.type or ("online_upload" if files else "online_url" if req.url else
                            "online_text_entry" if req.body else None)
        if kind is None:
            raise HTTPException(422, "Provide body, url, or files")
        allowed = a.get("submission_types") or []
        problems, warnings = [], []
        if kind not in allowed:
            problems.append(f"This assignment accepts {allowed}, not {kind}")
        if a.get("locked_for_user"):
            problems.append(a.get("lock_explanation") or "The assignment is locked")
        if kind == "online_upload" and not files:
            problems.append("online_upload needs at least one file")
        if kind == "online_url" and not req.url:
            problems.append("online_url needs url")
        if kind == "online_text_entry" and not req.body:
            problems.append("online_text_entry needs body")
        extensions = [e.lower().lstrip(".") for e in a.get("allowed_extensions") or []]
        for name, data, _ in files:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if extensions and ext not in extensions:
                problems.append(f"{name}: extension .{ext} not allowed ({extensions})")
            if len(data) > MAX_UPLOAD_BYTES:
                problems.append(f"{name}: larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
        sub = a.get("submission") or {}
        attempts_used = sub.get("attempt") or 0
        allowed_attempts = a.get("allowed_attempts")
        if allowed_attempts not in (None, -1) and attempts_used >= allowed_attempts:
            problems.append(f"No attempts left ({attempts_used}/{allowed_attempts})")
        if sub.get("submitted_at"):
            warnings.append(f"This replaces your attempt {attempts_used} submitted {sub['submitted_at']}")
        if a.get("due_at") and datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
            warnings.append(f"Past due ({a['due_at']}); it will be marked late")

        preview = {
            "assignment": a.get("name"),
            "course_id": course_id,
            "assignment_id": assignment_id,
            "type": kind,
            "due_at": a.get("due_at"),
            "attempt": attempts_used + 1,
            "body_preview": (req.body or "")[:300] or None,
            "url": req.url,
            "files": [{"name": n, "size": len(d), "content_type": t} for n, d, t in files],
            "comment": req.comment,
        }
        if problems:
            raise HTTPException(422, {"problems": problems, "warnings": warnings, "preview": preview})
        if not req.confirm:
            return {"submitted": False, "preview": preview, "warnings": warnings,
                    "hint": "Nothing was submitted. Re-send the same request with confirm=true to submit."}

        submission: dict[str, Any] = {"submission_type": kind}
        if kind == "online_text_entry":
            submission["body"] = req.body
        elif kind == "online_url":
            submission["url"] = req.url
        else:
            uploaded = [await canvas.upload_for_submission(course_id, assignment_id, n, d, t)
                        for n, d, t in files]
            submission["file_ids"] = [f["id"] for f in uploaded]
        result = await canvas.submit(course_id, assignment_id, submission, req.comment)
        return {"submitted": True, "warnings": warnings, "submission": result}

    @router.post("/courses/{course_id}/assignments/{assignment_id}/comment", tags=["submit"])
    async def comment(course_id: int, assignment_id: int, body: CommentRequest):
        """Add a comment to your submission (visible to the instructor). Previews unless `confirm`."""
        if not body.confirm:
            return {"posted": False, "text": body.text, "hint": "Re-send with confirm=true to post"}
        return {"posted": True, "submission": await canvas.comment(course_id, assignment_id, body.text)}

    return router


async def _read_submit_request(request: Request) -> tuple[SubmitRequest, list[tuple[str, bytes, str]]]:
    content_type = request.headers.get("content-type", "")
    files: list[tuple[str, bytes, str]] = []
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        for upload in form.getlist("files") + form.getlist("file"):
            if hasattr(upload, "read"):
                data = await upload.read()
                files.append((upload.filename or "upload", data,
                              upload.content_type or _guess_type(upload.filename)))
        fields = {k: form.get(k) for k in ("type", "body", "url", "comment") if form.get(k)}
        fields["confirm"] = str(form.get("confirm", "false")).lower() in ("1", "true", "yes")
        return SubmitRequest(**fields), files
    req = SubmitRequest.model_validate(await request.json())
    for f in req.files:
        try:
            data = base64.b64decode(f.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, f"{f.name}: content_base64 is not valid base64") from None
        files.append((f.name, data, f.content_type or _guess_type(f.name)))
    return req, files


def _guess_type(name: str | None) -> str:
    return mimetypes.guess_type(name or "")[0] or "application/octet-stream"
