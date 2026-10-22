"""Searchable catalog of every working account (SQLite + FTS5).

The database is a cache, never the source of truth: config.json holds the
accounts and their passwords, this file only what their servers list. A schema
change simply rebuilds it. Accounts on the same server with the same packages
share one *source*, so a catalog is downloaded and stored once.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from zone_new_companion.services.title_parser import QUALITY_RANK

LOGGER = logging.getLogger(__name__)

# 2: parsed titles, languages and account health for grouping and filters.
SCHEMA_VERSION = 2
# Matching rows (stream x account) read per search, best first; they are
# grouped into at most GROUP_LIMIT titles.
ROW_LIMIT = 5000
GROUP_LIMIT = 300
SORTS = ("relevance", "year", "added", "accounts", "title")

_SCHEMA = """
CREATE TABLE source (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT UNIQUE NOT NULL,
    server TEXT NOT NULL,
    indexed_at REAL NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE account (
    key TEXT PRIMARY KEY,
    portal_type TEXT NOT NULL,
    label TEXT NOT NULL,
    alive INTEGER NOT NULL,
    starred INTEGER NOT NULL,
    stream_ok INTEGER,
    expires_at REAL,
    max_conn INTEGER,
    active_conn INTEGER,
    source_id INTEGER REFERENCES source(id) ON DELETE SET NULL
);
CREATE INDEX account_source ON account(source_id);
CREATE TABLE category (
    source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (source_id, kind, provider_id)
) WITHOUT ROWID;
CREATE TABLE item (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    category_id TEXT NOT NULL,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    year INTEGER NOT NULL,
    quality TEXT NOT NULL,
    quality_rank INTEGER NOT NULL,
    -- Country codes as " DE MULTI " ('' = untagged), see country_filter.
    item_langs TEXT NOT NULL,
    cat_langs TEXT NOT NULL,
    extension TEXT NOT NULL,
    icon TEXT NOT NULL,
    added_at INTEGER NOT NULL
);
CREATE INDEX item_source ON item(source_id);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE item_fts USING fts5(
    name, content='item', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3'
);
CREATE TRIGGER item_ai AFTER INSERT ON item BEGIN
    INSERT INTO item_fts(rowid, name) VALUES (new.id, new.name);
END;
CREATE TRIGGER item_ad AFTER DELETE ON item BEGIN
    INSERT INTO item_fts(item_fts, rowid, name) VALUES ('delete', old.id, old.name);
END;
"""


@dataclass(slots=True)
class CatalogAccount:
    """What the catalog knows about one working account (no credentials)."""

    key: str
    portal_type: str
    label: str
    alive: bool
    starred: bool
    stream_ok: bool | None = None
    expires_at: float | None = None  # None: unlimited or unknown
    max_conn: int | None = None
    active_conn: int | None = None


@dataclass(slots=True)
class CatalogItem:
    """One stream as downloaded from a server, with its parsed name."""

    kind: str  # "live" | "movies" | "series"
    provider_id: str
    category_id: str
    name: str
    title: str = ""
    title_norm: str = ""
    year: int = 0
    quality: str = ""
    item_langs: frozenset[str] = frozenset()
    cat_langs: frozenset[str] = frozenset()
    extension: str = ""
    icon: str = ""
    added_at: int = 0


@dataclass(slots=True)
class SearchFilters:
    """What a search is narrowed to; the defaults filter nothing."""

    kind: str | None = None
    account_key: str | None = None
    starred_only: bool = False
    # Wanted country codes (country_filter.codes_for_language); empty = all.
    languages: frozenset[str] = frozenset()
    # Hide untagged entries too, like the browse tabs' "Hide untagged".
    language_strict: bool = False
    min_quality: str = ""
    year_from: int = 0
    year_to: int = 0
    added_within_days: int = 0
    category_text: str = ""
    stream_tested_only: bool = False
    # Leave out accounts expiring sooner than this.
    valid_for_days: int = 0
    free_slots_only: bool = False
    sort: str = "relevance"


@dataclass(slots=True)
class SearchHit:
    """A matching stream on one account."""

    account_key: str
    account_label: str
    kind: str
    provider_id: str
    name: str
    title: str
    title_norm: str
    year: int
    quality: str
    languages: str  # "DE MULTI"; the item's own tags, else its category's
    category: str
    extension: str
    icon: str
    added_at: int
    starred: bool
    stream_ok: bool | None
    free_slots: bool | None  # None: the server does not say

    @property
    def play_rank(self) -> tuple:
        """Lower plays first: starred, streaming, free slot, best quality."""
        stream = {True: 0, None: 1, False: 2}[self.stream_ok]
        return (not self.starred, stream, self.free_slots is False, -QUALITY_RANK.get(self.quality, 0))


@dataclass(slots=True)
class SearchGroup:
    """One title (same kind, normalized title and year) across accounts."""

    kind: str
    title: str
    year: int
    hits: list[SearchHit] = field(default_factory=list)
    # Position of its best hit in relevance order.
    relevance: int = 0

    @property
    def best(self) -> SearchHit:
        return min(self.hits, key=lambda hit: hit.play_rank)

    @property
    def account_count(self) -> int:
        return len({hit.account_key for hit in self.hits})

    @property
    def qualities(self) -> list[str]:
        return sorted({hit.quality for hit in self.hits if hit.quality}, key=lambda q: -QUALITY_RANK[q])

    @property
    def languages(self) -> list[str]:
        return sorted({code for hit in self.hits for code in hit.languages.split()})

    @property
    def added_at(self) -> int:
        return max(hit.added_at for hit in self.hits)


@dataclass(slots=True)
class SearchResult:
    groups: list[SearchGroup]
    # More matched than was read or shown: typing more narrows it down.
    truncated: bool = False


@dataclass(slots=True)
class CatalogStats:
    accounts: int
    indexed_accounts: int
    sources: int
    items: int


class CatalogStore:
    """Thread-safe access to catalog.db; each thread gets its own connection."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()
        # One writer at a time; SQLite would serialize anyway, this avoids busy waits.
        self._write_lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.has_fts = self._open_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        # Lets purged catalogs give disk space back; only takes effect on a new
        # file, so it must come before WAL writes the header.
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._connect()
            self._local.connection = connection
        return connection

    def _open_schema(self) -> bool:
        """Create the tables, rebuilding the file on a schema change; whether FTS5 works."""
        connection = self._conn()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            LOGGER.info("Catalog schema %s is outdated, rebuilding", version)
            connection.close()
            self._local.connection = None
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self._path}{suffix}").unlink(missing_ok=True)
            connection = self._conn()
            version = 0
        if version == 0:
            connection.executescript(_SCHEMA)
            try:
                connection.executescript(_FTS_SCHEMA)
            except sqlite3.OperationalError:
                LOGGER.warning("SQLite without FTS5, catalog search falls back to LIKE")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'item_fts'",
        ).fetchone() is not None

    # Accounts -----------------------------------------------------------------

    def sync_accounts(self, accounts: Iterable[CatalogAccount], unindexed: Iterable[str] = ()) -> None:
        """Mirror the working-accounts list; removed accounts and their orphaned data go.

        ``unindexed`` accounts (beyond the index limit) lose their catalog link,
        so they are not searched and their data goes unless another account
        shares it.
        """
        unlink = [(key,) for key in unindexed]
        rows = [
            (
                a.key, a.portal_type, a.label, int(a.alive), int(a.starred),
                None if a.stream_ok is None else int(a.stream_ok), a.expires_at, a.max_conn, a.active_conn,
            )
            for a in accounts
        ]
        with self._write_lock:
            connection = self._conn()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("CREATE TEMP TABLE IF NOT EXISTS keep(key TEXT PRIMARY KEY)")
                connection.execute("DELETE FROM keep")
                connection.executemany("INSERT OR IGNORE INTO keep VALUES (?)", [(row[0],) for row in rows])
                connection.execute("DELETE FROM account WHERE key NOT IN (SELECT key FROM keep)")
                connection.executemany(
                    "INSERT INTO account(key, portal_type, label, alive, starred, stream_ok, expires_at, "
                    "max_conn, active_conn) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET portal_type = excluded.portal_type, "
                    "label = excluded.label, alive = excluded.alive, starred = excluded.starred, "
                    "stream_ok = excluded.stream_ok, expires_at = excluded.expires_at, "
                    "max_conn = excluded.max_conn, active_conn = excluded.active_conn",
                    rows,
                )
                connection.executemany("UPDATE account SET source_id = NULL WHERE key = ?", unlink)
                purged = self._purge_orphans(connection)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            if purged:
                self._compact(connection)

    def _compact(self, connection: sqlite3.Connection) -> None:
        """Give deleted catalogs' space back (call with the write lock held).

        FTS5 only marks deleted rows; optimize merges them out of the index.
        """
        if self.has_fts:
            connection.execute("INSERT INTO item_fts(item_fts) VALUES ('optimize')")
        # Each step frees a page; fetchall runs it to the end.
        connection.execute("PRAGMA incremental_vacuum").fetchall()

    @staticmethod
    def _purge_orphans(connection: sqlite3.Connection) -> int:
        """Drop catalogs no account uses any more; how many went."""
        return connection.execute(
            "DELETE FROM source WHERE id NOT IN (SELECT source_id FROM account WHERE source_id IS NOT NULL)",
        ).rowcount

    def needing_index(self, keys: list[str], max_age_seconds: float) -> list[str]:
        """Of ``keys`` (in order), those with no catalog yet or an outdated one."""
        info = self.index_info()
        cutoff = time.time() - max_age_seconds
        return [key for key in keys if key not in info or info[key][0] < cutoff]

    def index_info(self) -> dict[str, tuple[float, int]]:
        """(indexed_at, stream count) per account that has a catalog."""
        rows = self._conn().execute(
            "SELECT a.key, s.indexed_at, s.item_count FROM account a JOIN source s ON s.id = a.source_id",
        ).fetchall()
        return {key: (indexed_at, count) for key, indexed_at, count in rows}

    # Sources ------------------------------------------------------------------

    def fresh_source(self, fingerprint: str, max_age_seconds: float) -> int | None:
        """The id of an indexed source with this fingerprint younger than max_age."""
        row = self._conn().execute(
            "SELECT id FROM source WHERE fingerprint = ? AND indexed_at >= ?",
            (fingerprint, time.time() - max_age_seconds),
        ).fetchone()
        return row[0] if row else None

    def link_account(self, account_key: str, source_id: int) -> None:
        with self._write_lock:
            connection = self._conn()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("UPDATE account SET source_id = ? WHERE key = ?", (source_id, account_key))
                purged = self._purge_orphans(connection)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            if purged:
                self._compact(connection)

    def replace_source(
        self,
        account_key: str,
        fingerprint: str,
        server: str,
        categories: Iterable[tuple[str, str, str]],
        items: Iterable[CatalogItem],
    ) -> int:
        """Store a freshly downloaded catalog and link the account to it.

        One transaction: searches keep seeing the old catalog until it commits.
        ``categories`` are (kind, provider_id, name).
        """
        with self._write_lock:
            connection = self._conn()
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("SELECT 1 FROM account WHERE key = ?", (account_key,)).fetchone() is None:
                    # Removed while it was downloading: nothing to keep.
                    connection.execute("ROLLBACK")
                    return 0
                connection.execute(
                    "INSERT INTO source(fingerprint, server) VALUES (?, ?) ON CONFLICT(fingerprint) DO NOTHING",
                    (fingerprint, server),
                )
                source_id = connection.execute(
                    "SELECT id FROM source WHERE fingerprint = ?", (fingerprint,),
                ).fetchone()[0]
                connection.execute("DELETE FROM item WHERE source_id = ?", (source_id,))
                connection.execute("DELETE FROM category WHERE source_id = ?", (source_id,))
                connection.executemany(
                    "INSERT OR IGNORE INTO category(source_id, kind, provider_id, name) VALUES (?, ?, ?, ?)",
                    ((source_id, kind, provider_id, name) for kind, provider_id, name in categories),
                )
                cursor = connection.executemany(
                    "INSERT INTO item(source_id, kind, provider_id, category_id, name, title, title_norm, year, "
                    "quality, quality_rank, item_langs, cat_langs, extension, icon, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            source_id, i.kind, i.provider_id, i.category_id, i.name, i.title or i.name,
                            i.title_norm, i.year, i.quality, QUALITY_RANK.get(i.quality, 0),
                            _codes(i.item_langs), _codes(i.cat_langs), i.extension, i.icon, i.added_at,
                        )
                        for i in items
                    ),
                )
                count = cursor.rowcount
                connection.execute(
                    "UPDATE source SET indexed_at = ?, item_count = ?, server = ? WHERE id = ?",
                    (time.time(), count, server, source_id),
                )
                connection.execute("UPDATE account SET source_id = ? WHERE key = ?", (source_id, account_key))
                self._purge_orphans(connection)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            # The previous copy of this catalog (and any orphan) was deleted.
            self._compact(connection)
        return count

    # Search -------------------------------------------------------------------

    def search(self, text: str, filters: SearchFilters | None = None) -> SearchResult:
        """Titles whose stream names contain every word of ``text`` (as word prefixes).

        Only accounts that are currently up are searched. Equal titles on
        several accounts (or listed twice) come back as one group.
        """
        filters = filters or SearchFilters()
        words = _words(text)
        if not words:
            return SearchResult([])
        where = ["a.alive = 1"]
        params: list[object] = []
        if self.has_fts:
            source = "item_fts f JOIN item i ON i.id = f.rowid"
            where.insert(0, "item_fts MATCH ?")
            params.append(" ".join(f'"{word}"*' for word in words))
            order = "bm25(item_fts), i.name"
        else:
            source = "item i"
            for word in words:
                where.append("i.name LIKE ?")
                params.append(f"%{word}%")
            order = "length(i.name), i.name"
        _add_filters(filters, where, params)
        params.append(ROW_LIMIT)
        rows = self._conn().execute(
            f"SELECT a.key, a.label, i.kind, i.provider_id, i.name, i.title, i.title_norm, i.year, i.quality, "
            f"CASE WHEN i.item_langs != '' THEN i.item_langs ELSE i.cat_langs END, COALESCE(c.name, ''), "
            f"i.extension, i.icon, i.added_at, a.starred, a.stream_ok, "
            f"CASE WHEN a.max_conn > 0 THEN a.active_conn < a.max_conn END "
            f"FROM {source} "
            f"JOIN account a ON a.source_id = i.source_id "
            f"LEFT JOIN category c ON c.source_id = i.source_id AND c.kind = i.kind "
            f"AND c.provider_id = i.category_id "
            f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
            params,
        ).fetchall()
        hits = [
            SearchHit(
                *row[:9], row[9].strip(), *row[10:14], bool(row[14]),
                None if row[15] is None else bool(row[15]), None if row[16] is None else bool(row[16]),
            )
            for row in rows
        ]
        groups = _sort_groups(_group(hits), filters.sort)
        return SearchResult(groups[:GROUP_LIMIT], truncated=len(rows) >= ROW_LIMIT or len(groups) > GROUP_LIMIT)

    # Maintenance --------------------------------------------------------------

    def size_bytes(self) -> int:
        """Disk space of the database, write-ahead log included."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(f"{self._path}{suffix}").stat().st_size
            except OSError:
                pass
        return total

    def clear(self) -> None:
        """Delete every catalog and shrink the file; the accounts stay.

        Done in SQL, not by deleting the file: other threads keep connections open.
        """
        with self._write_lock:
            connection = self._conn()
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Cascades to categories and items; accounts lose their link.
                connection.execute("DELETE FROM source")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            if self.has_fts:
                connection.execute("INSERT INTO item_fts(item_fts) VALUES ('rebuild')")
            connection.execute("VACUUM")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def stats(self) -> CatalogStats:
        connection = self._conn()
        accounts, indexed = connection.execute(
            "SELECT COUNT(*), COUNT(source_id) FROM account WHERE alive = 1",
        ).fetchone()
        sources, items = connection.execute("SELECT COUNT(*), COALESCE(SUM(item_count), 0) FROM source").fetchone()
        return CatalogStats(accounts=accounts, indexed_accounts=indexed, sources=sources, items=items)


def _codes(codes: frozenset[str]) -> str:
    """Country codes as stored: " DE MULTI " (spaces make instr() match whole codes)."""
    return f" {' '.join(sorted(codes))} " if codes else ""


def _add_filters(filters: SearchFilters, where: list[str], params: list[object]) -> None:
    """Append the SQL conditions (and their parameters) for ``filters``."""
    now = time.time()
    if filters.kind:
        where.append("i.kind = ?")
        params.append(filters.kind)
    if filters.account_key:
        where.append("a.key = ?")
        params.append(filters.account_key)
    if filters.starred_only:
        where.append("a.starred = 1")
    if filters.languages:
        # country_filter.item_visible: a wanted category tag wins; else the
        # item's own tags decide; else the category's; untagged only when not strict.
        wanted = sorted(filters.languages)
        cat_hit = "(" + " OR ".join("instr(i.cat_langs, ?)" for _ in wanted) + ")"
        own_hit = "(" + " OR ".join("instr(i.item_langs, ?)" for _ in wanted) + ")"
        where.append(
            f"({cat_hit} OR (i.item_langs != '' AND (instr(i.item_langs, ' MULTI ') OR {own_hit})) "
            f"OR (i.item_langs = '' AND ((i.cat_langs != '' AND instr(i.cat_langs, ' MULTI ')) "
            f"OR (i.cat_langs = '' AND ?))))",
        )
        params.extend(f" {code} " for code in wanted)
        params.extend(f" {code} " for code in wanted)
        params.append(0 if filters.language_strict else 1)
    if filters.min_quality in QUALITY_RANK:
        where.append("i.quality_rank >= ?")
        params.append(QUALITY_RANK[filters.min_quality])
    if filters.year_from:
        where.append("i.year >= ?")
        params.append(filters.year_from)
    if filters.year_to:
        where.append("i.year BETWEEN 1 AND ?")
        params.append(filters.year_to)
    if filters.added_within_days:
        where.append("i.added_at >= ?")
        params.append(int(now - filters.added_within_days * 86400))
    if filters.category_text.strip():
        where.append("c.name LIKE ?")
        params.append(f"%{filters.category_text.strip()}%")
    if filters.stream_tested_only:
        where.append("a.stream_ok = 1")
    if filters.valid_for_days:
        where.append("(a.expires_at IS NULL OR a.expires_at >= ?)")
        params.append(now + filters.valid_for_days * 86400)
    if filters.free_slots_only:
        where.append("a.max_conn > 0 AND a.active_conn < a.max_conn")


def _group(hits: list[SearchHit]) -> list[SearchGroup]:
    """Hits (best first) into titles; an undated copy joins the one dated group of its title."""
    groups: dict[tuple[str, str, int], SearchGroup] = {}
    for position, hit in enumerate(hits):
        key = (hit.kind, hit.title_norm or hit.name.casefold(), hit.year)
        group = groups.get(key)
        if group is None:
            group = groups[key] = SearchGroup(kind=hit.kind, title=hit.title, year=hit.year, relevance=position)
        group.hits.append(hit)
    dated: dict[tuple[str, str], list[SearchGroup]] = {}
    for (kind, norm, year), group in groups.items():
        if year:
            dated.setdefault((kind, norm), []).append(group)
    for (kind, norm, year), group in list(groups.items()):
        targets = dated.get((kind, norm), [])
        if not year and len(targets) == 1:
            target = targets[0]
            target.hits.extend(group.hits)
            target.relevance = min(target.relevance, group.relevance)
            del groups[(kind, norm, year)]
    return list(groups.values())


def _sort_groups(groups: list[SearchGroup], sort: str) -> list[SearchGroup]:
    by_relevance = sorted(groups, key=lambda group: group.relevance)
    if sort == "year":
        return sorted(by_relevance, key=lambda group: (group.year == 0, -group.year))
    if sort == "added":
        return sorted(by_relevance, key=lambda group: -group.added_at)
    if sort == "accounts":
        return sorted(by_relevance, key=lambda group: -group.account_count)
    if sort == "title":
        return sorted(by_relevance, key=lambda group: group.title.casefold())
    return by_relevance


def _words(text: str) -> list[str]:
    """Search words with FTS syntax characters stripped."""
    return [word for word in re.split(r"[^\w]+", text.casefold()) if word]
