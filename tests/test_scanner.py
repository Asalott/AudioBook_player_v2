import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import FakeBackend, FakeClock, jpeg_bytes, make_audiobook, make_config

from database import Database
from library import BookMeta
from playback import PlaybackService
from scanner import LibraryScanner, walk_library
from watcher import LibraryWatcher


class FakeReader:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def __call__(self, path):
        self.calls.append(Path(path).name)
        if Path(path).name in self.fail:
            raise ValueError("broken file")
        return BookMeta(title=Path(path).stem, author="Någon", duration_ms=60_000,
                        chapters=[{"title": "K", "start": 0.0, "end": 60.0}], cover=jpeg_bytes((300, 300)))


class ScannerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name)
        self.books = Path(self.config.books_dir)
        self.db = Database(self.config.db_path, self.config.books_dir)
        self.reader = FakeReader()
        self.now = time.time() + 100  # every file is "settled"
        self.scanner = LibraryScanner(self.db, self.config, metadata_reader=self.reader, clock=lambda: self.now)

    def tearDown(self):
        self.scanner.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def write(self, rel, data=b"audio"):
        path = self.books / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def by_title(self, include_missing=True):
        return {b["title"]: b for b in self.db.list_books(include_missing=include_missing)}


class TestScanner(ScannerTestCase):
    def test_initial_scan_and_no_work_on_rescan(self):
        self.write("a.m4b")
        self.write("Serie/Åsa – öde del 1.m4b")
        self.write("c.mp3")
        self.write("notes.txt")
        self.write("._a.m4b")          # macOS resource fork
        self.write(".hidden/x.m4b")
        result = self.scanner.scan()
        self.assertEqual(result["added"], 3)
        self.assertEqual(set(self.by_title()), {"a", "Åsa – öde del 1", "c"})
        self.assertEqual(self.db.get_book(self.by_title()["a"]["id"])["chapters"][0]["title"], "K")

        self.reader.calls.clear()
        result = self.scanner.scan()
        self.assertEqual(self.reader.calls, [], "unchanged files must not be opened")
        self.assertEqual(result["unchanged"], 3)

    def test_only_changed_file_is_reread(self):
        self.write("a.m4b")
        self.write("b.m4b")
        self.scanner.scan()
        self.reader.calls.clear()
        self.write("b.m4b", b"new longer audio data")
        result = self.scanner.scan()
        self.assertEqual(self.reader.calls, ["b.m4b"])
        self.assertEqual(result["updated"], 1)

    def test_removed_file_keeps_position_and_comes_back(self):
        path = self.write("a.m4b")
        self.scanner.scan()
        book_id = self.by_title()["a"]["id"]
        self.db.save_position(book_id, 12_345)
        path.unlink()
        result = self.scanner.scan()
        self.assertEqual(result["missing"], 1)
        self.assertEqual(self.db.list_books(), [])  # hidden from the library ...
        self.assertEqual(self.db.get_book(book_id)["last_position"], 12_345)  # ... but not forgotten
        self.write("a.m4b")
        self.scanner.scan()
        self.assertEqual(self.db.get_book(book_id)["last_position"], 12_345)
        self.assertFalse(self.db.get_book(book_id)["missing"])

    def test_renamed_or_moved_file_is_relinked(self):
        self.write("Overlord, Vol. 1 - The Undead King.m4b", b"x" * 100)
        self.scanner.scan()
        book_id = self.db.list_books()[0]["id"]
        self.db.save_position(book_id, 999)
        # Moved into a folder and " - " replaced by a private-use char by a copy tool.
        (self.books / "Overlord, Vol. 1 - The Undead King.m4b").unlink()
        self.write("Overlord/Overlord, Vol. 1 The Undead King.m4b", b"x" * 100)
        result = self.scanner.scan()
        self.assertEqual(result["relinked"], 1)
        self.assertEqual(result["added"], 0)
        books = self.db.list_books(include_missing=True)
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["id"], book_id)
        self.assertEqual(books[0]["last_position"], 999)

    def test_renamed_file_with_same_size_is_relinked(self):
        self.write("old name.m4b", b"y" * 1234)
        self.scanner.scan()
        book_id = self.db.list_books()[0]["id"]
        os.rename(self.books / "old name.m4b", self.books / "completely different.m4b")
        self.scanner.scan()
        self.assertEqual(self.db.list_books()[0]["id"], book_id)

    def test_file_being_copied_is_skipped_until_settled(self):
        self.write("copying.m4b")
        self.now = time.time()  # file was modified "just now"
        result = self.scanner.scan()
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.db.list_books(), [])
        self.now = time.time() + 100
        self.assertEqual(self.scanner.scan()["added"], 1)

    def test_broken_file_does_not_stop_the_scan(self):
        self.write("a.m4b")
        self.write("broken.m4b")
        self.write("c.m4b")
        self.reader.fail.add("broken.m4b")
        result = self.scanner.scan()
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["added"], 2)

    def test_empty_and_missing_folder(self):
        self.assertEqual(self.scanner.scan()["added"], 0)
        self.books.rmdir()
        self.assertEqual(self.scanner.scan()["added"], 0)
        self.assertEqual(walk_library(self.books), {})

    def test_covers_are_cached(self):
        self.write("a.m4b")
        self.scanner.scan()
        book = self.db.list_books()[0]
        self.assertTrue((Path(self.config.covers_dir) / f"{book['id']}_s.jpg").is_file())
        self.assertGreater(book["cover_version"], 0)

    def test_library_version_changes_only_on_changes(self):
        self.write("a.m4b")
        self.scanner.scan()
        version = self.scanner.library_version
        self.scanner.scan()
        self.assertEqual(self.scanner.library_version, version)

    def test_async_scan_reports_progress_and_queues(self):
        gate = threading.Event()
        original = self.reader.__call__

        def slow_reader(path):
            gate.wait(5)
            return original(path)

        self.scanner.read_metadata = slow_reader
        for i in range(3):
            self.write(f"{i}.m4b")
        self.assertEqual(self.scanner.scan_async(), "started")
        time.sleep(0.2)
        status = self.scanner.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["total"], 3)
        self.assertEqual(self.scanner.scan_async(), "queued")
        gate.set()
        self.assertTrue(self.scanner.wait(10))
        self.assertFalse(self.scanner.status()["running"])
        self.assertEqual(len(self.db.list_books()), 3)
        self.assertEqual(self.scanner.status()["last_result"]["reason"], "queued")

    def test_current_book_removed_while_selected(self):
        self.write("a.m4b")
        self.scanner.scan()
        clock = FakeClock()
        playback = PlaybackService(self.db, self.config, lambda: FakeBackend(clock), clock=clock)
        self.scanner.playback = playback
        book_id = self.db.list_books()[0]["id"]
        playback.select(book_id)
        playback.play()
        clock.advance(5)
        (self.books / "a.m4b").unlink()
        self.scanner.scan()
        self.assertIsNone(playback.book)
        self.assertAlmostEqual(self.db.get_book(book_id)["last_position"], 5000, delta=10)
        playback.shutdown()


