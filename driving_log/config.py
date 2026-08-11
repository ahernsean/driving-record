from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

APEX_LATITUDE = 35.7327
APEX_LONGITUDE = -78.8503
DEFAULT_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    database_path: Path
    archive_dir: Path
    restore_dir: Path
    host: str
    port: int
    public_host: str
    public_scheme: str = "http"
    operation_secret: str = "development-only-operation-secret"
    development_allow_non_loopback: bool = False
    auth_required: bool = True
    sean_password_hash: str = ""
    jen_password_hash: str = ""
    session_secret: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        state = Path(
            os.environ.get(
                "DRIVING_LOG_STATE_DIR",
                str(Path(__file__).resolve().parent.parent / "driving-log-runtime"),
            )
        ).expanduser()
        host = os.environ.get("DRIVING_LOG_HOST", "127.0.0.1")
        development_override = os.environ.get("DRIVING_LOG_DEV_ALLOW_NON_LOOPBACK") == "1"
        if not _is_loopback(host) and not development_override:
            raise ValueError(
                "Refusing non-loopback bind; set DRIVING_LOG_DEV_ALLOW_NON_LOOPBACK=1 "
                "only for development"
            )
        public_host = os.environ.get("DRIVING_LOG_PUBLIC_HOST", "127.0.0.1:8766")
        public_scheme = os.environ.get("DRIVING_LOG_PUBLIC_SCHEME", "http")
        auth_required_text = os.environ.get("DRIVING_LOG_AUTH_REQUIRED")
        if public_scheme not in {"http", "https"}:
            raise ValueError("DRIVING_LOG_PUBLIC_SCHEME must be http or https")
        return cls(
            state_dir=state,
            database_path=Path(
                os.environ.get("DRIVING_LOG_DATABASE", str(state / "driving-log.sqlite3"))
            ),
            archive_dir=Path(os.environ.get("DRIVING_LOG_ARCHIVE_DIR", str(state / "archives"))),
            restore_dir=Path(
                os.environ.get("DRIVING_LOG_RESTORE_DIR", str(state / "restore-requests"))
            ),
            host=host,
            port=int(os.environ.get("DRIVING_LOG_PORT", "8766")),
            public_host=public_host,
            public_scheme=public_scheme,
            operation_secret=os.environ.get(
                "DRIVING_LOG_OPERATION_SECRET",
                "development-only-operation-secret",
            ),
            development_allow_non_loopback=development_override,
            auth_required=(
                auth_required_text != "0"
                if auth_required_text is not None
                else not public_host.startswith(("127.0.0.1", "localhost", "testserver"))
            ),
            sean_password_hash=os.environ.get("DRIVING_LOG_SEAN_PASSWORD_HASH", ""),
            jen_password_hash=os.environ.get("DRIVING_LOG_JEN_PASSWORD_HASH", ""),
            session_secret=os.environ.get("DRIVING_LOG_SESSION_SECRET", ""),
        )

    def validate_authentication(self) -> None:
        if self.auth_required and not all(
            (self.sean_password_hash, self.jen_password_hash, self.session_secret)
        ):
            raise ValueError(
                "authentication is required; configure DRIVING_LOG_SEAN_PASSWORD_HASH, "
                "DRIVING_LOG_JEN_PASSWORD_HASH, and DRIVING_LOG_SESSION_SECRET"
            )

    def ensure_directories(self) -> None:
        for path in (self.state_dir, self.archive_dir, self.restore_dir):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
