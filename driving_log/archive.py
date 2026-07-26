from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from driving_log import __version__
from driving_log.db import Database
from driving_log.migrations import LATEST_SCHEMA_VERSION

ARCHIVE_FORMAT = "driving-log-archive-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_archive(database: Database, archive_dir: Path, out: Path | None = None) -> Path:
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_dir = out.parent if out else archive_dir
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out or target_dir / f"driving-log-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(dir=target_dir) as temporary:
        workspace = Path(temporary)
        snapshot = workspace / "database.sqlite3"
        source = database.connect()
        destination = sqlite3.connect(snapshot)
        source.backup(destination)
        destination.close()
        source.close()
        snapshot_connection = sqlite3.connect(snapshot)
        check = snapshot_connection.execute("PRAGMA quick_check").fetchone()[0]
        snapshot_connection.close()
        if check != "ok":
            raise RuntimeError(f"snapshot quick_check failed: {check}")
        manifest = {
            "archive_format": ARCHIVE_FORMAT,
            "application_version": __version__,
            "schema_version": LATEST_SCHEMA_VERSION,
            "created_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "database_size": snapshot.stat().st_size,
            "database_sha256": _sha256(snapshot),
        }
        (workspace / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary_bundle = target_dir / f".{target.name}.tmp-{os.getpid()}"
        with tarfile.open(temporary_bundle, "w:gz") as bundle:
            bundle.add(snapshot, arcname="database.sqlite3")
            bundle.add(workspace / "manifest.json", arcname="manifest.json")
        with temporary_bundle.open("rb") as bundle_file:
            os.fsync(bundle_file.fileno())
        os.chmod(temporary_bundle, 0o600)
        os.replace(temporary_bundle, target)
        directory_fd = os.open(target_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    verify_archive(target)
    return target


def verify_archive(path: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        with tarfile.open(path, "r:gz") as bundle:
            names = set(bundle.getnames())
            if names != {"database.sqlite3", "manifest.json"}:
                raise ValueError("archive contains unexpected or missing entries")
            for member in bundle.getmembers():
                if (
                    not member.isfile()
                    or Path(member.name).is_absolute()
                    or ".." in Path(member.name).parts
                ):
                    raise ValueError("archive contains an unsafe entry")
            bundle.extractall(workspace, filter="data")
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("archive_format") != ARCHIVE_FORMAT:
            raise ValueError("unsupported archive format")
        if int(manifest.get("schema_version", -1)) > LATEST_SCHEMA_VERSION:
            raise ValueError("archive schema is newer than this application")
        snapshot = workspace / "database.sqlite3"
        if _sha256(snapshot) != manifest.get("database_sha256"):
            raise ValueError("archive database hash mismatch")
        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        connection.close()
        if check != "ok":
            raise ValueError(f"archive database quick_check failed: {check}")
        return {**manifest, "verified": True, "archive_sha256": _sha256(path)}


def restore_archive(database_path: Path, archive: Path, *, confirm: bool) -> Path:
    if not confirm:
        raise ValueError("restore requires explicit confirmation")
    verify_archive(archive)
    database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    quarantine = database_path.parent / f"quarantine-{stamp}"
    quarantine.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(dir=database_path.parent) as temporary:
        workspace = Path(temporary)
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember("database.sqlite3")
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("archive database is missing")
            restored = workspace / "restored.sqlite3"
            with restored.open("wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        os.chmod(restored, 0o600)
        for suffix in ("", "-wal", "-shm"):
            current = Path(f"{database_path}{suffix}")
            if current.exists():
                os.replace(current, quarantine / current.name)
        os.replace(restored, database_path)
        directory_fd = os.open(database_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    restored_database = Database(database_path)
    restored_database.initialize()
    return quarantine
