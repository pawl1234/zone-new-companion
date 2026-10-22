"""Fast parallel account checker for Xtream, M3U and Stalker accounts.

Mirrors the web app's bulk checker: paste many Xtream `get.php` / `player_api.php`
URLs (one per line, or "Host/User/Pass/M3U" account blocks), check them all in parallel against `get_user_info`, and
report which accounts are reachable and valid. Saved accounts of every type
(Xtream, M3U playlist, Stalker portal + MAC) are rechecked the same way via
``check_accounts``. Once a host keeps failing at the connection level, the
remaining entries for that same host are skipped so dead servers don't slow
the whole run down; a single timeout of a busy server doesn't count. Only a few accounts are checked at once per server: panels reject
bursts with 503 answers that would read as dead accounts.

Logging in also fetches the account's category lists. They identify its
catalog (accounts on one server can have different packages), are handed back
in ``BulkChecker.catalogs`` for the search index, and let the stream probe
reuse channel ids already known for that catalog instead of downloading
channel lists again.

This module is intentionally Qt-free so it stays easy to test in isolation.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from itertools import zip_longest
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

from zone_new_companion.models import Credentials, PlaylistCategory, PortalType
from zone_new_companion.services.catalog_indexer import catalog_fingerprint
from zone_new_companion.services.network import normalize_url
from zone_new_companion.services.stalker_service import StalkerService
from zone_new_companion.services.xtream_service import XtreamService

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
# Xtream panels build a get.php playlist on request; big ones take a while to
# send the first byte.
M3U_PLAYLIST_READ_TIMEOUT = 30.0

# Category lists per fetch_categories tab; all three make up the catalog fingerprint.
CATEGORY_ACTIONS = {
    "Live": "get_live_categories",
    "Movies": "get_vod_categories",
    "Series": "get_series_categories",
}

# Upper bound on parallel requests across all servers.
MAX_WORKERS = 24
# Accounts checked at once on one server. Panels answer more with "503 Service
# Temporarily Unavailable", which reads as a dead account; 24 at once got 7 of
# 20 working accounts rejected, 6 at once none, in the same time.
MAX_PER_HOST = 6
# Failed connections in a row before a server counts as dead for the rest of
# the run: few for one that never answered, a full round of its parallel checks
# more for one that did (a busy server times out now and then).
DEAD_AFTER_FAILURES = 3
DEAD_AFTER_STREAK = 2 * MAX_PER_HOST


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


# Field names understood in pasted "Key: value" account blocks.
_BLOCK_KEYS = {
    "host": "host", "server": "host", "portal": "host",
    "user": "user", "username": "user",
    "pass": "pass", "password": "pass",
    "m3u": "url", "url": "url", "link": "url",
}
_BLOCK_FIELD = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ]*?)\s*:\s*(.+?)\s*$")
_SEPARATOR = re.compile(r"^\s*[-=_*#~]{3,}\s*$")


def split_bulk_entries(lines: list[str]) -> list[str]:
    """Turn pasted text into one checkable entry per account.

    Plain URLs (one per line) pass through unchanged. Account blocks like::

        Host: example.com:80
        User: USER
        Pass: PASS
        Status: Active
        M3U: http://example.com:80/get.php?username=USER&password=PASS

    become their ``M3U``/``URL`` link, or a ``get.php`` link built from
    Host/User/Pass when the block has none. Blocks end at a separator line
    (``-----``), a blank line, or a repeated field; other fields are ignored.
    """
    entries: list[str] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if not block:
            return
        url, host = block.get("url", ""), block.get("host", "")
        user, password = block.get("user", ""), block.get("pass", "")
        if url and parse_qs(urlparse(url).query).get("username"):
            entries.append(url)
        elif host and user and password:
            base = host if host.lower().startswith(("http://", "https://")) else f"http://{host}"
            query = urlencode({"username": user, "password": password, "type": "m3u_plus"})
            entries.append(f"{_origin(base)}/get.php?{query}")
        else:
            # Unusable block: keep it as one entry so the user sees it failed.
            entries.append(url or " ".join(f"{key}: {value}" for key, value in block.items()))
        block.clear()

    for raw in lines:
        if not raw.strip() or _SEPARATOR.match(raw):
            flush()
            continue
        match = None if raw.strip().lower().startswith(("http://", "https://")) else _BLOCK_FIELD.match(raw)
        key = _BLOCK_KEYS.get(match.group(1).lower()) if match else None
        if key:
            if key in block:
                flush()
            block[key] = match.group(2)
        elif match:
            continue  # Another field of the block (Status, Expires, ...).
        else:
            flush()
            entries.append(raw.strip())
    flush()
    return entries


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


def _get_php_account(item: BulkInput) -> BulkInput | None:
    """The Xtream account behind an M3U ``get.php`` link, if it is one."""
    if item.portal_type != PortalType.M3U.value:
        return None
    parsed = urlparse(item.base_url)
    params = parse_qs(parsed.query)
    username = (params.get("username") or [""])[0]
    password = (params.get("password") or [""])[0]
    if not (parsed.path.endswith("get.php") and username and password):
        return None
    return BulkInput(item.line_number, item.raw, _origin(item.base_url), username, password)


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
    """A stream probe failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _name_not_found(error: str) -> bool:
    """The server's name doesn't resolve: retrying the same server is pointless."""
    text = error.lower()
    return "name resolution" in text or "nameresolution" in text or "nodename nor servname" in text or (
        "name or service" in text
    )


