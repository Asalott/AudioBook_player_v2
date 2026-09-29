# SQLite storage for the library, positions and settings.
#
# One shared connection guarded by a lock is used from every thread (Flask
# request threads, the playback ticker and the background scanner). WAL mode
# with synchronous=NORMAL keeps the number of fsyncs - and thereby SD card
# wear - low while still being crash safe.
#
# The schema is versioned with PRAGMA user_version and upgraded by
# `migrate()`. Before an existing database is upgraded a backup copy is
# written next to it, so no listening data can be lost by a migration.
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from config import AUDIO_EXTENSIONS

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

BOOK_COLUMNS = (
    "id, title, author, path, chapters, cover_path, last_position, series, volume, "
    "duration_ms, file_size, file_mtime, missing, cover_version, added_at, updated_at, "
    "favorite, favorited_at, listen_count, total_listened_ms, completion_armed, last_played_at"
)


def match_key(filename):
    """A forgiving key for recognising the same file after it was renamed by a
    copy tool (e.g. ':' or '"' replaced by a private-use character on
    Windows/exFAT, different Unicode normalisation of å/ä/ö, changed case)."""
    stem = PurePosixPath(filename).stem
    stem = unicodedata.normalize("NFC", stem).casefold()
    return "".join(ch for ch in stem if ch.isalnum())


def _pure_path(stored):
    if "\\" in stored or re.match(r"^[A-Za-z]:", stored):
        return PureWindowsPath(stored)
    return PurePosixPath(stored)


