from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version as package_version
from typing import cast

from driving_log.config import APEX_LATITUDE, APEX_LONGITUDE, DEFAULT_TIMEZONE
from driving_log.db import Database, utc_now_text
from driving_log.solar import SOLAR_CALCULATION_VERSION, week_segments
from driving_log.validation import validate_interval, validate_road_type


class ConflictError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriveInput:
    driver_name: str
    supervisor_name: str | None
    supervisor_dl_number: str | None
    supervisor_dl_state: str | None
    started_at_utc: datetime
    ended_at_utc: datetime
    timezone_name: str = DEFAULT_TIMEZONE
    road_type: str = "unknown"
    weather: str = ""
    notes: str = ""
    source: str = "manual"
    source_reference: str | None = None
    import_batch_id: str | None = None


def _canonical_payload(value: DriveInput) -> dict[str, object]:
    payload = asdict(value)
    payload["started_at_utc"] = value.started_at_utc.astimezone(UTC).isoformat()
    payload["ended_at_utc"] = value.ended_at_utc.astimezone(UTC).isoformat()
    payload["road_type"] = validate_road_type(value.road_type)
    return payload


def payload_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _offset_minutes(value: datetime, timezone_name: str) -> int:
    from zoneinfo import ZoneInfo

    offset = value.astimezone(ZoneInfo(timezone_name)).utcoffset()
    if offset is None:
        raise ValueError("timezone has no UTC offset")
    return int(offset.total_seconds() // 60)


def _drive_values(
    drive_id: str,
    request_id: str | None,
    request_payload_hash: str | None,
    value: DriveInput,
    *,
    live_drive_id: str | None = None,
) -> dict[str, object]:
    payload = _canonical_payload(value)
    start = value.started_at_utc.astimezone(UTC)
    end = value.ended_at_utc.astimezone(UTC)
    interval = validate_interval(start, end, timezone_name=value.timezone_name)
    now = utc_now_text()
    return {
        "id": drive_id,
        "request_id": request_id,
        "request_payload_hash": request_payload_hash,
        "live_drive_id": live_drive_id,
        "driver_name": str(payload["driver_name"]).strip(),
        "supervisor_name": (value.supervisor_name.strip() if value.supervisor_name else None),
        "supervisor_dl_number": (
            value.supervisor_dl_number.strip() if value.supervisor_dl_number else None
        ),
        "supervisor_dl_state": (
            value.supervisor_dl_state.strip().upper() if value.supervisor_dl_state else None
        ),
        "started_at_utc": start.isoformat().replace("+00:00", "Z"),
        "ended_at_utc": end.isoformat().replace("+00:00", "Z"),
        "timezone_name": value.timezone_name,
        "started_utc_offset_minutes": _offset_minutes(start, value.timezone_name),
        "ended_utc_offset_minutes": _offset_minutes(end, value.timezone_name),
        "duration_minutes": interval.duration_minutes,
        "day_minutes": interval.day_minutes,
        "night_minutes": interval.night_minutes,
        "solar_calculation_version": SOLAR_CALCULATION_VERSION,
        "solar_latitude": APEX_LATITUDE,
        "solar_longitude": APEX_LONGITUDE,
        "tzdata_version": package_version("tzdata"),
        "road_type": payload["road_type"],
        "weather": value.weather.strip(),
        "notes": value.notes.strip(),
        "source": value.source,
        "source_reference": value.source_reference,
        "import_batch_id": value.import_batch_id,
        "created_at": now,
        "updated_at": now,
    }


INSERT_DRIVE_SQL = """
INSERT INTO drives (
 id, request_id, request_payload_hash, live_drive_id, driver_name, supervisor_name,
 supervisor_dl_number, supervisor_dl_state, started_at_utc, ended_at_utc, timezone_name,
 started_utc_offset_minutes, ended_utc_offset_minutes, duration_minutes, day_minutes,
 night_minutes, solar_calculation_version, solar_latitude, solar_longitude, tzdata_version,
 road_type, weather, notes, source, source_reference, import_batch_id, created_at, updated_at
) VALUES (
 :id, :request_id, :request_payload_hash, :live_drive_id, :driver_name,
 :supervisor_name, :supervisor_dl_number, :supervisor_dl_state, :started_at_utc,
 :ended_at_utc, :timezone_name, :started_utc_offset_minutes,
 :ended_utc_offset_minutes, :duration_minutes, :day_minutes, :night_minutes,
 :solar_calculation_version, :solar_latitude, :solar_longitude, :tzdata_version,
 :road_type, :weather, :notes, :source, :source_reference, :import_batch_id,
 :created_at, :updated_at
)
"""


class RecordService:
    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        value: DriveInput,
        *,
        request_id: str | None = None,
        drive_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row:
        canonical = _canonical_payload(value)
        digest = payload_hash(canonical)
        selected_id = drive_id or str(uuid.uuid4())
        if connection is not None:
            return self._create_in_transaction(connection, value, selected_id, request_id, digest)
        with self.database.transaction() as transaction:
            return self._create_in_transaction(transaction, value, selected_id, request_id, digest)

    def _create_in_transaction(
        self,
        connection: sqlite3.Connection,
        value: DriveInput,
        drive_id: str,
        request_id: str | None,
        digest: str,
    ) -> sqlite3.Row:
        if request_id:
            existing = connection.execute(
                "SELECT * FROM drives WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing:
                if existing["request_payload_hash"] != digest:
                    raise ConflictError("request ID was already used with different drive data")
                return cast(sqlite3.Row, existing)
        connection.execute(
            INSERT_DRIVE_SQL,
            _drive_values(drive_id, request_id, digest, value),
        )
        self.database.audit(
            connection,
            event_id=str(uuid.uuid4()),
            request_id=request_id,
            action="drive.create",
            payload_hash=digest,
            entity_type="drive",
            entity_id=drive_id,
            outcome="created",
            metadata={"source": value.source},
        )
        return cast(
            sqlite3.Row,
            connection.execute("SELECT * FROM drives WHERE id = ?", (drive_id,)).fetchone(),
        )

    def update(
        self,
        drive_id: str,
        value: DriveInput,
        *,
        expected_version: int,
        request_id: str,
    ) -> sqlite3.Row:
        digest = payload_hash(
            {
                "drive_id": drive_id,
                "drive": _canonical_payload(value),
                "expected_version": expected_version,
            }
        )
        with self.database.transaction() as connection:
            retry = self._mutation_retry(connection, request_id, "drive.update", digest, drive_id)
            if retry:
                current_retry = self.get(drive_id, connection=connection)
                if current_retry["version"] != expected_version + 1:
                    raise ConflictError("the original update result has since changed")
                return current_retry
            current = self.get(drive_id, connection=connection)
            if current["version"] != expected_version:
                raise ConflictError(
                    f"drive version is {current['version']}, expected {expected_version}"
                )
            values = _drive_values(
                drive_id,
                current["request_id"],
                current["request_payload_hash"],
                value,
                live_drive_id=current["live_drive_id"],
            )
            connection.execute(
                """
                UPDATE drives SET
                  driver_name=:driver_name, supervisor_name=:supervisor_name,
                  supervisor_dl_number=:supervisor_dl_number,
                  supervisor_dl_state=:supervisor_dl_state,
                  started_at_utc=:started_at_utc, ended_at_utc=:ended_at_utc,
                  timezone_name=:timezone_name,
                  started_utc_offset_minutes=:started_utc_offset_minutes,
                  ended_utc_offset_minutes=:ended_utc_offset_minutes,
                  duration_minutes=:duration_minutes, day_minutes=:day_minutes,
                  night_minutes=:night_minutes,
                  solar_calculation_version=:solar_calculation_version,
                  solar_latitude=:solar_latitude, solar_longitude=:solar_longitude,
                  tzdata_version=:tzdata_version, road_type=:road_type,
                  weather=:weather, notes=:notes, updated_at=:updated_at,
                  version=version+1
                WHERE id=:id AND version=:expected_version
                """,
                {**values, "expected_version": expected_version},
            )
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="drive.update",
                payload_hash=digest,
                entity_type="drive",
                entity_id=drive_id,
                outcome="updated",
                metadata={
                    "previous_version": expected_version,
                    "result_version": expected_version + 1,
                },
            )
            return self.get(drive_id, connection=connection)

    def delete(self, drive_id: str, *, expected_version: int, request_id: str) -> sqlite3.Row:
        digest = payload_hash({"drive_id": drive_id, "expected_version": expected_version})
        with self.database.transaction() as connection:
            retry = self._mutation_retry(connection, request_id, "drive.delete", digest, drive_id)
            current = self.get(drive_id, include_deleted=True, connection=connection)
            if retry:
                return current
            if current["version"] != expected_version or current["deleted_at"]:
                raise ConflictError("drive changed before deletion")
            now = utc_now_text()
            connection.execute(
                "UPDATE drives SET deleted_at=?, updated_at=?, version=version+1 WHERE id=?",
                (now, now, drive_id),
            )
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="drive.delete",
                payload_hash=digest,
                entity_type="drive",
                entity_id=drive_id,
                outcome="deleted",
            )
            return self.get(drive_id, include_deleted=True, connection=connection)

    @staticmethod
    def _mutation_retry(
        connection: sqlite3.Connection,
        request_id: str,
        action: str,
        digest: str,
        entity_id: str,
    ) -> bool:
        event = connection.execute(
            "SELECT action, payload_hash, entity_id FROM audit_events WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if not event:
            return False
        if (
            event["action"] != action
            or event["payload_hash"] != digest
            or event["entity_id"] != entity_id
        ):
            raise ConflictError("request ID was already used for a different mutation")
        return True

    def get(
        self,
        drive_id: str,
        *,
        include_deleted: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row:
        owned = connection is None
        selected = connection or self.database.connect_readonly()
        try:
            sql = "SELECT * FROM drives WHERE id=?"
            if not include_deleted:
                sql += " AND deleted_at IS NULL"
            row = selected.execute(sql, (drive_id,)).fetchone()
            if not row:
                raise NotFoundError(f"drive not found: {drive_id}")
            return cast(sqlite3.Row, row)
        finally:
            if owned:
                selected.close()

    def list_drives(self, *, include_deleted: bool = False) -> list[sqlite3.Row]:
        connection = self.database.connect_readonly()
        sql = "SELECT * FROM drives"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY started_at_utc DESC, id"
        rows = connection.execute(sql).fetchall()
        connection.close()
        return rows

    def totals(self) -> dict[str, object]:
        connection = self.database.connect_readonly()
        totals = connection.execute(
            """
            SELECT COALESCE(SUM(duration_minutes),0), COALESCE(SUM(day_minutes),0),
                   COALESCE(SUM(night_minutes),0), COUNT(*)
            FROM drives WHERE deleted_at IS NULL
            """
        ).fetchone()
        drives = connection.execute(
            "SELECT id, started_at_utc, ended_at_utc FROM drives "
            "WHERE deleted_at IS NULL ORDER BY started_at_utc, id"
        ).fetchall()
        connection.close()
        weeks: dict[str, dict[str, object]] = {}
        for drive in drives:
            start = datetime.fromisoformat(drive["started_at_utc"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(drive["ended_at_utc"].replace("Z", "+00:00"))
            for week_start, week_end, minutes in week_segments(start, end):
                key = week_start.isoformat()
                week = weeks.setdefault(
                    key,
                    {
                        "week_start_utc": key,
                        "week_end_utc": week_end.isoformat(),
                        "minutes": 0,
                        "drive_ids": [],
                        "first_overage_drive_id": None,
                    },
                )
                current_minutes = week["minutes"]
                assert isinstance(current_minutes, int)
                if current_minutes <= 600 < current_minutes + minutes:
                    week["first_overage_drive_id"] = drive["id"]
                week["minutes"] = current_minutes + minutes
                drive_ids = week["drive_ids"]
                assert isinstance(drive_ids, list)
                drive_ids.append(drive["id"])
        for week in weeks.values():
            current_minutes = week["minutes"]
            assert isinstance(current_minutes, int)
            week["overage_minutes"] = max(0, current_minutes - 600)
        return {
            "total_minutes": totals[0],
            "day_minutes": totals[1],
            "night_minutes": totals[2],
            "drive_count": totals[3],
            "weeks": sorted(weeks.values(), key=lambda item: str(item["week_start_utc"])),
        }

    def warnings_for(self, drive_id: str) -> list[dict[str, str]]:
        connection = self.database.connect_readonly()
        drive = self.get(drive_id, connection=connection)
        warnings: list[dict[str, str]] = []
        if drive["duration_minutes"] > 300:
            warnings.append({"code": "long_drive", "message": "Drive exceeds five hours"})
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(drive["timezone_name"])
        start = datetime.fromisoformat(drive["started_at_utc"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(drive["ended_at_utc"].replace("Z", "+00:00"))
        if start.astimezone(zone).date() != end.astimezone(zone).date():
            warnings.append({"code": "crosses_midnight", "message": "Drive crosses midnight"})
        overlaps = connection.execute(
            """
            SELECT id FROM drives WHERE id != ? AND deleted_at IS NULL
            AND started_at_utc < ? AND ended_at_utc > ? ORDER BY started_at_utc
            """,
            (drive_id, drive["ended_at_utc"], drive["started_at_utc"]),
        ).fetchall()
        persisted = connection.execute(
            "SELECT warning_code, warning_message FROM drive_warnings WHERE drive_id=?",
            (drive_id,),
        ).fetchall()
        connection.close()
        for overlap in overlaps:
            warnings.append(
                {
                    "code": "overlap",
                    "message": f"Overlaps drive {overlap['id']}",
                }
            )
        warnings.extend({"code": row[0], "message": row[1]} for row in persisted)
        return warnings
