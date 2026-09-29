import json
import tempfile
import unittest
from pathlib import Path

from helpers import FakeBackend, FakeClock, create_legacy_db, jpeg_bytes, make_config

from server import create_app, shutdown_app


class ServerTestCase(unittest.TestCase):
    legacy_rows = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name)
        self.clock = FakeClock()
        for name in ("a.m4b", "b.m4b"):
            (Path(self.config.books_dir) / name).write_bytes(b"x")
        if self.legacy_rows is not None:
            create_legacy_db(self.config.db_path, self.legacy_rows)
        self.app = create_app(self.config, backend_factory=lambda: FakeBackend(self.clock),
                              start_background=False)
        self.state = self.app.extensions["audiobook"]
        self.client = self.app.test_client()

    def tearDown(self):
        shutdown_app(self.app)
        self.tmp.cleanup()

    def add_book(self, path, **cols):
        values = {"title": path, "path": path, "chapters": "[]", "duration_ms": 600_000, **cols}
        keys = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return self.state.db.execute(f"INSERT INTO books ({keys}) VALUES ({marks})", tuple(values.values())).lastrowid


class TestApi(ServerTestCase):
    def test_books_without_cover_do_not_crash(self):
        # Old bug: Path(None) raised TypeError and the whole library failed.
        self.add_book("a.m4b", cover_path=None)
        res = self.client.get("/api/books")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()[0]["title"], "a.m4b")

    def test_missing_books_are_hidden(self):
        self.add_book("a.m4b")
        self.add_book("gone.m4b", missing=1)
        titles = [b["title"] for b in self.client.get("/api/books").get_json()]
        self.assertEqual(titles, ["a.m4b"])

    def test_sorting_by_series_and_volume(self):
        self.add_book("a.m4b", title="Vol 2", series="S", volume=2)
        self.add_book("b.m4b", title="Vol 10", series="S", volume=10)
        self.add_book("c.m4b", title="Fristående", series=None)
        self.add_book("d.m4b", title="Vol 1", series="S", volume=1)
        titles = [b["title"] for b in self.client.get("/api/books").get_json()]
        self.assertEqual(titles, ["Vol 1", "Vol 2", "Vol 10", "Fristående"])

    def test_bad_requests_return_400(self):
        book = self.add_book("a.m4b")
        self.assertEqual(self.client.post("/api/select-book").status_code, 400)
        self.assertEqual(self.client.post("/api/select-book", json={"id": "1"}).status_code, 400)
        self.client.post("/api/select-book", json={"id": book})
        self.assertEqual(self.client.post("/api/sleep-timer", json={"minutes": "30"}).status_code, 400)
        self.assertEqual(self.client.post("/api/seek", json={"ms": None}).status_code, 400)
        self.assertEqual(self.client.post("/api/seek", data="not json").status_code, 200)  # defaults to 0

    def test_controls_without_book(self):
        self.assertEqual(self.client.post("/api/play").status_code, 400)
        self.assertEqual(self.client.get("/api/current-book").status_code, 404)
        self.assertEqual(self.client.get("/api/status").get_json()["book_id"], None)

    def test_select_missing_file(self):
        book = self.add_book("does-not-exist.m4b")
        res = self.client.post("/api/select-book", json={"id": book})
        self.assertEqual(res.status_code, 404)

    def test_play_status_and_sleep_timer(self):
        book = self.add_book("a.m4b")
        self.client.post("/api/select-book", json={"id": book})
        self.assertEqual(self.client.post("/api/toggle").get_json()["playing"], True)
        self.clock.advance(5)
        status = self.client.get("/api/status").get_json()
        self.assertAlmostEqual(status["time"], 5000, delta=10)
        self.assertEqual(self.client.post("/api/sleep-timer", json={"minutes": 30}).get_json()["sleep_remaining"], 1800)
        self.assertIsNone(self.client.post("/api/sleep-timer/cancel").get_json()["sleep_remaining"])
        self.client.post("/api/seek", json={"to_ms": 60_000})
        cur = self.client.get("/api/current-book").get_json()
        self.assertEqual(cur["position_ms"], 60_000)
        self.client.post("/api/pause")
        self.assertEqual(self.state.db.get_book(book)["last_position"], 60_000)

    def test_migrated_book_without_duration_uses_chapters(self):
        # Legacy rows have no duration_ms until rescanned; progress must still work.
        chapters = json.dumps([{"title": "1", "start": 0, "end": 3600}, {"title": "2", "start": 3600, "end": 36000}])
        book = self.add_book("a.m4b", chapters=chapters, duration_ms=None, last_position=120_000,
                             total_listened_ms=120_000)
        listed = self.client.get("/api/books").get_json()[0]
        self.assertEqual(listed["duration_ms"], 36_000_000)
        self.assertAlmostEqual(listed["progress"], 120_000 / 36_000_000)
        stats = self.client.get("/api/stats").get_json()
        self.assertGreater(stats["books"][0]["progress"], 0)
        self.assertEqual(stats["books"][0]["id"], book)

    def test_unicode_titles_in_json(self):
        self.add_book("a.m4b", title="Åsa & älgen – öde")
        res = self.client.get("/api/books")
        self.assertIn("Åsa & älgen – öde".encode("utf-8"), res.data)


