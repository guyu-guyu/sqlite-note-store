"""Storage layer — the only module that reads and writes SQLite rows.

Design contract
---------------
1. Every mutation is atomic: use `with conn:` context so a failure
   half-way through never leaves entries orphaned from their group row.
2. The FTS shadow table (`entries_fts`) is maintained here — not by
   triggers — because FTS5 external-content sync is subtle and
   easier to reason about when writes go through a single function.
3. Consumers (provider.py, export.py) never write raw SQL. If they
   need a query, we add a named function here. That keeps the shape
   contract enforceable and the surface area easy to audit.

Terminology
-----------
The DB's atomic container is a `group` — a thematic set of similar
entries (exported as one .md file). Cold storage holds time-queue
`batches`, not topical groups. See schema.py for the full mapping.

Reference (read-only): markdown-note-store-plugin/…/__init__.py.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import markdown_io


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass
class GroupRow:
    """One row from the `groups` table, hydrated to Python-native types."""

    id: int
    path: str
    category: str
    slug: str
    title: str
    tags: list[str]
    dirty: bool
    created: str
    updated: str


@dataclass
class EntryRow:
    """One row from the `entries` table, hydrated to Python-native types."""

    id: int
    group_id: int
    header: str
    content: str
    last_used: str | None
    comments: list[dict[str, Any]]
    order_index: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """UTC ISO 8601 with timezone — matches the reference plugin's format."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_group(row: sqlite3.Row) -> GroupRow:
    return GroupRow(
        id=row["id"],
        path=row["path"],
        category=row["category"],
        slug=row["slug"],
        title=row["title"] or "",
        tags=json.loads(row["tags"] or "[]"),
        dirty=bool(row["dirty"]),
        created=row["created"],
        updated=row["updated"],
    )


def _row_to_entry(row: sqlite3.Row) -> EntryRow:
    return EntryRow(
        id=row["id"],
        group_id=row["group_id"],
        header=row["header"],
        content=row["content"],
        last_used=row["last_used"],
        comments=json.loads(row["comments"] or "[]"),
        order_index=row["order_index"],
    )


_SLUG_TRANS = str.maketrans({
    " ": "-",
    "/": "-",
    "\\": "-",
    ":": "-",
    "?": "",
    "*": "",
    '"': "",
    "<": "",
    ">": "",
    "|": "",
})


def slugify(title: str, fallback: str = "note") -> str:
    """Derive a filesystem-safe slug from a title.

    Kept simple and unicode-friendly — Chinese titles stay readable
    (卡牌BR → 卡牌BR), matching the reference plugin's filenames.
    """
    s = unicodedata.normalize("NFC", title or "").strip()
    s = s.translate(_SLUG_TRANS)
    s = re.sub(r"-+", "-", s).strip("-.")
    return s or fallback


def build_path(category: str, slug: str) -> str:
    """Assemble the canonical 'category/slug.md' path used across the store."""
    return f"{category}/{slug}.md"


# ---------------------------------------------------------------------------
# FTS shadow — invoked from every write path so callers can't forget.
# ---------------------------------------------------------------------------


def _fts_delete_by_entry_id(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry_id,))


def _fts_insert_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    row = conn.execute(
        """
        SELECT e.id, e.header, e.content, g.category, g.title, g.path
          FROM entries e
          JOIN groups  g ON g.id = e.group_id
         WHERE e.id = ?
        """,
        (entry_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO entries_fts(rowid, header, content, category, group_title, group_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], row["header"], row["content"], row["category"],
         row["title"] or "", row["path"]),
    )


