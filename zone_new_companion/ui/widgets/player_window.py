"""Built-in player: libmpv renders into a Qt OpenGL widget, with VLC-style controls.

Live streams start a few seconds behind the live edge, so mpv's cache can
cover a dropped connection while FFmpeg reconnects underneath. If a stream
still ends, the window reloads it itself with a growing delay; movies and
episodes resume where they stopped.
"""

from __future__ import annotations

import locale
import logging
import time
from types import ModuleType

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QKeyEvent, QMouseEvent, QOpenGLContext, QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLWindow
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from zone_new_companion.ui.theme import set_role, set_variant, theme

LOGGER = logging.getLogger(__name__)

# Same user agent the VLC launch sends; some portals only serve known players.
USER_AGENT = "Lavf/57.83.100"
# How far behind the live edge playback starts (and refills to after a stall).
LIVE_BUFFER_S = 5
SKIP_S = 30
SMALL_SKIP_S = 10
# Waits between reload attempts once mpv itself gave up on a stream.
_RETRY_DELAYS_MS = (1000, 2000, 4000, 8000)
_MAX_RETRIES = 15
# Playing this long again counts as recovered and resets the retry count.
_STABLE_S = 30
_TICK_MS = 250
_HIDE_CONTROLS_MS = 2500

# mpv's end-file reasons (MpvEventEndFile).
_EOF = 0
_ERROR = 4


def _proc_address(_ctx, name: bytes) -> int:
    context = QOpenGLContext.currentContext()
    if context is None:
        return 0
    address = context.getProcAddress(name)
    return int(address) if address else 0


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _track_label(track: dict) -> str:
    title, lang = track.get("title"), track.get("lang")
    name = title or lang or f"Track {track.get('id')}"
    if title and lang:
        name += f" ({lang})"
    # Several tracks often share a language; codec and channels tell them apart.
    details = [track.get("codec")]
    if track.get("demux-channel-count"):
        details.append(f"{track['demux-channel-count']}ch")
    return " · ".join([name] + [d for d in details if d])


class _Bridge(QObject):
    """Carries mpv's event-thread callbacks over to the GUI thread."""

    end_file = pyqtSignal(int)
    file_loaded = pyqtSignal()
    tracks_changed = pyqtSignal()


class _VideoView(QOpenGLWindow):
    """Draws mpv's frames through its render API (works on Windows, macOS and Linux).

    A native OpenGL window rather than a QOpenGLWidget: Qt composites a
    QOpenGLWidget together with the rest of the window, and mpv's GL calls
    upset that compositing (black video, a mirrored copy of the controls).
    """

    frame_ready = pyqtSignal()
    # mpv cannot open a video output before the render context exists.
    ready = pyqtSignal()
    double_clicked = pyqtSignal()
    mouse_moved = pyqtSignal()
    # The native window keeps the keyboard focus once clicked; keys go on to the player.
    key_pressed = pyqtSignal(QKeyEvent)

    def __init__(self, mpv_module: ModuleType, player) -> None:
        super().__init__(QOpenGLWindow.UpdateBehavior.NoPartialUpdate)
        surface = QSurfaceFormat()
        surface.setVersion(3, 2)
        surface.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        self.setFormat(surface)
        self._mpv = mpv_module
        self._player = player
        self._render = None
        # Kept on self: ctypes drops the callback if nothing references it.
        self._get_proc_address = mpv_module.MpvGlGetProcAddressFn(_proc_address)
        self.frame_ready.connect(self.update, Qt.ConnectionType.QueuedConnection)

    def initializeGL(self) -> None:  # noqa: N802
        self._render = self._mpv.MpvRenderContext(
            self._player, "opengl", opengl_init_params={"get_proc_address": self._get_proc_address},
        )
        self._render.update_cb = self.frame_ready.emit
        self.context().aboutToBeDestroyed.connect(self.release)
        self.ready.emit()

    @property
    def is_ready(self) -> bool:
        return self._render is not None

    def paintGL(self) -> None:  # noqa: N802
        if self._render is None:
            return
        ratio = self.devicePixelRatio()
        self._render.render(
            flip_y=True,
            opengl_fbo={
                "w": round(self.width() * ratio),
                "h": round(self.height() * ratio),
                "fbo": self.defaultFramebufferObject(),
            },
        )

    def release(self) -> None:
        """Free the render context; it must go before the GL context and the mpv core."""
        if self._render is None:
            return
        self.makeCurrent()
        self._render.free()
        self._render = None
        self.doneCurrent()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.double_clicked.emit()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.mouse_moved.emit()
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        self.key_pressed.emit(event)