class Database:
    def __init__(self, db_path, books_dir=None):
        self.db_path = Path(db_path)
        self.books_dir = Path(books_dir).resolve() if books_dir else None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # ------------------------------------------------------------------ core
    def execute(self, sql, params=()):
        with self._lock:
            with self._conn:
                return self._conn.execute(sql, params)

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql, params=()):
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def transaction(self):
        """Usage: `with db.transaction() as conn: conn.execute(...)` - holds the
        lock for the whole block and commits (or rolls back) at the end."""
        return _Transaction(self)

    def close(self):
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    # ------------------------------------------------------------ migrations
    def schema_version(self):
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _table_exists(self, name):
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None

    def _columns(self, table):
        with self._lock:
            return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def backup(self, suffix):
        target = self.db_path.with_name(f"{self.db_path.name}.{suffix}.bak")
        with self._lock:
            dest = sqlite3.connect(str(target))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        log.warning("Backup of database written to %s", target)
        return target

    def migrate(self):
        version = self.schema_version()
        if version >= SCHEMA_VERSION:
            return
        has_data = self._table_exists("books") and self.query_one("SELECT COUNT(*) AS n FROM books")["n"] > 0
        if has_data:
            self.backup(f"v{version}-{time.strftime('%Y%m%d-%H%M%S')}")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            getattr(self, f"_migrate_to_{target}")()
            with self._lock:
                self._conn.execute(f"PRAGMA user_version = {target}")
            log.info("Database migrated to schema version %d", target)

    def _migrate_to_1(self):
        """Legacy database -> v1: new file tracking columns and library-relative
        paths (the legacy app stored absolute paths, which broke as soon as the
        folder was moved or the database copied to another machine)."""
        with self.transaction() as conn:
            conn.execute("""
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
                )""")
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            existing = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
            for column, coltype in [
                ("last_position", "INTEGER DEFAULT 0"),
                ("series", "TEXT"),
                ("volume", "INTEGER"),
                ("duration_ms", "INTEGER"),
                ("file_size", "INTEGER"),
                ("file_mtime", "REAL"),
                ("missing", "INTEGER NOT NULL DEFAULT 0"),
                ("cover_version", "INTEGER"),
                ("added_at", "REAL"),
                ("updated_at", "REAL"),
            ]:
                if column not in existing:
                    conn.execute(f"ALTER TABLE books ADD COLUMN {column} {coltype}")

            rows = conn.execute("SELECT id, path FROM books").fetchall()
            index = self._library_index() if rows else {}
            taken = set()
            now = time.time()
            for row in rows:
                rel = self._legacy_to_relative(row["path"], index)
                if rel in taken:
                    # Two legacy rows map to the same file: keep the second row
                    # untouched (it is flagged missing by the next scan) rather
                    # than dropping its position.
                    log.warning("Duplicate library path %s for book %s", rel, row["id"])
                    continue
                taken.add(rel)
                try:
                    conn.execute(
                        "UPDATE books SET path=?, file_size=NULL, file_mtime=NULL, "
                        "added_at=COALESCE(added_at, ?) WHERE id=?",
                        (rel, now, row["id"]),
                    )
                except sqlite3.IntegrityError:
                    log.warning("Could not normalise path of book %s", row["id"])

    def _migrate_to_2(self):
        """v1 -> v2: favourites and listening statistics. Existing positions
        are untouched; a book that is currently in progress simply starts
        with zero recorded listening time."""
        with self.transaction() as conn:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
            for column, coltype in [
                ("favorite", "INTEGER NOT NULL DEFAULT 0"),
                ("favorited_at", "REAL"),
                ("listen_count", "INTEGER NOT NULL DEFAULT 0"),
                ("total_listened_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("completion_armed", "INTEGER NOT NULL DEFAULT 1"),
                ("last_played_at", "REAL"),
            ]:
                if column not in existing:
                    conn.execute(f"ALTER TABLE books ADD COLUMN {column} {coltype}")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS listening_daily (
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    day TEXT NOT NULL,
                    ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (book_id, day)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS completions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    completed_at REAL NOT NULL
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_listening_daily_day ON listening_daily(day)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_completions_book ON completions(book_id)")

    def _library_index(self):
        """filename -> relative path and match_key -> relative path for all
        audio files currently in the library folder."""
        by_name, by_key = {}, {}
        if self.books_dir and self.books_dir.is_dir():
            for p in self.books_dir.rglob("*"):
                if p.suffix.lower() in AUDIO_EXTENSIONS and p.is_file():
                    rel = p.relative_to(self.books_dir).as_posix()
                    by_name.setdefault(p.name, rel)
                    by_key.setdefault(match_key(p.name), rel)
        return {"name": by_name, "key": by_key}

    def _legacy_to_relative(self, stored, index):
        p = _pure_path(stored)
        if not p.is_absolute():
            return PurePosixPath(*p.parts).as_posix()
        if self.books_dir:
            try:
                return Path(stored).resolve().relative_to(self.books_dir).as_posix()
            except (ValueError, OSError):
                pass
        if p.name in index.get("name", {}):
            return index["name"][p.name]
        key = match_key(p.name)
        if key in index.get("key", {}):
            return index["key"][key]
        # Unknown location: keep the parts after a "books" folder if there is
        # one, otherwise just the file name. The scanner will re-link it.
        parts = list(p.parts)
        books_idx = [i for i, part in enumerate(parts) if part.lower() == "books"]
        if books_idx and books_idx[-1] + 1 < len(parts):
            return PurePosixPath(*parts[books_idx[-1] + 1:]).as_posix()
        return p.name

    # ---------------------------------------------------------------- books
    def list_books(self, include_missing=False):
        where = "" if include_missing else "WHERE missing = 0"
        return [self._book(r) for r in self.query(f"SELECT {BOOK_COLUMNS} FROM books {where}")]

    def get_book(self, book_id):
        row = self.query_one(f"SELECT {BOOK_COLUMNS} FROM books WHERE id = ?", (book_id,))
        return self._book(row) if row else None

    @staticmethod
    def _book(row):
        try:
            row["chapters"] = json.loads(row["chapters"]) if row["chapters"] else []
        except (TypeError, ValueError):
            row["chapters"] = []
        row["missing"] = bool(row["missing"])
        if "favorite" in row:
            row["favorite"] = bool(row["favorite"])
        row["last_position"] = row["last_position"] or 0
        if not row.get("duration_ms") and row["chapters"]:
            # Books migrated from the old database have no stored duration
            # until they are rescanned; the chapter list knows it.
            row["duration_ms"] = int(row["chapters"][-1]["end"] * 1000)
        return row

    def save_position(self, book_id, milliseconds):
        self.execute(
            "UPDATE books SET last_position = ? WHERE id = ?",
            (max(0, int(milliseconds)), book_id),
        )

    def set_cover_version(self, book_id, version):
        self.execute("UPDATE books SET cover_version = ? WHERE id = ?", (version, book_id))

    def set_favorite(self, book_id, favorite):
        cur = self.execute(
            "UPDATE books SET favorite = ?, favorited_at = ? WHERE id = ?",
            (1 if favorite else 0, time.time() if favorite else None, book_id),
        )
        return cur.rowcount > 0

    # ------------------------------------------------------------- settings
    def get_setting(self, key, default=None):
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, None if value is None else str(value)),
        )


class _Transaction:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        return self.db._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db._conn.commit()
            else:
                self.db._conn.rollback()
        finally:
            self.db._lock.release()
        return False
