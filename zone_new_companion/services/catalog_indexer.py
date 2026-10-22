"""Download an account's whole catalog into the CatalogStore."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from typing import Callable
from urllib.parse import parse_qs, urlparse

from zone_new_companion.models import Credentials, MediaItem, PlaylistCategory, PortalType
from zone_new_companion.services.catalog_store import CatalogItem, CatalogStore
from zone_new_companion.services.country_filter import detect_countries, detect_item_countries
from zone_new_companion.services.m3u_service import M3UService, PlaylistEntry, parse_playlist
from zone_new_companion.services.title_parser import parse_title
from zone_new_companion.services.xtream_service import XtreamService

LOGGER = logging.getLogger(__name__)

# Catalogs older than this are downloaded again, unless the user picks
# another interval under View → Search index.
MAX_AGE_SECONDS = 24 * 3600
# Portal types the indexer can read; the rest is skipped for now.
INDEXED_PORTALS = ("xtream", "m3u")
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


def category_rows(categories: dict[str, list[PlaylistCategory]]) -> list[tuple[str, str, str]]:
    """fetch_categories' result as the store's (kind, provider_id, name), in the provider's order."""
    return [(_KINDS[tab], category.id, category.name) for tab, rows in categories.items() for category in rows]


def categories_from_rows(rows: list[tuple[str, str, str]]) -> dict[str, list[PlaylistCategory]]:
    """The store's category rows back in fetch_categories' shape."""
    tabs = {kind: tab for tab, kind in _KINDS.items()}
    grouped: dict[str, list[PlaylistCategory]] = {tab: [] for tab in _KINDS}
    for kind, provider_id, name in rows:
        if kind in tabs:
            grouped[tabs[kind]].append(PlaylistCategory(id=provider_id, name=name, media_kind=kind))
    return grouped


def server_of(base_url: str) -> str:
    return (urlparse(base_url).netloc or base_url).lower()


def xtream_credentials(credentials: Credentials) -> Credentials | None:
    """The Xtream account behind an M3U ``get.php`` link, if it is one."""
    if credentials.portal_type != PortalType.M3U:
        return None
    parsed = urlparse(credentials.base_url)
    params = parse_qs(parsed.query)
    username = (params.get("username") or [""])[0]
    password = (params.get("password") or [""])[0]
    if not (parsed.path.endswith("get.php") and username and password):
        return None
    return replace(
        credentials,
        base_url=f"{parsed.scheme or 'http'}://{parsed.netloc}",
        portal_type=PortalType.XTREAM,
        username=username,
        password=password,
    )


def index_m3u_account(
    store: CatalogStore,
    m3u: M3UService,
    xtream: XtreamService,
    credentials: Credentials,
    account_key: str,
    max_age_seconds: float = MAX_AGE_SECONDS,
    cancelled: Callable[[], bool] = lambda: False,
) -> str:
    """Index one M3U account; a get.php link goes through its Xtream API.

    The API lists movies and series apart and shares a catalog with the same
    account added as Xtream; when the panel refuses it, the playlist is read.
    """
    api = xtream_credentials(credentials)
    if api is not None:
        try:
            return index_account(store, xtream, api, account_key, max_age_seconds, cancelled)
        except RuntimeError as exc:
            LOGGER.info("Xtream API of %s failed (%s), reading the playlist", server_of(api.base_url), exc)
    server = server_of(credentials.base_url)
    fingerprint = hashlib.sha1(f"m3u\n{credentials.base_url}".encode("utf-8")).hexdigest()
    source_id = store.fresh_source(fingerprint, max_age_seconds)
    if source_id is not None:
        store.link_account(account_key, source_id)
        return f"{server}: shares an indexed playlist"
    entries = parse_playlist(m3u.fetch_playlist(credentials.base_url))
    if cancelled():
        return f"{server}: cancelled"
    items: list[CatalogItem] = []
    categories: dict[tuple[str, str], str] = {}
    langs: dict[str, frozenset[str]] = {}
    for entry in entries:
        kind = _m3u_kind(entry.url)
        categories[(kind, entry.group)] = entry.group or "Uncategorized"
        if entry.group not in langs:
            langs[entry.group] = frozenset(detect_countries(entry.group))
        items.append(_m3u_item(entry, kind, langs[entry.group]))
    rows = [(kind, group, name) for (kind, group), name in categories.items()]
    count = store.replace_source(account_key, fingerprint, server, rows, items)
    return f"{server}: {count:,} playlist entries indexed"


def _m3u_kind(url: str) -> str:
    """Xtream-built playlists put movies and episodes under /movie/ and /series/."""
    path = urlparse(url).path
    if "/movie/" in path:
        return "movies"
    if "/series/" in path:
        return "series"
    return "live"


def _m3u_item(entry: PlaylistEntry, kind: str, category_langs: frozenset[str]) -> CatalogItem:
    parsed = parse_title(entry.name, entry.group)
    path = urlparse(entry.url).path
    extension = path.rsplit(".", 1)[1] if "." in path.rsplit("/", 1)[-1] else ""
    return CatalogItem(
        kind=kind,
        # Playlists carry no ids; the URL is what stays the same between downloads.
        provider_id=hashlib.sha1(entry.url.encode("utf-8")).hexdigest()[:16],
        category_id=entry.group,
        name=entry.name,
        title=parsed.title,
        title_norm=parsed.title_norm,
        year=parsed.year,
        quality=parsed.quality,
        item_langs=frozenset(detect_item_countries(entry.name, entry.tvg_id)),
        cat_langs=category_langs,
        extension=extension,
        icon=entry.logo,
        url=entry.url,
    )


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
    if cancelled():
        return f"{server}: cancelled"
    count = store.replace_source(account_key, fingerprint, server, category_rows(categories), items)
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
