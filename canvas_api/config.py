"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    totp_secret: str | None = None

    @property
    def login_email(self) -> str:
        # Microsoft Entra wants the full UPN; a bare NetID gets the default domain.
        if "@" in self.username:
            return self.username
        return f"{self.username}@{os.environ.get('CANVAS_API_EMAIL_DOMAIN', 'tamu.edu')}"


@dataclass(frozen=True)
class Settings:
    base_url: str
    state_dir: Path
    api_key_file: Path
    credentials_file: Path | None
    host: str
    port: int
    keepalive_seconds: int
    login_timeout_seconds: int

    @property
    def profile_dir(self) -> Path:
        return self.state_dir / "profile"

    @property
    def base_host(self) -> str:
        return self.base_url.split("://", 1)[1].split("/", 1)[0]

    @classmethod
    def from_env(cls) -> "Settings":
        state_dir = Path(os.environ.get("CANVAS_API_STATE_DIR", "./state")).resolve()
        creds = os.environ.get("CANVAS_API_CREDENTIALS_FILE")
        return cls(
            base_url=os.environ.get("CANVAS_BASE_URL", "https://canvas.tamu.edu").rstrip("/"),
            state_dir=state_dir,
            api_key_file=Path(os.environ.get("CANVAS_API_KEY_FILE", state_dir / "api-key")),
            credentials_file=Path(creds) if creds else None,
            host=os.environ.get("CANVAS_API_HOST", "127.0.0.1"),
            port=int(os.environ.get("CANVAS_API_PORT", "8787")),
            keepalive_seconds=int(os.environ.get("CANVAS_API_KEEPALIVE_SECONDS", "600")),
            login_timeout_seconds=int(os.environ.get("CANVAS_API_LOGIN_TIMEOUT_SECONDS", "300")),
        )

    def load_credentials(self) -> Credentials | None:
        """Read KEY=VALUE credentials; re-read on every login so edits apply without restart."""
        if self.credentials_file is None or not self.credentials_file.is_file():
            return None
        values: dict[str, str] = {}
        for line in self.credentials_file.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value
        username = values.get("CANVAS_USERNAME", "").strip()
        password = values.get("CANVAS_PASSWORD", "")
        if not username or not password:
            return None
        totp = values.get("CANVAS_TOTP_SECRET", "").replace(" ", "").strip() or None
        return Credentials(username=username, password=password, totp_secret=totp)

    def load_api_keys(self) -> list[str]:
        if not self.api_key_file.is_file():
            return []
        return [k.strip() for k in self.api_key_file.read_text().splitlines() if k.strip()]
