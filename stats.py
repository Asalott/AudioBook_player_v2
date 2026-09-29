# Listening statistics.
#
# The playback service reports actual playing time ("listened" events: wall
# clock time while VLC is really playing - pauses and seeks add nothing).
# Time is summed in memory and written to SQLite every
# `stats_flush_interval` seconds and whenever playback pauses/stops, so a
# normal listening hour costs ~60 small writes instead of 3600.
#
# A "listen" (listen_count) is counted when playback reaches
# `completion_threshold` (95 %) of a book. It is then disarmed until the
# book is started over (position below 10 %), so seeking back and forth
# near the end never counts twice.
import datetime as dt
import logging
import threading
import time

log = logging.getLogger(__name__)

REARM_FRACTION = 0.10


def day_key(timestamp):
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


class StatsRecorder:
    def __init__(self, db, config, wall=time.time, clock=time.monotonic):
        self.db = db
        self.config = config
        self.wall = wall
        self.clock = clock
        self._lock = threading.Lock()
        self._pending = {}          # (book_id, day) -> ms
        self._last_flush = clock()
        self._armed = {}            # book_id -> bool (cache of completion_armed)

    # ------------------------------------------------------------- events
    def on_event(self, event, book=None, **data):
        if book is None:
            return
        if event == "listened":
            self._add(book["id"], data.get("ms", 0))
            if self.clock() - self._last_flush >= self.config.stats_flush_interval:
                self.flush()
        elif event in ("progress", "position"):
            self._check_completion(book, data.get("position_ms", 0), data.get("duration_ms", 0),
                                   seek=event == "position" and not data.get("playing", False))
            if data.get("flush"):
                self.flush()
        elif event == "finished":
            duration = book.get("duration_ms") or 0
            self._check_completion(book, duration, duration, seek=False)
            self.flush()

    def _add(self, book_id, ms):
        if ms <= 0:
            return
        key = (book_id, day_key(self.wall()))
        with self._lock:
            self._pending[key] = self._pending.get(key, 0) + int(ms)

    def _is_armed(self, book_id):
        if book_id not in self._armed:
            row = self.db.query_one("SELECT completion_armed FROM books WHERE id = ?", (book_id,))
            self._armed[book_id] = bool(row and row["completion_armed"])
        return self._armed[book_id]

    def _check_completion(self, book, position_ms, duration_ms, seek):
        if not duration_ms:
            return
        fraction = position_ms / duration_ms
        book_id = book["id"]
        armed = self._is_armed(book_id)
        if not armed and fraction < REARM_FRACTION:
            self.db.execute("UPDATE books SET completion_armed = 1 WHERE id = ?", (book_id,))
            self._armed[book_id] = True
        elif armed and not seek and fraction >= self.config.completion_threshold:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO completions (book_id, completed_at) VALUES (?, ?)",
                             (book_id, self.wall()))
                conn.execute("UPDATE books SET listen_count = listen_count + 1, completion_armed = 0 "
                             "WHERE id = ?", (book_id,))
            self._armed[book_id] = False
            log.info("Book %s counted as listened", book_id)

    def flush(self):
        with self._lock:
            pending, self._pending = self._pending, {}
            self._last_flush = self.clock()
        if not pending:
            return
        now = self.wall()
        try:
            with self.db.transaction() as conn:
                for (book_id, day), ms in pending.items():
                    conn.execute(
                        "INSERT INTO listening_daily (book_id, day, ms) VALUES (?, ?, ?) "
                        "ON CONFLICT(book_id, day) DO UPDATE SET ms = ms + excluded.ms",
                        (book_id, day, ms),
                    )
                    conn.execute(
                        "UPDATE books SET total_listened_ms = total_listened_ms + ?, last_played_at = ? "
                        "WHERE id = ?", (ms, now, book_id),
                    )
        except Exception:
            log.exception("Could not write listening statistics; keeping them for the next flush")
            with self._lock:
                for key, ms in pending.items():
                    self._pending[key] = self._pending.get(key, 0) + ms

    # ------------------------------------------------------------ queries
    def summary(self, weeks=8, months=6):
        self.flush()
        today = dt.date.fromtimestamp(self.wall())
        rows = self.db.query("SELECT day, SUM(ms) AS ms FROM listening_daily GROUP BY day")
        per_day = {dt.date.fromisoformat(r["day"]): r["ms"] for r in rows}

        week_start = today - dt.timedelta(days=today.weekday())
        week_list = []
        for i in range(weeks - 1, -1, -1):
            start = week_start - dt.timedelta(weeks=i)
            end = start + dt.timedelta(days=7)
            ms = sum(v for d, v in per_day.items() if start <= d < end)
            week_list.append({"label": f"v{start.isocalendar()[1]}", "start": start.isoformat(), "ms": ms})

        month_list = []
        y, m = today.year, today.month
        for _ in range(months):
            ms = sum(v for d, v in per_day.items() if d.year == y and d.month == m)
            month_list.append({"year": y, "month": m, "ms": ms})
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        month_list.reverse()

        books = self.db.list_books()
        for b in books:
            del b["chapters"]
            duration = b["duration_ms"] or 0
            b["progress"] = min(1.0, (b["last_position"] or 0) / duration) if duration else 0
        listened = [b for b in books if b["total_listened_ms"] > 0 or b["listen_count"] > 0]
        top = sorted(listened, key=lambda b: (b["listen_count"], b["total_listened_ms"]), reverse=True)

        return {
            "total_ms": sum(per_day.values()),
            "today_ms": per_day.get(today, 0),
            "week_ms": week_list[-1]["ms"] if week_list else 0,
            "month_ms": month_list[-1]["ms"] if month_list else 0,
            "books_finished": sum(b["listen_count"] for b in books),
            "books_started": len(listened),
            "weeks": week_list,
            "months": month_list,
            "top": top[:10],
            "books": sorted(listened, key=lambda b: b["last_played_at"] or 0, reverse=True),
            "favorites": sorted((b for b in books if b["favorite"]), key=lambda b: b["title"].casefold()),
        }
