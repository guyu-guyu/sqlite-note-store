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

In the DB each exported .md file under <category>/ corresponds to one
`groups` row (a thematic container of entries); each cold-storage .md
corresponds to one `cold_batches` row (a time-queue archive batch).

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

    Returns a small stats dict for logging: groups/entries/batches counts.
    """
    out_root.mkdir(parents=True, exist_ok=True)

    if clean:
        _clean_managed_dirs(out_root, conn)

    groups_written = 0
    entries_written = 0

    for g in storage.list_groups(conn):
        entries = storage.list_entries(conn, g.id)
        meta = _group_row_to_meta(g)
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

        target = out_root / g.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        groups_written += 1
        entries_written += len(entries)

    cold_batches_written = 0
    cold_entries_written = 0
    cold_dir = out_root / COLD_DIRNAME
    for cb in storage.list_cold_batches(conn):
        cold_entries = storage.list_cold_entries(conn, cb["id"])
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
        (cold_dir / cb["filename"]).write_text(text, encoding="utf-8")
        cold_batches_written += 1
        cold_entries_written += len(cold_entries)

    if write_index:
        (out_root / INDEX_FILENAME).write_text(
            _build_index_markdown(conn), encoding="utf-8"
        )

    return {
        "groups": groups_written,
        "entries": entries_written,
        "cold_batches": cold_batches_written,
        "cold_entries": cold_entries_written,
    }


def _group_row_to_meta(g: storage.GroupRow) -> dict[str, Any]:
    """Reverse of the meta parser — same key set, same order preferences."""
    meta: dict[str, Any] = {
        "title": g.title or g.slug,
        "dirty": g.dirty,
        "created": g.created,
        "updated": g.updated,
    }
    if g.tags:
        meta["tags"] = g.tags
    return meta


def _clean_managed_dirs(out_root: Path, conn: sqlite3.Connection) -> None:
    """Remove category dirs and cold-storage before a clean export.

    We only touch directories the DB itself owns (category names from
    `groups` plus `cold-storage/`). Anything else the user dropped in
    `out_root` stays untouched — this is a stronger guarantee than
    `rm -rf` and matches the reference plugin's cautious file handling.
    """
    categories = {
        r["category"] for r in conn.execute("SELECT DISTINCT category FROM groups")
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


def _category_tree(groups: list[storage.GroupRow]) -> list[dict[str, Any]]:
    """Build a nested category tree from flat category paths.

    Node shape: {"name": str, "path": str, "groups": [GroupRow],
                 "children": [node]}.  Groups with an empty category are
    filed under the 'uncategorized' root.
    """
    roots: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}

    for g in groups:
        segments = [s for s in (g.category or "").split("/") if s] or ["uncategorized"]
        parent_children = roots
        parent_path = ""
        for i, seg in enumerate(segments):
            node_path = f"{parent_path}/{seg}" if parent_path else seg
            node = by_path.get(node_path)
            if node is None:
                node = {"name": seg, "path": node_path, "groups": [], "children": []}
                by_path[node_path] = node
                parent_children.append(node)
            if i == len(segments) - 1:
                node["groups"].append(g)
            parent_path = node_path
            parent_children = node["children"]
    return roots


def _render_tree(
    nodes: list[dict[str, Any]],
    depth: int,
    lines: list[str],
    counts: dict[int, int],
) -> None:
    """Depth-first tree renderer.  depth 0 = '## name' heading, deeper
    levels indent 2 spaces per level; groups are leaves."""
    for node in nodes:
        if depth == 0:
            lines.append(f"## {node['name']}")
            lines.append("")
        else:
            lines.append("  " * (depth - 1) + f"- {node['name']}")
        indent = "  " * depth
        for g in sorted(node["groups"], key=lambda r: r.slug):
            marker = " *(dirty)*" if g.dirty else ""
            lines.append(
                f"{indent}- [{g.title or g.slug}]({g.path}) — {counts.get(g.id, 0)} entries{marker}"
            )
        if node["children"]:
            _render_tree(node["children"], depth + 1, lines, counts)
        lines.append("")


def _build_index_markdown(conn: sqlite3.Connection) -> str:
    """Human-readable summary. Not a source of truth; safe to regenerate."""
    lines: list[str] = ["# Note Repository Index", ""]

    groups = storage.list_groups(conn)
    total_entries = storage.count_active_entries(conn)
    cold_entries = storage.count_cold_entries(conn)
    dirty_groups = [g for g in groups if g.dirty]
    lines.append(
        f"- Groups: **{len(groups)}** · Entries: **{total_entries}** "
        f"· Cold entries: **{cold_entries}** · Dirty: **{len(dirty_groups)}**"
    )
    lines.append(
        f"- Generated: `{datetime.now(timezone.utc).isoformat()}`"
    )
    lines.append("")

    # Group by category — rendered as a nested tree.
    counts = {
        r["group_id"]: r["n"]
        for r in conn.execute(
            "SELECT group_id, COUNT(*) AS n FROM entries GROUP BY group_id"
        )
    }
    _render_tree(_category_tree(groups), 0, lines, counts)

    if cold_entries:
        lines.append("## cold-storage")
        lines.append("")
        for cb in storage.list_cold_batches(conn):
            count = conn.execute(
                "SELECT COUNT(*) FROM cold_entries WHERE cold_batch_id = ?",
                (cb["id"],),
            ).fetchone()[0]
            lines.append(
                f"- [{cb['filename']}]({COLD_DIRNAME}/{cb['filename']}) — {count} entries"
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
                 entries, new paths get added, unmentioned groups stay.
                 The reference plugin never merges arbitrary sources, so
                 replace=True is the honest "migration from markdown"
                 operation.

    Skips the auto-generated INDEX.md at the root; a `## header` inside
    it isn't meant to be searchable content.
    """
    if replace:
        _wipe_all(conn)

    stats = {
        "groups": 0,
        "entries": 0,
        "cold_batches": 0,
        "cold_entries": 0,
        "skipped": 0,
    }

    for md_path in sorted(in_root.rglob("*.md")):
        rel = md_path.relative_to(in_root)
        if rel.name == INDEX_FILENAME and rel.parent == Path("."):
            continue
        if rel.parts and rel.parts[0] == COLD_DIRNAME:
            _import_cold_batch(conn, md_path, rel.name, stats)
        else:
            _import_active_group(conn, md_path, rel, stats)

    conn.commit()
    return stats


