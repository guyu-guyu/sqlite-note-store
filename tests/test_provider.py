"""Provider-level end-to-end tests — the 8-tool surface."""

from __future__ import annotations

import json

import pytest

from provider import SQLiteNoteStoreProvider


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
              path="game/卡牌BR战斗流程", tags="game,br")
    assert r["created_new_group"] is True
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


def test_second_write_appends_to_same_group(tmp_path):
    p = _new_provider(tmp_path)
    r1 = _call(p, "note_write", title="Notes", content="one")
    r2 = _call(p, "note_write", title="Notes", content="two")
    assert r1["path"] == r2["path"]
    assert r2["created_new_group"] is False
    doc = _call(p, "note_read_group", path=r1["path"])
    assert len(doc["entries"]) == 2
    p.shutdown()


def test_search_finds_written_content(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Battle Flow", content="draw cards, resolve, score",
          path="game/Battle-Flow")
    res = _call(p, "note_search", query="resolve")
    assert res["count"] >= 1
    p.shutdown()


def test_use_refreshes_last_used(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="X", content="body",
              path="game/X", entry_header="target-entry")
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


def test_rewrite_empty_deletes_group(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Del", content="bye")
    _call(p, "note_rewrite", path=r["path"], entries=[])
    doc = json.loads(p.handle_tool_call("note_read", {"path": r["path"]}))
    assert "error" in doc
    p.shutdown()


def test_maintain_returns_dirty_groups_and_does_not_clear_flag(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="M", content="body")
    res = _call(p, "note_maintain")
    assert r["path"] in res["dirty_groups"]

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
    _call(p, "note_write", title="Findable", content="unique-marker text")
    p.queue_prefetch("unique-marker")
    result = p.prefetch("unique-marker")
    assert "unique-marker" in result
    p.shutdown()


def test_prefetch_respects_char_budget(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Big", content="x" * 5000)
    p._prefetch_char_limit = 100
    out = p._run_prefetch_search("x")
    assert len(out) <= 100
    p.shutdown()


def test_system_prompt_block_includes_index(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Alpha", content="a", path="game/Alpha")
    _call(p, "note_write", title="Beta", content="b", path="ops/Beta")
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


def test_note_move_relocates_group(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Flow", content="body",
              path="game/br/Flow", entry_header="e1")
    moved = _call(p, "note_move", path=r["path"], new_category="game/card")
    assert moved["status"] == "ok"
    assert moved["path"] == "game/card/Flow"  # slug 保留大小写,不转小写
    assert moved["old_path"] == "game/br/Flow"

    # Old path gone, new path readable with entries intact.
    old = json.loads(p.handle_tool_call("note_read", {"path": "game/br/Flow"}))
    assert "error" in old
    doc = _call(p, "note_read_group", path="game/card/Flow")
    assert doc["entries"][0]["header"] == "e1"

    # FTS reflects the new path (group meta columns refreshed).
    res = _call(p, "note_search", query="body")
    assert any(h["path"] == "game/card/Flow" for h in res["results"])
    p.shutdown()


def test_note_move_conflict_returns_error(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Flow", content="one", path="game/br/Flow")
    _call(p, "note_write", title="Flow", content="two", path="game/card/Flow")
    raw = p.handle_tool_call(
        "note_move", {"path": "game/br/Flow.md", "new_category": "game/card"}
    )
    assert "error" in json.loads(raw)
    p.shutdown()


def test_note_move_missing_group_returns_error(tmp_path):
    p = _new_provider(tmp_path)
    raw = p.handle_tool_call("note_move", {"path": "nope/x", "new_category": "y"})
    assert "error" in json.loads(raw)
    p.shutdown()


def test_note_rename_category_renames_groups(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="A", content="a", path="game/br/A")
    _call(p, "note_write", title="B", content="b", path="game/br/B")
    res = _call(p, "note_rename_category", old_category="game/br", new_category="game/card")
    assert res["status"] == "ok"
    assert res["renamed"] == 2
    doc = _call(p, "note_read_group", path="game/card/A")
    assert doc["title"] == "A"
    p.shutdown()


def test_note_rename_category_conflict_aborts_whole(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="A", content="a", path="game/br/A")
    _call(p, "note_write", title="A", content="dup", path="game/card/A")
    raw = p.handle_tool_call(
        "note_rename_category", {"old_category": "game/br", "new_category": "game/card"}
    )
    assert "error" in json.loads(raw)
    # Nothing partially applied: old path still readable.
    _call(p, "note_read_group", path="game/br/A")
    p.shutdown()


def test_note_rename_category_empty_or_missing_errors(tmp_path):
    p = _new_provider(tmp_path)
    raw = p.handle_tool_call("note_rename_category", {"old_category": "nope", "new_category": "x"})
    assert "error" in json.loads(raw)
    raw2 = p.handle_tool_call("note_rename_category", {"old_category": "", "new_category": "x"})
    assert "error" in json.loads(raw2)
    p.shutdown()


def test_note_rename_group_renames_title_and_path(tmp_path):
    """改组名：title 与 slug/path 一起更新（分类不变），条目保留。"""
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="旧组名", content="body", path="game/旧组名",
          entry_header="e1")
    r = _call(p, "note_rename_group", path="game/旧组名", new_title="新组名")
    assert r["status"] == "ok"
    assert r["path"] == "game/新组名"
    assert r["old_path"] == "game/旧组名"
    assert r["title"] == "新组名"

    # 旧 path 失效，新 path 可读且条目完整
    old = json.loads(p.handle_tool_call("note_read", {"path": "game/旧组名"}))
    assert "error" in old
    doc = _call(p, "note_read_group", path="game/新组名")
    assert doc["title"] == "新组名"
    assert doc["category"] == "game"  # 分类不变
    assert len(doc["entries"]) == 1
    assert doc["entries"][0]["header"] == "e1"
    p.shutdown()


def test_note_rename_group_keeps_dirty_unchanged(tmp_path):
    """改名是机械动作：不清 dirty（内容没变），也不新增 dirty。"""
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="A组", content="x", path="game/A组",
          entry_header="e1")
    _call(p, "note_rewrite", path="game/A组",
          entries=[{"header": "e1", "content": "x"}])
    assert _call(p, "note_read", path="game/A组")["dirty"] is False

    _call(p, "note_rename_group", path="game/A组", new_title="B组")
    doc = _call(p, "note_read_group", path="game/B组")
    assert doc["dirty"] is False  # 改名前 clean → 改名后仍 clean
    p.shutdown()


def test_note_rename_group_conflict_returns_error(tmp_path):
    """新 path 已存在 → 报错，旧组保持原样。"""
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="A", content="a", path="game/A")
    _call(p, "note_write", title="B", content="b", path="game/B")
    raw = p.handle_tool_call("note_rename_group", {"path": "game/A", "new_title": "B"})
    assert "error" in json.loads(raw)
    _call(p, "note_read_group", path="game/A")  # 旧组未受影响
    p.shutdown()


def test_note_rename_group_refreshes_fts(tmp_path):
    """改名后 FTS 的 group_path/group_title 列必须同步，搜索命中新路径。"""
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="旧名", content="unique-body-token",
          path="ops/旧名")
    _call(p, "note_rename_group", path="ops/旧名", new_title="新名")
    res = _call(p, "note_search", query="unique-body-token")
    assert any(h["path"] == "ops/新名" for h in res["results"])
    p.shutdown()


