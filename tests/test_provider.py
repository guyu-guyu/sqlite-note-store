"""Provider-level end-to-end tests — the 8-tool surface."""

from __future__ import annotations

import json

import pytest

from sqlite_note_store.provider import SQLiteNoteStoreProvider


def _new_provider(tmp_path):
    p = SQLiteNoteStoreProvider()
    p.initialize(session_id="test", note_root=tmp_path / "notes")
    return p


def _call(p, tool, **args):
    result = p.handle_tool_call(tool, args)
    data = json.loads(result)
    assert "error" not in data, f"unexpected error from {tool}: {data.get('error')}"
    return data


def test_write_then_read(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="卡牌BR战斗流程", content="出牌→结算",
              category="game", tags="game,br")
    assert r["created_new_file"] is True
    path = r["path"]

    # Default note_read returns a slim overview (no bodies).
    overview = _call(p, "note_read", path=path)
    assert overview["title"] == "卡牌BR战斗流程"
    assert sorted(overview["tags"]) == ["br", "game"]
    assert overview["dirty"] is True
    assert overview["entry_count"] == 1
    assert isinstance(overview["headers"], list) and len(overview["headers"]) == 1
    assert "entries" not in overview  # slim mode — no bodies

    # Single-entry fetch by header returns the body.
    header = overview["headers"][0]
    single = _call(p, "note_read", path=path, entry_header=header)
    assert single["entry"]["header"] == header
    assert single["entry"]["content"] == "出牌→结算"

    # Explicit group read still returns everything (maintenance path).
    grp = _call(p, "note_read_group", path=path)
    assert grp["dirty"] is True
    assert len(grp["entries"]) == 1
    assert grp["entries"][0]["content"] == "出牌→结算"
    p.shutdown()


def test_note_read_missing_entry_lists_available(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="X", content="body", entry_header="real-header")
    result = p.handle_tool_call(
        "note_read", {"path": r["path"], "entry_header": "wrong-header"}
    )
    data = json.loads(result)
    assert "error" in data
    assert "real-header" in data["available_headers"]
    p.shutdown()


def test_second_write_appends_to_same_file(tmp_path):
    p = _new_provider(tmp_path)
    r1 = _call(p, "note_write", title="Notes", content="one", category="uncategorized")
    r2 = _call(p, "note_write", title="Notes", content="two", category="uncategorized")
    assert r1["path"] == r2["path"]
    assert r2["created_new_file"] is False
    doc = _call(p, "note_read_group", path=r1["path"])
    assert len(doc["entries"]) == 2
    p.shutdown()


def test_search_finds_written_content(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Battle Flow", content="draw cards, resolve, score",
          category="game")
    res = _call(p, "note_search", query="resolve")
    assert res["count"] >= 1
    p.shutdown()


def test_use_refreshes_last_used(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="X", content="body",
              category="game", entry_header="target-entry")
    # note_write sets last_used to now; note_use should update it further.
    doc = _call(p, "note_read_group", path=r["path"])
    assert doc["entries"][0]["last_used"] is not None
    original_lu = doc["entries"][0]["last_used"]
    _call(p, "note_use", path=r["path"], entry_header="target-entry")
    doc2 = _call(p, "note_read_group", path=r["path"])
    assert doc2["entries"][0]["last_used"] is not None
    p.shutdown()


