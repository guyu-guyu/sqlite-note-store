# sqlite-note-store

Hermes Agent 的 SQLite 后端记忆库插件。权威存储放在单个 SQLite 数据库里，同时保留把整库无损导出成 Markdown 目录树的能力。

> **前置阅读：** `DESIGN_PHILOSOPHY.md` — 讲这个插件为什么这样设计。本 README 只讲"怎么装、怎么用、内部长什么样"。

> **术语约定：** 存储层概念是**组（group）**——一个主题容器，把相似条目归拢在一起（`groups` 表一行），导出时才对应一个 `category/slug.md` 文件。冷存储按创建时间分成**批次（batch）**（`cold_batches` 表一行），不是主题组。下文除明确指磁盘产物（md 文件、INDEX.md）外，“文件”均指组。**DB 中的 path 不带 `.md` 后缀**——后缀只在导出边界拼上。

## 特性

- **单文件 SQLite 权威存储** — 一次写入 = 一行 INSERT/UPDATE + FTS5 索引同步，不再重写整个 md 文件、不再全仓 FTS rebuild
- **12 个 LLM 工具** — `note_search` / `note_write` / `note_read` / `note_read_group` / `note_use` / `note_recall` / `note_comment` / `note_maintain` / `note_rewrite` / `note_move` / `note_rename_category` / `note_rename_group`
- **随时导出成 Markdown 目录** — 生成可 grep、可 vim 打开的目录树
- **随时从 Markdown 目录导入** — 支持任意符合投影结构的 md 目录一键迁入
- **Round-trip 无损** — 有单元测试断言 export→import→export 位对位相等
- **无 pip 依赖** — Python 标准库 sqlite3 就够了
- **Web 看板** — `dashboard/` 随插件目录一起安装，挂到 Hermes Dashboard，不用导出 md 即可可视化查看和编辑

## 安装

### 方式一：本地目录（开发/调试）

```bash
# 1. 整体软链仓库根（$HERMES_HOME 默认 ~/.hermes）——一条命令，仓库即插件
ln -sfn /path/to/sqlite-note-store-plugin "${HERMES_HOME:-$HOME/.hermes}/plugins/sqlite-note-store"

# 2. 在 config.yaml 中指定 memory provider（这是激活的唯一开关）
# memory:
#   provider: sqlite-note-store

# 3. 重启会话
```

插件目录就是**仓库根**：`plugin.yaml`、`__init__.py`（register）、`provider.py`、`skills/`（维护 skill）、`dashboard/`（看板）都在仓库根，一条软链即包含全部。仓库改动实时同步，无需重装。

> **激活机制**：memory provider 的**激活**只由 `memory.provider` 配置键决定，与 `plugins.enabled` 无关。但**如果要使用 Web 看板（dashboard）**，插件名（manifest 的 `name`）**必须出现在 `plugins.enabled` 里**——dashboard 前端用它对用户插件做门控，不在列表里的插件 tab 会被静默过滤（见下方故障排查）。
>
> **验证**：在 hermes-agent 仓库根目录运行 `python -c "from plugins.memory import discover_memory_providers; print([p[0] for p in discover_memory_providers()])"`，应包含 `sqlite-note-store`。若加载失败，启动日志会包含 `Failed to load memory provider` 或 `Memory provider ... initialize failed`（加载失败只是降级跳过，不会阻塞 Hermes 启动，所以**没报错 ≠ 已加载**，务必查配置与日志）。

### 方式二：git clone 安装（分发/更新）

```bash
# 仓库根即插件：clone 到插件目录即可用，之后 git pull 保持更新
git clone https://github.com/guyu-guyu/sqlite-note-store.git "${HERMES_HOME:-$HOME/.hermes}/plugins/sqlite-note-store"
cd "${HERMES_HOME:-$HOME/.hermes}/plugins/sqlite-note-store"
git pull   # 以后更新
```

启用后，第一次会话开始时 SQLite 数据库会自动创建，维护 skill 自动安装。

## 故障排查

> 经验来自真实安装踩坑。Hermes 对插件加载失败是**静默降级**（记日志 + 跳过，不阻塞启动），所以排查的第一原则是：**看日志、按名验证，别信“没报错”**。