class PlayerWindow(QWidget):
    """One reusable player window; a new stream replaces the current one."""

    def __init__(self, mpv_module: ModuleType) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Player")
        self.resize(1024, 640)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Qt resets the locale on startup; libmpv refuses to run without a C numeric locale.
        locale.setlocale(locale.LC_NUMERIC, "C")
        self._player = mpv_module.MPV(
            log_handler=self._log,
            loglevel="warn",
            vo="libmpv",
            hwdec="auto-safe",
            idle="yes",
            keep_open="no",
            osc=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            ytdl=False,
            user_agent=USER_AGENT,
            network_timeout=15,
            # FFmpeg reconnects dropped HTTP connections on its own...
            stream_lavf_o="reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_delay_max=5",
            # ...while a cushion of buffered video keeps the picture running meanwhile.
            cache="yes",
            cache_pause_initial="yes",
            cache_pause_wait=LIVE_BUFFER_S,
            demuxer_max_bytes="256MiB",
            # Kept history makes −30 s work on live TV, too.
            demuxer_max_back_bytes="512MiB",
            demuxer_seekable_cache="yes",
            # A reconnect resends seconds already received; smoothing the timestamp
            # jump keeps the picture from freezing until it catches up.
            demuxer_lavf_linearize_timestamps="yes",
            sub_auto="fuzzy",
        )

        self._bridge = _Bridge(self)
        self._bridge.end_file.connect(self._on_end_file)
        self._bridge.file_loaded.connect(self._on_file_loaded)
        self._bridge.tracks_changed.connect(self._refresh_tracks)
        self._player.event_callback("end-file")(lambda event: self._bridge.end_file.emit(event.data.reason))
        self._player.event_callback("file-loaded")(lambda _event: self._bridge.file_loaded.emit())
        self._player.observe_property("track-list", lambda _name, _value: self._bridge.tracks_changed.emit())

        self._url: str | None = None
        self._live = False
        self._lost = False
        self._retries = 0
        self._loaded_at = 0.0
        self._last_pos: float | None = None
        self._seeking = False

        self._video = _VideoView(mpv_module, self._player)
        self._video.ready.connect(self._reload)
        self._video.double_clicked.connect(self.toggle_fullscreen)
        self._video.mouse_moved.connect(self._wake_controls)
        self._video.key_pressed.connect(self.keyPressEvent)
        video_box = QWidget.createWindowContainer(self._video, self)
        video_box.setMinimumSize(320, 180)
        video_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._controls = self._build_controls()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(video_box, 1)
        layout.addWidget(self._controls)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(_TICK_MS)
        self._tick_timer.timeout.connect(self._tick)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._reload)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_HIDE_CONTROLS_MS)
        self._hide_timer.timeout.connect(self._hide_controls)

    # --- building ---------------------------------------------------------

    def _button(self, icon: str, tip: str, slot) -> QPushButton:
        button = set_variant(QPushButton(), "ghost")
        button.setIcon(theme.icon(icon))
        button.setToolTip(tip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        return button

    def _build_controls(self) -> QWidget:
        bar = QWidget(self)
        column = QVBoxLayout(bar)
        column.setContentsMargins(12, 6, 12, 8)
        column.setSpacing(4)

        self._position = QSlider(Qt.Orientation.Horizontal)
        self._position.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._position.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self._position.sliderReleased.connect(self._seek_to_slider)
        column.addWidget(self._position)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._play_button = self._button("pause", "Play / pause (Space)", self.toggle_pause)
        row.addWidget(self._play_button)
        row.addWidget(self._button("rotate-ccw", f"Back {SKIP_S} s (J, ← {SMALL_SKIP_S} s)", lambda: self.skip(-SKIP_S)))
        row.addWidget(self._button("rotate-cw", f"Forward {SKIP_S} s (L, → {SMALL_SKIP_S} s)", lambda: self.skip(SKIP_S)))
        self._time_label = QLabel("--:--")
        row.addWidget(self._time_label)
        row.addStretch(1)
        self._status_label = set_role(QLabel(), "muted")
        row.addWidget(self._status_label)

        self._audio_box = QComboBox()
        self._audio_box.setToolTip("Audio track (A)")
        self._audio_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._audio_box.activated.connect(lambda _i: self._choose_track("aid", self._audio_box))
        row.addWidget(self._audio_box)
        self._sub_box = QComboBox()
        self._sub_box.setToolTip("Subtitles (S)")
        self._sub_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._sub_box.activated.connect(lambda _i: self._choose_track("sid", self._sub_box))
        row.addWidget(self._sub_box)

        self._mute_button = self._button("volume-2", "Mute (M)", self.toggle_mute)
        row.addWidget(self._mute_button)
        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._volume.setRange(0, 100)
        self._volume.setValue(100)
        self._volume.setFixedWidth(90)
        self._volume.valueChanged.connect(lambda value: setattr(self._player, "volume", value))
        row.addWidget(self._volume)
        row.addWidget(self._button("maximize", "Fullscreen (F, double-click)", self.toggle_fullscreen))
        column.addLayout(row)
        return bar

    # --- playback ---------------------------------------------------------

    def play(self, url: str, title: str, live: bool) -> None:
        """Show the window and play ``url``, replacing whatever is playing."""
        self._retry_timer.stop()
        self._url = url
        self._live = live
        self._retries = 0
        self._last_pos = None
        self._lost = False
        self.setWindowTitle(title)
        self._position.setVisible(not live)
        self._set_status("Connecting…")
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self._tick_timer.start()
        # The first show creates the render context; ``ready`` loads the stream then.
        if self._video.is_ready:
            self._load()

    def _load(self) -> None:
        start = {"start": f"{self._last_pos:.1f}"} if self._last_pos and not self._live else {}
        self._player.loadfile(self._url, **start)
        self._player.pause = False

    def _reload(self) -> None:
        if self._url is not None:
            self._load()

    def _on_file_loaded(self) -> None:
        self._loaded_at = time.monotonic()
        self._lost = False

    def _on_end_file(self, reason: int) -> None:
        # A replaced or stopped file ends as well; only a real end or failure matters.
        if self._url is None or reason not in (_EOF, _ERROR):
            return
        if reason == _EOF and not self._live:
            self._url = None
            self._set_status("Finished")
            return
        if self._retries >= _MAX_RETRIES:
            self._lost = True
            self._set_status("Stream lost – press play to retry")
            return
        delay = _RETRY_DELAYS_MS[min(self._retries, len(_RETRY_DELAYS_MS) - 1)]
        self._retries += 1
        self._set_status(f"Reconnecting… ({self._retries})")
        LOGGER.info("Stream ended (reason %s), reload %d in %d ms", reason, self._retries, delay)
        self._retry_timer.start(delay)

    def toggle_pause(self) -> None:
        if self._lost:
            self._retries = 0
            self._set_status("Connecting…")
            self._reload()
            return
        self._player.pause = not self._player.pause

    def skip(self, seconds: int) -> None:
        self._seek(seconds, "relative")

    def toggle_mute(self) -> None:
        self._player.mute = not self._player.mute

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._show_controls()
        else:
            self.showFullScreen()
            self._wake_controls()

    def _seek_to_slider(self) -> None:
        self._seeking = False
        self._seek(self._position.value(), "absolute")

    def _seek(self, seconds: float, reference: str) -> None:
        if self._url is None:
            return
        try:
            self._player.seek(seconds, reference=reference, precision="keyframes")
        except SystemError:
            pass  # nothing seekable loaded yet

    # --- tracks -----------------------------------------------------------

    def _refresh_tracks(self) -> None:
        tracks = self._prop("track-list") or []
        self._fill(self._audio_box, [t for t in tracks if t.get("type") == "audio"], off=False)
        self._fill(self._sub_box, [t for t in tracks if t.get("type") == "sub"], off=True)

    @staticmethod
    def _fill(box: QComboBox, tracks: list[dict], off: bool) -> None:
        box.blockSignals(True)
        box.clear()
        if off:
            box.addItem("Subtitles off", "no")
        for track in tracks:
            box.addItem(_track_label(track), track.get("id"))
            if track.get("selected"):
                box.setCurrentIndex(box.count() - 1)
        box.setEnabled(box.count() > 1)
        box.setVisible(bool(tracks))
        box.blockSignals(False)

    def _choose_track(self, prop: str, box: QComboBox) -> None:
        setattr(self._player, prop, box.currentData())

    # --- state ------------------------------------------------------------

    def _prop(self, name: str):
        try:
            return getattr(self._player, name.replace("-", "_"))
        except Exception:  # noqa: BLE001 - unavailable while nothing plays
            return None

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def _tick(self) -> None:
        pos = self._prop("time-pos")
        paused = bool(self._prop("pause"))
        self._play_button.setIcon(theme.icon("play" if paused or self._lost else "pause"))
        self._mute_button.setIcon(theme.icon("volume-x" if self._prop("mute") else "volume-2"))
        if self._url is None or self._lost:
            return
        if pos is not None:
            self._last_pos = pos
        if self._retries and time.monotonic() - self._loaded_at > _STABLE_S:
            self._retries = 0

        buffering = self._prop("paused-for-cache")
        ahead = None
        cache_end = self._prop("demuxer-cache-time")
        if cache_end is not None and pos is not None:
            ahead = max(cache_end - pos, 0)
        if self._retry_timer.isActive():
            pass  # keep "Reconnecting…"
        elif buffering:
            self._set_status(f"Buffering {self._prop('cache-buffering-state') or 0}%")
        elif self._live:
            self._set_status(f"LIVE · {ahead:.0f} s buffered" if ahead is not None else "LIVE")
        elif ahead is not None:
            self._set_status(f"{ahead:.0f} s buffered")

        if self._live:
            self._time_label.setText(_clock(pos))
            return
        duration = self._prop("duration")
        self._time_label.setText(f"{_clock(pos)} / {_clock(duration)}")
        if duration and not self._seeking:
            self._position.setRange(0, int(duration))
            self._position.setValue(int(pos or 0))

    # --- fullscreen controls ----------------------------------------------

    def _wake_controls(self) -> None:
        if self.isFullScreen():
            self._show_controls()
            self._hide_timer.start()

    def _show_controls(self) -> None:
        self._controls.show()
        self._video.unsetCursor()

    def _hide_controls(self) -> None:
        if self.isFullScreen():
            self._controls.hide()
            self._video.setCursor(QCursor(Qt.CursorShape.BlankCursor))

    # --- Qt events --------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        actions = {
            Qt.Key.Key_Space: self.toggle_pause,
            Qt.Key.Key_K: self.toggle_pause,
            Qt.Key.Key_Left: lambda: self.skip(-SMALL_SKIP_S),
            Qt.Key.Key_Right: lambda: self.skip(SMALL_SKIP_S),
            Qt.Key.Key_J: lambda: self.skip(-SKIP_S),
            Qt.Key.Key_L: lambda: self.skip(SKIP_S),
            Qt.Key.Key_Up: lambda: self._volume.setValue(self._volume.value() + 5),
            Qt.Key.Key_Down: lambda: self._volume.setValue(self._volume.value() - 5),
            Qt.Key.Key_M: self.toggle_mute,
            Qt.Key.Key_F: self.toggle_fullscreen,
            Qt.Key.Key_A: lambda: self._player.cycle("aid"),
            Qt.Key.Key_S: lambda: self._player.cycle("sid"),
        }
        if key == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
        elif key in actions:
            actions[key]()
            if key in (Qt.Key.Key_A, Qt.Key.Key_S):
                self._refresh_tracks()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        """Closing stops the stream (and frees the account's connection); the window is reused."""
        self._url = None
        self._retry_timer.stop()
        self._tick_timer.stop()
        self._player.command("stop")
        if self.isFullScreen():
            self.showNormal()
        self._show_controls()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Tear down mpv when the app quits."""
        self._retry_timer.stop()
        self._tick_timer.stop()
        self._video.release()
        self._player.terminate()

    @staticmethod
    def _log(level: str, component: str, message: str) -> None:
        # Live TV joined mid-stream logs decoder errors that heal by themselves.
        log = LOGGER.warning if level == "fatal" else LOGGER.debug
        log("mpv [%s] %s: %s", level, component, message.strip())
