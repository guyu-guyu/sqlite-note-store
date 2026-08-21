"""SQLiteNoteStoreProvider — the MemoryProvider facade.

This module wires storage / markdown_io / export into the exact set of
tools the reference `markdown-note-store` plugin exposes, so the LLM's
mental model doesn't change. Everything below the tool boundary is
SQLite; everything above it is byte-compatible with the old plugin.

Tool surface (identical names, identical response shapes):
    note_search   — FTS5 across active entries only (never cold).
    note_write    — append or replace an entry; auto-slug title → group.
    note_read     — dump a single active group's entries, slim or single.
    note_use      — refresh an entry's `last_used`.
    note_recall   — read a cold-storage entry, no mutation to cold side.
    note_comment  — attach an ephemeral TODO to an entry, marks dirty.
    note_maintain — mechanical work (cold-evict, index) + return dirty list.
    note_rewrite  — sole dirty-clearing entry point.
    note_move     — mechanically relocate a group to another category (hierarchy maintenance).
    note_rename_category — rename a category path (exact match), updating its groups' prefixes.

Design decisions honored (see hermes-memory-provider skill):
    - Python detects, LLM decides, note_rewrite persists.
    - note_maintain NEVER clears the dirty flag on its own.
    - Cold storage is an append-to-latest queue keyed on the newest cold
      batch's `created` timestamp — not a per-day partition.
    - Entries are the atomic unit of memory; groups are grouping
      containers with group-level dirty.
    - Every mutation goes through a single write connection guarded by
      `self._lock` — matches the "one connection, one lock" resolution
      of the audit pitfall #14.

Terminology: a `group` is the thematic container of similar entries —
the DB-side counterpart of what exports as one `category/slug.md` file.
Cold storage holds time-queue `batches`, not topical groups.
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
DEFAULT_MAX_COLD_BATCHES = 50
DEFAULT_MAX_ACTIVE_GROUP_SIZE_BYTES = 50 * 1024  # 50 KB soft cap
DEFAULT_MAX_GROUPS_PER_CATEGORY = 50
DEFAULT_MAX_CATEGORY_DEPTH = 3  # detection threshold only — never enforced
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
        self._max_cold_batches = DEFAULT_MAX_COLD_BATCHES
        self._max_active_group_size = DEFAULT_MAX_ACTIVE_GROUP_SIZE_BYTES
        self._max_groups_per_category = DEFAULT_MAX_GROUPS_PER_CATEGORY
        self._max_category_depth = DEFAULT_MAX_CATEGORY_DEPTH
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
            "Reading path: scan the index below first to spot the right group, "
            "then `note_read(path)` for a slim headers overview, then "
            "`note_read(path, entry_header)` to fetch just the entry you want — "
            "cheap on context. Only during maintenance (processing a dirty group) "
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
                    "prompt for matching group titles. Only use this search when "
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
                    "meaningful title — it determines the group's slug/filename "
                    "and helps future retrieval via INDEX. Do NOT use generic "
                    "titles like 'note' or 'memo'."
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
                    "Read a single note entry (default) or a slim group "
                    "overview. Pass `entry_header` to fetch that specific "
                    "entry's full content — this is the token-efficient "
                    "default for day-to-day recall. Omit `entry_header` to "
                    "get just the group's title + headers list, then decide "
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
                    "Read every entry in a group with full content. Use this "
                    "during MAINTENANCE (when processing a dirty group so you "
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
                    "cold-storage batch. Returns the content — use note_write "
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
                    "group dirty so it surfaces in the next note_maintain. "
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
                    "group detection, INDEX regeneration. Returns "
                    "`dirty_groups`, `oversized_groups`, "
                    "`overpopulated_categories` — the LLM must resolve each "
                    "by reading the group and calling note_rewrite. "
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
                    "Replace a group's entries with the given curated list, "
                    "clearing dirty and consuming any comments. entries=[] "
                    "deletes the group. This is the SOLE dirty-clearing tool."
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
            {
                "name": "note_move",
                "description": (
                    "Move a group to another category (its slug/filename "
                    "stays the same). Use this for hierarchy maintenance: "
                    "promote a group up a level, nest it deeper, or merge "
                    "categories by moving all groups of one category into "
                    "another. Errors if the target path already exists — "
                    "merge via note_rewrite first or pick another category. "
                    "Does not mark dirty."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "current group path like 'category/slug.md'"},
                        "new_category": {"type": "string", "description": "target category path, e.g. 'game/br' or 'game'"},
                    },
                    "required": ["path", "new_category"],
                },
            },
            {
                "name": "note_rename_category",
                "description": (
                    "Rename a category path (exact match). All groups "
                    "directly in that category get the new prefix; "
                    "subcategories are NOT affected. Use for fixing "
                    "category names; use note_move to relocate whole "
                    "sub-trees. Errors if any target path already exists."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "old_category": {"type": "string"},
                        "new_category": {"type": "string"},
                    },
                    "required": ["old_category", "new_category"],
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

        # If the group already exists, append; otherwise create fresh.
        existing = storage.get_group_by_path(self._conn, path)
        if existing:
            group_id = existing.id
            # Preserve the group's existing title/tags unless caller changed.
            merged_tags = sorted(set(existing.tags) | set(tags))
            storage.upsert_group(
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
            group_id = storage.upsert_group(
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
            self._conn, group_id, header=header, content=content,
            last_used=storage._now(),
        )
        return {
            "status": "ok",
            "path": path,
            "group_id": group_id,
            "entry_id": entry_id,
            "created_new_group": existing is None,
        }

    def _tool_note_read(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read a single entry (default) or a slim group overview.

        Two modes — never returns full content for all entries:
          * ``entry_header`` given → return that single entry.
          * ``entry_header`` omitted → return a slim overview
            ``{path, title, category, tags, dirty, headers: [...]}``
            so the LLM can pick which entry to fetch next.

        For maintenance workflows that legitimately need every entry's
        full content, call ``note_read_group`` instead.
        """
        path = args["path"]
        row = storage.get_group_by_path(self._conn, path)
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
        """Read every entry in a group — the maintenance-time reader.

        Kept as a separate tool from ``note_read`` so day-to-day recall
        can stay lean while maintenance workflows still get the full
        neighbor context they need to merge / dedupe / re-organize.
        """
        path = args["path"]
        row = storage.get_group_by_path(self._conn, path)
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
        row = storage.get_group_by_path(self._conn, path)
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
        row = storage.get_group_by_path(self._conn, path)
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
        row = storage.get_group_by_path(self._conn, path)
        if row is None:
            return {"error": f"note not found: {path}"}
        raw_entries = args.get("entries") or []
        if not raw_entries:
            storage.delete_group(self._conn, row.id)
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

    def _tool_note_move(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        new_category = (args.get("new_category") or "").strip().strip("/") or "uncategorized"
        row = storage.get_group_by_path(self._conn, path)
        if row is None:
            return {"error": f"note not found: {path}"}
        new_path = storage.build_path(new_category, row.slug)
        if new_path == row.path:
            return {"status": "ok", "path": row.path, "old_path": row.path,
                    "category": new_category, "group_id": row.id, "unchanged": True}
        if storage.get_group_by_path(self._conn, new_path) is not None:
            return {"error": f"conflict: {new_path} already exists — merge via note_rewrite or pick another category"}
        storage.move_group(self._conn, row.id, new_category=new_category, new_path=new_path)
        storage._fts_rebuild_for_group(self._conn, row.id)
        return {"status": "ok", "path": new_path, "old_path": row.path,
                "category": new_category, "group_id": row.id}

    def _tool_note_rename_category(self, args: dict[str, Any]) -> dict[str, Any]:
        old_category = (args.get("old_category") or "").strip().strip("/")
        new_category = (args.get("new_category") or "").strip().strip("/")
        if not old_category or not new_category:
            return {"error": "old_category and new_category are required"}
        if old_category == new_category:
            return {"status": "ok", "renamed": 0,
                    "old_category": old_category, "new_category": new_category}
        groups = storage.list_groups(self._conn, category=old_category)
        if not groups:
            return {"error": f"category not found or empty: {old_category}"}
        # Conflict pre-check — abort whole rename on any clash.
        for g in groups:
            new_path = new_category + g.path[len(old_category):]
            clash = storage.get_group_by_path(self._conn, new_path)
            if clash is not None and clash.id != g.id:
                return {"error": f"conflict: {new_path} already exists — merge first"}
        for g in groups:
            new_path = new_category + g.path[len(old_category):]
            storage.move_group(self._conn, g.id, new_category=new_category, new_path=new_path)
            storage._fts_rebuild_for_group(self._conn, g.id)
        return {"status": "ok", "renamed": len(groups),
                "old_category": old_category, "new_category": new_category}

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
            batch_id = storage.get_or_create_cold_batch_for_today(self._conn)
            for e in stale:
                storage.move_entry_to_cold(self._conn, e.id, cold_batch_id=batch_id)
                cold_moved += 1

        # 2) Enforce cold-batch cap — sole physical-delete path in the system.
        cold_batches_pruned = storage.enforce_cold_batch_limit(
            self._conn, max_batches=self._max_cold_batches
        )

        # 3) Detect oversized groups & overpopulated categories → force dirty.
        oversized = self._detect_oversized_groups()
        overpop = self._detect_overpopulated_categories()
        for g in oversized:
            row = storage.get_group_by_path(self._conn, g["path"])
            if row:
                storage.mark_dirty(self._conn, row.id, True)
        for entry in overpop:
            for p in entry["direct_groups"][
                : max(0, len(entry["direct_groups"]) - self._max_groups_per_category)
            ]:
                row = storage.get_group_by_path(self._conn, p)
                if row:
                    storage.mark_dirty(self._conn, row.id, True)

        # 4) Enumerate dirty groups — LLM must resolve each with note_rewrite.
        dirty_groups = storage.list_groups(self._conn, dirty_only=True)
        dirty_group_paths = [g.path for g in dirty_groups]

        return {
            "dirty_groups": dirty_group_paths,
            "cold_moved": cold_moved,
            "cold_batches_pruned": cold_batches_pruned,
            "oversized_groups": oversized,
            "overpopulated_categories": overpop,
            "deep_categories": self._detect_deep_categories(),
            "hierarchy_summary": self._hierarchy_summary(),
        }

    # ---- maintenance detectors -------------------------------------------

    def _detect_oversized_groups(self) -> list[dict[str, Any]]:
        """Groups whose rendered markdown would exceed the size cap.

        We compute the size against the *rendered* form because that's
        what the LLM will see when it reads the group — bytes on disk
        are the honest signal for whether a split is warranted.
        """
        oversized: list[dict[str, Any]] = []
        for g in storage.list_groups(self._conn):
            entries = storage.list_entries(self._conn, g.id)
            text = markdown_io.render_file(
                {"title": g.title, "tags": g.tags, "dirty": g.dirty,
                 "created": g.created, "updated": g.updated},
                [markdown_io.ParsedEntry(
                    header=e.header, content=e.content,
                    last_used=e.last_used, comments=e.comments,
                ) for e in entries],
            )
            size = len(text.encode("utf-8"))
            if size > self._max_active_group_size:
                oversized.append({
                    "path": g.path,
                    "size_bytes": size,
                    "size_kb": round(size / 1024, 1),
                })
        return oversized

    def _detect_deep_categories(self) -> list[str]:
        """Category paths deeper than the suggested max depth.

        Report-only — never marks dirty or blocks writes; the LLM
        decides how to flatten (see maintenance skill).
        """
        depths: dict[str, int] = {}
        for g in storage.list_groups(self._conn):
            depth = len([s for s in (g.category or "").split("/") if s])
            if depth > self._max_category_depth:
                depths[g.category] = depth
        return sorted(depths)

    def _hierarchy_summary(self) -> dict[str, int]:
        """Per-level node counts so the LLM can see the tree shape."""
        tree = export_mod._category_tree(storage.list_groups(self._conn))

        def walk(nodes: list[dict[str, Any]]) -> list[int]:
            depths: list[int] = []
            for node in nodes:
                depths.append(
                    len([s for s in (node["path"] or "").split("/") if s])
                )
                depths.extend(walk(node["children"]))
            return depths

        out: dict[str, int] = {}
        for depth in walk(tree):
            key = f"depth{depth}" if depth <= 3 else "depth4+"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))

    def _detect_overpopulated_categories(self) -> list[dict[str, Any]]:
        """Categories whose node (direct subcategories + direct groups)
        exceeds the cap.  Every tree node counts, not just leaves."""
        groups = storage.list_groups(self._conn)
        tree = export_mod._category_tree(groups)

        def walk(nodes: list[dict[str, Any]]):
            for node in nodes:
                yield node
                yield from walk(node["children"])

        out: list[dict[str, Any]] = []
        for node in walk(tree):
            child_count = len(node["children"]) + len(node["groups"])
            if child_count > self._max_groups_per_category:
                direct_groups = sorted(node["groups"], key=lambda r: r.created)
                out.append({
                    "category": node["path"],
                    "child_count": child_count,
                    "subcategories": [c["path"] for c in node["children"]],
                    "direct_groups": [g.path for g in direct_groups],
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
