import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bot import configure_logging
from src.core.state import StatusExporter
from src.core.sync_runtime import SYNC_FILES, sync_runtime_to_persist


class TestRuntimeSyncHealth(unittest.TestCase):
    def test_successful_sync_writes_health_snapshot(self):
        with tempfile.TemporaryDirectory() as runtime_dir, tempfile.TemporaryDirectory() as persist_dir:
            for name in SYNC_FILES:
                Path(runtime_dir, name).write_text(name, encoding="utf-8")

            count = sync_runtime_to_persist(runtime_dir, persist_dir)

            health = json.loads(Path(runtime_dir, "sync_health.json").read_text(encoding="utf-8"))
            self.assertEqual(count, len(SYNC_FILES))
            self.assertTrue(health["sync_healthy"])
            self.assertEqual(health["sync_files_synced"], len(SYNC_FILES))
            self.assertEqual(health["sync_files_total"], len(SYNC_FILES))
            self.assertEqual(health["sync_error_count"], 0)
            self.assertIsNotNone(health["last_sync_success_at"])

    def test_partial_sync_preserves_last_success_and_counts_failed_files(self):
        with tempfile.TemporaryDirectory() as runtime_dir, tempfile.TemporaryDirectory() as persist_dir:
            Path(runtime_dir, SYNC_FILES[0]).write_text("state", encoding="utf-8")
            previous_success = "2026-07-21T10:00:00+00:00"
            Path(runtime_dir, "sync_health.json").write_text(
                json.dumps({"last_sync_success_at": previous_success, "sync_error_count": 2}),
                encoding="utf-8",
            )

            with patch("src.core.sync_runtime._atomic_copy", side_effect=OSError("EIO")):
                count = sync_runtime_to_persist(runtime_dir, persist_dir, files=[SYNC_FILES[0]])

            health = json.loads(Path(runtime_dir, "sync_health.json").read_text(encoding="utf-8"))
            self.assertEqual(count, 0)
            self.assertFalse(health["sync_healthy"])
            self.assertEqual(health["last_sync_success_at"], previous_success)
            self.assertEqual(health["sync_error_count"], 3)
            self.assertIn("EIO", health["sync_last_error"])

    def test_missing_requested_file_marks_sync_unhealthy(self):
        with tempfile.TemporaryDirectory() as runtime_dir, tempfile.TemporaryDirectory() as persist_dir:
            count = sync_runtime_to_persist(
                runtime_dir,
                persist_dir,
                files=["paper_trade_state.json"],
            )

            health = json.loads(Path(runtime_dir, "sync_health.json").read_text(encoding="utf-8"))
            self.assertEqual(count, 0)
            self.assertFalse(health["sync_healthy"])
            self.assertEqual(health["sync_files_total"], 1)
            self.assertEqual(health["sync_error_count"], 1)
            self.assertIn("missing", health["sync_last_error"])


class TestStatusSyncHealth(unittest.TestCase):
    def test_export_merges_sync_health_and_calculates_lag(self):
        with tempfile.TemporaryDirectory() as runtime_dir:
            last_success = datetime.now(timezone.utc) - timedelta(seconds=15)
            Path(runtime_dir, "sync_health.json").write_text(
                json.dumps({
                    "last_sync_success_at": last_success.isoformat(),
                    "sync_healthy": True,
                    "sync_error_count": 4,
                    "sync_files_synced": 10,
                    "sync_files_total": 10,
                }),
                encoding="utf-8",
            )
            status_path = Path(runtime_dir, "bot_status.json")

            with patch.dict(os.environ, {"RUNTIME_DIR": runtime_dir}), patch(
                "src.core.state.STATUS_FILE", str(status_path)
            ):
                StatusExporter.export({"running": True})

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(status["sync_healthy"])
            self.assertGreaterEqual(status["sync_lag_seconds"], 14)
            self.assertLess(status["sync_lag_seconds"], 30)
            self.assertEqual(status["sync_error_count"], 4)
    def test_export_merges_failed_sync_health_without_overriding_failure(self):
        with tempfile.TemporaryDirectory() as runtime_dir:
            last_success = datetime.now(timezone.utc) - timedelta(seconds=15)
            Path(runtime_dir, "sync_health.json").write_text(
                json.dumps({
                    "last_sync_success_at": last_success.isoformat(),
                    "sync_healthy": False,
                    "sync_last_error": "paper_trade_state.json: EIO",
                }),
                encoding="utf-8",
            )
            status_path = Path(runtime_dir, "bot_status.json")

            with patch.dict(os.environ, {"RUNTIME_DIR": runtime_dir}), patch(
                "src.core.state.STATUS_FILE", str(status_path)
            ):
                StatusExporter.export({"running": True})

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["sync_healthy"])
            self.assertIn("EIO", status["sync_last_error"])


class TestRuntimeLogging(unittest.TestCase):
    def test_configure_logging_uses_runtime_rotating_file(self):
        with tempfile.TemporaryDirectory() as log_dir:
            handler = configure_logging(log_dir=log_dir, max_bytes=1024, backup_count=3)
            try:
                self.assertEqual(Path(handler.baseFilename), Path(log_dir, "paper_bot.log"))
                self.assertEqual(handler.maxBytes, 1024)
                self.assertEqual(handler.backupCount, 3)
                logging.getLogger("runtime-test").info("hello")
                handler.flush()
                self.assertIn("hello", Path(log_dir, "paper_bot.log").read_text(encoding="utf-8"))
            finally:
                logging.getLogger().removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