class TestRealMetadataScan(ScannerTestCase):
    def test_scan_real_file(self):
        if make_audiobook(self.books / "Riktig bok – Åäö.m4b", seconds=4) is None:
            self.skipTest("ffmpeg not available")
        scanner = LibraryScanner(self.db, self.config, clock=lambda: self.now)
        scanner.scan()
        book = self.db.list_books()[0]
        self.assertEqual(len(book["chapters"]), 3)
        self.assertAlmostEqual(book["duration_ms"], 4000, delta=300)
        self.assertEqual(book["author"], "Författare Å")


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.calls = []
        self.fired = threading.Event()

    def tearDown(self):
        self.tmp.cleanup()

    def callback(self):
        self.calls.append(time.monotonic())
        self.fired.set()

    def _check(self, **kwargs):
        watcher = LibraryWatcher(self.dir, self.callback, debounce=0.5, **kwargs)
        watcher.start()
        try:
            time.sleep(0.3)
            for i in range(5):  # a burst of changes -> one callback
                (self.dir / f"book{i}.m4b").write_bytes(b"x" * (i + 1))
                time.sleep(0.05)
            self.assertTrue(self.fired.wait(5), "watcher did not fire")
            time.sleep(0.8)
            self.assertEqual(len(self.calls), 1)
            self.fired.clear()
            (self.dir / "book0.m4b").unlink()
            self.assertTrue(self.fired.wait(5), "deletion not detected")
            self.fired.clear()
            (self.dir / "cover.txt").write_text("ignored")
            self.assertFalse(self.fired.wait(1.2), "irrelevant file triggered a scan")
        finally:
            watcher.stop()

    def test_watchdog(self):
        self._check()

    def test_polling_fallback(self):
        self._check(force_polling=True, poll_interval=0.2)


if __name__ == "__main__":
    unittest.main()