### 1. 插件装了但 LLM 工具不出现（memory provider 未加载）

**① 目录结构必须扁平**（最常见错误）：

```text
# ✅ 正确：插件目录 = 仓库根（一条软链或 git clone）
$HERMES_HOME/plugins/sqlite-note-store → 仓库根
├── plugin.yaml      # 在根
├── __init__.py      # register(ctx) 在根
├── provider.py      # SQLiteNoteStoreProvider + register
├── schema.py / storage.py / export.py / markdown_io.py
├── skills/
└── dashboard/       # 看板（manifest.json + dist/ + plugin_api.py）

# ❌ 错误：把内容拆散逐文件链接，或 plugin.yaml 埋在子目录里 ——
#    发现机制要求的是一个完整目录（git clone 即用）
```

**② 激活开关**：`config.yaml` 必须设置 `memory.provider: sqlite-note-store`（名字与 `provider.name`、目录名完全一致）。这是唯一激活开关。

**③ 路径**：插件必须装在 `${HERMES_HOME:-$HOME/.hermes}/plugins/` 下，确认 `echo $HERMES_HOME` 的实际值。

**④ 验证与日志**：

```bash
# 在 hermes-agent 仓库根目录：确认发现
python -c "from plugins.memory import discover_memory_providers; print([p[0] for p in discover_memory_providers()])"

# 启动日志里搜这些关键词定位失败原因
#   Failed to load memory provider ...   → 导入/注册异常（日志会带具体错误）
#   Memory provider ... initialize failed → initialize() 抛异常
#   Rejected memory provider ...         → 已有其他外部 provider 激活（只能有一个）
```

### 2. dashboard 没有「记忆笔记」面板

Hermes 扫描 `$HERMES_HOME/plugins/<name>/dashboard/manifest.json` 发现看板插件，但有三道门控，**环环相扣**，全部满足才会显示：

**① manifest 的 `name` 必须等于 `plugins.enabled` 里的名字**：

```jsonc
// dashboard/manifest.json
{ "name": "sqlite-note-store", ... }  // 必须与 plugins.enabled 一致
```

前端门控（`_is_active`）拿 manifest 的 name 与 `plugins.enabled` 比对——不一致时 tab 被静默过滤。

**② 前端 bundle 的注册名与 API 前缀必须用 manifest name**：

```js
// dashboard/dist/index.js
var API = "/api/plugins/sqlite-note-store";            // 不是 /api/plugins/notes
window.__HERMES_PLUGINS__.register("sqlite-note-store", NotesPage);  // 不是 "notes"
```

宿主按 manifest name 期待 `register(<name>, Component)`；注册名不匹配时面板报“插件脚本未调用 register()”。API 路由也挂在 `/api/plugins/<manifest name>/` 下。

**③ 改了 manifest / 新增插件后，dashboard 进程要重启**：

- 插件列表有进程内缓存：`GET /api/dashboard/plugins/rescan`（需登录）可强制重扫静态资源，无需重启
- 但 **API 路由只在启动时挂载**（`_mount_plugin_api_routes`）——改名/新增后必须重启：

```bash
hermes dashboard --stop
nohup hermes dashboard --host 0.0.0.0 --port 9119 --no-open > /tmp/dashboard.log 2>&1 &
```

**④ 登录与验证**（自托管 basic auth）：

```bash
PASS=$(grep PASSWORD ~/.hermes/dashboard-credentials.txt | cut -d= -f2)
curl -c /tmp/cookie.txt -X POST http://localhost:9119/auth/password-login \
  -H "Content-Type: application/json" \
  -d "{\"provider\":\"basic\",\"username\":\"admin\",\"password\":\"$PASS\"}"
curl -b /tmp/cookie.txt http://localhost:9119/api/dashboard/plugins          # 应含 sqlite-note-store
curl -b /tmp/cookie.txt http://localhost:9119/api/plugins/sqlite-note-store/stats  # 应返回 JSON
curl -b /tmp/cookie.txt -o /dev/null -w "%{http_code}\n" \
  http://localhost:9119/dashboard-plugins/sqlite-note-store/dist/index.js   # 应 200
```

