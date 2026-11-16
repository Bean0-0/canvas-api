from fastapi import FastAPI
from fastapi.testclient import TestClient

from canvas_api.canvas import CanvasError, html_links, html_to_text, parse_quiz_history
from canvas_api.routes import create_router

BASE = "https://canvas.example.edu"

QUIZ_HISTORY = """<html><head><title>Student's Quiz History: Week 3 Quiz</title></head><body>
<div id="content">
  <div class="quiz_score"><span class="score_value">7</span> out of 10</div>
  <p>Score for this attempt: 7 out of 10</p>
  <div class="display_question question multiple_choice_question correct" id="question_1">
    <span class="name question_name">Question 1</span>
    <span class="question_points_holder"><div class="user_points">5 <span class="points question_points"> / 5</span> pts</div></span>
    <span class="question_type">multiple_choice_question</span>
    <div class="question_text user_content"><p>What is 2 + 2?</p></div>
    <div class="answers"><div class="answers_wrapper">
      <div class="answer selected_answer correct_answer"><input type="radio" class="question_input" checked><div class="answer_text">4</div></div>
      <div class="answer"><input type="radio" class="question_input"><div class="answer_text">5</div></div>
    </div></div>
  </div>
  <div class="display_question question short_answer_question incorrect" id="question_2">
    <span class="name question_name">Question 2</span>
    <span class="question_points_holder"><div class="user_points">0 <span class="points"> / 3</span> pts</div></span>
    <span class="question_type">short_answer_question</span>
    <div class="question_text user_content"><p>Capital of <b>France</b>?</p></div>
    <div class="answers"><input type="text" class="question_input" value="Lyon"></div>
    <div class="quiz_comment"><div class="answer_comment">Paris</div></div>
  </div>
  <div class="display_question question text_only_question" id="question_3">
    <span class="question_type">text_only_question</span>
  </div>
  <ul><li class="quiz_version selected"><a href="?version=2">Attempt 2: 7</a></li>
      <li class="quiz_version"><a href="?version=1">Attempt 1: 4</a></li></ul>
</div></body></html>"""

PROTECTED = """<html><head><title>Student's Quiz History: Lab Quiz</title></head><body><div id="content">
Quiz results are protected for this quiz and are not visible to students. Correct answers are hidden.
Score for this quiz: 40 out of 50</div></body></html>"""


def test_quiz_history_parsing():
    r = parse_quiz_history(QUIZ_HISTORY, BASE)
    assert r["title"] == "Week 3 Quiz"
    assert (r["score"], r["points_possible"]) == (7.0, 10.0)
    assert r["results_visible"] is True
    q1, q2 = r["questions"]
    assert q1["result"] == "correct" and q1["points"] == 5.0 and q1["points_possible"] == 5.0
    assert q1["question"] == "What is 2 + 2?"
    assert q1["answers"][0] == {"text": "4", "selected": True, "correct": True}
    assert q1["answers"][1] == {"text": "5", "selected": False}
    assert q2["result"] == "incorrect" and q2["responses"] == ["Lyon"] and q2["comments"] == ["Paris"]
    assert [a["version"] for a in r["attempts"]] == [2, 1]


def test_protected_quiz_results():
    r = parse_quiz_history(PROTECTED, BASE)
    assert r["score"] == 40.0 and r["results_visible"] is False and r["questions"] == []
    assert any("protected" in n for n in r["notices"])


def test_html_to_text_keeps_structure_and_links():
    html = '<h2>Lab 4</h2><p>Read <a href="/courses/1/files/99">the handout</a> first.</p><ul><li>one</li><li>two</li></ul>'
    assert html_to_text(html, BASE) == (
        f"Lab 4\n\nRead the handout ({BASE}/courses/1/files/99) first.\n\n- one\n\n- two"
    )
    assert html_links(html, BASE)[0]["file_id"] == 99


class FakeCanvas:
    """Stands in for Canvas: one assignment that accepts .pdf uploads and text entry."""

    def __init__(self, **assignment):
        self.assignment = {"id": 5, "name": "Essay", "submission_types": ["online_upload", "online_text_entry"],
                           "allowed_extensions": ["pdf"], "due_at": "2999-01-01T00:00:00Z",
                           "allowed_attempts": -1, "submission": {"attempt": 0}, **assignment}
        self.submitted = None
        self.uploads = []

    async def get(self, path, params=None):
        return self.assignment

    async def upload_for_submission(self, course_id, assignment_id, name, data, content_type):
        self.uploads.append((name, data))
        return {"id": 77, "name": name}

    async def submit(self, course_id, assignment_id, submission, comment):
        self.submitted = (submission, comment)
        return {"status": "submitted", "attempt": 1}


def client_for(fake):
    app = FastAPI()
    app.include_router(create_router(fake))

    @app.exception_handler(CanvasError)
    async def _err(_, exc):  # pragma: no cover
        raise exc

    return TestClient(app)


def test_submit_previews_without_confirm():
    fake = FakeCanvas()
    r = client_for(fake).post("/courses/1/assignments/5/submit", json={"body": "<p>My essay</p>"})
    assert r.status_code == 200
    assert r.json()["submitted"] is False and r.json()["preview"]["type"] == "online_text_entry"
    assert fake.submitted is None


