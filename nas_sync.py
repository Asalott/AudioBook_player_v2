# Fetching audiobooks from a NAS (SMB share).
#
# Books are *copied* from a folder on the NAS into `books/<nas_subdir>/`
# instead of being played over the network. That way the library keeps
# working when the NAS, the WiFi or the VPN is down, and VLC never has a
# file disappear under it in the middle of a book.
#
# A sync only downloads files that are new or changed (size/mtime). Files
# are written as hidden ".name.part" files (ignored by the scanner) and
# renamed into place when complete, so a half-copied book is never scanned.
# Files that were removed from the NAS are left alone locally.
#
# A sync can be started by hand or by a schedule ("every N days/weeks/months
# at HH:MM"). A failed scheduled sync (NAS offline) is retried after an hour.
import calendar
import json
import logging
import os
import shutil
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath

from config import AUDIO_EXTENSIONS

try:
    import smbclient
    SMB_IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - not installed, or a DLL blocked on Windows
    smbclient = None
    SMB_IMPORT_ERROR = e

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
RETRY_SECONDS = 3600
# Leave this much free on the SD card when copying.
FREE_SPACE_MARGIN = 200 * 1024 * 1024
UNITS = ("day", "week", "month")


class NasError(Exception):
    pass


# ----------------------------------------------------------------- sources
def parse_address(address):
    """'\\\\host\\share\\dir', '//host/share/dir' or 'smb://host[:port]/share/dir'
    -> (host, port, share, [dir parts]). Raises NasError when invalid."""
    a = (address or "").strip()
    if a.lower().startswith("smb://"):
        a = a[6:]
    elif a.startswith("\\\\") or a.startswith("//"):
        a = a[2:]
    else:
        raise NasError("Adressen ska se ut som \\\\nas\\delning\\mapp eller smb://nas/delning/mapp")
    parts = [p for p in a.replace("\\", "/").split("/") if p]
    if len(parts) < 2:
        raise NasError("Adressen måste innehålla både server och delning, t.ex. \\\\nas\\media")
    host, port = parts[0], 445
    if ":" in host:
        host, _, p = host.rpartition(":")
        try:
            port = int(p)
        except ValueError:
            raise NasError("Ogiltigt portnummer i adressen") from None
    return host, port, parts[1], parts[2:]


def is_smb_address(address):
    a = (address or "").strip()
    return a.lower().startswith("smb://") or a.startswith("\\\\") or a.startswith("//")


def _walk(scandir, join, root):
    """Recursive listing of audio files: {relative posix path: (size, mtime)}."""
    found = {}
    stack = [(root, PurePosixPath())]
    while stack:
        folder, rel = stack.pop()
        for entry in scandir(folder):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                stack.append((join(folder, entry.name), rel / entry.name))
            elif os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS and entry.is_file():
                st = entry.stat()
                found[(rel / entry.name).as_posix()] = (st.st_size, st.st_mtime)
    return found


class SmbSource:
    def __init__(self, address, username="", password=""):
        if smbclient is None:
            if isinstance(SMB_IMPORT_ERROR, ModuleNotFoundError):
                raise NasError("Paketet smbprotocol saknas (pip install smbprotocol)")
            raise NasError(f"SMB-stödet kunde inte laddas: {SMB_IMPORT_ERROR}")
        self.host, self.port, share, folder = parse_address(address)
        self.root = "\\\\" + "\\".join([self.host, share] + folder)
        self.username = username or None
        self.password = password or None

    def __enter__(self):
        try:
            smbclient.register_session(self.host, username=self.username, password=self.password,
                                       port=self.port, connection_timeout=10)
        except Exception as e:
            raise NasError(f"Kunde inte ansluta till {self.host}: {_reason(e)}") from e
        return self

    def __exit__(self, *exc):
        try:
            smbclient.delete_session(self.host, port=self.port)
        except Exception:
            pass

    def walk(self):
        try:
            return _walk(smbclient.scandir, lambda a, b: a + "\\" + b, self.root)
        except Exception as e:
            raise NasError(f"Kunde inte läsa mappen {self.root}: {_reason(e)}") from e

    def open(self, rel):
        return smbclient.open_file(self.root + "\\" + rel.replace("/", "\\"), mode="rb")


class LocalSource:
    """A folder that is already reachable as a path (a share mounted with
    mount.cifs/fstab, a USB disk). Also used by the tests."""

    def __init__(self, path):
        self.root = Path(path).expanduser()

    def __enter__(self):
        if not self.root.is_dir():
            raise NasError(f"Mappen {self.root} finns inte")
        return self

    def __exit__(self, *exc):
        pass

    def walk(self):
        try:
            return _walk(os.scandir, os.path.join, str(self.root))
        except OSError as e:
            raise NasError(f"Kunde inte läsa mappen {self.root}: {e}") from e

    def open(self, rel):
        return open(self.root / rel, "rb")


