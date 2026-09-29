import tempfile
import unittest
from pathlib import Path

from helpers import make_audiobook

import covers
from library import clean_series_name, clean_title, read_metadata


class TestTitles(unittest.TestCase):
    def test_clean_title_keeps_swedish_letters(self):
        self.assertEqual(clean_title("Åsa & älgen – öde [B00ABC]"), "Åsa & älgen öde")

    def test_clean_title_removes_private_use_characters(self):
        self.assertEqual(clean_title("Overlord, Vol. 1 The Undead King [B09CVBKH5L]"),
                         "Overlord, Vol. 1 The Undead King")

    def test_series_name(self):
        self.assertEqual(clean_series_name("Overlord, Vol. 3: The Bloody Valkyrie"), ("Overlord", 3))
        self.assertEqual(clean_series_name("Heroes of Olympus (Book 2)"), ("Heroes of Olympus", None))


class TestBrokenFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_garbage_file_gives_fallback_metadata(self):
        path = self.dir / "Trasig bok [X1].m4b"
        path.write_bytes(b"\x00not really audio" * 10)
        meta = read_metadata(path)
        self.assertEqual(meta.title, "Trasig bok")
        self.assertEqual(meta.author, "Okänd")
        self.assertEqual(meta.chapters, [])
        self.assertIsNone(meta.cover)

    def test_empty_file(self):
        path = self.dir / "tom.m4b"
        path.write_bytes(b"")
        self.assertEqual(read_metadata(path).duration_ms, 0)

    def test_corrupt_cover_data_is_not_fatal(self):
        self.assertFalse(covers.write_cover_cache(self.dir, 1, b"not an image"))
        self.assertFalse(covers.cover_file(self.dir, 1).exists())


class TestRealFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = make_audiobook(Path(cls.tmp.name) / "Serie, Vol. 2 – Åäö [B0TEST].m4b")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        if self.path is None:
            self.skipTest("ffmpeg not available")

    def test_metadata(self):
        meta = read_metadata(self.path)
        self.assertEqual(meta.title, "Serie, Vol. 2 Åäö")
        self.assertEqual(meta.author, "Författare Å")
        self.assertEqual((meta.series, meta.volume), ("Serie", 2))
        self.assertEqual([c["title"] for c in meta.chapters], ["Kapitel 1", "Kapitel 2", "Kapitel 3"])
        self.assertAlmostEqual(meta.duration_ms, 6000, delta=300)
        self.assertTrue(meta.cover)


if __name__ == "__main__":
    unittest.main()
