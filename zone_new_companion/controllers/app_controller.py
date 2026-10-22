"""Application controller."""

from __future__ import annotations

import hashlib
import logging
import queue
import re
import threading
import time
from dataclasses import replace
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qs, urlparse

from zone_new_companion.services.logger_service import logger_service

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from zone_new_companion.config import AppConfig, ConfigStore
from zone_new_companion.models import Credentials, MediaItem, PlaylistCategory, PortalType
from zone_new_companion.services.base import PortalService
from zone_new_companion.services.bulk_checker import (
    BulkChecker,
    BulkResult,
    CatalogSighting,
    account_key,
    carry_stream_history,
    credentials_for,
    friendly_error,
    mark_server_unavailable,
    now_stamp,
    result_from_credentials,
)
from zone_new_companion.services.catalog_indexer import (
    INDEXED_PORTALS,
    catalog_fingerprint,
    categories_from_rows,
    category_rows,
    index_account,
    index_m3u_account,
    server_of,
    xtream_credentials,
)
from zone_new_companion.services.catalog_store import CatalogAccount, CatalogStore, SearchFilters, SearchHit, SearchResult
from zone_new_companion.services.country_filter import codes_for_language, filter_items
from zone_new_companion.services.filter_debug import FilterDebugDumper, debug_enabled
from zone_new_companion.services.hls_probe import find_hls_url
from zone_new_companion.services.m3u_service import M3UService
from zone_new_companion.services.player_launcher import launch_stream
from zone_new_companion.services.stalker_service import StalkerService
from zone_new_companion.services.stream_verifier import StreamVerifier
from zone_new_companion.services.xtream_service import XtreamService, stream_url_for
from zone_new_companion.state import StateStore
from zone_new_companion.workers.task_worker import BulkCheckSignals, TaskWorker, safe_emit

LOGGER = logging.getLogger(__name__)

# How often the open app looks for outdated or failed catalogs.
INDEX_CHECK_INTERVAL_MS = 3600 * 1000


