import datetime as dt
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from helpers import FakeBackend, FakeClock, create_legacy_db, make_config

from database import SCHEMA_VERSION, Database
from playback import PlaybackService
from server import create_app, shutdown_app
from stats import StatsRecorder

DURATION = 1_000_000  # 1000 s


class StatsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name, stats_flush_interval=60, position_save_interval=30)
        self.db = Database(self.config.db_path, self.config.books_dir)
        self.clock = FakeClock()
        self.wall = [time.mktime((2026, 9, 21, 12, 0, 0, 0, 0, -1))]  # Monday noon
        self.stats = StatsRecorder(self.db, self.config, wall=lambda: self.wall[0], clock=self.clock)
        self.book_ids = [self.add_book("a.m4b"), self.add_book("b.m4b")]
        self.backend = None
        self.svc = PlaybackService(self.db, self.config, self.make_backend, clock=self.clock)
        self.svc.add_listener(self.stats.on_event)

    def tearDown(self):
        self.svc.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def make_backend(self):
        self.backend = FakeBackend(self.clock, length_ms=DURATION)
        return self.backend

    def add_book(self, name):
        (Path(self.config.books_dir) / name).write_bytes(b"x")
        return self.db.execute(
            "INSERT INTO books (title, path, chapters, duration_ms) VALUES (?, ?, '[]', ?)",
            (name, name, DURATION),
        ).lastrowid

    def run_for(self, seconds):
        for _ in range(int(seconds)):
            self.clock.advance(1)
            self.wall[0] += 1
            self.svc.tick()

    def book(self, book_id):
        return self.db.get_book(book_id)


class TestListeningTime(StatsTestCase):
    def test_counts_only_playing_time(self):
        self.svc.select(self.book_ids[0])
        self.svc.play()
        self.run_for(100)
        self.svc.pause()
        self.clock.advance(3600)  # paused for an hour
        self.wall[0] += 3600
        self.svc.play()
        self.run_for(20)
        self.svc.pause()
        self.assertAlmostEqual(self.book(self.book_ids[0])["total_listened_ms"], 120_000, delta=50)

    def test_seeking_does_not_add_time(self):
        self.svc.select(self.book_ids[0])
        self.svc.play()
        self.run_for(10)
        for _ in range(20):
            self.svc.seek_relative(30_000)  # skip 10 minutes forward
        self.run_for(10)
        self.svc.pause()
        self.assertAlmostEqual(self.book(self.book_ids[0])["total_listened_ms"], 20_000, delta=50)

    def test_writes_are_batched(self):
        writes = []
        original = self.db.transaction

        def counting():
            writes.append(1)
            return original()

        self.db.transaction = counting
        self.svc.select(self.book_ids[0])
        self.svc.play()
        self.run_for(300)
        self.assertLessEqual(len(writes), 6, "stats must be flushed about once a minute, not every second")
        self.db.transaction = original

    def test_flushed_on_shutdown(self):
        self.svc.select(self.book_ids[0])
        self.svc.play()
        self.run_for(30)
        self.svc.shutdown()
        self.stats.flush()
        self.assertAlmostEqual(self.book(self.book_ids[0])["total_listened_ms"], 30_000, delta=50)

    def test_per_day_week_and_month(self):
        self.svc.select(self.book_ids[0])
        self.svc.play()
        self.run_for(60)
        self.svc.pause()
        self.wall[0] -= 7 * 86400  # last week
        self.svc.play()
        self.run_for(120)
        self.svc.pause()
        self.wall[0] += 7 * 86400
        summary = self.stats.summary()
        self.assertAlmostEqual(summary["total_ms"], 180_000, delta=100)
        self.assertAlmostEqual(summary["week_ms"], 60_000, delta=100)
        self.assertAlmostEqual(summary["weeks"][-2]["ms"], 120_000, delta=100)
        self.assertEqual(len(summary["weeks"]), 8)
        self.assertEqual(len(summary["months"]), 6)
        self.assertEqual(summary["months"][-1]["month"], 9)
        self.assertAlmostEqual(summary["today_ms"], 60_000, delta=100)

    def test_time_is_split_per_book(self):
        self.svc.select(self.book_ids[0])
        self.svc.play()
        self.run_for(40)
        self.svc.select(self.book_ids[1])
        self.svc.play()
        self.run_for(25)
        self.svc.pause()
        self.assertAlmostEqual(self.book(self.book_ids[0])["total_listened_ms"], 40_000, delta=50)
        self.assertAlmostEqual(self.book(self.book_ids[1])["total_listened_ms"], 25_000, delta=50)


