from __future__ import annotations

import hashlib
import hmac
import json
import os
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
from driving_log.cli import doctor, main, tailscale_public_host
from driving_log.config import Settings
from driving_log.db import Database
from driving_log.operations import (
    _migrate_legacy_runtime,
    apply_retention,
    install_user_units,
    process_restore_request,
    replicate_archive,
    service_snapshot,
    tailscale_snapshot,
)


class OperationTests(unittest.TestCase):
    @staticmethod
    def settings(root: Path, *, public_host: str = "testserver") -> Settings:
        return Settings(
            state_dir=root,
            database_path=root / "database.sqlite3",
            archive_dir=root / "archives",
            restore_dir=root / "restore",
            host="127.0.0.1",
            port=8766,
            public_host=public_host,
            auth_required=False,
        )

    def test_replication_is_independently_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "state" / "driving.sqlite3")
            database.initialize()
            archive = create_archive(database, root / "archives")
            replicated = replicate_archive(archive, root / "external")
            self.assertTrue(verify_archive(replicated)["verified"])
            self.assertEqual(replicated.stat().st_mode & 0o777, 0o600)

    def test_legacy_runtime_copy_keeps_the_original_and_uses_sqlite_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            Database(legacy / "driving-log.sqlite3").initialize()
            (legacy / "archives").mkdir()
            (legacy / "archives" / "saved.tar.gz").write_bytes(b"archive")
            runtime = root / "repo" / "driving-log-runtime"
            runtime.mkdir(parents=True)
            self.assertTrue(_migrate_legacy_runtime(legacy, runtime))
            self.assertTrue((legacy / "driving-log.sqlite3").exists())
            self.assertTrue((runtime / "driving-log.sqlite3").exists())
            self.assertEqual((runtime / "archives" / "saved.tar.gz").read_bytes(), b"archive")
            self.assertFalse(_migrate_legacy_runtime(legacy, runtime))

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

    def test_retention_never_counts_or_removes_pre_migration_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime(2026, 7, 26, tzinfo=UTC)
            daily = []
            for day in range(20):
                stamp = (now - timedelta(days=day)).strftime("%Y%m%dT%H%M%SZ")
                path = root / f"driving-log-{stamp}.tar.gz"
                path.touch()
                daily.append(path)
            pre_migration = [
                root
                / (
                    "driving-log-pre-migration-v1-"
                    f"{(now - timedelta(days=day)).strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
                )
                for day in range(3)
            ]
            for path in pre_migration:
                path.touch()
            apply_retention(root)
            self.assertTrue(all(path.exists() for path in pre_migration))
            self.assertTrue(all(path.exists() for path in daily[:14]))

    def test_systemd_service_binds_only_through_configured_loopback(self) -> None:
        root = Path(__file__).parent.parent
        service = (root / "deploy/systemd/driving-log-web.service").read_text()
        self.assertIn("EnvironmentFile=", service)
        self.assertNotIn("0.0.0.0", service)
        self.assertNotIn("--host", service)
        readme = (root / "deploy/README.md").read_text()
        self.assertIn("127.0.0.1:8766", readme)
        self.assertIn("127.0.0.1:8765", readme)
        self.assertIn("tailscale funnel", readme.lower())
        self.assertIn("--set-password Sean", readme)
        self.assertIn("--set-password Daniel", readme)

    def test_no_private_seed_is_tracked_by_ignore_contract(self) -> None:
        ignore = (Path(__file__).parent.parent / ".gitignore").read_text()
        self.assertIn("records/*.pdf", ignore)
        self.assertIn("records/log.txt", ignore)

    def test_install_writes_private_environment_and_all_units(self) -> None:
        source_repo = Path(__file__).parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            repo = home / "repo"
            shutil.copytree(source_repo / "deploy", repo / "deploy")
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
            self.assertEqual(environment, repo / "driving-log-runtime" / "environment")
            self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
            contents = environment.read_text()
            self.assertIn("DRIVING_LOG_HOST=127.0.0.1", contents)
            self.assertIn("DRIVING_LOG_PORT=8766", contents)
            self.assertIn("DRIVING_LOG_PUBLIC_SCHEME=http", contents)
            self.assertIn("DRIVING_LOG_AUTH_REQUIRED=1", contents)
            self.assertIn("DRIVING_LOG_OPERATION_SECRET=", contents)
            self.assertIn("DRIVING_LOG_SESSION_SECRET=", contents)
            self.assertNotIn("DRIVING_LOG_FORM_SECRET=", contents)
            self.assertIn(f"DRIVING_LOG_EXTERNAL_ARCHIVE_DIR={home / 'external'}", contents)
            unit_dir = Path(result["unit_directory"])
            self.assertEqual(len(list(unit_dir.iterdir())), 4)
            self.assertEqual(systemctl.call_count, 4)
            self.assertEqual(
                systemctl.call_args_list,
                [
                    mock.call("daemon-reload"),
                    mock.call(
                        "try-restart",
                        "driving-log-web.service",
                        "driving-log-archive.timer",
                    ),
                    mock.call("daemon-reload"),
                    mock.call(
                        "try-restart",
                        "driving-log-web.service",
                        "driving-log-archive.timer",
                    ),
                ],
            )

            environment.write_text(
                contents + "DRIVING_LOG_DATABASE=/custom/database.sqlite3\n",
                encoding="utf-8",
            )
            with (
                mock.patch("driving_log.operations.Path.home", return_value=home),
                mock.patch("driving_log.operations.run_systemctl"),
            ):
                install_user_units(repo, public_host="driving.example.ts.net:8443")
            self.assertIn(
                "DRIVING_LOG_DATABASE=/custom/database.sqlite3",
                environment.read_text(encoding="utf-8"),
            )

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
        malformed = CompletedProcess(["tailscale"], 0, "not-json", "")
        with mock.patch("driving_log.operations.subprocess.run", return_value=malformed):
            self.assertEqual(tailscale_snapshot()["raw"], "not-json")
        with mock.patch(
            "driving_log.operations.run_systemctl", side_effect=FileNotFoundError("systemctl")
        ):
            self.assertFalse(
                service_snapshot()["driving-log-web.service"]["available"]  # type: ignore[index]
            )
        with mock.patch(
            "driving_log.operations.subprocess.run",
            side_effect=FileNotFoundError("tailscale"),
        ):
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
                "operation_id": operation_id,
                "archive_path": str(copied.resolve()),
                "archive_sha256": verify_archive(copied)["archive_sha256"],
                "not_before": time.time() - 1,
                "not_after": time.time() + 60,
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
            with self.assertRaisesRegex(ValueError, "already consumed"):
                process_restore_request(
                    operation_id,
                    root / "restore-requests",
                    database_path,
                    "restore-secret",
                )

    def test_cli_dispatches_service_and_diagnostic_commands(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                "os.environ",
                {"DRIVING_LOG_STATE_DIR": temporary, "DRIVING_LOG_HOST": "127.0.0.1"},
                clear=False,
            ),
        ):
            completed = CompletedProcess(["systemctl"], 0, "ok\n", "")
            with mock.patch(
                "driving_log.cli.install_user_units",
                return_value={"environment": "test"},
            ) as install:
                self.assertEqual(
                    main(["install", "--public-host", "rocky:8443", "--public-scheme", "http"]),
                    0,
                )
                install.assert_called_once()
            with mock.patch("driving_log.cli.run_systemctl", return_value=completed) as systemctl:
                for command in ("start", "stop", "restart"):
                    self.assertEqual(main([command]), 0)
                self.assertEqual(systemctl.call_count, 3)
            with mock.patch(
                "driving_log.cli.service_snapshot",
                return_value={"driving-log-web.service": {"ActiveState": "active"}},
            ):
                self.assertEqual(main(["status"]), 0)
            with mock.patch(
                "driving_log.cli.subprocess.run",
                return_value=CompletedProcess(["journalctl"], 7, "", ""),
            ):
                self.assertEqual(main(["logs"]), 7)
            with mock.patch("uvicorn.run") as run:
                self.assertEqual(main(["serve", "--host", "127.0.0.1", "--port", "9999"]), 0)
                run.assert_called_once()
            with mock.patch("driving_log.cli.doctor", return_value={"ready": True, "check": "ok"}):
                self.assertEqual(main(["doctor"]), 0)
                self.assertEqual(main(["doctor", "--json"]), 0)
                self.assertEqual(main(["db", "check"]), 0)
            with mock.patch(
                "driving_log.cli.doctor", return_value={"ready": False, "error": "bad"}
            ):
                self.assertEqual(main(["doctor"]), 1)
                self.assertEqual(main(["db", "check"]), 1)
            with mock.patch(
                "driving_log.cli.restore_archive",
                return_value=Path(temporary) / "quarantine",
            ):
                self.assertEqual(
                    main(
                        ["archive", "restore", str(Path(temporary) / "archive.tar.gz"), "--confirm"]
                    ),
                    0,
                )
            with mock.patch(
                "driving_log.cli.process_restore_request",
                return_value=Path(temporary) / "result.json",
            ):
                self.assertEqual(main(["archive", "restore-request", str(uuid.uuid4())]), 0)

    def test_set_password_writes_a_private_hash_to_the_runtime_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            environment = state / "environment"
            environment.write_text("DRIVING_LOG_SESSION_SECRET=session\n", encoding="utf-8")
            with (
                mock.patch.dict("os.environ", {"DRIVING_LOG_STATE_DIR": temporary}, clear=False),
                mock.patch(
                    "driving_log.cli.getpass.getpass", side_effect=["new-password", "new-password"]
                ),
            ):
                self.assertEqual(main(["--set-password", "Sean"]), 0)
            contents = environment.read_text(encoding="utf-8")
            self.assertIn("DRIVING_LOG_SEAN_PASSWORD_HASH=scrypt$", contents)
            self.assertNotIn("new-password", contents)

    def test_set_password_supports_daniel_view_only_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            environment = state / "environment"
            environment.write_text("DRIVING_LOG_STATE_DIR=" + str(state) + "\n")
            with (
                mock.patch.dict(os.environ, {"DRIVING_LOG_STATE_DIR": str(state)}),
                mock.patch(
                    "driving_log.cli.getpass.getpass", side_effect=["new-password", "new-password"]
                ),
            ):
                self.assertEqual(main(["--set-password", "Daniel"]), 0)
            contents = environment.read_text()
            self.assertIn("DRIVING_LOG_DANIEL_PASSWORD_HASH=scrypt$", contents)
            self.assertNotIn("new-password", contents)
            self.assertEqual(environment.stat().st_mode & 0o777, 0o600)

    def test_set_password_requires_matching_confirmation_and_runtime_environment(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict("os.environ", {"DRIVING_LOG_STATE_DIR": temporary}, clear=False),
            mock.patch("driving_log.cli.getpass.getpass", side_effect=["first", "second"]),
            self.assertRaisesRegex(ValueError, "do not match"),
        ):
            main(["--set-password", "Jen"])
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict("os.environ", {"DRIVING_LOG_STATE_DIR": temporary}, clear=False),
            mock.patch("driving_log.cli.getpass.getpass", side_effect=["password", "password"]),
            self.assertRaisesRegex(ValueError, "run driving-log install"),
        ):
            main(["--set-password", "Jen"])

    def test_tailscale_public_host_rejects_unusable_status(self) -> None:
        with (
            mock.patch(
                "driving_log.cli.subprocess.run",
                return_value=CompletedProcess(["tailscale"], 1, "", "unavailable"),
            ),
            self.assertRaisesRegex(ValueError, "could not read"),
        ):
            tailscale_public_host()
        with (
            mock.patch(
                "driving_log.cli.subprocess.run",
                return_value=CompletedProcess(["tailscale"], 0, "{}", ""),
            ),
            self.assertRaisesRegex(ValueError, "could not find"),
        ):
            tailscale_public_host()

    def test_install_uses_the_tailscale_dns_name_when_public_host_is_omitted(self) -> None:
        status = CompletedProcess(
            ["tailscale"], 0, '{"Self":{"DNSName":"driving.tailnet.ts.net."}}', ""
        )
        with (
            mock.patch("driving_log.cli.subprocess.run", return_value=status),
            mock.patch("driving_log.cli.install_user_units", return_value={}) as install,
        ):
            self.assertEqual(
                main(["install", "--public-scheme", "https"]),
                0,
            )
        self.assertEqual(install.call_args.kwargs["public_host"], "driving.tailnet.ts.net:8443")

    def test_cli_dispatches_seed_csv_and_archive_variants(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                "os.environ",
                {"DRIVING_LOG_STATE_DIR": temporary, "DRIVING_LOG_HOST": "127.0.0.1"},
                clear=False,
            ),
        ):
            root = Path(temporary)
            pdf = root / "seed.pdf"
            log = root / "log.txt"
            csv_input = root / "input.csv"
            pdf.write_bytes(b"pdf")
            log.write_text("log")
            csv_input.write_bytes(b"csv")
            with mock.patch("driving_log.cli.preview_seed", return_value={"total_rows": 2}):
                self.assertEqual(
                    main(["seed", "--preview", "--pdf", str(pdf), "--log", str(log)]),
                    0,
                )
            with mock.patch("driving_log.cli.apply_seed", return_value={"created": 2}):
                self.assertEqual(main(["seed", "--pdf", str(pdf), "--log", str(log)]), 0)
            exported = root / "export.csv"
            with mock.patch("driving_log.cli.export_csv", return_value=b"header\n"):
                self.assertEqual(main(["csv", "export", "--out", str(exported)]), 0)
                self.assertEqual(exported.read_bytes(), b"header\n")
            with mock.patch("driving_log.cli.import_csv", return_value={"created": 1}):
                self.assertEqual(main(["csv", "import", "--in", str(csv_input)]), 0)

            archive = root / "explicit.tar.gz"
            archive.write_bytes(b"archive")
            with (
                mock.patch("driving_log.cli.create_archive", return_value=archive),
                mock.patch("driving_log.cli.apply_retention", return_value=[root / "old.tar.gz"]),
            ):
                self.assertEqual(main(["archive", "create", "--out", str(archive)]), 0)
            with mock.patch(
                "driving_log.cli.verify_archive",
                return_value={"verified": True},
            ):
                self.assertEqual(main(["archive", "verify", str(archive)]), 0)
            with mock.patch.dict(
                "os.environ",
                {"DRIVING_LOG_EXTERNAL_ARCHIVE_DIR": str(root / "external")},
            ):
                settings_archive = root / "archives"
                settings_archive.mkdir(exist_ok=True)
                local = settings_archive / "latest.tar.gz"
                local.write_bytes(b"archive")
                with mock.patch("driving_log.cli.replicate_archive", return_value=local):
                    self.assertEqual(main(["archive", "replicate"]), 0)

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                "os.environ",
                {
                    "DRIVING_LOG_STATE_DIR": temporary,
                    "DRIVING_LOG_HOST": "127.0.0.1",
                    "DRIVING_LOG_EXTERNAL_ARCHIVE_DIR": str(Path(temporary) / "external"),
                },
                clear=False,
            ),
        ):
            with self.assertRaisesRegex(SystemExit, "no archives found"):
                main(["archive", "verify"])
            with self.assertRaisesRegex(SystemExit, "no local archive"):
                main(["archive", "replicate"])

    def test_doctor_reports_database_archive_and_production_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = self.settings(root)
            with (
                mock.patch("driving_log.cli.service_snapshot", return_value={}),
                mock.patch(
                    "driving_log.cli.tailscale_snapshot",
                    return_value={"available": False},
                ),
            ):
                result = doctor(local)
            self.assertFalse(result["ready"])
            self.assertIn("database does not exist", str(result["error"]))
            self.assertFalse(result["newest_archive_verification"]["verified"])  # type: ignore[index]

            database = Database(local.database_path)
            database.initialize()
            archive = create_archive(database, local.archive_dir)
            with (
                mock.patch("driving_log.cli.verify_archive", side_effect=ValueError("damaged")),
                mock.patch("driving_log.cli.service_snapshot", return_value={}),
                mock.patch(
                    "driving_log.cli.tailscale_snapshot",
                    return_value={"available": False},
                ),
            ):
                damaged = doctor(local)
            self.assertEqual(damaged["newest_archive"], str(archive))
            self.assertEqual(
                damaged["newest_archive_verification"],
                {"verified": False, "error": "damaged"},
            )

            production = self.settings(root, public_host="rocky.tail.test:8443")
            services = {"driving-log-web.service": {"ActiveState": "active"}}
            tailscale = {
                "configuration": {
                    "TCP": {"8443": {"HTTP": True}},
                    "Web": {
                        "rocky.tail.test:8443": {"Proxy": "http://127.0.0.1:8766"},
                        "rocky.tail.test:80": {"Proxy": "http://127.0.0.1:8765"},
                    },
                }
            }
            with (
                mock.patch("driving_log.cli.service_snapshot", return_value=services),
                mock.patch("driving_log.cli.tailscale_snapshot", return_value=tailscale),
            ):
                healthy = doctor(production)
            self.assertTrue(healthy["ready"])
            self.assertTrue(healthy["deployment_checks"]["wordle_http_80_preserved"])  # type: ignore[index]

    def test_retention_and_restore_request_rejection_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid_archive = root / "driving-log-not-a-timestamp.tar.gz"
            invalid_archive.write_bytes(b"keep")
            self.assertEqual(apply_retention(root), [])
            self.assertTrue(invalid_archive.exists())

            operation_id = str(uuid.uuid4())
            operation_dir = root / operation_id
            operation_dir.mkdir()
            request_path = operation_dir / "request.json"
            secret = "restore-secret"

            def write_request(payload: dict[str, object], *, valid: bool = True) -> None:
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                signature = hmac.new(
                    secret.encode(),
                    canonical,
                    hashlib.sha256,
                ).hexdigest()
                request_path.write_text(
                    json.dumps(
                        {
                            **payload,
                            "signature": signature if valid else "invalid",
                        }
                    )
                )

            base: dict[str, object] = {
                "operation_id": operation_id,
                "archive_path": str(operation_dir / "archive.tar.gz"),
                "archive_sha256": "expected",
                "not_before": time.time() - 1,
                "not_after": time.time() + 60,
            }
            write_request(base, valid=False)
            with self.assertRaisesRegex(ValueError, "signature"):
                process_restore_request(operation_id, root, root / "database.sqlite3", secret)

            write_request({**base, "not_before": time.time() + 60})
            with self.assertRaisesRegex(ValueError, "not active"):
                process_restore_request(operation_id, root, root / "database.sqlite3", secret)

            write_request({**base, "not_after": time.time() - 1})
            with self.assertRaisesRegex(ValueError, "expired"):
                process_restore_request(operation_id, root, root / "database.sqlite3", secret)

            write_request({**base, "operation_id": str(uuid.uuid4())})
            with self.assertRaisesRegex(ValueError, "operation ID"):
                process_restore_request(operation_id, root, root / "database.sqlite3", secret)

            write_request({**base, "archive_path": str(root / "outside.tar.gz")})
            with self.assertRaisesRegex(ValueError, "operation directory"):
                process_restore_request(operation_id, root, root / "database.sqlite3", secret)

            (operation_dir / "archive.tar.gz").write_bytes(b"archive")
            write_request(base)
            with (
                mock.patch(
                    "driving_log.operations.verify_archive",
                    return_value={"archive_sha256": "different"},
                ),
                self.assertRaisesRegex(ValueError, "hash mismatch"),
            ):
                process_restore_request(
                    operation_id,
                    root,
                    root / "database.sqlite3",
                    secret,
                )


if __name__ == "__main__":
    unittest.main()
