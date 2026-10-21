"""Language filter for playlists.

Xtream, M3U and Stalker portals carry no language field for streams, so the
country is guessed from naming conventions providers use:

* a code as its own part between separators: ``DE: Das Erste``,
  ``VOD | EN - NETFLIX``, ``|DE| ARD``, ``ARD HD [DE]``
* a country or language name anywhere: ``GERMANY HD``, ``DEUTSCH FILME``
* the EPG id's country suffix: ``daserste.de``

Tags must be written in capitals, the way providers write them, so titles such
as ``It: Chapter Two`` or ``The French Connection`` are not taken for tags.
Only codes known below count. Entries tagged with another country are hidden.
Untagged ones stay visible, unless ``strict`` is set: then a category needs a
tag of the wanted language to show. A multi-audio marker (``MULTI-AUDIO``,
``MULTI-SUB-AUDIO``, ``MULTI-SUB/AUDIO``, ``MULTI-DUB``, ``MULTI-LANG``)
counts as every language, since such streams usually carry the major audio
tracks; ``MULTI-SUB`` alone is only subtitles and does not.

Inside a category tagged with the wanted country every entry stays: providers
prefix its entries loosely (``AU - ORF 1`` for Austria, ``GE - ES: ...`` with
the German title "Es", ``FR - Männerhort``). Elsewhere an item's leading tag
(``EN - Title [MULTI-SUB]``) decides, so the title itself is not searched.
"""

from __future__ import annotations

import re
from typing import Iterable

from zone_new_companion.models import MediaItem, PlaylistCategory

# Canonical code -> spellings providers use for it.
_ALIASES: dict[str, set[str]] = {
    "DE": {"DE", "GE", "GER", "GERMANY", "GERMAN", "DEUTSCH", "DEUTSCHLAND", "DACH"},
    "AT": {"AT", "AUT", "AUSTRIA", "ÖSTERREICH", "OSTERREICH"},
    "CH": {"CH", "SUI", "SWISS", "SCHWEIZ", "SWITZERLAND"},
    "UK": {"UK", "GB", "ENGLAND", "BRITAIN"},
    "EN": {"EN", "ENG", "ENGLISH"},
    "US": {"US", "USA"},
    "FR": {"FR", "FRA", "FRANCE", "FRENCH"},
    "IT": {"IT", "ITA", "ITALY", "ITALIA", "ITALIAN"},
    "ES": {"ES", "ESP", "SPAIN", "SPANISH", "ESPANA"},
    "PT": {"PT", "POR", "PORTUGAL", "PORTUGUESE"},
    "NL": {"NL", "NLD", "NETHERLANDS", "NETHERLAND", "HOLLAND", "DUTCH"},
    "PL": {"PL", "POL", "POLAND", "POLSKA", "POLISH"},
    "TR": {"TR", "TUR", "TURKEY", "TURKIYE", "TÜRKIYE", "TURKISH"},
    "AR": {"AR", "ARA", "ARAB", "ARABIC"},
    "RU": {"RU", "RUS", "RUSSIA", "RUSSIAN"},
    "AL": {"AL", "ALB", "ALBANIA"},
    "GR": {"GR", "GRE", "GREECE", "GREEK"},
    "BE": {"BE", "BEL", "BELGIUM", "BELGIQUE"},
    "IE": {"IE", "IRL", "IRELAND"},
    "CA": {"CA", "CAN", "CANADA"},
    "AU": {"AU", "AUS", "AUSTRALIA"},
    "BR": {"BR", "BRA", "BRAZIL", "BRASIL"},
    "MX": {"MX", "MEX", "MEXICO", "LATINO", "LATAM"},
    "SE": {"SE", "SWE", "SWEDEN", "SWEDISH", "SVENSKA"},
    "DK": {"DK", "DEN", "DENMARK", "DANISH", "DANSK"},
    "NO": {"NO", "NOR", "NORWAY", "NORWEGIAN", "NORSK"},
    "FI": {"FI", "FIN", "FINLAND", "FINNISH", "SUOMI"},
    "EXYU": {"EXYU", "YU", "RS", "HR", "BA", "SI", "MK", "ME"},
    "RO": {"RO", "ROU", "ROMANIA"},
    "HU": {"HU", "HUN", "HUNGARY", "HUNGARIA", "HUNGARIAN"},
    "CZ": {"CZ", "CZE", "CZECH", "SK", "SVK", "SLOVAKIA"},
    "BG": {"BG", "BUL", "BULGARIA"},
    "UA": {"UA", "UKR", "UKRAINE", "UKRAIN"},
    "IN": {"IN", "IND", "INDIA", "INDIAN", "HINDI", "BOLLYWOOD", "TELUGU", "MALAYALAM", "KANNADA"},
    "PK": {"PK", "PAK", "PAKISTAN", "URDU", "PUNJABI"},
    "IR": {"IR", "IRAN", "PERSIAN", "FARSI"},
    "KU": {"KU", "KURD", "KURDISH", "KURDISTAN"},
    "AFG": {"AFG", "AFGHANISTAN"},
    "IL": {"IL", "ISR", "ISRAEL", "HEBREW"},
    "CN": {"CN", "CHN", "CHINA", "CHINESE"},
    "JP": {"JP", "JPN", "JAPAN", "JAPANESE"},
    "KR": {"KR", "KOR", "KOREA", "KOREAN"},
    "PH": {"PH", "PHI", "PHILIPPINES", "FILIPINO"},
    "TH": {"TH", "THA", "THAILAND", "THAI"},
    "VN": {"VN", "VIE", "VIETNAM"},
    "AFR": {"AF", "AFR", "AFRICA", "AFRICAN", "NG", "ZA", "NIGERIA", "SENEGAL", "SOMALIA", "UGANDA"},
    "MAG": {"MAG", "MAGHREB", "MA", "DZ", "TN"},
    "EG": {"EG", "EGY", "EGYPT"},
    "SA": {"SA", "KSA", "SAUDI", "AE", "UAE"},
    "IS": {"IS", "ISL", "ICELAND", "ICELANDIC"},
    "LT": {"LT", "LTU", "LITHUANIA"},
    "LV": {"LV", "LVA", "LATVIA"},
    "EE": {"EE", "EST", "ESTONIA"},
    "AM": {"ARM", "ARMENIA"},
    "GE": {"GEO", "GEORGIA"},
    "AZ": {"AZ", "AZE", "AZERBAIJAN"},
    "CY": {"CY", "CYP", "CYPRUS"},
    "MT": {"MT", "MLT", "MALTA"},
    "LU": {"LU", "LUX", "LUXEMBOURG"},
    "ID": {"INDONESIA", "INDO"},
    "MY": {"MY", "MYS", "MALAYSIA"},
    "BD": {"BD", "BGD", "BANGLA", "BANGLADESH"},
    "LK": {"LK", "SRILANKA", "TAMIL"},
    "CB": {"CARIBBEAN", "CARIBBIEAN", "CARIB"},
    "NZ": {"NZ", "ZEALAND"},
    "KH": {"CAMBODIA"},
    "KZ": {"KAZAKHSTAN", "KAZAHKSTAN"},
    "SV": {"SALVADOR"},
}

