"""CLI: `python -m sqlite_note_store import|export|status`.

Kept minimal — the actual work lives in export.py. This file is just a
thin argparse layer so users can run the migration without writing
Python.

Examples:
    python -m sqlite_note_store import /data/projects/.hermes/notes
    python -m sqlite_note_store export /tmp/notes-backup --clean
    python -m sqlite_note_store status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import export as export_mod
from . import schema, storage


def _default_note_root() -> Path:
    return Path.home() / ".hermes" / "notes"


def _cmd_import(args: argparse.Namespace) -> int:
    src = Path(args.src)
    if not src.is_dir():
        print(f"error: source directory not found: {src}", file=sys.stderr)
        return 2
    dst = Path(args.db_root or _default_note_root())
    conn = schema.connect(dst)
    stats = export_mod.import_from_directory(conn, src, replace=args.replace)
    conn.close()
    print(
        f"imported: groups={stats['groups']} entries={stats['entries']} "
        f"cold_batches={stats['cold_batches']} cold_entries={stats['cold_entries']}"
    )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    dst = Path(args.dst)
    src = Path(args.db_root or _default_note_root())
    conn = schema.connect(src)
    stats = export_mod.export_to_directory(
        conn, dst, clean=args.clean, write_index=not args.no_index
    )
    conn.close()
    print(
        f"exported: groups={stats['groups']} entries={stats['entries']} "
        f"cold_batches={stats['cold_batches']} cold_entries={stats['cold_entries']} → {dst}"
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.db_root or _default_note_root())
    if not (root / schema.DB_FILENAME).exists():
        print(f"no SQLite store at {root}/{schema.DB_FILENAME}")
        return 1
    conn = schema.connect(root)
    groups = storage.list_groups(conn)
    dirty = [g for g in groups if g.dirty]
    entries = storage.count_active_entries(conn)
    cold = storage.count_cold_entries(conn)
    conn.close()
    print(f"root:    {root}")
    print(f"groups:  {len(groups)} ({len(dirty)} dirty)")
    print(f"entries: {entries} active / {cold} cold")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlite-notes")
    parser.add_argument(
        "--db-root",
        help="SQLite note store root (defaults to ~/.hermes/notes)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser(
        "import", help="Import a markdown-note-store directory tree"
    )
    p_imp.add_argument("src", help="Directory to import from")
    p_imp.add_argument(
        "--replace",
        action="store_true",
        help="Wipe DB before importing (safe migration mode)",
    )
    p_imp.set_defaults(func=_cmd_import)

    p_exp = sub.add_parser(
        "export", help="Export the SQLite DB into a markdown-note-store directory"
    )
    p_exp.add_argument("dst", help="Directory to export to")
    p_exp.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing category dirs before writing",
    )
    p_exp.add_argument("--no-index", action="store_true", help="Skip INDEX.md")
    p_exp.set_defaults(func=_cmd_export)

    p_stat = sub.add_parser("status", help="Print DB summary")
    p_stat.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
