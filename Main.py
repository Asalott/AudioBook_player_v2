# Entry point: python Main.py
#
# Starts the Flask server and makes sure the listening position and
# statistics are written to disk when the app is stopped (Ctrl+C, or
# SIGTERM from systemd).
import atexit
import logging
import signal
import sys

from config import Config
from server import create_app, shutdown_app


def main():
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # One log line per HTTP request (the UI polls every second) is pure
    # noise that ends up on the SD card through journald.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app = create_app(config)

    done = []

    def _shutdown(*_):
        if not done:
            done.append(True)
            shutdown_app(app)

    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    app.run(host=config.host, port=config.port, threaded=True, use_reloader=False, debug=False)


if __name__ == "__main__":
    main()
