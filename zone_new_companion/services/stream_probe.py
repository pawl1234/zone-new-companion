"""Decide whether a stream plays, and with audio and video, without external programs.

libmpv (bundled for the built-in player) opens the stream headless, decodes a
few seconds and reports which tracks produced frames. Without libmpv the
container is read instead (see container_probe), which sees the tracks but
cannot decode them.
"""

from __future__ import annotations

import locale
import threading
import time
from dataclasses import dataclass
from types import ModuleType

import requests

from zone_new_companion.services import libmpv
from zone_new_companion.services.container_probe import ProbeError, probe_container
from zone_new_companion.services.logger_service import logger_service

# Time for the stream to open and decode its first frames.
OPEN_SECONDS = 12
# Playback that must run once it started, proving data keeps arriving...
PLAY_SECONDS = 2.0
# ...within this long, which leaves room for buffering.
PLAY_WINDOW_SECONDS = 8
POLL_SECONDS = 0.2
# A failure this fast is a refusal at connect, typical of a one-connection account
# that is busy with another probe; StreamVerifier tries those again one by one.
QUICK_REFUSAL_SECONDS = 4


@dataclass(slots=True)
class ProbeResult:
    """What a probe found out about one stream."""

    has_video: bool = False
    has_audio: bool = False
    # Why the stream counts as offline; empty when it works.
    failure: str = ""
    quick_refusal: bool = False

    @property
    def status(self) -> str:
        if self.failure:
            return f"OFF ({self.failure})"
        if self.has_video and self.has_audio:
            return "OK (Video+Audio)"
        if self.has_video:
            return "OK (Video)"
        if self.has_audio:
            return "OK (Audio)"
        return "OK (Reachable)"


def probe_stream(url: str, session: requests.Session) -> ProbeResult:
    """Probe with libmpv when it loads, otherwise by reading the container."""
    mpv = libmpv.load()
    if mpv is not None:
        return _probe_with_mpv(mpv, url)
    return _probe_container(url, session)


def _probe_with_mpv(mpv: ModuleType, url: str) -> ProbeResult:
    started = time.monotonic()
    loaded = threading.Event()
    ended = threading.Event()
    end_reason: list[int] = []

    # Qt resets the locale on startup; libmpv refuses to run without a C numeric locale.
    locale.setlocale(locale.LC_NUMERIC, "C")
    player = mpv.MPV(
        vo="null",
        ao="null",
        config=False,
        load_scripts=False,
        terminal=False,
        ytdl=False,
        idle="yes",
        osc=False,
        input_default_bindings=False,
        user_agent=libmpv.USER_AGENT,
        network_timeout=OPEN_SECONDS,
        sid="no",
        # Decoding only has to prove frames come out: keep it cheap.
        hwdec="no",
        vd_lavc_threads=1,
        vd_lavc_fast=True,
        vd_lavc_skiploopfilter="all",
    )

    @player.event_callback("file-loaded")
    def _on_loaded(_event) -> None:
        loaded.set()

    @player.event_callback("end-file")
    def _on_end(event) -> None:
        end_reason.append(event.data.reason)
        ended.set()

    has_video = has_audio = flowing = False
    try:
        player.play(url)
        deadline = started + OPEN_SECONDS
        first_pos: float | None = None
        while not ended.is_set() and time.monotonic() < deadline:
            has_video = has_video or bool(player.video_params)
            has_audio = has_audio or bool(player.audio_params)
            position = player.time_pos
            if position is not None and (has_video or has_audio):
                if first_pos is None:
                    first_pos = position
                    deadline = time.monotonic() + PLAY_WINDOW_SECONDS
                elif position - first_pos >= PLAY_SECONDS:
                    flowing = True
                    break
            time.sleep(POLL_SECONDS)
        # A short VOD file may end before PLAY_SECONDS; it still played.
        if not flowing and ended.is_set() and end_reason[:1] == [mpv.MpvEventEndFile.EOF]:
            flowing = has_video or has_audio
    except mpv.ShutdownError:
        pass
    finally:
        player.terminate()

    if flowing:
        return ProbeResult(has_video=has_video, has_audio=has_audio)
    if not loaded.is_set():
        elapsed = time.monotonic() - started
        # mpv gives up by itself at network_timeout; that is a timeout, too.
        failure = "Unreachable" if ended.is_set() and elapsed < OPEN_SECONDS - 1 else "Timeout"
        quick = ended.is_set() and elapsed < QUICK_REFUSAL_SECONDS
        return ProbeResult(failure=failure, quick_refusal=quick)
    if has_video or has_audio:
        return ProbeResult(has_video=has_video, has_audio=has_audio, failure="Stalled")
    return ProbeResult(failure="No Video/Audio")


def _probe_container(url: str, session: requests.Session) -> ProbeResult:
    started = time.monotonic()
    try:
        tracks = probe_container(session, url)
    except ProbeError as exc:
        quick = time.monotonic() - started < QUICK_REFUSAL_SECONDS
        return ProbeResult(failure=str(exc), quick_refusal=quick)
    except Exception as exc:  # noqa: BLE001 - a parser bug must not stop the batch
        logger_service.debug(f"Container probe failed for {url}: {exc}")
        return ProbeResult()
    if tracks is None:
        return ProbeResult()  # bytes arrived in a format we cannot read
    has_video, has_audio = tracks
    if not has_video and not has_audio:
        return ProbeResult(failure="No Video/Audio")
    return ProbeResult(has_video=has_video, has_audio=has_audio)