def _fts_rebuild_for_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Wipe and re-index all entries of a single group.

    Used after replacing a group's entry set — cheaper than diffing.
    """
    ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM entries WHERE group_id = ?", (group_id,)
        )
    ]
    for eid in ids:
        _fts_delete_by_entry_id(conn, eid)
        _fts_insert_entry(conn, eid)


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------


def get_group_by_path(conn: sqlite3.Connection, path: str) -> GroupRow | None:
    row = conn.execute(
        "SELECT * FROM groups WHERE path = ?", (path,)
    ).fetchone()
    return _row_to_group(row) if row else None


def list_groups(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    dirty_only: bool = False,
) -> list[GroupRow]:
    sql = "SELECT * FROM groups"
    clauses: list[str] = []
    params: list[Any] = []
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if dirty_only:
        clauses.append("dirty = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY category, slug"
    return [_row_to_group(r) for r in conn.execute(sql, params)]


def list_entries(conn: sqlite3.Connection, group_id: int) -> list[EntryRow]:
    rows = conn.execute(
        "SELECT * FROM entries WHERE group_id = ? ORDER BY order_index, id",
        (group_id,),
    )
    return [_row_to_entry(r) for r in rows]


def find_entry(
    conn: sqlite3.Connection, group_id: int, header: str
) -> EntryRow | None:
    row = conn.execute(
        "SELECT * FROM entries WHERE group_id = ? AND header = ? LIMIT 1",
        (group_id, header),
    ).fetchone()
    return _row_to_entry(row) if row else None


def search_fts(
    conn: sqlite3.Connection, query: str, limit: int = 5
) -> list[dict[str, Any]]:
    """FTS5 match query — returns [{path, title, category, snippet}, ...].

    FTS5 treats certain punctuation (`-`, `:`, `"`, parentheses) as
    operators, so a naive user query like `crash-fix` would parse as
    NOT-token instead of a phrase. We wrap the query in double quotes
    (escaping any embedded `"`) so anything the LLM passes reaches the
    tokenizer as a literal phrase.
    """
    if not query.strip():
        return []
    safe = '"' + query.replace('"', '""') + '"'
    rows = conn.execute(
        """
        SELECT group_path AS path,
               group_title AS title,
               category,
               snippet(entries_fts, 1, '<<', '>>', '...', 64) AS snippet
          FROM entries_fts
         WHERE entries_fts MATCH ?
         ORDER BY rank
         LIMIT ?
        """,
        (safe, limit),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------------


def upsert_group(
    conn: sqlite3.Connection,
    *,
    path: str,
    category: str,
    slug: str,
    title: str,
    tags: list[str],
    dirty: bool,
    created: str | None = None,
    updated: str | None = None,
) -> int:
    """Insert or update a group row, returning the row id.

    Preserves `created` on update — the on-disk 'created' timestamp is a
    durable property of the group, not a mutation timestamp.
    """
    now = _now()
    created = created or now
    updated = updated or now
    tags_json = json.dumps(tags, ensure_ascii=False)
    existing = get_group_by_path(conn, path)
    if existing:
        conn.execute(
            "UPDATE groups SET category=?, slug=?, title=?, tags=?, dirty=?, updated=? "
            "WHERE id=?",
            (category, slug, title, tags_json, int(dirty), updated, existing.id),
        )
        return existing.id
    cur = conn.execute(
        "INSERT INTO groups(path, category, slug, title, tags, dirty, created, updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (path, category, slug, title, tags_json, int(dirty), created, updated),
    )
    return cur.lastrowid


def delete_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Delete a group and (via CASCADE) its entries; also wipe from FTS."""
    for eid in [r[0] for r in conn.execute("SELECT id FROM entries WHERE group_id=?", (group_id,))]:
        _fts_delete_by_entry_id(conn, eid)
    conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))


