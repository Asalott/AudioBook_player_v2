import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from helpers import FakeBackend, FakeClock, make_config

from database import Database
from nas_sync import LocalSource, NasError, NasSync, next_run, parse_address, validate_schedule
from server import create_app, shutdown_app


def ts(*args):
    return datetime(*args).timestamp()


class FakeScanner:
    def __init__(self):
        self.calls = []

    def scan_async(self, reason):
        self.calls.append(reason)


class TestAddress(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_address(r"\\nas\media\Ljud böcker\Barn"), ("nas", 445, "media", ["Ljud böcker", "Barn"]))
        self.assertEqual(parse_address("//192.168.1.10/media"), ("192.168.1.10", 445, "media", []))
        self.assertEqual(parse_address("smb://nas:1445/media/a/"), ("nas", 1445, "media", ["a"]))

    def test_invalid(self):
        for bad in ("", "nas/media", r"\\nas", "smb://nas:x/media"):
            with self.assertRaises(NasError):
                parse_address(bad)


class TestSchedule(unittest.TestCase):
    def test_validate(self):
        self.assertIsNone(validate_schedule(None))
        self.assertEqual(validate_schedule({"every": 2, "unit": "week", "time": "3:5"}),
                         {"every": 2, "unit": "week", "time": "03:05"})
        for bad in ({"every": 0, "unit": "day"}, {"every": 1, "unit": "year"},
                    {"every": True, "unit": "day"}, {"every": 1, "unit": "day", "time": "25:00"}):
            with self.assertRaises(NasError):
                validate_schedule(bad)

    def test_first_run_is_next_occurrence_of_time(self):
        daily = {"every": 1, "unit": "day", "time": "03:00"}
        self.assertEqual(next_run(daily, None, ts(2026, 10, 5, 1, 0)), ts(2026, 10, 5, 3, 0))
        self.assertEqual(next_run(daily, None, ts(2026, 10, 5, 12, 0)), ts(2026, 10, 6, 3, 0))

    def test_interval_from_last_success(self):
        weekly = {"every": 2, "unit": "week", "time": "04:30"}
        self.assertEqual(next_run(weekly, ts(2026, 10, 5, 4, 31), 0), ts(2026, 10, 19, 4, 30))
        monthly = {"every": 1, "unit": "month", "time": "03:00"}
        self.assertEqual(next_run(monthly, ts(2026, 1, 31, 3, 0), 0), ts(2026, 2, 28, 3, 0))
        self.assertEqual(next_run({**monthly, "every": 3}, ts(2026, 11, 15, 3, 0), 0), ts(2027, 2, 15, 3, 0))

    def test_failure_retries_after_an_hour(self):
        daily = {"every": 1, "unit": "day", "time": "03:00"}
        failed = ts(2026, 10, 6, 3, 0, 5)
        self.assertEqual(next_run(daily, ts(2026, 10, 5, 3, 0), 0, failed), failed + 3600)

    def test_off(self):
        self.assertIsNone(next_run(None, None, None))


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote"
        self.remote.mkdir()
        self.config = make_config(root)
        self.db = Database(self.config.db_path, self.config.books_dir)
        self.scanner = FakeScanner()
        self.clock = FakeClock(ts(2026, 10, 5, 12, 0))
        self.nas = NasSync(self.db, self.config, self.scanner, source_factory=lambda s: LocalSource(s["address"]),
                           clock=self.clock)
        self.nas.save_settings({"address": str(self.remote)})
        self.local = Path(self.config.books_dir) / "NAS"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def put(self, rel, data=b"audio", mtime=1_700_000_000):
        path = self.remote / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
        return path

    def test_copies_new_audio_files_only(self):
        self.put("Serie/Bok 1.m4b", b"one")
        self.put("Bok 2.mp3", b"two")
        self.put("cover.jpg", b"img")
        self.put(".hidden.m4b")
        result = self.nas.sync()
        self.assertEqual((result["copied"], result["unchanged"], result["errors"]), (2, 0, 0))
        self.assertEqual((self.local / "Serie" / "Bok 1.m4b").read_bytes(), b"one")
        self.assertEqual((self.local / "Bok 2.mp3").stat().st_mtime, 1_700_000_000)
        self.assertFalse((self.local / "cover.jpg").exists())
        self.assertEqual(list(self.local.rglob(".*")), [])
        self.assertEqual(self.scanner.calls, ["nas"])

    def test_second_sync_only_copies_changes(self):
        self.put("a.m4b", b"one")
        self.put("b.m4b", b"two")
        self.nas.sync()
        self.put("b.m4b", b"two, changed", mtime=1_700_000_100)
        self.put("c.m4b", b"three")
        result = self.nas.sync()
        self.assertEqual((result["copied"], result["unchanged"]), (2, 1))
        self.assertEqual((self.local / "b.m4b").read_bytes(), b"two, changed")
        self.scanner.calls.clear()
        self.assertEqual(self.nas.sync()["copied"], 0)
        self.assertEqual(self.scanner.calls, [])  # nothing new, no rescan

    def test_files_removed_from_nas_are_kept(self):
        path = self.put("a.m4b")
        self.nas.sync()
        path.unlink()
        self.nas.sync()
        self.assertTrue((self.local / "a.m4b").exists())

    def test_unreachable_nas_records_failure_and_touches_nothing(self):
        self.put("a.m4b")
        self.nas.sync()
        self.nas.save_settings({"address": str(self.remote / "gone")})
        with self.assertRaises(NasError):
            self.nas.sync()
        self.assertTrue((self.local / "a.m4b").exists())
        self.assertEqual(self.nas.status()["last_failure"], self.clock())

    def test_schedule_due_after_success(self):
        self.nas.save_settings({"schedule": {"every": 1, "unit": "day", "time": "03:00"}})
        self.assertEqual(self.nas.next_run(), ts(2026, 10, 6, 3, 0))
        self.clock.t = ts(2026, 10, 6, 3, 0, 30)
        self.nas.sync("schedule")
        self.assertEqual(self.nas.next_run(), ts(2026, 10, 7, 3, 0))

    def test_password_is_kept_unless_given(self):
        self.nas.save_settings({"username": "anna", "password": "hemligt"})
        self.nas.save_settings({"username": "anna2"})
        self.assertEqual(self.nas.settings()["password"], "hemligt")
        self.assertEqual(self.nas.public_settings(), {"address": str(self.remote), "username": "anna2",
                                                      "has_password": True, "schedule": None})

    def test_async_sync(self):
        self.put("a.m4b")
        self.assertEqual(self.nas.sync_async(), "started")
        self.assertTrue(self.nas.wait(5))
        status = self.nas.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["last_result"]["copied"], 1)
        self.assertIsNone(status["error"])


class TestNasApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.remote = Path(self.tmp.name) / "remote"
        self.remote.mkdir()
        (self.remote / "a.m4b").write_bytes(b"x")
        self.app = create_app(make_config(self.tmp.name), backend_factory=lambda: FakeBackend(FakeClock()),
                              start_background=False, nas_source_factory=lambda s: LocalSource(s["address"]))
        self.client = self.app.test_client()

    def tearDown(self):
        shutdown_app(self.app)
        self.tmp.cleanup()

    def test_settings_roundtrip_never_returns_password(self):
        res = self.client.post("/api/nas", json={"address": str(self.remote), "username": "u", "password": "p",
                                                 "schedule": {"every": 1, "unit": "month", "time": "02:00"}})
        self.assertEqual(res.status_code, 200)
        data = self.client.get("/api/nas").get_json()
        self.assertNotIn("password", data)
        self.assertTrue(data["has_password"])
        self.assertEqual(data["schedule"]["unit"], "month")
        self.assertIsNotNone(data["next_run"])

    def test_bad_input_is_400(self):
        self.assertEqual(self.client.post("/api/nas", json={"address": r"\\nas"}).status_code, 400)
        self.assertEqual(self.client.post("/api/nas", json={"schedule": {"every": 0, "unit": "day"}}).status_code, 400)
        self.assertEqual(self.client.post("/api/nas", data="x").status_code, 400)

    def test_test_and_sync(self):
        res = self.client.post("/api/nas/test", json={"address": str(self.remote)})
        self.assertEqual(res.get_json(), {"files": 1, "bytes": 1})
        self.assertEqual(self.client.post("/api/nas/test", json={"address": str(self.remote / "nope")}).status_code, 400)
        self.client.post("/api/nas", json={"address": str(self.remote)})
        self.assertEqual(self.client.post("/api/nas/sync").status_code, 202)
        self.app.extensions["audiobook"].nas.wait(5)
        self.assertEqual(self.client.get("/api/nas").get_json()["last_result"]["copied"], 1)


if __name__ == "__main__":
    unittest.main()
