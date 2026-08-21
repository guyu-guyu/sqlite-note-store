---
name: note-maintenance
description: 维护 SQLite 记忆库 — 读取脏组的所有条目，消化评论，合并去重，拆分超大组，迁移错分类条目，通过 note_rewrite 保存并清除脏标记。
platforms: [linux, macos, windows]
---

# SQLite 记忆库维护 Skill

> **术语**：存储层概念是 **组（group）**——一个主题容器，把相似条目归拢在一起，导出时才对应一个 `category/slug.md` 文件。冷存储按创建时间分成**批次（batch）**，不是主题组。

## 触发条件

- 用户主动要求维护笔记库（"整理一下笔记"、"处理脏组"、"维护记忆"）
- `note_maintain` 返回 `dirty_groups` 非空
- `note_maintain` 返回 `oversized_groups` 或 `overpopulated_categories` 非空
- 定期维护（例如每日/每周任务）

## 数据模型

记忆库存储在单个 SQLite 数据库中（`notes.sqlite3`），核心表结构：

| 表 | 作用 | 关键字段 |
|---|---|---|
| `groups` | 条目容器，按主题归拢相似条目（导出为一个 .md 文件） | `id`, `path`, `category`, `title`, `tags`, `dirty`, `created`, `updated` |
| `entries` | 记忆原子单位，每条属于一个 group | `id`, `group_id`, `header`, `content`, `last_used`, `comments`(JSON) |
| `entries_fts` | FTS5 全文搜索索引（存储层在每次写入时自动同步，无需手动维护） | 映射 `entries` 的 `header` + `content` |
| `cold_batches` | 冷存储批次（按创建时间命名的时间队列） | `id`, `filename`, `created` |
| `cold_entries` | 被清退到冷存储的条目 | `id`, `cold_batch_id`, `header`, `content`, `original_category`, `order_index` |

**关键概念**：
- **`path`** 是 `category/slug.md` 格式的字符串标识符（如 `pubgm-lua/取日志.md`），不是磁盘文件路径——它是 SQLite `groups` 表的一列
- **`dirty`** 是 `groups` 表的布尔列，**组级**标记
- **`comments`** 是 `entries` 表的 JSON 数组列，ephemeral TODO
- **`category`** 是 `groups` 表的字符串列，不是文件系统目录
- **INDEX** 不存在于磁盘上——每轮对话由 `system_prompt_block()` 实时从 SQL 查询构建并注入 system prompt
- **FTS5 索引** 由存储层在每次写入时自动同步，`note_rewrite` 后无需手动重建

## 什么是条目（Entry）——最重要的概念

**一条目 = 一个完整的、可独立使用知识单元。**

这是维护的核心判断标准。一条目被单独读出来时，读者应该能获得**关于某件事的完整答案**，不需要依赖同一组里的其他条目。

### 条目的粒度判断

| 情况 | 正确做法 | 示例 |
|---|---|---|
| 多个条目是同一话题的不同章节，单独读不完整 | **合并为一条**，内部用 markdown `###` 分节 | "UE4 日志路径" + "logcat 命令" + "线上捞日志" + "参考文档" → 合并为一条"PUBGM Android 取日志" |
| 两个条目讲的是完全独立的知识 | **保持独立** | "取日志" 和 "塞 Lua" 是两个独立操作，各自独立 |
| 两条目内容精确重复 | **合并去重** | "adb logcat 用法" 出现两次 → 只保留更完整的 |

**关键原则：宁可一条目长一点（内部用 markdown 分节），也不要把一个完整话题拆成多个碎片。** 条目的 header 是搜索和引用的锚点——碎片化意味着搜索时只能看到局部，引用时不够完整。

### ❌ 碎片化反模式