### 3. 改了代码不生效？

按 README 方式一（符号链接）安装时，仓库改动**实时同步**到插件目录：
- 后端（provider 等 .py）→ 重启 Hermes 会话生效
- dashboard manifest / dist → 重启 dashboard 进程生效（或先 rescan 试静态资源）
- 维护 skill → 下次会话 initialize() 时按 mtime 自动同步

## 数据在哪里

| 路径 | 内容 |
|---|---|
| `~/.hermes/notes/notes.sqlite3` | SQLite 权威存储（WAL 模式） |
| `~/.hermes/notes/notes.sqlite3-wal` / `.sqlite3-shm` | SQLite WAL 副产物 |
| `~/.hermes/skills/note-taking/note-maintenance-sqlite/SKILL.md` | 维护技能（LLM 用） |

**导出成 Markdown 目录：**

```python
import schema, export
from pathlib import Path

conn = schema.connect(Path.home() / ".hermes/notes")
export.export_to_directory(conn, Path("/tmp/notes-backup"), clean=True)
```

**从 Markdown 目录迁入：**

```python
conn = schema.connect(Path.home() / ".hermes/notes")
export.import_from_directory(conn, Path("/path/to/some-markdown-notes"), replace=True)
```

## 记忆库索引 (INDEX) —— 常驻上下文的目录页

设计意图见 `DESIGN_PHILOSOPHY.md`。这里只讲**它在新插件里长什么样、怎么实现的**。

### 存在形态

**没有磁盘上的 `INDEX.md` 文件**。INDEX 由 provider 在**每一轮对话开始时实时构建**，直接注入到 LLM 的 system prompt 里。不落盘，不缓存，不由 `note_maintain` 生成。

### 每轮注入的内容

```markdown
# Note Repository (sqlite-note-store)
Persistent memory keyed on `title` (auto-slugged to a file).
Reading path: scan the index below first to spot the right group,
then `note_read(path)` for a slim headers overview, then
`note_read(path, entry_header)` to fetch just the entry you want —
cheap on context. Only during maintenance (processing a dirty group)
use `note_read_group(path)` to see every entry's body.
Fall back to `note_search(query)` when the index doesn't match.

## Live Index

# Note Repository Index

- Groups: **12** · Entries: **47** · Cold entries: **83** · Dirty: **2**
- Generated: `2026-08-20T18:52:00+00:00`

## coding
- [Python 调试技巧](coding/python-debug) — 5 entries
- [Rust 常用命令](coding/rust-commands.md) — 3 entries *(dirty)*

## game
- br
  - [卡牌BR战斗流程](game/br/br-flow) — 8 entries
  - [局外系统](game/br/meta.md) — 12 entries *(dirty)*
- fps
  - [武器平衡](game/fps/weapon.md) — 3 entries

## cold-storage
- [2026-07-15](cold-storage/2026-07-15.md) — 23 entries
```

顶栏统计给出 `Groups / Entries / Cold / Dirty` 四个数字，加上生成时间戳。之后按分类分组列出所有活跃组，每个组标注条目数；dirty 组带 `*(dirty)*` 标记。冷存储只列文件名 + 条目数。

### 实现路径

```
provider.py::system_prompt_block()   ← 每轮由 Hermes 调用
    │
    └── export.py::_build_index_markdown(conn)
            │
            ├── 4 条简单 SQL 查询（groups / entries count / cold count / cold batches）
            └── 纯字符串拼接
```

单次调用 <1ms，每次读的都是最新状态。

### LLM 的决策链路

1. **看 INDEX** — 每一轮已经在 system prompt 里，无需主动调用
2. **INDEX 能匹配** → `note_read(path)` 拿单条概要 → `note_read(path, entry_header)` 取内容
3. **INDEX 找不到匹配** → `note_search(query)` 兜底

### 想在磁盘上留一份 INDEX.md？

导出即可（INDEX.md 默认包含在导出目录里）：

```bash
python __main__.py export /tmp/view          # 生成 /tmp/view/INDEX.md
python __main__.py export /tmp/view --no-index  # 跳过 INDEX.md
```

## 工具形态

以下是完整的 12 个工具的签名。

### `note_search(query, limit=5)`

