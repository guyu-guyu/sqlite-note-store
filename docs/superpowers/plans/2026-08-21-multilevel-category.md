# 多层 Category 层级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持多段分类路径(`game/br/br-flow.md`),形成 领域/主题/子主题 树;新增 `note_move` / `note_rename_category` 工具让 LLM 维护层级;`note_maintain` 报告层级健康度。

**Architecture:** category 从单段字符串扩展为多段路径字符串(数据模型不变,`path` UNIQUE 天然支持);INDEX 从扁平分组改为缩进树;层级深度不做硬限制,由 `note_maintain` 检测报告(默认阈值 3 层)+ 维护 skill 规范,LLM 用新工具自行整理。

**Tech Stack:** Python 3 标准库(sqlite3),pytest,无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-21-multilevel-category-design.md`

## Global Constraints

- 数据模型不变:`groups` 表结构、`path` UNIQUE、`slug` 由 title 派生、`build_path` 不变、冷存储 `original_category` 存完整路径
- 深度不硬限制:代码不阻止、不标脏深层分类,只报告(`DEFAULT_MAX_CATEGORY_DEPTH = 3` 仅检测阈值)
- 不检测过稀/单链分类;不建独立分类表
- 机械层不替 LLM 做合并决策:path 冲突一律返回 error
- 工具语义:`note_move` 不标脏;`note_rename_category` 精确匹配(只改 `category == old` 的组,子分类不受影响——与 dashboard 现有行为一致)
- 所有 Python 文件保持零外部依赖;现有 43 项测试必须持续全绿

---

### Task 1: import 支持多段分类路径

**Files:**
- Modify: `sqlite_note_store/export.py`(`_import_active_group`,约 200 行附近)
- Test: `tests/test_export.py`(末尾追加)

**Interfaces:**
- Consumes: `schema.connect()`, `storage.upsert_group`, `storage.replace_entries`(现有)
- Produces: 无新接口;行为变化——导入 `a/b/c.md` 后 `group.category == "a/b"`、`group.path == "a/b/c.md"`

- [ ] **Step 1: 写失败测试**

在 `tests/test_export.py` 末尾追加:

```python
def test_import_multilevel_category_preserves_path(tmp_path):
    """A nested dir tree imports with the full prefix as category."""
    in_root = tmp_path / "in"
    (in_root / "game" / "br").mkdir(parents=True)
    (in_root / "game" / "br" / "flow.md").write_text(
        "---\ntitle: Flow\ndirty: false\n"
        "created: 2026-01-01T00:00:00+00:00\nupdated: 2026-01-01T00:00:00+00:00\n---\n\n"
        "## a\nA\n",
        encoding="utf-8",
    )

    conn = schema.connect(tmp_path / "db")
    stats = export.import_from_directory(conn, in_root)
    assert stats["groups"] == 1
    row = storage.get_group_by_path(conn, "game/br/flow.md")
    assert row is not None
    assert row.category == "game/br"
    conn.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_export.py::test_import_multilevel_category_preserves_path -q`
Expected: FAIL — `assert row.category == "game/br"` 得到 `"game"`

- [ ] **Step 3: 实现**

在 `sqlite_note_store/export.py` 的 `_import_active_group` 中,把分类前缀从首段改为全部前缀段:

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_export.py -q`
Expected: PASS(含新测试与既有 6 项)

- [ ] **Step 5: 提交**

```bash
git add sqlite_note_store/export.py tests/test_export.py
git commit -m "feat: import preserves multi-level category paths"
```

---

### Task 2: INDEX 树形渲染 + 共享分类树构建

**Files:**
- Modify: `sqlite_note_store/export.py`(新增 `_category_tree`、`_render_tree`,重构 `_build_index_markdown`)
- Test: `tests/test_export.py`(末尾追加)

**Interfaces:**
- Produces:
  - `_category_tree(groups: list[storage.GroupRow]) -> list[dict[str, Any]]` — 节点形如 `{"name": str, "path": str, "groups": list[GroupRow], "children": list[node]}`,顶层为根列表;空/无分类归入 `"uncategorized"`
  - `_build_index_markdown(conn)` 输出改为缩进树(见下)
