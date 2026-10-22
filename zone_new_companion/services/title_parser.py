"""Split provider stream names into title, year and quality.

Providers decorate names freely: ``|DE| Dune: Part Two (2024) [4K]``,
``DE: SKY SPORT BUNDESLIGA 1 FHD``, ``EN - Breaking Bad [MULTI-SUB]``. To find
the same movie on several accounts the decoration has to go: what is left,
case-folded and without accents, is the ``title_norm`` results are grouped by.

Like country_filter, tags count only in capitals (``It: Chapter Two`` keeps
its title), and the language tags are not parsed here but by country_filter.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

# Quality label -> rank; filters ask for "at least".
QUALITY_RANK = {"SD": 1, "HD": 2, "FHD": 3, "4K": 4}
QUALITIES = tuple(QUALITY_RANK)

# Case-insensitive markers (unambiguous anywhere).
_QUALITY_ANY_CASE = [
    ("4K", re.compile(r"\b(?:4K|UHD|2160P)\b|ᵁᴴᴰ", re.IGNORECASE)),
    ("FHD", re.compile(r"\b(?:FHD|FULL[\s-]?HD|1080[PI])\b", re.IGNORECASE)),
    ("HD", re.compile(r"\b720P\b", re.IGNORECASE)),
    ("SD", re.compile(r"\b(?:480P|576P)\b", re.IGNORECASE)),
]
# Short ones only in capitals: "HD" yes, "Hd" or the word "sd" in a title no.
_QUALITY_CAPS = [("HD", re.compile(r"\bHD\b\+?")), ("SD", re.compile(r"\bSD\b"))]

# Later "years" are part of the title ("Blade Runner 2049").
_MAX_YEAR = date.today().year + 2
_YEAR = re.compile(r"[(\[]\s*((?:19|20)\d\d)\s*[)\]]|(?:^|[\s\-–|:])((?:19|20)\d\d)\s*$")
# Bracketed decoration: [MULTI-SUB], (VIP), {NEW} ... but not a bracketed year.
_BRACKETED = re.compile(r"[\[({][^\[\](){}]*[\])}]")
# Leading tags: "|DE|", "DE:", "DE -", "VOD |", "4K-NF:" ... (capitals, digits, + and -).
_LEAD_TAG = re.compile(r"^\s*(?:[|│┃]\s*)?([A-Z0-9][A-Z0-9+\-]{0,7})\s*(?:[|│┃:]|\s[-–—]\s)\s*")
_TRAILING_TAG = re.compile(r"\s*(?:[|│┃]|\s[-–—])\s*([A-Z0-9][A-Z0-9+\-]{0,7})\s*$")
# Technical markers dropped anywhere (capitals only, as providers write them).
_NOISE = re.compile(
    r"\b(?:HEVC|H\.?26[45]|X26[45]|HDR10\+?|HDR|DV|DOLBY|ATMOS|AAC|AC3|DTS|"
    r"MULTI[\s_\-/]*(?:SUB|AUDIO|DUB|LANG)[\w\-/]*|DUAL[\s_\-]*AUDIO|VIP|NEW|RAW|BACKUP|"
    r"50FPS|60FPS)\b",
)
_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w]+")
_EDGE_PUNCT = " -–—|│┃:•.,_*#►▶»"


@dataclass(slots=True, frozen=True)
class ParsedTitle:
    title: str  # display form: "Dune: Part Two"
    title_norm: str  # grouping key: "dune part two"
    year: int  # 0 when unknown
    quality: str  # one of QUALITIES, "" when unknown


def parse_title(name: str, category_name: str = "") -> ParsedTitle:
    """Clean a stream name; the category may supply the quality ("DE | 4K MOVIES")."""
    text = unicodedata.normalize("NFKC", name).strip()
    quality = detect_quality(text) or detect_quality(category_name)

    for _pattern_label, pattern in (*_QUALITY_ANY_CASE, *_QUALITY_CAPS):
        text = pattern.sub(" ", text)
    text = _SPACES.sub(" ", _NOISE.sub(" ", text)).strip()

    year = 0
    match = _YEAR.search(text)
    # A title that is only a year ("2012", "1917") keeps it.
    if match and int(match.group(1) or match.group(2)) <= _MAX_YEAR and text[: match.start()].strip(_EDGE_PUNCT + "[("):
        year = int(match.group(1) or match.group(2))
        text = text[: match.start()] + " " + text[match.end():]

    text = _BRACKETED.sub(" ", text)
    # Leading tags may stack: "|DE| VOD: Title", "4K-NF: Title" once 4K is gone.
    for _ in range(3):
        text = text.strip(_EDGE_PUNCT)
        stripped = _LEAD_TAG.sub("", text, count=1)
        if stripped == text or not stripped.strip(_EDGE_PUNCT):
            break
        text = stripped
    stripped = _TRAILING_TAG.sub("", text)
    if stripped.strip(_EDGE_PUNCT):
        text = stripped
    title = _SPACES.sub(" ", text).strip(_EDGE_PUNCT)
    if not title:
        title = name.strip()
    return ParsedTitle(title=title, title_norm=normalize(title), year=year, quality=quality)


def detect_quality(text: str) -> str:
    """The best quality named in ``text``, or ""."""
    for label, pattern in _QUALITY_ANY_CASE:
        if pattern.search(text):
            return label
    for label, pattern in _QUALITY_CAPS:
        if pattern.search(text):
            return label
    return ""


def normalize(text: str) -> str:
    """Case-folded words without accents: the key that groups equal titles."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _SPACES.sub(" ", _NON_WORD.sub(" ", plain.replace("&", " and "))).strip()
