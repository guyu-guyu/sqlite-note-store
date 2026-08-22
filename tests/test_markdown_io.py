"""markdown_io round-trip tests.

These tests are the executable contract for the "data structure aligned
with markdown" requirement — every field the reference plugin writes
into a markdown file must survive a parse → render → parse cycle.
"""

from __future__ import annotations

import markdown_io


def test_parse_yaml_front_matter_minimal():
    text = (
        "---\n"
        "title: 卡牌BR\n"
        "dirty: true\n"
        "created: 2026-08-01T00:00:00+00:00\n"
        "updated: 2026-08-20T00:00:00+00:00\n"
        "---\n"
        "\n"
        "## e1\ncontent-1\n"
    )
    meta, body = markdown_io.parse_yaml_front_matter(text)
    assert meta["title"] == "卡牌BR"
    assert meta["dirty"] is True
    assert body.startswith("## e1")


def test_parse_yaml_front_matter_tags_list():
    text = "---\ntitle: T\ntags: [a, b, c]\ndirty: false\n---\n\n"
    meta, _ = markdown_io.parse_yaml_front_matter(text)
    assert meta["tags"] == ["a", "b", "c"]


def test_build_yaml_front_matter_ordering():
    """Preferred keys come first; extras sort alphabetically."""
    meta = {
        "updated": "u",
        "title": "T",
        "custom_z": "z",
        "custom_a": "a",
        "dirty": False,
        "created": "c",
    }
    out = markdown_io.build_yaml_front_matter(meta)
    lines = out.strip().splitlines()
    # First and last are '---'; check the order between them.
    assert lines[0] == "---"
    assert lines[-1] == "---"
    body_lines = lines[1:-1]
    order = [ln.split(":")[0] for ln in body_lines]
    assert order == ["title", "dirty", "created", "updated", "custom_a", "custom_z"]


def test_parse_entries_with_inline_meta():
    body = (
        '## first {"last_used": "2026-08-01T00:00:00+00:00"}\n'
        "line-1\n"
        "line-2\n"
        "\n"
        '## second {"last_used": "2026-08-02T00:00:00+00:00", '
        '"comments": [{"type": "wrong", "text": "fix me", "timestamp": "t"}]}\n'
        "body-of-second\n"
    )
    entries = markdown_io.parse_entries(body)
    assert len(entries) == 2
    assert entries[0].header == "first"
    assert entries[0].last_used == "2026-08-01T00:00:00+00:00"
    assert entries[0].content == "line-1\nline-2"
    assert entries[1].comments[0]["type"] == "wrong"


def test_parse_entries_bare_headers():
    body = "## a\nAAA\n\n## b\nBBB\n"
    entries = markdown_io.parse_entries(body)
    assert [e.header for e in entries] == ["a", "b"]
    assert entries[0].last_used is None
    assert entries[0].comments == []


def test_round_trip_stability():
    """parse → render → parse yields identical structure."""
    original = markdown_io.ParsedFile(
        meta={
            "title": "卡牌BR",
            "tags": ["game", "br"],
            "dirty": True,
            "created": "2026-08-01T00:00:00+00:00",
            "updated": "2026-08-20T00:00:00+00:00",
        },
        entries=[
            markdown_io.ParsedEntry(
                header="战斗流程",
                content="步骤 1: 出牌\n步骤 2: 结算",
                last_used="2026-08-15T00:00:00+00:00",
                comments=[],
            ),
            markdown_io.ParsedEntry(
                header="Bug 反馈",
                content="修复合服问题",
                last_used="2026-08-19T00:00:00+00:00",
                comments=[
                    {
                        "type": "needs_improvement",
                        "text": "add more detail",
                        "timestamp": "2026-08-20T00:00:00+00:00",
                    }
                ],
            ),
        ],
    )
    text = markdown_io.render_file(original.meta, original.entries)
    reparsed = markdown_io.parse_file(text)

    assert reparsed.meta["title"] == original.meta["title"]
    assert reparsed.meta["tags"] == original.meta["tags"]
    assert reparsed.meta["dirty"] is True
    assert len(reparsed.entries) == len(original.entries)
    for orig_e, new_e in zip(original.entries, reparsed.entries):
        assert new_e.header == orig_e.header
        assert new_e.content == orig_e.content
        assert new_e.last_used == orig_e.last_used
        assert new_e.comments == orig_e.comments


def test_no_front_matter_returns_empty_meta():
    text = "## just a heading\nno YAML above\n"
    meta, body = markdown_io.parse_yaml_front_matter(text)
    assert meta == {}
    assert body == text


def test_render_file_shape_matches_reference():
    """Sanity: the exact byte layout the reference plugin writes.

    Reference: markdown_note_store/__init__.py::_build_yaml_front_matter
    followed by _build_body_from_entries.
    """
    meta = {"title": "T", "dirty": False, "created": "c", "updated": "u"}
    entries = [markdown_io.ParsedEntry(header="h", content="c")]
    out = markdown_io.render_file(meta, entries)
    # Front matter → blank line → '## h' → blank → 'c' → trailing '\n'.
    assert out.startswith("---\ntitle: T\ndirty: false\ncreated: c\nupdated: u\n---\n\n")
    assert "## h\n" in out
    assert out.endswith("\n")
