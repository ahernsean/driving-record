from __future__ import annotations

import gzip
import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from driving_log.config import DEFAULT_TIMEZONE
from driving_log.db import Database, utc_now_text
from driving_log.records import ConflictError, DriveInput, RecordService
from driving_log.solar import (
    AmbiguousLocalTimeError,
    NonexistentLocalTimeError,
    resolve_local,
)

SEED_NAMESPACE = uuid.UUID("39f4c819-c091-47ac-a8b8-b3be06c51be3")
PDF_LINE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<time>\d{1,2}:\d{2}\s+[AP]M)\s+"
    r"(?P<details>.+?)\s+Sean Ahern\s*$"
)
LOG_LINE = re.compile(
    r"^\*\s*(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<start>\d{1,2}:\d{2})(?P<start_ampm>\s*[AP]M)?"
    r"(?:\s*-\s*(?P<end>\d{1,2}:\d{2})(?P<end_ampm>\s*[AP]M)?)?"
    r"(?P<details>.*)$"
)


@dataclass(frozen=True)
class SeedCandidate:
    source_type: str
    source_name: str
    row_key: str
    raw_text: str
    drive_id: str
    drive: DriveInput
    source_day_minutes: int | None
    source_night_minutes: int | None
    warnings: tuple[tuple[str, str], ...] = ()


def _stable_id(source_name: str, row_key: str) -> str:
    return str(uuid.uuid5(SEED_NAMESPACE, f"{source_name}:{row_key}"))


def _resolve_seed_local(value: datetime) -> tuple[datetime, tuple[str, str] | None]:
    try:
        return resolve_local(value), None
    except AmbiguousLocalTimeError:
        return resolve_local(value, fold=0), (
            "seed_ambiguous_local_time",
            "Repeated local time was interpreted as the first occurrence",
        )
    except NonexistentLocalTimeError:
        zone = ZoneInfo(DEFAULT_TIMEZONE)
        normalized = value.replace(tzinfo=zone, fold=0).astimezone(UTC).astimezone(zone)
        return normalized, (
            "seed_nonexistent_local_time",
            f"Nonexistent local time was normalized forward to {normalized:%Y-%m-%d %H:%M}",
        )


def _road_type_from_text(text: str) -> str:
    description = text.lower()
    has_local = "local" in description
    has_highway = "highway" in description
    if has_local and has_highway:
        return "mixed"
    if has_highway:
        return "highway"
    if has_local:
        return "local"
    return "unknown"


def parse_pdf_text(text: str, source_name: str) -> list[SeedCandidate]:
    candidates: list[SeedCandidate] = []
    for line in text.splitlines():
        match = PDF_LINE.match(line.strip())
        if not match:
            continue
        details = match.group("details")
        durations = re.findall(r"(\d+)h\s+(\d+)m", details)
        if len(durations) != 1:
            raise ValueError(f"unexpected PDF duration: {line}")
        hours, minutes = map(int, durations[0])
        duration = hours * 60 + minutes
        local_start_naive = datetime.strptime(
            f"{match.group('date')} {match.group('time')}", "%m/%d/%Y %I:%M %p"
        )
        local_start, local_time_warning = _resolve_seed_local(local_start_naive)
        source_night = duration if re.search(r"-\s+\d+h\s+\d+m", details) else None
        source_day = None if source_night is not None else duration
        road_type = _road_type_from_text(details)
        row_key = str(len(candidates) + 1)
        candidates.append(
            SeedCandidate(
                source_type="seed_pdf",
                source_name=source_name,
                row_key=row_key,
                raw_text=line.strip(),
                drive_id=_stable_id(source_name, row_key),
                drive=DriveInput(
                    driver_name="Daniel Ahern",
                    supervisor_name="Sean Ahern",
                    supervisor_dl_number=None,
                    supervisor_dl_state=None,
                    started_at_utc=local_start.astimezone(UTC),
                    ended_at_utc=local_start.astimezone(UTC) + timedelta(minutes=duration),
                    road_type=road_type,
                    source="seed_pdf",
                    source_reference=f"{source_name} row {row_key}",
                ),
                source_day_minutes=source_day,
                source_night_minutes=source_night,
                warnings=(local_time_warning,) if local_time_warning else (),
            )
        )
    if not candidates:
        raise ValueError("no drive rows found in PDF text")
    duplicate_groups: dict[tuple[datetime, datetime], list[int]] = {}
    for index, candidate in enumerate(candidates):
        key = (candidate.drive.started_at_utc, candidate.drive.ended_at_utc)
        duplicate_groups.setdefault(key, []).append(index)
    for indexes in duplicate_groups.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            candidate = candidates[index]
            candidates[index] = SeedCandidate(
                **{
                    **candidate.__dict__,
                    "warnings": candidate.warnings
                    + (
                        (
                            "seed_possible_duplicate",
                            "Source contains another entry with the same start and duration",
                        ),
                    ),
                }
            )
    return candidates


