# 多层 Category 层级设计

日期: 2026-08-21
状态: 待审阅

## 背景与目标

当前 `category` 只有一层:`groups.path = <category>/<slug>.md`,分类是平铺的。缺陷:

1. **无关联梯度**:同分类下的所有组关联度等价,LLM 从一组出发扩散时没有"远近"信号
2. **平铺膨胀**:每分类 50 组上限导致分类数量本身膨胀,且无法表达"更大主题"

目标:支持多段分类路径(`game/br/br-flow.md`),形成 领域/主题/子主题 树。核心价值是**关联梯度**——从出发组向外扩散时,同目录 > 兄弟目录 > 父分类,越早读到的关联性越强。深度不做硬限制,由维护 skill 规范 + `note_maintain` 检测报告,LLM 自行维护。

## 关键设计决策(已确认)

1. **方案 A:category 多段路径字符串**,不建独立分类表(分类仍由 groups 派生)
2. **深度不硬限制**:代码不阻止深层分类,`note_maintain` 只检测报告(默认阈值 3 层),LLM 维护
3. **新增工具 `note_move(path, new_category)`**:机械移动组
4. **新增工具 `note_rename_category(old_category, new_category)`**:机械重命名分类(前缀替换)
5. **`note_maintain` 扩展**:`deep_categories` + `hierarchy_summary` + `overpopulated_categories` 扩展到中间节点
6. **INDEX 树形渲染**:缩进树,叶子为组

## 数据模型

**不变**:`groups` 表结构、`path` UNIQUE、`slug`(由 title 派生,文件名最后一段)、`build_path()`、冷存储 `original_category`(存完整路径)。

**变化**:`category` 列语义从"单段"扩展为"多段路径字符串",如 `game/br`。`path = f"{category}/{slug}.md"` 天然支持多级目录。导出 `target.parent.mkdir(parents=True)` 已支持多级。

## 改动点

### 1. import(export.py)

`_import_active_group`:category 从 `parts[0]` 改为 `"/".join(parts[:-1])`(多段目录导入后保留完整层级)。根目录 md 落 `uncategorized` 不变。

### 2. INDEX 树形渲染(export.py `_build_index_markdown`)

按 `/` 分段构建分类树,深度遍历渲染:

```markdown
## game
- br
  - [卡牌BR战斗流程](game/br/br-flow.md) — 8 entries
  - [局外系统](game/br/meta.md) — 12 entries *(dirty)*
- fps
  - [武器平衡](game/fps/weapon.md) — 3 entries
```

- depth1 保持 `## name` 标题(延续现状样式),更深层缩进 2 空格
- 中间节点只显示名字;叶子是组链接(含 entry 数、dirty 标记)
- 分类节点渲染在其所有组/子分类之前,自顶向下深度优先

### 3. 新工具 `note_move(path, new_category)`(provider.py)

| 点 | 语义 |
|---|---|
| slug | 保持原 slug,新 path = `new_category/slug.md` |
| new_category 缺省/空 | 与 note_write 一致,默认 `uncategorized` |
| 冲突 | 新 path 已存在 → 返回 error(LLM 先合并或换分类) |
| dirty | 不标脏(内容未变,只变位置) |
| updated | 刷新 |
| FTS | 同步刷新该组所有条目的 `category`/`group_path` 列(新增 `_fts_refresh_group_meta`) |
| 返回 | `{status, path, old_path, category, group_id}` |

### 4. 新工具 `note_rename_category(old_category, new_category)`(provider.py)

- 校验:old 分类非空(存在组);new_category 非空
- 冲突:任何受影响组的新 path 已存在(排除自身)→ error,机械层不合并
- 执行:逐组更新 `category` + `path`(前缀替换),刷新 `updated`,同步 FTS(每组的 `_fts_refresh_group_meta`)
- 返回 `{status, renamed: N, old_category, new_category}`
- 空分类(无组)→ error"分类不存在或为空"

### 5. `note_maintain` 扩展(provider.py)

新增常量 `DEFAULT_MAX_CATEGORY_DEPTH = 3`(仅检测阈值,不标脏不阻止)。

- `deep_categories`:段数 > 3 的分类路径列表(如 `["game/br/advanced/deep"]`)——**只报告**
- `hierarchy_summary`:每层分类节点数,如 `{"depth1": 3, "depth2": 12, "depth3": 40, "depth4+": 2}`——LLM 一眼看出层级形状
- `overpopulated_categories`:从"叶子分类"扩展为**每个分类节点**统计直接子节点(子分类 + 组)数 > 50;超限节点的直接组按 created 升序标脏超出部分(与现状一致);纯中间节点(无直接组)不标脏,靠报告提醒
- 树构建逻辑抽为共享函数,放 **export.py**(INDEX 渲染同层),provider 复用 `export_mod` 的树构建;`storage.py` 只新增 `_fts_refresh_group_meta`(刷新单组 FTS 的 category/group_path 列)

### 6. 维护 skill(skills/note-maintenance/SKILL.md)

- 术语:层级树;新增"层级规范"章节:建议深度 ≤ 3(领域/主题/子主题)、每节点 ≤ 50 子节点
- 新增"扩散读法":从出发组向外,先同目录组 → 兄弟分类组 → 父分类 → 其他领域
- 新增"层级维护动作":上提(`note_move(path, 父分类)`)、下移(`note_move(path, "父/新子分类")`)、合并分类(逐个 move,空分类自然消失)、改名(`note_rename_category`)、超深整理(响应 `deep_categories`/`hierarchy_summary`)
- 强调响应式:只在检测报告提示时维护,不主动重构层级
- 数据模型表格更新 category 说明;frontmatter description 同步

### 7. Dashboard(plugin_api.py + dist/index.js)

- `PUT /api/categories` 现有实现已支持多段(精确匹配 old_name 全路径替换)——无需大改,补注释
- `GET /categories` 返回全路径字符串,新建组下拉可直接用
- `GET /index` 返回结构不变(扁平 `category` 字段=全路径);前端 `dist/index.js` 渲染时按 `/` 段数缩进显示(最小改动)

### 8. 测试

- storage:多段 category 的 upsert / path UNIQUE / 移动后 FTS 同步
- export:多段目录导出形状 + round-trip 无损
- provider:
  - `note_move` 成功 / 冲突 / 组不存在
  - `note_rename_category` 成功 / 冲突 / 空分类
  - `note_maintain` 的 `deep_categories` / `hierarchy_summary` / 中间节点超限
  - INDEX 树形渲染(system_prompt_block 含缩进)
- 现有 43 项全部保持通过

## 明确不做

- 代码不硬限制深度(不阻止、不标脏,只报告)
- 不检测过稀 / 单链分类(保持响应式理念)
- 不建独立分类表(分类由 groups 派生,重命名/删除 = 批量改组)
- 不改 `note_rewrite` / `note_write` 语义;冷存储结构不变
- LLM 工具面只新增 `note_move` + `note_rename_category`,不加其他

## 影响面

- `sqlite_note_store/export.py`(import、INDEX 渲染)
- `sqlite_note_store/storage.py`(FTS 刷新辅助、树构建共享函数)
- `sqlite_note_store/provider.py`(2 个新工具、note_maintain 扩展、新常量)
- `sqlite_note_store/skills/note-maintenance/SKILL.md`
- `dashboard/plugin_api.py`(注释/小改)+ `dashboard/dist/index.js`(缩进渲染)
- `tests/`(新增 ~6 项,现有 43 项不变)