- Consumes: Task 5 复用 `export_mod._category_tree`

- [ ] **Step 1: 写失败测试**

在 `tests/test_export.py` 末尾追加:

```python
def test_index_renders_category_tree(tmp_path):
    """INDEX renders a nested category tree with indentation."""
    conn = schema.connect(tmp_path / "db")
    storage.upsert_group(
        conn, path="game/br/flow.md", category="game/br", slug="flow",
        title="Flow", tags=[], dirty=True,
        created="2026-01-01T00:00:00+00:00", updated="2026-01-01T00:00:00+00:00",
    )
    storage.append_entry(conn, storage.get_group_by_path(conn, "game/br/flow.md").id,
                         header="h", content="c")
    storage.upsert_group(
        conn, path="game/fps/weapon.md", category="game/fps", slug="weapon",
        title="Weapon", tags=[], dirty=False,
        created="2026-01-02T00:00:00+00:00", updated="2026-01-02T00:00:00+00:00",
    )
    storage.append_entry(conn, storage.get_group_by_path(conn, "game/fps/weapon.md").id,
                         header="h", content="c")
    conn.commit()

    text = export._build_index_markdown(conn)
    conn.close()

    assert "## game" in text
    assert "- br" in text
    assert "  - [Flow](game/br/flow.md) — 1 entries *(dirty)*" in text
    assert "- fps" in text
    assert "  - [Weapon](game/fps/weapon.md) — 1 entries" in text
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_export.py::test_index_renders_category_tree -q`
Expected: FAIL — 当前输出是扁平的 `## game/br` + `## game/fps`,没有缩进

- [ ] **Step 3: 实现**

在 `sqlite_note_store/export.py` 中,把 `_build_index_markdown` 的扁平分组逻辑替换为树构建 + 树渲染:

```python
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
```

再把 `_build_index_markdown` 中 `# Group by category.` 到冷存储部分之前的整段(原扁平 `by_cat` 逻辑)替换为:

```python
    # Group by category — rendered as a nested tree.
    counts = {
        r["group_id"]: r["n"]
        for r in conn.execute(
            "SELECT group_id, COUNT(*) AS n FROM entries GROUP BY group_id"
        )
    }
    _render_tree(_category_tree(groups), 0, lines, counts)
```

(其余部分——统计行、冷存储段——保持不变。注意:替换后 `by_cat` 不再需要。)

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_export.py -q`
Expected: PASS(新测试 + 既有 7 项;既有 `test_export_writes_expected_shape` 只断言 INDEX.md 存在,不受影响)

- [ ] **Step 5: 提交**

```bash
git add sqlite_note_store/export.py tests/test_export.py
git commit -m "feat: render INDEX as nested category tree"
```

---

### Task 3: `note_move` 工具

**Files:**
- Modify: `sqlite_note_store/storage.py`(新增 `move_group`)
- Modify: `sqlite_note_store/provider.py`(`get_tool_schemas` 加 schema;新增 `_tool_note_move`)
- Test: `tests/test_provider.py`(末尾追加)

**Interfaces:**
- Produces:
  - `storage.move_group(conn, group_id: int, *, new_category: str, new_path: str) -> None` — UPDATE groups SET category/path/updated
  - 工具 `note_move(path: str, new_category: str)` → `{status, path, old_path, category, group_id}` 或 `{error}`
- Consumes: 现有 `storage.get_group_by_path` / `build_path` / `_fts_rebuild_for_group`(该函数已具备"重插该组全部 FTS 行并刷新 category/group_path 列"的能力,等价于 spec 中的 `_fts_refresh_group_meta`,直接复用,不新增)

- [ ] **Step 1: 写失败测试**

在 `tests/test_provider.py` 末尾追加:

```python
def test_note_move_relocates_group(tmp_path):
    p = _new_provider(tmp_path)
    r = _call(p, "note_write", title="Flow", content="body",
              category="game/br", entry_header="e1")
    moved = _call(p, "note_move", path=r["path"], new_category="game/card")
    assert moved["status"] == "ok"
    assert moved["path"] == "game/card/Flow.md"  # slug 保留大小写,不转小写
    assert moved["old_path"] == "game/br/Flow.md"

    # Old path gone, new path readable with entries intact.
    old = json.loads(p.handle_tool_call("note_read", {"path": "game/br/Flow.md"}))
    assert "error" in old
    doc = _call(p, "note_read_group", path="game/card/Flow.md")
    assert doc["entries"][0]["header"] == "e1"

    # FTS reflects the new path (group meta columns refreshed).
    res = _call(p, "note_search", query="body")
    assert any(h["path"] == "game/card/Flow.md" for h in res["results"])
    p.shutdown()


