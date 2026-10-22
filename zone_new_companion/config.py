"""Persistent configuration management."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from zone_new_companion.models import Credentials, PortalType
from zone_new_companion.services.bulk_checker import BulkResult

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class UiSettings:
    """UI settings persisted between restarts."""

    # "system" follows the OS light/dark setting; else "light" or "dark".
    theme: str = "system"
    width: int = 1200
    height: int = 760
    language: str = ""
    # Hide categories that carry no tag of the chosen language.
    language_strict: bool = True
    # Working accounts whose catalog is downloaded for search (0 = all).
    search_index_limit: int = 5


@dataclass(slots=True)
class AppConfig:
    """Whole application persisted config."""

    ui: UiSettings = field(default_factory=UiSettings)
    successful_history: list[Credentials] = field(default_factory=list)
    last_input: Credentials | None = None
    # Working accounts from the last bulk check, restored on startup.
    working_accounts: list[BulkResult] = field(default_factory=list)


class ConfigStore:
    """Manage config read/write as JSON."""

    def __init__(self, config_path: Path) -> None:
        self._path = config_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> AppConfig:
        """Load config from disk; defaults if it is missing or unreadable."""
        if not self._path.exists():
            return AppConfig()
        try:
            with self._path.open("r", encoding="utf-8") as file_handle:
                return self._parse(json.load(file_handle))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # Keep the broken file so the next save does not destroy it.
            backup = self._path.with_name(f"{self._path.name}.corrupt")
            LOGGER.error("Unreadable config %s (%s); starting with defaults, kept as %s", self._path, exc, backup)
            try:
                os.replace(self._path, backup)
            except OSError:
                LOGGER.exception("Could not move the unreadable config aside")
            return AppConfig()

    @staticmethod
    def _parse(payload: dict[str, Any]) -> AppConfig:
        ui_payload = payload.get("ui", {})
        history_payload = payload.get("successful_history", payload.get("profiles", []))
        history = [
            Credentials(
                name=str(row.get("name", "Last Used")),
                base_url=str(row.get("base_url", "")),
                portal_type=PortalType(str(row.get("portal_type", PortalType.XTREAM.value))),
                username=str(row.get("username", "")),
                password=str(row.get("password", "")),
                mac_address=str(row.get("mac_address", "")),
                saved_at=str(row.get("saved_at", "")),
            )
            for row in history_payload
        ]
        last_input_payload = payload.get("last_input")
        last_input = None
        if isinstance(last_input_payload, dict):
            last_input = Credentials(
                name=str(last_input_payload.get("name", "Last Used")),
                base_url=str(last_input_payload.get("base_url", "")),
                portal_type=PortalType(
                    str(last_input_payload.get("portal_type", PortalType.XTREAM.value)),
                ),
                username=str(last_input_payload.get("username", "")),
                password=str(last_input_payload.get("password", "")),
                mac_address=str(last_input_payload.get("mac_address", "")),
                saved_at=str(last_input_payload.get("saved_at", "")),
            )
        ui = UiSettings(
            theme=str(ui_payload.get("theme", "system")),
            width=int(ui_payload.get("width", 1200)),
            height=int(ui_payload.get("height", 760)),
            language=str(ui_payload.get("language", "")),
            language_strict=bool(ui_payload.get("language_strict", True)),
            search_index_limit=max(0, int(ui_payload.get("search_index_limit", 5))),
        )
        working_accounts = [
            BulkResult(
                line_number=int(row.get("line_number", 0)),
                raw=str(row.get("raw", "")),
                base_url=str(row.get("base_url", "")),
                username=str(row.get("username", "")),
                password=str(row.get("password", "")),
                portal_type=str(row.get("portal_type", PortalType.XTREAM.value)),
                mac_address=str(row.get("mac_address", "")),
                ok=bool(row.get("ok", True)),
                status=str(row.get("status", "")),
                expiry=str(row.get("expiry", "")),
                max_connections=str(row.get("max_connections", "")),
                active_connections=str(row.get("active_connections", "")),
                timezone=str(row.get("timezone", "")),
                real_url=str(row.get("real_url", "")),
                starred=bool(row.get("starred", False)),
                stream_ok=row.get("stream_ok") if isinstance(row.get("stream_ok"), bool) else None,
                stream_status=str(row.get("stream_status", "")),
                stream_checked_at=str(row.get("stream_checked_at", "")),
                stream_down_since=str(row.get("stream_down_since", "")),
            )
            for row in payload.get("working_accounts", [])
            if isinstance(row, dict)
        ]
        return AppConfig(
            ui=ui,
            successful_history=history,
            last_input=last_input,
            working_accounts=working_accounts,
        )

    def save(self, config: AppConfig) -> None:
        """Safely write config to disk."""
        payload: dict[str, Any] = asdict(config)
        # Write a temp file next to the config and swap it in, so a crash
        # mid-write leaves the previous config intact.
        fd, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
                json.dump(payload, file_handle, indent=2)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(temp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
