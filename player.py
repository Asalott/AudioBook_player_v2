# Thin wrapper around libVLC.
#
# One VLC instance and one media player are created for the whole app and
# reused for every book (the old code created a new instance per book and
# never released the previous one, so native threads and audio outputs
# leaked and two books could play at the same time).
#
# The start position is passed to VLC as a media option, so no "play muted
# for 150 ms and seek" trick is needed.
import logging

log = logging.getLogger(__name__)


class VlcBackend:
    def __init__(self):
        import vlc  # imported lazily so tests can run without libVLC
        self._vlc = vlc
        self._instance = vlc.Instance("--no-video", "--quiet", "--intf=dummy", "--file-caching=1000")
        self._player = self._instance.media_player_new()
        self._path = None
        self._started = False

    def open(self, path):
        """Selects a file without starting playback."""
        self.stop()
        self._path = str(path)

    def play(self, start_ms=0):
        if self._path is None:
            return False
        state = self.state()
        if self._started and state in ("paused", "playing", "opening"):
            self._player.set_pause(0)
            return True
        media = self._instance.media_new_path(self._path)
        if start_ms and start_ms > 0:
            media.add_option(f"start-time={start_ms / 1000:.3f}")
        self._player.set_media(media)
        media.release()  # the player keeps its own reference
        ok = self._player.play() == 0
        self._started = ok
        return ok

    def pause(self):
        if self._started:
            self._player.set_pause(1)  # set_pause, unlike pause(), is not a toggle

    def stop(self):
        if self._started:
            self._player.stop()
        self._started = False

    @property
    def started(self):
        return self._started and self.state() not in ("stopped", "ended", "error")

    def state(self):
        s = self._player.get_state()
        State = self._vlc.State
        return {
            State.Playing: "playing",
            State.Paused: "paused",
            State.Opening: "opening",
            State.Buffering: "opening",
            State.Ended: "ended",
            State.Error: "error",
        }.get(s, "stopped")

    def get_time(self):
        t = self._player.get_time()
        return t if t is not None and t >= 0 else None

    def set_time(self, ms):
        if self.started:
            self._player.set_time(int(ms))

    def set_volume(self, volume):
        self._player.audio_set_volume(int(volume))

    def release(self):
        try:
            self.stop()
            self._player.release()
            self._instance.release()
        except Exception as e:
            log.warning("Error while releasing VLC: %s", e)