class TestPagesAndSettings(ServerTestCase):
    def test_index_is_versioned_and_not_cached(self):
        res = self.client.get("/")
        html = res.get_data(as_text=True)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("__V__", html)
        self.assertRegex(html, r'/static/app\.js\?v=\d+')
        self.assertEqual(res.headers["Cache-Control"], "no-cache")
        for asset in ("/static/app.js", "/static/app.css"):
            r = self.client.get(asset)
            self.assertEqual(r.status_code, 200)
            r.close()

    def test_old_pages_redirect(self):
        self.assertEqual(self.client.get("/spelare.html").headers["Location"], "/#/player")
        self.assertEqual(self.client.get("/bibliotek.html").headers["Location"], "/")

    def test_theme_setting(self):
        self.assertEqual(self.client.get("/api/settings").get_json()["theme"], "evening")
        self.assertEqual(self.client.post("/api/settings", json={"theme": "dark"}).status_code, 200)
        self.assertEqual(self.client.get("/api/settings").get_json()["theme"], "dark")
        self.assertEqual(self.client.post("/api/settings", json={"theme": "neon"}).status_code, 400)


class TestScanApi(ServerTestCase):
    def test_startup_does_not_scan(self):
        # Files exist in the folder but the app starts from the (empty) database.
        self.assertEqual(self.client.get("/api/books").get_json(), [])
        self.assertIsNone(self.client.get("/api/scan-status").get_json()["last_result"])

    def test_manual_scan_runs_in_background(self):
        from library import BookMeta
        self.state.scanner.read_metadata = lambda p: BookMeta(title=p.stem, duration_ms=1000)
        self.state.scanner.clock = lambda: 4102444800  # far future: all files settled
        version = self.client.get("/api/status").get_json()["library_version"]
        res = self.client.post("/api/scan-library")
        self.assertEqual(res.status_code, 202)
        self.assertIn(res.get_json()["status"], ("started", "queued"))
        self.assertTrue(self.state.scanner.wait(10))
        self.assertEqual(sorted(b["title"] for b in self.client.get("/api/books").get_json()), ["a", "b"])
        status = self.client.get("/api/scan-status").get_json()
        self.assertEqual(status["last_result"]["added"], 2)
        self.assertGreater(self.client.get("/api/status").get_json()["library_version"], version)


class TestCovers(ServerTestCase):
    def test_cover_generated_from_legacy_file_and_downscaled(self):
        covers = Path(self.config.covers_dir)
        covers.mkdir(parents=True, exist_ok=True)
        (covers / 'Weird\uf022 name #1 \xe5.jpg').write_bytes(jpeg_bytes((1600, 1600)))
        book = self.add_book("a.m4b", cover_path='covers\\Weird\uf022 name #1 \xe5.jpg')
        books = self.client.get("/api/books").get_json()
        res = self.client.get(books[0]["cover_small"])
        self.assertEqual(res.status_code, 200)
        self.assertIn("immutable", res.headers["Cache-Control"])
        from PIL import Image
        import io
        self.assertLessEqual(max(Image.open(io.BytesIO(res.data)).size), 240)
        large = self.client.get(f"/covers/{book}")
        self.assertLessEqual(max(Image.open(io.BytesIO(large.data)).size), 480)
        large.close()
        res.close()

    def test_book_without_any_cover(self):
        book = self.add_book("a.m4b")
        self.assertEqual(self.client.get(f"/covers/{book}").status_code, 404)
        # Remembered: the next listing has no cover URL at all.
        self.assertIsNone(self.client.get("/api/books").get_json()[0]["cover"])

    def test_path_traversal_is_impossible(self):
        self.assertEqual(self.client.get("/covers/..%2Fbooks.db").status_code, 404)


class TestLegacyStartup(ServerTestCase):
    legacy_rows = [{"id": 5, "title": "Gammal", "last_position": 1234, "path": r"C:\old\books\a.m4b",
                    "chapters": json.dumps([{"title": "K1", "start": 0, "end": 10}])}]

    def test_legacy_book_plays_from_new_location(self):
        res = self.client.post("/api/select-book", json={"id": 5})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get("/api/current-book").get_json()["position"], 1234)


if __name__ == "__main__":
    unittest.main()
