from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from driving_log.db import Database
from driving_log.live import LiveDriveService
from driving_log.records import ConflictError, RecordService


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class LiveDriveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "driving.sqlite3")
        self.database.initialize()
        self.clock = MutableClock(datetime(2026, 7, 26, 12, 0, 15, tzinfo=UTC))
        self.service = LiveDriveService(self.database, self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_is_idempotent_and_singleton_survives_new_service(self) -> None:
        request = str(uuid.uuid4())
        first = self.service.start(request_id=request)
        retry = self.service.start(request_id=request)
        self.assertEqual(first["id"], retry["id"])
        with self.assertRaises(ConflictError):
            self.service.start(request_id=str(uuid.uuid4()))
        recovered = LiveDriveService(self.database).current()
        self.assertEqual(recovered["id"], first["id"])  # type: ignore[index]

    def test_ending_recovery_resume_and_cancel(self) -> None:
        live = self.service.start(request_id=str(uuid.uuid4()))
        self.clock.value += timedelta(minutes=20)
        end_request = str(uuid.uuid4())
        ending = self.service.end(live["id"], request_id=end_request)
        self.assertEqual(ending["status"], "ending")
        self.assertEqual(
            self.service.end(live["id"], request_id=end_request)["provisional_ended_at_utc"],
            ending["provisional_ended_at_utc"],
        )
        with self.assertRaises(ConflictError):
            self.service.end(live["id"], request_id=str(uuid.uuid4()))
        resumed = self.service.resume(live["id"], request_id=str(uuid.uuid4()))
        self.assertEqual(resumed["status"], "active")
        cancelled = self.service.cancel(live["id"], request_id=str(uuid.uuid4()))
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(RecordService(self.database).totals()["drive_count"], 0)

    def test_finalize_is_atomic_stable_and_idempotent(self) -> None:
        live = self.service.start(request_id=str(uuid.uuid4()))
        self.clock.value += timedelta(minutes=42, seconds=20)
        self.service.end(live["id"], request_id=str(uuid.uuid4()))
        final_request = str(uuid.uuid4())
        drive = self.service.finalize(
            live["id"],
            request_id=final_request,
            road_type="mixed",
            weather="rain",
        )
        retry = self.service.finalize(
            live["id"],
            request_id=final_request,
            road_type="mixed",
            weather="rain",
        )
        self.assertEqual(drive["id"], retry["id"])
        self.assertEqual(drive["duration_minutes"], 42)
        self.assertEqual(len(RecordService(self.database).list_drives()), 1)
        with self.assertRaises(ConflictError):
            self.service.finalize(
                live["id"],
                request_id=str(uuid.uuid4()),
                road_type="highway",
            )

    def test_invalid_finalize_rolls_back_completed_identity(self) -> None:
        live = self.service.start(request_id=str(uuid.uuid4()))
        self.clock.value += timedelta(seconds=10)
        self.service.end(live["id"], request_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, "30 seconds"):
            self.service.finalize(
                live["id"],
                request_id=str(uuid.uuid4()),
                road_type="local",
            )
        recovered = self.service.current()
        self.assertEqual(recovered["status"], "ending")  # type: ignore[index]
        self.assertEqual(RecordService(self.database).list_drives(), [])


if __name__ == "__main__":
    unittest.main()
