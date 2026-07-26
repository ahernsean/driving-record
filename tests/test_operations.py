from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import tempfile
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from driving_log.archive import create_archive, verify_archive
from driving_log.cli import main
from driving_log.db import Database
from driving_log.operations import (
    apply_retention,
    install_user_units,
    process_restore_request,
    replicate_archive,
    service_snapshot,
    tailscale_snapshot,
)


class OperationTests(unittest.TestCase):
    def test_replication_is_independently_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "state" / "driving.sqlite3")
            database.initialize()
            archive = create_archive(database, root / "archives")
            replicated = replicate_archive(archive, root / "external")
            self.assertTrue(verify_archive(replicated)["verified"])
            self.assertEqual(replicated.stat().st_mode & 0o777, 0o600)

    def test_retention_keeps_fourteen_daily_and_eight_weekly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime(2026, 7, 26, tzinfo=UTC)
            paths = []
            for day in range(70):
                stamp = (now - timedelta(days=day)).strftime("%Y%m%dT%H%M%SZ")
                path = root / f"driving-log-{stamp}.tar.gz"
                path.touch()
                paths.append(path)
            removed = apply_retention(root)
            remaining = list(root.glob("*.tar.gz"))
            self.assertGreaterEqual(len(remaining), 14)
            self.assertLessEqual(len(remaining), 22)
            self.assertTrue(removed)
            self.assertTrue(all(path in paths for path in removed))

    def test_systemd_service_binds_only_through_configured_loopback(self) -> None:
        root = Path(__file__).parent.parent
        service = (root / "deploy/systemd/driving-log-web.service").read_text()
        self.assertIn("EnvironmentFile=", service)
        self.assertNotIn("0.0.0.0", service)
        self.assertNotIn("--host", service)
        readme = (root / "deploy/README.md").read_text()
        self.assertIn("127.0.0.1:8766", readme)
        self.assertIn("127.0.0.1:8765", readme)
        self.assertNotIn("tailscale funnel", readme.lower())

    def test_no_private_seed_is_tracked_by_ignore_contract(self) -> None:
        ignore = (Path(__file__).parent.parent / ".gitignore").read_text()
        self.assertIn("records/*.pdf", ignore)
        self.assertIn("records/log.txt", ignore)

    def test_install_writes_private_environment_and_all_units(self) -> None:
        repo = Path(__file__).parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with (
                mock.patch("driving_log.operations.Path.home", return_value=home),
                mock.patch("driving_log.operations.run_systemctl") as systemctl,
            ):
                result = install_user_units(
                    repo,
                    public_host="driving.example.ts.net:8443",
                    external_archive_dir=home / "external",
                )
                install_user_units(
                    repo,
                    public_host="driving.example.ts.net:8443",
                )
            environment = Path(result["environment"])
            self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
            contents = environment.read_text()
            self.assertIn("DRIVING_LOG_HOST=127.0.0.1", contents)
            self.assertIn("DRIVING_LOG_PORT=8766", contents)
            self.assertIn("DRIVING_LOG_PUBLIC_SCHEME=http", contents)
            self.assertIn("DRIVING_LOG_OPERATION_SECRET=", contents)
            self.assertNotIn("DRIVING_LOG_FORM_SECRET=", contents)
            self.assertIn(f"DRIVING_LOG_EXTERNAL_ARCHIVE_DIR={home / 'external'}", contents)
            unit_dir = Path(result["unit_directory"])
            self.assertEqual(len(list(unit_dir.iterdir())), 4)
            self.assertEqual(systemctl.call_count, 2)

    def test_operational_snapshots_report_success_and_failure(self) -> None:
        completed = CompletedProcess(
            ["systemctl"],
            0,
            "ActiveState=active\nSubState=running\nNRestarts=0\n",
            "",
        )
        with mock.patch("driving_log.operations.run_systemctl", return_value=completed):
            snapshot = service_snapshot()
        self.assertEqual(
            snapshot["driving-log-web.service"]["ActiveState"],  # type: ignore[index]
            "active",
        )

        tailscale = CompletedProcess(["tailscale"], 0, '{"TCP":{"8443":{"HTTP":true}}}', "")
        with mock.patch("driving_log.operations.subprocess.run", return_value=tailscale):
            self.assertTrue(tailscale_snapshot()["available"])
        failure = CompletedProcess(["tailscale"], 1, "", "daemon unavailable")
        with mock.patch("driving_log.operations.subprocess.run", return_value=failure):
            self.assertFalse(tailscale_snapshot()["available"])

    def test_operational_cli_archive_live_and_import_status(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                "os.environ",
                {"DRIVING_LOG_STATE_DIR": temporary},
                clear=False,
            ),
        ):
            self.assertEqual(main(["archive", "create"]), 0)
            self.assertEqual(main(["archive", "list"]), 0)
            self.assertEqual(main(["archive", "verify"]), 0)
            self.assertEqual(main(["archive", "replicate"]), 0)
            self.assertEqual(main(["live", "status"]), 0)
            self.assertEqual(main(["imports", "status"]), 0)

    def test_signed_restore_request_restores_and_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "state" / "driving.sqlite3"
            database = Database(database_path)
            database.initialize()
            archive = create_archive(database, root / "archives")
            operation_id = str(uuid.uuid4())
            operation_dir = root / "restore-requests" / operation_id
            operation_dir.mkdir(parents=True)
            copied = operation_dir / archive.name
            shutil.copy2(archive, copied)
            payload: dict[str, object] = {
                "archive_path": str(copied.resolve()),
                "archive_sha256": verify_archive(copied)["archive_sha256"],
                "not_before": time.time() - 1,
            }
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            payload["signature"] = hmac.new(
                b"restore-secret", canonical, hashlib.sha256
            ).hexdigest()
            (operation_dir / "request.json").write_text(json.dumps(payload))

            result_path = process_restore_request(
                operation_id,
                root / "restore-requests",
                database_path,
                "restore-secret",
            )

            result = json.loads(result_path.read_text())
            self.assertEqual(result["status"], "completed")
            self.assertTrue(Path(result["quarantine"]).is_dir())


if __name__ == "__main__":
    unittest.main()
