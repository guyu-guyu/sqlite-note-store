"""Markdown IO — parse and render the on-disk shape.

Rationale
---------
SQLite is the store of record; markdown is the interchange format.
This module owns the exact byte-level shape the markdown-note-store
plugin writes, so a SQLite-backed store can be re-exported to the same
directory tree and read back identically. Kept out of storage.py so
tests can round-trip strings without touching the DB.

On-disk shape (mirrors markdown-note-store v1.1.0):

    ---
    title: <string>
    tags: [t1, t2]        # optional
    dirty: true|false
    created: <ISO 8601>
    updated: <ISO 8601>
    ---

    ## <entry header> {last_used: <ISO 8601>}
    <entry body...>

    ## <entry header> {last_used: ..., comments: [{"type": ..., ...}]}
    <entry body...>

Cold storage files are plain markdown — the YAML block is intentionally
omitted (filename encodes the date; dirty/title are meaningless in a
time-queue archive).

Reference used (read-only):
  /projects/markdown-note-store-plugin/markdown_note_store/__init__.py
  helpers: _parse_yaml_front_matter, _build_yaml_front_matter,
           _parse_entries, _build_body_from_entries.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data holders
# ---------------------------------------------------------------------------


@dataclass
class ParsedEntry:
    """One '## <header>' block parsed out of a markdown body."""

    header: str
    content: str
    last_used: str | None = None
    comments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ParsedFile:
    """Full result of parsing a single active-note markdown file."""

    meta: dict[str, Any]  # YAML front-matter dict (title/tags/dirty/…).
    entries: list[ParsedEntry]


# ---------------------------------------------------------------------------
# YAML front matter — minimal parser to avoid taking a PyYAML dep.
# ---------------------------------------------------------------------------
#
# The reference plugin ships a hand-rolled YAML parser so we can stay
# dependency-free. We reuse that same shape here for byte-parity — the
# subset of YAML we ever emit is tiny:
#
#   key: scalar
#   key: [a, b, c]
#   key: true|false
#
# We deliberately do NOT support nested maps or multi-line scalars —
# nothing in the note repository ever needs them.

_FRONT_MATTER_DELIM = "---"


def parse_yaml_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split `text` into (meta_dict, body_string).

    Returns ({}, text) if no valid front matter is present — this
    matches the reference plugin's lenient behavior for hand-edited
    files.
    """
    if not text.startswith(_FRONT_MATTER_DELIM):
        return {}, text

    lines = text.splitlines()
    if len(lines) < 2 or lines[0].strip() != _FRONT_MATTER_DELIM:
        return {}, text

    # Find the closing delimiter.
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONT_MATTER_DELIM:
            end_idx = i
            break
    if end_idx is None:
        return {}, text

    meta_lines = lines[1:end_idx]
    body_lines = lines[end_idx + 1 :]
    # Drop the single blank line that _build_yaml_front_matter writes
    # right after the closing '---' so round-tripping doesn't grow the
    # body every rewrite.
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]

    meta: dict[str, Any] = {}
    for line in meta_lines:
        if ":" not in line:
            continue
        key, _, raw_val = line.partition(":")
        key = key.strip()
        val = raw_val.strip()
        meta[key] = _parse_yaml_scalar(val)

    return meta, "\n".join(body_lines)


def _parse_yaml_scalar(val: str) -> Any:
    """Coerce a single YAML value string into Python."""
    if val == "":
        return ""
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"').strip("'") for item in inner.split(",")]
    # Strip matched quotes.
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1]
    return val


def build_yaml_front_matter(meta: dict[str, Any]) -> str:
    """Serialize a meta dict back to the '---\\n…\\n---\\n\\n' block.

    Key ordering mirrors the reference plugin so diffs stay minimal:
    title, tags, dirty, created, updated, then any extras alphabetically.
    """
    if not meta:
        return ""

    preferred = ["title", "tags", "dirty", "created", "updated"]
    seen: set[str] = set()
    lines = [_FRONT_MATTER_DELIM]

    for key in preferred:
        if key in meta:
            lines.append(f"{key}: {_dump_yaml_scalar(meta[key])}")
            seen.add(key)

    for key in sorted(k for k in meta.keys() if k not in seen):
        lines.append(f"{key}: {_dump_yaml_scalar(meta[key])}")

    lines.append(_FRONT_MATTER_DELIM)
    lines.append("")  # trailing blank between YAML and body
    return "\n".join(lines) + "\n"


def _dump_yaml_scalar(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, list):
        parts = [str(v) for v in val]
        return "[" + ", ".join(parts) + "]"
    return str(val)