# A title's leading tag means something else than a category tag on panels:
# "CH - Panda Plan" is Chinese (Swiss channels sit in "EU | SWISS" anyway).
_ITEM_PREFIXES: dict[str, str] = {"CH": "CN", "KO": "KR", "SP": "ES", "TW": "CN", "BL": "IN"}

# Stands for "every language": multi-audio streams carry most audio tracks.
MULTI = "MULTI"
_MULTI_AUDIO = re.compile(
    r"\bMULTI[\s_\-/]*(?:SUB[\s_\-/]*)?(?:A+UDIO|DUB|LANG(?:UAGE)?S?)\b|\bDUAL[\s_\-]*AUDIO\b",
    re.IGNORECASE,
)

# Language key -> (label, country codes whose tags count as that language).
LANGUAGES: dict[str, tuple[str, set[str]]] = {
    "de": ("German", {"DE", "AT", "CH"}),
    "en": ("English", {"EN", "UK", "US", "IE", "CA", "AU"}),
    "fr": ("French", {"FR", "BE"}),
    "it": ("Italian", {"IT"}),
    "es": ("Spanish", {"ES", "MX"}),
    "pt": ("Portuguese", {"PT", "BR"}),
    "nl": ("Dutch", {"NL", "BE"}),
    "sv": ("Swedish", {"SE"}),
    "da": ("Danish", {"DK"}),
    "no": ("Norwegian", {"NO"}),
    "fi": ("Finnish", {"FI"}),
}
_LOOKUP: dict[str, str] = {alias: code for code, aliases in _ALIASES.items() for alias in aliases}
# Long names are matched as any word; short codes only as a whole part.
_LONG_NAMES = {alias for alias in _LOOKUP if len(alias) > 4}

