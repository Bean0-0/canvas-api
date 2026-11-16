"""A virtual FIDO2 security key inside headless Chromium, persisted to disk.

Chrome's DevTools WebAuthn domain lets a page talk to a software authenticator. Its keys
live in memory, so every credential is exported to `passkeys.json` (mode 0600) and loaded
back into a fresh authenticator whenever the control tab is created.

The file can be shared with another service on the same machine (for example encando-api
in /var/lib/tamu-auth, mode 0660). Two copies of one key are only safe if every assertion
carries a signature counter higher than any the relying party has seen, or Duo may treat the
key as cloned. So before each use, `sync()` raises the counter to the larger of what the file,
the browser, and the clock say (seconds since 2024-01-01). The clock only moves forward, so
both copies always present a higher counter than the last one used, without coordinating.
All reads and writes of the file hold `passkeys.lock`.
"""

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
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

# Shared by every service that uses the key; must be identical in each (see module docstring).
COUNTER_EPOCH = 1704067200  # 2024-01-01T00:00:00Z: fits in 32 bits for another century


def clock_counter() -> int:
    return max(0, int(time.time()) - COUNTER_EPOCH)


class PasskeyStore:
    def __init__(self, path: Path, shared: bool = False):
        self.path = path
        self.mode = 0o660 if shared else 0o600
        self.lock_path = path.with_name(path.name + ".lock")
        self._session: CDPSession | None = None
        self._authenticator_id: str | None = None
        self._saved: list[dict] = []

    # ------------------------------------------------------------------- file

    @contextmanager
    def _locked(self):
        """Exclusive lock shared with every other user of the file. Held only for file I/O."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, self.mode)
        try:
            try:
                os.fchmod(fd, self.mode)
            except PermissionError:  # created by the other service; its mode is already right
                pass
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text())
        except ValueError:
            log.exception("unreadable passkey file %s", self.path)
            return []
        return [c for c in data.get("credentials", []) if isinstance(c, dict)]

    def _write(self, credentials: list[dict]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, self.mode)
        os.fchmod(fd, self.mode)  # the service umask (0077) would otherwise hide it from the group
        with os.fdopen(fd, "w") as handle:
            json.dump({"credentials": credentials}, handle)
        tmp.replace(self.path)

    @staticmethod
    def _merge(*sources: list[dict]) -> list[dict]:
        """Union by credentialId: never drop a key, keep the highest counter and any known rpId."""
        merged: dict[str, dict] = {}
        order: list[str] = []
        for source in sources:
            for c in source:
                cid = c.get("credentialId")
                if not cid:
                    continue
                if cid not in merged:
                    merged[cid] = dict(c)
                    order.append(cid)
                    continue
                m = merged[cid]
                m["signCount"] = max(m.get("signCount") or 0, c.get("signCount") or 0)
                m["rpId"] = m.get("rpId") or c.get("rpId")
                for k in CREDENTIAL_FIELDS:
                    if m.get(k) is None and c.get(k) is not None:
                        m[k] = c[k]
        return [merged[cid] for cid in order]

    async def has_any(self) -> bool:
        return bool(await self.credentials())

    # ---------------------------------------------------------- authenticator

    async def _add(self, credential: dict) -> None:
        await self._session.send("WebAuthn.addCredential", {
            "authenticatorId": self._authenticator_id,
            "credential": {k: credential[k] for k in CREDENTIAL_FIELDS if credential.get(k) is not None},
        })

    async def attach(self, context: BrowserContext, page: Page) -> None:
        """Give `page` a virtual security key holding every saved credential."""
        self._session = await context.new_cdp_session(page)
        await self._session.send("WebAuthn.enable", {"enableUI": False})
        result = await self._session.send("WebAuthn.addVirtualAuthenticator", {"options": AUTHENTICATOR_OPTIONS})
        self._authenticator_id = result["authenticatorId"]
        self._saved = []
        await self.sync()
        log.info("virtual security key ready with %d passkey(s)", len(self._saved))

    async def sync(self) -> None:
        """Before using the key: load credentials the other service added, and raise every
        counter to max(file, browser, clock) so the next assertion is higher than any before."""
        if self._session is None:
            return
        with self._locked():
            stored = self.load()
            loaded = {c.get("credentialId"): c for c in await self._authenticator_credentials()}
            floor = clock_counter()
            for credential in stored:
                cid = credential.get("credentialId")
                if not credential.get("rpId"):
                    log.error("skipping passkey %s: no rpId recorded", cid)
                    continue
                current = loaded.get(cid)
                target = max(credential.get("signCount") or 0, (current or {}).get("signCount") or 0, floor)
                if current is not None and current.get("signCount") == target:
                    continue
                if current is not None:
                    await self._session.send("WebAuthn.removeCredential", {
                        "authenticatorId": self._authenticator_id, "credentialId": cid})
                await self._add({**credential, "signCount": target})
                credential["signCount"] = target
            # Credentials registered in this browser but not yet saved stay in the file too.
            merged = self._merge(stored, list(loaded.values()))
            if merged != stored:
                self._write(merged)
            self._saved = merged

    async def _authenticator_credentials(self) -> list[dict]:
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

    async def credentials(self) -> list[dict]:
        if self._session is None or self._authenticator_id is None:
            return self._saved or self.load()
        return await self._authenticator_credentials()

    async def save(self) -> int:
        """Persist new credentials and sign counters. Returns the number of stored passkeys.

        Merges with the file instead of overwriting it: never drop a stored key (the private
        key cannot be recovered once lost) and never lower a counter the other service raised."""
        if self._session is None:
            return len(self._saved)
        current = await self._authenticator_credentials()
        with self._locked():
            stored = self.load()
            merged = self._merge(stored, current)
            if merged != stored:
                self._write(merged)
                log.info("saved %d passkey(s) to %s", len(merged), self.path)
            self._saved = merged
        return len(merged)

    async def summary(self) -> list[dict]:
        """Non-secret description of stored passkeys."""
        return [
            {"rpId": c.get("rpId"), "credentialId": c.get("credentialId"), "signCount": c.get("signCount"),
             "discoverable": c.get("isResidentCredential")}
            for c in await self.credentials()
        ]
