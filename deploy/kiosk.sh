#!/usr/bin/env bash
# Opens the player full screen in Chromium once the server answers.
URL="${ABP_URL:-http://127.0.0.1:5000/}"

for _ in $(seq 1 60); do
  curl -fs -o /dev/null "$URL" && break
  sleep 0.5
done

BROWSER="$(command -v chromium-browser || command -v chromium)"
exec "$BROWSER" \
  --kiosk "$URL" \
  --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=Translate \
  --overscroll-history-navigation=0 --disable-pinch \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --touch-events=enabled \
  --ozone-platform-hint=auto \
  "$@"
