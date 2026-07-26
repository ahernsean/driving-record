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
    form_secret: str
    development_allow_non_loopback: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        state = Path(
            os.environ.get(
                "DRIVING_LOG_STATE_DIR",
                str(Path.home() / ".local" / "state" / "driving-log"),
            )
        ).expanduser()
        host = os.environ.get("DRIVING_LOG_HOST", "127.0.0.1")
        development_override = os.environ.get("DRIVING_LOG_DEV_ALLOW_NON_LOOPBACK") == "1"
        if not _is_loopback(host) and not development_override:
            raise ValueError(
                "Refusing non-loopback bind; set DRIVING_LOG_DEV_ALLOW_NON_LOOPBACK=1 "
                "only for development"
            )
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
            public_host=os.environ.get("DRIVING_LOG_PUBLIC_HOST", "127.0.0.1:8766"),
            form_secret=os.environ.get("DRIVING_LOG_FORM_SECRET", "development-only-secret"),
            development_allow_non_loopback=development_override,
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
