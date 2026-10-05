# Incremental library scanner.
#
# A scan first walks the library folder and only stat()s the files (cheap,
# milliseconds even for large libraries). The result is compared with the
# database and only new or changed files are opened to read metadata,
# chapters and cover art. Files that disappeared are *flagged* as missing -
# never deleted - so positions and statistics survive e.g. an unplugged USB
# stick or a file that is being replaced. A new file that looks like a
# missing book (same size, or same name apart from odd characters) is
# re-linked to the old row instead of becoming a new book.
#
# Scans run in one background thread; asking for a scan while one is running
# queues exactly one follow-up scan.
import json
import logging
import os
import threading
import time
from pathlib import Path

import covers
from config import AUDIO_EXTENSIONS
from database import match_key
from library import read_metadata

log = logging.getLogger(__name__)

# Files modified more recently than this are probably still being copied;
# they are skipped and picked up by a follow-up scan.
SETTLE_SECONDS = 3.0


def library_reachable(books_dir):
    """True if the library folder exists and can be listed.

    A missing or unreadable folder usually means an unplugged USB stick or
    an offline network share - not that every book was deleted.
    """
    try:
        with os.scandir(books_dir):
            return True
    except OSError:
        return False


def walk_library(books_dir):
    """{relative posix path: (size, mtime)} for every audio file."""
    found = {}
    books_dir = Path(books_dir)
    if not books_dir.is_dir():
        return found
    stack = [books_dir]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as e:
            log.warning("Cannot read folder %s: %s", current, e)
            continue
        for entry in entries:
            if entry.name.startswith("."):  # hidden files, macOS "._" resource forks
                continue
            try:
                if entry.is_dir(follow_symlinks=True):
                    stack.append(Path(entry.path))
                elif os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS and entry.is_file():
                    st = entry.stat()
                    rel = Path(entry.path).relative_to(books_dir).as_posix()
                    found[rel] = (st.st_size, st.st_mtime)
            except OSError as e:
                log.warning("Cannot stat %s: %s", entry.path, e)
    return found


