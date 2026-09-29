# Playback service: owns the audio backend and the "current book" state.
#
# All state is guarded by one lock, so concurrent Flask requests can no
# longer interleave (e.g. /api/status while /api/select-book swaps players).
# A single background ticker thread (1 s) keeps the position up to date,
# handles the sleep timer and the end of a book, and writes the position to
# the database at most every `position_save_interval` seconds while playing
# - plus immediately on pause, stop, book change and shutdown.
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
# Largest wall-clock gap between two ticks that is trusted (protects against
# suspend/resume or a stalled thread).
MAX_TICK_GAP = 5.0
# While VLC is starting or seeking, get_time() can briefly report stale
# values. Readings further than this from the expected position are ignored
# during the guard window.
SEEK_GUARD_SECONDS = 3.0
SEEK_TOLERANCE_MS = 5000
# Chapter "previous" jumps to the start of the current chapter when more
# than this far into it.
PREV_CHAPTER_THRESHOLD_S = 3.0
CHAPTER_EPSILON_S = 0.05


class PlaybackError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class PlaybackService:
    def __init__(self, db, config, backend_factory, clock=time.monotonic):
        self.db = db
        self.config = config
        self._backend_factory = backend_factory
        self._backend = None
        self._clock = clock
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None

        self.book = None
        self._playing = False
        self._position = 0
        self._last_tick = None
        self._last_save = 0.0
        self._saved_position = None
        self._guard_until = 0.0
        self._guard_origin = (0, 0.0)
        self._sleep_remaining_ms = None
        self._listeners = []

    # ----------------------------------------------------------- lifecycle
    @property
    def backend(self):
        if self._backend is None:
            self._backend = self._backend_factory()
        return self._backend

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="playback-ticker", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop_event.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("Playback tick failed")

    def shutdown(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        with self._lock:
            if self.book is not None:
                if self._playing:
                    self._update_position()
                self._persist(force=True)
            if self._backend is not None:
                self._backend.release()
                self._backend = None

    def add_listener(self, callback):
        """callback(event, book=..., **data) - events used by the statistics
        module: 'listened' (ms of real playing time), 'progress' (every tick
        while playing), 'position' (seek/save; flush=True on pause, stop and
        shutdown) and 'finished'."""
        self._listeners.append(callback)

    def _emit(self, event, **data):
        for callback in self._listeners:
            try:
                callback(event, **data)
            except Exception:
                log.exception("Listener failed for %s", event)

    # ---------------------------------------------------------- selection
    def restore_last_book(self):
        """Loads (without playing) the book that was open last time."""
        try:
            book_id = int(self.db.get_setting("last_book_id") or 0)
        except ValueError:
            book_id = 0
        if book_id:
            try:
                self.select(book_id, remember=False)
            except PlaybackError as e:
                log.warning("Could not restore last book %s: %s", book_id, e)

    def select(self, book_id, remember=True):
        with self._lock:
            if self.book is not None and self.book["id"] == book_id:
                return self.book
            book = self.db.get_book(book_id)
            if book is None:
                raise PlaybackError("Boken finns inte", 404)
            path = self.book_file(book)
            if book["missing"] or not path.is_file():
                raise PlaybackError("Ljudfilen saknas", 404)
            self._leave_current_book()
            self.book = book
            self._position = book["last_position"]
            self._saved_position = self._position
            self.backend.open(path)
            if remember:
                self.db.set_setting("last_book_id", book_id)
            return book

    def book_file(self, book):
        return Path(self.config.books_dir) / book["path"]

    def _leave_current_book(self):
        if self.book is None:
            return
        if self._playing:
            self._update_position()
            self._account_listening()
        self._playing = False
        self._persist(force=True)
        self.backend.stop()
        self.book = None

    def forget_book(self, book_id):
        """Called when a book disappears from the library."""
        with self._lock:
            if self.book is not None and self.book["id"] == book_id:
                self._leave_current_book()

    def refresh_book(self, book_id):
        """Reloads metadata (chapters, duration) after a rescan."""
        with self._lock:
            if self.book is not None and self.book["id"] == book_id:
                fresh = self.db.get_book(book_id)
                if fresh:
                    fresh["last_position"] = self._position
                    self.book = fresh

    # ------------------------------------------------------------ controls
    def _require_book(self):
        if self.book is None:
            raise PlaybackError("Ingen bok vald", 400)

    def play(self):
        with self._lock:
            self._require_book()
            if self._playing:
                return
            if not self.book_file(self.book).is_file():
                raise PlaybackError("Ljudfilen saknas", 404)
            duration = self.duration_ms
            if duration and self._position >= duration - 1000:
                self._position = 0  # finished: start over
            if not self.backend.play(self._position):
                raise PlaybackError("Kunde inte starta uppspelningen", 500)
            self._set_guard(self._position)
            self._playing = True
            self._last_tick = self._last_save = self._clock()

    def pause(self):
        with self._lock:
            self._require_book()
            if not self._playing:
                return
            self._update_position()
            self._account_listening()
            self.backend.pause()
            self._playing = False
            self._persist(force=True)

    def toggle(self):
        with self._lock:
            if self._playing:
                self.pause()
            else:
                self.play()

    def stop(self):
        """Saves the position and releases the audio device."""
        with self._lock:
            if self.book is None:
                return
            if self._playing:
                self._update_position()
                self._account_listening()
            self._playing = False
            self.backend.stop()
            self._persist(force=True)

    def seek_to(self, ms):
        with self._lock:
            self._require_book()
            if self._playing:
                self._update_position()
                self._account_listening()
            duration = self.duration_ms
            ms = max(0, int(ms))
            if duration:
                ms = min(ms, max(0, duration - 500))
            self._position = ms
            if self.backend.started:
                self.backend.set_time(ms)
                self._set_guard(ms)
            if not self._playing:
                self._persist(force=True)
            self._emit("position", book=self.book, position_ms=ms, duration_ms=duration)

    def seek_relative(self, delta_ms):
        with self._lock:
            self._require_book()
            if self._playing:
                self._update_position()
            self.seek_to(self._position + int(delta_ms))

    def next_chapter(self):
        with self._lock:
            self._require_book()
            chapters = self.book["chapters"]
            if not chapters:
                return
            index = self._chapter_index(self.position_ms / 1000)
            if index < len(chapters) - 1:
                self.seek_to(chapters[index + 1]["start"] * 1000)

    def prev_chapter(self):
        with self._lock:
            self._require_book()
            chapters = self.book["chapters"]
            if not chapters:
                self.seek_to(0)
                return
            now = self.position_ms / 1000
            index = self._chapter_index(now)
            if now - chapters[index]["start"] > PREV_CHAPTER_THRESHOLD_S or index == 0:
                target = chapters[index]["start"]
            else:
                target = chapters[index - 1]["start"]
            self.seek_to(target * 1000)

    def _chapter_index(self, seconds):
        chapters = self.book["chapters"]
        # Positions are whole milliseconds while chapter starts are floats, so
        # allow a small tolerance at the boundaries.
        seconds += CHAPTER_EPSILON_S
        for i, c in enumerate(chapters):
            if c["start"] <= seconds < c["end"]:
                return i
        if chapters and seconds < chapters[0]["start"]:
            return 0
        return max(0, len(chapters) - 1)

    # --------------------------------------------------------- sleep timer
    def set_sleep_timer(self, minutes):
        with self._lock:
            self._require_book()
            self._sleep_remaining_ms = int(minutes * 60 * 1000) if minutes > 0 else None

    def cancel_sleep_timer(self):
        with self._lock:
            self._sleep_remaining_ms = None

    # -------------------------------------------------------------- status
    @property
    def playing(self):
        return self._playing

    @property
    def position_ms(self):
        with self._lock:
            if self._playing:
                self._update_position()
            return self._position

    @property
    def duration_ms(self):
        if self.book is None:
            return 0
        if self.book.get("duration_ms"):
            return self.book["duration_ms"]
        chapters = self.book["chapters"]
        return int(chapters[-1]["end"] * 1000) if chapters else 0

    def status(self):
        with self._lock:
            if self.book is None:
                return {"book_id": None, "playing": False}
            position = self.position_ms
            chapters = self.book["chapters"]
            return {
                "book_id": self.book["id"],
                "playing": self._playing,
                "time": position,
                "length": self.duration_ms,
                "chapter": self._chapter_index(position / 1000) if chapters else None,
                "sleep_remaining": None if self._sleep_remaining_ms is None
                else round(self._sleep_remaining_ms / 1000),
            }

    # ---------------------------------------------------------- internals
    def _set_guard(self, expected_ms):
        now = self._clock()
        self._guard_until = now + SEEK_GUARD_SECONDS
        self._guard_origin = (expected_ms, now)

    def _update_position(self):
        """Reads the position from the backend, ignoring bogus readings right
        after start/seek."""
        if not self.backend.started:
            return
        reading = self.backend.get_time()
        if reading is None:
            return
        now = self._clock()
        if now < self._guard_until:
            origin_ms, origin_t = self._guard_origin
            expected = origin_ms + ((now - origin_t) * 1000 if self._playing else 0)
            if abs(reading - expected) > SEEK_TOLERANCE_MS:
                return
        self._position = reading

    def _account_listening(self):
        """Counts wall-clock time spent actually playing since the last tick."""
        now = self._clock()
        if self._last_tick is not None and self._playing:
            elapsed = min(max(0.0, now - self._last_tick), MAX_TICK_GAP)
            if elapsed > 0:
                elapsed_ms = int(elapsed * 1000)
                self._emit("listened", book=self.book, ms=elapsed_ms)
                if self._sleep_remaining_ms is not None:
                    self._sleep_remaining_ms -= elapsed_ms
        self._last_tick = now

    def _persist(self, force=False):
        if self.book is None:
            return
        position = int(self._position)
        if not force and self._saved_position is not None and abs(position - self._saved_position) < 1000:
            return
        if position != self._saved_position:
            try:
                self.db.save_position(self.book["id"], position)
                self._saved_position = position
            except Exception:
                log.exception("Could not save position")
        self._last_save = self._clock()
        self._emit("position", book=self.book, position_ms=position,
                   duration_ms=self.duration_ms, flush=force, playing=self._playing)

    def tick(self):
        with self._lock:
            if self.book is None or not self._playing:
                self._last_tick = self._clock()
                return
            state = self.backend.state()
            if state == "ended":
                self._account_listening()
                self._position = self.duration_ms or self._position
                self._playing = False
                self.backend.stop()
                self._sleep_remaining_ms = None
                self._persist(force=True)
                self._emit("finished", book=self.book)
                return
            if state == "error":
                log.error("Playback error in %s", self.book["path"])
                self._account_listening()
                self._playing = False
                self.backend.stop()
                self._persist(force=True)
                return
            self._update_position()
            if state == "playing":
                self._account_listening()
                self._emit("progress", book=self.book, position_ms=self._position,
                           duration_ms=self.duration_ms, playing=True)
            else:
                self._last_tick = self._clock()
            if self._sleep_remaining_ms is not None and self._sleep_remaining_ms <= 0:
                self._sleep_remaining_ms = None
                self.pause()
                return
            if self._clock() - self._last_save >= self.config.position_save_interval:
                self._persist()
