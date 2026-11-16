"""FastAPI service: Canvas REST/GraphQL passthrough plus remote control of the headless browser."""

import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from pydantic import BaseModel, Field

from .browser import CanvasBrowser, strip_hijack_prefix
from .canvas import Canvas, CanvasError, SessionExpired
from .config import Credentials, Settings
from .login import LoginManager
from .routes import create_router

log = logging.getLogger(__name__)

PASS_REQUEST_HEADERS = ("content-type", "accept")
PASS_RESPONSE_HEADERS = ("content-type", "content-disposition", "x-total-count", "last-modified", "etag")
LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


class LoginRequest(BaseModel):
    wait_seconds: float = Field(30, ge=0, le=120, description="Wait for completion or an MFA prompt")
    username: str | None = Field(None, description="One-time NetID/email; used for this login only, never stored")
    password: str | None = Field(None, description="One-time password; used for this login only, never stored")


class CodeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class GotoRequest(BaseModel):
    url: str = Field(description="Absolute URL, or a path on the Canvas site")
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "domcontentloaded"


class SelectorRequest(BaseModel):
    selector: str = Field(description="Playwright selector, e.g. `text=Submit` or `#id`")
    timeout_ms: float = 10_000


class FillRequest(SelectorRequest):
    value: str


class PressRequest(BaseModel):
    key: str = Field(description="e.g. Enter, Tab, Control+A")
    selector: str | None = None


class EvaluateRequest(BaseModel):
    expression: str = Field(description="JS expression or function, e.g. `() => document.title`")
    arg: Any = None


class WaitRequest(SelectorRequest):
    state: Literal["attached", "detached", "visible", "hidden"] = "visible"