```
# 错误：4 个碎片条目，每个都不完整
组: PUBGM-Android-取日志.md
  条目1: "游戏日志（UE4 日志）"   ← 只有路径和包名
  条目2: "系统日志（logcat）"      ← 只有命令
  条目3: "线上捞日志"              ← 只有链接
  条目4: "参考文档"                ← 只有链接

# 正确：合并为 1 条完整条目
组: PUBGM-Android-取日志.md
  条目1: "PUBGM Android 取日志完整流程"
         ### 游戏日志（UE4 日志）
         ...(路径、包名、读取方式、崩溃日志提取)...
         ### 系统日志（logcat）
         ...(adb logcat 命令)...
         ### 线上捞日志
         ...(平台链接)...
         ### 参考文档
         ...(iWiki 链接)...
```

读者拉出这一条目，立刻获得取日志的完整方法；搜索命中任何一个关键词（logcat、UE4、线上捞日志），都能看到完整的上下文。

---

## 核心概念

### 脏标记 = "这个组的条目有变化，LLM 该来看看"

一个组被标为 `dirty: true`（`groups.dirty = 1`）可能是因为：
- **新增了条目**（`note_write` 追加）——可能和旧条目重复/相似，需要合并
- **加了评论**（`note_comment`）——需要根据评论修正条目
- **内容被修改过**
- **超出存储上限**（组渲染后超 50KB / 分类超 50 组）——`note_maintain` 会强制标脏，等待 LLM 拆分/合并/迁移

**所有脏组都需要 LLM 亲自处理**。Python 代码不能自己判断该合并、去重、迁移什么——只有 LLM 能读懂条目内容做这些决策。

### 活跃组绝不物理删除

**唯一的物理删除路径是冷存储超限**（超过 50 个冷存储批次时按创建日期删最旧的）。活跃区任何情况下**都不会**由 Python 直接删行。

- 组超 50KB → LLM 用 `note_rewrite` **拆分**成多个组
- 组过小（1-2 条目）→ LLM 顺手把它并入同分类的相邻主题组（处理脏组时观察，不主动扫描）
- 分类超 50 组 → LLM **合并**同主题组，或把整个旧组**迁移到冷存储**（通过让最旧条目自然过期 + `note_use` 只标记引用过的）
- 分类过稀（1-2 个组）→ LLM 顺手把它上提合并到相邻分类（处理脏组时观察，不主动扫描）
- 需要"删"一个活跃组时 → 用 `note_rewrite(path, entries=[])`（`groups` 表对应行被删除）；但只有 LLM 明确判断该组所有内容都已消化或迁移后才可以这么做

### 分工模型

| 角色 | 职责 |
|---|---|
| `note_maintain` (Python) | 冷迁移过期条目（INSERT 冷表 + DELETE 活跃行）、检测超限组/分类并强制标脏、淘汰超限冷批次（SQL DELETE）、返回脏组列表（INDEX 由 `system_prompt_block` 每轮实时 SQL 查询构建，无磁盘文件） |
| **LLM (此 skill)** | 读每个脏组的所有条目、整理内容、消化评论、合并去重、拆分超大组、迁移错分类条目、调用 `note_rewrite` 保存 |
| `note_rewrite` (Python) | 事务内 DELETE 旧 entries + INSERT 新 entries + UPDATE groups.dirty=0，FTS5 索引自动同步；`entries=[]` 删除组行 |

**关键**：`note_maintain` 不会自己清任何脏标记。脏标记只有在 LLM 调用 `note_rewrite` 时才被清除（`groups.dirty` 从 1 改为 0）。

### 评论是**待办 TODO**，不是修改记录

- **对话中**发现某条记忆有问题 → `note_comment` 向 `entries.comments` JSON 数组追加一条（同时标脏组）
- **维护时** → LLM 读评论 → 改写条目消化评论 → **评论清空**（`note_rewrite` 的 entries 里不含 comments 字段，写入后 `comments` 自动为空数组）

评论不应成为条目内容的一部分。它是元数据，处理完就删。

---

## 完整工作流

### 第一步：探测

调用 `note_maintain(force=False)`。观察返回：

