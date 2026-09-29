#!/usr/bin/env bash
# Installs or updates the audiobook player on Raspberry Pi OS (Bookworm or later).
# Run as the desktop user (not root):  bash deploy/install.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

echo "==> Systempaket"
sudo apt-get update
sudo apt-get install -y python3-venv vlc ffmpeg curl fonts-noto-core
sudo apt-get install -y chromium-browser 2>/dev/null || sudo apt-get install -y chromium

echo "==> Python-miljö"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p books

echo "==> Tjänst"
mkdir -p ~/.config/systemd/user
sed "s|@APP_DIR@|$APP_DIR|g" deploy/audiobook.service > ~/.config/systemd/user/audiobook.service
systemctl --user daemon-reload
systemctl --user enable audiobook.service
systemctl --user restart audiobook.service
# Start the user service at boot, even before the desktop logs in.
sudo loginctl enable-linger "$USER"

echo "==> Kiosk-autostart"
chmod +x deploy/kiosk.sh
mkdir -p ~/.config/autostart
sed "s|@APP_DIR@|$APP_DIR|g" deploy/audiobook-kiosk.desktop > ~/.config/autostart/audiobook-kiosk.desktop

echo "Klart. Status: systemctl --user status audiobook   Loggar: journalctl --user -u audiobook"
