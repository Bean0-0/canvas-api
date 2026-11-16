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


WEBAUTHN_PAGE = b"""<!doctype html><title>webauthn</title><script>
const rnd = n => crypto.getRandomValues(new Uint8Array(n));
window.register = () => navigator.credentials.create({publicKey: {
  challenge: rnd(32), rp: {name: "test", id: "localhost"},
  user: {id: rnd(16), name: "box", displayName: "box"},
  pubKeyCredParams: [{type: "public-key", alg: -7}],
  authenticatorSelection: {residentKey: "required", userVerification: "required"},
}}).then(c => c.id);
window.signin = () => navigator.credentials.get({publicKey: {
  challenge: rnd(32), rpId: "localhost", userVerification: "required",
}}).then(c => c.id);
const b64 = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
window.registerKey = () => navigator.credentials.create({publicKey: {
  challenge: rnd(32), rp: {name: "test", id: "localhost"},
  user: {id: rnd(16), name: "box", displayName: "box"},
  pubKeyCredParams: [{type: "public-key", alg: -7}],
  authenticatorSelection: {residentKey: "discouraged"},
}}).then(c => c.id);
window.useKey = id => navigator.credentials.get({publicKey: {
  challenge: rnd(32), rpId: "localhost", allowCredentials: [{type: "public-key", id: b64(id)}],
}}).then(c => c.id);
</script>"""


def test_passkey_survives_restart(tmp_path):
    """A passkey registered by a site is saved and usable after the browser restarts."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(WEBAUTHN_PAGE)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://localhost:{server.server_port}/"
    settings = make_settings(tmp_path)

    async def session(action, arg=None):
        browser = CanvasBrowser(settings)
        await browser.start()
        try:
            page = await browser.control_page()
            await page.goto(url)
            result = await page.evaluate(f"a => {action}(a)", arg)
            await browser.passkeys.save()
            return result, await browser.passkeys.summary()
        finally:
            await browser.stop()

    try:
        created, stored = asyncio.run(session("register"))
        assert [p["rpId"] for p in stored] == ["localhost"]
        assert (tmp_path / "passkeys.json").stat().st_mode & 0o777 == 0o600
        used, stored = asyncio.run(session("signin"))
        assert used == created
        assert stored[0]["signCount"] >= 1

        # Non-discoverable keys (what Duo registers): Chrome stops reporting their rpId
        # after a reload, so it must survive several restarts from our own records.
        (tmp_path / "passkeys.json").unlink()
        key_id, _ = asyncio.run(session("registerKey"))
        for _ in range(3):
            used, stored = asyncio.run(session("useKey", key_id))
            assert used == key_id
            assert [p["rpId"] for p in stored] == ["localhost"]
    finally:
        server.shutdown()


def test_unloadable_passkey_is_never_dropped(tmp_path):
    """A stored key the browser cannot load must survive a save untouched."""
    import json

    settings = make_settings(tmp_path)
    broken = {"credentialId": "abc", "privateKey": "secret", "rpId": None, "signCount": 4}
    (tmp_path / "passkeys.json").write_text(json.dumps({"credentials": [broken]}))

    async def run():
        browser = CanvasBrowser(settings)
        await browser.start()
        try:
            await browser.control_page()
            await browser.passkeys.save()
        finally:
            await browser.stop()

    asyncio.run(run())
    kept = json.loads((tmp_path / "passkeys.json").read_text())["credentials"]
    assert {"credentialId": "abc", "privateKey": "secret", "rpId": None, "signCount": 4} in kept
