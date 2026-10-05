# File system watcher for the library folder.
#
# Uses watchdog (inotify on the Raspberry Pi) when installed. Events are
# debounced: the callback runs once, `debounce` seconds after the *last*
# change, so copying a 500 MB book (thousands of "modified" events) triggers
# a single incremental rescan. Without watchdog a slow polling fallback that
# only compares file sizes/mtimes is used.
import logging
import threading
import time
from pathlib import Path

from config import AUDIO_EXTENSIONS

log = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - depends on the installation
    Observer = None
    FileSystemEventHandler = object


def _relevant(path):
    name = Path(path).name
    if name.startswith("."):
        return False
    suffix = Path(path).suffix.lower()
    # Directory events have no suffix; they matter when a folder of books is
    # moved or deleted.
    return suffix in AUDIO_EXTENSIONS or suffix == ""


class _Handler(FileSystemEventHandler):
    def __init__(self, poke):
        super().__init__()
        self._poke = poke

    def on_any_event(self, event):
        if event.event_type in ("opened", "closed_no_write"):
            return
        paths = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
        if any(p and _relevant(p) for p in paths):
            self._poke()


class LibraryWatcher:
    def __init__(self, books_dir, on_change, debounce=5.0, poll_interval=60.0, force_polling=False):
        self.books_dir = Path(books_dir)
        self.on_change = on_change
        self.debounce = debounce
        self.poll_interval = poll_interval
        self.force_polling = force_polling or Observer is None
        self._observer = None
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._last_event = 0.0
        self._threads = []

    @property
    def mode(self):
        return "polling" if self.force_polling else "inotify/watchdog"

    def start(self):
        if not self.books_dir.is_dir():
            # Don't create the folder: it may be an offline mount point, and an
            # empty local folder there would look like a deleted library.
            # Polling notices when it shows up.
            log.warning("Library folder %s does not exist yet, polling for it", self.books_dir)
            self.force_polling = True
        if not self.force_polling:
            try:
                self._observer = Observer()
                self._observer.schedule(_Handler(self.poke), str(self.books_dir), recursive=True)
                self._observer.daemon = True
                self._observer.start()
            except Exception as e:  # e.g. inotify watch limit reached
                log.warning("File watcher unavailable (%s), falling back to polling", e)
                self._observer = None
                self.force_polling = True
        if self.force_polling:
            self._spawn(self._poll_loop, "library-poll")
        self._spawn(self._debounce_loop, "library-debounce")
        log.info("Watching %s (%s)", self.books_dir, self.mode)

    def _spawn(self, target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def poke(self):
        self._last_event = time.monotonic()
        self._changed.set()

    def _debounce_loop(self):
        while not self._stop.is_set():
            if not self._changed.wait(0.5):
                continue
            # Wait until no event has arrived for `debounce` seconds.
            while not self._stop.is_set():
                quiet = time.monotonic() - self._last_event
                if quiet >= self.debounce:
                    break
                self._stop.wait(self.debounce - quiet)
            if self._stop.is_set():
                return
            self._changed.clear()
            try:
                self.on_change()
            except Exception:
                log.exception("Library change callback failed")

    def _poll_loop(self):
        from scanner import walk_library
        previous = walk_library(self.books_dir)
        while not self._stop.wait(self.poll_interval):
            current = walk_library(self.books_dir)
            if current != previous:
                previous = current
                self.poke()

    def stop(self):
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads = []
