import tempfile
import unittest
from pathlib import Path

from helpers import create_legacy_db, make_config

from database import Database, match_key


class TestLegacyMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name)
        books = Path(self.config.books_dir)
        (books / "Serie").mkdir()
        # File names as they look on disk after being copied from Windows.
        (books / "Overlord, Vol. 1\uf022 The Undead King [B09].m4b").write_bytes(b"x")
        (books / "Serie" / "Åsa och älgen.m4b").write_bytes(b"x")
        (books / "Moved to other name.m4b").write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_absolute_windows_paths_become_relative_and_positions_survive(self):
        create_legacy_db(self.config.db_path, [
            {"id": 3, "title": "Overlord", "last_position": 5051,
             "path": "C:\\Users\\X\\Prodjeckt\\AudioBook_player\\books\\Overlord, Vol. 1\uf022 The Undead King [B09].m4b"},
            {"id": 7, "title": "Åsa", "last_position": 21059780,
             "path": r"C:\Users\X\old place\books\Serie\Åsa och älgen.m4b"},
            {"id": 9, "title": "Gone", "last_position": 777,
             "path": "/home/pi/books/Not there anymore.m4b"},
        ], last_book_id=7)

        db = Database(self.config.db_path, self.config.books_dir)
        try:
            self.assertEqual(db.schema_version() >= 1, True)
            by_id = {b["id"]: b for b in db.list_books(include_missing=True)}
            self.assertEqual(by_id[3]["path"], "Overlord, Vol. 1\uf022 The Undead King [B09].m4b")
            self.assertEqual(by_id[7]["path"], "Serie/Åsa och älgen.m4b")
            self.assertEqual(by_id[9]["path"], "Not there anymore.m4b")
            self.assertEqual(by_id[3]["last_position"], 5051)
            self.assertEqual(by_id[7]["last_position"], 21059780)
            self.assertEqual(by_id[9]["last_position"], 777)
            self.assertEqual(db.get_setting("last_book_id"), "7")
        finally:
            db.close()
        backups = list(Path(self.config.db_path).parent.glob("books.db.v0-*.bak"))
        self.assertEqual(len(backups), 1)

    def test_path_matched_by_forgiving_key(self):
        # "Overlord, Vol. 1: The Undead King" on Linux, "\uf022" on Windows.
        create_legacy_db(self.config.db_path, [
            {"id": 1, "title": "Overlord", "last_position": 99,
             "path": "/media/usb/audio/Overlord, Vol. 1: The Undead King [B09].m4b"},
        ])
        db = Database(self.config.db_path, self.config.books_dir)
        try:
            self.assertEqual(db.get_book(1)["path"], "Overlord, Vol. 1\uf022 The Undead King [B09].m4b")
        finally:
            db.close()

    def test_migration_is_idempotent(self):
        create_legacy_db(self.config.db_path, [{"id": 1, "title": "x", "path": "/a/books/Serie/Åsa och älgen.m4b",
                                                "last_position": 5}])
        Database(self.config.db_path, self.config.books_dir).close()
        db = Database(self.config.db_path, self.config.books_dir)
        try:
            self.assertEqual(db.get_book(1)["path"], "Serie/Åsa och älgen.m4b")
            self.assertEqual(db.get_book(1)["last_position"], 5)
        finally:
            db.close()
        self.assertEqual(len(list(Path(self.config.db_path).parent.glob("*.bak"))), 1)

    def test_fresh_database(self):
        db = Database(self.config.db_path, self.config.books_dir)
        try:
            self.assertEqual(db.list_books(), [])
            self.assertIsNone(db.get_setting("last_book_id"))
        finally:
            db.close()
        self.assertEqual(list(Path(self.config.db_path).parent.glob("*.bak")), [])

    def test_corrupt_chapters_json_does_not_crash(self):
        create_legacy_db(self.config.db_path, [{"id": 1, "title": "x", "path": "x.m4b", "chapters": "{not json"}])
        db = Database(self.config.db_path, self.config.books_dir)
        try:
            self.assertEqual(db.get_book(1)["chapters"], [])
        finally:
            db.close()

    def test_match_key(self):
        self.assertEqual(match_key("Overlord, Vol. 1\uf022 X.m4b"), match_key("overlord vol 1: x.M4B"))
        # NFC vs NFD å must match
        self.assertEqual(match_key("A\u030asa.m4b"), match_key("\u00c5sa.m4b"))


if __name__ == "__main__":
    unittest.main()
