"""Filesystem <-> SQLite bridge — the "always exportable to markdown" guarantee.

This module exists to guarantee one thing:

    Any SQLite state produced by storage.py can be written out as a
    directory tree indistinguishable from what markdown-note-store would
    have created — and read back into a fresh DB without loss.

That guarantee is what lets us treat SQLite as the primary store while
keeping markdown as a first-class export/interchange format. Every
plugin promise about "your notes are still just markdown" flows through
here.

Directory shape produced (mirrors the reference plugin):

    <root>/
      INDEX.md                    # generated summary; regenerated on export
      <category>/
        <slug>.md
        ...
      cold-storage/
        YYYY-MM-DD.md
        YYYY-MM-DD-NN.md          # collision suffix
        ...

Tests round-trip a curated fixture DB through export → parse → import
and assert row-level equivalence. That's the executable form of the
"data structure aligned with markdown" requirement.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import markdown_io, schema, storage


COLD_DIRNAME = "cold-storage"
INDEX_FILENAME = "INDEX.md"


# ---------------------------------------------------------------------------
# Export: SQLite -> filesystem
# ---------------------------------------------------------------------------


def export_to_directory(
    conn: sqlite3.Connection,
    out_root: Path,
    *,
    clean: bool = False,
    write_index: bool = True,
) -> dict[str, int]:
    """Serialize the entire DB into markdown files under `out_root`.

    Args:
        clean: if True, wipe existing category/cold-storage dirs first so
               the export represents *only* the current DB state. When
               False (the default) we still overwrite same-path files
               but preserve unrelated user files — safer default for the
               "export next to the live DB" case.
        write_index: emit an INDEX.md summary at the root. Only ever a
                     convenience for humans; the DB itself is SoT, so
                     regenerating it every time is fine.

    Returns a small stats dict for logging: files/entries/cold counts.
    """
    out_root.mkdir(parents=True, exist_ok=True)

    if clean:
        _clean_managed_dirs(out_root, conn)

    files_written = 0
    entries_written = 0

    for f in storage.list_files(conn):
        entries = storage.list_entries(conn, f.id)
        meta = _file_row_to_meta(f)
        parsed = [
            markdown_io.ParsedEntry(
                header=e.header,
                content=e.content,
                last_used=e.last_used,
                comments=e.comments,
            )
            for e in entries
        ]
        text = markdown_io.render_file(meta, parsed)

        target = out_root / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        files_written += 1
        entries_written += len(entries)

    cold_files_written = 0
    cold_entries_written = 0
    cold_dir = out_root / COLD_DIRNAME
    for cf in storage.list_cold_files(conn):
        cold_entries = storage.list_cold_entries(conn, cf["id"])
        parsed_cold = [
            markdown_io.ParsedEntry(
                header=ce["header"],
                content=ce["content"],
                last_used=ce.get("last_used"),
                comments=[],
            )
            for ce in cold_entries
        ]
        # Cold-storage files intentionally omit YAML front matter — the
        # filename encodes the creation date and dirty/title are
        # meaningless in a time-queue archive. Matches reference plugin.
        text = markdown_io.build_body_from_entries(parsed_cold)
        cold_dir.mkdir(parents=True, exist_ok=True)
        (cold_dir / cf["filename"]).write_text(text, encoding="utf-8")
        cold_files_written += 1
        cold_entries_written += len(cold_entries)

    if write_index:
        (out_root / INDEX_FILENAME).write_text(
            _build_index_markdown(conn), encoding="utf-8"
        )

    return {
        "files": files_written,
        "entries": entries_written,
        "cold_files": cold_files_written,
        "cold_entries": cold_entries_written,
    }


def _file_row_to_meta(f: storage.FileRow) -> dict[str, Any]:
    """Reverse of the meta parser — same key set, same order preferences."""
    meta: dict[str, Any] = {
        "title": f.title or f.slug,
        "dirty": f.dirty,
        "created": f.created,
        "updated": f.updated,
    }
    if f.tags:
        meta["tags"] = f.tags
    return meta


def _clean_managed_dirs(out_root: Path, conn: sqlite3.Connection) -> None:
    """Remove category dirs and cold-storage before a clean export.

    We only touch directories the DB itself owns (category names from
    `files` plus `cold-storage/`). Anything else the user dropped in
    `out_root` stays untouched — this is a stronger guarantee than
    `rm -rf` and matches the reference plugin's cautious file handling.
    """
    categories = {
        r["category"] for r in conn.execute("SELECT DISTINCT category FROM files")
    }
    for cat in categories:
        p = out_root / cat
        if p.is_dir():
            shutil.rmtree(p)
    cold_dir = out_root / COLD_DIRNAME
    if cold_dir.is_dir():
        shutil.rmtree(cold_dir)


# ---------------------------------------------------------------------------
# INDEX.md — regenerated on every export.
# ---------------------------------------------------------------------------


def _build_index_markdown(conn: sqlite3.Connection) -> str:
    """Human-readable summary. Not a source of truth; safe to regenerate."""
    lines: list[str] = ["# Note Repository Index", ""]

    files = storage.list_files(conn)
    total_entries = storage.count_active_entries(conn)
    cold_entries = storage.count_cold_entries(conn)
    dirty_files = [f for f in files if f.dirty]
    lines.append(
        f"- Files: **{len(files)}** · Entries: **{total_entries}** "
        f"· Cold entries: **{cold_entries}** · Dirty: **{len(dirty_files)}**"
    )
    lines.append(
        f"- Generated: `{datetime.now(timezone.utc).isoformat()}`"
    )
    lines.append("")

    # Group by category.
    by_cat: dict[str, list[storage.FileRow]] = {}
    for f in files:
        by_cat.setdefault(f.category, []).append(f)

    for category in sorted(by_cat):
        lines.append(f"## {category}")
        lines.append("")
        for f in sorted(by_cat[category], key=lambda r: r.slug):
            entry_count = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE file_id = ?", (f.id,)
            ).fetchone()[0]
            marker = " *(dirty)*" if f.dirty else ""
            lines.append(
                f"- [{f.title or f.slug}]({f.path}) — {entry_count} entries{marker}"
            )
        lines.append("")

    if cold_entries:
        lines.append("## cold-storage")
        lines.append("")
        for cf in storage.list_cold_files(conn):
            count = conn.execute(
                "SELECT COUNT(*) FROM cold_entries WHERE cold_file_id = ?",
                (cf["id"],),
            ).fetchone()[0]
            lines.append(
                f"- [{cf['filename']}]({COLD_DIRNAME}/{cf['filename']}) — {count} entries"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Import: filesystem -> SQLite
# ---------------------------------------------------------------------------


def import_from_directory(
    conn: sqlite3.Connection, in_root: Path, *, replace: bool = False
) -> dict[str, int]:
    """Load a markdown-note-store directory tree into the current DB.

    Args:
        replace: if True, wipe existing DB contents before importing.
                 Otherwise we upsert by path — same path replaces its
                 entries, new paths get added, unmentioned files stay.
                 The reference plugin never merges arbitrary sources, so
                 replace=True is the honest "migration from markdown"
                 operation.

    Skips the auto-generated INDEX.md at the root; a `## header` inside
    it isn't meant to be searchable content.
    """
    if replace:
        _wipe_all(conn)

    stats = {"files": 0, "entries": 0, "cold_files": 0, "cold_entries": 0, "skipped": 0}

    for md_path in sorted(in_root.rglob("*.md")):
        rel = md_path.relative_to(in_root)
        if rel.name == INDEX_FILENAME and rel.parent == Path("."):
            continue
        if rel.parts and rel.parts[0] == COLD_DIRNAME:
            _import_cold_file(conn, md_path, rel.name, stats)
        else:
            _import_active_file(conn, md_path, rel, stats)

    conn.commit()
    return stats


def _wipe_all(conn: sqlite3.Connection) -> None:
    """Full reset — leaves schema in place, drops rows and FTS index.

    Cheaper (and safer) than dropping/re-creating tables; the FTS shadow
    also gets cleared because it lives outside the CASCADE graph.
    """
    conn.execute("DELETE FROM entries_fts")
    conn.execute("DELETE FROM entries")
    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM cold_entries")
    conn.execute("DELETE FROM cold_files")


def _import_active_file(
    conn: sqlite3.Connection,
    abs_path: Path,
    rel_path: Path,
    stats: dict[str, int],
) -> None:
    """Parse an active-note file and upsert its file row + entries."""
    parts = rel_path.parts
    if len(parts) < 2:
        # A .md sitting at the root with no category. Reference plugin
        # would have placed it under 'uncategorized'; we do the same on
        # import for consistency.
        category = "uncategorized"
        slug = rel_path.stem
        canonical_rel = f"{category}/{slug}.md"
    else:
        category = parts[0]
        slug = rel_path.stem
        canonical_rel = "/".join([*parts[:-1], f"{slug}.md"])

    text = abs_path.read_text(encoding="utf-8")
    parsed = markdown_io.parse_file(text)

    title = str(parsed.meta.get("title") or slug)
    tags_raw = parsed.meta.get("tags") or []
    tags = list(tags_raw) if isinstance(tags_raw, list) else []
    dirty = bool(parsed.meta.get("dirty"))
    now = _now_iso()
    created = str(parsed.meta.get("created") or now)
    updated = str(parsed.meta.get("updated") or now)

    file_id = storage.upsert_file(
        conn,
        path=canonical_rel,
        category=category,
        slug=slug,
        title=title,
        tags=tags,
        dirty=dirty,
        created=created,
        updated=updated,
    )
    # Replace entries wholesale so re-import is idempotent.
    storage.replace_entries(conn, file_id, parsed.entries)
    stats["files"] += 1
    stats["entries"] += len(parsed.entries)


def _import_cold_file(
    conn: sqlite3.Connection,
    abs_path: Path,
    filename: str,
    stats: dict[str, int],
) -> None:
    """Parse a cold-storage archive file and load its entries.

    Cold files have no YAML front matter, so the parser just sees a
    sequence of `## header` blocks. We pull creation time from the
    filename when it looks like ISO date; falling back to file mtime.
    """
    text = abs_path.read_text(encoding="utf-8")
    entries = markdown_io.parse_entries(text)
    created = _cold_created_from_filename(filename) or _now_iso()

    # If a row with this filename already exists (re-import path), keep
    # its id but wipe entries so we don't accumulate duplicates.
    existing = conn.execute(
        "SELECT id FROM cold_files WHERE filename = ?", (filename,)
    ).fetchone()
    if existing:
        cold_id = existing["id"]
        conn.execute("DELETE FROM cold_entries WHERE cold_file_id = ?", (cold_id,))
    else:
        cur = conn.execute(
            "INSERT INTO cold_files(filename, created) VALUES (?, ?)",
            (filename, created),
        )
        cold_id = cur.lastrowid

    for idx, e in enumerate(entries):
        conn.execute(
            "INSERT INTO cold_entries(cold_file_id, header, content, last_used, "
            "original_category, order_index) VALUES (?, ?, ?, ?, ?, ?)",
            (cold_id, e.header, e.content, e.last_used, None, idx),
        )
    stats["cold_files"] += 1
    stats["cold_entries"] += len(entries)


def _cold_created_from_filename(filename: str) -> str | None:
    """Extract 'YYYY-MM-DD' from 'YYYY-MM-DD.md' / 'YYYY-MM-DD-NN.md'."""
    stem = filename[:-3] if filename.endswith(".md") else filename
    # First 10 chars should be an ISO date if the filename follows the
    # cold-storage convention.
    if len(stem) >= 10 and stem[4] == "-" and stem[7] == "-":
        date_str = stem[:10]
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return None
        return f"{date_str}T00:00:00+00:00"
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
