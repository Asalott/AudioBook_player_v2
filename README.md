# Ljudboksspelare

En liten ljudboksspelare med pekskärm för Raspberry Pi 4/5. Python/Flask-server
med VLC för uppspelning och ett webbgränssnitt som körs i Chromium i kioskläge.

## Arkitektur

| Fil | Ansvar |
|---|---|
| `Main.py` | Startpunkt. Sparar position och statistik vid Ctrl+C/SIGTERM. |
| `server.py` | Flask-API + gränssnittet (`create_app()`). |
| `playback.py` | Uppspelningstjänst: aktuell bok, position, kapitel, sovtimer (trådsäker). |
| `player.py` | Tunt lager över libVLC (en instans för hela appen). |
| `database.py` | SQLite (WAL) med versionerade migreringar och automatisk backup. |
| `scanner.py` | Inkrementell biblioteksskanning i bakgrunden. |
| `watcher.py` | Filbevakning (inotify via watchdog) med debounce. |
| `nas_sync.py` | Hämtar böcker från en NAS (SMB), manuellt eller enligt schema. |
| `stats.py` | Lyssningstid, antal genomlyssningar, topplista. |
| `library.py` / `covers.py` | Metadata, kapitel (ffprobe) och nedskalade omslag. |
| `static/` | Gränssnittet (bibliotek, spelare, statistik, inställningar). |

* Appen startar direkt från databasen. Ingen skanning sker vid uppstart.
* Nya, ändrade och borttagna filer i `books/` upptäcks automatiskt medan appen
  körs (omskanning 5 s efter senaste ändringen). Knappen *Inställningar → Sök
  efter böcker* skannar manuellt, med förlopp.
* Borttagna böcker döljs men raderas aldrig ur databasen, så position och
  statistik finns kvar om filen kommer tillbaka eller flyttas/döps om.
* Positionen sparas var 30:e sekund under uppspelning och direkt vid paus,
  byte av bok och avslut. Statistik sparas en gång i minuten och vid paus.
* En genomlyssning räknas när uppspelningen når 95 % av boken.

## Installation på Raspberry Pi

```bash
git clone <repo> ~/audiobook && cd ~/audiobook
bash deploy/install.sh
```

Skriptet installerar VLC, ffmpeg och Chromium, skapar `.venv`, installerar en
systemd-användartjänst (`audiobook.service`) och en autostart som öppnar
spelaren i kioskläge. Lägg böckerna (`.m4b`, `.m4a`, `.mp3`) i `books/`.

Uppdatering: `git pull && bash deploy/install.sh`. Databasen migreras
automatiskt vid start, och en kopia sparas först som `books.db.v<N>-<tid>.bak`.

### Inställningar via miljövariabler

Sätts i `~/.config/systemd/user/audiobook.service` (`Environment=...`):

| Variabel | Standard | |
|---|---|---|
| `ABP_BOOKS_DIR` | `./books` | Mapp med ljudböcker (t.ex. ett USB-minne) |
| `ABP_DATA_DIR` | appmappen | Var `books.db` och `covers/` ligger |
| `ABP_HOST` / `ABP_PORT` | `127.0.0.1` / `5000` | |
| `ABP_WATCH` | `1` | `0` stänger av filbevakningen |
| `ABP_SCAN_DEBOUNCE` | `5` | Sekunder utan ändringar innan omskanning |
| `ABP_LOG_LEVEL` | `WARNING` | |
| `ABP_NAS_SUBDIR` | `NAS` | Undermapp i `books/` som NAS-böckerna kopieras till |

### Hämta böcker från en NAS

Under *Inställningar → Hämta från NAS* anger du adressen till mappen på NAS:en
(`\\nas\media\Ljudböcker`, `//192.168.1.10/media/Ljudböcker` eller
`smb://nas:445/media/Ljudböcker`) samt användarnamn och lösenord. *Testa
anslutningen* räknar ljudfilerna utan att kopiera något, *Hämta nu* startar en
hämtning, och under *Hämta automatiskt* väljer du varje dag/vecka/månad eller
ett eget intervall och en tid.

* Böckerna **kopieras** till `books/NAS/`, så de går att lyssna på även när
  NAS:en, nätverket eller VPN:en är nere. Bara nya och ändrade filer hämtas.
* Filer som tas bort från NAS:en ligger kvar i spelaren.
* Om NAS:en inte går att nå vid en schemalagd hämtning görs ett nytt försök
  efter en timme. Var spelaren avstängd vid den tiden hämtas det direkt vid start.
* Hämtningen avbryts innan den börjar om det inte finns plats (200 MB lämnas fritt).
* Lösenordet sparas okrypterat i `books.db` och skickas aldrig tillbaka till
  gränssnittet.
* En adress utan `\\`/`//`/`smb://` tolkas som en vanlig mapp, t.ex. en
  delning som redan är monterad via `/etc/fstab`.

### Tips för Pi:n

* Stort bibliotek: höj inotify-gränsen om loggen varnar,
  `echo fs.inotify.max_user_watches=65536 | sudo tee /etc/sysctl.d/90-inotify.conf`.
* Mjukare typsnitt: gränssnittet använder Nunito/Lato om de finns installerade,
  annars Noto Sans (installeras av skriptet).
* Kontrollera bildfrekvensen: starta `deploy/kiosk.sh --show-fps-counter`.

## Utveckling

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests      # enhetstester (ingen VLC behövs)
.venv/bin/python scripts/verify.py       # helhetstest med riktig VLC + ffmpeg
.venv/bin/python Main.py                 # http://127.0.0.1:5000
```

På Windows:

```powershell
python -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q tests
.venv\Scripts\python.exe Main.py          # http://127.0.0.1:5000
```

## Inte med i repot

Ljudböcker (`books/`), databasen (`books.db` med WAL-filer och backuper),
omslag (`covers/`) och `.venv/` ignoreras via `.gitignore`. Databasen och
omslagen skapas automatiskt vid första start och skanning.