class LibraryScanner:
    def __init__(self, db, config, playback=None, metadata_reader=read_metadata, clock=time.time):
        self.db = db
        self.config = config
        self.playback = playback
        self.read_metadata = metadata_reader
        self.clock = clock
        self._lock = threading.Lock()          # guards the fields below
        self._run_lock = threading.Lock()      # only one scan at a time
        self._thread = None
        self._pending = False
        self._cancel = threading.Event()
        self.library_version = 0
        self._status = {"running": False, "phase": "idle", "done": 0, "total": 0,
                        "current": None, "last_result": None, "last_finished": None, "error": None}

    # --------------------------------------------------------------- public
    def status(self):
        with self._lock:
            status = dict(self._status)
            status["library_version"] = self.library_version
            return status

    def scan_async(self, reason="manual"):
        """Starts a background scan. Returns 'started' or 'queued'."""
        if self._cancel.is_set():
            return "stopped"
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._pending = True
                return "queued"
            self._pending = False
            self._status.update(running=True, phase="starting", done=0, total=0, current=None, error=None)
            self._thread = threading.Thread(target=self._worker, args=(reason,), name="library-scan", daemon=True)
            self._thread.start()
            return "started"

    def wait(self, timeout=None):
        """Waits until no scan is running (used by tests and scripts)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                thread = self._thread
            if thread is None or not thread.is_alive():
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            thread.join(0.05 if remaining is None else min(0.05, remaining))

    def shutdown(self):
        self._cancel.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=10)

    # --------------------------------------------------------------- worker
    def _worker(self, reason):
        while True:
            try:
                self.scan(reason)
            except Exception as e:
                log.exception("Library scan failed")
                with self._lock:
                    self._status["error"] = str(e)
            with self._lock:
                if self._pending and not self._cancel.is_set():
                    self._pending = False
                    reason = "queued"
                    continue
                self._status.update(running=False, phase="idle", current=None)
                return

    def _progress(self, **values):
        with self._lock:
            self._status.update(values)

    def scan(self, reason="manual"):
        """Synchronous incremental scan. Returns a summary dict."""
        with self._run_lock:
            started = time.monotonic()
            result = {"added": 0, "updated": 0, "missing": 0, "restored": 0,
                      "relinked": 0, "unchanged": 0, "skipped": 0, "errors": 0}
            self._progress(running=True, phase="listing", done=0, total=0, current=None)

            if not library_reachable(self.config.books_dir):
                # Don't flag the whole library as missing while the folder
                # (USB stick, NAS share) is away; the next scan catches up.
                log.warning("Library folder %s is not reachable, scan skipped", self.config.books_dir)
                result.update(offline=True, seconds=round(time.monotonic() - started, 2), reason=reason)
                self._progress(last_result=result, last_finished=self.clock(), phase="done")
                return result

            on_disk = walk_library(self.config.books_dir)
            rows = {b["path"]: b for b in self.db.list_books(include_missing=True)}
            now = self.clock()

            settling = {rel for rel, (_, mtime) in on_disk.items() if now - mtime < SETTLE_SECONDS}
            result["skipped"] = len(settling)

            to_read = []      # (rel, existing row or None)
            for rel, (size, mtime) in on_disk.items():
                if rel in settling:
                    continue
                row = rows.get(rel)
                if row is None:
                    to_read.append((rel, None))
                elif row["file_size"] != size or row["file_mtime"] != mtime:
                    to_read.append((rel, row))
                else:
                    if row["missing"]:
                        self.db.execute("UPDATE books SET missing = 0 WHERE id = ?", (row["id"],))
                        result["restored"] += 1
                    else:
                        result["unchanged"] += 1

            gone = [row for rel, row in rows.items() if rel not in on_disk]

            # Re-link new files to books that disappeared (moved/renamed file).
            orphans = {row["id"]: row for row in gone}
            relinked = []
            for i, (rel, row) in enumerate(to_read):
                if row is not None:
                    continue
                size = on_disk[rel][0]
                match = self._find_orphan(rel, size, orphans)
                if match is not None:
                    del orphans[match["id"]]
                    self.db.execute("UPDATE books SET path = ?, missing = 0 WHERE id = ?", (rel, match["id"]))
                    match["path"] = rel
                    to_read[i] = (rel, match)
                    relinked.append(match["id"])
            result["relinked"] = len(relinked)

            for row in orphans.values():
                if not row["missing"]:
                    self.db.execute("UPDATE books SET missing = 1 WHERE id = ?", (row["id"],))
                    result["missing"] += 1
                    if self.playback is not None:
                        self.playback.forget_book(row["id"])

            self._progress(phase="reading", total=len(to_read))
            for done, (rel, row) in enumerate(to_read):
                if self._cancel.is_set():
                    break
                self._progress(done=done, current=Path(rel).name)
                size, mtime = on_disk[rel]
                try:
                    meta = self.read_metadata(Path(self.config.books_dir) / rel)
                    book_id = self._store(rel, size, mtime, meta, row)
                    if row is None:
                        result["added"] += 1
                    elif row["id"] not in relinked:
                        result["updated"] += 1
                    if self.playback is not None:
                        self.playback.refresh_book(book_id)
                except Exception as e:
                    result["errors"] += 1
                    log.warning("Could not add %s: %s", rel, e)
            self._progress(done=len(to_read), current=None)

            if any(result[k] for k in ("added", "updated", "missing", "restored", "relinked")):
                with self._lock:
                    self.library_version += 1

            result["seconds"] = round(time.monotonic() - started, 2)
            result["reason"] = reason
            self._progress(last_result=result, last_finished=self.clock(), phase="done")
            if settling and not self._cancel.is_set():
                # Files still being copied: look again once they have settled.
                timer = threading.Timer(SETTLE_SECONDS + 1, self.scan_async, args=("settle",))
                timer.daemon = True
                timer.start()
            log.info("Library scan (%s): %s", reason, result)
            return result

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _find_orphan(rel, size, orphans):
        key = match_key(Path(rel).name)
        for row in orphans.values():
            if match_key(Path(row["path"]).name) == key:
                return row
        same_size = [row for row in orphans.values() if row["file_size"] and row["file_size"] == size]
        return same_size[0] if len(same_size) == 1 else None

    def _store(self, rel, size, mtime, meta, row):
        now = self.clock()
        values = (meta.title, meta.author, meta.series, meta.volume,
                  json.dumps(meta.chapters, ensure_ascii=False), meta.duration_ms or None, size, mtime, now)
        if row is None:
            cur = self.db.execute(
                "INSERT INTO books (title, author, series, volume, chapters, duration_ms, file_size, "
                "file_mtime, updated_at, path, added_at, missing, last_position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                values + (rel, now),
            )
            book_id = cur.lastrowid
        else:
            book_id = row["id"]
            self.db.execute(
                "UPDATE books SET title=?, author=?, series=?, volume=?, chapters=?, duration_ms=?, "
                "file_size=?, file_mtime=?, updated_at=?, missing=0 WHERE id=?",
                values + (book_id,),
            )
        ok = covers.write_cover_cache(self.config.covers_dir, book_id, meta.cover)
        self.db.set_cover_version(book_id, int(now) if ok else 0)
        return book_id
