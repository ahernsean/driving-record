from __future__ import annotations

import re
import tempfile
import unittest
import uuid
from pathlib import Path

import anyio
import httpx

from driving_log.app import create_app
from driving_log.config import Settings
from driving_log.web import _format_local_datetime


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        state = Path(self.temporary.name)
        self.settings = Settings(
            state_dir=state,
            database_path=state / "database.sqlite3",
            archive_dir=state / "archives",
            restore_dir=state / "restore",
            host="127.0.0.1",
            port=8766,
            public_host="testserver",
        )
        self.app = create_app(self.settings)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_async(self, function: object) -> None:
        anyio.run(function)  # type: ignore[arg-type]

    def test_dashboard_has_no_login_and_manual_record_flow(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as client,
            ):
                dashboard = await client.get("/")
                self.assertEqual(dashboard.status_code, 200)
                self.assertEqual(dashboard.headers["cache-control"], "no-store")
                self.assertIn("Start a drive", dashboard.text)
                self.assertNotIn("login", dashboard.text.lower())
                self.assertRegex(
                    dashboard.text,
                    r"/static/app\.css\?v=[0-9a-f]{12}",
                )
                response = await client.post(
                    "/drives",
                    headers={"Origin": "http://testserver"},
                    data={
                        "request_id": str(uuid.uuid4()),
                        "driver_name": "Daniel Ahern",
                        "supervisor_name": "Sean Ahern",
                        "started_at_local": "2026-07-20T12:00",
                        "ended_at_local": "2026-07-20T12:30",
                        "road_type": "local",
                    },
                )
                self.assertEqual(response.status_code, 303)
                detail = await client.get(response.headers["location"])
                self.assertIn("30m", detail.text)
                self.assertIn("Monday, Jul 20, 2026 at 12:00 PM EDT", detail.text)
                self.assertIn("Monday, Jul 20, 2026 at 12:30 PM EDT", detail.text)
                self.assertNotIn("Started (UTC)", detail.text)
                listing = await client.get("/drives")
                self.assertIn("Drive history", listing.text)
                self.assertIn("local", listing.text)
                self.assertIn("Monday, Jul 20, 2026 at 12:00 PM EDT", listing.text)

        self.run_async(scenario)

    def test_timestamp_formatter_uses_local_date_and_dst_offset(self) -> None:
        self.assertEqual(
            _format_local_datetime("2026-07-20T02:30:00Z"),
            "Sunday, Jul 19, 2026 at 10:30 PM EDT",
        )
        self.assertEqual(
            _format_local_datetime("2026-01-20T17:30:00Z"),
            "Tuesday, Jan 20, 2026 at 12:30 PM EST",
        )

    def test_live_drive_recovers_in_fresh_client_and_finalizes_once(self) -> None:
        async def scenario() -> None:
            async with self.app.router.lifespan_context(self.app):
                transport = httpx.ASGITransport(app=self.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as first:
                    start = await first.post(
                        "/live/start",
                        headers={"Origin": "http://testserver"},
                        data={
                            "request_id": str(uuid.uuid4()),
                        },
                    )
                    self.assertEqual(start.status_code, 303)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as fresh:
                    page = await fresh.get("/live")
                    self.assertIn("data-live-start", page.text)
                    live_id = re.search(r"/live/([^/]+)/end", page.text)
                    self.assertIsNotNone(live_id)
                    selected_id = live_id.group(1)  # type: ignore[union-attr]
                    ended = await fresh.post(
                        f"/live/{selected_id}/end",
                        headers={"Origin": "http://testserver"},
                        data={
                            "request_id": str(uuid.uuid4()),
                        },
                    )
                    self.assertEqual(ended.status_code, 303)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as newest:
                    completion = await newest.get("/live")
                    self.assertIn("The end time is safely stored", completion.text)
                    # The HTTP scenario is intentionally short, so correct the end into
                    # the future enough to produce a valid minute-based record.
                    finalized = await newest.post(
                        f"/live/{selected_id}/finalize",
                        headers={"Origin": "http://testserver"},
                        data={
                            "request_id": str(uuid.uuid4()),
                            "road_type": "local",
                            "corrected_end_local": "2026-07-27T12:00",
                            "acknowledge_warnings": "yes",
                        },
                    )
                    self.assertEqual(finalized.status_code, 303)
                    dashboard = await newest.get("/")
                    self.assertIn("Recorded drives", dashboard.text)

        self.run_async(scenario)


if __name__ == "__main__":
    unittest.main()
