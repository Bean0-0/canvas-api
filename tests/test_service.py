import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from canvas_api.app import _next_link, create_app
from canvas_api.browser import CanvasBrowser, strip_hijack_prefix
from canvas_api.config import Settings


def make_settings(tmp_path: Path, creds: str | None = None) -> Settings:
    (tmp_path / "api-key").write_text("secret-key\n")
    cred_file = None
    if creds is not None:
        cred_file = tmp_path / "credentials.env"
        cred_file.write_text(creds)
    return Settings(
        base_url="https://canvas.example.edu",
        state_dir=tmp_path,
        api_key_file=tmp_path / "api-key",
        credentials_file=cred_file,
        host="127.0.0.1",
        port=0,
        keepalive_seconds=600,
        login_timeout_seconds=5,
    )


def test_credentials_parsing(tmp_path):
    settings = make_settings(tmp_path, "# comment\nCANVAS_USERNAME=netid\nCANVAS_PASSWORD=p=a ss#\n")
    creds = settings.load_credentials()
    assert creds.username == "netid"
    assert creds.password == "p=a ss#"
    assert creds.totp_secret is None
    assert creds.login_email == "netid@tamu.edu"


def test_missing_credentials(tmp_path):
    assert make_settings(tmp_path).load_credentials() is None
    assert make_settings(tmp_path, "CANVAS_USERNAME=x\n").load_credentials() is None


def test_next_link():
    header = ('<https://c.edu/api/v1/courses?page=1>; rel="current",'
              '<https://c.edu/api/v1/courses?page=2>; rel="next"')
    assert _next_link(header) == "https://c.edu/api/v1/courses?page=2"
    assert _next_link('<https://c.edu/x>; rel="last"') is None


def test_strip_prefix():
    assert strip_hijack_prefix(b'while(1);[1]') == b"[1]"
    assert strip_hijack_prefix(b"[1]") == b"[1]"


def test_api_key_required(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))  # no lifespan: browser not started
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/session").status_code == 401
    assert client.get("/api/v1/courses", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/openapi.json").status_code == 200


def test_browser_launches(tmp_path):
    """The packaged headless Chromium starts and presents a non-headless user agent."""

    async def run():
        browser = CanvasBrowser(make_settings(tmp_path))
        await browser.start()
        try:
            page = await browser.control_page()
            await page.set_content("<title>ok</title>")
            assert await page.title() == "ok"
            assert "HeadlessChrome" not in await page.evaluate("navigator.userAgent")
        finally:
            await browser.stop()

    asyncio.run(run())
