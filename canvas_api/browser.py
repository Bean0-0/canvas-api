"""A single persistent headless Chromium profile holding the Canvas session."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import unquote, urlencode

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .config import Settings
from .passkeys import PasskeyStore

log = logging.getLogger(__name__)

# Canvas prefixes JSON for cookie-authenticated, non-XHR requests to stop JSON hijacking.
JSON_HIJACK_PREFIX = b"while(1);"


@dataclass
class FetchResult:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    def json(self):
        return json.loads(strip_hijack_prefix(self.body) or b"null")


def strip_hijack_prefix(body: bytes) -> bytes:
    if body.startswith(JSON_HIJACK_PREFIX):
        return body[len(JSON_HIJACK_PREFIX):]
    return body


class CanvasBrowser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._page_lock = asyncio.Lock()
        self.passkeys = PasskeyStore(settings.state_dir / "passkeys.json")
        self._network: deque[dict] = deque(maxlen=1000)

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("browser not started")
        return self._context

    async def start(self) -> None:
        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
        # Microsoft sign-in treats "HeadlessChrome" as a bot; present as normal Chrome.
        probe = await self._pw.chromium.launch(headless=True, args=args)
        try:
            ua = await (await probe.new_page()).evaluate("navigator.userAgent")
        finally:
            await probe.close()
        self._context = await self._pw.chromium.launch_persistent_context(
            str(self.settings.profile_dir),
            headless=True,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
            user_agent=ua.replace("HeadlessChrome", "Chrome"),
            args=args,
        )
        log.info("browser started with profile %s", self.settings.profile_dir)

    async def stop(self) -> None:
        if self._context is not None:
            try:
                await self.passkeys.save()
            except Exception:
                log.exception("could not save passkeys on shutdown")
            await self._context.close()
        if self._pw is not None:
            await self._pw.stop()

    async def control_page(self) -> Page:
        """The one tab used for login and remote browser control."""
        async with self._page_lock:
            if self._page is None or self._page.is_closed():
                pages = self.context.pages
                self._page = pages[0] if pages else await self.context.new_page()
                for extra in pages[1:]:
                    await extra.close()
                await self.passkeys.attach(self.context, self._page)
                self._page.on("response", self._record_response)
            return self._page

    def _record_response(self, response) -> None:
        request = response.request
        if request.resource_type not in ("xhr", "fetch"):
            return
        headers = {k: v for k, v in request.headers.items() if k.lower() != "cookie"}
        try:
            post_data = (request.post_data or "")[:2000] or None
        except Exception:  # binary bodies can't be decoded
            post_data = "<binary>"
        self._network.append({
            "time": time.time(),
            "method": request.method,
            "url": request.url,
            "status": response.status,
            "content_type": response.headers.get("content-type"),
            "post_data": post_data,
            "request_headers": headers,
        })

    def network_log(self, contains: str | None, limit: int, include_headers: bool) -> list[dict]:
        entries = [e for e in self._network if not contains or contains in e["url"]][-limit:]
        if include_headers:
            return entries
        return [{k: v for k, v in e.items() if k != "request_headers"} for e in entries]

    def clear_network_log(self) -> None:
        self._network.clear()

    async def csrf_token(self) -> str | None:
        for cookie in await self.context.cookies(self.settings.base_url):
            if cookie["name"] == "_csrf_token":
                return unquote(cookie["value"])
        return None

    async def fetch(
        self,
        method: str,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        timeout_ms: float = 60_000,
    ) -> FetchResult:
        """Make a request with the browser's cookies, adding Canvas's CSRF header."""
        if url.startswith("/"):
            url = self.settings.base_url + url
        send_headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        send_headers.update(headers or {})
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            token = await self.csrf_token()
            if token:
                send_headers["X-CSRF-Token"] = token
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        response = await self.context.request.fetch(
            url,
            method=method.upper(),
            headers=send_headers,
            data=body if body else None,
            max_redirects=20 if follow_redirects else 0,
            timeout=timeout_ms,
            fail_on_status_code=False,
        )
        try:
            return FetchResult(
                status=response.status,
                headers=dict(response.headers),
                body=await response.body(),
                url=response.url,
            )
        finally:
            await response.dispose()

    async def current_user(self) -> dict | None:
        """The logged-in Canvas user, or None when the session is gone."""
        try:
            result = await self.fetch("GET", "/api/v1/users/self", follow_redirects=False)
        except Exception:  # network trouble is not proof of logout
            log.exception("session check failed")
            raise
        if result.status != 200:
            return None
        try:
            data = result.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) and "id" in data else None
