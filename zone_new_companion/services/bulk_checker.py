"""Fast parallel account checker for Xtream, M3U and Stalker accounts.

Mirrors the web app's bulk checker: paste many Xtream `get.php` / `player_api.php`
URLs (one per line), check them all in parallel against `get_user_info`, and
report which accounts are reachable and valid. Saved accounts of every type
(Xtream, M3U playlist, Stalker portal + MAC) are rechecked the same way via
``check_accounts``. Once a host fails at the connection level, the remaining
entries for that same host are skipped so dead servers don't slow the whole
run down.

This module is intentionally Qt-free so it stays easy to test in isolation.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

from zone_new_companion.models import Credentials, PortalType
from zone_new_companion.services.network import normalize_url
from zone_new_companion.services.stalker_service import StalkerService

# Short timeouts keep the run snappy; slow/dead hosts shouldn't hold everyone up.
FAST_CONNECT_TIMEOUT = 4.0
FAST_READ_TIMEOUT = 8.0

# Xtream account states that mean the account itself is no longer usable.
REJECTED_STATUSES = {"expired", "banned", "disabled"}

# Stream probe: how many live channels to try, and the wall-clock budget for
# reading the first bytes of one stream.
PROBE_CHANNELS = 2
PROBE_READ_SECONDS = 5.0
PROBE_MAX_REDIRECTS = 5
# Enough bytes to see several MPEG-TS packets (188 bytes each).
PROBE_BYTES = 188 * 12
# Head of an M3U playlist read to validate it and pick channels to probe;
# full playlists can be tens of megabytes.
M3U_HEAD_BYTES = 256 * 1024
# M3U answers that mean the playlist (or its credentials) is gone for good.
M3U_REJECT_CODES = {401, 403, 404, 410}

# Upper bound on parallel requests. Enough to saturate the run without hammering
# a single host so hard that it rate-limits us.
MAX_WORKERS = 24


@dataclass(slots=True)
class BulkInput:
    """One parsed, checkable line."""

    line_number: int
    raw: str
    base_url: str
    username: str
    password: str
    # PortalType value; M3U keeps the whole playlist URL in base_url.
    portal_type: str = PortalType.XTREAM.value
    mac_address: str = ""


@dataclass(slots=True)
class BulkResult:
    """Outcome for one line (valid, invalid, or skipped)."""

    line_number: int
    raw: str
    base_url: str = ""
    username: str = ""
    password: str = ""
    # PortalType value: "xtream", "m3u" (base_url is the playlist URL) or
    # "stalker" (base_url is the portal, identified by mac_address).
    portal_type: str = PortalType.XTREAM.value
    mac_address: str = ""
    ok: bool = False
    error: str = ""
    status: str = ""
    expiry: str = ""
    max_connections: str = ""
    active_connections: str = ""
    timezone: str = ""
    real_url: str = ""
    # Pinned by the user in the Working Accounts list; starred rows sort first.
    starred: bool = False
    # Second stage: can a live channel actually be streamed? None = not tested.
    stream_ok: bool | None = None
    stream_status: str = ""
    stream_checked_at: str = ""
    # When streams were first seen down; cleared once they work again.
    stream_down_since: str = ""
    # The server answered and refused the account (bad credentials, expired,
    # banned). Unlike an unreachable or overloaded server, this is final.
    rejected: bool = False
    # Internal: connection-level failure => the whole host is treated as dead.
    conn_level: bool = field(default=False, repr=False)
    # Internal: the account's allowed stream formats from get_user_info.
    output_formats: list[str] = field(default_factory=list, repr=False)


def _origin(url: str) -> str:
    """Return scheme://host[:port] for a parsed URL (no path/query)."""
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{scheme}://{host}{port}"