def parse_log_text(text: str, source_name: str) -> list[SeedCandidate]:
    candidates: list[SeedCandidate] = []
    for line in text.splitlines():
        match = LOG_LINE.match(line.strip())
        if not match:
            continue
        start_text = f"{match.group('date')} {match.group('start')}"
        start_format = "%Y-%m-%d %H:%M"
        if match.group("start_ampm"):
            start_text += f" {match.group('start_ampm').strip()}"
            start_format = "%Y-%m-%d %I:%M %p"
        start_naive = datetime.strptime(start_text, start_format)
        local_start, start_warning = _resolve_seed_local(start_naive)
        details = match.group("details")
        duration_match = re.search(
            r"(?:(\d+):(\d+)\s*(?:hours?|hrs?)|\b(\d+)\s*min(?:utes?)?)", details
        )
        written_duration: int | None = None
        if duration_match:
            if duration_match.group(3):
                written_duration = int(duration_match.group(3))
            else:
                written_duration = int(duration_match.group(1)) * 60 + int(duration_match.group(2))
        end_text = match.group("end")
        local_end: datetime | None = None
        if end_text:
            end_format = "%H:%M"
            if match.group("end_ampm"):
                end_text += f" {match.group('end_ampm').strip()}"
                end_format = "%I:%M %p"
            end_time = datetime.strptime(end_text, end_format).time()
            local_end, end_warning = _resolve_seed_local(
                datetime.combine(start_naive.date(), end_time)
            )
            if local_end <= local_start:
                local_end += timedelta(days=1)
        else:
            end_warning = None
        warnings: list[tuple[str, str]] = []
        if start_warning:
            warnings.append(start_warning)
        if end_warning:
            warnings.append(end_warning)
        if written_duration is None and local_end is not None:
            written_duration = round((local_end - local_start).total_seconds() / 60)
            warnings.append(
                (
                    "seed_ambiguous_duration",
                    "Duration was inferred from the written start and end timestamps",
                )
            )
        if written_duration is None:
            raise ValueError(f"log row has no usable duration: {line}")
        computed_end = (
            local_start.astimezone(UTC) + timedelta(minutes=written_duration)
        ).astimezone(local_start.tzinfo)
        if local_end is not None and local_end != computed_end:
            warnings.append(
                (
                    "seed_ambiguous_duration",
                    "Written duration does not match start and end timestamps; duration was used",
                )
            )
        description = details.lower()
        road_type = _road_type_from_text(details)
        weather_terms = ("rain", "fog", "clear", "cloud", "wet")
        weather = ", ".join(term for term in weather_terms if term in description)
        row_key = str(len(candidates) + 1)
        candidates.append(
            SeedCandidate(
                source_type="seed_log_txt",
                source_name=source_name,
                row_key=row_key,
                raw_text=line.strip(),
                drive_id=_stable_id(source_name, row_key),
                drive=DriveInput(
                    driver_name="Daniel Ahern",
                    supervisor_name="Sean Ahern",
                    supervisor_dl_number=None,
                    supervisor_dl_state=None,
                    started_at_utc=local_start.astimezone(UTC),
                    ended_at_utc=computed_end.astimezone(UTC),
                    road_type=road_type,
                    weather=weather,
                    notes=details.strip(" ,:"),
                    source="seed_log_txt",
                    source_reference=f"{source_name} row {row_key}",
                ),
                source_day_minutes=None,
                source_night_minutes=None,
                warnings=tuple(warnings),
            )
        )
    if not candidates:
        raise ValueError("no drive rows found in text log")
    return candidates


def extract_pdf(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "PDF seed import requires pdftotext; install the poppler-utils package"
        ) from exc
    return result.stdout


