"""Fast parallel Xtream availability checker for bulk-pasted URLs.

Mirrors the web app's bulk checker: paste many Xtream `get.php` / `player_api.php`
URLs (one per line), check them all in parallel against `get_user_info`, and
report which accounts are reachable and valid. Once a host fails at the
connection level, the remaining lines for that same host are skipped so dead
servers don't slow the whole run down.

This module is intentionally Qt-free so it stays easy to test in isolation.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qs, urlparse

import requests

from zone_new_companion.services.network import normalize_url

# Short timeouts keep the run snappy; slow/dead hosts shouldn't hold everyone up.
FAST_CONNECT_TIMEOUT = 4.0
FAST_READ_TIMEOUT = 8.0

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


@dataclass(slots=True)
class BulkResult:
    """Outcome for one line (valid, invalid, or skipped)."""

    line_number: int
    raw: str
    base_url: str = ""
    username: str = ""
    password: str = ""
    ok: bool = False
    error: str = ""
    status: str = ""
    expiry: str = ""
    max_connections: str = ""
    active_connections: str = ""
    timezone: str = ""
    real_url: str = ""
    # Internal: connection-level failure => the whole host is treated as dead.
    conn_level: bool = field(default=False, repr=False)


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
        progress_cb: Callable[[BulkResult, int, int], None] | None = None,
    ) -> list[BulkResult]:
        """Check every non-empty line and return results in input order.

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

        total = line_number
        done = len(results)

        if not parsed:
            return [results[n] for n in sorted(results)]

        # Submit same-host lines next to each other so that once a host is marked
        # dead, its remaining lines can short-circuit instead of timing out.
        parsed.sort(key=lambda item: urlparse(item.base_url).netloc)

        dead_hosts: dict[str, str] = {}
        lock = threading.Lock()
        session = self._build_session()

        def work(item: BulkInput) -> BulkResult:
            if self._cancel.is_set():
                return BulkResult(line_number=item.line_number, raw=item.raw, error="Cancelled.")

            host = urlparse(item.base_url).netloc
            with lock:
                dead_reason = dead_hosts.get(host)
            if dead_reason:
                return BulkResult(
                    line_number=item.line_number,
                    raw=item.raw,
                    base_url=item.base_url,
                    username=item.username,
                    password=item.password,
                    error=f"Skipped — {host} unreachable ({dead_reason})",
                )

            result = self._check_one(item, session)
            if result.conn_level:
                with lock:
                    dead_hosts.setdefault(host, result.error)
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

                user_info = payload.get("user_info") if isinstance(payload, dict) else None
                if not isinstance(user_info, dict) or not user_info:
                    message = ""
                    if isinstance(payload, dict):
                        message = str(payload.get("message", "")).strip()
                    last_error = message or "Invalid credentials or unsupported server."
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
                )

        return BulkResult(
            line_number=item.line_number,
            raw=item.raw,
            base_url=item.base_url,
            username=item.username,
            password=item.password,
            error=last_error,
        )

    @staticmethod
    def _short_error(exc: Exception) -> str:
        text = str(exc).strip()
        if not text:
            return exc.__class__.__name__
        # Trim noisy requests/urllib3 wrapper text to something readable.
        if len(text) > 140:
            text = text[:137] + "..."
        return text
