# Central configuration for the audiobook player.
#
# All paths are resolved relative to this file (not the current working
# directory), so the app behaves the same whether it is started by hand,
# from systemd or from an autostart script. Every value can be overridden
# with an environment variable (see README.md).
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# File extensions that are treated as audiobooks.
AUDIO_EXTENSIONS = (".m4b", ".m4a", ".mp3")


def _env_path(name, default):
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    books_dir: Path = BASE_DIR / "books"
    db_path: Path = BASE_DIR / "books.db"
    covers_dir: Path = BASE_DIR / "covers"
    host: str = "127.0.0.1"
    port: int = 5000
    # How often the current position is written while playing (seconds).
    position_save_interval: float = 30.0
    # How often accumulated listening time is written while playing (seconds).
    stats_flush_interval: float = 60.0
    # Quiet period after the last file system change before rescanning (seconds).
    scan_debounce: float = 5.0
    # Start the file system watcher (disabled in most tests).
    watch_library: bool = True
    # Fraction of a book that must be played for it to count as "listened".
    completion_threshold: float = 0.95
    log_level: str = "WARNING"
    # Subfolder of books_dir that books fetched from the NAS are copied to.
    nas_subdir: str = "NAS"

    @classmethod
    def from_env(cls):
        data_dir = _env_path("ABP_DATA_DIR", None)
        return cls(
            books_dir=_env_path("ABP_BOOKS_DIR", BASE_DIR / "books"),
            db_path=(data_dir / "books.db") if data_dir else BASE_DIR / "books.db",
            covers_dir=(data_dir / "covers") if data_dir else BASE_DIR / "covers",
            host=os.environ.get("ABP_HOST", "127.0.0.1"),
            port=int(os.environ.get("ABP_PORT", "5000")),
            scan_debounce=_env_float("ABP_SCAN_DEBOUNCE", 5.0),
            watch_library=os.environ.get("ABP_WATCH", "1") != "0",
            log_level=os.environ.get("ABP_LOG_LEVEL", "WARNING").upper(),
            nas_subdir=os.environ.get("ABP_NAS_SUBDIR", "NAS"),
        )