def test_note_move_conflict_returns_error(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="Flow", content="one", category="game/br")
    _call(p, "note_write", title="Flow", content="two", category="game/card")
    raw = p.handle_tool_call(
        "note_move", {"path": "game/br/Flow.md", "new_category": "game/card"}
    )
    assert "error" in json.loads(raw)
    p.shutdown()


def test_note_move_missing_group_returns_error(tmp_path):
    p = _new_provider(tmp_path)
    raw = p.handle_tool_call("note_move", {"path": "nope/x.md", "new_category": "y"})
    assert "error" in json.loads(raw)
    p.shutdown()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_provider.py::test_note_move_relocates_group tests/test_provider.py::test_note_move_conflict_returns_error tests/test_provider.py::test_note_move_missing_group_returns_error -q`
Expected: FAIL — `unknown tool: note_move`

- [ ] **Step 3: 实现 move_group(storage.py)**

在 `sqlite_note_store/storage.py` 的 `delete_group` 之后新增:

```python
def move_group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    new_category: str,
    new_path: str,
) -> None:
    """Relocate a group to a new category/path (slug unchanged).

    Mechanical move only — does NOT touch dirty (content is unchanged)
    and does NOT check conflicts; callers validate new_path first.
    """
    conn.execute(
        "UPDATE groups SET category = ?, path = ?, updated = ? WHERE id = ?",
        (new_category, new_path, _now(), group_id),
    )
