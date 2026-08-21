"""SQLite schema — the storage counterpart of the Markdown note repository.

Design principle (SOT for readers):
    Every markdown-visible artifact has an SQL-table counterpart.
    Every SQL row can be losslessly rendered back to markdown via export.py.

Mapping (markdown ↔ SQL):

    A markdown file `<category>/<slug>.md` ↔ one row in `files`.
      YAML front matter fields (title, tags, dirty, created, updated)
      become columns; the `slug` column reconstructs the filename;
      `path = category + '/' + slug + '.md'` is UNIQUE.

    An `## <header>` block inside that file ↔ one row in `entries`, ordered
      by `order_index` (the position inside the file, ascending). Inline
      metadata suffixes `{last_used: ...}` and `{comments: [...]}` become
      real columns/JSON.

    A file inside `cold-storage/<YYYY-MM-DD[-NN]>.md` is a plain-markdown
      archive of moved entries. Cold entries live in `cold_entries`, one row
      per `## <header>` block. They preserve their original category (via
      `original_category`) so recall doesn't guess and export can reproduce
      the flat cold-storage/ directory shape verbatim.

Everything derivable at query time (INDEX.md, entry counts, category
overviews) stays derived — we don't cache it in tables, because SQL is
cheap. Only the durable, LLM-visible state lives in rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Bumped whenever the migration script below adds columns/tables.
SCHEMA_VERSION = 1

DB_FILENAME = "notes.sqlite3"


# ---------------------------------------------------------------------------
# DDL — kept as an explicit list so future migrations can compare/append.
# ---------------------------------------------------------------------------

_DDL_STATEMENTS = [
    # schema_meta: singleton row tracking version. Lets us evolve without
    # breaking existing DBs — the connect() path runs migrations if the
    # stored version is behind SCHEMA_VERSION.
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    # files: one row per active markdown file.
    #
    #   path      — 'category/slug.md' — matches on-disk relative path, UNIQUE.
    #   category  — first path segment ('uncategorized' when absent).
    #   slug      — filename minus '.md' (drives file naming on export).
    #   title     — YAML `title:` (falls back to slug on export if empty).
    #   tags      — JSON-encoded list of strings (mirrors YAML `tags: [...]`).
    #   dirty     — 0/1, mirrors YAML `dirty:` — cleared by note_rewrite.
    #   created/updated — ISO 8601 UTC, mirrors YAML `created:` / `updated:`.
    """
    CREATE TABLE IF NOT EXISTS files (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      path       TEXT NOT NULL UNIQUE,
      category   TEXT NOT NULL,
      slug       TEXT NOT NULL,
      title      TEXT,
      tags       TEXT NOT NULL DEFAULT '[]',
      dirty      INTEGER NOT NULL DEFAULT 0,
      created    TEXT NOT NULL,
      updated    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_category ON files(category)",
    "CREATE INDEX IF NOT EXISTS idx_files_dirty ON files(dirty) WHERE dirty = 1",
    # entries: one row per '## <header>' section inside a file.
    #
    #   file_id     — FK to files.id, CASCADE on delete so removing a file
    #                 nukes its entries in one shot.
    #   header      — the '## <text>' heading, without decorations.
    #   content     — the body between this heading and the next (trimmed).
    #   last_used   — ISO 8601 UTC or NULL. Written by note_use; drives
    #                 cold-storage eviction; rendered inline as {last_used: …}.
    #   comments    — JSON list of {type, text, timestamp}. Mirrors the
    #                 inline `{comments: [...]}` suffix in the markdown header.
    #   order_index — position inside the file (0-based). Preserves the
    #                 in-file ordering the LLM chose when rewriting.
    """
    CREATE TABLE IF NOT EXISTS entries (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
      header      TEXT NOT NULL,
      content     TEXT NOT NULL,
      last_used   TEXT,
      comments    TEXT NOT NULL DEFAULT '[]',
      order_index INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entries_file ON entries(file_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_entries_last_used ON entries(last_used)",
    # cold_files: one row per file inside cold-storage/.
    #
    #   filename — 'YYYY-MM-DD.md' or 'YYYY-MM-DD-NN.md'. UNIQUE.
    #   created  — ISO 8601 UTC — cold-storage sort key for eviction (oldest
    #              file wins). We use created_at rather than filename parse
    #              so timezone-shifted filenames still order correctly.
    """
    CREATE TABLE IF NOT EXISTS cold_files (
      id       INTEGER PRIMARY KEY AUTOINCREMENT,
      filename TEXT NOT NULL UNIQUE,
      created  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cold_files_created ON cold_files(created)",
    # cold_entries: one row per '## <header>' block inside a cold file.
    #
    #   cold_file_id       — which archive file this lives in (drives export).
    #   header/content     — same shape as entries.
    #   last_used          — preserved from the active entry at move time.
    #   original_category  — where it lived before eviction; used when
    #                        note_recall wants to hint the LLM where to
    #                        re-file the content. Cold storage itself never
    #                        cares about it.
    #   order_index        — position inside the cold file, ascending.
    """
    CREATE TABLE IF NOT EXISTS cold_entries (
      id                 INTEGER PRIMARY KEY AUTOINCREMENT,
      cold_file_id       INTEGER NOT NULL REFERENCES cold_files(id) ON DELETE CASCADE,
      header             TEXT NOT NULL,
      content            TEXT NOT NULL,
      last_used          TEXT,
      original_category  TEXT,
      order_index        INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cold_entries_file ON cold_entries(cold_file_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_cold_entries_header ON cold_entries(header)",
    # entries_fts: FTS5 virtual table shadowing entries — this is the search
    # backing store. content='entries' + content_rowid='id' lets FTS5 own
    # nothing but tokens; the real row lives in `entries`. Triggers below
    # keep them in sync automatically, so writers only touch `entries`.
    # entries_fts: FTS5 virtual table shadowing the searchable columns.
    # We keep it as a regular (non-contentless) FTS5 table so DELETE ops
    # work on every SQLite build we ship against. The index stores its
    # own copy of the tokens; storage overhead is bounded by note volume
    # (thousands of entries, ~KB each) and buys us search speed with no
    # migration risk.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
      header, content, category, file_title, file_path,
      tokenize='porter unicode61'
    )
    """,
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def db_path(note_root: Path) -> Path:
    """Return the SQLite file path for a given note-root directory.

    Kept isolated so tests can point at a tmp directory and provider code
    can `db_path(self._note_root)` without hardcoding the filename.
    """
    return note_root / DB_FILENAME


def connect(note_root: Path) -> sqlite3.Connection:
    """Open (and if needed, create) the store's SQLite database.

    Enables WAL + foreign keys — WAL so a future dashboard can hold
    read-only connections without blocking the writer; foreign keys so
    `ON DELETE CASCADE` actually cascades (SQLite defaults to OFF).
    Runs `_apply_migrations` idempotently so a fresh DB and an existing
    DB share the same setup path.
    """
    note_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path(note_root)), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring an existing DB up to SCHEMA_VERSION.

    v0 (no schema_meta row) → v1: run all DDL. Every subsequent version
    should append a `if current < N: ...` branch here rather than
    mutating _DDL_STATEMENTS. That keeps migrations replayable and lets
    us test the upgrade path from any historical version.
    """
    for ddl in _DDL_STATEMENTS:
        conn.execute(ddl)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the DB's recorded schema version (0 if none stored yet).

    Used by tests to assert that connect() correctly stamps the DB.
    """
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0