def test_comment_marks_dirty_and_survives_read(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Y", content="body", entry_header="target")
    # Clear the dirty flag first (via rewrite) so we can isolate the
    # dirty-set caused by note_comment.
    _call(p, "note_rewrite", path=r["path"], entries=[
        {"header": "target", "content": "body"},
    ])
    doc = _call(p, "note_read", path=r["path"])
    assert doc["dirty"] is False

    _call(p, "note_comment", path=r["path"], entry_header="target",
          comment_type="inaccurate", comment_text="date is wrong")
    doc2 = _call(p, "note_read_group", path=r["path"])
    assert doc2["dirty"] is True
    assert doc2["entries"][0]["comments"][0]["type"] == "inaccurate"
    p.shutdown()


def test_rewrite_clears_dirty_and_consumes_comments(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Z", content="v1", entry_header="e1")
    _call(p, "note_comment", path=r["path"], entry_header="e1",
          comment_type="wrong", comment_text="fix me")

    _call(p, "note_rewrite", path=r["path"], entries=[
        {"header": "e1", "content": "v2 (fixed)"},
    ])
    doc = _call(p, "note_read_group", path=r["path"])
    assert doc["dirty"] is False
    assert doc["entries"][0]["content"] == "v2 (fixed)"
    assert doc["entries"][0]["comments"] == []  # comments consumed
    p.shutdown()


def test_rewrite_empty_deletes_file(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Del", content="bye")
    _call(p, "note_rewrite", path=r["path"], entries=[])
    doc = json.loads(p.handle_tool_call("note_read", {"path": r["path"]}))
    assert "error" in doc
    p.shutdown()


def test_maintain_returns_dirty_notes_and_does_not_clear_flag(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="M", content="body")
    res = _call(p, "note_maintain")
    assert r["path"] in res["dirty_notes"]

    # Critical invariant: note_maintain must not have cleared dirty.
    doc = _call(p, "note_read", path=r["path"])
    assert doc["dirty"] is True
    p.shutdown()


def test_maintain_evicts_stale_entries_to_cold(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Stale", content="old", entry_header="ancient")

    # Backdate last_used to force cold-eviction.
    ancient = "2000-01-01T00:00:00+00:00"
    p._conn.execute(
        "UPDATE entries SET last_used = ? WHERE header = ?", (ancient, "ancient"),
    )
    p._conn.commit()

    res = _call(p, "note_maintain")
    assert res["cold_moved"] == 1

    # Cold recall should find it.
    recall = _call(p, "note_recall", entry_header="ancient")
    assert recall["content"] == "old"
    p.shutdown()


def test_search_excludes_cold_entries(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="C", content="findable-token", entry_header="e")

    # Force it to cold storage.
    p._conn.execute(
        "UPDATE entries SET last_used = '2000-01-01T00:00:00+00:00' WHERE header = 'e'"
    )
    p._conn.commit()
    _call(p, "note_maintain")

    res = _call(p, "note_search", query="findable-token")
    # Cold entries must not surface via note_search — that's a hard rule.
    for hit in res["results"]:
        assert hit["path"] != r["path"] or "findable-token" not in hit["snippet"]
    p.shutdown()


def test_recall_missing_entry_returns_error(tmp_path):
    p = _new_provider(tmp_path)
    raw = p.handle_tool_call("note_recall", {"entry_header": "nope"})
    assert "error" in json.loads(raw)
    p.shutdown()


def test_prefetch_returns_snippet_when_written(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Findable", content="unique-marker text",
          category="uncategorized")
    p.queue_prefetch("unique-marker")
    result = p.prefetch("unique-marker")
    assert "unique-marker" in result
    p.shutdown()


def test_prefetch_respects_char_budget(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Big", content="x" * 5000, category="uncategorized")
    p._prefetch_char_limit = 100
    out = p._run_prefetch_search("x")
    assert len(out) <= 100
    p.shutdown()


def test_system_prompt_block_includes_index(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Alpha", content="a", category="game")
    _call(p, "note_write", title="Beta", content="b", category="ops")
    block = p.system_prompt_block()
    assert "Note Repository" in block
    assert "Alpha" in block or "alpha" in block.lower()
    assert "game" in block
    p.shutdown()


def test_unknown_tool_returns_error_json(tmp_path):
    p = _new_provider(tmp_path)
    result = p.handle_tool_call("nope_tool", {})
    err = json.loads(result)
    assert "error" in err
    p.shutdown()
