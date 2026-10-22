"""Download an account's whole catalog into the CatalogStore."""

from __future__ import annotations

import hashlib
import logging
from typing import Callable
from urllib.parse import urlparse

from zone_new_companion.models import Credentials, MediaItem, PlaylistCategory
from zone_new_companion.services.catalog_store import CatalogItem, CatalogStore
from zone_new_companion.services.country_filter import detect_countries, detect_item_countries
from zone_new_companion.services.title_parser import parse_title
from zone_new_companion.services.xtream_service import XtreamService

LOGGER = logging.getLogger(__name__)

# Catalogs older than this are downloaded again.
MAX_AGE_SECONDS = 24 * 3600
# Portal types the indexer can read; the rest is skipped for now.
INDEXED_PORTALS = ("xtream",)
# Accounts indexed at most unless the user picks another limit (0 = all).
DEFAULT_INDEX_LIMIT = 5
# fetch_categories' tab names -> media kinds.
_KINDS = {"Live": "live", "Movies": "movies", "Series": "series"}


def catalog_fingerprint(base_url: str, categories: dict[str, list[PlaylistCategory]]) -> str:
    """Same server and same category ids means the same catalog (shared source)."""
    parts = [server_of(base_url)]
    for tab in sorted(categories):
        parts.append(tab + ":" + ",".join(sorted(category.id for category in categories[tab])))
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def server_of(base_url: str) -> str:
    return (urlparse(base_url).netloc or base_url).lower()


def index_account(
    store: CatalogStore,
    service: XtreamService,
    credentials: Credentials,
    account_key: str,
    max_age_seconds: float = MAX_AGE_SECONDS,
    cancelled: Callable[[], bool] = lambda: False,
) -> str:
    """Index one Xtream account; returns a short summary for the status line.

    ``cancelled`` is checked between downloads (the app is closing); a
    cancelled run stores nothing.
    """
    server = server_of(credentials.base_url)
    categories = service.fetch_categories(credentials)
    if cancelled():
        return f"{server}: cancelled"
    fingerprint = catalog_fingerprint(credentials.base_url, categories)
    source_id = store.fresh_source(fingerprint, max_age_seconds)
    if source_id is not None:
        # Another account already brought this exact catalog.
        store.link_account(account_key, source_id)
        return f"{server}: shares an indexed catalog"

    items: list[CatalogItem] = []
    for tab, kind in _KINDS.items():
        if cancelled():
            return f"{server}: cancelled"
        names = {category.id: category.name for category in categories[tab]}
        # Category tags are the same for all its streams: detect them once.
        langs = {category_id: frozenset(detect_countries(name)) for category_id, name in names.items()}
        for media in _fetch_kind(service, credentials, kind, categories[tab], cancelled):
            items.append(_catalog_item(media, names, langs))
    category_rows = [
        (_KINDS[tab], category.id, category.name) for tab, rows in categories.items() for category in rows
    ]
    if cancelled():
        return f"{server}: cancelled"
    count = store.replace_source(account_key, fingerprint, server, category_rows, items)
    return f"{server}: {count:,} streams indexed"


def _fetch_kind(
    service: XtreamService,
    credentials: Credentials,
    kind: str,
    categories: list[PlaylistCategory],
    cancelled: Callable[[], bool],
) -> list[MediaItem]:
    """Every stream of a kind; per category when the panel refuses the all-in-one call."""
    try:
        items = service.fetch_all_items(credentials, kind)
    except RuntimeError as exc:
        LOGGER.info("All-in-one %s fetch failed (%s), going per category", kind, exc)
        items = []
    if items or not categories:
        return items
    for category in categories:
        if cancelled():
            break
        try:
            items.extend(service.fetch_items(credentials, category))
        except RuntimeError as exc:
            LOGGER.warning("Skipping category %s: %s", category.name, exc)
    return items


def _catalog_item(
    media: MediaItem,
    category_names: dict[str, str],
    category_langs: dict[str, frozenset[str]],
) -> CatalogItem:
    row = media.metadata
    try:
        added_at = int(row.get("added") or row.get("last_modified") or 0)
    except (TypeError, ValueError):
        added_at = 0
    category_id = str(row.get("category_id") or "")
    parsed = parse_title(media.name, category_names.get(category_id, ""))
    return CatalogItem(
        kind=media.media_kind,
        provider_id=media.id,
        category_id=category_id,
        name=media.name,
        title=parsed.title,
        title_norm=parsed.title_norm,
        year=parsed.year or _year_of(row),
        quality=parsed.quality,
        item_langs=frozenset(detect_item_countries(media.name, str(row.get("epg_channel_id") or ""))),
        cat_langs=category_langs.get(category_id, frozenset()),
        extension=str(row.get("container_extension") or ""),
        icon=str(row.get("stream_icon") or row.get("cover") or ""),
        added_at=added_at,
    )


def _year_of(row: dict) -> int:
    """The release year some panels list separately ("year", "releaseDate")."""
    for key in ("year", "releaseDate", "releasedate", "release_date"):
        raw = str(row.get(key) or "")[:4]
        if raw.isdigit() and 1900 <= int(raw) <= 2100:
            return int(raw)
    return 0