def test_note_rename_group_missing_or_same_returns(tmp_path):
    """路径不存在报错；title 相同返回 unchanged。"""
    p = _new_provider(tmp_path)
    raw = p.handle_tool_call("note_rename_group", {"path": "nope/x", "new_title": "y"})
    assert "error" in json.loads(raw)
    _call(p, "note_write", title="A", content="a", path="game/A")
    r = _call(p, "note_rename_group", path="game/A", new_title="A")
    assert r["unchanged"] is True
    p.shutdown()


def test_maintain_reports_hierarchy_shape(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="A", content="a", path="game/br/A")
    _call(p, "note_write", title="B", content="b", path="game/br/x/y/z/B")
    res = _call(p, "note_maintain")
    # game(d1) / game.br(d2) / game.br.x(d3) / ...y(d4+) / ...z(d4+)
    assert res["hierarchy_summary"]["depth1"] == 1
    assert res["hierarchy_summary"]["depth2"] == 1
    assert res["hierarchy_summary"]["depth3"] == 1
    assert res["hierarchy_summary"]["depth4+"] == 2
    assert "game/br/x/y/z" in res["deep_categories"]
    assert "game/br" not in res["deep_categories"]
    p.shutdown()


def test_maintain_detects_overpopulated_intermediate_node(tmp_path):
    p = _new_provider(tmp_path)
    p._max_groups_per_category = 2
    _call(p, "note_write", title="A", content="a", path="game/A")
    _call(p, "note_write", title="B", content="b", path="game/B")
    _call(p, "note_write", title="C", content="c", path="game/C")
    _call(p, "note_write", title="D", content="d", path="game/br/D")
    res = _call(p, "note_maintain")
    # Node 'game' holds 3 direct groups + 1 subcategory = 4 > 2.
    over = [o for o in res["overpopulated_categories"] if o["category"] == "game"]
    assert len(over) == 1
    assert over[0]["child_count"] == 4
    assert set(over[0]["subcategories"]) == {"game/br"}
    # Out-of-cap direct groups (3 - 2 = 1, oldest first) got force-dirtied.
    dirty = _call(p, "note_maintain")["dirty_groups"]
    assert "game/A" in dirty  # slug 保留大小写
    p.shutdown()


