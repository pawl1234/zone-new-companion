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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Search hits returned at most; one hit per item and account.
RESULT_LIMIT = 500

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


@dataclass(slots=True)
class CatalogItem:
    """One stream as downloaded from a server."""

    kind: str  # "live" | "movies" | "series"
    provider_id: str
    category_id: str
    name: str
    extension: str = ""
    icon: str = ""
    added_at: int = 0


@dataclass(slots=True)
class SearchHit:
    """A matching stream on one account."""

    account_key: str
    account_label: str
    kind: str
    provider_id: str
    name: str
    category: str
    extension: str
    icon: str
    added_at: int


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

    def sync_accounts(self, accounts: Iterable[CatalogAccount]) -> None:
        """Mirror the working-accounts list; removed accounts and their orphaned data go."""
        rows = [(a.key, a.portal_type, a.label, int(a.alive), int(a.starred)) for a in accounts]
        with self._write_lock:
            connection = self._conn()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("CREATE TEMP TABLE IF NOT EXISTS keep(key TEXT PRIMARY KEY)")
                connection.execute("DELETE FROM keep")
                connection.executemany("INSERT OR IGNORE INTO keep VALUES (?)", [(row[0],) for row in rows])
                connection.execute("DELETE FROM account WHERE key NOT IN (SELECT key FROM keep)")
                connection.executemany(
                    "INSERT INTO account(key, portal_type, label, alive, starred) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET portal_type = excluded.portal_type, "
                    "label = excluded.label, alive = excluded.alive, starred = excluded.starred",
                    rows,
                )
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

    def accounts_needing_index(self, max_age_seconds: float, portal_types: tuple[str, ...]) -> list[str]:
        """Keys of alive accounts with no catalog yet or an outdated one."""
        marks = ",".join("?" for _ in portal_types)
        rows = self._conn().execute(
            f"SELECT a.key FROM account a LEFT JOIN source s ON s.id = a.source_id "
            f"WHERE a.alive = 1 AND a.portal_type IN ({marks}) "
            f"AND (s.id IS NULL OR s.indexed_at < ?) ORDER BY a.starred DESC",
            (*portal_types, time.time() - max_age_seconds),
        ).fetchall()
        return [row[0] for row in rows]

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
                    "INSERT INTO item(source_id, kind, provider_id, category_id, name, extension, icon, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (source_id, i.kind, i.provider_id, i.category_id, i.name, i.extension, i.icon, i.added_at)
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

    def search(
        self,
        text: str,
        *,
        kind: str | None = None,
        account_key: str | None = None,
        starred_only: bool = False,
        limit: int = RESULT_LIMIT,
    ) -> list[SearchHit]:
        """Streams whose name contains every word of ``text`` (as word prefixes).

        Only accounts that are currently up are searched.
        """
        words = _words(text)
        if not words:
            return []
        filters = ["a.alive = 1"]
        params: list[object] = []
        if self.has_fts:
            source = "item_fts f JOIN item i ON i.id = f.rowid"
            filters.insert(0, "item_fts MATCH ?")
            params.append(" ".join(f'"{word}"*' for word in words))
            order = "bm25(item_fts), i.name"
        else:
            source = "item i"
            for word in words:
                filters.append("i.name LIKE ?")
                params.append(f"%{word}%")
            order = "length(i.name), i.name"
        if kind:
            filters.append("i.kind = ?")
            params.append(kind)
        if account_key:
            filters.append("a.key = ?")
            params.append(account_key)
        if starred_only:
            filters.append("a.starred = 1")
        params.append(limit)
        rows = self._conn().execute(
            f"SELECT a.key, a.label, i.kind, i.provider_id, i.name, COALESCE(c.name, ''), "
            f"i.extension, i.icon, i.added_at "
            f"FROM {source} "
            f"JOIN account a ON a.source_id = i.source_id "
            f"LEFT JOIN category c ON c.source_id = i.source_id AND c.kind = i.kind "
            f"AND c.provider_id = i.category_id "
            f"WHERE {' AND '.join(filters)} ORDER BY {order} LIMIT ?",
            params,
        ).fetchall()
        return [SearchHit(*row) for row in rows]

    def stats(self) -> CatalogStats:
        connection = self._conn()
        accounts, indexed = connection.execute(
            "SELECT COUNT(*), COUNT(source_id) FROM account WHERE alive = 1",
        ).fetchone()
        sources, items = connection.execute("SELECT COUNT(*), COALESCE(SUM(item_count), 0) FROM source").fetchone()
        return CatalogStats(accounts=accounts, indexed_accounts=indexed, sources=sources, items=items)


def _words(text: str) -> list[str]:
    """Search words with FTS syntax characters stripped."""
    return [word for word in re.split(r"[^\w]+", text.casefold()) if word]