def append_entry(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    header: str,
    content: str,
    last_used: str | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> int:
    """Append one entry at the end of `group_id`'s ordered entry list."""
    max_row = conn.execute(
        "SELECT COALESCE(MAX(order_index), -1) FROM entries WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    next_order = (max_row[0] if max_row else -1) + 1
    cur = conn.execute(
        "INSERT INTO entries(group_id, header, content, last_used, comments, order_index) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            group_id,
            header,
            content,
            last_used,
            json.dumps(comments or [], ensure_ascii=False),
            next_order,
        ),
    )
    _fts_insert_entry(conn, cur.lastrowid)
    return cur.lastrowid


def replace_entries(
    conn: sqlite3.Connection,
    group_id: int,
    entries: Iterable[markdown_io.ParsedEntry],
) -> None:
    """Wipe and re-insert `group_id`'s entries in the given order.

    Backing operation for note_rewrite: called inside a transaction so
    the write is either fully applied or fully rolled back.
    """
    for eid in [r[0] for r in conn.execute("SELECT id FROM entries WHERE group_id=?", (group_id,))]:
        _fts_delete_by_entry_id(conn, eid)
    conn.execute("DELETE FROM entries WHERE group_id = ?", (group_id,))
    for idx, e in enumerate(entries):
        cur = conn.execute(
            "INSERT INTO entries(group_id, header, content, last_used, comments, order_index) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                group_id,
                e.header,
                e.content,
                e.last_used,
                json.dumps(e.comments or [], ensure_ascii=False),
                idx,
            ),
        )
        _fts_insert_entry(conn, cur.lastrowid)


def set_entry_last_used(
    conn: sqlite3.Connection, entry_id: int, when: str | None = None
) -> None:
    """Bump `last_used` — used by note_use to prevent cold-storage eviction."""
    conn.execute(
        "UPDATE entries SET last_used = ? WHERE id = ?",
        (when or _now(), entry_id),
    )


