"""Drives the Canvas -> Microsoft Entra (-> Duo) single sign-on flow in the control tab."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import pyotp
from playwright.async_api import Page

from .browser import CanvasBrowser
from .config import Credentials, Settings

log = logging.getLogger(__name__)

# How long a background (keepalive-triggered) login waits before retrying after MFA.
AUTO_RETRY_AFTER_MFA_SECONDS = 6 * 3600


@dataclass
class LoginStatus:
    state: str = "idle"  # idle|running|awaiting_mfa|awaiting_code|logged_in|failed|needs_attention
    message: str = ""
    mfa_number: str | None = None  # Authenticator number-match / Duo verified-push code
    started_at: float | None = None
    finished_at: float | None = None
    url: str | None = None
    automatic: bool = False
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


MS_PASSKEY_TILE = '[role=button]:has-text("Face, fingerprint, PIN or security key")'
MS_TO_DUO = "text=/redirected to Duo/i"
DUO_SECURITY_KEY = (':is(button, a, [role=button]):visible:has-text("hardware security key"), '
                    ':is(button, a, [role=button]):visible:has-text("Use security key")')
DUO_OTHER_OPTIONS = ':is(button, a):visible:has-text("Other options")'
DUO_TRUST_BROWSER = '#trust-browser-button, button:visible:has-text("Yes, this is my device")'


class MfaRequired(Exception):
    pass


class LoginManager:
    def __init__(self, settings: Settings, browser: CanvasBrowser):
        self.settings = settings
        self.browser = browser
        self.status = LoginStatus()
        self._task: asyncio.Task | None = None
        self._code: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._changed = asyncio.Condition()
        self._last_auto_mfa_stop = 0.0
        self._one_time_creds: Credentials | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _update(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self.status, key, value)
        async with self._changed:
            self._changed.notify_all()

    async def _step(self, text: str) -> None:
        log.info("login: %s", text)
        self.status.steps.append(text)
        await self._update(state="running", message=text, mfa_number=None)

    def start(self, automatic: bool = False, credentials: Credentials | None = None) -> LoginStatus:
        """Begin a login. `credentials` are used for this attempt only and never written to disk."""
        if credentials is not None:
            self._one_time_creds = credentials
        if self.running:
            if not automatic:
                # A person is now waiting: let the in-flight login continue into MFA.
                self.status.automatic = False
            return self.status
        if automatic and time.time() - self._last_auto_mfa_stop < AUTO_RETRY_AFTER_MFA_SECONDS:
            return self.status
        self.status = LoginStatus(state="running", started_at=time.time(), automatic=automatic)
        while not self._code.empty():
            self._code.get_nowait()
        self._task = asyncio.create_task(self._run())
        return self.status

    async def wait_for_progress(self, timeout: float) -> LoginStatus:
        """Wait until the login finishes or needs the user (e.g. shows an MFA number)."""
        deadline = time.monotonic() + timeout
        async with self._changed:
            while self.status.state == "running":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except asyncio.TimeoutError:
                    break
        return self.status

    async def submit_code(self, code: str) -> None:
        if self._code.full():
            self._code.get_nowait()
        await self._code.put(code)

    async def _run(self) -> None:
        page = await self.browser.control_page()
        try:
            if await self.browser.current_user():
                await self._update(state="logged_in", message="Already logged in", finished_at=time.time())
                return
            await self._drive(page)
            await self._update(state="logged_in", message="Logged in", mfa_number=None,
                               finished_at=time.time(), url=page.url)
        except MfaRequired as exc:
            self._last_auto_mfa_stop = time.time()
            await self._update(state="failed", message=str(exc), finished_at=time.time(), url=page.url)
        except LoginError as exc:
            await self._update(state=exc.state, message=str(exc), finished_at=time.time(), url=page.url)
        except Exception as exc:  # keep the service alive; surface the error to the caller
            log.exception("login crashed")
            await self._update(state="needs_attention", message=f"Login error: {exc}",
                               finished_at=time.time(), url=page.url)
        finally:
            self._one_time_creds = None
            try:
                await self.browser.passkeys.save()  # persist sign counters / new keys
            except Exception:
                log.exception("could not save passkeys")

    async def _drive(self, page: Page) -> None:
        creds = self._one_time_creds or self.settings.load_credentials()
        base_host = self.settings.base_host
        rp_ids = {c.get("rpId", "") for c in await self.browser.passkeys.credentials()}
        ms_passkey = any("microsoft" in rp for rp in rp_ids)
        duo_passkey = any("duo" in rp for rp in rp_ids)
        passkey_attempts = 0
        duo_key_attempts = 0
        duo_seen_at: float | None = None
        await self._step("Opening Canvas login")
        await page.goto(self.settings.base_url + "/login", wait_until="domcontentloaded")
        deadline = time.monotonic() + self.settings.login_timeout_seconds
        done_actions: set[str] = set()
        password_attempts = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            self.status.url = page.url
            host = urlparse(page.url).hostname or ""

            if host == base_host and not urlparse(page.url).path.startswith("/login"):
                if await self.browser.current_user():
                    return

            # --- Microsoft Entra -------------------------------------------------
            if await _visible(page, "#usernameError, #passwordError, #idTD_Error, .alert-error"):
                text = await page.locator("#usernameError, #passwordError, #idTD_Error, .alert-error").first.inner_text()
                raise LoginError("failed", f"Sign-in error: {text.strip()}")

            if await _visible(page, "#idRichContext_DisplaySign"):
                if self.status.automatic:
                    raise MfaRequired("MFA approval required; start a login with POST /session/login")
                number = (await page.locator("#idRichContext_DisplaySign").inner_text()).strip()
                if self.status.mfa_number != number:
                    await self._update(state="awaiting_mfa", mfa_number=number,
                                       message=f"Approve the sign-in in Microsoft Authenticator and enter {number}")
                continue

            if await _usable(page, "input[name=otc]"):
                code = await self._otp_code(creds)
                await page.fill("input[name=otc]", code)
                await page.click("#idSubmit_SAOTCC_Continue, input[type=submit]")
                await self._update(state="running", message="Submitted verification code")
                continue

            if await _visible(page, "[data-value=PhoneAppOTP], [data-value=PhoneAppNotification]"):
                if self.status.automatic:
                    raise MfaRequired("MFA required; start a login with POST /session/login")
                method = "PhoneAppOTP" if creds and creds.totp_secret else "PhoneAppNotification"
                if not await _visible(page, f"[data-value={method}]"):
                    method = "PhoneAppNotification" if method == "PhoneAppOTP" else "PhoneAppOTP"
                await self._step(f"Choosing MFA method {method}")
                await page.click(f"[data-value={method}]")
                continue

            if await _visible(page, "#KmsiCheckboxField, #KmsiDescription"):
                await self._step("Choosing 'Stay signed in'")
                if await _visible(page, "#KmsiCheckboxField"):
                    await page.check("#KmsiCheckboxField")
                await page.click("#idSIButton9")
                continue

            if await _usable(page, "input[name=passwd]"):
                if creds is None:
                    raise LoginError("needs_attention", "Microsoft asked for a password but no credentials are configured")
                password_attempts += 1
                if password_attempts > 2:
                    raise LoginError("failed", "Password was not accepted")
                await self._step("Entering password")
                await page.fill("input[name=passwd]", creds.password)
                await page.click("#idSIButton9")
                await page.wait_for_load_state("domcontentloaded")
                continue

            if ms_passkey and passkey_attempts < 2:
                if await _visible(page, MS_PASSKEY_TILE):
                    passkey_attempts += 1
                    await self._step("Signing in with the box's passkey")
                    await page.locator(MS_PASSKEY_TILE).first.click()
                    await asyncio.sleep(3)
                    continue
                if await _visible(page, "[data-test-id=signinOptions]") and await _usable(page, "input[name=loginfmt]"):
                    await self._step("Opening Microsoft sign-in options")
                    await page.locator("[data-test-id=signinOptions]").first.click()
                    continue

            if await _usable(page, "input[name=loginfmt]"):
                if creds is None:
                    raise LoginError("needs_attention", "Microsoft asked for a username but no credentials are configured")
                await self._step("Entering username")
                await page.fill("input[name=loginfmt]", creds.login_email)
                await page.click("#idSIButton9")
                continue

            if creds and await _visible(page, "#tilesHolder"):
                tile = page.locator(f"#tilesHolder [data-test-id='{creds.login_email}']")
                if await tile.count() and "tile" not in done_actions:
                    done_actions.add("tile")
                    await self._step("Picking account tile")
                    await tile.first.click()
                    continue

            if await _visible(page, MS_TO_DUO) and await _visible(page, "#idSIButton9"):
                await self._step("Continuing to Duo")
                await page.click("#idSIButton9")
                await page.wait_for_load_state("domcontentloaded")
                continue

            # --- Duo Universal Prompt (if the tenant federates MFA to Duo) ---------
            if "duosecurity.com" in host or "duo.com" in host:
                duo_seen_at = duo_seen_at or time.monotonic()
                if await _visible(page, DUO_TRUST_BROWSER):
                    await self._step("Telling Duo this is a trusted device")
                    await page.locator(DUO_TRUST_BROWSER).first.click()
                    continue
                if duo_passkey and duo_key_attempts < 3:
                    if await _visible(page, DUO_SECURITY_KEY):
                        duo_key_attempts += 1
                        await self._step("Using the box's security key in Duo")
                        await page.locator(DUO_SECURITY_KEY).first.click()
                        await asyncio.sleep(3)
                        continue
                    # Duo auto-starts the last-used method; give its security-key prompt a chance first.
                    waited = time.monotonic() - duo_seen_at
                    if waited > 10 and await _visible(page, DUO_OTHER_OPTIONS) and "duo-other" not in done_actions:
                        done_actions.add("duo-other")
                        await self._step("Opening Duo's other options")
                        await page.locator(DUO_OTHER_OPTIONS).first.click()
                        continue
                    if self.status.state != "running" or "Waiting for Duo" not in self.status.message:
                        await self._update(state="running", message="Waiting for Duo to accept the security key")
                    continue
                if self.status.automatic:
                    raise MfaRequired("Duo approval required; start a login with POST /session/login")
                number = await _duo_verified_push_code(page)
                if number:
                    if self.status.mfa_number != number:
                        await self._update(state="awaiting_mfa", mfa_number=number,
                                           message=f"Enter {number} in Duo Mobile to approve")
                    continue
                if await _visible(page, "input[name=passcode-input]"):
                    code = await self._otp_code(creds)
                    await page.fill("input[name=passcode-input]", code)
                    await page.click("button[type=submit]")
                    continue
                if self.status.state != "awaiting_mfa":
                    await self._update(state="awaiting_mfa", message="Approve the Duo push on your phone")
                continue

            # Microsoft's "Approve sign in request" page without a number to type.
            if await _visible(page, "#idDiv_SAOTCAS_Title"):
                if self.status.automatic:
                    raise MfaRequired("MFA approval required; start a login with POST /session/login")
                if self.status.state != "awaiting_mfa":
                    await self._update(state="awaiting_mfa", message="Approve the sign-in on your phone")
                continue

        raise LoginError(
            "needs_attention",
            "Login did not finish in time. Inspect with GET /browser/screenshot and drive it with the "
            "/browser endpoints, then call GET /session.",
        )

    async def _otp_code(self, creds) -> str:
        if creds and creds.totp_secret:
            return pyotp.TOTP(creds.totp_secret).now()
        if self.status.automatic:
            raise MfaRequired("A verification code is required; start a login with POST /session/login")
        await self._update(state="awaiting_code", message="Submit the verification code with POST /session/login/code")
        try:
            code = await asyncio.wait_for(self._code.get(), timeout=self.settings.login_timeout_seconds)
        except asyncio.TimeoutError:
            raise LoginError("failed", "No verification code was submitted") from None
        await self._update(state="running", message="Submitting verification code")
        return code.strip()


class LoginError(Exception):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


async def _visible(page: Page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:  # navigation in progress
        return False


async def _usable(page: Page, selector: str) -> bool:
    """Visible and full-sized: Microsoft keeps tiny decoy inputs around for password managers."""
    try:
        box = await page.locator(selector).first.bounding_box(timeout=500)
    except Exception:
        return False
    return bool(box and box["width"] >= 60 and box["height"] >= 20)


async def _duo_verified_push_code(page: Page) -> str | None:
    """The number Duo shows on 'Enter code in Duo Mobile' (verified push)."""
    try:
        text = await page.locator("body").inner_text(timeout=500)
    except Exception:
        return None
    if "Enter code in Duo Mobile" not in text:
        return None
    match = re.search(r"(?m)^\s*(\d{3,6})\s*$", text)
    return match.group(1) if match else None