def parse_bulk_line(line_number: int, raw: str) -> BulkInput:
    """Parse one pasted line into a checkable entry.

    Accepts full Xtream URLs (get.php / player_api.php / m3u) that carry
    ``username`` and ``password`` query parameters. Raises ``ValueError`` for
    anything that can't be understood.
    """
    trimmed = raw.strip()
    if not trimmed:
        raise ValueError("Empty line.")

    with_scheme = trimmed if trimmed.lower().startswith(("http://", "https://")) else f"http://{trimmed}"
    parsed = urlparse(with_scheme)
    if not parsed.hostname:
        raise ValueError("Invalid URL.")

    params = parse_qs(parsed.query)
    username = (params.get("username") or [""])[0].strip()
    password = (params.get("password") or [""])[0].strip()
    if not username or not password:
        raise ValueError("URL is missing username/password parameters.")

    return BulkInput(
        line_number=line_number,
        raw=trimmed,
        base_url=_origin(with_scheme),
        username=username,
        password=password,
    )


def account_key(result: BulkResult) -> tuple[str, str, str, str, str]:
    """Identity of an account across checks (type + host + credentials)."""
    return (result.portal_type, result.base_url, result.username, result.password, result.mac_address.upper())


def result_from_credentials(credentials: Credentials) -> BulkResult:
    """A not-yet-checked account for the given login details."""
    return BulkResult(
        line_number=1,
        raw=credentials.base_url,
        base_url=credentials.base_url,
        username=credentials.username,
        password=credentials.password,
        portal_type=credentials.portal_type.value,
        mac_address=credentials.mac_address,
    )


def credentials_for(result: BulkResult, name: str = "Working Account") -> Credentials:
    """Login details to browse a checked account."""
    return Credentials(
        name=name,
        base_url=result.base_url,
        portal_type=PortalType(result.portal_type),
        username=result.username,
        password=result.password,
        mac_address=result.mac_address,
    )


def account_url(result: BulkResult) -> str:
    """A checkable get.php line for an already-parsed account."""
    query = urlencode({"username": result.username, "password": result.password})
    return f"{result.base_url.rstrip('/')}/get.php?{query}"


def friendly_error(error: str) -> str:
    """Turn a raw requests/HTTP error into a short plain-language reason."""
    text = error.lower()
    if not text:
        return "Not working"
    if "cancelled" in text:
        return "Check cancelled"
    if "timed out" in text or "timeout" in text:
        return "Server not responding"
    if "name or service" in text or "nameresolution" in text or "nodename" in text or "name resolution" in text:
        return "Server address not found"
    if "connection" in text and ("refused" in text or "failed to establish" in text or "reset" in text):
        return "Server refused the connection"
    if "ssl" in text:
        return "Secure connection failed"
    match = re.search(r"\b(\d{3}) (?:client|server) error", text)
    if match:
        code = int(match.group(1))
        if code in (401, 403):
            return "Access denied by server"
        if code == 404:
            return "Login API not found"
        if code == 503:
            return "Server temporarily unavailable"
        if code >= 500:
            return f"Server error ({code})"
        return f"Server rejected the request ({code})"
    if "unreachable" in text or "max retries exceeded" in text:
        return "Server unreachable"
    if "expecting value" in text or "json" in text or "unsupported server" in text:
        return "Server sent no valid answer"
    if "invalid username" in text or "invalid credentials" in text:
        return "Wrong username or password"
    if text.startswith("account "):
        return error.rstrip(".")
    return error if len(error) <= 60 else error[:57] + "..."


def now_stamp() -> str:
    """Local timestamp used for the stream check / down-since fields."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def is_server_unavailable(result: BulkResult) -> bool:
    """Login failed because the server was unreachable/overloaded, not refused.

    Such accounts may well come back, so they are kept (red) rather than
    dropped. Unparseable lines and cancelled checks never count.
    """
    return (
        not result.ok
        and not result.rejected
        and bool(result.base_url)
        and result.error != "Cancelled."
    )


def mark_server_unavailable(result: BulkResult) -> None:
    """Record an unavailable server as a stream outage on the result."""
    result.stream_ok = False
    result.stream_checked_at = now_stamp()
    result.stream_status = f"Server unavailable: {friendly_error(result.error)}"


def carry_stream_history(result: BulkResult, previous: BulkResult | None) -> None:
    """Keep the "down since" time across checks while streams stay down."""
    if result.stream_ok is False:
        earlier = previous.stream_down_since if previous is not None else ""
        result.stream_down_since = earlier or result.stream_checked_at or now_stamp()
    elif result.stream_ok:
        result.stream_down_since = ""


def _result_for(item: BulkInput, **fields: object) -> BulkResult:
    """An unchecked result carrying the entry's account details."""
    return BulkResult(
        line_number=item.line_number,
        raw=item.raw,
        base_url=item.base_url,
        username=item.username,
        password=item.password,
        portal_type=item.portal_type,
        mac_address=item.mac_address,
        **fields,
    )


