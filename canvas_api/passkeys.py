"""A virtual FIDO2 security key inside headless Chromium, persisted to disk.

Chrome's DevTools WebAuthn domain lets a page talk to a software authenticator. Its keys
live in memory, so every credential is exported to `passkeys.json` (mode 0600) and loaded
back into a fresh authenticator whenever the control tab is created.
"""

import json
import logging
import os
from pathlib import Path

from playwright.async_api import BrowserContext, CDPSession, Page

log = logging.getLogger(__name__)

AUTHENTICATOR_OPTIONS = {
    "protocol": "ctap2",
    "ctap2Version": "ctap2_1",
    "transport": "usb",  # presents as a roaming security key
    "hasResidentKey": True,  # discoverable credentials: sign in without a username
    "hasUserVerification": True,
    "isUserVerified": True,
    "automaticPresenceSimulation": True,  # touch the key automatically
}
CREDENTIAL_FIELDS = ("credentialId", "isResidentCredential", "rpId", "privateKey", "userHandle", "signCount")


class PasskeyStore:
    def __init__(self, path: Path):
        self.path = path
        self._session: CDPSession | None = None
        self._authenticator_id: str | None = None
        self._saved: list[dict] = []

    def load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text())
        except ValueError:
            log.exception("unreadable passkey file %s", self.path)
            return []
        return [c for c in data.get("credentials", []) if isinstance(c, dict)]

    async def has_any(self) -> bool:
        return bool(await self.credentials())

    async def attach(self, context: BrowserContext, page: Page) -> None:
        """Give `page` a virtual security key holding every saved credential."""
        self._session = await context.new_cdp_session(page)
        await self._session.send("WebAuthn.enable", {"enableUI": False})
        result = await self._session.send("WebAuthn.addVirtualAuthenticator", {"options": AUTHENTICATOR_OPTIONS})
        self._authenticator_id = result["authenticatorId"]
        self._saved = self.load()
        for credential in self._saved:
            if not credential.get("rpId"):
                log.error("skipping passkey %s: no rpId recorded", credential.get("credentialId"))
                continue
            await self._session.send("WebAuthn.addCredential", {
                "authenticatorId": self._authenticator_id,
                "credential": {k: credential[k] for k in CREDENTIAL_FIELDS if credential.get(k) is not None},
            })
        log.info("virtual security key ready with %d passkey(s)", len(self._saved))

    async def credentials(self) -> list[dict]:
        if self._session is None or self._authenticator_id is None:
            return self._saved
        result = await self._session.send("WebAuthn.getCredentials", {"authenticatorId": self._authenticator_id})
        # Chrome omits rpId for non-discoverable credentials it was given; keep the one we know.
        known = {c.get("credentialId"): c.get("rpId") for c in self._saved}
        current = []
        for c in result["credentials"]:
            entry = {k: c[k] for k in CREDENTIAL_FIELDS if c.get(k) is not None}
            if not entry.get("rpId") and known.get(entry.get("credentialId")):
                entry["rpId"] = known[entry["credentialId"]]
            current.append(entry)
        return current

    async def save(self) -> int:
        """Persist new credentials and sign counters. Returns the number of stored passkeys."""
        if self._session is None:
            return len(self._saved)
        current = await self.credentials()
        # Never drop a stored key: keep any saved credential the authenticator doesn't report
        # (e.g. one that could not be loaded). The private key cannot be recovered once lost.
        reported = {c.get("credentialId") for c in current}
        current += [c for c in self.load() if c.get("credentialId") not in reported]
        if current == self._saved:
            return len(current)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"credentials": current}, handle)
        tmp.replace(self.path)
        self._saved = current
        log.info("saved %d passkey(s) to %s", len(current), self.path)
        return len(current)

    async def summary(self) -> list[dict]:
        """Non-secret description of stored passkeys."""
        return [
            {"rpId": c.get("rpId"), "credentialId": c.get("credentialId"), "signCount": c.get("signCount"),
             "discoverable": c.get("isResidentCredential")}
            for c in await self.credentials()
        ]