def test_note_move_normalizes_trailing_slash(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Flow", content="body", path="game/br/Flow")
    moved = _call(p, "note_move", path=r["path"], new_category="game/card/")
    assert moved["path"] == "game/card/Flow"  # no double slash
    assert moved["category"] == "game/card"
    p.shutdown()


def test_note_write_by_path_lands_in_existing_group(tmp_path):
    """显式 path 时，即使 title 不同也必须落进已有组而不是新建。

    模拟“导入组”（slug 来自文件名、与 title 无关）：先建组再把 slug/path
    改成与 title 无关的值，然后传 path 写入——必须追加进该组。
    """
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="卡牌BR战斗流程", content="老条目",
              path="game/br/卡牌BR战斗流程", entry_header="e-old")
    p._conn.execute(
        "UPDATE groups SET slug='flow', path='game/br/flow' WHERE id=?",
        (r["group_id"],),
    )
    p._conn.commit()

    r2 = _call(p, "note_write", title="回合结算规则", content="抽牌→出牌→结算",
               path="game/br/flow")
    assert r2["created_new_group"] is False
    assert r2["path"] == "game/br/flow"

    doc = _call(p, "note_read_group", path="game/br/flow")
    assert doc["title"] == "卡牌BR战斗流程"  # 保留原标题，不按新 title 改名
    assert len(doc["entries"]) == 2          # 追加而非新建组
    assert doc["dirty"] is True              # 写入必须置 dirty
    assert doc["entries"][1]["content"] == "抽牌→出牌→结算"
    p.shutdown()


def test_note_write_path_is_normalized(tmp_path):
    """path 首尾斜杠/空串应归一化：空 path 回退到 title 派生逻辑。"""
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Notes", content="one")
    r2 = _call(p, "note_write", title="Notes", content="two",
               path=" / ")
    assert r2["path"] == r["path"]  # 空白 path 视为未传，同 title 落同组
    assert r2["created_new_group"] is False
    p.shutdown()


def test_note_write_new_path_creates_group(tmp_path):
    """path 不存在时在该位置新建组——path 即完整位置（分类/名字）。"""
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="卡牌BR战斗流程", content="y",
              path="game/br/flow")
    assert r["created_new_group"] is True
    assert r["path"] == "game/br/flow"
    doc = _call(p, "note_read_group", path="game/br/flow")
    assert doc["title"] == "卡牌BR战斗流程"
    assert doc["category"] == "game/br"  # category 从 path 拆出
    p.shutdown()