# ---------------------------------------------------------------------------
# Entry blocks — '## header {last_used: ..., comments: [...]}\\n<body>'
# ---------------------------------------------------------------------------
#
# The reference plugin appends inline JSON-flavored metadata to the
# header line so a plain-markdown reader still sees a normal '## …'
# heading, while a tolerant parser can pluck the metadata back out.
# We keep that exact scheme so byte-parity round-tripping works.

_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")
# Meta suffix: '{k1: v1, k2: v2}' at the tail of the header. We treat it
# permissively — try JSON first (with a wrapping '{...}'), fall back to
# regex extraction of two known keys only. Anything else lives inside
# `content`.
_META_SUFFIX_RE = re.compile(r"^(.*?)\s*(\{.*\})\s*$")


def parse_entries(body: str) -> list[ParsedEntry]:
    """Split a markdown body into entries at every '## ' boundary."""
    entries: list[ParsedEntry] = []
    current_header: str | None = None
    current_meta_json: str | None = None
    current_lines: list[str] = []

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        m = _HEADER_RE.match(line)
        if m:
            if current_header is not None:
                entries.append(
                    _finalize_entry(current_header, current_meta_json, current_lines)
                )
            header_line = m.group(1)
            # Extract inline metadata suffix if present.
            meta_match = _META_SUFFIX_RE.match(header_line)
            if meta_match and meta_match.group(2).startswith("{"):
                current_header = meta_match.group(1).strip()
                current_meta_json = meta_match.group(2)
            else:
                current_header = header_line.strip()
                current_meta_json = None
            current_lines = []
        else:
            if current_header is None:
                # Body text before the first header — ignored, matching
                # the reference plugin's "entries start at '## '" contract.
                continue
            current_lines.append(raw_line)

    if current_header is not None:
        entries.append(_finalize_entry(current_header, current_meta_json, current_lines))

    return entries


def _finalize_entry(
    header: str, meta_json: str | None, content_lines: list[str]
) -> ParsedEntry:
    last_used: str | None = None
    comments: list[dict[str, Any]] = []
    if meta_json:
        parsed = _tolerant_meta_json(meta_json)
        raw_lu = parsed.get("last_used")
        if isinstance(raw_lu, str) and raw_lu:
            last_used = raw_lu
        raw_c = parsed.get("comments")
        if isinstance(raw_c, list):
            comments = [c for c in raw_c if isinstance(c, dict)]

    # Strip leading/trailing blank lines so re-emit is stable.
    while content_lines and content_lines[0].strip() == "":
        content_lines.pop(0)
    while content_lines and content_lines[-1].strip() == "":
        content_lines.pop()

    return ParsedEntry(
        header=header,
        content="\n".join(content_lines),
        last_used=last_used,
        comments=comments,
    )


def _tolerant_meta_json(text: str) -> dict[str, Any]:
    """Best-effort parse of the '{...}' inline metadata block.

    Uses json.loads first because build_body writes strict JSON. Falls
    back to regex extraction of `last_used` for the rare case a human
    hand-edited the header — comments there are just given up on,
    because their structure is too rich for a regex to reconstruct
    faithfully.
    """
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    out: dict[str, Any] = {}
    lu_match = re.search(r"last_used:\s*([^,}\s]+)", text)
    if lu_match:
        out["last_used"] = lu_match.group(1).strip('"').strip("'")
    return out


def build_body_from_entries(entries: list[ParsedEntry]) -> str:
    """Render a list of entries back to markdown body text.

    Emits inline metadata only when the entry actually has any — a
    fresh entry with no last_used/comments gets a plain '## header'
    line, matching the reference plugin's minimal output.
    """
    parts: list[str] = []
    for e in entries:
        meta: dict[str, Any] = {}
        if e.last_used:
            meta["last_used"] = e.last_used
        if e.comments:
            meta["comments"] = e.comments
        if meta:
            suffix = " " + json.dumps(meta, ensure_ascii=False, separators=(", ", ": "))
        else:
            suffix = ""
        parts.append(f"## {e.header}{suffix}")
        parts.append("")
        if e.content.strip():
            parts.append(e.content.rstrip())
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def parse_file(text: str) -> ParsedFile:
    """One-shot parser: text → (meta, entries)."""
    meta, body = parse_yaml_front_matter(text)
    return ParsedFile(meta=meta, entries=parse_entries(body))


def render_file(meta: dict[str, Any], entries: list[ParsedEntry]) -> str:
    """One-shot renderer: (meta, entries) → text."""
    return build_yaml_front_matter(meta) + build_body_from_entries(entries)
