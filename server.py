# Flask server: JSON API + static UI.
#
# `create_app()` builds everything (database, playback service, scanner and
# watcher) so tests can create isolated apps with a fake audio backend.
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, abort, jsonify, redirect, request, send_file, send_from_directory

import covers
from config import BASE_DIR, Config
from database import Database, _pure_path
from library import read_metadata
from nas_sync import NasError, NasSync
from playback import PlaybackError, PlaybackService
from scanner import LibraryScanner
from stats import StatsRecorder
from watcher import LibraryWatcher

log = logging.getLogger(__name__)

STATIC_DIR = BASE_DIR / "static"


def _number(data, key, default=None):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlaybackError(f"'{key}' måste vara ett tal", 400)
    return value


def create_app(config=None, backend_factory=None, start_background=True, nas_source_factory=None):
    config = config or Config.from_env()
    if backend_factory is None:
        from player import VlcBackend
        backend_factory = VlcBackend

    db = Database(config.db_path, config.books_dir)
    playback = PlaybackService(db, config, backend_factory)
    scanner = LibraryScanner(db, config, playback)
    stats = StatsRecorder(db, config)
    nas = NasSync(db, config, scanner, **({"source_factory": nas_source_factory} if nas_source_factory else {}))
    playback.add_listener(stats.on_event)

    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.json.ensure_ascii = False
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
    state = SimpleNamespace(config=config, db=db, playback=playback, scanner=scanner, stats=stats, nas=nas, watcher=None)
    app.extensions["audiobook"] = state
    cover_lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def cover_url(book, size):
        version = book.get("cover_version")
        if version == 0:
            return None  # known to have no cover
        return f"/covers/{book['id']}?s={size}&v={version or 0}"

    def book_json(book, chapters=False):
        position = book["last_position"]
        if playback.book is not None and playback.book["id"] == book["id"]:
            position = playback.position_ms
        duration = book.get("duration_ms") or 0
        data = {
            "id": book["id"],
            "title": book["title"],
            "author": book["author"],
            "series": book["series"],
            "volume": book["volume"],
            "duration_ms": duration,
            "position_ms": position,
            "progress": min(1.0, position / duration) if duration else 0,
            "cover": cover_url(book, "large"),
            "cover_small": cover_url(book, "small"),
            "added_at": book.get("added_at"),
            "favorite": book.get("favorite", False),
            "listen_count": book.get("listen_count", 0),
            "listened_ms": book.get("total_listened_ms", 0),
            "last_played_at": book.get("last_played_at"),
        }
        if chapters:
            data["chapters"] = book["chapters"]
        return data

    @app.errorhandler(PlaybackError)
    def playback_error(e):
        return jsonify({"status": "error", "message": str(e)}), e.status

    @app.errorhandler(NasError)
    def nas_error(e):
        return jsonify({"status": "error", "message": str(e)}), 400

    # --------------------------------------------------------------- pages
    @app.get("/")
    def index():
        # Cache-busting: static URLs carry the newest static file mtime, so an
        # update on the Pi is picked up without clearing the kiosk cache.
        version = str(int(max(f.stat().st_mtime for f in STATIC_DIR.iterdir())))
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__V__", version)
        response = app.response_class(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/spelare.html")
    @app.get("/bibliotek.html")
    def legacy_pages():
        return redirect("/#/player" if request.path == "/spelare.html" else "/")

    # ------------------------------------------------------------ settings
    THEMES = ("light", "dark", "evening", "system")

    @app.get("/api/settings")
    def get_settings():
        return jsonify({"theme": db.get_setting("theme", "evening")})

    @app.post("/api/settings")
    def set_settings():
        data = request.get_json(silent=True) or {}
        theme = data.get("theme")
        if theme not in THEMES:
            raise PlaybackError("Okänt tema", 400)
        db.set_setting("theme", theme)
        return jsonify({"theme": theme})

    # --------------------------------------------------------------- books
    @app.get("/api/books")
    def get_books():
        books = db.list_books()
        books.sort(key=lambda b: (b["series"] is None, (b["series"] or "").casefold(),
                                  b["volume"] if b["volume"] is not None else 1_000_000,
                                  b["title"].casefold()))
        return jsonify([book_json(b) for b in books])

    @app.get("/covers/<int:book_id>")
    def get_cover(book_id):
        size = "small" if request.args.get("s") == "small" else "large"
        path = covers.cover_file(config.covers_dir, book_id, size)
        if not path.is_file():
            with cover_lock:
                if not path.is_file() and not _build_cover(book_id):
                    abort(404)
        response = send_file(path, mimetype="image/jpeg", conditional=True, max_age=31536000)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    def _build_cover(book_id):
        """Creates the cached cover on demand (books migrated from the old
        database, or a cache that was deleted)."""
        book = db.get_book(book_id)
        if book is None:
            return False
        data = None
        if book.get("cover_path"):
            legacy = Path(config.covers_dir) / _pure_path(book["cover_path"]).name
            if legacy.is_file():
                data = legacy.read_bytes()
        if data is None:
            audio = playback.book_file(book)
            if audio.is_file():
                data = read_metadata(audio).cover
        ok = covers.write_cover_cache(config.covers_dir, book_id, data)
        if book.get("cover_version") in (None, 0) or not ok:
            db.set_cover_version(book_id, 1 if ok else 0)
        return ok

    # ------------------------------------------------------------ playback
    @app.post("/api/select-book")
    def select_book():
        data = request.get_json(silent=True) or {}
        book_id = data.get("id")
        if not isinstance(book_id, int):
            raise PlaybackError("Ogiltigt bok-id", 400)
        playback.select(book_id)
        return jsonify({"status": "ok"})

    @app.get("/api/current-book")
    def get_current_book():
        book = playback.book
        if book is None:
            return jsonify({"status": "error", "message": "Ingen bok vald"}), 404
        data = book_json(book, chapters=True)
        data["position"] = data["position_ms"]  # legacy field name
        return jsonify(data)

    @app.post("/api/play")
    def play():
        playback.play()
        return jsonify({"status": "playing"})

    @app.post("/api/pause")
    def pause():
        playback.pause()
        return jsonify({"status": "pause"})

    @app.post("/api/toggle")
    def toggle():
        playback.toggle()
        return jsonify(playback.status())

    @app.post("/api/stop")
    def stop():
        playback.stop()
        return jsonify({"status": "stop"})

    @app.post("/api/seek")
    def seek():
        data = request.get_json(silent=True) or {}
        if "to_ms" in data:
            playback.seek_to(_number(data, "to_ms"))
        else:
            playback.seek_relative(_number(data, "ms", 0))
        return jsonify(playback.status())

    @app.post("/api/next-chapter")
    def next_chapter():
        playback.next_chapter()
        return jsonify(playback.status())

    @app.post("/api/prev-chapter")
    def prev_chapter():
        playback.prev_chapter()
        return jsonify(playback.status())

    @app.post("/api/sleep-timer")
    def sleep_timer():
        data = request.get_json(silent=True) or {}
        minutes = _number(data, "minutes", 0)
        if minutes < 0 or minutes > 24 * 60:
            raise PlaybackError("Ogiltig tid", 400)
        playback.set_sleep_timer(minutes)
        return jsonify(playback.status())

    @app.post("/api/sleep-timer/cancel")
    def cancel_sleep_timer():
        playback.cancel_sleep_timer()
        return jsonify(playback.status())

    @app.get("/api/status")
    def status():
        data = playback.status()
        scan = scanner.status()
        data["library_version"] = scan["library_version"]
        data["scanning"] = scan["running"]
        return jsonify(data)

    @app.post("/api/save-position")
    def save_position():
        # Kept for old clients. The server saves by itself now.
        return jsonify({"status": "ok"})

    # ------------------------------------------------- favourites & stats
    @app.post("/api/books/<int:book_id>/favorite")
    def set_favorite(book_id):
        data = request.get_json(silent=True) or {}
        favorite = data.get("favorite")
        if not isinstance(favorite, bool):
            raise PlaybackError("'favorite' måste vara true eller false", 400)
        if not db.set_favorite(book_id, favorite):
            raise PlaybackError("Boken finns inte", 404)
        if playback.book is not None and playback.book["id"] == book_id:
            playback.book["favorite"] = favorite
        return jsonify({"id": book_id, "favorite": favorite})

    @app.get("/api/stats")
    def get_stats():
        summary = stats.summary()
        for key in ("top", "books", "favorites"):
            for b in summary[key]:
                b["cover_small"] = cover_url(b, "small")
        return jsonify(summary)

    # ------------------------------------------------------------- library
    @app.post("/api/scan-library")
    def scan_library():
        result = scanner.scan_async("manual")
        return jsonify({"status": result, **scanner.status()}), 202

    @app.get("/api/scan-status")
    def scan_status():
        data = scanner.status()
        data["watching"] = state.watcher.mode if state.watcher is not None else None
        return jsonify(data)

    # ----------------------------------------------------------------- NAS
    @app.get("/api/nas")
    def get_nas():
        return jsonify({**nas.public_settings(), **nas.status()})

    @app.post("/api/nas")
    def set_nas():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise NasError("Ogiltig förfrågan")
        nas.save_settings(data)
        return jsonify({**nas.public_settings(), **nas.status()})

    @app.post("/api/nas/test")
    def test_nas():
        data = request.get_json(silent=True)
        return jsonify(nas.test(data if isinstance(data, dict) else None))

    @app.post("/api/nas/sync")
    def sync_nas():
        result = nas.sync_async("manual")
        return jsonify({"status": result, **nas.status()}), 202

    # --------------------------------------------------------- background
    # Startup deliberately does *not* scan the library: the app starts
    # straight from the database. The watcher picks up changes made while
    # the app is running, and the settings view has a manual rescan button.
    if start_background:
        playback.restore_last_book()
        playback.start()
        nas.start()
        if config.watch_library:
            state.watcher = LibraryWatcher(config.books_dir, lambda: scanner.scan_async("watcher"),
                                           debounce=config.scan_debounce)
            try:
                state.watcher.start()
            except Exception:
                log.exception("Could not start the library watcher")
                state.watcher = None

    return app


def shutdown_app(app):
    state = app.extensions["audiobook"]
    if state.watcher is not None:
        state.watcher.stop()
    state.nas.shutdown()
    if state.scanner is not None:
        state.scanner.shutdown()
    state.playback.shutdown()
    state.stats.flush()
    state.db.close()


if __name__ == "__main__":
    import Main
    Main.main()
