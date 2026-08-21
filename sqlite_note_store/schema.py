"""SQLite schema — the storage counterpart of the Markdown note repository.

Design principle (SOT for readers):
    Every markdown-visible artifact has an SQL-table counterpart.
    Every SQL row can be losslessly rendered back to markdown via export.py.

Mapping (markdown ↔ SQL):

    SQLite is the store of record; "file" is only the exported shape.
    In the DB, a markdown file `<category>/<slug>.md` is one row in
    `groups` — a thematic container of similar entries (exported as
    one .md file). YAML front matter fields (title, tags, dirty, created,
    updated) become columns; the `slug` column reconstructs the filename;
    `path = category + '/' + slug + '.md'` is UNIQUE.

    An `## <header>` block inside that group ↔ one row in `entries`, ordered
      by `order_index` (the position inside the group, ascending). Inline
      metadata suffixes `{last_used: ...}` and `{comments: [...]}` become
      real columns/JSON.

    A batch inside `cold-storage/<YYYY-MM-DD[-NN]>.md` is a plain-markdown
      archive of moved entries (one row in `cold_batches` — a time-queue
      batch, not a topical group). Cold entries live in `cold_entries`, one
      row per `## <header>` block. They preserve their original category (via
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
    # groups: one row per active group — the thematic container of
    # similar entries, exported as a single markdown file.
    #
    #   path      — 'category/slug.md' — matches on-disk relative path, UNIQUE.
    #   category  — first path segment ('uncategorized' when absent).
    #   slug      — filename minus '.md' (drives file naming on export).
    #   title     — YAML `title:` (falls back to slug on export if empty).
    #   tags      — JSON-encoded list of strings (mirrors YAML `tags: [...]`).
    #   dirty     — 0/1, mirrors YAML `dirty:` — cleared by note_rewrite.
    #   created/updated — ISO 8601 UTC, mirrors YAML `created:` / `updated:`.
    """
    CREATE TABLE IF NOT EXISTS groups (
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
    "CREATE INDEX IF NOT EXISTS idx_groups_category ON groups(category)",
    "CREATE INDEX IF NOT EXISTS idx_groups_dirty ON groups(dirty) WHERE dirty = 1",
    # entries: one row per '## <header>' section inside a group.
    #
    #   group_id    — FK to groups.id, CASCADE on delete so removing a group
    #                 nukes its entries in one shot.
    #   header      — the '## <text>' heading, without decorations.
    #   content     — the body between this heading and the next (trimmed).
    #   last_used   — ISO 8601 UTC or NULL. Written by note_use; drives
    #                 cold-storage eviction; rendered inline as {last_used: …}.
    #   comments    — JSON list of {type, text, timestamp}. Mirrors the
    #                 inline `{comments: [...]}` suffix in the markdown header.
    #   order_index — position inside the group (0-based). Preserves the
    #                 in-group ordering the LLM chose when rewriting.
    """
    CREATE TABLE IF NOT EXISTS entries (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      group_id    INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
      header      TEXT NOT NULL,
      content     TEXT NOT NULL,
      last_used   TEXT,
      comments    TEXT NOT NULL DEFAULT '[]',
      order_index INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entries_group ON entries(group_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_entries_last_used ON entries(last_used)",
    # cold_batches: one row per archive batch inside cold-storage/ — a
    # time-queue batch named by creation date, NOT a topical group.
    #
    #   filename — 'YYYY-MM-DD.md' or 'YYYY-MM-DD-NN.md'. UNIQUE.
    #   created  — ISO 8601 UTC — cold-storage sort key for eviction (oldest
    #              batch wins). We use created_at rather than filename parse
    #              so timezone-shifted filenames still order correctly.
    """
    CREATE TABLE IF NOT EXISTS cold_batches (
      id       INTEGER PRIMARY KEY AUTOINCREMENT,
      filename TEXT NOT NULL UNIQUE,
      created  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cold_batches_created ON cold_batches(created)",
    # cold_entries: one row per '## <header>' block inside a cold batch.
    #
    #   cold_batch_id      — which archive batch this lives in (drives export).
    #   header/content     — same shape as entries.
    #   last_used          — preserved from the active entry at move time.
    #   original_category  — where it lived before eviction; used when
    #                        note_recall wants to hint the LLM where to
    #                        re-file the content. Cold storage itself never
    #                        cares about it.
    #   order_index        — position inside the cold batch, ascending.
    """
    CREATE TABLE IF NOT EXISTS cold_entries (
      id                 INTEGER PRIMARY KEY AUTOINCREMENT,
      cold_batch_id      INTEGER NOT NULL REFERENCES cold_batches(id) ON DELETE CASCADE,
      header             TEXT NOT NULL,
      content            TEXT NOT NULL,
      last_used          TEXT,
      original_category  TEXT,
      order_index        INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cold_entries_batch ON cold_entries(cold_batch_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_cold_entries_header ON cold_entries(header)",
    # entries_fts: FTS5 virtual table shadowing the searchable columns.
    # The shadow rows are maintained explicitly by storage.py (not by SQL
    # triggers — see storage._fts_*) so every write path is auditable.
    # We keep it as a regular (non-contentless) FTS5 table so DELETE ops
    # work on every SQLite build we ship against. The index stores its
    # own copy of the tokens; storage overhead is bounded by note volume
    # (thousands of entries, ~KB each) and buys us search speed with no
    # migration risk.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
      header, content, category, group_title, group_path,
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

    The plugin is pre-release: the DDL list IS the current shape and there
    is no legacy-DB upgrade path. When the schema changes, edit
    _DDL_STATEMENTS in place and bump SCHEMA_VERSION for observability.
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