def test_submit_text_with_confirm():
    fake = FakeCanvas()
    r = client_for(fake).post("/courses/1/assignments/5/submit",
                              json={"body": "<p>My essay</p>", "comment": "hi", "confirm": True})
    assert r.json()["submitted"] is True
    assert fake.submitted == ({"submission_type": "online_text_entry", "body": "<p>My essay</p>"}, "hi")


def test_submit_multipart_upload():
    fake = FakeCanvas()
    r = client_for(fake).post("/courses/1/assignments/5/submit",
                              files={"files": ("report.pdf", b"%PDF-1.4", "application/pdf")},
                              data={"confirm": "true"})
    assert r.json()["submitted"] is True
    assert fake.uploads == [("report.pdf", b"%PDF-1.4")]
    assert fake.submitted[0] == {"submission_type": "online_upload", "file_ids": [77]}


def test_submit_rejects_wrong_extension_and_type():
    fake = FakeCanvas()
    r = client_for(fake).post("/courses/1/assignments/5/submit",
                              json={"files": [{"name": "a.docx", "content_base64": "aGk="}], "confirm": True})
    assert r.status_code == 422 and "not allowed" in str(r.json())
    r = client_for(fake).post("/courses/1/assignments/5/submit", json={"url": "https://x.y", "confirm": True})
    assert r.status_code == 422 and "accepts" in str(r.json())
    assert fake.submitted is None and fake.uploads == []


def test_submit_rejects_when_no_attempts_left():
    fake = FakeCanvas(allowed_attempts=1, submission={"attempt": 1, "submitted_at": "2026-01-01T00:00:00Z"})
    r = client_for(fake).post("/courses/1/assignments/5/submit", json={"body": "x", "confirm": True})
    assert r.status_code == 422 and "No attempts left" in str(r.json())


def test_quiz_title_falls_back_to_heading():
    html = '<html><body><div id="content"><h1 id="quiz_title">Week 9 Quiz</h1>Score for this attempt: 1 out of 2</div></body></html>'
    assert parse_quiz_history(html)["title"] == "Week 9 Quiz"


from canvas_api.canvas import Canvas, _filter_files  # noqa: E402


class FakeFilesCanvas(Canvas):
    """Canvas with canned API responses for one module."""

    FILES = {
        1: {"id": 1, "display_name": "Lecture 1.pdf", "content-type": "application/pdf", "size": 10},
        2: {"id": 2, "display_name": "Data.xlsx", "content-type": "application/vnd.ms-excel", "size": 20},
        3: {"id": 3, "display_name": "diagram.png", "content-type": "image/png", "size": 30},
    }

    def __init__(self):
        self.base_url = BASE

    async def get(self, path, params=None):
        if path == "/api/v1/courses/9/modules/5":
            return {"id": 5, "name": "Week 1: Intro"}
        if path == "/api/v1/courses/9/pages/lab-handout":
            return {"body": '<a href="/courses/9/files/2">data</a> <img src="/courses/9/files/3/preview">'
                            ' <a href="/courses/9/files/404">gone</a>'}
        if path.startswith("/api/v1/files/"):
            fid = int(path.rsplit("/", 1)[1])
            if fid not in self.FILES:
                raise CanvasError(404, "not found")
            return self.FILES[fid]
        raise AssertionError(path)

    async def get_all(self, path, params=None, max_pages=50):
        assert path == "/api/v1/courses/9/modules/5/items"
        return [{"type": "File", "title": "Lecture 1", "content_id": 1},
                {"type": "Page", "title": "Lab Handout", "page_url": "lab-handout"},
                {"type": "ExternalUrl", "title": "Video", "external_url": "https://x.y"}]

    async def download(self, file_id):
        return self.FILES[file_id], f"bytes-{file_id}".encode()


def test_module_files_collects_items_and_attachments():
    import asyncio

    r = asyncio.run(FakeFilesCanvas().module_files(9, 5))
    assert r["module"] == "Week 1: Intro"
    assert [(f["path"], f["name"], f["sources"]) for f in r["files"]] == [
        ("", "Lecture 1.pdf", ["module"]), ("Lab Handout", "Data.xlsx", ["page"])]
    assert any("404" in e for e in r["errors"])  # unreachable link reported, not fatal
    with_images = asyncio.run(FakeFilesCanvas().module_files(9, 5, include_images=True))
    assert "diagram.png" in [f["name"] for f in with_images["files"]]
    only_pdf = asyncio.run(FakeFilesCanvas().module_files(9, 5, types=["pdf"]))
    assert [f["name"] for f in only_pdf["files"]] == ["Lecture 1.pdf"]


def test_module_zip():
    import io
    import json
    import zipfile

    r = client_for(FakeFilesCanvas()).get("/courses/9/modules/5/files/zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="Week_1_Intro.zip"' in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["Lab Handout/Data.xlsx", "Lecture 1.pdf", "manifest.json"]
    assert zf.read("Lab Handout/Data.xlsx") == b"bytes-2"
    assert json.loads(zf.read("manifest.json"))["count"] == 2


def test_filter_files_by_match():
    files = [{"name": "HW1.pdf", "content_type": "application/pdf", "path": ""},
             {"name": "Notes.pdf", "content_type": "application/pdf", "path": ""}]
    assert [f["name"] for f in _filter_files(files, False, None, "hw")] == ["HW1.pdf"]