```

- [ ] **Step 4: 实现 provider schema + handler(provider.py)**

在 `get_tool_schemas` 返回列表末尾(`note_rewrite` 条目之后)追加两个 schema 中的第一个:

```python
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
```

在 `_tool_note_rewrite` 之后新增 handler:

```python
    def _tool_note_move(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        new_category = (args.get("new_category") or "").strip() or "uncategorized"
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
```

- [ ] **Step 5: 运行确认通过**

Run: `python3 -m pytest tests/test_provider.py -q`
Expected: PASS(新增 3 项 + 既有全部)

- [ ] **Step 6: 提交**

```bash
git add sqlite_note_store/storage.py sqlite_note_store/provider.py tests/test_provider.py
git commit -m "feat: add note_move tool for hierarchy maintenance"
```

---

### Task 4: `note_rename_category` 工具

**Files:**
- Modify: `sqlite_note_store/provider.py`(`get_tool_schemas` 追加;新增 `_tool_note_rename_category`)
- Test: `tests/test_provider.py`(末尾追加)

**Interfaces:**
- Produces: 工具 `note_rename_category(old_category: str, new_category: str)` → `{status, renamed: int, old_category, new_category}` 或 `{error}`
- Consumes: Task 3 的 `storage.move_group` + `storage._fts_rebuild_for_group`;现有 `storage.list_groups(conn, category=...)`(精确匹配整串分类路径)

- [ ] **Step 1: 写失败测试**

在 `tests/test_provider.py` 末尾追加:

```python
def test_note_rename_category_renames_groups(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="A", content="a", category="game/br")
    _call(p, "note_write", title="B", content="b", category="game/br")
    res = _call(p, "note_rename_category", old_category="game/br", new_category="game/card")
    assert res["status"] == "ok"
    assert res["renamed"] == 2
    doc = _call(p, "note_read_group", path="game/card/a.md")
    assert doc["title"] == "A"
    p.shutdown()


def test_note_rename_category_conflict_aborts_whole(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="A", content="a", category="game/br")
    _call(p, "note_write", title="A", content="dup", category="game/card")
    raw = p.handle_tool_call(
        "note_rename_category", {"old_category": "game/br", "new_category": "game/card"}
    )
    assert "error" in json.loads(raw)
    # Nothing partially applied: old path still readable.
    _call(p, "note_read_group", path="game/br/a.md")
    p.shutdown()


def test_note_rename_category_empty_or_missing_errors(tmp_path):
    p = _new_provider(tmp_path)
    raw = p.handle_tool_call("note_rename_category", {"old_category": "nope", "new_category": "x"})
    assert "error" in json.loads(raw)
    raw2 = p.handle_tool_call("note_rename_category", {"old_category": "", "new_category": "x"})
    assert "error" in json.loads(raw2)
    p.shutdown()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_provider.py::test_note_rename_category_renames_groups tests/test_provider.py::test_note_rename_category_conflict_aborts_whole tests/test_provider.py::test_note_rename_category_empty_or_missing_errors -q`
Expected: FAIL — `unknown tool: note_rename_category`

- [ ] **Step 3: 实现**

在 `get_tool_schemas` 中 `note_move` 条目之后追加:

```python
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
```

在 `_tool_note_move` 之后新增 handler:

```python
    def _tool_note_rename_category(self, args: dict[str, Any]) -> dict[str, Any]:
        old_category = (args.get("old_category") or "").strip()
        new_category = (args.get("new_category") or "").strip()
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_provider.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add sqlite_note_store/provider.py tests/test_provider.py
git commit -m "feat: add note_rename_category tool"
```

---

### Task 5: `note_maintain` 层级检测扩展

**Files:**
- Modify: `sqlite_note_store/provider.py`(常量、`_tool_note_maintain`、`_detect_overpopulated_categories`、新增 `_detect_deep_categories` / `_hierarchy_summary`)
- Test: `tests/test_provider.py`(末尾追加)

**Interfaces:**
- Produces:
  - `DEFAULT_MAX_CATEGORY_DEPTH = 3`(模块级常量)
  - `note_maintain` 返回新增 `deep_categories: list[str]`(段数 > 3 的分类路径,排序去重)与 `hierarchy_summary: dict[str, int]`(键 `depth1`/`depth2`/`depth3`/`depth4+`,值为分类节点数)
  - `overpopulated_categories` 形状变更:`[{category, child_count, subcategories, direct_groups}]`(原 `[{category, file_count, files}]`)
- Consumes: Task 2 的 `export_mod._category_tree`

- [ ] **Step 1: 写失败测试**

在 `tests/test_provider.py` 末尾追加:

```python
def test_maintain_reports_hierarchy_shape(tmp_path):
    p = _new_provider(tmp_path)
    _call(p, "note_write", title="A", content="a", category="game/br")
    _call(p, "note_write", title="B", content="b", category="game/br/x/y/z")
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
    _call(p, "note_write", title="A", content="a", category="game")
    _call(p, "note_write", title="B", content="b", category="game")
    _call(p, "note_write", title="C", content="c", category="game")
    _call(p, "note_write", title="D", content="d", category="game/br")
    res = _call(p, "note_maintain")
    # Node 'game' holds 3 direct groups + 1 subcategory = 4 > 2.
    over = [o for o in res["overpopulated_categories"] if o["category"] == "game"]
    assert len(over) == 1
    assert over[0]["child_count"] == 4
    assert set(over[0]["subcategories"]) == {"game/br"}
    # Out-of-cap direct groups (3 - 2 = 1, oldest first) got force-dirtied.
    dirty = _call(p, "note_maintain")["dirty_groups"]
    assert "game/A.md" in dirty  # slug 保留大小写
    p.shutdown()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_provider.py::test_maintain_reports_hierarchy_shape tests/test_provider.py::test_maintain_detects_overpopulated_intermediate_node -q`
Expected: FAIL — `hierarchy_summary` 键不存在

- [ ] **Step 3: 实现**

在 `provider.py` 模块常量区(`DEFAULT_MAX_GROUPS_PER_CATEGORY = 50` 之后)新增:

```python
DEFAULT_MAX_CATEGORY_DEPTH = 3  # detection threshold only — never enforced
```

在 `__init__` 的配置区(`self._max_groups_per_category = DEFAULT_MAX_GROUPS_PER_CATEGORY` 之后)新增:

```python
        self._max_category_depth = DEFAULT_MAX_CATEGORY_DEPTH
```

把 `_tool_note_maintain` 的返回语句替换为:

```python
        return {
            "dirty_groups": dirty_group_paths,
            "cold_moved": cold_moved,
            "cold_batches_pruned": cold_batches_pruned,
            "oversized_groups": oversized,
            "overpopulated_categories": overpop,
            "deep_categories": self._detect_deep_categories(),
            "hierarchy_summary": self._hierarchy_summary(),
        }
```

把 `_detect_overpopulated_categories` 整体替换为(节点级检测,复用 Task 2 的树):

```python
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
```

`_tool_note_maintain` 中原有的超限标脏循环也要同步改为新键(替换该循环):

```python
        for entry in overpop:
            for p in entry["direct_groups"][
                : max(0, len(entry["direct_groups"]) - self._max_groups_per_category)
            ]:
                row = storage.get_group_by_path(self._conn, p)
                if row:
                    storage.mark_dirty(self._conn, row.id, True)
```

在 `_detect_oversized_groups` 之前新增两个检测器:

```python
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
        depths: dict[str, int] = {}
        for g in storage.list_groups(self._conn):
            depths.setdefault(
                g.category,
                len([s for s in (g.category or "").split("/") if s]),
            )
        out: dict[str, int] = {}
        for depth in depths.values():
            key = f"depth{depth}" if depth <= 3 else "depth4+"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_provider.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add sqlite_note_store/provider.py tests/test_provider.py
git commit -m "feat: note_maintain reports category hierarchy health"
```

---

### Task 6: 维护 skill 与 README 同步

**Files:**
- Modify: `sqlite_note_store/skills/note-maintenance/SKILL.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 3/4/5 的工具与返回字段(`note_move`、`note_rename_category`、`deep_categories`、`hierarchy_summary`、新 `overpopulated_categories` 形状)

- [ ] **Step 1: SKILL.md 更新**

逐处修改 `sqlite_note_store/skills/note-maintenance/SKILL.md`:

1. frontmatter `description` 改为:`维护 SQLite 记忆库 — 读取脏组的所有条目，消化评论，合并去重，拆分超大组，迁移错分类条目，维护分类层级（深度 ≤ 3），通过 note_rewrite / note_move / note_rename_category 保存并清除脏标记。`
2. 「触发条件」追加两条:
   ```
   - `note_maintain` 返回 `deep_categories` 非空（层级过深，建议上提整理）
   - `note_maintain` 返回 `overpopulated_categories`（含中间节点）非空
   ```
3. 「关键概念」中 `category` 一行改为:`**`category`** 是 `groups` 表的字符串列，多段路径（如 `game/br`），对应导出目录的嵌套层级`
4. 新增「层级规范」章节(插在「核心概念」之后、「完整工作流」之前):

```markdown
## 层级规范（LLM 维护，代码不强制）

分类是多段路径（`game/br/...`），形成 领域/主题/子主题 树。**深度与宽度由 LLM 维护**——代码只检测报告，不阻止、不强制：

- **建议深度 ≤ 3 层**（领域/主题/子主题）。`note_maintain` 返回的 `deep_categories` 列出超过 3 层的分类路径，`hierarchy_summary` 给出每层节点数（`depth4+` 出现 = 该整理了）
- **建议每节点 ≤ 50 子节点**（子分类 + 直接组）。`overpopulated_categories` 现在对每个节点统计，中间节点超限也会报告
- **扩散读法**：从一条记忆出发向外检索时，先读同目录的组（最近），再看兄弟分类，最后才上探父分类——层级越近关联越强，不要在第一步就上探
- **层级维护动作**（响应式，仅在检测报告提示时做，不主动重构）：
  - 上提:`note_move(path, "父分类")` — 组提升一层
  - 下移:`note_move(path, "父/新子分类")` — 组归入更细主题
  - 合并分类:把 B 分类下所有组逐个 `note_move` 到 A，空分类随最后一个组移走自然消失
  - 改名:`note_rename_category("旧路径", "新路径")` — 精确匹配，只改直接挂在该分类下的组，子分类不受影响；改整个子树请用 `note_move` 逐个搬
  - 冲突时机械层会报错：目标 path 已存在 → 先 `note_rewrite` 合并或换分类
```

5. 「完整工作流 第一步」的返回说明同步(替换 `overpopulated_categories` 一行并追加两个字段):

```markdown
- `overpopulated_categories`: 超限节点的列表（中间节点也算），`[{category, child_count, subcategories, direct_groups}]`——需要合并/迁移
- `deep_categories`: 深度超过 3 层的分类路径列表——需要上提整理
- `hierarchy_summary`: 每层节点数统计（`depth1`…`depth4+`）——看层级形状
```

6. 其余「文件」措辞保持上一轮已统一为「组」的现状，不重复修改。

- [ ] **Step 2: README.md 更新**

1. 「特性」第一条的 9 个工具改为 11 个:`note_search / note_write / note_read / note_read_group / note_use / note_recall / note_comment / note_maintain / note_rewrite / note_move / note_rename_category`
2. 「每轮注入的内容」示例中 `## game` 段改为树形:

```markdown
## game
- br
  - [卡牌BR战斗流程](game/br/br-flow.md) — 8 entries
  - [局外系统](game/br/meta.md) — 12 entries *(dirty)*
- fps
  - [武器平衡](game/fps/weapon.md) — 3 entries
```

3. 「工具形态」末尾追加两节(在 `note_rewrite` 之后、Dashboard API 表格之前):

```markdown
### `note_move(path, new_category)`

把组移动到另一个分类（slug/文件名不变）。用于层级维护：上提、下移、合并分类。目标 path 已存在时报错（先合并或换分类）。**不标脏**——内容没变，只是位置变。

### `note_rename_category(old_category, new_category)`

重命名一个分类路径（精确匹配），该分类下的所有组同步改前缀。**子分类不受影响**；移动整个子树请用 `note_move` 逐个搬。任何目标 path 冲突则整体中止。
```

4. 「设计理念 ↔ 实现」表格追加一行:

```markdown
| 层级由 LLM 维护，代码只检测报告 | `note_maintain` 返回 `deep_categories` + `hierarchy_summary`；深度不强制 |
```

5. 模块结构图 `provider.py` 一行改为:`provider.py — MemoryProvider 门面类 + 11 个 tool 处理器 + Dashboard API`

- [ ] **Step 3: 验证文档引用一致**

Run: `grep -n "note_move\|note_rename_category\|deep_categories\|hierarchy_summary" README.md sqlite_note_store/skills/note-maintenance/SKILL.md | wc -l`
Expected: ≥ 10(文档已覆盖新工具与字段)

- [ ] **Step 4: 提交**

```bash
git add README.md sqlite_note_store/skills/note-maintenance/SKILL.md
git commit -m "docs: hierarchy norms and new tools in skill + README"
```

---

### Task 7: Dashboard 前端分类缩进

**Files:**
- Modify: `dashboard/dist/index.js`(INDEX 树渲染处,约 79-130 行)
- Modify: `dashboard/plugin_api.py`(`rename_category` 补注释)
- Test: `python3 -m py_compile` + `node --check`

**Interfaces:**
- Consumes: 后端 `/index` 返回不变(扁平 `category` 全路径字符串)
- Produces: 无 API 变化

- [ ] **Step 1: 改前端缩进渲染**

在 `dashboard/dist/index.js` 中:

1. 在文件顶部工具函数区(现有 `ago` 等 helper 附近)新增两个 helper:

```js
function catDepth(name) { return (name || "").split("/").filter(Boolean).length; }
function catLabel(name) { var s = (name || "").split("/").filter(Boolean); return s[s.length - 1] || name; }
```

2. 分类名渲染行(现有代码,约第 93 行):

```js
h("span", { style: isCatActive ? { color: "#3b82f6" } : {} }, cat.category || "(未分类)"),
```

改为:

```js
h("span", { style: Object.assign({ paddingLeft: (catDepth(cat.category) - 1) * 14 + "px" }, isCatActive ? { color: "#3b82f6" } : {}) }, catLabel(cat.category) || "(未分类)"),
```

- [ ] **Step 2: 改 plugin_api.py 注释**

在 `dashboard/plugin_api.py` 的 `rename_category` docstring 末尾追加一行:

```python
    """重命名分类——批量更新该分类下所有组的 category + path。
    支持多段路径（精确匹配整个分类路径，如 game/br → game/card）；
    子分类（更深的路径）不受影响。"""
```

- [ ] **Step 3: 验证**

Run: `python3 -m py_compile dashboard/plugin_api.py && node --check dashboard/dist/index.js && python3 -m pytest tests/ -q`
Expected: 全部通过(43 + 新增项)

- [ ] **Step 4: 提交**

```bash
git add dashboard/dist/index.js dashboard/plugin_api.py
git commit -m "feat: dashboard renders multi-level categories indented"
```

---

### Task 8: 全量回归与收尾

**Files:**
- 无新文件

- [ ] **Step 1: 全量测试**

Run: `python3 -m pytest tests/ -q`
Expected: 43 + 12 新增 = 55 项全绿(实际新增数以各 Task 为准)

- [ ] **Step 2: 残留扫描**

Run: `grep -rn "file_count\|\[\"files\"\]\|by_cat" sqlite_note_store/ tests/`
Expected: 无输出(`file_count`/`files` 键已全部替换为 `child_count`/`direct_groups`)

- [ ] **Step 3: CLI 冒烟**

Run:

```bash
TMP=$(mktemp -d)
mkdir -p $TMP/src/game/br
printf -- "---\ntitle: Flow\ndirty: false\ncreated: 2026-08-01T00:00:00+00:00\nupdated: 2026-08-01T00:00:00+00:00\n---\n\n## a\nA\n" > $TMP/src/game/br/flow.md
python3 -m sqlite_note_store --db-root $TMP/db import $TMP/src --replace
python3 -m sqlite_note_store --db-root $TMP/db status
python3 -m sqlite_note_store --db-root $TMP/db export $TMP/out --clean
grep -A2 "## game" $TMP/out/INDEX.md
```

Expected: `groups=1`、`groups: 1`、导出 INDEX 含 `## game` + `- br` + 缩进的组行

- [ ] **Step 4: 提交收尾**

```bash
git status
```

(若有未提交改动,按所属 Task 提交;预期 Step 3 无残留)

---

## Self-Review 记录

**Spec 覆盖:** 多段 import(Task 1)✓ 树形 INDEX(Task 2)✓ note_move(Task 3)✓ note_rename_category(Task 4)✓ note_maintain 扩展(Task 5)✓ skill+README(Task 6)✓ dashboard(Task 7)✓ 测试与明确不做项(Task 8 + Global Constraints)✓
**占位符扫描:** 无 TBD/TODO;每个代码步骤含完整实现
**类型一致性:** `_category_tree` 节点形状在 Task 2 定义、Task 5 消费一致;`overpopulated_categories` 新形状在 Task 5 定义、Task 6 文档同步;`move_group` 签名 Task 3 定义、Task 4 复用一致;`_fts_rebuild_for_group` 复用现有函数(行为即 spec 的 `_fts_refresh_group_meta`)
