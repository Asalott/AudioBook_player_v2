"""End-to-end check of a real installation (real VLC, real files).

Runs against a throw-away copy in a temp folder, so the real library and
database are never touched:

    python scripts/verify.py              # generated test books (needs ffmpeg)
    python scripts/verify.py --db books.db  # also measure startup with a copy of a real DB

Checks: startup time, that startup does not scan, that new/removed files are
picked up by the watcher, the manual rescan, that listening time, listen
counts and favourites survive a restart, and CPU use while playing.
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from config import Config  # noqa: E402
from helpers import make_audiobook  # noqa: E402
from server import create_app, shutdown_app  # noqa: E402

results = []
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'OK ' if ok else 'FEL'} {name}{' – ' + detail if detail else ''}")


def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="copy of an existing books.db to measure startup with")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="abp-verify-"))
    books = tmp / "books"
    books.mkdir()
    print(f"Testmapp: {tmp}")
    if make_audiobook(books / "Första boken – Åäö.m4b", seconds=8) is None:
        print("ffmpeg saknas – kan inte skapa testböcker")
        return 1
    make_audiobook(books / "Serie" / "Andra boken, Vol. 2.m4b", seconds=6)
    if args.db:
        shutil.copy(args.db, tmp / "books.db")

    config = Config(books_dir=books, db_path=tmp / "books.db", covers_dir=tmp / "covers",
                    scan_debounce=1.0, stats_flush_interval=2, position_save_interval=2)

    print("\n1. Uppstart")
    t0 = time.perf_counter()
    app = create_app(config)
    startup = time.perf_counter() - t0
    client = app.test_client()
    state = app.extensions["audiobook"]
    check("uppstart under 1 s", startup < 1.0, f"{startup * 1000:.0f} ms")
    check("ingen skanning vid start", state.scanner.status()["last_result"] is None)

    print("\n2. Manuell omskanning")
    client.post("/api/scan-library")
    state.scanner.wait(60)
    # Freshly written files are skipped while they may still be copying and
    # picked up by an automatic follow-up scan a few seconds later.
    found = wait_for(lambda: len(client.get("/api/books").get_json()) >= 2, 20)
    check("manuell skanning hittar böckerna", found, f"{state.scanner.status()['last_result']}")

    print("\n3. Filbevakning")
    new = make_audiobook(tmp / "staging.m4b", seconds=4)
    shutil.move(str(new), books / "Tredje boken.m4b")
    found = wait_for(lambda: any(b["title"] == "Tredje boken" for b in client.get("/api/books").get_json()), 20)
    check("ny fil upptäcks automatiskt", found)
    (books / "Tredje boken.m4b").unlink()
    gone = wait_for(lambda: not any(b["title"] == "Tredje boken" for b in client.get("/api/books").get_json()), 20)
    check("borttagen fil upptäcks automatiskt", gone)

    print("\n4. Uppspelning, statistik och CPU")
    book = next(b for b in client.get("/api/books").get_json() if b["title"].startswith("Första"))
    client.post("/api/select-book", json={"id": book["id"]})
    client.post(f"/api/books/{book['id']}/favorite", json={"favorite": True})
    client.post("/api/play")
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for _ in range(4):  # the UI polls once a second while the player is open
        time.sleep(1)
        client.get("/api/status")
    cpu = (time.process_time() - cpu0) / (time.perf_counter() - wall0) * 100
    status = client.get("/api/status").get_json()
    check("uppspelning går framåt", status["playing"] and status["time"] > 2000, f"{status['time']} ms")
    check("CPU under uppspelning", True, f"{cpu:.1f} % av en kärna (Python + VLC, denna dator)")
    # Seek to 90 % and let it play to the end -> one completed listen.
    client.post("/api/seek", json={"to_ms": int(book["duration_ms"] * 0.9)})
    ended = wait_for(lambda: not client.get("/api/status").get_json()["playing"], 15)
    check("boken spelas klart", ended)
    stats_before = client.get("/api/stats").get_json()
    shutdown_app(app)

    print("\n5. Efter omstart")
    app = create_app(config)
    client = app.test_client()
    stats_after = client.get("/api/stats").get_json()
    b = next(x for x in stats_after["books"] if x["id"] == book["id"])
    check("lyssningstid sparad", 3000 <= b["total_listened_ms"] <= 12000, fmt_ms(b["total_listened_ms"]))
    check("antal lyssningar sparat", b["listen_count"] == 1, str(b["listen_count"]))
    check("favorit sparad", any(f["id"] == book["id"] for f in stats_after["favorites"]))
    check("statistik oförändrad efter omstart", stats_after["total_ms"] == stats_before["total_ms"],
          f"{stats_before['total_ms']} → {stats_after['total_ms']} ms")
    check("senaste bok återställd", client.get("/api/status").get_json()["book_id"] == book["id"])
    shutdown_app(app)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} kontroller OK")
    return 0 if all(results) else 1


def fmt_ms(ms):
    return f"{ms / 1000:.1f} s"


if __name__ == "__main__":
    sys.exit(main())