def append_comment(
    conn: sqlite3.Connection,
    entry_id: int,
    *,
    comment_type: str,
    comment_text: str,
    when: str | None = None,
) -> None:
    """Append a comment and mark the parent group dirty.

    Comments are ephemeral — they exist to signal the maintenance loop
    that this entry needs attention. `note_rewrite` clears them.
    """
    row = conn.execute(
        "SELECT group_id, comments FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"entry_id {entry_id} not found")
    group_id = row["group_id"]
    comments = json.loads(row["comments"] or "[]")
    comments.append({
        "type": comment_type,
        "text": comment_text,
        "timestamp": when or _now(),
    })
    conn.execute(
        "UPDATE entries SET comments = ? WHERE id = ?",
        (json.dumps(comments, ensure_ascii=False), entry_id),
    )
    conn.execute(
        "UPDATE groups SET dirty = 1, updated = ? WHERE id = ?",
        (_now(), group_id),
    )


def mark_dirty(conn: sqlite3.Connection, group_id: int, dirty: bool = True) -> None:
    conn.execute(
        "UPDATE groups SET dirty = ?, updated = ? WHERE id = ?",
        (int(dirty), _now(), group_id),
    )


# ---------------------------------------------------------------------------
# Cold storage — time-queue batches
# ---------------------------------------------------------------------------


def get_or_create_cold_batch_for_today(conn: sqlite3.Connection) -> int:
    """Return the id of today's cold-storage batch, creating one if needed.

    The reference plugin picks *newest* batch first; we match that so
    batches fill in insertion order rather than fragmenting one-per-day.
    """
    newest = conn.execute(
        "SELECT id FROM cold_batches ORDER BY created DESC LIMIT 1"
    ).fetchone()
    if newest is not None:
        return newest["id"]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _create_cold_batch(conn, date_str)


def _create_cold_batch(conn: sqlite3.Connection, date_str: str) -> int:
    """Insert a new cold batch, disambiguating collisions with '-NN' suffix."""
    base = date_str
    filename = f"{base}.md"
    suffix = 1
    while conn.execute(
        "SELECT 1 FROM cold_batches WHERE filename = ?", (filename,)
    ).fetchone():
        filename = f"{base}-{suffix:02d}.md"
        suffix += 1
    cur = conn.execute(
        "INSERT INTO cold_batches(filename, created) VALUES (?, ?)",
        (filename, _now()),
    )
    return cur.lastrowid


def rollover_cold_batch(conn: sqlite3.Connection) -> int:
    """Force-create a new cold batch — used when the current one is 'full'."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _create_cold_batch(conn, date_str)


def move_entry_to_cold(
    conn: sqlite3.Connection, entry_id: int, *, cold_batch_id: int
) -> int | None:
    """Move an active entry into a cold-storage batch.

    Returns the new cold_entries.id, or None if the entry was gone.
    Deletes the active-side row so it stops showing up in searches.
    """
    row = conn.execute(
        "SELECT e.id, e.header, e.content, e.last_used, g.category "
        "FROM entries e JOIN groups g ON g.id = e.group_id "
        "WHERE e.id = ?",
        (entry_id,),
    ).fetchone()
    if row is None:
        return None

    max_row = conn.execute(
        "SELECT COALESCE(MAX(order_index), -1) FROM cold_entries WHERE cold_batch_id = ?",
        (cold_batch_id,),
    ).fetchone()
    next_order = (max_row[0] if max_row else -1) + 1

    cur = conn.execute(
        "INSERT INTO cold_entries(cold_batch_id, header, content, last_used, "
        "original_category, order_index) VALUES (?, ?, ?, ?, ?, ?)",
        (cold_batch_id, row["header"], row["content"], row["last_used"],
         row["category"], next_order),
    )
    _fts_delete_by_entry_id(conn, entry_id)
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    return cur.lastrowid


def find_cold_entry(conn: sqlite3.Connection, header: str) -> dict[str, Any] | None:
    """Look up a cold entry by header — newest cold batch first.

    Returned shape matches the reference plugin's note_recall payload.
    """
    row = conn.execute(
        """
        SELECT ce.header, ce.content, ce.last_used, ce.original_category,
               cb.filename
          FROM cold_entries ce
          JOIN cold_batches cb ON cb.id = ce.cold_batch_id
         WHERE ce.header = ?
         ORDER BY cb.created DESC, ce.order_index ASC
         LIMIT 1
        """,
        (header,),
    ).fetchone()
    if row is None:
        return None
    return {
        "header": row["header"],
        "content": row["content"],
        "last_used": row["last_used"],
        "original_category": row["original_category"],
        "batch_filename": row["filename"],
    }


def enforce_cold_batch_limit(conn: sqlite3.Connection, max_batches: int) -> int:
    """Delete the oldest cold batches past `max_batches`. Returns count deleted."""
    rows = conn.execute(
        "SELECT id FROM cold_batches ORDER BY created ASC"
    ).fetchall()
    if len(rows) <= max_batches:
        return 0
    to_delete = rows[: len(rows) - max_batches]
    for r in to_delete:
        conn.execute("DELETE FROM cold_batches WHERE id = ?", (r["id"],))
    return len(to_delete)


def list_cold_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, filename, created FROM cold_batches ORDER BY created"
    )]


def list_cold_entries(conn: sqlite3.Connection, cold_batch_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT header, content, last_used, original_category "
        "FROM cold_entries WHERE cold_batch_id = ? ORDER BY order_index, id",
        (cold_batch_id,),
    )]


def count_active_entries(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]


def count_cold_entries(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cold_entries").fetchone()[0]


def select_stale_active_entries(
    conn: sqlite3.Connection, older_than_iso: str
) -> list[EntryRow]:
    """Entries whose last_used is older than the threshold.

    NULL last_used is treated as "never used" — these are old entries
    imported from the reference plugin that were never touched, not
    freshly created entries (note_write always sets last_used to now).
    """
    rows = conn.execute(
        "SELECT * FROM entries "
        "WHERE last_used IS NOT NULL AND last_used <= ? "
        "ORDER BY last_used",
        (older_than_iso,),
    )
    return [_row_to_entry(r) for r in rows]
