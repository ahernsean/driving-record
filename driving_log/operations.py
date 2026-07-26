from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from driving_log.archive import restore_archive, verify_archive

UNIT_NAMES = (
    "driving-log-web.service",
    "driving-log-archive.service",
    "driving-log-archive.timer",
    "driving-log-restore@.service",
)


def run_systemctl(action: str, *units: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", action, *units],
        check=check,
        text=True,
        capture_output=True,
    )


def install_user_units(
    repo_root: Path,
    *,
    public_host: str,
    public_scheme: str = "http",
    external_archive_dir: Path | None = None,
) -> dict[str, str]:
    config_dir = Path.home() / ".config" / "driving-log"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir = Path.home() / ".local" / "state" / "driving-log"
    for directory in (
        state_dir,
        state_dir / "archives",
        state_dir / "restore-requests",
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    environment_path = config_dir / "environment"
    existing: dict[str, str] = {}
    if environment_path.exists():
        for line in environment_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                existing[key] = value
    secret = (
        existing.get("DRIVING_LOG_OPERATION_SECRET")
        or existing.get("DRIVING_LOG_FORM_SECRET")
        or secrets.token_urlsafe(48)
    )
    environment = {
        "DRIVING_LOG_HOST": "127.0.0.1",
        "DRIVING_LOG_PORT": "8766",
        "DRIVING_LOG_PUBLIC_HOST": public_host,
        "DRIVING_LOG_PUBLIC_SCHEME": public_scheme,
        "DRIVING_LOG_OPERATION_SECRET": secret,
    }
    selected_external = (
        str(external_archive_dir.expanduser().resolve())
        if external_archive_dir
        else existing.get("DRIVING_LOG_EXTERNAL_ARCHIVE_DIR")
    )
    if selected_external:
        environment["DRIVING_LOG_EXTERNAL_ARCHIVE_DIR"] = str(selected_external)
    temporary = environment_path.with_suffix(".tmp")
    temporary.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(environment.items())),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, environment_path)
    source_dir = repo_root / "deploy" / "systemd"
    for name in UNIT_NAMES:
        shutil.copy2(source_dir / name, unit_dir / name)
    run_systemctl("daemon-reload")
    return {
        "environment": str(environment_path),
        "unit_directory": str(unit_dir),
        "public_host": public_host,
    }


def service_snapshot() -> dict[str, object]:
    result: dict[str, object] = {}
    for unit in ("driving-log-web.service", "driving-log-archive.timer"):
        status = run_systemctl(
            "show",
            unit,
            "--property=ActiveState,SubState,NRestarts,UnitFileState",
            check=False,
        )
        result[unit] = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in status.stdout.splitlines()
            if "=" in line
        }
    return result


def tailscale_snapshot() -> dict[str, object]:
    status = subprocess.run(
        ["tailscale", "serve", "status", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode:
        return {"available": False, "error": status.stderr.strip()}
    try:
        return {"available": True, "configuration": json.loads(status.stdout)}
    except json.JSONDecodeError:
        return {"available": True, "raw": status.stdout.strip()}


def replicate_archive(archive: Path, destination: Path) -> Path:
    verify_archive(archive)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = destination / archive.name
    temporary = destination / f".{archive.name}.tmp-{os.getpid()}"
    shutil.copyfile(archive, temporary)
    with temporary.open("rb") as copied:
        os.fsync(copied.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, target)
    directory_fd = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    verify_archive(target)
    return target


def apply_retention(archive_dir: Path) -> list[Path]:
    archives = sorted(archive_dir.glob("driving-log-*.tar.gz"), reverse=True)
    keep = set(archives[:14])
    weekly: set[tuple[int, int]] = set()
    for archive in archives:
        stamp = archive.name.removeprefix("driving-log-").removesuffix(".tar.gz")
        created = None
        for time_format in ("%Y%m%dT%H%M%S%fZ", "%Y%m%dT%H%M%SZ"):
            try:
                created = datetime.strptime(stamp, time_format).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
        if created is None:
            keep.add(archive)
            continue
        week = created.isocalendar()[:2]
        if week not in weekly and len(weekly) < 8:
            weekly.add(week)
            keep.add(archive)
    removed: list[Path] = []
    for archive in archives:
        if archive not in keep and ".pre-migration." not in archive.name:
            archive.unlink()
            removed.append(archive)
    return removed


def process_restore_request(
    operation_id: str, restore_dir: Path, database_path: Path, secret: str
) -> Path:
    uuid.UUID(operation_id)
    operation_dir = restore_dir / operation_id
    request_path = operation_dir / "request.json"
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    signature = str(payload.pop("signature", ""))
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("restore request signature is invalid")
    if float(payload["not_before"]) > time.time():
        raise ValueError("restore request is not active yet")
    archive = Path(str(payload["archive_path"])).resolve()
    if archive.parent != operation_dir.resolve():
        raise ValueError("restore archive must be inside its operation directory")
    verified = verify_archive(archive)
    if verified["archive_sha256"] != payload["archive_sha256"]:
        raise ValueError("restore request archive hash mismatch")
    quarantine = restore_archive(database_path, archive, confirm=True)
    result = {
        "operation_id": operation_id,
        "status": "completed",
        "quarantine": str(quarantine),
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    result_path = operation_dir / "result.json"
    temporary = operation_dir / ".result.json.tmp"
    temporary.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, result_path)
    return result_path