def _ts_synced(data: bytes) -> bool:
    """True if ``data`` holds MPEG-TS packets (0x47 sync byte every 188 bytes)."""
    for offset in range(min(188, len(data))):
        if all(
            offset + n * 188 < len(data) and data[offset + n * 188] == 0x47
            for n in range(3)
        ):
            return True
    return False


def _stream_error(status_code: int) -> str:
    """Readable reason for a failed stream request."""
    if status_code in (401, 403):
        return f"Blocked (HTTP {status_code})"
    if status_code == 404:
        return "Channel not found (HTTP 404)"
    if status_code in (429, 458, 509):
        return f"Too many connections (HTTP {status_code})"
    if status_code >= 500:
        return f"Stream server error (HTTP {status_code})"
    return f"HTTP {status_code}"


class _StreamFailure(Exception):
    """A stream probe failed; ``host_dead`` marks a connection-level failure."""

    def __init__(self, reason: str, host: str = "", host_dead: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host
        self.host_dead = host_dead


def _format_expiry(exp_date: object) -> str:
    """Normalize an Xtream ``exp_date`` epoch into a readable local time."""
    raw = str(exp_date or "").strip()
    if not raw or raw.lower() in {"null", "none"}:
        return "Unlimited"
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(int(raw)).astimezone().strftime("%Y-%m-%d %H:%M")
        except (ValueError, OverflowError, OSError):
            return raw
    return raw


class BulkChecker:
    """Run many Xtream availability checks in parallel, with cancellation."""

    def __init__(self) -> None:
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Request that an in-flight run stop as soon as possible."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_many(
        self,
        lines: list[str],
        *,
        probe_streams: bool = True,
        progress_cb: Callable[[BulkResult, int, int], None] | None = None,
    ) -> list[BulkResult]:
        """Check every non-empty line and return results in input order.

        With ``probe_streams`` every account that logs in is also tested for
        actual streaming: a live channel is opened and must deliver video data.

        ``progress_cb(result, done, total)`` is invoked from worker threads as
        each line resolves, so callers that touch a GUI must marshal it onto the
        UI thread (e.g. via a Qt signal).
        """
        self._cancel.clear()

        parsed: list[BulkInput] = []
        results: dict[int, BulkResult] = {}

        line_number = 0
        for raw in lines:
            if not raw.strip():
                continue
            line_number += 1
            try:
                parsed.append(parse_bulk_line(line_number, raw))
            except ValueError as exc:
                results[line_number] = BulkResult(
                    line_number=line_number,
                    raw=raw.strip(),
                    ok=False,
                    error=str(exc),
                )

        return self._run(parsed, results, line_number, probe_streams, progress_cb)

    def check_accounts(
        self,
        accounts: list[BulkResult],
        *,
        probe_streams: bool = True,
        progress_cb: Callable[[BulkResult, int, int], None] | None = None,
    ) -> list[BulkResult]:
        """Check saved accounts of any type (Xtream, M3U, Stalker).

        Results come back in the order given; ``progress_cb`` as in
        ``check_many``.
        """
        self._cancel.clear()
        parsed = [
            BulkInput(
                line_number=index,
                raw=account.raw or account.base_url,
                base_url=account.base_url,
                username=account.username,
                password=account.password,
                portal_type=account.portal_type,
                mac_address=account.mac_address,
            )
            for index, account in enumerate(accounts, start=1)
        ]
        return self._run(parsed, {}, len(parsed), probe_streams, progress_cb)

    def _run(
        self,
        parsed: list[BulkInput],
        results: dict[int, BulkResult],
        total: int,
        probe_streams: bool,
        progress_cb: Callable[[BulkResult, int, int], None] | None,
    ) -> list[BulkResult]:
        """Check ``parsed`` in parallel next to already known ``results``."""
        done = len(results)

        if not parsed:
            return [results[n] for n in sorted(results)]

        # Submit same-host lines next to each other so that once a host is marked
        # dead, its remaining lines can short-circuit instead of timing out.
        parsed.sort(key=lambda item: urlparse(item.base_url).netloc)

        dead_hosts: dict[str, str] = {}
        # Stream servers (often a redirect target) that refused connections.
        dead_stream_hosts: dict[str, str] = {}
        lock = threading.Lock()
        session = self._build_session()

        def work(item: BulkInput) -> BulkResult:
            if self._cancel.is_set():
                return _result_for(item, error="Cancelled.")

            host = urlparse(item.base_url).netloc
            with lock:
                dead_reason = dead_hosts.get(host)
            if dead_reason:
                skipped = _result_for(item, error=f"Skipped — {host} unreachable ({dead_reason})")
                mark_server_unavailable(skipped)
                return skipped

            probe = probe_streams and not self._cancel.is_set()
            if item.portal_type == PortalType.M3U.value:
                result, stream_urls = self._check_m3u(item, session)
            elif item.portal_type == PortalType.STALKER.value:
                result, stream_urls = self._check_stalker(item, probe)
            else:
                result, stream_urls = self._check_one(item, session), None
            if result.conn_level:
                with lock:
                    dead_hosts.setdefault(host, result.error)
            elif result.ok and probe and not self._cancel.is_set():
                if stream_urls is None:
                    self._probe_streams(item, result, session, dead_stream_hosts, lock)
                else:
                    self._probe_urls(result, stream_urls, session, dead_stream_hosts, lock)
            if is_server_unavailable(result):
                mark_server_unavailable(result)
            return result

        try:
            workers = min(MAX_WORKERS, max(6, len(parsed)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for result in pool.map(work, parsed):
                    results[result.line_number] = result
                    done += 1
                    if progress_cb is not None:
                        progress_cb(result, done, total)
        finally:
            session.close()

        return [results[n] for n in sorted(results)]

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
            max_retries=0,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                    "AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp"
                ),
                "Accept": "*/*",
            },
        )
        return session

    def _check_one(self, item: BulkInput, session: requests.Session) -> BulkResult:
        """Call get_user_info and translate the response into a BulkResult."""
        base = item.base_url.rstrip("/")
        endpoints = [
            normalize_url(base, "player_api.php"),
            normalize_url(base, "panel_api.php"),
        ]
        params = {
            "username": item.username,
            "password": item.password,
            "action": "get_user_info",
        }
        timeout = (FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT)

        last_error = "Check failed."
        # Set once the server itself answers that the account is not valid.
        rejection = ""
        for endpoint in endpoints:
            # Some portals require GET, others POST — try both before giving up.
            for method in ("get", "post"):
                if self._cancel.is_set():
                    return BulkResult(
                        line_number=item.line_number,
                        raw=item.raw,
                        base_url=item.base_url,
                        username=item.username,
                        password=item.password,
                        error="Cancelled.",
                    )
                try:
                    if method == "get":
                        response = session.get(endpoint, params=params, timeout=timeout)
                    else:
                        response = session.post(endpoint, data=params, timeout=timeout)
                    response.raise_for_status()
                    payload = response.json()
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.SSLError) as exc:
                    # Connection-level: the host itself is unreachable -> mark dead.
                    return BulkResult(
                        line_number=item.line_number,
                        raw=item.raw,
                        base_url=item.base_url,
                        username=item.username,
                        password=item.password,
                        error=self._short_error(exc),
                        conn_level=True,
                    )
                except (requests.RequestException, ValueError) as exc:
                    last_error = self._short_error(exc)
                    continue

                if not isinstance(payload, dict):
                    last_error = "Unsupported server response."
                    continue
                user_info = payload.get("user_info")
                if not isinstance(user_info, dict) or not user_info or str(user_info.get("auth", "1")) == "0":
                    message = str(payload.get("message", "")).strip()
                    rejection = message or "Invalid username or password."
                    continue
                account_status = str(user_info.get("status", "")).strip()
                if account_status.lower() in REJECTED_STATUSES:
                    rejection = f"Account {account_status.lower()}."
                    continue

                server_info = payload.get("server_info") if isinstance(payload, dict) else {}
                if not isinstance(server_info, dict):
                    server_info = {}

                protocol = str(server_info.get("server_protocol", "http"))
                host = str(server_info.get("url", urlparse(item.base_url).hostname or ""))
                return BulkResult(
                    line_number=item.line_number,
                    raw=item.raw,
                    base_url=item.base_url,
                    username=item.username,
                    password=item.password,
                    ok=True,
                    status=str(user_info.get("status", "Unknown")),
                    expiry=_format_expiry(user_info.get("exp_date")),
                    max_connections=str(user_info.get("max_connections", "N/A")),
                    active_connections=str(user_info.get("active_cons", "0")),
                    timezone=str(server_info.get("timezone", "Unknown")),
                    real_url=f"{protocol}://{host}" if host else item.base_url,
                    output_formats=[str(fmt) for fmt in user_info.get("allowed_output_formats") or []],
                )

        return BulkResult(
            line_number=item.line_number,
            raw=item.raw,
            base_url=item.base_url,
            username=item.username,
            password=item.password,
            error=rejection or last_error,
            rejected=bool(rejection),
        )

    def _check_m3u(self, item: BulkInput, session: requests.Session) -> tuple[BulkResult, list[str]]:
        """Fetch the head of an M3U playlist; return it with a few channel URLs."""
        result = _result_for(item)
        try:
            response = session.get(
                item.base_url, stream=True, timeout=(FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT),
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.SSLError) as exc:
            result.error, result.conn_level = self._short_error(exc), True
            return result, []
        except requests.RequestException as exc:
            result.error = self._short_error(exc)
            return result, []
        if response.status_code >= 400:
            response.close()
            result.error = f"{response.status_code} Client Error" if response.status_code < 500 else (
                f"{response.status_code} Server Error"
            )
            result.rejected = response.status_code in M3U_REJECT_CODES
            return result, []
        final_url = response.url
        text = self._read_head(response, limit=M3U_HEAD_BYTES).decode("utf-8", "replace")
        if "#EXTINF" not in text and "#EXTM3U" not in text:
            # The server answered, but with no playlist: expired or wrong link.
            result.error = "Empty playlist" if not text.strip() else "Not an M3U playlist"
            result.rejected = True
            return result, []

        lines = [line.strip() for line in text.splitlines()]
        stream_urls = [
            lines[index + 1]
            for index, line in enumerate(lines[:-1])
            if line.startswith("#EXTINF") and lines[index + 1].startswith(("http://", "https://"))
        ]
        result.ok = True
        result.status = "Playlist OK"
        result.expiry = "N/A"
        result.real_url = _origin(final_url)
        # A get.php playlist is an Xtream account underneath: ask for its details.
        parsed = urlparse(item.base_url)
        params = parse_qs(parsed.query)
        username = (params.get("username") or [""])[0]
        password = (params.get("password") or [""])[0]
        if parsed.path.endswith("get.php") and username and password:
            xtream = BulkInput(item.line_number, item.raw, _origin(item.base_url), username, password)
            info = self._check_one(xtream, session)
            if info.ok:
                result.status = info.status
                result.expiry = info.expiry
                result.max_connections = info.max_connections
                result.active_connections = info.active_connections
                result.timezone = info.timezone
        # Live channels come first in most playlists; skip VOD files.
        live = [url for url in stream_urls if not re.search(r"/(movie|series)/", url)]
        return result, (live or stream_urls)[:PROBE_CHANNELS]

    def _check_stalker(self, item: BulkInput, probe: bool) -> tuple[BulkResult, list[str]]:
        """Log in to a Stalker portal with the MAC; return a few channel URLs."""
        result = _result_for(item)
        timeout = (FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT)
        service = StalkerService()
        credentials = Credentials(
            name="Check", base_url=item.base_url, portal_type=PortalType.STALKER, mac_address=item.mac_address,
        )
        # Own session: portals keep per-MAC cookies that must not mix.
        session = self._build_session()
        try:
            info = service.check_login(credentials, session, timeout)
        except PermissionError as exc:
            result.error, result.rejected = str(exc), True
            session.close()
            return result, []
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.SSLError) as exc:
            result.error, result.conn_level = self._short_error(exc), True
            session.close()
            return result, []
        except requests.RequestException as exc:
            result.error = self._short_error(exc)
            session.close()
            return result, []

        result.ok = True
        result.status = info["status"]
        result.expiry = info["expiry"]
        result.max_connections = info["max_connections"]
        result.active_connections = info["active_connections"]
        result.timezone = info["timezone"]
        result.real_url = _origin(item.base_url)
        stream_urls: list[str] = []
        if probe and not self._cancel.is_set():
            try:
                stream_urls = service.sample_live_stream_urls(session, timeout, PROBE_CHANNELS)
            except requests.RequestException:
                stream_urls = []
        session.close()
        return result, stream_urls

    def _probe_streams(
        self,
        item: BulkInput,
        result: BulkResult,
        session: requests.Session,
        dead_stream_hosts: dict[str, str],
        lock: threading.Lock,
    ) -> None:
        """Open a few live channels and record whether video actually flows."""
        result.stream_checked_at = now_stamp()
        try:
            stream_ids = self._sample_live_streams(item, session)
        except requests.RequestException as exc:
            result.stream_ok, result.stream_status = False, f"Channel list failed: {friendly_error(str(exc))}"
            return
        if not stream_ids:
            result.stream_ok, result.stream_status = False, "No live channels"
            return

        formats = result.output_formats
        ext = "ts" if not formats or "ts" in formats else ("m3u8" if "m3u8" in formats else formats[0])
        base = item.base_url.rstrip("/")
        urls = [f"{base}/live/{item.username}/{item.password}/{stream_id}.{ext}" for stream_id in stream_ids]
        self._probe_urls(result, urls, session, dead_stream_hosts, lock)

    def _probe_urls(
        self,
        result: BulkResult,
        urls: list[str],
        session: requests.Session,
        dead_stream_hosts: dict[str, str],
        lock: threading.Lock,
    ) -> None:
        """Record whether any of ``urls`` delivers video data."""
        result.stream_checked_at = now_stamp()
        if not urls:
            result.stream_ok, result.stream_status = False, "No live channels"
            return
        first_failure = ""
        for url in urls:
            if self._cancel.is_set():
                return
            try:
                self._probe_one_stream(url, session, dead_stream_hosts, lock)
            except _StreamFailure as failure:
                if failure.host_dead and failure.host:
                    with lock:
                        dead_stream_hosts.setdefault(failure.host, failure.reason)
                first_failure = first_failure or failure.reason
                continue
            result.stream_ok, result.stream_status = True, "Streaming"
            return
        result.stream_ok, result.stream_status = False, first_failure or "Not streaming"

    def _sample_live_streams(self, item: BulkInput, session: requests.Session) -> list[str]:
        """A few live stream ids, taken from the first non-empty categories."""
        endpoint = normalize_url(item.base_url.rstrip("/"), "player_api.php")
        auth = {"username": item.username, "password": item.password}
        timeout = (FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT)

        response = session.get(endpoint, params={**auth, "action": "get_live_categories"}, timeout=timeout)
        response.raise_for_status()
        try:
            categories = response.json()
        except ValueError:
            categories = []
        if not isinstance(categories, list):
            categories = []

        stream_ids: list[str] = []
        # Category lists are small; the full get_live_streams can be megabytes.
        for category in categories[:3]:
            if not isinstance(category, dict) or len(stream_ids) >= PROBE_CHANNELS:
                break
            params = {**auth, "action": "get_live_streams", "category_id": category.get("category_id")}
            response = session.get(endpoint, params=params, timeout=timeout)
            response.raise_for_status()
            try:
                rows = response.json()
            except ValueError:
                continue
            for row in rows if isinstance(rows, list) else []:
                stream_id = row.get("stream_id") if isinstance(row, dict) else None
                if stream_id:
                    stream_ids.append(str(stream_id))
                if len(stream_ids) >= PROBE_CHANNELS:
                    break
        return stream_ids

    def _open_stream(
        self,
        url: str,
        session: requests.Session,
        dead_stream_hosts: dict[str, str],
        lock: threading.Lock,
    ) -> requests.Response:
        """GET ``url``, following redirects by hand so dead hosts short-circuit."""
        timeout = (FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT)
        for _ in range(PROBE_MAX_REDIRECTS + 1):
            host = urlparse(url).netloc
            with lock:
                dead_reason = dead_stream_hosts.get(host)
            if dead_reason:
                raise _StreamFailure(f"Stream server {host} unreachable")
            try:
                response = session.get(url, stream=True, timeout=timeout, allow_redirects=False)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                raise _StreamFailure(f"Stream server {host} unreachable", host, host_dead=True) from exc
            except requests.RequestException as exc:
                raise _StreamFailure(self._short_error(exc)) from exc
            location = response.headers.get("Location")
            if response.is_redirect and location:
                response.close()
                url = urljoin(url, location)
                continue
            if response.status_code >= 400:
                response.close()
                raise _StreamFailure(_stream_error(response.status_code))
            return response
        raise _StreamFailure("Too many redirects")

    def _read_head(self, response: requests.Response, limit: int = PROBE_BYTES) -> bytes:
        """Read up to ``limit`` bytes within the probe's time budget, then close."""
        data = bytearray()
        deadline = time.monotonic() + PROBE_READ_SECONDS
        try:
            for chunk in response.iter_content(chunk_size=4096):
                data.extend(chunk)
                if len(data) >= limit or time.monotonic() > deadline or self._cancel.is_set():
                    break
        except requests.RequestException:
            pass
        finally:
            # Free the connection slot right away - many accounts allow only one.
            response.close()
        return bytes(data)

    def _probe_one_stream(
        self,
        url: str,
        session: requests.Session,
        dead_stream_hosts: dict[str, str],
        lock: threading.Lock,
    ) -> None:
        """Raise ``_StreamFailure`` unless ``url`` delivers media data."""
        response = self._open_stream(url, session, dead_stream_hosts, lock)
        final_url = response.url
        is_playlist = ".m3u8" in urlparse(url).path
        head = self._read_head(response, limit=64 * 1024 if is_playlist else PROBE_BYTES)
        if _ts_synced(head):
            return
        if not head.lstrip().startswith(b"#EXTM3U"):
            raise _StreamFailure("No video data" if head else "Empty stream")

        # HLS: follow a master playlist to its first variant, then fetch a segment.
        for _ in range(2):
            lines = [line.strip() for line in head.decode("utf-8", "replace").splitlines()]
            target = next((line for line in lines if line and not line.startswith("#")), "")
            if not target:
                raise _StreamFailure("Empty playlist")
            response = self._open_stream(urljoin(final_url, target), session, dead_stream_hosts, lock)
            final_url = response.url
            is_playlist = ".m3u8" in target.split("?")[0]
            head = self._read_head(response, limit=64 * 1024 if is_playlist else PROBE_BYTES)
            if not is_playlist:
                if len(head) < 188:
                    raise _StreamFailure("No video data")
                return
        raise _StreamFailure("Playlist nesting too deep")

    @staticmethod
    def _short_error(exc: Exception) -> str:
        text = str(exc).strip()
        if not text:
            return exc.__class__.__name__
        lowered = text.lower()
        # The useful part of a DNS failure sits at the end of a long message.
        if "nameresolution" in lowered or "nodename nor servname" in lowered or "name or service" in lowered:
            return "Name resolution failed (server address not found)"
        # Trim noisy requests/urllib3 wrapper text to something readable.
        if len(text) > 140:
            text = text[:137] + "..."
        return text
