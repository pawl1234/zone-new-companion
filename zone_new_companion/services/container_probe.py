"""Check a stream for audio and video by reading its container, without decoding.

The fallback for StreamVerifier when libmpv is missing. It downloads the start
of the stream (for HLS: the playlist, then one segment) and reads the track
list from it: MPEG-TS program tables, MP4 track handlers, or a raw ADTS audio
stream. A track counts only when packets for it actually arrived.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin

import requests
from requests import RequestException

from zone_new_companion.services.network import DEFAULT_TIMEOUT

# Enough MPEG-TS for several PAT/PMT repeats and a few hundred ms of video.
SAMPLE_BYTES = 1_500_000
# A progressive MP4 lists its tracks in the moov box; long films have big ones.
MAX_MOOV_BYTES = 16_000_000
READ_SECONDS = 10

TS_PACKET = 188
# Packets per track before it counts as present.
MIN_VIDEO_PACKETS = 5
MIN_AUDIO_PACKETS = 2

# MPEG-TS stream_type values from the PMT.
TS_VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x20, 0x24, 0x42, 0xD1, 0xEA}
TS_AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x1C, 0x81, 0x84, 0x87}
# Descriptors that mark a private-data (0x06) stream as audio: AC-3, E-AC-3, DTS, AAC, extension.
TS_AUDIO_DESCRIPTORS = {0x6A, 0x7A, 0x7B, 0x7C, 0x7F}
TS_REGISTRATION_AUDIO = {b"AC-3", b"EAC3", b"Opus", b"DTS1", b"DTS2", b"DTS3"}
TS_REGISTRATION_VIDEO = {b"HEVC", b"VC-1"}


class ProbeError(Exception):
    """The stream could not be fetched; the message is the user-facing reason."""


def probe_container(session: requests.Session, url: str) -> tuple[bool, bool] | None:
    """(has_video, has_audio) found in the stream, or None when its format is unknown.

    Raises ProbeError when nothing could be downloaded.
    """
    data, final_url = _fetch(session, url, SAMPLE_BYTES)
    if _is_playlist(data):
        return _probe_hls(session, final_url, data)
    if ".m3u8" in final_url.lower():
        raise ProbeError("Invalid Playlist")
    return _probe_bytes(session, final_url, data)


def _probe_hls(session: requests.Session, url: str, playlist: bytes) -> tuple[bool, bool] | None:
    text = playlist.decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in text:
        variant = _uri_after(text, "#EXT-X-STREAM-INF")
        if not variant:
            raise ProbeError("Empty Playlist")
        url = urljoin(url, variant)
        playlist, url = _fetch(session, url, SAMPLE_BYTES)
        text = playlist.decode("utf-8", "replace")

    segments = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not segments:
        raise ProbeError("Empty Playlist")
    # The newest segment: on live TV the oldest may already be gone.
    segment, _ = _fetch(session, urljoin(url, segments[-1]), SAMPLE_BYTES)

    init_uri = _map_uri(text)
    if init_uri:
        # Fragmented MP4: tracks are in the init segment, the media in the segments.
        init, _ = _fetch(session, urljoin(url, init_uri), MAX_MOOV_BYTES)
        tracks = _parse_mp4(init)
        if tracks is None:
            return None
        has_media = _has_box(segment, b"mdat")
        return tracks[0] and has_media, tracks[1] and has_media
    return _probe_bytes(session, url, segment)


def _probe_bytes(session: requests.Session, url: str, data: bytes) -> tuple[bool, bool] | None:
    if _ts_start(data) is not None:
        return _parse_ts(data)
    if len(data) >= 8 and data[4:8] in (b"ftyp", b"moov", b"styp", b"moof", b"free", b"mdat", b"wide"):
        data = _complete_moov(session, url, data)
        return _parse_mp4(data)
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF0 == 0xF0:
        return False, True  # ADTS AAC: an audio-only stream
    if data.lstrip()[:1] == b"<":
        raise ProbeError("Error Page")  # HTML or XML, never a media container
    return None


# --- HTTP -------------------------------------------------------------------


def _fetch(session: requests.Session, url: str, limit: int) -> tuple[bytes, str]:
    """Up to `limit` bytes of the response body, and the URL after redirects."""
    try:
        response = session.get(url, stream=True, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
    except RequestException as exc:
        raise ProbeError("Unreachable") from exc
    with response:
        if response.status_code >= 400:
            raise ProbeError(f"HTTP {response.status_code}")
        data = _read(response, limit)
    if not data:
        raise ProbeError("No Data")
    return data, response.url or url


def _read(response: requests.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + READ_SECONDS
    try:
        for chunk in response.iter_content(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= limit or time.monotonic() > deadline:
                break
    except RequestException:
        pass  # A live stream cut off mid-read still gave us what arrived.
    return b"".join(chunks)


def _complete_moov(session: requests.Session, url: str, data: bytes) -> bytes:
    """Download the rest of a moov box that starts in `data` but is cut off."""
    for start, box_type, size in _boxes(data, 0, len(data)):
        if box_type != b"moov":
            continue
        end = start + size
        if end > len(data) and end <= MAX_MOOV_BYTES:
            more, _ = _fetch(session, url, end)
            if len(more) > len(data):
                return more
        break
    return data


# --- HLS --------------------------------------------------------------------


def _is_playlist(data: bytes) -> bool:
    return data.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U")


def _uri_after(text: str, tag: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(tag):
            for following in lines[index + 1:]:
                if following.strip() and not following.startswith("#"):
                    return following.strip()
    return ""


def _map_uri(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#EXT-X-MAP:") and 'URI="' in line:
            return line.split('URI="', 1)[1].split('"', 1)[0]
    return ""


# --- MPEG-TS ----------------------------------------------------------------


def _ts_start(data: bytes) -> int | None:
    """Offset of the first packet, found by three sync bytes 188 apart."""
    for offset in range(min(TS_PACKET, len(data) - 2 * TS_PACKET)):
        if data[offset] == data[offset + TS_PACKET] == data[offset + 2 * TS_PACKET] == 0x47:
            return offset
    return None


def _parse_ts(data: bytes) -> tuple[bool, bool] | None:
    """Tracks with packets in an MPEG-TS sample; None without a readable PMT."""
    start = _ts_start(data)
    if start is None:
        return None
    pmt_pids: set[int] = set()
    kinds: dict[int, str] = {}
    packets: dict[int, int] = {}
    for offset in range(start, len(data) - TS_PACKET + 1, TS_PACKET):
        packet = data[offset:offset + TS_PACKET]
        if packet[0] != 0x47:
            continue
        unit_start = packet[1] & 0x40
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        adaptation = (packet[3] >> 4) & 0x3
        if adaptation in (0, 2):
            continue  # no payload
        payload_at = 4 + (1 + packet[4] if adaptation == 3 else 0)
        if payload_at >= TS_PACKET:
            continue
        payload = packet[payload_at:]
        if pid == 0 and unit_start:
            pmt_pids.update(_pat_pids(payload))
        elif pid in pmt_pids and unit_start:
            kinds.update(_pmt_streams(payload))
        elif pid in kinds:
            packets[pid] = packets.get(pid, 0) + 1
    if not kinds:
        return None
    has_video = any(kind == "video" and packets.get(pid, 0) >= MIN_VIDEO_PACKETS for pid, kind in kinds.items())
    has_audio = any(kind == "audio" and packets.get(pid, 0) >= MIN_AUDIO_PACKETS for pid, kind in kinds.items())
    return has_video, has_audio


def _section(payload: bytes) -> bytes:
    """A PSI section without its header and CRC (assumes it fits in one packet)."""
    pointer = payload[0]
    section = payload[1 + pointer:]
    if len(section) < 3:
        return b""
    length = ((section[1] & 0x0F) << 8) | section[2]
    return section[:3 + length - 4]


def _pat_pids(payload: bytes) -> set[int]:
    section = _section(payload)
    if not section or section[0] != 0x00:
        return set()
    pids = set()
    for at in range(8, len(section) - 3, 4):
        program = (section[at] << 8) | section[at + 1]
        if program != 0:
            pids.add(((section[at + 2] & 0x1F) << 8) | section[at + 3])
    return pids


def _pmt_streams(payload: bytes) -> dict[int, str]:
    section = _section(payload)
    if len(section) < 12 or section[0] != 0x02:
        return {}
    at = 12 + (((section[10] & 0x0F) << 8) | section[11])
    streams = {}
    while at + 5 <= len(section):
        stream_type = section[at]
        pid = ((section[at + 1] & 0x1F) << 8) | section[at + 2]
        info_length = ((section[at + 3] & 0x0F) << 8) | section[at + 4]
        descriptors = section[at + 5:at + 5 + info_length]
        kind = _ts_kind(stream_type, descriptors)
        if kind:
            streams[pid] = kind
        at += 5 + info_length
    return streams


def _ts_kind(stream_type: int, descriptors: bytes) -> str:
    if stream_type in TS_VIDEO_TYPES:
        return "video"
    if stream_type in TS_AUDIO_TYPES:
        return "audio"
    at = 0
    while at + 2 <= len(descriptors):
        tag, length = descriptors[at], descriptors[at + 1]
        body = descriptors[at + 2:at + 2 + length]
        if tag in TS_AUDIO_DESCRIPTORS:
            return "audio"
        if tag == 0x05 and body[:4] in TS_REGISTRATION_AUDIO:
            return "audio"
        if tag == 0x05 and body[:4] in TS_REGISTRATION_VIDEO:
            return "video"
        at += 2 + length
    return ""


# --- MP4 --------------------------------------------------------------------


def _boxes(data: bytes, start: int, end: int):
    """(offset, type, size) of each box between start and end; sizes may run past the data."""
    at = start
    while at + 8 <= end:
        size = int.from_bytes(data[at:at + 4], "big")
        box_type = data[at + 4:at + 8]
        header = 8
        if size == 1 and at + 16 <= end:
            size = int.from_bytes(data[at + 8:at + 16], "big")
            header = 16
        elif size == 0:
            size = end - at
        if size < header:
            return
        yield at, box_type, size
        at += size


def _parse_mp4(data: bytes) -> tuple[bool, bool] | None:
    """Track kinds from the moov box; None when the sample has no moov."""
    for start, box_type, size in _boxes(data, 0, len(data)):
        if box_type == b"moov":
            handlers = _handlers(data, start + 8, min(start + size, len(data)))
            return b"vide" in handlers, b"soun" in handlers
    return None


def _handlers(data: bytes, start: int, end: int) -> set[bytes]:
    handlers = set()
    for at, box_type, size in _boxes(data, start, end):
        box_end = min(at + size, end)
        if box_type in (b"trak", b"mdia"):
            handlers |= _handlers(data, at + 8, box_end)
        elif box_type == b"hdlr" and at + 20 <= box_end:
            # Full box header (4), pre_defined (4), then the handler type.
            handlers.add(data[at + 16:at + 20])
    return handlers


def _has_box(data: bytes, wanted: bytes) -> bool:
    return any(box_type == wanted for _, box_type, _ in _boxes(data, 0, len(data)))