class ApiKeys:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._mtime: float | None = None
        self._keys: list[str] = []

    def keys(self) -> list[str]:
        try:
            mtime = self.settings.api_key_file.stat().st_mtime
        except FileNotFoundError:
            return []
        if mtime != self._mtime:
            self._keys = self.settings.load_api_keys()
            self._mtime = mtime
        return self._keys

    def check(self, presented: str | None) -> bool:
        if not presented:
            return False
        return any(hmac.compare_digest(presented.encode(), key.encode()) for key in self.keys())


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    browser = CanvasBrowser(settings)
    login = LoginManager(settings, browser)
    canvas = Canvas(browser)
    api_keys = ApiKeys(settings)

    async def keepalive() -> None:
        """Touch Canvas periodically so the session stays warm; re-login silently when possible."""
        while True:
            try:
                if not login.running and await browser.current_user() is None:
                    if settings.load_credentials() or await browser.passkeys.has_any():
                        log.info("session missing; starting automatic login")
                        login.start(automatic=True)
            except Exception:
                log.exception("keepalive check failed")
            await asyncio.sleep(settings.keepalive_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await browser.start()
        task = asyncio.create_task(keepalive())
        try:
            yield
        finally:
            task.cancel()
            await browser.stop()

    app = FastAPI(
        title="Canvas API",
        version="0.1.0",
        description=(
            f"Authenticated access to {settings.base_url} through a logged-in headless browser. "
            "Send `Authorization: Bearer <key>` on every request except /health. "
            "`/api/...` forwards to the Canvas REST API (`/api/v1/...`) and GraphQL (`/api/graphql`); "
            "add `all_pages=true` to merge paginated lists."
        ),
        lifespan=lifespan,
    )

    async def require_key(request: Request) -> None:
        if not api_keys.keys():
            raise HTTPException(503, "No API key configured on the server")
        header = request.headers.get("authorization", "")
        presented = header[7:] if header.lower().startswith("bearer ") else request.headers.get("x-api-key")
        if not api_keys.check(presented):
            raise HTTPException(401, "Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_key)]

    @app.exception_handler(SessionExpired)
    async def _expired(_: Request, exc: SessionExpired):
        if settings.load_credentials() or await browser.passkeys.has_any():
            login.start(automatic=True)
        return JSONResponse({"error": "canvas_session_expired",
                             "detail": "The Canvas session is not logged in. POST /session/login to sign in.",
                             "login": login.status.to_dict()}, status_code=401)

    @app.exception_handler(CanvasError)
    async def _canvas_error(_: Request, exc: CanvasError):
        return JSONResponse({"error": "canvas_error", "status": exc.status, "detail": exc.detail},
                            status_code=exc.status if 400 <= exc.status < 600 else 502)

    @app.exception_handler(PlaywrightTimeout)
    async def _timeout(_: Request, exc: PlaywrightTimeout):
        return JSONResponse({"detail": f"Browser timeout: {exc.message}"}, status_code=504)

    @app.exception_handler(PlaywrightError)
    async def _pw_error(_: Request, exc: PlaywrightError):
        return JSONResponse({"detail": f"Browser error: {exc.message}"}, status_code=502)

    # ------------------------------------------------------------------ session

    @app.get("/health", tags=["service"])
    async def health():
        return {"ok": True}

    @app.get("/session", tags=["session"], dependencies=auth)
    async def session():
        user = await browser.current_user()
        return {"logged_in": user is not None, "user": user, "login": login.status.to_dict()}

    @app.post("/session/login", tags=["session"], dependencies=auth)
    async def start_login(body: LoginRequest | None = None):
        """Start SSO login. Returns early with `awaiting_mfa` + `mfa_number` when your phone must approve."""
        body = body or LoginRequest()
        one_time = None
        if body.username and body.password:
            one_time = Credentials(username=body.username, password=body.password)
        login.start(automatic=False, credentials=one_time)
        status = await login.wait_for_progress(body.wait_seconds)
        return status.to_dict()

    @app.get("/session/login", tags=["session"], dependencies=auth)
    async def login_status(wait_seconds: float = 0):
        if wait_seconds:
            await login.wait_for_progress(min(wait_seconds, 120))
        return login.status.to_dict()

    @app.post("/session/login/code", tags=["session"], dependencies=auth)
    async def login_code(body: CodeRequest):
        """Provide a one-time verification code when the login state is `awaiting_code`."""
        await login.submit_code(body.code)
        return (await login.wait_for_progress(15)).to_dict()

    @app.get("/passkey", tags=["session"], dependencies=auth)
    async def passkey_status():
        """Passkeys held by the browser's virtual security key (no private material)."""
        return {"passkeys": await browser.passkeys.summary()}

    @app.post("/passkey/save", tags=["session"], dependencies=auth)
    async def passkey_save():
        """Write newly registered passkeys to disk (also happens automatically)."""
        return {"saved": await browser.passkeys.save()}

    @app.post("/session/logout", tags=["session"], dependencies=auth)
    async def logout():
        await browser.context.clear_cookies()
        return {"logged_in": False}

    # ------------------------------------------------------------- canvas proxy

    def forward_headers(request: Request) -> dict[str, str]:
        return {k: v for k, v in request.headers.items() if k.lower() in PASS_REQUEST_HEADERS}

    def rewrite_links(value: str, request: Request) -> str:
        own = str(request.base_url).rstrip("/")
        return value.replace(settings.base_url, own)

    def session_expired(status: int, body: bytes) -> bool:
        return status == 401 and b"unauthenticated" in body

    async def canvas_api(path: str, request: Request, all_pages: bool = False, max_pages: int = 50):
        """Forward to `{base_url}/api/{path}` as the logged-in user. See https://canvas.instructure.com/doc/api/."""
        params = [(k, v) for k, v in request.query_params.multi_items() if k not in ("all_pages", "max_pages")]
        body = await request.body()
        headers = forward_headers(request)
        result = await browser.fetch(request.method, f"/api/{path}", params=params, body=body, headers=headers)

        if session_expired(result.status, result.body):
            if settings.load_credentials() or await browser.passkeys.has_any():
                login.start(automatic=True)
            return JSONResponse(
                {"error": "canvas_session_expired",
                 "detail": "The Canvas session is not logged in. POST /session/login to sign in.",
                 "login": login.status.to_dict()},
                status_code=401,
            )

        content_type = result.headers.get("content-type", "")
        if all_pages and request.method == "GET" and result.status == 200 and "json" in content_type:
            items = result.json()
            pages = 1
            next_url = _next_link(result.headers.get("link", ""))
            while isinstance(items, list) and next_url and pages < max_pages:
                page = await browser.fetch("GET", next_url)
                if page.status != 200:
                    break
                more = page.json()
                if not isinstance(more, list):
                    break
                items.extend(more)
                pages += 1
                next_url = _next_link(page.headers.get("link", ""))
            return JSONResponse(items, headers={"X-Pages-Fetched": str(pages),
                                                "X-Truncated": "true" if next_url else "false"})

        out_headers = {k: v for k, v in result.headers.items() if k.lower() in PASS_RESPONSE_HEADERS}
        if "link" in result.headers:
            out_headers["Link"] = rewrite_links(result.headers["link"], request)
        content = strip_hijack_prefix(result.body) if "json" in content_type else result.body
        return Response(content, status_code=result.status, headers=out_headers)

    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        app.add_api_route("/api/{path:path}", canvas_api, methods=[method], tags=["canvas"],
                          dependencies=auth, operation_id=f"canvas_api_{method.lower()}")

    @app.get("/web/{path:path}", tags=["canvas"], dependencies=auth)
    async def canvas_web(path: str, request: Request):
        """GET any Canvas URL with the session (HTML pages, `/files/:id/download`, exports)."""
        params = list(request.query_params.multi_items())
        result = await browser.fetch("GET", f"/{path}", params=params, headers={"Accept": "*/*"})
        out_headers = {k: v for k, v in result.headers.items() if k.lower() in PASS_RESPONSE_HEADERS}
        out_headers["X-Final-URL"] = result.url
        return Response(result.body, status_code=result.status, headers=out_headers)

    # ----------------------------------------------------------- browser control

    async def page_state() -> dict:
        page = await browser.control_page()
        await browser.passkeys.save()  # a browser action may have registered a passkey
        return {"url": page.url, "title": await page.title()}

    @app.get("/browser", tags=["browser"], dependencies=auth)
    async def browser_state():
        return await page_state()

    @app.post("/browser/goto", tags=["browser"], dependencies=auth)
    async def browser_goto(body: GotoRequest):
        page = await browser.control_page()
        url = settings.base_url + body.url if body.url.startswith("/") else body.url
        response = await page.goto(url, wait_until=body.wait_until)
        return {**await page_state(), "status": response.status if response else None}

    @app.get("/browser/screenshot", tags=["browser"], dependencies=auth,
             response_class=Response, responses={200: {"content": {"image/png": {}}}})
    async def browser_screenshot(full_page: bool = False):
        page = await browser.control_page()
        return Response(await page.screenshot(full_page=full_page), media_type="image/png")

    @app.get("/browser/content", tags=["browser"], dependencies=auth)
    async def browser_content(format: Literal["text", "html"] = "text", selector: str = "body"):
        page = await browser.control_page()
        if format == "html":
            content = await page.content() if selector == "body" else await page.locator(selector).first.inner_html()
        else:
            content = await page.locator(selector).first.inner_text()
        return {**await page_state(), "content": content}

    @app.post("/browser/click", tags=["browser"], dependencies=auth)
    async def browser_click(body: SelectorRequest):
        page = await browser.control_page()
        await page.locator(body.selector).first.click(timeout=body.timeout_ms)
        return await page_state()

    @app.post("/browser/fill", tags=["browser"], dependencies=auth)
    async def browser_fill(body: FillRequest):
        page = await browser.control_page()
        await page.locator(body.selector).first.fill(body.value, timeout=body.timeout_ms)
        return await page_state()

    @app.post("/browser/press", tags=["browser"], dependencies=auth)
    async def browser_press(body: PressRequest):
        page = await browser.control_page()
        if body.selector:
            await page.locator(body.selector).first.press(body.key)
        else:
            await page.keyboard.press(body.key)
        return await page_state()

    @app.post("/browser/wait", tags=["browser"], dependencies=auth)
    async def browser_wait(body: WaitRequest):
        page = await browser.control_page()
        await page.locator(body.selector).first.wait_for(state=body.state, timeout=body.timeout_ms)
        return await page_state()

    @app.post("/browser/evaluate", tags=["browser"], dependencies=auth)
    async def browser_evaluate(body: EvaluateRequest):
        page = await browser.control_page()
        result = await page.evaluate(body.expression, body.arg)
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = repr(result)
        return {"result": result}

    @app.get("/browser/network", tags=["browser"], dependencies=auth)
    async def browser_network(contains: str | None = None, limit: int = 200, include_headers: bool = False):
        """API calls (XHR/fetch) the browser tab made, newest last. Use it to find internal endpoints:
        POST /browser/goto a page, then read what it called."""
        return {"requests": browser.network_log(contains, limit, include_headers)}

    @app.delete("/browser/network", tags=["browser"], dependencies=auth)
    async def browser_network_clear():
        browser.clear_network_log()
        return {"cleared": True}

    app.include_router(create_router(canvas), dependencies=auth)
    return app


def _next_link(link_header: str) -> str | None:
    for url, rel in LINK_RE.findall(link_header):
        if rel == "next":
            return url
    return None