def preview_seed(pdf_path: Path, log_path: Path) -> dict[str, object]:
    pdf_text = extract_pdf(pdf_path)
    log_text = log_path.read_text(encoding="utf-8")
    pdf = parse_pdf_text(pdf_text, pdf_path.name)
    log = parse_log_text(log_text, log_path.name)
    return {
        "pdf_rows": len(pdf),
        "log_rows": len(log),
        "total_rows": len(pdf) + len(log),
        "raw_minutes": sum(
            round((item.drive.ended_at_utc - item.drive.started_at_utc).total_seconds() / 60)
            for item in (*pdf, *log)
        ),
        "warnings": sum(len(item.warnings) for item in (*pdf, *log)),
    }


def apply_seed(database: Database, pdf_path: Path, log_path: Path) -> dict[str, object]:
    sources = (
        (
            pdf_path.name,
            pdf_path.read_bytes(),
            parse_pdf_text(extract_pdf(pdf_path), pdf_path.name),
        ),
        (
            log_path.name,
            log_path.read_bytes(),
            parse_log_text(log_path.read_text(encoding="utf-8"), log_path.name),
        ),
    )
    service = RecordService(database)
    inserted = 0
    unchanged = 0
    warning_count = 0
    for source_name, raw_content, candidates in sources:
        digest = hashlib.sha256(raw_content).hexdigest()
        with database.transaction() as connection:
            existing = connection.execute(
                "SELECT summary_json FROM import_batches WHERE content_sha256=?", (digest,)
            ).fetchone()
            if existing:
                summary = json.loads(existing["summary_json"])
                unchanged += int(summary["created"]) + int(summary["unchanged"])
                continue
            batch_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO import_batches VALUES (?, ?, ?, ?, '1', ?, ?, 'applying', '{}')
                """,
                (
                    batch_id,
                    candidates[0].source_type,
                    source_name,
                    digest,
                    utc_now_text(),
                    gzip.compress(raw_content),
                ),
            )
            source_inserted = 0
            source_unchanged = 0
            for candidate in candidates:
                row = connection.execute(
                    "SELECT id, raw_text FROM import_rows "
                    "WHERE source_type=? AND source_instance_id=? "
                    "AND source_row_key=?",
                    (candidate.source_type, source_name, candidate.row_key),
                ).fetchone()
                if row:
                    if row["raw_text"] != candidate.raw_text:
                        raise ConflictError(
                            f"{source_name} row {candidate.row_key} changed since prior import"
                        )
                    source_unchanged += 1
                    continue
                imported = DriveInput(**{**candidate.drive.__dict__, "import_batch_id": batch_id})
                drive = service.create(imported, drive_id=candidate.drive_id, connection=connection)
                parsed = {
                    "started_at_utc": drive["started_at_utc"],
                    "ended_at_utc": drive["ended_at_utc"],
                    "source_day_minutes": candidate.source_day_minutes,
                    "source_night_minutes": candidate.source_night_minutes,
                }
                connection.execute(
                    """
                    INSERT INTO import_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', NULL)
                    """,
                    (
                        str(uuid.uuid4()),
                        batch_id,
                        candidate.source_type,
                        source_name,
                        candidate.row_key,
                        candidate.raw_text,
                        json.dumps(parsed, sort_keys=True),
                        drive["id"],
                    ),
                )
                warnings = list(candidate.warnings)
                if (candidate.source_day_minutes is not None and drive["night_minutes"] > 0) or (
                    candidate.source_night_minutes is not None and drive["day_minutes"] > 0
                ):
                    warnings.append(
                        (
                            "seed_day_night_mismatch",
                            "Source day/night column differs from Apex solar classification",
                        )
                    )
                for code, message in warnings:
                    if code == "seed_possible_duplicate":
                        continue
                    connection.execute(
                        "INSERT OR IGNORE INTO drive_warnings VALUES (?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), drive["id"], code, message, utc_now_text()),
                    )
                warning_count += len(warnings)
                source_inserted += 1
            summary = {
                "created": source_inserted,
                "unchanged": source_unchanged,
                "failed": 0,
            }
            connection.execute(
                "UPDATE import_batches SET status='completed', summary_json=? WHERE id=?",
                (json.dumps(summary, sort_keys=True), batch_id),
            )
            inserted += source_inserted
            unchanged += source_unchanged
    totals = service.totals()
    return {
        "created": inserted,
        "unchanged": unchanged,
        "warnings": warning_count,
        **totals,
    }