在**活跃条目**上跑 FTS5 搜索。**永远不搜冷存储**。返回 `[{path, title, category, snippet}]`。

### `note_write(title, content, tags="", path=None)`

追加一条新条目。会：
- **优先写入已有组**：传入 `path`（如 `game/br/flow`，不带 `.md`，见 INDEX）时，条目直接追加进该组，保留原组标题并合并 tags
- **必要时才新建**：`path` 不存在时在该位置新建组（path 最后一段即组名，前段即分类）；不传 `path` 时用 `slugify(title)` 在 `uncategorized` 下派生（同 title 落同组）
- 无论如何**都会把组置 dirty**，等 LLM 在下次维护时判断是否需要合并

### `note_read(path, entry_header=None)`

**两种模式，默认省 context：**

- `note_read(path)` — 返回组的**精简概要**：`{title, category, tags, dirty, entry_count, headers}`，**不包含 content**。
- `note_read(path, entry_header="...")` — 返回**单个条目**的完整内容 `{header, content, last_used, comments}`。日常对话引用记忆的正确路径。

### `note_read_group(path)`

一次拉完整个组的所有 entries + 每条 comments。**仅在维护流程使用** — LLM 处理 dirty 组时需要看所有邻居才能判断合并/去重/拆分。

### `note_use(path, entry_header)`

刷新条目的 `last_used`，让它不至于因为长时间没被引用而被冷迁移。LLM 在**真正引用一条记忆时**主动调用。

### `note_recall(entry_header)`

**只读**地从冷存储捞一条记忆。不改冷存储。如果要"救回"到活跃库，请另外调 `note_write`。

### `note_comment(path, entry_header, comment_type, comment_text)`

给某个 entry 挂一条 ephemeral TODO，同时把组标 dirty。`comment_type`：`inaccurate` / `needs_improvement` / `wrong` / `conflicting` / `misplaced`。

### `note_maintain(force=False)`

做**所有不涉及语义判断的机械工作**，返回 dirty 清单让 LLM 处理：

- 冷迁移超过 `cold_evict_days`（默认 90 天）没用过的条目
- 冷批次超上限（默认 50 个）删最老
- 检测超大组 → force dirty
- 检测超限分类 → force dirty
- 返回 `{dirty_groups, cold_moved, cold_batches_pruned, oversized_groups, overpopulated_categories, deep_categories, hierarchy_summary}`

**`note_maintain` 从不清 dirty**，这是最关键的一条契约。

### `note_rewrite(path, entries)`

**唯一能清 dirty 的工具**。传入 `entries=[{header, content, last_used?}, ...]`，整个组的条目被这一列表替换，组标 `dirty: false`，所有 comments 被消费。`entries=[]` 删除组。

### `note_move(path, new_category)`

把组移动到另一个分类（slug/文件名不变）。用于层级维护：上提、下移、合并分类。目标 path 已存在时报错（先合并或换分类）。**不标脏**——内容没变，只是位置变。

### `note_rename_category(old_category, new_category)`

重命名一个分类路径（精确匹配），该分类下的所有组同步改前缀。**子分类不受影响**；移动整个子树请用 `note_move` 逐个搬。任何目标 path 冲突则整体中止。

### `note_rename_group(path, new_title)`

重命名一个组的标题（显示名），slug 与 path 按新标题重新派生（**分类不变**）。用于修正写错的组名——这是 LLM 唯一能改组名的工具。目标 path 已存在则报错；**不标 dirty**（内容没变，只是名字变）。

### 组/分类编辑（Dashboard API）

除了上述 LLM 工具，provider 还通过 HTTP API 暴露了组和分类的管理能力，供 Web 看板使用（路由名沿用历史术语 file，对应存储概念 group）：

| 操作 | 端点 | 保护 |
|---|---|---|
| 重命名分类 | `PUT /api/categories`（body: `{old_name, new_name}`） | — |
| 删除分类 | `DELETE /api/categories/{name}` | 有组→禁止 (409) |
| 重命名组 | `PUT /api/files/{id}`（body: `{title}`） | — |
| 移动组到其他分类 | `PUT /api/files/{id}`（body: `{category}`） | — |
| 删除组 | `DELETE /api/files/{id}` | 有条目→禁止 (409) |
| 编辑条目 | `PUT /api/entries/{id}` | — |
| 删除条目 | `DELETE /api/entries/{id}` | — |

