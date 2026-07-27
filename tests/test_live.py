from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from driving_log.db import Database
from driving_log.live import LiveDriveService, NoOpenDriveError
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
        with self.assertRaisesRegex(ConflictError, "different data"):
            self.service.start(request_id=request, driver_name="Different Driver")
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
        resume_request = str(uuid.uuid4())
        resumed = self.service.resume(live["id"], request_id=resume_request)
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(
            self.service.resume(live["id"], request_id=resume_request)["status"],
            "active",
        )
        with self.assertRaisesRegex(ConflictError, "only an ending"):
            self.service.resume(live["id"], request_id=str(uuid.uuid4()))
        cancel_request = str(uuid.uuid4())
        cancelled = self.service.cancel(live["id"], request_id=cancel_request)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            self.service.cancel(live["id"], request_id=cancel_request)["status"],
            "cancelled",
        )
        self.assertEqual(
            self.service.cancel(live["id"], request_id=str(uuid.uuid4()))["status"],
            "cancelled",
        )
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
            end_location="Apex Friendship High School",
        )
        retry = self.service.finalize(
            live["id"],
            request_id=final_request,
            road_type="mixed",
            weather="rain",
            end_location="Apex Friendship High School",
        )
        self.assertEqual(drive["id"], retry["id"])
        self.assertEqual(drive["duration_minutes"], 42)
        self.assertEqual(drive["end_location"], "Apex Friendship High School")
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

    def test_finalize_can_correct_both_start_and_end(self) -> None:
        live = self.service.start(request_id=str(uuid.uuid4()))
        self.clock.value += timedelta(minutes=30)
        self.service.end(live["id"], request_id=str(uuid.uuid4()))
        corrected_start = datetime(2026, 7, 26, 11, 55, tzinfo=UTC)
        corrected_end = datetime(2026, 7, 26, 12, 35, tzinfo=UTC)

        drive = self.service.finalize(
            live["id"],
            request_id=str(uuid.uuid4()),
            road_type="local",
            corrected_start_utc=corrected_start,
            corrected_end_utc=corrected_end,
        )

        self.assertEqual(drive["started_at_utc"], "2026-07-26T11:55:00Z")
        self.assertEqual(drive["ended_at_utc"], "2026-07-26T12:35:00Z")
        self.assertEqual(drive["duration_minutes"], 40)

    def test_missing_invalid_and_completed_state_guards(self) -> None:
        with (
            self.database.transaction() as connection,
            self.assertRaises(NoOpenDriveError),
        ):
            self.service.get(str(uuid.uuid4()), connection)

        live = self.service.start(request_id=str(uuid.uuid4()))
        self.clock.value += timedelta(minutes=30)
        self.service.end(live["id"], request_id=str(uuid.uuid4()))
        self.service.finalize(
            live["id"],
            request_id=str(uuid.uuid4()),
            road_type="local",
        )
        self.assertEqual(
            self.service.end(live["id"], request_id=str(uuid.uuid4()))["status"],
            "completed",
        )
        with self.assertRaisesRegex(ConflictError, "cannot be cancelled"):
            self.service.cancel(live["id"], request_id=str(uuid.uuid4()))

    def test_request_ids_are_bound_to_resume_and_cancel_actions(self) -> None:
        live = self.service.start(request_id=str(uuid.uuid4()))
        self.clock.value += timedelta(minutes=5)
        self.service.end(live["id"], request_id=str(uuid.uuid4()))
        reused = str(uuid.uuid4())
        with self.database.transaction() as connection:
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=reused,
                action="different.action",
                payload_hash="different",
                entity_type="live_drive",
                entity_id=live["id"],
                outcome="test",
            )
        with self.assertRaisesRegex(ConflictError, "resume request ID"):
            self.service.resume(live["id"], request_id=reused)
        with self.assertRaisesRegex(ConflictError, "cancel request ID"):
            self.service.cancel(live["id"], request_id=reused)


if __name__ == "__main__":
    unittest.main()
