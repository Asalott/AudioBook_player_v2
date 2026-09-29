# Metadata extraction for audiobook files (title, author, series, chapters,
# duration and cover art).
#
# Every function here is defensive: a broken, truncated or tag-less file
# gives a book with fallback values instead of aborting a whole scan.
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.mp4 import MP4

log = logging.getLogger(__name__)

UNKNOWN_AUTHOR = "Okänd"
UNKNOWN_CHAPTER = "Okänt kapitel"


@dataclass
class BookMeta:
    title: str
    author: str = UNKNOWN_AUTHOR
    series: str = None
    volume: int = None
    chapters: list = field(default_factory=list)
    duration_ms: int = 0
    cover: bytes = None


def clean_title(title):
    """Tidies a file name for display: removes ASIN codes and odd characters
    but keeps letters from every language (å, ä, ö ...)."""
    title = re.sub(r"\[[^\]]*\]", "", title)                              # [B09CVBKH5L]
    title = re.sub(r"[^\w\s.,'!?&:-]", "", title, flags=re.UNICODE)       # odd characters
    title = re.sub(r"\s+", " ", title).strip()
    return title


def read_metadata(book_path):
    """Reads everything the library needs from one audio file."""
    book_path = Path(book_path)
    title = clean_title(book_path.stem) or book_path.stem
    meta = BookMeta(title=title)
    try:
        audio = mutagen.File(str(book_path))
    except Exception as e:  # mutagen raises many different error types
        log.warning("Could not read tags from %s: %s", book_path.name, e)
        audio = None

    if audio is not None:
        if audio.info is not None and getattr(audio.info, "length", None):
            meta.duration_ms = int(audio.info.length * 1000)
        tags = audio.tags or {}
        if isinstance(audio, MP4):
            meta.author = _mp4_author(tags) or UNKNOWN_AUTHOR
            meta.series, meta.volume = _mp4_series(tags)
            meta.cover = _mp4_cover(tags)
        else:
            meta.author = _id3_text(tags, "TPE1") or UNKNOWN_AUTHOR
            meta.series, meta.volume = _id3_series(tags)
            meta.cover = _id3_cover(tags)
        meta.author = _first_author(meta.author)

    if not meta.series:
        match = re.match(r"^(.*?),\s*Vol\.?\s*(\d+)", title)
        if match:
            meta.series, meta.volume = match.group(1).strip(), int(match.group(2))

    meta.chapters = extract_chapters(book_path)
    if not meta.duration_ms and meta.chapters:
        meta.duration_ms = int(meta.chapters[-1]["end"] * 1000)
    return meta


# ------------------------------------------------------------------ chapters
def _ffprobe():
    local = Path(sys.executable).parent / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    return str(local) if local.exists() else shutil.which("ffprobe")


def extract_chapters(book_path):
    """Chapter list [{title, start, end}] in seconds, [] when unavailable."""
    ffprobe = _ffprobe()
    if ffprobe:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-print_format", "json", "-show_chapters", "-i", str(book_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=True, timeout=60,
            )
            data = json.loads(result.stdout or "{}")
            chapters = []
            for ch in data.get("chapters", []):
                start = float(ch.get("start_time", 0) or 0)
                end = float(ch.get("end_time", 0) or 0)
                if end <= start:
                    continue
                title = (ch.get("tags") or {}).get("title") or UNKNOWN_CHAPTER
                chapters.append({"title": title, "start": start, "end": end})
            return chapters
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.warning("Could not read chapters from %s: %s", Path(book_path).name, e)
    # Fallback without ffprobe: Nero chapters via mutagen (MP4 only).
    try:
        audio = MP4(str(book_path))
        if audio.chapters:
            items = list(audio.chapters)
            total = audio.info.length
            return [
                {
                    "title": c.title or UNKNOWN_CHAPTER,
                    "start": float(c.start),
                    "end": float(items[i + 1].start if i + 1 < len(items) else total),
                }
                for i, c in enumerate(items)
            ]
    except Exception:
        pass
    return []


# ------------------------------------------------------------------ MP4 tags
def _freeform(tags, key):
    value = tags.get(key)
    if not value:
        return None
    try:
        return bytes(value[0]).decode("utf-8", errors="replace").strip() or None
    except (TypeError, ValueError):
        return None


def _mp4_author(tags):
    raw = _freeform(tags, "----:com.pilabor.tone:ARTIST")
    if not raw:
        artist = tags.get("\xa9ART")
        raw = str(artist[0]).strip() if artist else None
    return raw


def _first_author(raw):
    """The artist tag often mixes in illustrator/translator ("Name - role");
    keep the first name that is not marked with a role."""
    if not raw:
        return UNKNOWN_AUTHOR
    names = [n.strip() for n in raw.split(",") if n.strip()]
    authors = [n for n in names if " - " not in n]
    return authors[0] if authors else (names[0] if names else UNKNOWN_AUTHOR)


def _mp4_series(tags):
    # 1. The "tone" tagger's dedicated series tags.
    series = _freeform(tags, "----:com.pilabor.tone:SERIES")
    if series:
        part = _freeform(tags, "----:com.pilabor.tone:PART")
        return series, _to_int(part)
    # 2. The album tag (+ track number).
    album = tags.get("\xa9alb")
    if album:
        series, embedded = clean_series_name(str(album[0]))
        track = tags.get("trkn")
        volume = (track[0][0] if track and track[0] else None) or embedded
        if series:
            return series, volume
    return None, None


def _mp4_cover(tags):
    covers = tags.get("covr")
    return bytes(covers[0]) if covers else None


# ------------------------------------------------------------------ ID3 tags
def _id3_text(tags, key):
    try:
        frame = tags.get(key)
        return str(frame.text[0]).strip() if frame and frame.text else None
    except (AttributeError, IndexError):
        return None


def _id3_series(tags):
    album = _id3_text(tags, "TALB")
    if not album:
        return None, None
    series, embedded = clean_series_name(album)
    track = _id3_text(tags, "TRCK")
    volume = _to_int(track.split("/")[0]) if track else None
    return series or None, volume or embedded


def _id3_cover(tags):
    try:
        for key in tags.keys():
            if key.startswith("APIC"):
                return bytes(tags[key].data)
    except AttributeError:
        pass
    return None


# ------------------------------------------------------------------- helpers
def clean_series_name(raw):
    volume = None
    vol_match = re.search(r",?\s*Vol\.?\s*(\d+)", raw, flags=re.IGNORECASE)
    if vol_match:
        volume = int(vol_match.group(1))
    cleaned = re.sub(r",?\s*Vol\.?\s*\d+", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\([^)]*\)", "", cleaned)
    cleaned = cleaned.split(":")[0]
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, volume


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
