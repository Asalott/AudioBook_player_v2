import io
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeBackend:
    """Behaves like VlcBackend but time comes from a FakeClock."""

    def __init__(self, clock, length_ms=None):
        self.clock = clock
        self.length_ms = length_ms
        self.path = None
        self._started = False
        self._state = "stopped"
        self._pos = 0
        self._t0 = 0
        self.released = False
        self.stale_reading = None  # simulate VLC reporting a bogus time

    def open(self, path):
        self.stop()
        self.path = str(path)

    def play(self, start_ms=0):
        if self.path is None:
            return False
        if self._started and self._state == "paused":
            self._t0 = self.clock()
            self._state = "playing"
            return True
        self._pos = start_ms or 0
        self._t0 = self.clock()
        self._started = True
        self._state = "playing"
        return True

    def pause(self):
        if self._state == "playing":
            self._pos = self._raw_time()
            self._state = "paused"

    def stop(self):
        self._started = False
        self._state = "stopped"

    @property
    def started(self):
        return self._started and self._state in ("playing", "paused")

    def _raw_time(self):
        if self._state == "playing":
            return int(self._pos + (self.clock() - self._t0) * 1000)
        return self._pos

    def state(self):
        if self._state == "playing" and self.length_ms and self._raw_time() >= self.length_ms:
            return "ended"
        return self._state

    def get_time(self):
        if not self._started:
            return None
        if self.stale_reading is not None:
            return self.stale_reading
        t = self._raw_time()
        return min(t, self.length_ms) if self.length_ms else t

    def set_time(self, ms):
        self._pos = int(ms)
        self._t0 = self.clock()

    def set_volume(self, volume):
        pass

    def release(self):
        self.released = True


def make_config(tmp, **overrides):
    tmp = Path(tmp)
    (tmp / "books").mkdir(exist_ok=True)
    values = dict(
        books_dir=tmp / "books",
        db_path=tmp / "data" / "books.db",
        covers_dir=tmp / "data" / "covers",
        watch_library=False,
        scan_debounce=0.2,
    )
    values.update(overrides)
    return Config(**values)


def create_legacy_db(path, rows, last_book_id=None):
    """Creates a database exactly like the one written by the old app."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute('''
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT,
            path TEXT NOT NULL UNIQUE,
            chapters TEXT,
            cover_path TEXT,
            last_position INTEGER DEFAULT 0,
            series TEXT,
            volume INTEGER
        )''')
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    for row in rows:
        conn.execute(
            "INSERT INTO books (id, title, author, path, chapters, cover_path, last_position, series, volume) "
            "VALUES (:id, :title, :author, :path, :chapters, :cover_path, :last_position, :series, :volume)",
            {"author": "A", "chapters": "[]", "last_position": 0, "cover_path": None, "series": None, "volume": None, **row},
        )
    if last_book_id is not None:
        conn.execute("INSERT INTO settings VALUES ('last_book_id', ?)", (str(last_book_id),))
    conn.commit()
    conn.close()


def ffmpeg():
    return shutil.which("ffmpeg") or (
        str(Path(sys.executable).parent / "ffmpeg.exe") if (Path(sys.executable).parent / "ffmpeg.exe").exists() else None
    )


def make_audiobook(path, seconds=6, title="Testbok", artist="Författare Å", album="Serie, Vol. 2", chapters=3):
    """Creates a small real .m4b with chapters and a cover using ffmpeg.
    Returns None when ffmpeg is not available."""
    exe = ffmpeg()
    if not exe:
        return None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = path.with_suffix(".ffmeta")
    step = seconds * 1000 // chapters
    lines = [";FFMETADATA1", f"title={title}", f"artist={artist}", f"album={album}"]
    for i in range(chapters):
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={i * step}", f"END={(i + 1) * step}", f"title=Kapitel {i + 1}"]
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cover = path.with_suffix(".png")
    from PIL import Image
    Image.new("RGB", (900, 900), (143, 169, 138)).save(cover)
    subprocess.run(
        [exe, "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-i", str(meta), "-i", str(cover), "-map", "0:a", "-map", "2:v", "-map_metadata", "1",
         "-map_chapters", "1", "-c:a", "aac", "-b:a", "32k", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
         "-f", "mp4", str(path)],
        check=True,
    )
    meta.unlink()
    cover.unlink()
    return path


def jpeg_bytes(size=(1200, 1200)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, (200, 180, 150)).save(out, "JPEG")
    return out.getvalue()
