from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import sqlite3
import uuid
from datetime import datetime
from typing import cast

from driving_log.db import Database, utc_now_text
from driving_log.records import ConflictError, DriveInput, RecordService

FORMAT_VERSION = "1"
CSV_COLUMNS = (
    "format_version",
    "id",
    "driver_name",
    "supervisor_name",
    "supervisor_dl_number",
    "supervisor_dl_state",
    "started_at_utc",
    "ended_at_utc",
    "timezone_name",
    "started_utc_offset_minutes",
    "ended_utc_offset_minutes",
    "duration_minutes",
    "day_minutes",
    "night_minutes",
    "solar_calculation_version",
    "solar_latitude",
    "solar_longitude",
    "tzdata_version",
    "road_type",
    "weather",
    "notes",
    "source",
    "source_reference",
)


def _export_row(row: sqlite3.Row) -> dict[str, str]:
    return {
        column: FORMAT_VERSION
        if column == "format_version"
        else str(row[column] if row[column] is not None else "")
        for column in CSV_COLUMNS
    }


def export_csv(database: Database) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in RecordService(database).list_drives():
        writer.writerow(_export_row(row))
    return output.getvalue().encode("utf-8")


def import_csv(database: Database, content: bytes, source_name: str) -> dict[str, object]:
    digest = hashlib.sha256(content).hexdigest()
    connection = database.connect()
    existing = connection.execute(
        "SELECT summary_json, status FROM import_batches WHERE content_sha256=?", (digest,)
    ).fetchone()
    connection.close()
    if existing:
        if existing["status"] != "completed":
            raise ConflictError("matching prior import is incomplete")
        return cast(dict[str, object], json.loads(existing["summary_json"]))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
        raise ValueError("CSV headers do not match format version 1")
    raw_rows = list(reader)
    ids = [row["id"] for row in raw_rows]
    if len(ids) != len(set(ids)):
        raise ValueError("CSV contains duplicate drive IDs")
    candidates: list[tuple[dict[str, str], DriveInput]] = []
    for number, row in enumerate(raw_rows, 2):
        if row["format_version"] != FORMAT_VERSION:
            raise ValueError(f"row {number}: unsupported format version")
        try:
            value = DriveInput(
                driver_name=row["driver_name"],
                supervisor_name=row["supervisor_name"] or None,
                supervisor_dl_number=row["supervisor_dl_number"] or None,
                supervisor_dl_state=row["supervisor_dl_state"] or None,
                started_at_utc=datetime.fromisoformat(row["started_at_utc"].replace("Z", "+00:00")),
                ended_at_utc=datetime.fromisoformat(row["ended_at_utc"].replace("Z", "+00:00")),
                timezone_name=row["timezone_name"],
                road_type=row["road_type"],
                weather=row["weather"],
                notes=row["notes"],
                source=row["source"],
                source_reference=row["source_reference"] or None,
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"row {number}: invalid drive data: {exc}") from exc
        candidates.append((row, value))
    batch_id = str(uuid.uuid4())
    created = 0
    skipped = 0
    service = RecordService(database)
    with database.transaction() as transaction:
        transaction.execute(
            """
            INSERT INTO import_batches VALUES (?, 'csv', ?, ?, ?, ?, ?, 'applying', '{}')
            """,
            (
                batch_id,
                source_name,
                digest,
                FORMAT_VERSION,
                utc_now_text(),
                gzip.compress(content),
            ),
        )
        for index, (raw, value) in enumerate(candidates, 1):
            current = transaction.execute(
                "SELECT * FROM drives WHERE id=?", (raw["id"],)
            ).fetchone()
            if current:
                exported = _export_row(current)
                if exported != raw:
                    raise ConflictError(f"drive {raw['id']} already exists with different content")
                result_id = current["id"]
                status = "unchanged"
                skipped += 1
            else:
                imported_value = DriveInput(**{**value.__dict__, "import_batch_id": batch_id})
                result = service.create(imported_value, drive_id=raw["id"], connection=transaction)
                if _export_row(result) != raw:
                    raise ValueError(
                        f"drive {raw['id']} contains inconsistent derived or context fields"
                    )
                result_id = result["id"]
                status = "created"
                created += 1
            transaction.execute(
                """
                INSERT INTO import_rows VALUES (?, ?, 'csv', ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    str(uuid.uuid4()),
                    batch_id,
                    digest,
                    str(index),
                    json.dumps(raw, sort_keys=True),
                    json.dumps(raw, sort_keys=True),
                    result_id,
                    status,
                ),
            )
        summary = {"batch_id": batch_id, "created": created, "skipped": skipped, "failed": 0}
        transaction.execute(
            "UPDATE import_batches SET status='completed', summary_json=? WHERE id=?",
            (json.dumps(summary, sort_keys=True), batch_id),
        )
    return summary