- `dirty_groups`: **脏组 path 列表**——你要处理的目标
- `oversized_groups`: 单组渲染后超 50KB 的列表，`[{path, size_bytes, size_kb}]`——需要拆分
- `overpopulated_categories`: 分类超 50 组的列表，`[{category, group_count, groups}]`——需要合并/迁移
- `cold_moved`: 自动清退到冷存储的条目数（从 `entries` 表迁移到 `cold_entries` 表）
- `cold_batches_pruned`: 冷存储超限被删除的旧冷批次数

超限的组已经被自动标脏，会同时出现在 `dirty_groups` 里。

如果 `dirty_groups` / `oversized_groups` / `overpopulated_categories` 都为空 → 已完成，向用户报告即可。

### 第二步：读每个脏组

对 `dirty_groups` 中的每个 path 调用 **`note_read_group(path=<path>)`**——注意是 `note_read_group`，不是 `note_read`。

- `note_read_group` 一次性返回组的所有条目 + 每条的 `header` / `content` / `last_used` / `comments`——**这是维护流程的正确读法**，因为你需要看到所有邻居才能判断合并/去重/拆分
- `note_read`（无 `entry_header`）只返回一个精简概要（headers 列表，没有 content），是日常对话场景省 context 的，**维护时不要用**

你会看到：
- 组元数据（`title` / `category` / `tags` / `dirty` / `created` / `updated`）
- 每个条目：`header` + `content` + `last_used`
- 该条目的 `comments` 数组（如果有评论）

### 第三步：整理

对当前组做以下判断（**这是整个流程的核心**）：

#### 3.1 消化评论

对**有评论**的条目，按评论 `type` 采取行动：

| 评论类型 | 你要做的 |
|---|---|
| `inaccurate` | 修正条目内容中不准确的部分 |
| `wrong` | 条目根本错误 → 彻底重写，或**从列表中删除** |
| `needs_improvement` | 补充细节、改写得更清晰 |
| `conflicting` | 查看组里其他条目 → 保留正确的、合并信息、删掉矛盾的 |
| `misplaced` | **迁移到目标分类**（用 `note_write` 到新分类创建条目，然后从当前列表中删除）|

#### 3.2 合并碎片条目（最重要的整理动作）

**判断标准：如果读者只看一条目，能获得完整答案吗？** 如果不能——如果一个条目必须和同一组的其他条目配合才能构成完整知识——合并它们。

合并时：
- 新 header 用能概括整组内容的标题（如 "PUBGM Android 取日志完整流程" 而非 "日志"）
- 新 content 用 markdown `###` 分节，保留原有的结构
- `last_used` 取**最新**的（避免刚合并就被冷存储清退）

**碎片化的典型信号**：一个组里多个条目的 header 是"参考文档"、"相关链接"、"注意事项"这类依赖性标题——它们不是独立知识，只是某个主话题的补充章节，应该合并到主条目里。

参见上方「什么是条目」的定义和反模式示例。

#### 3.3 去重

内容几乎相同？只保留信息最全的那条。

#### 3.4 删除无价值条目

明显过期、错误、无意义的条目直接从列表中省略——LLM 就是最好的过滤器。

#### 3.5 保持不动

大多数条目不需要改。**维护是响应式的**——只处理有信号（脏标记来源）的部分，不要过度整理。

#### 3.6 检查组结构健康度（响应式，不全库扫描）

对**每个脏组**，除了整理条目内容，还要顺手评估它自己和它所在分类的"健康度"：

> **重要：不要扫描整个活跃笔记库**。只关注**当前正在处理的脏组**——看它的大小、看它所在分类的兄弟组数。维护始终以脏标记为起点，不主动巡检没被标脏的角落。实时 INDEX 每轮注入 system prompt，用它判断即可。

评估当前脏组时，判断以下四种偏离健康状态的情况：

**A. 单组超 50KB（在 `oversized_groups` 里）→ 拆分**

1. 读组所有条目（`note_read_group`）
2. 按主题/时间/使用频度分成 2-3 组
3. 主组用 `note_rewrite(path, entries=[主组条目])` 写回原组
4. 其他组用 `note_write(title=..., content=..., category=同分类)` 建成新组，title 反映子主题
5. **不要**把所有条目都留在原组里指望 Python 自动拆——Python 不会