class _HostHealth:
    """Which servers of a run count as dead, so their remaining checks are skipped.

    Any answer resets a server's streak of failed connections. It is given up
    after DEAD_AFTER_FAILURES failures in a row while it never answered in this
    run, after DEAD_AFTER_STREAK once it had; a name that doesn't resolve is
    dead at once.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._answered: set[str] = set()
        self._streak: dict[str, int] = {}
        self._dead: dict[str, str] = {}

    def dead_reason(self, host: str) -> str:
        with self._lock:
            return self._dead.get(host, "")

    def answered(self, host: str) -> None:
        with self._lock:
            self._answered.add(host)
            self._streak[host] = 0

    def failed(self, host: str, error: str) -> None:
        with self._lock:
            streak = self._streak[host] = self._streak.get(host, 0) + 1
            limit = DEAD_AFTER_STREAK if host in self._answered else DEAD_AFTER_FAILURES
            if streak >= limit or _name_not_found(error):
                self._dead.setdefault(host, error)


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


@dataclass(slots=True)
class CatalogSighting:
    """An account's category lists, as seen while checking it."""

    account: BulkResult
    fingerprint: str
    categories: dict[str, list[PlaylistCategory]]


class BulkChecker:
    """Run many Xtream availability checks in parallel, with cancellation."""

    def __init__(self, known_live_ids: Callable[[str, int], list[str]] | None = None) -> None:
        """``known_live_ids(fingerprint, count)`` gives live stream ids of an indexed catalog."""
        self._cancel = threading.Event()
        self._known_live_ids = known_live_ids
        # Filled by a run: the catalogs of the accounts that logged in.
        self.catalogs: list[CatalogSighting] = []

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
        for raw in split_bulk_entries(lines):
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
        self.catalogs = []

        if not parsed:
            return [results[n] for n in sorted(results)]

        # Alternate between servers so each gets its share of the workers; at
        # most MAX_PER_HOST run on one server, and once a server is marked dead
        # its waiting lines short-circuit instead of timing out.
        by_host: dict[str, list[BulkInput]] = {}
        for item in parsed:
            by_host.setdefault(urlparse(item.base_url).netloc, []).append(item)
        parsed = [item for row in zip_longest(*by_host.values()) for item in row if item is not None]
        host_slots = {host: threading.Semaphore(MAX_PER_HOST) for host in by_host}

        api_hosts = _HostHealth()
        # Stream servers, often a redirect target.
        stream_hosts = _HostHealth()
        # Live stream ids that streamed, per catalog fingerprint.
        catalog_ids: dict[str, list[str]] = {}
        lock = threading.Lock()
        session = self._build_session()

        def work(item: BulkInput) -> BulkResult:
            host = urlparse(item.base_url).netloc
            with host_slots[host]:
                return check(item, host)

        def check(item: BulkInput, host: str) -> BulkResult:
            if self._cancel.is_set():
                return _result_for(item, error="Cancelled.")

            dead_reason = api_hosts.dead_reason(host)
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
                api_hosts.failed(host, result.error)
            else:
                api_hosts.answered(host)
            if result.ok and probe and not self._cancel.is_set():
                if stream_urls is None:
                    account = _get_php_account(item) or item
                    self._probe_streams(account, result, session, stream_hosts, catalog_ids, lock)
                else:
                    self._probe_urls(result, stream_urls, session, stream_hosts)
            if is_server_unavailable(result):
                mark_server_unavailable(result)
            return result

        try:
            workers = min(MAX_WORKERS, max(6, len(parsed)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # Report each account as it finishes, not in input order: one
                # slow server would otherwise hold the progress count back.
                for future in as_completed([pool.submit(work, item) for item in parsed]):
                    result = future.result()
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

    def _check_m3u(self, item: BulkInput, session: requests.Session) -> tuple[BulkResult, list[str] | None]:
        """Fetch the head of an M3U playlist; return it with a few channel URLs.

        A ``get.php`` playlist is an Xtream account underneath. Its fast login
        API is asked first: an unreachable server or refused account is settled
        without waiting for the server to build the whole playlist, which can
        take longer than a normal read timeout. When the account logs in but
        the playlist still can't be read, ``None`` is returned in place of the
        channel URLs so the streams are probed through the Xtream API instead.
        """
        result = _result_for(item)
        xtream = _get_php_account(item)
        info = self._check_one(xtream, session) if xtream is not None else None
        if info is not None and (info.conn_level or info.rejected):
            result.error, result.conn_level, result.rejected = info.error, info.conn_level, info.rejected
            return result, []
        logged_in = info is not None and info.ok
        if logged_in:
            result.status = info.status
            result.expiry = info.expiry
            result.max_connections = info.max_connections
            result.active_connections = info.active_connections
            result.timezone = info.timezone
            result.output_formats = info.output_formats
        read_timeout = M3U_PLAYLIST_READ_TIMEOUT if xtream is not None else FAST_READ_TIMEOUT

        def playlist_failed(error: str, **fields: bool) -> tuple[BulkResult, list[str] | None]:
            if logged_in:
                # The account works; only the playlist download didn't.
                result.ok = True
                result.real_url = info.real_url
                return result, None
            result.error = error
            for name, value in fields.items():
                setattr(result, name, value)
            return result, []

        try:
            response = session.get(item.base_url, stream=True, timeout=(FAST_CONNECT_TIMEOUT, read_timeout))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.SSLError) as exc:
            return playlist_failed(self._short_error(exc), conn_level=True)
        except requests.RequestException as exc:
            return playlist_failed(self._short_error(exc))
        if response.status_code >= 400:
            response.close()
            error = f"{response.status_code} Client Error" if response.status_code < 500 else (
                f"{response.status_code} Server Error"
            )
            return playlist_failed(error, rejected=response.status_code in M3U_REJECT_CODES)
        final_url = response.url
        text = self._read_head(response, limit=M3U_HEAD_BYTES).decode("utf-8", "replace")
        if "#EXTINF" not in text and "#EXTM3U" not in text:
            # The server answered, but with no playlist: expired or wrong link.
            return playlist_failed("Empty playlist" if not text.strip() else "Not an M3U playlist", rejected=True)

        lines = [line.strip() for line in text.splitlines()]
        stream_urls = [
            lines[index + 1]
            for index, line in enumerate(lines[:-1])
            if line.startswith("#EXTINF") and lines[index + 1].startswith(("http://", "https://"))
        ]
        result.ok = True
        if not logged_in:
            result.status = "Playlist OK"
            result.expiry = "N/A"
        result.real_url = _origin(final_url)
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
        stream_hosts: _HostHealth,
        catalog_ids: dict[str, list[str]],
        lock: threading.Lock,
    ) -> None:
        """Open a few live channels and record whether video actually flows.

        The category lists are fetched first and recorded in ``catalogs``.
        Channels known for the same catalog (from this run or the search
        index) are tried before downloading the account's own channel lists.
        """
        result.stream_checked_at = now_stamp()
        try:
            categories, complete = self._fetch_categories(item, session)
        except requests.RequestException as exc:
            result.stream_ok, result.stream_status = False, f"Channel list failed: {friendly_error(str(exc))}"
            return
        fingerprint = catalog_fingerprint(item.base_url, categories)
        if complete:
            with lock:
                self.catalogs.append(CatalogSighting(result, fingerprint, categories))
        if not categories["Live"]:
            result.stream_ok, result.stream_status = False, "No live channels"
            return

        with lock:
            known = catalog_ids.get(fingerprint, []) if complete else []
        if not known and complete and self._known_live_ids is not None:
            known = self._known_live_ids(fingerprint, PROBE_CHANNELS)
        if known:
            self._probe_ids(item, result, known, session, stream_hosts)
            if result.stream_ok:
                with lock:
                    catalog_ids.setdefault(fingerprint, known)
                return
            if not result.stream_status.startswith("Channel not found") or self._cancel.is_set():
                return
            # Same categories but other channels: look at the account's own.

        try:
            stream_ids = self._sample_live_streams(item, session, categories["Live"])
        except requests.RequestException as exc:
            result.stream_ok, result.stream_status = False, f"Channel list failed: {friendly_error(str(exc))}"
            return
        if not stream_ids:
            result.stream_ok, result.stream_status = False, "No live channels"
            return
        self._probe_ids(item, result, stream_ids, session, stream_hosts)
        if result.stream_ok and complete:
            with lock:
                catalog_ids.setdefault(fingerprint, stream_ids)

    def _probe_ids(
        self,
        item: BulkInput,
        result: BulkResult,
        stream_ids: list[str],
        session: requests.Session,
        stream_hosts: _HostHealth,
    ) -> None:
        """Probe the account's live streams with these ids."""
        formats = result.output_formats
        ext = "ts" if not formats or "ts" in formats else ("m3u8" if "m3u8" in formats else formats[0])
        base = item.base_url.rstrip("/")
        urls = [f"{base}/live/{item.username}/{item.password}/{stream_id}.{ext}" for stream_id in stream_ids]
        self._probe_urls(result, urls, session, stream_hosts)

    def _probe_urls(
        self,
        result: BulkResult,
        urls: list[str],
        session: requests.Session,
        stream_hosts: _HostHealth,
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
                self._probe_one_stream(url, session, stream_hosts)
            except _StreamFailure as failure:
                first_failure = first_failure or failure.reason
                continue
            result.stream_ok, result.stream_status = True, "Streaming"
            return
        result.stream_ok, result.stream_status = False, first_failure or "Not streaming"

    def _fetch_categories(
        self, item: BulkInput, session: requests.Session,
    ) -> tuple[dict[str, list[PlaylistCategory]], bool]:
        """The account's category lists, shaped like ``XtreamService.fetch_categories``.

        Live categories must load (else ``RequestException``); an unreadable
        list counts as empty, and ``complete`` is then False so the
        fingerprint isn't trusted.
        """
        endpoint = normalize_url(item.base_url.rstrip("/"), "player_api.php")
        auth = {"username": item.username, "password": item.password}
        timeout = (FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT)
        categories: dict[str, list[PlaylistCategory]] = {}
        complete = True
        for tab, action in CATEGORY_ACTIONS.items():
            try:
                response = session.get(endpoint, params={**auth, "action": action}, timeout=timeout)
                response.raise_for_status()
                rows = XtreamService._coerce_row_list(response.json())
            except requests.RequestException:
                if tab == "Live":
                    raise
                complete, rows = False, []
            except ValueError:
                complete, rows = False, []
            categories[tab] = [
                PlaylistCategory(
                    id=str(row.get("category_id", "")),
                    name=str(row.get("category_name", "Unnamed")),
                    media_kind=tab.lower(),
                )
                for row in rows
            ]
        return categories, complete

    def _sample_live_streams(
        self, item: BulkInput, session: requests.Session, categories: list[PlaylistCategory],
    ) -> list[str]:
        """A few live stream ids, taken from the first non-empty categories."""
        endpoint = normalize_url(item.base_url.rstrip("/"), "player_api.php")
        auth = {"username": item.username, "password": item.password}
        timeout = (FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT)

        stream_ids: list[str] = []
        # Category lists are small; the full get_live_streams can be megabytes.
        for category in categories[:3]:
            if len(stream_ids) >= PROBE_CHANNELS:
                break
            params = {**auth, "action": "get_live_streams", "category_id": category.id}
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
        stream_hosts: _HostHealth,
    ) -> requests.Response:
        """GET ``url``, following redirects by hand so dead hosts short-circuit."""
        timeout = (FAST_CONNECT_TIMEOUT, FAST_READ_TIMEOUT)
        for _ in range(PROBE_MAX_REDIRECTS + 1):
            host = urlparse(url).netloc
            if stream_hosts.dead_reason(host):
                raise _StreamFailure(f"Stream server {host} unreachable")
            try:
                response = session.get(url, stream=True, timeout=timeout, allow_redirects=False)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                stream_hosts.failed(host, self._short_error(exc))
                raise _StreamFailure(f"Stream server {host} unreachable") from exc
            except requests.RequestException as exc:
                raise _StreamFailure(self._short_error(exc)) from exc
            stream_hosts.answered(host)
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
        stream_hosts: _HostHealth,
    ) -> None:
        """Raise ``_StreamFailure`` unless ``url`` delivers media data."""
        response = self._open_stream(url, session, stream_hosts)
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
            response = self._open_stream(urljoin(final_url, target), session, stream_hosts)
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