## 内部长什么样

模块分层，每层可以独立测试（仓库根即插件根）：

```
├── schema.py            — DDL + 建库（groups / entries / cold_batches / cold_entries + FTS5）
├── markdown_io.py       — parse_file() / render_file() / entry ⇋ row
├── storage.py           — CRUD + FTS 搜索 + 冷迁移 SQL
├── export.py            — SQLite ⇋ 目录树的双向桥
├── provider.py          — MemoryProvider 门面类 + 12 个 tool 处理器 + Dashboard API
├── plugin.yaml          — 元数据 + hooks
├── dashboard/
│   └── dist/index.js    — 前端 bundle（统计 + INDEX 树 + 编辑器 + 搜索 + 冷存储 + 新建条目弹窗）
└── skills/
    └── note-maintenance/SKILL.md  — 维护技能
```

**测试：**

```
tests/
├── test_schema.py        — 建表 / 幂等 / 外键级联
├── test_markdown_io.py   — YAML 解析 / 条目解析 / round-trip 稳定
├── test_storage.py       — CRUD / FTS / 冷迁移 / 冷批次上限
├── test_export.py        — export/import 双向、clean、round-trip
└── test_provider.py      — tool 端到端 + dirty 契约 + 冷屏蔽 + Dashboard API
```

54 项全绿。跑法：

```bash
cd sqlite-note-store-plugin
python -m pytest -v
```

## 设计理念 ↔ 实现 对应

| 理念（见 DESIGN_PHILOSOPHY.md） | 实现位置 |
|---|---|
| SQLite 是权威，Markdown 是投影 | `schema.py` 建库 + `export.py` 生成投影 |
| 导入导出无损 | `tests/test_export.py::test_import_roundtrip_is_idempotent` |
| 条目为单位，组为容器 | `entries` + `groups` 两表分离；写入按 slug 落 group |
| dirty 是组级 | `groups.dirty` 字段 + `mark_dirty()` |
| Python 检测，LLM 决策，note_rewrite 持久化 | `note_maintain` 只返 dirty_groups；`note_rewrite` 是唯一 `mark_dirty(False)` 入口 |
| Python 从不物理删除活跃组 | 唯一 `DELETE FROM groups` 是 `note_rewrite(entries=[])`，由 LLM 主动触发 |
| 冷存储是追加到最新批次的队列 | `get_or_create_cold_batch_for_today()` + `enforce_cold_batch_limit()` 按创建时间删最老批次 |
| Cold recall 是只读的 | `note_recall` 只 SELECT 不 UPDATE |
| 保留隐式关联性 | 维护时用 `note_read_group(path)` 一次拉整组；INDEX 用 group 作聚类锚 |
| 评论是 ephemeral TODO | `note_comment` 追加 JSON + 标脏；`note_rewrite` 自动清空 |
| 无 pip 依赖 | `plugin.yaml: pip_dependencies: []` |
| 层级由 LLM 维护，代码只检测报告 | `note_maintain` 返回 `deep_categories` + `hierarchy_summary`；深度不强制 |

## 已知边界

- **SQLite 3.42 编译时无 `contentless_delete`** → 用普通 FTS5（带副本），索引存储成本换 DELETE 兼容性
- **FTS5 查询中的标点** → provider 层用双引号包裹整个查询，`crash-fix` 这类字符不会被解析成 NOT 操作符
- **FTS5 中文分词** → unicode61 把连续 CJK（含粘连数字）当作单个 token，纯 FTS 无法匹配中段子串。`note_search` 用「FTS5 前缀匹配 + LIKE 兜底」双路径解决：token 前缀命中走 FTS（rank 排序优先），任意子串（含中文中段）由 LIKE 兜底补足——无需重建索引，量级小成本可忽略
- **单进程假设** → SQLite WAL 模式下多读者一写者是安全的，dashboard 只读连接不与 provider 写入抢锁；但**不要跨 Hermes 实例并发写同一 DB**