class TestCompletions(StatsTestCase):
    def test_counted_once_at_95_percent(self):
        self.svc.select(self.book_ids[0])
        self.svc.seek_to(940_000)
        self.svc.play()
        self.run_for(5)
        self.assertEqual(self.book(self.book_ids[0])["listen_count"], 0)
        self.run_for(10)  # passes 950 s = 95 %
        self.assertEqual(self.book(self.book_ids[0])["listen_count"], 1)
        # seeking back a bit and playing past 95 % again does not count twice
        self.svc.seek_to(900_000)
        self.run_for(60)
        self.assertEqual(self.book(self.book_ids[0])["listen_count"], 1)

    def test_seeking_to_the_end_does_not_count(self):
        self.svc.select(self.book_ids[0])
        self.svc.seek_to(990_000)
        self.assertEqual(self.book(self.book_ids[0])["listen_count"], 0)

    def test_second_listen_after_starting_over(self):
        self.svc.select(self.book_ids[0])
        self.svc.seek_to(960_000)
        self.svc.play()
        self.run_for(50)  # plays to the end
        self.assertFalse(self.svc.playing)
        self.assertEqual(self.book(self.book_ids[0])["listen_count"], 1)
        self.svc.play()  # starts over from 0 -> re-armed
        self.run_for(2)
        self.svc.seek_to(955_000)
        self.run_for(3)
        self.assertEqual(self.book(self.book_ids[0])["listen_count"], 2)
        rows = self.db.query("SELECT COUNT(*) AS n FROM completions WHERE book_id = ?", (self.book_ids[0],))
        self.assertEqual(rows[0]["n"], 2)

    def test_progress_and_top_list(self):
        self.svc.select(self.book_ids[1])
        self.svc.play()
        self.run_for(250)
        self.svc.pause()
        summary = self.stats.summary()
        self.assertEqual(summary["top"][0]["id"], self.book_ids[1])
        self.assertAlmostEqual(summary["top"][0]["progress"], 0.25, delta=0.01)
        self.assertEqual(summary["books_started"], 1)


class TestFavoritesApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name)
        (Path(self.config.books_dir) / "a.m4b").write_bytes(b"x")
        self.app = create_app(self.config, backend_factory=lambda: FakeBackend(FakeClock()), start_background=False)
        self.db = self.app.extensions["audiobook"].db
        self.book_id = self.db.execute(
            "INSERT INTO books (title, path, chapters, duration_ms) VALUES ('Åsa', 'a.m4b', '[]', 1000)").lastrowid
        self.client = self.app.test_client()

    def tearDown(self):
        shutdown_app(self.app)
        self.tmp.cleanup()

    def test_toggle_favorite(self):
        url = f"/api/books/{self.book_id}/favorite"
        self.assertEqual(self.client.post(url, json={"favorite": True}).get_json()["favorite"], True)
        self.assertTrue(self.client.get("/api/books").get_json()[0]["favorite"])
        stats = self.client.get("/api/stats").get_json()
        self.assertEqual([b["title"] for b in stats["favorites"]], ["Åsa"])
        self.client.post(url, json={"favorite": False})
        self.assertFalse(self.client.get("/api/books").get_json()[0]["favorite"])
        self.assertEqual(self.client.get("/api/stats").get_json()["favorites"], [])

    def test_bad_input(self):
        self.assertEqual(self.client.post(f"/api/books/{self.book_id}/favorite", json={"favorite": "yes"}).status_code, 400)
        self.assertEqual(self.client.post("/api/books/999/favorite", json={"favorite": True}).status_code, 404)

    def test_favorite_survives_rescan(self):
        from library import BookMeta
        scanner = self.app.extensions["audiobook"].scanner
        scanner.read_metadata = lambda p: BookMeta(title="Ny titel", duration_ms=1000)
        scanner.clock = lambda: 4102444800
        self.client.post(f"/api/books/{self.book_id}/favorite", json={"favorite": True})
        self.db.execute("UPDATE books SET listen_count = 3, total_listened_ms = 5000 WHERE id = ?", (self.book_id,))
        scanner.scan()  # file_size is unknown -> metadata is re-read
        book = self.db.get_book(self.book_id)
        self.assertEqual(book["title"], "Ny titel")
        self.assertTrue(book["favorite"])
        self.assertEqual((book["listen_count"], book["total_listened_ms"]), (3, 5000))


class TestMigration(unittest.TestCase):
    def test_legacy_database_migrates_to_latest_without_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            chapters = json.dumps([{"title": "Ett", "start": 0, "end": 10}])
            create_legacy_db(config.db_path, [
                {"id": 1, "title": "A", "path": "C:\\gammal\\books\\a.m4b", "last_position": 5051, "chapters": chapters},
                {"id": 2, "title": "B", "path": "C:\\gammal\\books\\b.m4b", "last_position": 21059780},
            ], last_book_id=2)
            db = Database(config.db_path, config.books_dir)
            try:
                self.assertEqual(db.schema_version(), SCHEMA_VERSION)
                a, b = db.get_book(1), db.get_book(2)
                self.assertEqual((a["last_position"], b["last_position"]), (5051, 21059780))
                self.assertEqual(a["chapters"][0]["title"], "Ett")
                self.assertEqual((a["listen_count"], a["total_listened_ms"], a["favorite"]), (0, 0, False))
                self.assertEqual(db.get_setting("last_book_id"), "2")
                tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"listening_daily", "completions"} <= tables)
            finally:
                db.close()
            backup = next(Path(config.db_path).parent.glob("*.bak"))
            conn = sqlite3.connect(str(backup))
            self.assertEqual(conn.execute("SELECT last_position FROM books WHERE id = 2").fetchone()[0], 21059780)
            conn.close()

    def test_v1_database_upgrades_to_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            create_legacy_db(config.db_path, [{"id": 1, "title": "A", "path": "a.m4b", "last_position": 7}])
            conn = sqlite3.connect(str(config.db_path))
            conn.execute("PRAGMA user_version = 1")
            for col in ("duration_ms INTEGER", "file_size INTEGER", "file_mtime REAL",
                        "missing INTEGER NOT NULL DEFAULT 0", "cover_version INTEGER", "added_at REAL", "updated_at REAL"):
                conn.execute(f"ALTER TABLE books ADD COLUMN {col}")
            conn.commit()
            conn.close()
            db = Database(config.db_path, config.books_dir)
            try:
                self.assertEqual(db.get_book(1)["last_position"], 7)
                self.assertEqual(db.get_book(1)["listen_count"], 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
