"""Application controller."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qs, urlparse

from zone_new_companion.services.logger_service import logger_service

from PyQt6.QtCore import QRunnable, QThreadPool

from zone_new_companion.config import AppConfig, ConfigStore
from zone_new_companion.models import Credentials, MediaItem, PlaylistCategory, PortalType
from zone_new_companion.services.base import PortalService
from zone_new_companion.services.bulk_checker import (
    BulkChecker,
    BulkResult,
    account_key,
    carry_stream_history,
    credentials_for,
    friendly_error,
    mark_server_unavailable,
    now_stamp,
    result_from_credentials,
)
from zone_new_companion.services.country_filter import codes_for_language, filter_items
from zone_new_companion.services.filter_debug import FilterDebugDumper, debug_enabled
from zone_new_companion.services.hls_probe import find_hls_url
from zone_new_companion.services.m3u_service import M3UService
from zone_new_companion.services.player_launcher import launch_stream
from zone_new_companion.services.stalker_service import StalkerService
from zone_new_companion.services.stream_verifier import StreamVerifier
from zone_new_companion.services.xtream_service import XtreamService
from zone_new_companion.state import StateStore
from zone_new_companion.workers.task_worker import BulkCheckSignals, TaskWorker

LOGGER = logging.getLogger(__name__)


class AppController:
    """Coordinate UI events, state, and services."""

    def __init__(self, state_store: StateStore, config_store: ConfigStore) -> None:
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
            self._state_store.update(
                active_profile=credentials,
                categories={"Live": [], "Movies": [], "Series": []},
                current_items={"Live": [], "Movies": [], "Series": []},
                verification_results={"Live": {}, "Movies": {}, "Series": {}},
                persistent_verification_results={},
                now_playing={"Live": {}, "Movies": {}, "Series": {}},
                busy=True,
                status_text=f"Connecting to {credentials.base_url}",
            )

            def task() -> tuple[dict[str, list[PlaylistCategory]], dict[str, str]]:
                logger_service.debug("Fetching categories and connection info...")
                categories = service.fetch_categories(credentials)
                info = service.fetch_connection_info(credentials)
                counts = ", ".join(f"{tab}: {len(rows)}" for tab, rows in categories.items())
                logger_service.info(f"Retrieved categories ({counts})")
                return categories, info

            worker = TaskWorker(task)
            worker.signals.succeeded.connect(lambda payload: self._on_connected(credentials, payload, on_success))
            worker.signals.failed.connect(lambda message: self._on_error(message, on_error))
            worker.signals.finished.connect(lambda: self._state_store.update(busy=False))
            self._thread_pool.start(worker)
            
            logger_service.info("Connection task started")
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
        """Worker-thread fetch of one category; recorded when debugging."""
        items = self._service_for(profile).fetch_items(profile, category)
        if self._debug is not None:
            state = self._state_store.state
            self._debug.record_items(profile, tab_name, category, items, state.language, state.language_strict)
        return items

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
    ) -> None:
        """Resolve the stream and play it in the built-in player, or in VLC.

        VLC plays when use_vlc is set or the built-in player is unavailable.
        With prefer_hls the stream's HLS (.m3u8) variant plays instead; it is an
        error when the server offers none.
        """
        profile = self._state_store.state.active_profile
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
        """Run ``check(checker, progress)`` in the pool, reporting through signals."""
        signals = BulkCheckSignals()
        checker = BulkChecker()
        self._bulk_checker = checker

        def emit_progress(result: BulkResult, done: int, total: int) -> None:
            signals.progress.emit(result, done, total)

        def task() -> list[BulkResult]:
            return check(checker, emit_progress)

        class _BulkRunnable(QRunnable):
            def run(self_inner) -> None:  # noqa: N805
                try:
                    results = task()
                    signals.succeeded.emit(results)
                except Exception as exc:  # noqa: BLE001
                    signals.failed.emit(str(exc))
                finally:
                    signals.finished.emit()

        self._thread_pool.start(_BulkRunnable())
        return signals

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
        """Load a working account (any type) as the active profile and browse it."""
        self.connect(credentials_for(result), on_success=on_success, on_error=on_error)

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
