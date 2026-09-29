import json
import tempfile
import unittest
from pathlib import Path

from helpers import FakeBackend, FakeClock, make_config

from database import Database
from playback import PlaybackError, PlaybackService

CHAPTERS = [
    {"title": "Ett", "start": 0.0, "end": 100.0},
    {"title": "Två", "start": 100.0, "end": 200.0},
    {"title": "Tre", "start": 200.0, "end": 300.0},
]


class PlaybackTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name, position_save_interval=30)
        self.db = Database(self.config.db_path, self.config.books_dir)
        self.clock = FakeClock()
        self.backends = []
        self.ids = [self.add_book("a.m4b", 300_000), self.add_book("b.m4b", 300_000)]
        self.svc = PlaybackService(self.db, self.config, self.make_backend, clock=self.clock)

    def tearDown(self):
        self.svc.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def make_backend(self):
        backend = FakeBackend(self.clock, length_ms=300_000)
        self.backends.append(backend)
        return backend

    def add_book(self, name, duration_ms, position=0):
        (Path(self.config.books_dir) / name).write_bytes(b"x")
        cur = self.db.execute(
            "INSERT INTO books (title, path, chapters, duration_ms, last_position) VALUES (?, ?, ?, ?, ?)",
            (name, name, json.dumps(CHAPTERS), duration_ms, position),
        )
        return cur.lastrowid

    def saved_position(self, book_id):
        return self.db.get_book(book_id)["last_position"]

    def run_for(self, seconds):
        for _ in range(int(seconds)):
            self.clock.advance(1)
            self.svc.tick()


