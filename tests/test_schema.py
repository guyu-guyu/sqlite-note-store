"""Tests for schema.connect / migrations / schema_version.

We keep schema tests below the storage-layer tests because the store
would fail catastrophically if these regress — so this is the
foundation smoke suite.
"""

from __future__ import annotations

from sqlite_note_store import schema


def test_connect_creates_tables_and_stamps_version(tmp_path):
    conn = schema.connect(tmp_path)
    try:
        # All expected tables + FTS virtual table exist.
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual') ORDER BY name"
        ).fetchall()
        table_names = {r["name"] for r in table_rows}
        for expected in {
            "schema_meta",
            "files",
            "entries",
            "cold_files",
            "cold_entries",
            "entries_fts",
        }:
            assert expected in table_names, f"missing table {expected}: got {table_names}"

        assert schema.schema_version(conn) == schema.SCHEMA_VERSION
    finally:
        conn.close()


def test_connect_is_idempotent(tmp_path):
    # First connect creates DB.
    conn1 = schema.connect(tmp_path)
    conn1.close()

    # Second connect on the same directory must not raise or wipe rows.
    conn2 = schema.connect(tmp_path)
    try:
        # Insert a row, close, reopen — data should survive.
        conn2.execute(
            "INSERT INTO files(path, category, slug, title, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("cat/foo.md", "cat", "foo", "Foo", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
        conn2.commit()
    finally:
        conn2.close()

    conn3 = schema.connect(tmp_path)
    try:
        count = conn3.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert count == 1
    finally:
        conn3.close()


def test_foreign_keys_cascade_on_file_delete(tmp_path):
    """entries must vanish when their parent file row is deleted.

    This is the promise that lets note_rewrite(entries=[]) drop a whole
    file with a single DELETE — if PRAGMA foreign_keys weren't ON, the
    ON DELETE CASCADE clause in the DDL would be silently ignored.
    """
    conn = schema.connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO files(path, category, slug, title, created, updated) "
            "VALUES ('cat/x.md', 'cat', 'x', 'X', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')"
        )
        file_id = conn.execute("SELECT id FROM files").fetchone()[0]
        conn.execute(
            "INSERT INTO entries(file_id, header, content, order_index) "
            "VALUES (?, 'h', 'c', 0)",
            (file_id,),
        )
        conn.commit()

        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()

        remaining = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        assert remaining == 0, "cascade did not fire — foreign_keys off?"
    finally:
        conn.close()