**B. 分类超 50 组（在 `overpopulated_categories` 里）→ 合并 / 迁移 / 归档三选一**

`overpopulated_categories[i].groups` 按创建日期升序，**最前面的是最旧的**，优先处理它们：

- **合并**：如果发现两个组主题重复/相似 → 把 B 的条目并入 A → `note_rewrite(A, entries=[A条目+B条目])` + `note_rewrite(B, entries=[])`（删空 B）
- **迁移**：如果某组其实属于其他分类 → 该组里每个条目用 `note_write(category=目标分类)` 写到新分类 → 原组 `note_rewrite(entries=[])` 删除
- **归档到冷存储**：如果整个组都过时了但历史上重要 → 让它自然过期（不要 `note_use`，等 `cold_evict_days` 天冷储清退）；如果需要立即让位置 → 在此组的所有条目 `note_write` 到一个新的临时聚合组，将旧组 `note_rewrite(entries=[])`（这不是理想方案，尽量走合并/迁移）

**C. 单组过小（脏组只剩 1-2 个条目 / 内容 < 2KB）→ 考虑合并到相邻主题**

处理完这个脏组后，如果它已经很单薄，检查同分类下**是否有主题相近的其他组**：

- 找到相似的兄弟组 A → 把当前脏组的条目 `note_rewrite(A, entries=[A条目+脏组条目])` 并入 A → 当前脏组 `note_rewrite(entries=[])` 删除
- 判断"相似"看 title 语义（比如 `hermes-cli` 和 `hermes-配置` 可以合成 `hermes`），而不是硬套字符匹配
- **只在能从实时 INDEX 顺手看到时才做**——不要为找可合并对象去扫描整个分类
- 如果条目本身还有价值但主题不再重要 → 让它自然过期到冷存储（不要 `note_use`），别为了合并而合并

**D. 分类过稀（当前脏组所在分类只剩 1-2 个组）→ 考虑上提合并**

处理完这个脏组后，如果它所在分类已经很空：

- 看看有没有主题相近的兄弟分类 → 把这个孤立分类下的所有组迁到相邻分类（每个组 `note_rewrite` 到目标分类下的合适组）→ 空分类随最后一个组删除而自然消失
- 或者把孤立分类下的组**上提**一级到父分类（同样通过 `note_rewrite` + `note_write` 迁移）
- 同样：**只在处理脏组顺手发现时做**，不要为找可合并分类去扫描活跃笔记库

**响应式原则再次强调**：C 和 D 的信号来自"你正在读这个脏组，顺便注意到分类里组也稀疏"，而不是 `note_maintain` 主动扫描告诉你。Python 侧不会返回 `undersized_groups` 或 `underpopulated_categories`——因为过小/过稀本身不是"错误"，只是一个可以让结构更清爽的机会，值不值得整理由 LLM 判断。

**验收**：处理完后再跑一次 `note_maintain`，`oversized_groups` 和 `overpopulated_categories` 应为空。C/D 无自动验收，凭 LLM 观察实时 INDEX（下一轮的 system prompt 自动更新）是否合理判断。

### 第四步：调用 `note_rewrite` 保存

对每个整理完的脏组调用：

```
note_rewrite(
    path="pubgm-lua/取日志.md",
    entries=[
        {
            "header": "adb logcat 取日志",
            "content": "adb logcat -v time | grep Lua...",
            "last_used": "2026-08-20T14:30:00+00:00"
        },
        // ... 其他整理后的条目
    ],
    // title 和 tags 可选，通常不需要传（保留原值）
)
```