def _wipe_all(conn: sqlite3.Connection) -> None:
    """Full reset — leaves schema in place, drops rows and FTS index.

    Cheaper (and safer) than dropping/re-creating tables; the FTS shadow
    also gets cleared because it lives outside the CASCADE graph.
    """
    conn.execute("DELETE FROM entries_fts")
    conn.execute("DELETE FROM entries")
    conn.execute("DELETE FROM groups")
    conn.execute("DELETE FROM cold_entries")
    conn.execute("DELETE FROM cold_batches")


def _import_active_group(
    conn: sqlite3.Connection,
    abs_path: Path,
    rel_path: Path,
    stats: dict[str, int],
) -> None:
    """Parse an active-note file and upsert its group row + entries."""
    parts = rel_path.parts
    if len(parts) < 2:
        # A .md sitting at the root with no category. Reference plugin
        # would have placed it under 'uncategorized'; we do the same on
        # import for consistency.
        category = "uncategorized"
        slug = rel_path.stem
        canonical_rel = f"{category}/{slug}.md"
    else:
        category = "/".join(parts[:-1])  # multi-level: all path segments
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

    group_id = storage.upsert_group(
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
    storage.replace_entries(conn, group_id, parsed.entries)
    stats["groups"] += 1
    stats["entries"] += len(parsed.entries)


def _import_cold_batch(
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
        "SELECT id FROM cold_batches WHERE filename = ?", (filename,)
    ).fetchone()
    if existing:
        batch_id = existing["id"]
        conn.execute("DELETE FROM cold_entries WHERE cold_batch_id = ?", (batch_id,))
    else:
        cur = conn.execute(
            "INSERT INTO cold_batches(filename, created) VALUES (?, ?)",
            (filename, created),
        )
        batch_id = cur.lastrowid

    for idx, e in enumerate(entries):
        conn.execute(
            "INSERT INTO cold_entries(cold_batch_id, header, content, last_used, "
            "original_category, order_index) VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, e.header, e.content, e.last_used, None, idx),
        )
    stats["cold_batches"] += 1
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