def _reason(e):
    text = str(e).strip() or type(e).__name__
    low = text.lower()
    if "logon" in low or "access_denied" in low or "access is denied" in low:
        return "fel användarnamn eller lösenord"
    if "bad_network_name" in low:
        return "delningen finns inte"
    if "object_name_not_found" in low or "object_path_not_found" in low:
        return "mappen finns inte"
    if "timed out" in low or "timeout" in low:
        return "ingen kontakt (tidsgränsen nåddes)"
    return text.splitlines()[0][:200]


def default_source_factory(settings):
    address = settings.get("address") or ""
    if not address:
        raise NasError("Ingen NAS-adress är inställd")
    if is_smb_address(address):
        return SmbSource(address, settings.get("username"), settings.get("password"))
    return LocalSource(address)


# ---------------------------------------------------------------- schedule
def validate_schedule(schedule):
    """None (off) or {"every": 1-365, "unit": day|week|month, "time": "HH:MM"}."""
    if schedule is None:
        return None
    if not isinstance(schedule, dict):
        raise NasError("Ogiltigt schema")
    every, unit, at = schedule.get("every"), schedule.get("unit"), schedule.get("time", "03:00")
    if isinstance(every, bool) or not isinstance(every, int) or not 1 <= every <= 365:
        raise NasError("Intervallet måste vara ett heltal mellan 1 och 365")
    if unit not in UNITS:
        raise NasError("Okänd enhet för intervallet")
    try:
        hh, mm = (int(x) for x in str(at).split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError
    except ValueError:
        raise NasError("Tiden ska anges som TT:MM") from None
    return {"every": every, "unit": unit, "time": f"{hh:02d}:{mm:02d}"}


def _add_interval(d, every, unit):
    if unit == "day":
        return d + timedelta(days=every)
    if unit == "week":
        return d + timedelta(weeks=every)
    month = d.month - 1 + every
    year, month = d.year + month // 12, month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def next_run(schedule, last_success, saved_at, last_failure=None):
    """Timestamp of the next scheduled sync, or None when off."""
    if not schedule:
        return None
    hh, mm = (int(x) for x in schedule["time"].split(":"))
    at = lambda d: datetime(d.year, d.month, d.day, hh, mm).timestamp()  # noqa: E731
    if last_success:
        due = at(_add_interval(datetime.fromtimestamp(last_success).date(), schedule["every"], schedule["unit"]))
    else:
        # Never synced: the first time HH:MM comes around after the schedule was saved.
        start = datetime.fromtimestamp(saved_at or time.time())
        due = at(start.date())
        if due <= start.timestamp():
            due = at(start.date() + timedelta(days=1))
    if last_failure and last_failure >= due and last_failure > (last_success or 0):
        due = last_failure + RETRY_SECONDS
    return due


# -------------------------------------------------------------------- sync
class NasSync:
    def __init__(self, db, config, scanner=None, source_factory=default_source_factory, clock=time.time):
        self.db = db
        self.config = config
        self.scanner = scanner
        self.source_factory = source_factory
        self.clock = clock
        self._lock = threading.Lock()
        self._thread = None
        self._scheduler = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._status = {"running": False, "phase": "idle", "done": 0, "total": 0, "bytes_done": 0,
                        "bytes_total": 0, "current": None, "last_result": None, "error": None}

    @property
    def target_dir(self):
        return Path(self.config.books_dir) / self.config.nas_subdir

    # ------------------------------------------------------------ settings
    def settings(self):
        try:
            data = json.loads(self.db.get_setting("nas", "") or "{}")
        except ValueError:
            data = {}
        return {"address": data.get("address", ""), "username": data.get("username", ""),
                "password": data.get("password", ""), "schedule": data.get("schedule")}

    def public_settings(self):
        s = self.settings()
        return {"address": s["address"], "username": s["username"],
                "has_password": bool(s["password"]), "schedule": s["schedule"]}

    def save_settings(self, data):
        """`password` is only changed when the key is present."""
        current = self.settings()
        for key in ("address", "username", "password"):
            if key in data:
                value = data[key]
                if value is None:
                    value = ""
                if not isinstance(value, str) or len(value) > 500:
                    raise NasError(f"Ogiltigt värde för '{key}'")
                current[key] = value.strip() if key != "password" else value
        if current["address"] and is_smb_address(current["address"]):
            parse_address(current["address"])
        if "schedule" in data:
            schedule = validate_schedule(data["schedule"])
            if schedule != current["schedule"]:
                self.db.set_setting("nas_schedule_saved_at", self.clock())
            current["schedule"] = schedule
        self.db.set_setting("nas", json.dumps(current, ensure_ascii=False))
        self._wake.set()
        return self.public_settings()

    def _float_setting(self, key):
        try:
            return float(self.db.get_setting(key))
        except (TypeError, ValueError):
            return None

    def next_run(self):
        return next_run(self.settings()["schedule"], self._float_setting("nas_last_success"),
                        self._float_setting("nas_schedule_saved_at"), self._float_setting("nas_last_failure"))

    def status(self):
        with self._lock:
            status = dict(self._status)
        status["last_success"] = self._float_setting("nas_last_success")
        status["last_failure"] = self._float_setting("nas_last_failure")
        status["next_run"] = self.next_run()
        status["target"] = self.config.nas_subdir
        return status

    # ---------------------------------------------------------- test/sync
    def test(self, overrides=None):
        """Connects and lists the folder without copying anything."""
        settings = self.settings()
        for key, value in (overrides or {}).items():
            if key in ("address", "username", "password") and isinstance(value, str) and value != "":
                settings[key] = value.strip() if key != "password" else value
        with self.source_factory(settings) as source:
            files = source.walk()
        return {"files": len(files), "bytes": sum(size for size, _ in files.values())}

    def sync_async(self, reason="manual"):
        if self._stop.is_set():
            return "stopped"
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return "running"
            self._status.update(running=True, phase="connecting", done=0, total=0, bytes_done=0,
                                bytes_total=0, current=None, error=None)
            self._thread = threading.Thread(target=self._run, args=(reason,), name="nas-sync", daemon=True)
            self._thread.start()
            return "started"

    def wait(self, timeout=None):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run(self, reason):
        try:
            self.sync(reason)
        except Exception as e:
            if not isinstance(e, NasError):
                log.exception("NAS sync failed")
            self._progress(error=str(e))
        finally:
            self._progress(running=False, phase="idle", current=None)

    def _progress(self, **values):
        with self._lock:
            self._status.update(values)

    def sync(self, reason="manual"):
        started = time.monotonic()
        result = {"copied": 0, "unchanged": 0, "errors": 0, "bytes": 0, "reason": reason}
        try:
            self._progress(phase="connecting")
            with self.source_factory(self.settings()) as source:
                self._progress(phase="listing")
                remote = source.walk()
                target = self.target_dir
                todo = []
                for rel, (size, mtime) in sorted(remote.items()):
                    dest = target / rel
                    try:
                        st = dest.stat()
                        if st.st_size == size and abs(st.st_mtime - mtime) < 2:
                            result["unchanged"] += 1
                            continue
                    except OSError:
                        pass
                    todo.append((rel, size, mtime))
                total_bytes = sum(size for _, size, _ in todo)
                if todo:
                    target.mkdir(parents=True, exist_ok=True)
                    free = shutil.disk_usage(target).free
                    if total_bytes > free - FREE_SPACE_MARGIN:
                        raise NasError(f"Inte tillräckligt med plats: behöver {_gb(total_bytes)}, "
                                       f"{_gb(max(0, free - FREE_SPACE_MARGIN))} ledigt")
                self._progress(phase="copying", total=len(todo), bytes_total=total_bytes)
                for i, (rel, size, mtime) in enumerate(todo):
                    if self._stop.is_set():
                        break
                    self._progress(done=i, current=rel)
                    try:
                        self._copy(source, rel, mtime)
                        result["copied"] += 1
                        result["bytes"] += size
                    except Exception as e:
                        if self._stop.is_set():
                            break
                        log.warning("Could not copy %s from the NAS: %s", rel, e)
                        result["errors"] += 1
                if self._stop.is_set():
                    raise NasError("Avbruten")
                self._progress(done=len(todo), current=None)
        except Exception:
            self.db.set_setting("nas_last_failure", self.clock())
            raise
        result["seconds"] = round(time.monotonic() - started, 1)
        self.db.set_setting("nas_last_success", self.clock())
        self._progress(phase="done", last_result=result, last_finished=self.clock())
        log.info("NAS sync (%s): %s", reason, result)
        if result["copied"] and self.scanner is not None:
            self.scanner.scan_async("nas")
        return result

    def _copy(self, source, rel, mtime):
        dest = self.target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        try:
            with source.open(rel) as src, open(tmp, "wb") as out:
                while True:
                    if self._stop.is_set():
                        raise NasError("Avbruten")
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    with self._lock:
                        self._status["bytes_done"] += len(chunk)
            os.utime(tmp, (mtime, mtime))
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()

    # ----------------------------------------------------------- scheduler
    def start(self):
        if self._scheduler is None:
            self._scheduler = threading.Thread(target=self._schedule_loop, name="nas-schedule", daemon=True)
            self._scheduler.start()

    def _schedule_loop(self):
        while not self._stop.is_set():
            try:
                due = self.next_run()
                if due is not None and due <= self.clock():
                    self.sync_async("schedule")
                    self.wait()
            except Exception:
                log.exception("NAS schedule check failed")
            self._wake.wait(60)
            self._wake.clear()

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        self.wait(timeout=10)


def _gb(n):
    return f"{n / 1024 ** 3:.1f} GB".replace(".", ",")
