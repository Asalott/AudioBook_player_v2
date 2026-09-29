# Cover art cache.
#
# Embedded covers are often 1-2 MB JPEGs of 2000+ px. Decoding those on a
# Raspberry Pi for every card in the library is slow and wastes memory, so
# each cover is downscaled once to two small JPEGs named after the book id:
#   covers/<id>.jpg      - player view
#   covers/<id>_s.jpg    - library grid
# Files are written atomically (temp file + rename).
import io
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from PIL import Image
except ImportError:  # Pillow is optional; without it the original is stored.
    Image = None

SIZES = {"large": 480, "small": 240}


def cover_file(covers_dir, book_id, size="large"):
    suffix = "_s" if size == "small" else ""
    return Path(covers_dir) / f"{book_id}{suffix}.jpg"


def _atomic_write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _resize(data, max_px):
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (max_px, max_px))  # fast JPEG DCT downscaling while decoding
    img = img.convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=84, optimize=True, progressive=True)
    return out.getvalue()


def write_cover_cache(covers_dir, book_id, data):
    """Writes the cached covers for a book. Returns True on success."""
    if not data:
        remove_cover_cache(covers_dir, book_id)
        return False
    covers_dir = Path(covers_dir)
    covers_dir.mkdir(parents=True, exist_ok=True)
    try:
        for size, px in SIZES.items():
            payload = _resize(data, px) if Image is not None else data
            _atomic_write(cover_file(covers_dir, book_id, size), payload)
        return True
    except Exception as e:  # corrupt image data, disk full, ...
        log.warning("Could not cache cover for book %s: %s", book_id, e)
        remove_cover_cache(covers_dir, book_id)
        return False


def remove_cover_cache(covers_dir, book_id):
    for size in SIZES:
        try:
            cover_file(covers_dir, book_id, size).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove cover for book %s: %s", book_id, e)