class TestPlayback(PlaybackTestCase):
    def test_resume_from_saved_position(self):
        self.db.save_position(self.ids[0], 42_000)
        self.svc.select(self.ids[0])
        self.svc.play()
        self.run_for(3)
        self.assertAlmostEqual(self.svc.position_ms, 45_000, delta=10)

    def test_pause_saves_position(self):
        self.svc.select(self.ids[0])
        self.svc.play()
        self.run_for(10)
        self.svc.pause()
        self.assertAlmostEqual(self.saved_position(self.ids[0]), 10_000, delta=10)

    def test_pause_is_not_a_toggle(self):
        self.svc.select(self.ids[0])
        self.svc.play()
        self.svc.pause()
        self.svc.pause()
        self.assertFalse(self.svc.playing)
        self.assertEqual(self.backends[0].state(), "paused")

    def test_stop_then_play_resumes_instead_of_restarting(self):
        # Old bug: /api/stop followed by /api/play restarted the book at 0
        # and then overwrote the saved position.
        self.svc.select(self.ids[0])
        self.svc.play()
        self.run_for(20)
        self.svc.stop()
        self.svc.play()
        self.run_for(1)
        self.assertGreaterEqual(self.svc.position_ms, 20_000)

    def test_switching_book_stops_old_and_saves(self):
        self.svc.select(self.ids[0])
        self.svc.play()
        self.run_for(5)
        self.svc.select(self.ids[1])
        self.assertAlmostEqual(self.saved_position(self.ids[0]), 5_000, delta=10)
        self.assertFalse(self.svc.playing)
        self.assertEqual(len(self.backends), 1, "the backend must be reused, not recreated")

    def test_periodic_save_is_throttled(self):
        writes = []
        original = self.db.save_position
        self.db.save_position = lambda *a: (writes.append(a), original(*a))
        self.svc.select(self.ids[0])
        self.svc.play()
        self.run_for(95)
        # 95 s of playback with a 30 s interval -> 3 writes, not 95.
        self.assertEqual(len(writes), 3)

    def test_skip_backward_near_start_goes_to_zero(self):
        self.svc.select(self.ids[0])
        self.svc.play()
        self.run_for(10)
        self.svc.seek_relative(-15_000)
        self.assertEqual(self.svc.position_ms, 0)

    def test_skip_forward_past_end_is_clamped(self):
        self.svc.select(self.ids[0])
        self.svc.seek_to(299_000)
        self.svc.seek_relative(30_000)
        self.assertLessEqual(self.svc.position_ms, 300_000)
        self.assertGreater(self.svc.position_ms, 290_000)

    def test_seek_before_play_is_used_as_start(self):
        self.svc.select(self.ids[0])
        self.svc.seek_to(150_000)
        self.svc.play()
        self.run_for(1)
        self.assertAlmostEqual(self.svc.position_ms, 151_000, delta=10)

    def test_stale_backend_reading_is_ignored_after_seek(self):
        self.svc.select(self.ids[0])
        self.svc.play()
        self.svc.seek_to(200_000)
        self.backends[0].stale_reading = 0
        self.clock.advance(1)
        self.assertGreaterEqual(self.svc.position_ms, 200_000)

    def test_chapter_navigation(self):
        self.svc.select(self.ids[0])
        self.svc.seek_to(150_000)
        self.svc.next_chapter()
        self.assertEqual(self.svc.position_ms, 200_000)
        self.svc.seek_to(205_000)
        self.svc.prev_chapter()  # >3 s into chapter -> chapter start
        self.assertEqual(self.svc.position_ms, 200_000)
        self.svc.prev_chapter()  # at start -> previous chapter
        self.assertEqual(self.svc.position_ms, 100_000)

    def test_next_chapter_with_fractional_start(self):
        chapters = [{"title": "1", "start": 0.0, "end": 100.000989},
                    {"title": "2", "start": 100.000989, "end": 300.0}]
        self.db.execute("UPDATE books SET chapters = ?, duration_ms = NULL WHERE id = ?",
                        (json.dumps(chapters), self.ids[0]))
        self.svc.select(self.ids[0])
        self.svc.next_chapter()
        self.assertEqual(self.svc.status()["chapter"], 1)
        self.assertEqual(self.svc.status()["length"], 300_000)  # falls back to last chapter end

    def test_book_without_chapters(self):
        self.db.execute("UPDATE books SET chapters = '[]' WHERE id = ?", (self.ids[0],))
        self.svc.select(self.ids[0])
        self.svc.next_chapter()
        self.svc.prev_chapter()
        self.assertEqual(self.svc.status()["chapter"], None)

    def test_sleep_timer_counts_only_playing_time(self):
        self.svc.select(self.ids[0])
        self.svc.play()
        self.svc.set_sleep_timer(1)
        self.run_for(30)
        self.svc.pause()
        self.clock.advance(600)  # paused time does not count
        self.svc.play()
        self.run_for(29)
        self.assertTrue(self.svc.playing)
        self.run_for(2)
        self.assertFalse(self.svc.playing)
        self.assertIsNone(self.svc.status()["sleep_remaining"])
        self.assertAlmostEqual(self.saved_position(self.ids[0]), 60_000, delta=1500)

    def test_sleep_timer_cancel(self):
        self.svc.select(self.ids[0])
        self.svc.play()
        self.svc.set_sleep_timer(1)
        self.svc.cancel_sleep_timer()
        self.run_for(70)
        self.assertTrue(self.svc.playing)

    def test_end_of_book(self):
        events = []
        self.svc.add_listener(lambda e, **d: events.append(e))
        self.svc.select(self.ids[0])
        self.svc.seek_to(295_000)
        self.svc.play()
        self.run_for(7)
        self.assertFalse(self.svc.playing)
        self.assertIn("finished", events)
        self.assertEqual(self.saved_position(self.ids[0]), 300_000)
        self.svc.play()  # finished book starts over
        self.run_for(1)
        self.assertLess(self.svc.position_ms, 5_000)

    def test_missing_file(self):
        (Path(self.config.books_dir) / "a.m4b").unlink()
        with self.assertRaises(PlaybackError) as ctx:
            self.svc.select(self.ids[0])
        self.assertEqual(ctx.exception.status, 404)

    def test_file_removed_while_selected(self):
        self.svc.select(self.ids[0])
        (Path(self.config.books_dir) / "a.m4b").unlink()
        with self.assertRaises(PlaybackError):
            self.svc.play()

    def test_unknown_book(self):
        with self.assertRaises(PlaybackError):
            self.svc.select(9999)

    def test_controls_without_book(self):
        for action in (self.svc.play, self.svc.pause, self.svc.next_chapter):
            with self.assertRaises(PlaybackError):
                action()
        self.svc.stop()  # no-op, must not raise

    def test_restore_last_book(self):
        self.svc.select(self.ids[1])
        self.svc.shutdown()
        svc = PlaybackService(self.db, self.config, self.make_backend, clock=self.clock)
        svc.restore_last_book()
        self.assertEqual(svc.book["id"], self.ids[1])
        self.assertFalse(svc.playing)
        svc.shutdown()

    def test_shutdown_saves_and_releases(self):
        self.svc.select(self.ids[0])
        self.svc.play()
        self.run_for(12)
        self.svc.shutdown()
        self.assertAlmostEqual(self.saved_position(self.ids[0]), 12_000, delta=10)
        self.assertTrue(self.backends[0].released)

    def test_ticker_thread_stops(self):
        self.svc.start()
        thread = self.svc._thread
        self.svc.shutdown()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
