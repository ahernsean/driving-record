from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo

from driving_log.db import Database, utc_now_text
from driving_log.records import ConflictError, DriveInput, RecordService, payload_hash


class NoOpenDriveError(RuntimeError):
    pass


class LiveDriveService:
    def __init__(self, database: Database, clock: Callable[[], datetime] | None = None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(UTC))

    def current(self, connection: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        owned = connection is None
        selected = connection or self.database.connect_readonly()
        try:
            return cast(
                sqlite3.Row | None,
                selected.execute(
                    "SELECT * FROM live_drives WHERE status IN ('active','ending') "
                    "ORDER BY created_at LIMIT 1"
                ).fetchone(),
            )
        finally:
            if owned:
                selected.close()

    def find(self, live_id: str) -> sqlite3.Row:
        connection = self.database.connect_readonly()
        try:
            return self.get(live_id, connection)
        finally:
            connection.close()

    @staticmethod
    def get(live_id: str, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM live_drives WHERE id=?", (live_id,)).fetchone()
        if not row:
            raise NoOpenDriveError(f"live drive not found: {live_id}")
        return cast(sqlite3.Row, row)

    def start(
        self,
        *,
        request_id: str,
        driver_name: str = "Daniel Ahern",
        supervisor_name: str | None = "Sean Ahern",
        actor_identity: str | None = None,
    ) -> sqlite3.Row:
        payload = {
            "driver_name": driver_name.strip(),
            "supervisor_name": supervisor_name.strip() if supervisor_name else None,
        }
        digest = payload_hash(payload)
        with self.database.transaction() as connection:
            retry = connection.execute(
                "SELECT * FROM live_drives WHERE request_id=?", (request_id,)
            ).fetchone()
            if retry:
                if retry["request_payload_hash"] != digest:
                    raise ConflictError("start request was reused with different data")
                return cast(sqlite3.Row, retry)
            if self.current(connection):
                raise ConflictError("another live drive is already active")
            now = self.clock().astimezone(UTC)
            offset = now.astimezone(ZoneInfo("America/New_York")).utcoffset()
            assert offset is not None
            live_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO live_drives (
                  id, request_id, request_payload_hash, driver_name, supervisor_name,
                  started_at_utc, timezone_name, started_utc_offset_minutes,
                  started_from, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'America/New_York', ?, 'web', 'active', ?)
                """,
                (
                    live_id,
                    request_id,
                    digest,
                    payload["driver_name"],
                    payload["supervisor_name"],
                    now.isoformat().replace("+00:00", "Z"),
                    int(offset.total_seconds() // 60),
                    utc_now_text(),
                ),
            )
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="live.start",
                payload_hash=digest,
                entity_type="live_drive",
                entity_id=live_id,
                outcome="active",
                actor_identity=actor_identity,
            )
            return self.get(live_id, connection)

    def end(
        self,
        live_id: str,
        *,
        request_id: str,
        actor_identity: str | None = None,
    ) -> sqlite3.Row:
        with self.database.transaction() as connection:
            row = self.get(live_id, connection)
            if row["status"] == "ending":
                if row["end_request_id"] != request_id:
                    raise ConflictError("drive is already ending from another request")
                return row
            if row["status"] in ("completed", "cancelled"):
                return row
            if row["status"] != "active":
                raise ConflictError(f"cannot end drive in state {row['status']}")
            now = self.clock().astimezone(UTC)
            offset = now.astimezone(ZoneInfo(row["timezone_name"])).utcoffset()
            assert offset is not None
            connection.execute(
                """
                UPDATE live_drives SET status='ending', provisional_ended_at_utc=?,
                  provisional_ended_utc_offset_minutes=?, end_request_id=?
                WHERE id=? AND status='active'
                """,
                (
                    now.isoformat().replace("+00:00", "Z"),
                    int(offset.total_seconds() // 60),
                    request_id,
                    live_id,
                ),
            )
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="live.end",
                payload_hash=payload_hash({"live_id": live_id}),
                entity_type="live_drive",
                entity_id=live_id,
                outcome="ending",
                actor_identity=actor_identity,
            )
            return self.get(live_id, connection)

    def resume(
        self, live_id: str, *, request_id: str, actor_identity: str | None = None
    ) -> sqlite3.Row:
        digest = payload_hash({"live_id": live_id})
        with self.database.transaction() as connection:
            event = connection.execute(
                "SELECT action, payload_hash FROM audit_events WHERE request_id=?",
                (request_id,),
            ).fetchone()
            row = self.get(live_id, connection)
            if event:
                if event["action"] != "live.resume" or event["payload_hash"] != digest:
                    raise ConflictError("resume request ID conflict")
                return row
            if row["status"] != "ending":
                raise ConflictError("only an ending drive can be resumed")
            connection.execute(
                """
                UPDATE live_drives SET status='active', provisional_ended_at_utc=NULL,
                 provisional_ended_utc_offset_minutes=NULL, end_request_id=NULL
                WHERE id=?
                """,
                (live_id,),
            )
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="live.resume",
                payload_hash=digest,
                entity_type="live_drive",
                entity_id=live_id,
                outcome="active",
                actor_identity=actor_identity,
            )
            return self.get(live_id, connection)

    def cancel(
        self, live_id: str, *, request_id: str, actor_identity: str | None = None
    ) -> sqlite3.Row:
        digest = payload_hash({"live_id": live_id})
        with self.database.transaction() as connection:
            event = connection.execute(
                "SELECT action, payload_hash FROM audit_events WHERE request_id=?",
                (request_id,),
            ).fetchone()
            row = self.get(live_id, connection)
            if event:
                if event["action"] != "live.cancel" or event["payload_hash"] != digest:
                    raise ConflictError("cancel request ID conflict")
                return row
            if row["status"] == "completed":
                raise ConflictError("completed drive cannot be cancelled")
            if row["status"] == "cancelled":
                return row
            connection.execute(
                "UPDATE live_drives SET status='cancelled', finalized_at=? WHERE id=?",
                (utc_now_text(), live_id),
            )
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="live.cancel",
                payload_hash=digest,
                entity_type="live_drive",
                entity_id=live_id,
                outcome="cancelled",
                actor_identity=actor_identity,
            )
            return self.get(live_id, connection)

    def finalize(
        self,
        live_id: str,
        *,
        request_id: str,
        road_type: str,
        weather: str = "",
        notes: str = "",
        supervisor_name: str | None = None,
        supervisor_dl_number: str | None = None,
        supervisor_dl_state: str | None = None,
        corrected_start_utc: datetime | None = None,
        corrected_end_utc: datetime | None = None,
        actor_identity: str | None = None,
    ) -> sqlite3.Row:
        payload = {
            "road_type": road_type,
            "weather": weather,
            "notes": notes,
            "supervisor_name": supervisor_name,
            "supervisor_dl_number": supervisor_dl_number,
            "supervisor_dl_state": supervisor_dl_state,
            "corrected_start_utc": (
                corrected_start_utc.isoformat() if corrected_start_utc else None
            ),
            "corrected_end_utc": corrected_end_utc.isoformat() if corrected_end_utc else None,
        }
        digest = payload_hash(payload)
        with self.database.transaction() as connection:
            row = self.get(live_id, connection)
            if row["status"] == "completed":
                if row["finalization_payload_hash"] != digest:
                    raise ConflictError("live drive was finalized with different data")
                return cast(
                    sqlite3.Row,
                    connection.execute(
                        "SELECT * FROM drives WHERE id=?", (row["completed_drive_id"],)
                    ).fetchone(),
                )
            if row["status"] != "ending" or not row["provisional_ended_at_utc"]:
                raise ConflictError("live drive must be ending before finalization")
            start = corrected_start_utc or datetime.fromisoformat(
                row["started_at_utc"].replace("Z", "+00:00")
            )
            end = corrected_end_utc or datetime.fromisoformat(
                row["provisional_ended_at_utc"].replace("Z", "+00:00")
            )
            if (end - start).total_seconds() < 30:
                raise ValueError("drive must be at least 30 seconds")
            start = start.replace(second=0, microsecond=0)
            end = end.replace(second=0, microsecond=0)
            drive_id = str(uuid.uuid5(uuid.UUID(live_id), "completed-drive"))
            drive = RecordService(self.database).create(
                DriveInput(
                    driver_name=row["driver_name"],
                    supervisor_name=supervisor_name or row["supervisor_name"],
                    supervisor_dl_number=supervisor_dl_number,
                    supervisor_dl_state=supervisor_dl_state,
                    started_at_utc=start,
                    ended_at_utc=end,
                    timezone_name=row["timezone_name"],
                    road_type=road_type,
                    weather=weather,
                    notes=notes,
                    source="live_drive",
                    source_reference=f"live drive {live_id}",
                ),
                drive_id=drive_id,
                live_drive_id=live_id,
                connection=connection,
            )
            connection.execute(
                """
                UPDATE live_drives SET status='completed', completed_drive_id=?,
                  finalization_payload_hash=?, finalized_at=? WHERE id=?
                """,
                (drive_id, digest, utc_now_text(), live_id),
            )
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="live.finalize",
                payload_hash=digest,
                entity_type="live_drive",
                entity_id=live_id,
                outcome="completed",
                metadata={"drive_id": drive_id},
                actor_identity=actor_identity,
            )
            return drive
