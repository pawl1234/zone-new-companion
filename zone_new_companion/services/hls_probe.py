"""Find an HLS (.m3u8) variant of a stream URL.

Apple TV plays HLS over AirPlay but not raw MPEG-TS. Xtream and Stalker panels
usually serve the same stream as HLS when asked for the m3u8 extension, so a
.ts URL often has a working .m3u8 twin that needs no relay on the Mac.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from requests import RequestException

from zone_new_companion.services.logger_service import logger_service
from zone_new_companion.services.network import create_session

PROBE_TIMEOUT = 8
# A playlist announces itself in the first line; a TS stream starts with 0x47 sync bytes.
_HEAD_BYTES = 2048
# Xtream short form without extension: /live/user/pass/1234 or /user/pass/1234
_XTREAM_NO_EXT = re.compile(r"/\d+$")


def is_hls_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.path.lower().endswith(".m3u8"):
        return True
    return any(key == "extension" and value.lower() == "m3u8" for key, value in parse_qsl(parts.query))


def hls_candidates(url: str) -> list[str]:
    """HLS URLs to try for a stream URL, best guess first."""
    if is_hls_url(url):
        return [url]
    parts = urlsplit(url)
    candidates: list[str] = []

    path = parts.path
    lower_path = path.lower()
    if lower_path.endswith(".ts"):
        candidates.append(urlunsplit(parts._replace(path=f"{path[:-3]}.m3u8")))
    elif _XTREAM_NO_EXT.search(path):
        candidates.append(urlunsplit(parts._replace(path=f"{path}.m3u8")))

    # Stalker: .../play/live.php?mac=...&stream=123&extension=ts
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "extension" for key, _ in query):
        swapped = [(key, "m3u8" if key == "extension" else value) for key, value in query]
        candidates.append(urlunsplit(parts._replace(query=urlencode(swapped))))

    return candidates


def is_hls_playlist(url: str, session: requests.Session, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when the URL answers with an HLS playlist (#EXTM3U)."""
    try:
        with session.get(url, stream=True, timeout=timeout, allow_redirects=True) as response:
            if response.status_code != 200:
                return False
            head = b""
            for chunk in response.iter_content(chunk_size=512):
                head += chunk
                if len(head) >= _HEAD_BYTES:
                    break
    except RequestException as exc:
        logger_service.debug(f"HLS probe failed for {url}: {exc}")
        return False
    return head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U")


def find_hls_url(url: str, session: requests.Session | None = None) -> str | None:
    """Return a working HLS URL for the stream, or None if the server offers none."""
    session = session or create_session()
    for candidate in hls_candidates(url):
        if is_hls_playlist(candidate, session):
            logger_service.info(f"HLS variant found: {candidate}")
            return candidate
    logger_service.info(f"No HLS variant for: {url}")
    return None
