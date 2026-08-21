"""SQLiteNoteStoreProvider — the MemoryProvider facade.

This module wires storage / markdown_io / export into the exact set of
tools the reference `markdown-note-store` plugin exposes, so the LLM's
mental model doesn't change. Everything below the tool boundary is
SQLite; everything above it is byte-compatible with the old plugin.

Tool surface (identical names, identical response shapes):
    note_search   — FTS5 across active entries only (never cold).
    note_write    — append or replace an entry; auto-slug title → file.
    note_read     — dump a single active file's content, YAML + entries.
    note_use      — refresh an entry's `last_used`.
    note_recall   — read a cold-storage entry, no mutation to cold side.
    note_comment  — attach an ephemeral TODO to an entry, marks dirty.
    note_maintain — mechanical work (cold-evict, index) + return dirty list.
    note_rewrite  — sole dirty-clearing entry point.

Design decisions honored (see hermes-memory-provider skill):
    - Python detects, LLM decides, note_rewrite persists.
    - note_maintain NEVER clears the dirty flag on its own.
    - Cold storage is an append-to-latest queue keyed on the newest cold
      file's `created` timestamp — not a per-day partition.
    - Entries are the atomic unit of memory; files are grouping
      containers with file-level dirty.
    - Every mutation goes through a single write connection guarded by
      `self._lock` — matches the "one connection, one lock" resolution
      of the audit pitfall #14.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import export as export_mod
from . import markdown_io, schema, storage

logger = logging.getLogger(__name__)


# Tunable defaults — mirror the reference plugin's numbers so the two
# implementations behave interchangeably when swapping.
DEFAULT_COLD_EVICT_DAYS = 90
DEFAULT_MAX_COLD_FILES = 50
DEFAULT_MAX_ACTIVE_FILE_SIZE_BYTES = 50 * 1024  # 50 KB soft cap
DEFAULT_MAX_FILES_PER_CATEGORY = 50
DEFAULT_PREFETCH_CHAR_LIMIT = 2000
DEFAULT_TOOL_SEARCH_LIMIT = 5


# ---------------------------------------------------------------------------
# ABC import — done at module import time but tolerant of stand-alone runs.
# ---------------------------------------------------------------------------
try:
    from agent.memory_provider import MemoryProvider  # type: ignore
except ImportError:  # pragma: no cover — allows tests without the agent tree.
    class MemoryProvider:  # type: ignore
        """Fallback stub so the provider class stays importable in tests."""
        def initialize(self, session_id: str, **kwargs: Any) -> None:
            raise NotImplementedError


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class SQLiteNoteStoreProvider(MemoryProvider):
    """Hermes memory provider backed by a single SQLite database."""

    # -- lifecycle ----------------------------------------------------------

    def __init__(self) -> None:
        self._note_root: Path | None = None
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        # Config knobs — populated in `initialize()` from Hermes config.
        self._cold_evict_days = DEFAULT_COLD_EVICT_DAYS
        self._max_cold_files = DEFAULT_MAX_COLD_FILES
        self._max_active_file_size = DEFAULT_MAX_ACTIVE_FILE_SIZE_BYTES
        self._max_files_per_category = DEFAULT_MAX_FILES_PER_CATEGORY
        self._prefetch_char_limit = DEFAULT_PREFETCH_CHAR_LIMIT
        self._pending_prefetch_query: str | None = None
        self._cached_prefetch_result: str = ""

    @property
    def name(self) -> str:  # required by MemoryProvider
        return "sqlite-note-store"

    def is_available(self) -> bool:  # required by MemoryProvider
        # No external deps — always available.
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Open (or create) the SQLite DB and install the bundled skill.

        The note root is chosen in this order (matching the reference plugin):
          1. explicit kwarg `note_root`
          2. `<hermes_home>/notes/`
          3. `~/.hermes/notes/` fallback
        """
        note_root = kwargs.get("note_root")
        if note_root is None:
            hermes_home = kwargs.get("hermes_home")
            if hermes_home:
                note_root = Path(hermes_home) / "notes"
            else:
                note_root = Path.home() / ".hermes" / "notes"
        self._note_root = Path(note_root)
        self._conn = schema.connect(self._note_root)

        # Install bundled maintenance skill (idempotent).
        try:
            self._install_bundled_skill(kwargs.get("hermes_home"))
        except Exception as e:  # pragma: no cover — non-fatal.
            logger.debug("Skill install skipped: %s", e)

    def shutdown(self) -> None:  # required by MemoryProvider
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                finally:
                    self._conn.close()
                    self._conn = None

    def _install_bundled_skill(self, hermes_home: str | None) -> None:
        src = Path(__file__).parent / "skills" / "note-maintenance" / "SKILL.md"
        if not src.exists():
            return
        home = Path(hermes_home) if hermes_home else Path.home() / ".hermes"
        dst_dir = home / "skills" / "note-taking" / "note-maintenance-sqlite"
        dst = dst_dir / "SKILL.md"
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # -- context injection --------------------------------------------------

    def system_prompt_block(self) -> str:
        """Rendered every turn — INDEX summary + short usage guide.

        The guide is intentionally terse (< 700 chars) because Hermes
        already surfaces the maintenance skill via `skills_list`. Long
        prompts belong in the skill, not here.
        """
        if self._conn is None or self._note_root is None:
            return ""
        with self._lock:
            index_text = export_mod._build_index_markdown(self._conn)
        return (
            "# Note Repository (sqlite-note-store)\n"
            "Persistent memory keyed on `title` (auto-slugged to a file). "
            "Reading path: scan the index below first to spot the right file, "
            "then `note_read(path)` for a slim headers overview, then "
            "`note_read(path, entry_header)` to fetch just the entry you want — "
            "cheap on context. Only during maintenance (processing a dirty file) "
            "use `note_read_group(path)` to see every entry's body. "
            "Fall back to `note_search(query)` when the index doesn't match. "
            "Writing path: `note_write(title, content, category, tags)`. "
            "Cold storage is a time queue — do NOT browse it proactively; use "
            "`note_recall(entry_header)` only if the user asks to look 'from history'. "
            "Use `note_comment` to flag issues on an entry (dirty); do NOT edit "
            "note files with `write_file`/`terminal`.\n\n"
            f"## Live Index\n\n{index_text}"
        )

    # -- prefetch (background retrieval) -----------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return cached search results (queued from the previous turn).

        Kept synchronous and cheap so it never blocks the turn — real
        search happens in `queue_prefetch` (or here on cold start).
        """
        if not query.strip():
            self._cached_prefetch_result = ""
            return ""
        # If we have a fresh cached result matching this query, use it;
        # otherwise fall through to a bounded synchronous search.
        if self._pending_prefetch_query == query and self._cached_prefetch_result:
            result = self._cached_prefetch_result
            self._cached_prefetch_result = ""
            return result
        return self._run_prefetch_search(query)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Compute prefetch result for consumption in the NEXT turn.

        Runs synchronously against the local SQLite DB — no threads
        needed because FTS5 lookups are microsecond-scale.
        """
        if not query.strip():
            return
        self._pending_prefetch_query = query
        self._cached_prefetch_result = self._run_prefetch_search(query)

    def _run_prefetch_search(self, query: str) -> str:
        with self._lock:
            if self._conn is None:
                return ""
            hits = storage.search_fts(self._conn, query, limit=3)
        if not hits:
            return ""
        parts = ["# Note Repository — Relevant Snippets"]
        for h in hits:
            parts.append(f"- [{h['title'] or h['path']}]({h['path']}): {h['snippet']}")
        combined = "\n".join(parts)
        # Enforce the token-budget invariant from the skill.
        if len(combined) > self._prefetch_char_limit:
            combined = combined[: self._prefetch_char_limit - 3] + "..."
        return combined

    # -- tool schemas -------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Exact same names/params as the reference markdown-note-store."""
        return [
            {
                "name": "note_search",
                "description": (
                    "Search the SQLite-backed note repository for relevant "
                    "entries. IMPORTANT: First check the INDEX in the system "
                    "prompt for matching file titles. Only use this search when "
                    "INDEX doesn't have what you need. Excludes cold-storage."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": DEFAULT_TOOL_SEARCH_LIMIT},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "note_write",
                "description": (
                    "Persist a note entry. IMPORTANT: Provide a descriptive, "
                    "meaningful title — it determines the filename and helps "
                    "future retrieval via INDEX. Do NOT use generic titles "
                    "like 'note' or 'memo'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "category": {"type": "string", "default": "uncategorized"},
                        "tags": {"type": "string", "description": "comma-separated"},
                    },
                    "required": ["title", "content"],
                },
            },
            {
                "name": "note_read",
                "description": (
                    "Read a single note entry (default) or a slim file "
                    "overview. Pass `entry_header` to fetch that specific "
                    "entry's full content — this is the token-efficient "
                    "default for day-to-day recall. Omit `entry_header` to "
                    "get just the file's title + headers list, then decide "
                    "which entry to load. For maintenance (reading every "
                    "entry to merge/dedupe), use `note_read_group` instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "relative path like 'category/slug.md'",
                        },
                        "entry_header": {
                            "type": "string",
                            "description": (
                                "specific entry header to fetch; omit for "
                                "a slim overview listing all headers"
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "note_read_group",
                "description": (
                    "Read every entry in a file with full content. Use this "
                    "during MAINTENANCE (when processing a dirty file so you "
                    "can see all entries together for merge/dedupe/split "
                    "decisions). For normal recall, prefer `note_read` with "
                    "an `entry_header` — it's much cheaper on context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "relative path like 'category/slug.md'",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "note_use",
                "description": (
                    "Refresh an entry's last_used timestamp so it doesn't age "
                    "out into cold storage. Call this when you actually cite "
                    "or reference the entry in your response."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "entry_header": {"type": "string"},
                    },
                    "required": ["path", "entry_header"],
                },
            },
            {
                "name": "note_recall",
                "description": (
                    "Read an entry from cold storage without modifying the "
                    "cold-storage file. Returns the content — use note_write "
                    "to save it back to the active repository."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entry_header": {"type": "string"},
                    },
                    "required": ["entry_header"],
                },
            },
            {
                "name": "note_comment",
                "description": (
                    "Flag an issue with an entry (ephemeral TODO). Marks the "
                    "file dirty so it surfaces in the next note_maintain. "
                    "Only comment when there's an actual problem — do NOT "
                    "comment on entries that are correct and useful."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "entry_header": {"type": "string"},
                        "comment_type": {
                            "type": "string",
                            "enum": [
                                "inaccurate",
                                "needs_improvement",
                                "wrong",
                                "conflicting",
                                "misplaced",
                            ],
                        },
                        "comment_text": {"type": "string"},
                    },
                    "required": [
                        "path",
                        "entry_header",
                        "comment_type",
                        "comment_text",
                    ],
                },
            },
            {
                "name": "note_maintain",
                "description": (
                    "Run mechanical maintenance: cold-eviction, oversized "
                    "file detection, INDEX regeneration. Returns "
                    "`dirty_notes`, `oversized_files`, "
                    "`overpopulated_categories` — the LLM must resolve each "
                    "by reading the file and calling note_rewrite. "
                    "note_maintain NEVER clears dirty on its own."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "force": {"type": "boolean", "default": False},
                    },
                },
            },
            {
                "name": "note_rewrite",
                "description": (
                    "Replace a file's entries with the given curated list, "
                    "clearing dirty and consuming any comments. entries=[] "
                    "deletes the file. This is the SOLE dirty-clearing tool."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "entries": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "header": {"type": "string"},
                                    "content": {"type": "string"},
                                    "last_used": {"type": "string"},
                                },
                                "required": ["header", "content"],
                            },
                        },
                    },
                    "required": ["path", "entries"],
                },
            },
        ]

    # -- tool dispatch ------------------------------------------------------

    def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs: Any
    ) -> str:
        """Dispatch a tool call to a handler. Returns a JSON string.

        The MemoryProvider contract wants JSON — makes the tool layer
        format-agnostic and matches the reference plugin's behavior.
        """
        try:
            handler = getattr(self, f"_tool_{tool_name}", None)
            if handler is None:
                raise NotImplementedError(f"unknown tool: {tool_name}")
            with self._lock:
                if self._conn is None:
                    raise RuntimeError("provider not initialized")
                self._ensure_same_thread()
                result = handler(args or {})
                self._conn.commit()
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.exception("tool call failed: %s", tool_name)
            return json.dumps({"error": str(e), "tool": tool_name}, ensure_ascii=False)

    def _ensure_same_thread(self) -> None:
        """Reconnect if the current thread differs from the one that opened
        the connection.  Required when Hermes loads the plugin in one thread
        (startup) but dispatches tool calls from another (asyncio worker).
        schema.connect() already passes ``check_same_thread=False``, but this
        guard covers processes that loaded an older cached version.
        """
        if self._conn is None or self._note_root is None:
            return
        try:
            # sqlite3.Connection has no public thread-id attribute, but
            # attempting a trivial query is the cheapest correctness check.
            self._conn.execute("SELECT 1").fetchone()
        except sqlite3.ProgrammingError:
            logger.info("Reconnecting SQLite from thread %s", threading.get_ident())
            self._conn = schema.connect(self._note_root)

    # ---- tool implementations --------------------------------------------

    def _tool_note_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query", "")
        limit = int(args.get("limit") or DEFAULT_TOOL_SEARCH_LIMIT)
        results = storage.search_fts(self._conn, query, limit=limit)
        return {"results": results, "count": len(results)}

    def _tool_note_write(self, args: dict[str, Any]) -> dict[str, Any]:
        title = args["title"]
        content = args["content"]
        category = args.get("category") or "uncategorized"
        tags = _parse_tags(args.get("tags"))

        slug = storage.slugify(title)
        path = storage.build_path(category, slug)

        # If the file already exists, append; otherwise create fresh.
        existing = storage.get_file_by_path(self._conn, path)
        if existing:
            file_id = existing.id
            # Preserve the file's existing title/tags unless caller changed.
            merged_tags = sorted(set(existing.tags) | set(tags))
            storage.upsert_file(
                self._conn,
                path=path,
                category=category,
                slug=slug,
                title=existing.title or title,
                tags=merged_tags,
                dirty=True,  # new entry — LLM must review during maintenance.
                created=existing.created,
            )
        else:
            file_id = storage.upsert_file(
                self._conn,
                path=path,
                category=category,
                slug=slug,
                title=title,
                tags=tags,
                dirty=True,
            )

        # Header defaults to a timestamped label matching the reference
        # plugin — a single entry per note_write call.
        header = args.get("entry_header") or datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        entry_id = storage.append_entry(
            self._conn, file_id, header=header, content=content,
            last_used=storage._now(),
        )
        return {
            "status": "ok",
            "path": path,
            "file_id": file_id,
            "entry_id": entry_id,
            "created_new_file": existing is None,
        }

    def _tool_note_read(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read a single entry (default) or a slim file overview.

        Two modes — never returns full content for all entries:
          * ``entry_header`` given → return that single entry.
          * ``entry_header`` omitted → return a slim overview
            ``{path, title, category, tags, dirty, headers: [...]}``
            so the LLM can pick which entry to fetch next.

        For maintenance workflows that legitimately need every entry's
        full content, call ``note_read_group`` instead.
        """
        path = args["path"]
        row = storage.get_file_by_path(self._conn, path)
        if row is None:
            return {"error": f"note not found: {path}"}

        entry_header = args.get("entry_header")
        entries = storage.list_entries(self._conn, row.id)

        if entry_header is not None:
            match = next((e for e in entries if e.header == entry_header), None)
            if match is None:
                return {
                    "error": f"entry not found in {path}: {entry_header}",
                    "available_headers": [e.header for e in entries],
                }
            return {
                "path": path,
                "title": row.title,
                "category": row.category,
                "entry": {
                    "header": match.header,
                    "content": match.content,
                    "last_used": match.last_used,
                    "comments": match.comments,
                },
            }

        # Overview mode — no bodies. Keeps context small; caller decides
        # whether to fetch a specific entry or read the whole group.
        return {
            "path": path,
            "title": row.title,
            "category": row.category,
            "tags": row.tags,
            "dirty": row.dirty,
            "entry_count": len(entries),
            "headers": [e.header for e in entries],
            "hint": (
                "This is a slim overview. Call note_read(path, entry_header) "
                "for a specific entry, or note_read_group(path) if you need "
                "every entry's full content (e.g. during maintenance)."
            ),
        }

    def _tool_note_read_group(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read every entry in a file — the maintenance-time reader.

        Kept as a separate tool from ``note_read`` so day-to-day recall
        can stay lean while maintenance workflows still get the full
        neighbor context they need to merge / dedupe / re-organize.
        """
        path = args["path"]
        row = storage.get_file_by_path(self._conn, path)
        if row is None:
            return {"error": f"note not found: {path}"}
        entries = storage.list_entries(self._conn, row.id)
        return {
            "path": path,
            "title": row.title,
            "category": row.category,
            "tags": row.tags,
            "dirty": row.dirty,
            "created": row.created,
            "updated": row.updated,
            "entries": [
                {
                    "header": e.header,
                    "content": e.content,
                    "last_used": e.last_used,
                    "comments": e.comments,
                }
                for e in entries
            ],
        }

    def _tool_note_use(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        header = args["entry_header"]
        row = storage.get_file_by_path(self._conn, path)
        if row is None:
            return {"error": f"note not found: {path}"}
        entry = storage.find_entry(self._conn, row.id, header)
        if entry is None:
            return {"error": f"entry not found in {path}: {header}"}
        storage.set_entry_last_used(self._conn, entry.id)
        return {"status": "ok", "entry_id": entry.id}

    def _tool_note_recall(self, args: dict[str, Any]) -> dict[str, Any]:
        header = args["entry_header"]
        hit = storage.find_cold_entry(self._conn, header)
        if hit is None:
            return {"error": f"cold entry not found: {header}"}
        # Read-only on cold storage — the caller can note_write to bring
        # it back into the active repo (dup is fine).
        return {"status": "ok", **hit}

    def _tool_note_comment(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        header = args["entry_header"]
        row = storage.get_file_by_path(self._conn, path)
        if row is None:
            return {"error": f"note not found: {path}"}
        entry = storage.find_entry(self._conn, row.id, header)
        if entry is None:
            return {"error": f"entry not found in {path}: {header}"}
        storage.append_comment(
            self._conn,
            entry.id,
            comment_type=args["comment_type"],
            comment_text=args["comment_text"],
        )
        return {"status": "ok", "entry_id": entry.id}

    def _tool_note_rewrite(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        row = storage.get_file_by_path(self._conn, path)
        if row is None:
            return {"error": f"note not found: {path}"}
        raw_entries = args.get("entries") or []
        if not raw_entries:
            storage.delete_file(self._conn, row.id)
            return {"status": "ok", "action": "deleted", "path": path}
        entries = [
            markdown_io.ParsedEntry(
                header=e["header"],
                content=e["content"],
                last_used=e.get("last_used"),
                comments=[],  # rewrite consumes comments by design
            )
            for e in raw_entries
        ]
        storage.replace_entries(self._conn, row.id, entries)
        storage.mark_dirty(self._conn, row.id, dirty=False)
        return {"status": "ok", "action": "rewritten", "entry_count": len(entries)}

    def _tool_note_maintain(self, args: dict[str, Any]) -> dict[str, Any]:
        """Mechanical work only; NEVER clears dirty on its own."""
        _ = args.get("force", False)

        # 1) Cold-evict entries whose last_used is older than the threshold.
        threshold_iso = (
            datetime.now(timezone.utc) - timedelta(days=self._cold_evict_days)
        ).isoformat()
        stale = storage.select_stale_active_entries(self._conn, threshold_iso)
        cold_moved = 0
        if stale:
            cold_id = storage.get_or_create_cold_file_for_today(self._conn)
            for e in stale:
                storage.move_entry_to_cold(self._conn, e.id, cold_file_id=cold_id)
                cold_moved += 1

        # 2) Enforce cold-file cap — sole physical-delete path in the system.
        cold_files_pruned = storage.enforce_cold_file_limit(
            self._conn, max_files=self._max_cold_files
        )

        # 3) Detect oversized files & overpopulated categories → force dirty.
        oversized = self._detect_oversized_files()
        overpop = self._detect_overpopulated_categories()
        for path in oversized:
            row = storage.get_file_by_path(self._conn, path["path"])
            if row:
                storage.mark_dirty(self._conn, row.id, True)
        for entry in overpop:
            for p in entry["files"][
                : max(0, entry["file_count"] - self._max_files_per_category)
            ]:
                row = storage.get_file_by_path(self._conn, p)
                if row:
                    storage.mark_dirty(self._conn, row.id, True)

        # 4) Enumerate dirty files — LLM must resolve each with note_rewrite.
        dirty_files = storage.list_files(self._conn, dirty_only=True)
        dirty_notes = [f.path for f in dirty_files]

        return {
            "dirty_notes": dirty_notes,
            "cold_moved": cold_moved,
            "cold_files_pruned": cold_files_pruned,
            "oversized_files": oversized,
            "overpopulated_categories": overpop,
        }

    # ---- maintenance detectors -------------------------------------------

    def _detect_oversized_files(self) -> list[dict[str, Any]]:
        """Files whose rendered markdown would exceed the size cap.

        We compute the size against the *rendered* form because that's
        what the LLM will see when it reads the file — bytes on disk
        are the honest signal for whether a split is warranted.
        """
        oversized: list[dict[str, Any]] = []
        for f in storage.list_files(self._conn):
            entries = storage.list_entries(self._conn, f.id)
            text = markdown_io.render_file(
                {"title": f.title, "tags": f.tags, "dirty": f.dirty,
                 "created": f.created, "updated": f.updated},
                [markdown_io.ParsedEntry(
                    header=e.header, content=e.content,
                    last_used=e.last_used, comments=e.comments,
                ) for e in entries],
            )
            size = len(text.encode("utf-8"))
            if size > self._max_active_file_size:
                oversized.append({
                    "path": f.path,
                    "size_bytes": size,
                    "size_kb": round(size / 1024, 1),
                })
        return oversized

    def _detect_overpopulated_categories(self) -> list[dict[str, Any]]:
        """Categories with more files than allowed."""
        out: list[dict[str, Any]] = []
        by_cat: dict[str, list[storage.FileRow]] = {}
        for f in storage.list_files(self._conn):
            by_cat.setdefault(f.category, []).append(f)
        for cat, files in by_cat.items():
            if len(files) > self._max_files_per_category:
                # Sort oldest-first by `created` so the oldest slice gets
                # force-dirty'd — matches the reference plugin's rule.
                files_sorted = sorted(files, key=lambda r: r.created)
                out.append({
                    "category": cat,
                    "file_count": len(files),
                    "files": [f.path for f in files_sorted],
                })
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_tags(raw: Any) -> list[str]:
    """Accept a list, a comma string, or None — return normalized list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


# ---------------------------------------------------------------------------
# Registration entry point — Hermes looks for `register(ctx)`.
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:  # pragma: no cover — integration surface.
    """Registered by Hermes when the plugin is discovered."""
    try:
        ctx.register_memory_provider(SQLiteNoteStoreProvider())
    except AttributeError:
        # _ProviderCollector fake context — nothing else to do.
        pass