_SEPARATORS = re.compile(r"[|:\-–—/\[\](){}•►»▶★#]+")
_WORD = re.compile(r"[^\W\d_]+")
_EPG_SUFFIX = re.compile(r"\.([a-z]{2})$", re.IGNORECASE)


def codes_for_language(language: str) -> set[str]:
    """Country codes for a ``LANGUAGES`` key; empty (no filter) when unknown."""
    entry = LANGUAGES.get(language)
    return set(entry[1]) if entry else set()


def detect_countries(*texts: str, epg_id: str = "") -> set[str]:
    """Return the country codes tagged in the given names."""
    found: set[str] = set()
    for text in texts:
        for part in _SEPARATORS.split(text):
            part = part.strip()
            if part.isupper() and part in _LOOKUP:
                found.add(_LOOKUP[part])
        for word in _WORD.findall(text):
            if word.isupper() and word in _LONG_NAMES:
                found.add(_LOOKUP[word])
    suffix = _EPG_SUFFIX.search(epg_id.strip())
    if suffix and suffix.group(1).upper() in _LOOKUP:
        found.add(_LOOKUP[suffix.group(1).upper()])
    if any(_MULTI_AUDIO.search(text) for text in texts):
        found.add(MULTI)
    return found


def verdict(found: set[str], wanted: set[str]) -> bool | None:
    """True = wanted country (or multi-audio), False = other country, None = untagged."""
    if not found:
        return None
    return MULTI in found or bool(found & wanted)


def category_verdict(category: PlaylistCategory, wanted: set[str]) -> bool | None:
    return verdict(detect_countries(category.name), wanted)


def detect_item_countries(name: str, epg_id: str = "") -> set[str]:
    """Like ``detect_countries``, but a leading tag (``EN - Title``) wins.

    Without one, only whole parts count (``##### ICELAND #####``,
    ``Sky Sports F1 - USA``), not words inside a title (``RELAX AUSTRALIA``).
    """
    parts = [part.strip() for part in _SEPARATORS.split(name)]
    parts = [part for part in parts if part]
    found: set[str] = set()
    lead = parts[0] if len(parts) > 1 and parts[0].isupper() else ""
    if lead in _ITEM_PREFIXES or lead in _LOOKUP:
        found.add(_ITEM_PREFIXES.get(lead) or _LOOKUP[lead])
    else:
        found = {_LOOKUP[part] for part in parts if part.isupper() and part in _LOOKUP}
        suffix = _EPG_SUFFIX.search(epg_id.strip())
        if suffix and suffix.group(1).upper() in _LOOKUP:
            found.add(_LOOKUP[suffix.group(1).upper()])
    if _MULTI_AUDIO.search(name):
        found.add(MULTI)
    return found


def item_verdict(item: MediaItem, wanted: set[str]) -> bool | None:
    epg_id = str(item.metadata.get("epg_channel_id") or "")
    return verdict(detect_item_countries(item.name, epg_id=epg_id), wanted)


def item_visible(
    item: MediaItem,
    category: PlaylistCategory | None,
    wanted: set[str],
    strict: bool = False,
) -> tuple[bool, str]:
    """Whether an item shows, and which rule decided ("category", "own", "inherited")."""
    if not wanted or item.item_type in {"season", "episode"}:
        return True, "unfiltered"
    category_found = detect_countries(category.name) if category else set()
    if category_found & wanted:
        return True, "category"
    own = item_verdict(item, wanted)
    if own is not None:
        return shown(own, strict), "own"
    return shown(verdict(category_found, wanted), strict), "inherited"


def shown(result: bool | None, strict: bool) -> bool:
    return result is True or (result is None and not strict)


def category_visible(category: PlaylistCategory, wanted: set[str], strict: bool = False) -> bool:
    if not wanted:
        return True
    return shown(category_verdict(category, wanted), strict)


def filter_categories(
    categories: Iterable[PlaylistCategory],
    wanted: set[str],
    strict: bool = False,
) -> list[PlaylistCategory]:
    return [category for category in categories if category_visible(category, wanted, strict)]


def filter_items(
    items: Iterable[MediaItem],
    wanted: set[str],
    categories: Iterable[PlaylistCategory] = (),
    strict: bool = False,
) -> list[MediaItem]:
    """Keep items of the wanted countries (see ``item_visible``).

    The item's category is found via ``category_id`` in its metadata. Seasons
    and episodes are never filtered.
    """
    items = list(items)
    if not wanted:
        return items
    by_id = {category.id: category for category in categories}
    return [
        item for item in items
        if item_visible(item, by_id.get(str(item.metadata.get("category_id", ""))), wanted, strict)[0]
    ]
