"""Debug dumps of category/stream lists and the language filter's verdicts.

Enabled with ``DEBUG=1`` or ``--debug``. Every loaded category list and every
loaded category's streams are written to
``~/.zone-new-companion/debug/<host>.json``; "Debug > Dump full catalog"
fetches everything at once. Only names, ids, category ids and EPG ids are
written: no credentials, no stream URLs.

The dumps can be re-evaluated offline against the current filter code::

    python -m zone_new_companion.services.filter_debug ~/.zone-new-companion/debug/host.json --language de
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from zone_new_companion.models import Credentials, MediaItem, PlaylistCategory
from zone_new_companion.services.country_filter import (
    LANGUAGES,
    shown,
    category_verdict,
    codes_for_language,
    detect_countries,
    detect_item_countries,
    item_verdict,
    item_visible,
)

DEBUG_DIR = Path.home() / ".zone-new-companion" / "debug"


def debug_enabled() -> bool:
    return os.getenv("DEBUG", "0") == "1" or "--debug" in sys.argv


def _host(credentials: Credentials) -> str:
    host = urlparse(credentials.base_url).hostname or credentials.base_url or "unknown"
    return "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in host)


def _category_row(category: PlaylistCategory, wanted: set[str], strict: bool) -> dict[str, Any]:
    result = category_verdict(category, wanted)
    return {
        "id": category.id,
        "name": category.name,
        "detected": sorted(detect_countries(category.name)),
        "verdict": result,
        "visible": not wanted or shown(result, strict),
    }


def _item_row(item: MediaItem, category: PlaylistCategory, wanted: set[str], strict: bool) -> dict[str, Any]:
    epg_id = str(item.metadata.get("epg_channel_id") or "")
    visible, rule = item_visible(item, category, wanted, strict)
    return {
        "id": item.id,
        "name": item.name,
        "item_type": item.item_type,
        "category_id": str(item.metadata.get("category_id", "")),
        "epg_channel_id": epg_id,
        "detected": sorted(detect_item_countries(item.name, epg_id=epg_id)),
        "verdict": item_verdict(item, wanted),
        "rule": rule,
        "visible": visible,
    }


class FilterDebugDumper:
    """Collects what the app loaded per host and rewrites its JSON dump."""

    def __init__(self, directory: Path = DEBUG_DIR) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._dumps: dict[str, dict[str, Any]] = {}

    def path_for(self, credentials: Credentials) -> Path:
        return self._directory / f"{_host(credentials)}.json"

    def _dump(self, credentials: Credentials) -> dict[str, Any]:
        host = _host(credentials)
        if host not in self._dumps:
            self._dumps[host] = {
                "host": host,
                "portal_type": credentials.portal_type.value,
                "categories": {},
                "items": {},
            }
        return self._dumps[host]

    def record_categories(
        self,
        credentials: Credentials,
        categories: dict[str, list[PlaylistCategory]],
        language: str,
        strict: bool,
    ) -> Path:
        wanted = codes_for_language(language)
        with self._lock:
            dump = self._dump(credentials)
            dump["categories"] = {
                tab: [_category_row(category, wanted, strict) for category in rows]
                for tab, rows in categories.items()
            }
            return self._write(credentials, dump, language, strict)

    def record_items(
        self,
        credentials: Credentials,
        tab: str,
        category: PlaylistCategory,
        items: list[MediaItem],
        language: str,
        strict: bool,
    ) -> Path:
        wanted = codes_for_language(language)
        with self._lock:
            dump = self._dump(credentials)
            dump["items"].setdefault(tab, {})[category.id] = {
                "category": category.name,
                "items": [_item_row(item, category, wanted, strict) for item in items],
            }
            return self._write(credentials, dump, language, strict)

    def _write(self, credentials: Credentials, dump: dict[str, Any], language: str, strict: bool) -> Path:
        dump["updated_at"] = datetime.now().isoformat(timespec="seconds")
        dump["filter"] = {"language": language, "codes": sorted(codes_for_language(language)), "strict": strict}
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(credentials)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(path)
        return path


def report(dump: dict[str, Any], language: str, strict: bool) -> str:
    """Re-run the current filter over a dump; list what is shown and hidden."""
    wanted = codes_for_language(language)
    lines = [f"host={dump.get('host')} language={language or 'all'} codes={sorted(wanted)} strict={strict}", ""]
    for tab, rows in dump.get("categories", {}).items():
        categories = [PlaylistCategory(id=row["id"], name=row["name"], media_kind=tab.lower()) for row in rows]
        visible_categories = [c for c in categories if not wanted or shown(category_verdict(c, wanted), strict)]
        lines.append(f"== {tab} categories: {len(visible_categories)}/{len(categories)} shown")
        for category in categories:
            result = category_verdict(category, wanted)
            mark = "+" if category in visible_categories else "-"
            lines.append(f"  {mark} [{result!s:5}] {sorted(detect_countries(category.name))!s:14} {category.name}")
        by_id = {c.id: c for c in categories}
        for category_id, entry in dump.get("items", {}).get(tab, {}).items():
            category = by_id.get(category_id) or PlaylistCategory(category_id, entry["category"], tab.lower())
            items = [
                MediaItem(
                    id=row["id"],
                    name=row["name"],
                    media_kind=tab.lower(),
                    item_type=row.get("item_type", ""),
                    metadata={"epg_channel_id": row.get("epg_channel_id", ""), "category_id": category_id},
                )
                for row in entry["items"]
            ]
            visible = [_item_row(item, category, wanted, strict) for item in items]
            count = sum(row["visible"] for row in visible)
            lines.append(f"  -- items of '{category.name}': {count}/{len(items)} shown")
            for row in visible:
                mark = "+" if row["visible"] else "-"
                lines.append(f"     {mark} {row['rule']:9} {row['detected']!s:14} {row['name']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Re-run the language filter over a debug dump.")
    parser.add_argument("dump", type=Path)
    parser.add_argument("--language", default="de", choices=["", *LANGUAGES])
    parser.add_argument("--lenient", action="store_true", help="keep untagged entries (Hide untagged off)")
    args = parser.parse_args(argv)
    dump = json.loads(args.dump.read_text(encoding="utf-8"))
    print(report(dump, args.language, strict=not args.lenient))


if __name__ == "__main__":
    main()
