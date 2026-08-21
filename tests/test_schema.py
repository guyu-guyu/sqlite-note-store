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
            "groups",
            "entries",
            "cold_batches",
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
            "INSERT INTO groups(path, category, slug, title, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("cat/foo", "cat", "foo", "Foo", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
        conn2.commit()
    finally:
        conn2.close()

    conn3 = schema.connect(tmp_path)
    try:
        count = conn3.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        assert count == 1
    finally:
        conn3.close()


def test_foreign_keys_cascade_on_group_delete(tmp_path):
    """entries must vanish when their parent group row is deleted.

    This is the promise that lets note_rewrite(entries=[]) drop a whole
    group with a single DELETE — if PRAGMA foreign_keys weren't ON, the
    ON DELETE CASCADE clause in the DDL would be silently ignored.
    """
    conn = schema.connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO groups(path, category, slug, title, created, updated) "
            "VALUES ('cat/x', 'cat', 'x', 'X', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')"
        )
        group_id = conn.execute("SELECT id FROM groups").fetchone()[0]
        conn.execute(
            "INSERT INTO entries(group_id, header, content, order_index) "
            "VALUES (?, 'h', 'c', 0)",
            (group_id,),
        )
        conn.commit()

        conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        conn.commit()

        remaining = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        assert remaining == 0, "cascade did not fire — foreign_keys off?"
    finally:
        conn.close()