class AppController:
    """Coordinate UI events, state, and services."""

    def __init__(
        self,
        state_store: StateStore,
        config_store: ConfigStore,
        catalog: CatalogStore | None = None,
    ) -> None:
        self._state_store = state_store
        self._config_store = config_store
        self._config: AppConfig = config_store.load()
        self._thread_pool = QThreadPool.globalInstance()
        self._services: dict[PortalType, PortalService] = {
            PortalType.XTREAM: XtreamService(),
            PortalType.M3U: M3UService(),
            PortalType.STALKER: StalkerService(),
        }
        self._verifier = StreamVerifier()
        self._navigation_stack: dict[str, list[list[MediaItem]]] = {"Live": [], "Movies": [], "Series": []}
        self._bulk_checker: BulkChecker | None = None
        # Background loads shown in the status bar, keyed by a running token.
        self._activities: dict[int, str] = {}
        self._next_activity = 0
        # Per tab: bumped by every new load so a slower, older one is dropped
        # when it finally returns; plus what is loading and its activity.
        self._load_generation: dict[str, int] = {"Live": 0, "Movies": 0, "Series": 0}
        self._loading_key: dict[str, str | None] = {"Live": None, "Movies": None, "Series": None}
        self._loading_activity: dict[str, int | None] = {"Live": None, "Movies": None, "Series": None}
        self._resolving: set[str] = set()
        # Opens a resolved stream in the built-in player; None means VLC plays everything.
        self._builtin_player: Callable[[str, MediaItem], None] | None = None
        # DEBUG=1 / --debug: write loaded lists and filter verdicts to disk.
        self._debug = FilterDebugDumper() if debug_enabled() else None
        # Cross-account search: catalogs download one at a time on a daemon
        # thread (gentle on providers, never blocks browsing or closing the
        # app), with a separate service so its session stays off the UI's.
        self._catalog = catalog
        self._index_service = XtreamService()
        self._index_m3u = M3UService()
        # (account key, credentials, fresh_after): a catalog stored after
        # fresh_after (epoch) is reused, an older one downloaded again.
        self._index_queue: queue.Queue[tuple[str, Credentials, float] | None] = queue.Queue()
        self._index_stop = threading.Event()
        self._index_thread: threading.Thread | None = None
        # Created here, in the UI thread, so its signals arrive there.
        self._index_signals = _IndexSignals()
        self._index_signals.started.connect(self._on_index_started)
        self._index_signals.done.connect(self._on_index_done)
        self._indexing: set[str] = set()  # queued or running
        # Keys whose catalog is still wanted; a queued or running download of
        # any other (removed, beyond the limit) is dropped. Replaced, never mutated.
        self._index_wanted: frozenset[str] = frozenset()
        self._index_running: str | None = None
        # Categories a check saw for accounts not in the catalog yet (just
        # added): stored once the account is, and opening it uses them at once.
        self._sighted: dict[str, tuple[str, str, str, list[tuple[str, str, str]]]] = {}
        self._sighted_lock = threading.Lock()
        self._index_errors: dict[str, str] = {}
        self._catalog_listener: Callable[[str, dict], None] | None = None
        # Catalogs go stale while the app stays open: look for outdated ones
        # (and retry failed ones) every hour. Started by the first sync_catalog.
        self._index_timer: QTimer | None = None
        self._search_generation = 0
        self._state_store.update(language=self._config.ui.language, language_strict=self._config.ui.language_strict)

    @property
    def config(self) -> AppConfig:
        return self._config

    def history_entries(self) -> list[Credentials]:
        """Return successful credential history."""
        return list(self._config.successful_history)

    def _service_for(self, profile: Credentials) -> PortalService:
        """Get the appropriate service for the given profile."""
        try:
            service = self._services.get(profile.portal_type)
            if service is None:
                raise ValueError(f"Unsupported portal type: {profile.portal_type}")
            return service
        except Exception as e:
            LOGGER.error(f"Failed to get service for portal type {profile.portal_type}: {e}")
            raise

    def _validate_input(self, credentials: Credentials) -> str | None:
        """Validate input credentials with comprehensive error checking."""
        try:
            if not credentials.base_url:
                return "Base URL is required."
            
            # Validate URL format
            if not credentials.base_url.startswith(('http://', 'https://')):
                return "Base URL must start with http:// or https://"
            
            if re.search(r"\s", credentials.base_url):
                return "Base URL must not contain spaces."
            
            # Portal-specific validation
            if credentials.portal_type == PortalType.XTREAM:
                if not credentials.username or not credentials.password:
                    return "Xtream mode requires username and password."
                if len(credentials.username) < 3:
                    return "Username must be at least 3 characters long."
                    
            elif credentials.portal_type == PortalType.M3U:
                if not credentials.base_url:
                    return "M3U mode requires a valid playlist URL."
                # Additional M3U validation
                if not any(ext in credentials.base_url.lower() for ext in ['.m3u', '.m3u8', 'get.php']):
                    LOGGER.warning("M3U URL doesn't contain typical M3U indicators")
                    
            elif credentials.portal_type == PortalType.STALKER:
                if not credentials.mac_address:
                    return "Stalker mode requires MAC address."
                if not re.match(
                    r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$",
                    credentials.mac_address,
                ):
                    return "MAC address must match format 00:11:22:33:44:55."
            
            return None
            
        except Exception as e:
            LOGGER.error(f"Validation error: {e}")
            return f"Validation failed: {e}"

    def _normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Normalize user input, including Xtream get.php links."""
        base = credentials.base_url.strip()
        if credentials.portal_type != PortalType.XTREAM:
            return credentials

        parsed = urlparse(base)
        if parsed.path.endswith("/get.php"):
            params = parse_qs(parsed.query)
            username = (params.get("username") or [credentials.username])[0]
            password = (params.get("password") or [credentials.password])[0]
            host = parsed.hostname or ""
            scheme = parsed.scheme or "http"
            port = f":{parsed.port}" if parsed.port else ""
            base = f"{scheme}://{host}{port}"
            return Credentials(
                name=credentials.name,
                base_url=base,
                portal_type=credentials.portal_type,
                username=username,
                password=password,
                mac_address=credentials.mac_address,
                saved_at=credentials.saved_at,
            )
        return credentials

    def connect(self, credentials: Credentials, on_success: Callable[[str], None], on_error: Callable[[str], None]) -> None:
        """Load categories asynchronously and persist profile."""
        try:
            logger_service.info(f"Starting connection to {credentials.base_url}")
            logger_service.debug(f"Portal type: {credentials.portal_type}")
            
            credentials = self._normalize_credentials(credentials)
            self._config.last_input = credentials
            self._config_store.save(self._config)
            validation_error = self._validate_input(credentials)
            if validation_error:
                on_error(validation_error)
                return

            service = self._service_for(credentials)
            
            logger_service.info(f"Using service: {service.__class__.__name__}")
            
            # Switching profiles must fully refresh the bottom section: drop the
            # previous account's categories, loaded items, verification badges,
            # now-playing text and back-stack so nothing carries over.
            self._navigation_stack = {"Live": [], "Movies": [], "Series": []}
            for tab_name in self._load_generation:
                self._supersede_load(tab_name)
            # Categories a Recheck or an earlier visit stored open the account
            # at once; the server is asked in the background whether they changed.
            cached = self._stored_categories(credentials)
            self._state_store.update(
                active_profile=credentials,
                categories=cached or {"Live": [], "Movies": [], "Series": []},
                credential_info=self._stored_connection_info(credentials) if cached else {},
                current_items={"Live": [], "Movies": [], "Series": []},
                verification_results={"Live": {}, "Movies": {}, "Series": {}},
                persistent_verification_results={},
                now_playing={"Live": {}, "Movies": {}, "Series": {}},
                busy=cached is None,
                status_text=f"Connecting to {credentials.base_url}",
            )

            def task() -> tuple[dict[str, list[PlaylistCategory]], dict[str, str]]:
                logger_service.debug("Fetching categories and connection info...")
                categories = service.fetch_categories(credentials)
                info = service.fetch_connection_info(credentials)
                counts = ", ".join(f"{tab}: {len(rows)}" for tab, rows in categories.items())
                logger_service.info(f"Retrieved categories ({counts})")
                self._store_categories(credentials, categories)
                return categories, info

            worker = TaskWorker(task)
            if cached is None:
                worker.signals.succeeded.connect(lambda payload: self._on_connected(credentials, payload, on_success))
                worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
                worker.signals.finished.connect(lambda: self._state_store.update(busy=False))
            else:
                token = self._begin_activity("Checking the categories for changes…")
                worker.signals.succeeded.connect(lambda payload: self._on_refreshed(credentials, payload))
                worker.signals.failed.connect(lambda message: self._on_refresh_failed(credentials, message))
                worker.signals.finished.connect(lambda: self._end_activity(token))
            self._thread_pool.start(worker)

            logger_service.info("Connection task started")
            if cached is not None:
                self._remember_success(credentials)
                self._state_store.update(status_text="Saved categories loaded, checking for changes…")
                on_success(f"Connected ({credentials.portal_type.value})")
        except Exception as exc:
            logger_service.error(f"Connection failed: {exc}")
            on_error(str(exc))

    def _on_connected(
        self,
        credentials: Credentials,
        payload: tuple[dict[str, list[PlaylistCategory]], dict[str, str]],
        on_success: Callable[[str], None],
    ) -> None:
        categories, connection_info = payload
        self._remember_success(credentials)
        if self._debug is not None:
            state = self._state_store.state
            path = self._debug.record_categories(credentials, categories, state.language, state.language_strict)
            LOGGER.info("Debug dump: %s", path)
        self._state_store.update(
            categories=categories,
            credential_info=connection_info,
            status_text="Playlist categories loaded.",
        )
        on_success(f"Connected ({credentials.portal_type.value})")

    def _stored_categories(self, credentials: Credentials) -> dict[str, list[PlaylistCategory]] | None:
        """The account's categories from the search index or the check that added it."""
        if self._catalog is None or credentials.portal_type != PortalType.XTREAM:
            return None
        key = catalog_key_for(credentials)
        try:
            rows = self._catalog.account_categories(key)
        except Exception:  # noqa: BLE001 - the server is asked instead
            LOGGER.exception("Could not read stored categories")
            return None
        if not rows:
            with self._sighted_lock:
                sighted = self._sighted.get(key)
            rows = sighted[3] if sighted else []
        return categories_from_rows(rows) if rows else None

    def _stored_connection_info(self, credentials: Credentials) -> dict[str, str]:
        """Account details from the last check, until the server's own arrive."""
        key = catalog_key_for(credentials)
        account = next((item for item in self._config.working_accounts if catalog_key(item) == key), None)
        if account is None:
            return {}
        return {
            "Status": account.status or "Unknown",
            "Expiry": account.expiry or "Unknown",
            "Active/Max Connections": f"{account.active_connections or '0'}/{account.max_connections or 'Unknown'}",
            "Real URL": account.real_url or account.base_url,
            "Timezone": account.timezone or "Unknown",
        }

    def _store_categories(self, credentials: Credentials, categories: dict[str, list[PlaylistCategory]]) -> None:
        """Keep a working account's fetched categories in the index (worker thread)."""
        if self._catalog is None or credentials.portal_type != PortalType.XTREAM:
            return
        record = (
            catalog_key_for(credentials),
            catalog_fingerprint(credentials.base_url, categories),
            server_of(credentials.base_url),
            category_rows(categories),
        )
        try:
            self._catalog.record_categories([record])
        except Exception:  # noqa: BLE001 - browsing goes on without it
            LOGGER.exception("Could not store the categories")

    def _on_refreshed(
        self,
        credentials: Credentials,
        payload: tuple[dict[str, list[PlaylistCategory]], dict[str, str]],
    ) -> None:
        """The server answered after stored categories were shown: apply changes."""
        if self._state_store.state.active_profile != credentials:
            return  # Another account was opened meanwhile.
        categories, connection_info = payload
        changed = categories != self._state_store.state.categories
        if changed:
            self._state_store.update(categories=categories, status_text="Categories updated from the server.")
        else:
            self._state_store.update(status_text="Categories are up to date.")
        self._state_store.update(credential_info=connection_info)
        if changed:
            # A new catalog: the indexer downloads it if the account is searchable.
            self._schedule_indexing()

    def _on_refresh_failed(self, credentials: Credentials, message: str) -> None:
        if self._state_store.state.active_profile != credentials:
            return
        LOGGER.warning("Could not refresh the categories: %s", message)
        self._state_store.update(status_text=f"Showing saved categories; the server did not answer ({message})")

    def load_items(
        self,
        tab_name: str,
        category: PlaylistCategory,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Load category items in the background; the UI stays usable."""
        profile = self._state_store.state.active_profile
        if profile is None:
            on_error("No active profile.")
            return
        self._load_into_tab(
            tab_name,
            key=f"category:{category.id}",
            label=f"{tab_name}: loading '{category.name}'",
            fetch=lambda: self._fetch_category(profile, tab_name, category),
            title=category.name,
            push_stack=False,
            on_success=on_success,
            on_error=on_error,
        )

    def _fetch_category(self, profile: Credentials, tab_name: str, category: PlaylistCategory) -> list[MediaItem]:
        """Worker-thread fetch of one category; recorded when debugging.

        An indexed XTREAM account lists it from its catalog without asking the
        server; anything the catalog lacks comes from the server.
        """
        items = self._indexed_category(profile, category) or self._service_for(profile).fetch_items(profile, category)
        if self._debug is not None:
            state = self._state_store.state
            self._debug.record_items(profile, tab_name, category, items, state.language, state.language_strict)
        return items

    def _indexed_category(self, profile: Credentials, category: PlaylistCategory) -> list[MediaItem]:
        """The category's streams from the search index, as the server lists them (worker thread)."""
        if self._catalog is None or profile.portal_type != PortalType.XTREAM:
            return []
        try:
            entries = self._catalog.category_entries(catalog_key_for(profile), category.media_kind, category.id)
        except Exception:  # noqa: BLE001 - the server is asked instead
            LOGGER.exception("Could not read the category from the search index")
            return []
        item_type = {"live": "channel", "movies": "vod"}.get(category.media_kind, "series")
        return [
            MediaItem(
                id=entry.provider_id,
                name=entry.name,
                media_kind=category.media_kind,
                item_type=item_type,
                # What the server's rows give the list, the EPG and the language filter.
                metadata={
                    "category_id": entry.category_id,
                    "num": entry.num,
                    "epg_channel_id": entry.epg_id,
                    "stream_icon": entry.icon,
                    "container_extension": entry.extension,
                    "added": entry.added_at,
                },
                stream_url=(
                    stream_url_for(profile, item_type, entry.provider_id, entry.extension)
                    if item_type != "series" else None
                ),
            )
            for entry in entries
        ]

    @property
    def debug_enabled(self) -> bool:
        return self._debug is not None

    def dump_full_catalog(self, on_success: Callable[[str], None], on_error: Callable[[str], None]) -> None:
        """Debug: fetch every category's streams and write them to the dump."""
        profile = self._state_store.state.active_profile
        if self._debug is None or profile is None:
            on_error("Connect to an account first.")
            return
        service = self._service_for(profile)
        categories = self._state_store.state.categories
        state = self._state_store.state
        token = self._begin_activity("Debug: dumping full catalog…")

        def task() -> str:
            for tab_name, tab_categories in categories.items():
                if isinstance(service, XtreamService) and tab_categories:
                    # One request per kind instead of one per category.
                    grouped: dict[str, list[MediaItem]] = {}
                    for item in service.fetch_all_items(profile, tab_categories[0].media_kind):
                        grouped.setdefault(str(item.metadata.get("category_id", "")), []).append(item)
                    fetched = [(category, grouped.get(category.id, [])) for category in tab_categories]
                else:
                    fetched = [(category, service.fetch_items(profile, category)) for category in tab_categories]
                for category, items in fetched:
                    self._debug.record_items(profile, tab_name, category, items, state.language, state.language_strict)
            return str(self._debug.path_for(profile))

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(lambda path: on_success(f"Catalog written to {path}"))
        worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
        worker.signals.finished.connect(lambda: self._end_activity(token))
        self._thread_pool.start(worker)

    def _load_into_tab(
        self,
        tab_name: str,
        *,
        key: str,
        label: str,
        fetch: Callable[[], list[MediaItem]],
        title: str,
        push_stack: bool,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Fetch a list for a tab without blocking anything else.

        Clicking the same entry again while it loads (a double click) is
        ignored; clicking another one replaces the pending load, and the
        older result is dropped when it arrives.
        """
        if self._loading_key[tab_name] == key:
            return
        generation = self._supersede_load(tab_name)
        self._loading_key[tab_name] = key
        self._loading_activity[tab_name] = self._begin_activity(f"{label}…")

        def is_current() -> bool:
            return self._load_generation[tab_name] == generation

        def on_loaded(items: list[MediaItem]) -> None:
            if is_current():
                self._on_items_loaded(tab_name, items, on_success, push_stack=push_stack, title=title)

        def on_failed(message: str) -> None:
            if is_current():
                self._on_error(message, on_error)

        def on_finished() -> None:
            if is_current():
                self._supersede_load(tab_name)

        worker = TaskWorker(fetch)
        worker.signals.succeeded.connect(on_loaded)
        worker.signals.failed.connect(on_failed)
        worker.signals.finished.connect(on_finished)
        self._thread_pool.start(worker)

    def _supersede_load(self, tab_name: str) -> int:
        """Forget the tab's pending load (if any); return the new generation."""
        self._load_generation[tab_name] += 1
        self._loading_key[tab_name] = None
        token = self._loading_activity[tab_name]
        self._loading_activity[tab_name] = None
        if token is not None:
            self._end_activity(token)
        return self._load_generation[tab_name]

    def _begin_activity(self, label: str) -> int:
        self._next_activity += 1
        self._activities[self._next_activity] = label
        self._state_store.update(activity=tuple(self._activities.values()), status_text=label)
        return self._next_activity

    def _end_activity(self, token: int) -> None:
        if self._activities.pop(token, None) is None:
            return
        self._state_store.update(activity=tuple(self._activities.values()))

    def _on_items_loaded(
        self,
        tab_name: str,
        items: list[MediaItem],
        on_success: Callable[[str], None],
        *,
        push_stack: bool,
        title: str,
    ) -> None:
        current = dict(self._state_store.state.current_items)
        if push_stack and current.get(tab_name):
            self._navigation_stack[tab_name].append(list(current[tab_name]))
        current[tab_name] = items
        verification = dict(self._state_store.state.verification_results)
        verification[tab_name] = {}
        now_playing = dict(self._state_store.state.now_playing)
        now_playing[tab_name] = {}
        status = f"{tab_name} · {title}: {len(items)} items"
        state = self._state_store.state
        if state.language:
            shown = len(filter_items(
                items, codes_for_language(state.language), state.categories.get(tab_name, []), state.language_strict,
            ))
            status += f" ({shown} shown for the language filter)"
        self._state_store.update(
            current_items=current,
            verification_results=verification,
            now_playing=now_playing,
            status_text=status,
        )
        on_success(f"{tab_name}: {len(items)} entries")

        # Lightweight "now playing" prefetch for Live tab.
        if tab_name == "Live":
            self._prefetch_now_playing(items[:10])

    def set_builtin_player(self, player: Callable[[str, MediaItem], None] | None) -> None:
        """Play streams in the app's own player instead of external VLC."""
        self._builtin_player = player

    def play(
        self,
        item: MediaItem,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
        prefer_hls: bool = False,
        use_vlc: bool = False,
        profile: Credentials | None = None,
    ) -> None:
        """Resolve the stream and play it in the built-in player, or in VLC.

        VLC plays when use_vlc is set or the built-in player is unavailable.
        With prefer_hls the stream's HLS (.m3u8) variant plays instead; it is an
        error when the server offers none. ``profile`` defaults to the account
        being browsed (search hits bring their own).
        """
        profile = profile or self._state_store.state.active_profile
        if profile is None:
            on_error("No active profile.")
            return
        # A double click (or a click on ▶ right after) asks again while the
        # first request still resolves; one player window is enough.
        if item.id in self._resolving:
            return
        self._resolving.add(item.id)
        token = self._begin_activity(f"Starting {item.name}…")
        builtin = None if use_vlc else self._builtin_player

        def task() -> str:
            stream_url = self._service_for(profile).resolve_stream_url(profile, item)
            if prefer_hls:
                hls_url = find_hls_url(stream_url)
                if hls_url is None:
                    raise ValueError(f"No HLS (m3u8) stream for {item.name}.")
                stream_url = hls_url
            if builtin is None:
                launch_stream(stream_url)
            return stream_url

        def on_played(url: str) -> None:
            if builtin is not None:
                builtin(url, item)
            label = f"Playing {item.name}" + (" (HLS)" if prefer_hls else "") + (" in VLC" if builtin is None else "")
            self._state_store.update(status_text=label)
            on_success(label)

        def on_finished() -> None:
            self._resolving.discard(item.id)
            self._end_activity(token)

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(on_played)
        worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
        worker.signals.finished.connect(on_finished)
        self._thread_pool.start(worker)

    def probe_hls(
        self,
        item: MediaItem,
        on_result: Callable[[str | None], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Look for an HLS (.m3u8) variant of the item's stream; None if the server has none."""
        profile = self._state_store.state.active_profile
        if profile is None:
            on_error("No active profile.")
            return
        if item.item_type not in {"channel", "vod", "episode"}:
            on_error("Item is not a playable stream.")
            return
        token = self._begin_activity(f"Checking HLS for {item.name}…")

        def task() -> str | None:
            stream_url = self._service_for(profile).resolve_stream_url(profile, item)
            return find_hls_url(stream_url)

        def on_finished() -> None:
            self._end_activity(token)

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(on_result)
        worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
        worker.signals.finished.connect(on_finished)
        self._thread_pool.start(worker)

    def activate_item(
        self,
        tab_name: str,
        item: MediaItem,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Handle navigation or playback by item type."""
        if item.item_type in {"series", "season"}:
            self._expand_series_item(tab_name, item, on_success, on_error)
            return
        self.play(item, on_success=on_success, on_error=on_error)

    def _expand_series_item(
        self,
        tab_name: str,
        item: MediaItem,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        profile = self._state_store.state.active_profile
        if profile is None:
            on_error("No active profile.")
            return
        self._load_into_tab(
            tab_name,
            key=f"{item.item_type}:{item.id}",
            label=f"{tab_name}: opening '{item.name}'",
            fetch=lambda: self._service_for(profile).fetch_series_children(profile, item),
            title=item.name,
            push_stack=True,
            on_success=on_success,
            on_error=on_error,
        )

    def go_back(self, tab_name: str) -> bool:
        """Pop last media view for a tab."""
        if not self._navigation_stack[tab_name]:
            return False
        self._supersede_load(tab_name)
        previous = self._navigation_stack[tab_name].pop()
        current = dict(self._state_store.state.current_items)
        current[tab_name] = previous
        verification = dict(self._state_store.state.verification_results)
        verification[tab_name] = {}
        self._state_store.update(
            current_items=current,
            verification_results=verification,
            status_text="Returned to previous list.",
        )
        return True

    def verify_tab_streams(
        self,
        tab_name: str,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Verify current tab streams for reachability and A/V."""
        profile = self._state_store.state.active_profile
        if profile is None:
            on_error("No active profile.")
            return
        tab_items = self._visible_items(tab_name)
        if not tab_items:
            on_error("No loaded items to verify.")
            return
        if self._state_store.state.busy:
            on_error("Another operation is in progress.")
            return
        self._state_store.update(busy=True, status_text=f"Verifying {len(tab_items)} streams...")

        def task() -> dict[str, str]:
            service = self._service_for(profile)
            
            # Real-time verification callback
            def on_verification_update(item_id: str, status: str, verified_count: int, total_count: int):
                # Update verification results in real-time
                verification = dict(self._state_store.state.verification_results)
                # Copy the tab's map too: the table compares by identity to repaint.
                verification[tab_name] = {**verification.get(tab_name, {}), item_id: status}
                
                # Update status text with progress
                progress_percent = (verified_count / total_count) * 100
                status_text = f"Verifying {verified_count}/{total_count} ({progress_percent:.1f}%)"
                
                self._state_store.update(
                    verification_results=verification,
                    status_text=status_text
                )
            
            return self._verifier.verify_items(service, profile, tab_items, progress_callback=on_verification_update)

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(
            lambda results: self._on_verify_done(tab_name, results, on_success),
        )
        worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
        worker.signals.finished.connect(lambda: self._state_store.update(busy=False))
        self._thread_pool.start(worker)

    def verify_single_item(
        self,
        tab_name: str,
        item: MediaItem,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        profile = self._state_store.state.active_profile
        if profile is None:
            on_error("No active profile.")
            return
        if item.item_type not in {"channel", "vod", "episode"}:
            on_error("Item is not a playable stream.")
            return

        self._state_store.update(
            status_text=f"Priority verifying {item.name}...",
            priority_verification_active=True
        )

        def task() -> dict[str, str]:
            service = self._service_for(profile)
            # Increase threads for priority verification
            original_max = self._thread_pool.maxThreadCount()
            self._thread_pool.setMaxThreadCount(original_max + 4)
            try:
                result = self._verifier.verify_item(service, profile, item)
                return {item.id: result.status}
            finally:
                self._thread_pool.setMaxThreadCount(original_max)

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(lambda result_map: self._on_verify_done(tab_name, result_map, on_success))
        worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
        self._thread_pool.start(worker)

    def verify_all_channels(
        self,
        tab_name: str,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Verify all channels in the current tab efficiently."""
        profile = self._state_store.state.active_profile
        if profile is None:
            on_error("No active profile.")
            return

        items = self._visible_items(tab_name)
        if not items:
            on_error("No items to verify in this tab.")
            return

        self._state_store.update(status_text=f"Verifying all {len(items)} channels in {tab_name}...")

        def task() -> dict[str, str]:
            service = self._service_for(profile)
            return self._verifier.verify_items(service, profile, items)

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(lambda result_map: self._on_verify_done(tab_name, result_map, on_success))
        worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
        self._thread_pool.start(worker)

    def verify_tab(
        self,
        tab_name: str,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Verify all channels in a specific tab from menu."""
        self.verify_all_channels(tab_name, on_success, on_error)

    def cancel_verification(
        self,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Cancel all ongoing verification processes."""
        try:
            logger_service.info("Cancelling all verification processes")
            # Clear verification queue and stop background verification
            self._state_store.update(
                background_verification_active=False,
                verification_queue=[],
                priority_verification_active=False,
                busy=False
            )
            on_success("All verification processes cancelled.")
            logger_service.info("Verification processes cancelled successfully")
        except Exception as e:
            logger_service.error(f"Failed to cancel verification: {e}")
            on_error(f"Failed to cancel verification: {e}")

    # All logs methods removed for performance

    def request_now_playing(
        self,
        tab_name: str,
        item: MediaItem,
    ) -> None:
        profile = self._state_store.state.active_profile
        if profile is None:
            return
        if item.item_type != "channel":
            return
        cached = self._state_store.state.now_playing.get(tab_name, {}).get(item.id, "")
        if cached:
            return

        def task() -> tuple[str, str]:
            title = self._service_for(profile).fetch_now_playing(profile, item)
            return item.id, title

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(lambda pair: self._on_now_playing(tab_name, pair))
        self._thread_pool.start(worker)

    def _on_now_playing(self, tab_name: str, payload: tuple[str, str]) -> None:
        item_id, title = payload
        if not title:
            return
        now_playing = dict(self._state_store.state.now_playing)
        tab_map = dict(now_playing.get(tab_name, {}))
        tab_map[item_id] = title
        now_playing[tab_name] = tab_map
        self._state_store.update(now_playing=now_playing)

    def _prefetch_now_playing(self, items: list[MediaItem]) -> None:
        for item in items:
            if item.item_type == "channel":
                self.request_now_playing("Live", item)

    def _on_verify_done(
        self,
        tab_name: str,
        results: dict[str, str],
        on_success: Callable[[str], None],
    ) -> None:
        # Update current tab verification results
        verification = dict(self._state_store.state.verification_results)
        verification[tab_name] = results
        
        # Update persistent verification results (survives category changes)
        persistent_results = dict(self._state_store.state.persistent_verification_results)
        persistent_results.update(results)
        
        ok_count = sum(1 for status in results.values() if status.startswith("OK"))
        self._state_store.update(
            verification_results=verification,
            persistent_verification_results=persistent_results,
            status_text=f"Verification done: {ok_count}/{len(results)} reachable.",
        )
        on_success(f"Verification done: {ok_count}/{len(results)}")

    def start_bulk_check(self, raw_text: str) -> BulkCheckSignals:
        """Run a parallel availability check over pasted Xtream URLs.

        Returns a ``BulkCheckSignals`` the caller connects to for live progress
        and the final result list. The run is cancellable via
        ``cancel_bulk_check``.
        """
        lines = raw_text.splitlines()
        return self._run_check(lambda checker, progress: checker.check_many(lines, progress_cb=progress))

    def _run_check(
        self,
        check: Callable[[BulkChecker, Callable[[BulkResult, int, int], None]], list[BulkResult]],
    ) -> BulkCheckSignals:
        """Run ``check(checker, progress)`` in the pool, reporting through signals.

        The category lists seen while checking go into the search index
        before the results are reported.
        """
        signals = BulkCheckSignals()
        checker = BulkChecker(known_live_ids=self._catalog.live_ids if self._catalog is not None else None)
        self._bulk_checker = checker

        def emit_progress(result: BulkResult, done: int, total: int) -> None:
            safe_emit(signals.progress, result, done, total)

        def task() -> list[BulkResult]:
            results = check(checker, emit_progress)
            self._record_sightings(checker.catalogs)
            return results

        class _BulkRunnable(QRunnable):
            def run(self_inner) -> None:  # noqa: N805
                try:
                    results = task()
                except Exception as exc:  # noqa: BLE001
                    safe_emit(signals.failed, str(exc))
                else:
                    safe_emit(signals.succeeded, results)
                safe_emit(signals.finished)

        self._thread_pool.start(_BulkRunnable())
        return signals

    def _record_sightings(self, sightings: list[CatalogSighting]) -> None:
        """Store checked accounts' categories as a partial index (worker thread).

        Accounts not in the catalog yet are kept for ``sync_catalog``, which
        stores them once the account is added.
        """
        if self._catalog is None or not sightings:
            return
        records = [
            (
                catalog_key(sighting.account),
                sighting.fingerprint,
                server_of(sighting.account.base_url),
                category_rows(sighting.categories),
            )
            for sighting in sightings
        ]
        try:
            skipped = set(self._catalog.record_categories(records))
        except Exception:  # noqa: BLE001 - the check itself succeeded
            LOGGER.exception("Could not store the checked accounts' categories")
            return
        with self._sighted_lock:
            self._sighted = {record[0]: record for record in records if record[0] in skipped}

    def cancel_bulk_check(self) -> None:
        """Stop an in-flight bulk check."""
        if self._bulk_checker is not None:
            self._bulk_checker.cancel()

    def working_accounts(self) -> list[BulkResult]:
        """Working accounts saved from the last bulk check, starred first."""
        return list(self._config.working_accounts)

    def save_working_accounts(self, results: list[BulkResult]) -> None:
        """Persist the working accounts so they survive a restart."""
        # Stable sort: starred accounts first, then streaming (green), not yet
        # checked (grey), not streaming (red); order otherwise kept.
        stream_rank = {True: 0, None: 1, False: 2}
        self._config.working_accounts = sorted(
            results,
            key=lambda account: (not account.starred, stream_rank[account.stream_ok]),
        )
        self._config_store.save(self._config)
        self.sync_catalog()

    def merge_working_accounts(self, results: list[BulkResult]) -> list[BulkResult]:
        """Add a new bulk check's accounts to the saved ones.

        Accounts found again are replaced by their fresh result and keep their
        star; saved accounts the check did not include stay in the list (only
        a recheck removes accounts that stopped working).
        """
        previous = {account_key(account): account for account in self._config.working_accounts}
        merged: list[BulkResult] = []
        for result in results:
            earlier = previous.pop(account_key(result), None)
            carry_stream_history(result, earlier)
            if earlier is not None:
                result.starred = earlier.starred
                result.always_indexed = earlier.always_indexed
            merged.append(result)
        self.save_working_accounts(merged + list(previous.values()))
        return self.working_accounts()

    def toggle_working_account_star(self, result: BulkResult) -> list[BulkResult]:
        """Star or unstar one working account."""
        key = account_key(result)
        for account in self._config.working_accounts:
            if account_key(account) == key:
                account.starred = not account.starred
        self.save_working_accounts(self._config.working_accounts)
        return self.working_accounts()

    def recheck_working_accounts(self) -> BulkCheckSignals:
        """Re-run the availability check over the saved accounts of every type."""
        accounts = list(self._config.working_accounts)
        return self._run_check(lambda checker, progress: checker.check_accounts(accounts, progress_cb=progress))

    def check_new_account(self, credentials: Credentials) -> BulkCheckSignals | str:
        """Check one account typed into the import bar; an error text if the input is invalid."""
        credentials = self._normalize_credentials(credentials)
        self._config.last_input = credentials
        self._config_store.save(self._config)
        validation_error = self._validate_input(credentials)
        if validation_error:
            return validation_error
        account = result_from_credentials(credentials)
        return self._run_check(lambda checker, progress: checker.check_accounts([account], progress_cb=progress))

    def add_working_account(self, result: BulkResult) -> list[BulkResult]:
        """Put one checked account into the list, replacing an earlier copy of it."""
        key = account_key(result)
        kept: list[BulkResult] = []
        for account in self._config.working_accounts:
            if account_key(account) == key:
                carry_stream_history(result, account)
                result.starred = account.starred
                result.always_indexed = account.always_indexed
            else:
                kept.append(account)
        self.save_working_accounts([result, *kept])
        return self.working_accounts()

    def apply_recheck(self, results: list[BulkResult]) -> list[BulkResult]:
        """Refresh accounts from a recheck; return the ones the server rejected.

        Only a rejection by the server (wrong credentials, expired, banned) is
        reported for removal. An unreachable or overloaded server may come
        back, so those accounts stay and just turn red ("down since").
        Accounts the recheck did not cover (cancelled, or added meanwhile) are
        left untouched.
        """
        fresh = {account_key(result): result for result in results if result.error != "Cancelled."}
        rejected: list[BulkResult] = []
        for account in self._config.working_accounts:
            result = fresh.get(account_key(account))
            if result is None:
                continue
            if not result.ok:
                # No login means no streaming either.
                account.error = result.error
                if result.rejected:
                    result.stream_ok = False
                    result.stream_checked_at = now_stamp()
                    result.stream_status = f"Login rejected: {friendly_error(result.error)}"
                    rejected.append(account)
                elif result.stream_ok is None:
                    mark_server_unavailable(result)
                carry_stream_history(result, account)
                account.stream_ok = result.stream_ok
                account.stream_status = result.stream_status
                account.stream_checked_at = result.stream_checked_at
                account.stream_down_since = result.stream_down_since
                continue
            if result.stream_ok is not None:
                carry_stream_history(result, account)
                account.stream_ok = result.stream_ok
                account.stream_status = result.stream_status
                account.stream_checked_at = result.stream_checked_at
                account.stream_down_since = result.stream_down_since
            account.error = ""
            account.status = result.status
            account.expiry = result.expiry
            account.max_connections = result.max_connections
            account.active_connections = result.active_connections
            account.timezone = result.timezone
            account.real_url = result.real_url
        self.save_working_accounts(self._config.working_accounts)
        return rejected

    def remove_working_accounts(self, accounts: list[BulkResult]) -> list[BulkResult]:
        """Drop the given accounts from the saved list."""
        drop = {account_key(account) for account in accounts}
        self.save_working_accounts(
            [account for account in self._config.working_accounts if account_key(account) not in drop],
        )
        return self.working_accounts()

    def connect_from_bulk(
        self,
        result: BulkResult,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Load a working account (any type) as the active profile and browse it.

        Once it opens it joins the search index, whatever the limit: the user
        chose it, so its catalog is worth downloading.
        """

        def connected(message: str) -> None:
            if self._keep_indexed([result]):
                # Makes it searchable in the catalog too, then downloads it.
                self.sync_catalog()
            on_success(message)

        self.connect(credentials_for(result), on_success=connected, on_error=on_error)

    # Cross-account catalog search ------------------------------------------------

    def set_catalog_listener(self, listener: Callable[[str, dict], None]) -> None:
        """Receive the search status line and each working account's index state.

        The states map ``account_key(account)`` to (text, tooltip, color token).
        """
        self._catalog_listener = listener

    @property
    def index_limit(self) -> int:
        return self._config.ui.search_index_limit

    def set_index_limit(self, limit: int) -> None:
        """Index at most ``limit`` working accounts (0 = all) and apply it now."""
        self._config.ui.search_index_limit = max(0, limit)
        self._config_store.save(self._config)
        self.sync_catalog()

    @property
    def index_max_age_hours(self) -> int:
        return self._config.ui.search_index_max_age_hours

    def set_index_max_age_hours(self, hours: int) -> None:
        """Download catalogs again once they are older than ``hours``."""
        self._config.ui.search_index_max_age_hours = max(1, hours)
        self._config_store.save(self._config)
        self._schedule_indexing()

    def index_size(self) -> int:
        """Bytes catalog.db takes on disk."""
        return self._catalog.size_bytes() if self._catalog is not None else 0

    def clear_index(self, on_success: Callable[[str], None], on_error: Callable[[str], None]) -> None:
        """Delete every downloaded catalog, then index the accounts again."""
        if self._catalog is None:
            return
        catalog = self._catalog
        token = self._begin_activity("Clearing the search index…")
        worker = TaskWorker(catalog.clear)

        def cleared(_result: object) -> None:
            self._index_errors.clear()
            on_success("Search index cleared, downloading again…")
            self._schedule_indexing()

        worker.signals.succeeded.connect(cleared)
        worker.signals.failed.connect(lambda message: on_error(f"Could not clear the search index: {message}"))
        worker.signals.finished.connect(lambda: self._end_activity(token))
        self._thread_pool.start(worker)

    def _indexable(self, info: dict[str, tuple[float, int]] | None = None) -> list[BulkResult]:
        """The accounts whose catalog is kept: ``limit`` of the up and indexable ones.

        Starred accounts come first, then the ones indexed or being indexed,
        then the list order: a new or re-sorted account fills a free slot but
        never pushes a catalog out, while starring one brings it in.
        ``info`` is ``CatalogStore.index_info()`` when the caller has it.
        """
        candidates = [
            account for account in self._config.working_accounts
            if account.portal_type in INDEXED_PORTALS and account.stream_ok is not False
        ]
        # Accounts the user opened or added are indexed on top of the limit.
        chosen = [account for account in candidates if account.always_indexed]
        candidates = [account for account in candidates if not account.always_indexed]
        if not self.index_limit or len(candidates) <= self.index_limit:
            return chosen + candidates
        if info is None:
            info = self._catalog.index_info() if self._catalog is not None else {}

        def has_slot(account: BulkResult) -> bool:
            key = catalog_key(account)
            return key in info or key in self._indexing

        # Stable: the list order breaks ties.
        ranked = sorted(candidates, key=lambda account: (not account.starred, not has_slot(account)))
        return chosen + ranked[: self.index_limit]

    def _keep_indexed(self, selected: list[BulkResult]) -> bool:
        """Index the selected accounts whatever the limit, also after a restart.

        Returns whether any was new to it (the config is saved then).
        """
        keys = {account_key(account) for account in selected if account.portal_type in INDEXED_PORTALS}
        changed = False
        for account in self._config.working_accounts:
            if account_key(account) in keys and not account.always_indexed:
                account.always_indexed = True
                changed = True
        if changed:
            self._config_store.save(self._config)
        return changed

    def sync_catalog(self) -> None:
        """Mirror the working accounts into the catalog, then index what is missing.

        Accounts that are down (red) stay but drop out of search results;
        removed accounts, and accounts beyond the index limit, lose their data
        (shared catalogs live on while any account still uses them).
        """
        if self._catalog is None:
            return
        if self._index_timer is None:
            self._index_timer = QTimer()
            self._index_timer.setInterval(INDEX_CHECK_INTERVAL_MS)
            self._index_timer.timeout.connect(self._schedule_indexing)
            self._index_timer.start()
        catalog = self._catalog
        accounts = [
            CatalogAccount(
                key=catalog_key(account),
                portal_type=account.portal_type,
                label=f"{server_of(account.base_url)} · {account.username or account.mac_address or 'playlist'}",
                alive=account.stream_ok is not False,
                starred=account.starred,
                stream_ok=account.stream_ok,
                expires_at=_expiry_timestamp(account.expiry),
                max_conn=_int_or_none(account.max_connections),
                active_conn=_int_or_none(account.active_connections),
            )
            for account in self._config.working_accounts
        ]
        indexable = {catalog_key(account) for account in self._indexable()}
        self._index_wanted = frozenset(indexable)
        # Down accounts keep their catalog for when they come back.
        unindexed = [
            item.key for item in accounts
            if item.alive and item.portal_type in INDEXED_PORTALS and item.key not in indexable
        ]
        keys = {item.key for item in accounts}
        with self._sighted_lock:
            sighted = [self._sighted.pop(key) for key in list(self._sighted) if key in keys]

        def task() -> None:
            catalog.sync_accounts(accounts, unindexed)
            if sighted:
                # Now that the accounts are in, their check's categories can go in too.
                try:
                    catalog.record_categories(sighted)
                except Exception:  # noqa: BLE001 - opening them asks the server instead
                    LOGGER.exception("Could not store the added accounts' categories")

        worker = TaskWorker(task)
        worker.signals.succeeded.connect(lambda _result: self._schedule_indexing())
        worker.signals.failed.connect(lambda message: LOGGER.error("Catalog sync failed: %s", message))
        self._thread_pool.start(worker)

    def _schedule_indexing(self) -> None:
        if self._catalog is None or self._index_stop.is_set():
            return
        max_age = self.index_max_age_hours * 3600
        accounts = {catalog_key(account): account for account in self._indexable()}
        self._enqueue(accounts, self._catalog.needing_index(list(accounts), max_age), time.time() - max_age)

    def refresh_index(self, selected: list[BulkResult] | None = None) -> str:
        """Download the selected accounts' catalogs now, or every indexed one's again (None).

        Selected accounts join the index whatever the limit. Accounts sharing
        a catalog download it once: whatever a refresh stored after the
        request counts as fresh. Returns a line for the user.
        """
        if self._catalog is None:
            return "Search is not available."
        if selected is not None and self._keep_indexed(selected):
            # Makes them searchable in the catalog; the download starts below.
            self.sync_catalog()
        accounts = {catalog_key(item): item for item in self._indexable()}
        skipped: list[str] = []
        if selected is not None:
            keys = []
            for account in selected:
                key = catalog_key(account)
                if account.portal_type not in INDEXED_PORTALS:
                    skipped.append(f"{account.portal_type.upper()} accounts are not searchable yet.")
                elif key not in accounts:
                    # Kept for when a Recheck finds it working again.
                    skipped.append("This account is down; it is indexed once a Recheck finds it working.")
                elif key not in keys:
                    keys.append(key)
            if not keys and skipped:
                if len(selected) == 1:
                    return skipped[0]
                return "None of the selected accounts can be indexed now (down or not searchable)."
        else:
            keys = list(accounts)
        if not keys:
            return "No accounts to index."
        added = self._enqueue(accounts, keys, time.time())
        if not added:
            return "Already indexing."
        note = f" ({len(skipped)} skipped: down or not searchable)" if skipped else ""
        return f"Indexing {added} account{'s' if added != 1 else ''}…{note}"

    def _enqueue(self, accounts: dict[str, BulkResult], keys: list[str], fresh_after: float) -> int:
        """Queue ``keys`` not queued yet and start the indexer; how many were added.

        ``accounts`` are all indexable ones: the indexer drops any other job.
        """
        self._index_wanted = frozenset(accounts)
        added = 0
        for key in keys:
            if key not in self._indexing:
                self._indexing.add(key)
                self._index_queue.put((key, credentials_for(accounts[key]), fresh_after))
                added += 1
        if self._indexing and self._index_thread is None:
            self._index_thread = threading.Thread(target=self._index_loop, name="catalog-indexer", daemon=True)
            self._index_thread.start()
        self._report_catalog_status()
        return added

    def _index_loop(self) -> None:
        """Indexer thread: one queued account after the other until shutdown."""
        catalog, service, m3u, stop = self._catalog, self._index_service, self._index_m3u, self._index_stop
        while not stop.is_set():
            job = self._index_queue.get()
            if job is None or stop.is_set():
                return
            key, credentials, fresh_after = job
            if key not in self._index_wanted:
                # Removed or beyond the limit since it was queued.
                safe_emit(self._index_signals.done, key, "dropped, no longer indexed", "")
                continue

            def cancelled(key: str = key) -> bool:
                return stop.is_set() or key not in self._index_wanted

            safe_emit(self._index_signals.started, key)
            try:
                max_age = time.time() - fresh_after
                if credentials.portal_type == PortalType.M3U:
                    summary = index_m3u_account(
                        catalog, m3u, service, credentials, key, max_age_seconds=max_age, cancelled=cancelled,
                    )
                else:
                    summary = index_account(
                        catalog, service, credentials, key, max_age_seconds=max_age, cancelled=cancelled,
                    )
            except Exception as exc:  # noqa: BLE001 - reported per account, the loop goes on
                if stop.is_set():
                    return
                safe_emit(self._index_signals.done, key, "", str(exc))
            else:
                safe_emit(self._index_signals.done, key, summary, "")

    def _on_index_started(self, key: str) -> None:
        self._index_running = key
        self._report_catalog_status()

    def _on_index_done(self, key: str, summary: str, error: str) -> None:
        self._indexing.discard(key)
        self._index_running = None
        if error:
            LOGGER.warning("Indexing failed: %s", error)
            self._index_errors[key] = error
        else:
            LOGGER.info("Catalog: %s", summary)
            self._index_errors.pop(key, None)
        self._report_catalog_status()

    def shutdown(self) -> None:
        """Stop indexing and wait briefly for background work; call when the app quits.

        A download in progress is abandoned (the thread is a daemon), nothing
        half-done is stored.
        """
        self._index_stop.set()
        if self._index_timer is not None:
            self._index_timer.stop()
        self.cancel_bulk_check()
        while True:
            try:
                self._index_queue.get_nowait()
            except queue.Empty:
                break
        self._index_queue.put(None)
        if self._index_thread is not None:
            self._index_thread.join(timeout=2)
        self._thread_pool.clear()
        self._thread_pool.waitForDone(3000)

    def _report_catalog_status(self) -> None:
        """Push the status line and per-account states (UI thread only)."""
        if self._catalog is None or self._catalog_listener is None:
            return
        info = self._catalog.index_info()
        indexable = {catalog_key(account) for account in self._indexable(info)}
        states = {
            account_key(account): self._index_state(account, catalog_key(account), indexable, info)
            for account in self._config.working_accounts
        }
        searchable = sum(
            1 for account in self._config.working_accounts
            if account.stream_ok is not False and catalog_key(account) in info
        )
        limit = f"limit {self.index_limit}" if self.index_limit else "no limit"
        chosen = sum(1 for account in self._indexable(info) if account.always_indexed)
        if self.index_limit and chosen:
            limit += f" + {chosen} chosen"
        text = f"{searchable} account{'s' if searchable != 1 else ''} searchable ({limit})"
        streams = sum(count for _indexed_at, count in {info[key] for key in indexable if key in info})
        if streams:
            text += f" · {streams:,} streams"
        if self._indexing:
            text += f" · indexing {len(self._indexing)} account{'s' if len(self._indexing) != 1 else ''}…"
        self._catalog_listener(text, states)

    def _index_state(
        self,
        account: BulkResult,
        key: str,
        indexable: set[str],
        info: dict[str, tuple[float, int]],
    ) -> tuple[str, str, str]:
        """(text, tooltip, color token) for the account's search column."""
        if account.portal_type not in INDEXED_PORTALS:
            return "—", f"{account.portal_type.upper()} accounts are not searchable yet", "text_disabled"
        if account.stream_ok is False:
            return "Paused", "Down right now: left out of search until a Recheck finds it working", "text_muted"
        if key == self._index_running:
            return "Indexing…", "Downloading this account's catalog", "accent"
        if key in self._indexing:
            return "Queued", "Waiting for the accounts ahead of it", "text_muted"
        if key in info:
            indexed_at, count = info[key]
            tip = f"{count:,} streams, updated {_ago(indexed_at)}"
            if account.always_indexed:
                tip += "\nKept whatever the limit: you opened or added it"
            if key in self._index_errors:
                # The last refresh failed; the older catalog is still searched.
                return "Indexed", f"{tip}\nLast refresh failed: {self._index_errors[key]}", "warning"
            return "Indexed", tip, "success"
        if key in self._index_errors:
            return "Failed", f"Indexing failed: {self._index_errors[key]}\nRetried within the hour", "danger"
        if key not in indexable:
            limit = self.index_limit
            return (
                "Not indexed",
                f"Only {limit} working accounts are indexed automatically: "
                "starred first, then the ones already indexed.\n"
                "Open this account, right-click → Add to search index, or raise the limit under View → Search index.",
                "text_muted",
            )
        return "Waiting", "Will be indexed shortly", "text_muted"

    def search_catalog(
        self,
        text: str,
        scope: str,
        filters: SearchFilters,
        on_results: Callable[[SearchResult], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Search every indexed account; ``scope`` is "all", "starred" or "active".

        The language setting above the tabs applies here as well. Results of
        an older search that finishes late are dropped.
        """
        if self._catalog is None:
            on_results(SearchResult([]))
            return
        account_key: str | None = None
        if scope == "active":
            profile = self._state_store.state.active_profile
            if profile is None:
                on_error("Open an account first to search only in it.")
                return
            account_key = catalog_key_for(profile)
        self._search_generation += 1
        generation = self._search_generation
        catalog = self._catalog

        state = self._state_store.state
        filters = replace(
            filters,
            account_key=account_key,
            starred_only=scope == "starred",
            languages=frozenset(codes_for_language(state.language)),
            language_strict=state.language_strict,
        )

        worker = TaskWorker(lambda: catalog.search(text, filters))
        worker.signals.succeeded.connect(
            lambda result: on_results(result) if generation == self._search_generation else None,
        )
        worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
        self._thread_pool.start(worker)

    def _account_for_hit(self, hit: SearchHit) -> BulkResult | None:
        return next(
            (account for account in self._config.working_accounts if catalog_key(account) == hit.account_key),
            None,
        )

    @staticmethod
    def _hit_credentials(account: BulkResult, hit: SearchHit) -> Credentials:
        """Login for a hit: an M3U get.php account indexed through its Xtream API uses that API."""
        credentials = credentials_for(account)
        if hit.url:
            return credentials
        return xtream_credentials(credentials) or credentials

    def play_search_hit(
        self,
        hit: SearchHit,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
        use_vlc: bool = False,
    ) -> None:
        """Play a channel or movie found by search on the account that has it."""
        account = self._account_for_hit(hit)
        if account is None:
            on_error("That account is no longer in the list.")
            return
        credentials = self._hit_credentials(account, hit)
        item_type = "channel" if hit.kind == "live" else "vod"
        item = MediaItem(
            id=hit.provider_id,
            name=hit.name,
            media_kind=hit.kind,
            item_type=item_type,
            stream_url=hit.url or stream_url_for(credentials, item_type, hit.provider_id, hit.extension),
        )
        self.play(item, on_success=on_success, on_error=on_error, use_vlc=use_vlc, profile=credentials)

    def open_search_hit(
        self,
        hit: SearchHit,
        on_opened: Callable[[str], None],
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Browse the hit's account; a series opens straight into its seasons.

        ``on_opened`` gets the tab ("Live", "Movies", "Series") to show.
        """
        account = self._account_for_hit(hit)
        if account is None:
            on_error("That account is no longer in the list.")
            return
        credentials = self._hit_credentials(account, hit)
        # An M3U playlist lists everything under Live.
        tab_name = (
            "Live" if credentials.portal_type == PortalType.M3U
            else {"live": "Live", "movies": "Movies"}.get(hit.kind, "Series")
        )

        def on_connected(message: str) -> None:
            on_success(message)
            on_opened(tab_name)
            if not hit.playable:
                series = MediaItem(id=hit.provider_id, name=hit.name, media_kind="series", item_type="series")
                self.activate_item(tab_name, series, on_success=on_success, on_error=on_error)

        self.connect(credentials, on_success=on_connected, on_error=on_error)

    def _remember_success(self, credentials: Credentials) -> None:
        LOGGER.info("Saving successful connection for %s", credentials.base_url)
        stamped = Credentials(
            name=credentials.name,
            base_url=credentials.base_url,
            portal_type=credentials.portal_type,
            username=credentials.username,
            password=credentials.password,
            mac_address=credentials.mac_address,
            saved_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        key = (credentials.base_url, credentials.portal_type.value, credentials.username, credentials.mac_address)
        history: list[Credentials] = []
        for entry in self._config.successful_history:
            entry_key = (entry.base_url, entry.portal_type.value, entry.username, entry.mac_address)
            if entry_key != key:
                history.append(entry)
        history.insert(0, stamped)
        self._config.successful_history = history[:20]
        self._config_store.save(self._config)

    def clear_history(self) -> None:
        self._config.successful_history = []
        self._config_store.save(self._config)

    def _on_error(self, message: str, callback: Callable[[str], None]) -> None:
        LOGGER.error("Operation failed: %s", message)
        self._state_store.update(status_text=f"Error: {message}")
        callback(message)

    def set_language(self, language: str) -> None:
        """Persist and apply the language filter."""
        if language == self._state_store.state.language:
            return
        self._config.ui.language = language
        self._config_store.save(self._config)
        self._state_store.update(language=language)

    def set_language_strict(self, strict: bool) -> None:
        """Persist and apply whether untagged entries are hidden as well."""
        if strict == self._state_store.state.language_strict:
            return
        self._config.ui.language_strict = strict
        self._config_store.save(self._config)
        self._state_store.update(language_strict=strict)

    def _visible_items(self, tab_name: str) -> list[MediaItem]:
        """Items of a tab after the language filter, as shown in the table."""
        state = self._state_store.state
        return filter_items(
            state.current_items.get(tab_name, []),
            codes_for_language(state.language),
            state.categories.get(tab_name, []),
            state.language_strict,
        )

    def set_theme(self, mode: str) -> None:
        """Persist the light/dark mode ("system", "light" or "dark")."""
        self._config.ui.theme = mode
        self._config_store.save(self._config)

    def save_ui_state(self, width: int, height: int) -> None:
        """Persist current window size."""
        self._config.ui.width = width
        self._config.ui.height = height
        self._config_store.save(self._config)


def catalog_key(account: BulkResult) -> str:
    """The catalog's id for an account: a hash, so catalog.db holds no passwords."""
    return hashlib.sha1("\x1f".join(account_key(account)).encode("utf-8")).hexdigest()


def catalog_key_for(credentials: Credentials) -> str:
    return catalog_key(result_from_credentials(credentials))



class _IndexSignals(QObject):
    """The indexer thread reports here; slots run in the UI thread."""

    started = pyqtSignal(str)
    # (account key, summary, error)
    done = pyqtSignal(str, str, str)


def _ago(timestamp: float) -> str:
    minutes = int((time.time() - timestamp) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h ago" if hours < 24 else f"{hours // 24} d ago"


def _expiry_timestamp(expiry: str) -> float | None:
    """The bulk checker's "YYYY-MM-DD HH:MM" as a timestamp; None for unlimited/unknown."""
    try:
        return datetime.strptime(expiry.strip(), "%Y-%m-%d %H:%M").timestamp()
    except ValueError:
        return None


def _int_or_none(value: str) -> int | None:
    return int(value) if str(value).strip().isdigit() else None
