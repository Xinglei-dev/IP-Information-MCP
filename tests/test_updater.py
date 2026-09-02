"""Unit tests for release parsing and UpdateService orchestration."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.updater import Release, UpdateService, parse_release


class FakeDB:
    def __init__(self, version: str = "2026-09") -> None:
        self.version = version
        self.updating = False
        self.ready = True
        self.data_dir = Path(tempfile.mkdtemp(prefix="ip-info-updater-test-"))

    def begin_update(self) -> bool:
        if self.updating:
            return False
        self.updating = True
        return True

    def end_update(self, *, load: bool = False) -> None:
        self.updating = False


class ParseTests(unittest.TestCase):
    def test_parse_release(self):
        html = """
        <a href="https://download.db-ip.com/free/dbip-country-lite-2026-09.mmdb.gz">
        <a href="https://download.db-ip.com/free/dbip-city-lite-2026-09.mmdb.gz">
        """
        self.assertEqual(parse_release(html), Release(2026, 9))

    def test_parse_release_missing(self):
        self.assertIsNone(parse_release("<html>nothing</html>"))

    def test_release_urls(self):
        urls = Release(2026, 9).download_urls()
        self.assertIn(
            "https://download.db-ip.com/free/dbip-country-lite-2026-09.mmdb.gz",
            urls.values(),
        )
        self.assertEqual(len(urls), 3)


class UpdateServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = FakeDB("2026-09")
        self.service = UpdateService(self.db)

    def test_trigger_up_to_date(self):
        with mock.patch("src.updater.fetch_latest_release", return_value=Release(2026, 9)):
            result = self.service.trigger_update()
        self.assertEqual(result["status"], "up_to_date")
        self.assertFalse(self.db.updating)

    def test_trigger_start_background(self):
        import threading

        original_start = threading.Thread.start

        def noop_start(_self):
            return None

        threading.Thread.start = noop_start
        try:
            with mock.patch("src.updater.fetch_latest_release", return_value=Release(2026, 10)):
                result = self.service.trigger_update()
        finally:
            threading.Thread.start = original_start
        self.assertEqual(result["status"], "started")
        self.assertTrue(self.db.updating)

        # Run the worker body synchronously to verify it resets state.
        with mock.patch.object(
            self.service,
            "_download_and_reload",
            return_value={"ok": True, "version": "2026-10"},
        ):
            self.service._run_background(Release(2026, 10))
        self.assertFalse(self.db.updating)
        self.assertEqual(self.service.status()["status"], "success")
        self.assertEqual(self.service.status()["database_version"], "2026-09")

    def test_initialize_success(self):
        with (
            mock.patch("src.updater.fetch_latest_release", return_value=Release(2026, 10)),
            mock.patch.object(
                self.service,
                "_download_and_reload",
                return_value={"ok": True, "version": "2026-10"},
            ),
        ):
            result = self.service.initialize()
        self.assertTrue(result["ok"])
        self.assertFalse(self.db.updating)

    def test_initialize_error_wraps_and_resets(self):
        with (
            mock.patch(
                "src.updater.fetch_latest_release", side_effect=RuntimeError("network down")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "初始化失败"):
                self.service.initialize()
        self.assertFalse(self.db.updating)


if __name__ == "__main__":
    unittest.main()