**规则**：
- `entries` 是**完整的最终列表**——不是 diff，不是追加。传什么就存什么（事务内先 DELETE 旧 entries 再 INSERT 新的）。
- **不要传 `comments` 字段**——`note_rewrite` 自动清空评论（写入时 `comments` 为空数组）。
- **合并/迁移/删除的条目不出现在 entries 里**。它们已经被消化。
- 如果整理后**组里没有任何条目**（全都迁走或删除了），传 `entries=[]` → `groups` 表对应行被删除。
- `note_rewrite` 自动：`groups.dirty = 0`、`groups.updated` 刷新、FTS5 索引自动同步（无需手动重建）。

### 第五步：迁移条目（如果有 misplaced）

对 `misplaced` 评论标记的条目：
1. 在目标分类调用 `note_write(content, title, category="目标分类", tags="...")` 创建新条目
2. **不要**在源组的 `note_rewrite` entries 里包含它——这样它自动从源组消失

### 第六步：终结整理

处理完所有 `dirty_groups` 后，再调用一次 `note_maintain(force=False)` 让它：
- 确认脏组列表现在为空
- 扫描 `last_used` 超过 `cold_evict_days`（默认 90 天）的条目移入冷存储（`entries` → `cold_entries` 表迁移）
- 淘汰超 50 个的旧冷存储批次（`cold_batches` 表按创建时间 `created` 排序删最老行）
- （INDEX 不需要显式重写 — 下一轮对话时 `system_prompt_block()` 会用最新 SQL 查询实时构建）

### 第七步：报告

告诉用户：
- 处理了多少个脏组
- 分类型统计：修正 X 条、合并 Y 组、迁移 Z 条、删除 W 条
- 清退到冷存储多少条

---

## 常见陷阱

### ❌ 用 `write_file` / `terminal` / SQL 直接操作数据库

**绝对不要**。直接写 SQL 或用 `write_file` 操作 `notes.sqlite3` 会绕过：
- `groups.dirty` 管理（脏标记不会被正确设置/清除）
- FTS5 索引同步（搜索索引可能与条目内容不一致）
- 事务原子性（部分写入可能留下不一致状态）
- `last_used` 时间戳管理

只能用 `note_rewrite` / `note_write` / `note_comment` 等工具 API。

### ❌ 把评论文本追加到条目内容里

评论是元数据，不是内容。看到评论说"这个 API 应该用 foo.bar 而不是 foo.baz"，改的是**条目内容里的 foo.baz → foo.bar**，不是加一句 `> ⚠️ Correction: 应该用 foo.bar`。

### ❌ 只处理"有评论"的条目

**所有脏组都要看**。脏标记可能来自新增条目——那种情况下没有评论，但可能有和旧条目重复的新条目需要合并。逐条目扫一遍。

### ❌ 传 diff / 追加式 entries 到 note_rewrite

`note_rewrite` 是**全量重写**（事务内 DELETE + INSERT）。你传 3 条 entries，最终组里就只有 3 条——不管原来有多少。整理时先复制所有需要保留的旧条目，再加上你的修改。

### ❌ 合并条目时用旧的 last_used

合并 3 条 → 新条目 `last_used` 取三者中**最新**的。否则合并后立刻被冷存储清退。

### ❌ misplaced 迁移只加不删

`misplaced` 迁移要**两步**：目标分类 `note_write` 创建 + 源组 `note_rewrite` 时不包含此条。

### ❌ 主动大规模整理没脏标记的组

维护是**响应式**的——响应用户/系统写入产生的脏标记。没被标脏的组说明它没变化，不要擅自"改进"。

### ❌ 编辑冷存储

冷存储（`cold_batches` + `cold_entries` 表）不参与维护。用户想找回冷存储条目应用 `note_recall(entry_header)`。不要用 SQL 或任何工具去改冷存储表。

### ❌ 忽视超限告警

`oversized_groups` 或 `overpopulated_categories` 非空时**不能只处理评论/合并就了事**——超限的组下次维护还是会出现，实时 INDEX 里也会持续显示 `*(dirty)*`。见 3.6 检查组结构健康度。

### ❌ 主动扫描活跃库找可合并的过小组 / 过稀分类

3.6 C/D 是**处理脏组时顺手观察**——不是"每次维护都扫一遍全库找碎片"。没脏标记的分类哪怕只有 1 个组也不动。理由：过小/过稀不是错误，只是可以更好；主动扫描会产生大量低价值改动、噪音，还会违反"响应式维护"原则。

### ❌ 为合并而合并 / 为拆分而拆分

3.6 的每个动作都要**有明确收益**。两个组主题真的相近才合并；不是"看起来能合就合"。组真的超 50KB 才拆；不是"接近上限就先拆一下预防"。过度整理会打乱条目的自然聚集，让后续搜索更难。

---

## 完整示例

用户："帮我维护一下笔记库"

**第 1 步：探测**
```
note_maintain(force=False)
→ dirty_groups: ["pubgm-lua/取日志.md", "devops/hermes.md"]
  cold_moved: 3, cold_batches_pruned: 0
```

**第 2-3 步：读并整理 pubgm-lua/PUBGM-Android-取日志.md**
```
note_read_group(path="pubgm-lua/PUBGM-Android-取日志.md")
→ 4 个条目:
  1. "游戏日志（UE4 日志）" (last_used: 2026-08-15) — UE4 日志路径、包名、读取方式
  2. "系统日志（logcat）" (last_used: 2026-08-10) — adb logcat 命令
  3. "线上捞日志" (last_used: 2026-08-18) — 业受平台/运维网站链接
  4. "参考文档" (last_used: 2026-08-20) — iWiki 链接

分析:
  - 这 4 个条目讲的都是同一件事——"如何在 PUBGM Android 上取日志"
  - 每一条单独拿出来都不完整：只看条目1 不知道怎么取系统日志，
    只看条目3 不知道怎么取游戏日志
  - 条目 4 "参考文档" 是典型的碎片化信号——它不是独立知识，
    只是某个主话题的补充章节
  - 合并为 1 条，内部用 markdown ### 分节保持结构
```

**第 4 步：保存**
```
note_rewrite(
  path="pubgm-lua/PUBGM-Android-取日志.md",
  entries=[
    {
      header: "PUBGM Android 取日志完整流程",
      content: "### 游戏日志（UE4 日志）\n路径: /sdcard/.../Logs/\n...\n\n### 系统日志（logcat）\nadb logcat -s Lua ...\n...\n\n### 线上捞日志\n业受平台: http://...\n运维网站: https://...\n\n### 参考文档\n- https://iwiki.woa.com/...\n- ...",
      last_used: "2026-08-20T..."
    }
  ]
)
→ {status: "ok", action: "rewritten", entry_count: 1}
```

**对 devops/hermes.md 重复第 2-4 步。**

**第 6 步：终结**
```
note_maintain(force=False)
→ dirty_groups: []
  cold_moved: 0, cold_batches_pruned: 0
```

**第 7 步：报告**
```
处理了 2 个脏组：
- 合并 1 组碎片条目（取日志 4 条→1 条完整流程）
- 清退 3 条到冷存储
```

---

## 与设计理念的对应

- **SQLite 是权威存储**：所有数据活在 SQLite 表中（`groups`/`entries`/`cold_batches`/`cold_entries`），Markdown 只是可导出的只读投影
- **脏标记 = 变化信号**：`groups.dirty = 1` 表示组有变化，LLM 必须看
- **活跃组不物理删**：唯一删除路径是冷存储超限；活跃区靠 LLM 拆分/合并/迁移
- **评论 = 待办 TODO**：`entries.comments` JSON 数组，`note_rewrite` 时自动清空，不污染条目内容
- **组级维护**：LLM 用 `note_read_group` 打开组时看到所有条目，才能做合并/冲突整合
- **FTS5 自维护**：搜索索引由存储层在每次写入时自动同步，`note_rewrite` 后无需手动重建
- **冷存储不整理**：`cold_entries` 只是过期的时间队列，维护流程从不触碰
- **维护是响应式**：由脏标记驱动，不主动改动没有信号的组
- **note_rewrite 是唯一保存入口**：确保 dirty/FTS/事务由插件统一管理，不被绕开
