---
name: note-maintenance
description: 维护 SQLite 记忆库并同步常驻记忆 — 第一步把常驻记忆(MEMORY.md/USER.md)缺失内容写入记忆库(标脏),再读取脏组、消化评论、合并去重、拆分超大组、迁移错分类、维护层级(≤3层),最后以整理后的记忆库为准纠正常驻记忆,全程通过 note_rewrite / note_move / note_rename_category / note_rename_group 与 memory 工具操作。
platforms: [linux, macos, windows]
---

# SQLite 记忆库维护 Skill

**术语**:存储层概念是**组(group)**——一个主题容器，把相似条目归拢在一起。冷存储按创建时间分成**批次(batch)**，不是主题组。

## 触发条件

- 用户要求整理笔记库
- `note_maintain` 返回 `dirty_groups` / `oversized_groups` / `overpopulated_categories` / `deep_categories` 非空
- 定期维护（每日/每周）

## 数据模型

| 表 | 作用 | 关键字段 |
|---|---|---|
| `groups` | 条目容器（主题归拢相似条目） | `path`（`category/slug`，category 可多段如 `game/br`）、`dirty`、`category`、`title` |
| `entries` | 记忆原子单位，属于一个 group | `group_id`, `header`, `content`, `last_used`, `comments`(JSON) |
| `entries_fts` | FTS5 搜索索引（存储层自动同步，无需维护） | — |
| `cold_batches` | 冷存储批次（按创建时间命名的时间队列） | `id`, `filename`, `created` |
| `cold_entries` | 被清退到冷存储的条目 | `cold_batch_id`, `header`, `content`, `original_category` |

**关键概念**：
- **`path`** 是 `category/slug` 格式的字符串标识符（如 `game/br/flow`），即组在分类树中的位置（`groups` 表的一列）
- **`dirty`** 是组级标记；**`comments`** 是 ephemeral TODO（JSON 数组）
- **INDEX** 不落盘——每轮由 `system_prompt_block()` 实时 SQL 构建注入 system prompt；**FTS5** 由存储层在每次写入时自动同步，`note_rewrite` 后无需手动重建

## 什么是条目（Entry）——最重要的概念

**一条目 = 一个完整的、可独立使用的知识单元。** 单独读出来时能给读者完整答案，不强依赖同组其他条目。

| 情况 | 做法 |
|---|---|
| 多条讲同一话题的不同章节，单独读不完整 | **合并为一条**，内部用 markdown `###` 分节 |
| 完全独立的知识 | 保持独立 |
| 内容精确重复 | 合并去重，留更完整的 |

**碎片化信号**：header 是"参考文档"、"相关链接"、"注意事项"这类依赖性标题——不是独立知识，应合并进主条目。宁可一条目长一点，也不要把完整话题拆成碎片。

## 核心概念

### 脏标记 = "这个组有变化，LLM 该来看看"

`dirty: true` 可能来自：新增条目（可能与旧条目重复）、加了评论、内容被修改、超出存储上限（组渲染 >50KB / 节点 >50 子项，`note_maintain` 强制标脏）。**所有脏组都要 LLM 亲自处理。**

### Python 从不物理删除活跃组

唯一删除路径：冷存储超限时按创建时间删最老批次。活跃区靠 LLM 用 `note_rewrite(entries=[])` 删除——仅当该组所有内容已消化或迁移。

### 评论是待办 TODO，不是修改记录

对话中发现记忆有问题 → `note_comment` 追加评论（同时标脏组）；维护时读评论、改条目、消化它——评论在 `note_rewrite` 时自动清空，**不要**把评论文本写进条目内容。

### 层级规范（LLM 维护，代码不强制）

- **建议深度 ≤ 3 层**（领域/主题/子主题）：`deep_categories` 列出超 3 层的分类，`hierarchy_summary` 给出每层节点数——`depth4+` 出现 = 该整理了
- **建议每节点 ≤ 50 子项**（子分类 + 直接组）：`overpopulated_categories` 对每个节点统计
- **扩散读法**：从一条记忆出发检索时，先读同目录组（最近）→ 兄弟分类 → 父分类，不要第一步就上探
- **维护动作**（响应式，仅在检测报告提示时做）：
  - 上提/下移/合并分类：`note_move(path, new_category)`（slug 不变，冲突报错，不标脏）
  - 改名：`note_rename_category(old, new)`（精确匹配，只改直接组；子分类不受影响）
  - 组名修正：`note_rename_group(path, new_title)`（title/slug/path 一起改，分类不变，冲突报错）
  - 空分类（不再有直接组、也没有子分类）随最后一个组移走自然消失

## 完整工作流

### 第一步：同步常驻记忆 → 记忆库（确保库完全包含常驻记忆）

**时机：必须在最开头**——本步的 `note_write` 会标脏 group，让这些新条目进入后续整理流程。

1. **读常驻记忆**：`read_file` 读取 `~/.hermes/memories/MEMORY.md` 和 `USER.md`（或直接用 system prompt 中已注入的 MEMORY / USER PROFILE 块）
2. **逐条核对**：对每条常驻记忆内容，用 `note_search` / 对照 INDEX 判断 sqlite 记忆库中是否已有等价条目
3. **缺失则写入**：`note_write` 按常规流程添加——选主题最近的组（或新建组）、给有意义的 `entry_header`、组自动标脏
4. **完成判定**：记忆库**完全包含**常驻记忆的所有内容（sqlite ⊇ memory）

注意：
- 只写「常驻记忆有、库中没有」的内容；库中已有的不重复写
- 常驻记忆是摘要/子集性质的内容也照写（保持完整包含关系）
- 不要在这步用 memory 工具写常驻记忆——这步只往 sqlite 补内容

### 第二步：探测

调用 `note_maintain(force=False)`，观察返回：
- `dirty_groups`：脏组 path 列表——处理目标
- `oversized_groups`：单组渲染 >50KB——需要拆分
- `overpopulated_categories`：超限节点 `[{category, child_count, subcategories, direct_groups}]`——需要合并/迁移
- `deep_categories`：深度 >3 的分类路径——需要上提整理
- `hierarchy_summary`：每层节点数——看层级形状
- `cold_moved` / `cold_batches_pruned`：自动清退数（只读信息）

**完成判定**：`dirty_groups` / `oversized_groups` / `overpopulated_categories` / `deep_categories` 都为空（`hierarchy_summary` 无 `depth4+`），且顺手的名称校准（见 3.7）已应用 → 已完成，向用户报告。

### 第三步：读每个脏组

对每个 path 调用 **`note_read_group(path)`**——一次性返回组内所有条目 + `header` / `content` / `last_used` / `comments`。这是维护流程的正确读法：你需要看到所有邻居才能判断合并/去重/拆分。**不要用 `note_read`**（那是日常省 context 的）。

### 第四步：整理

**3.1 消化评论**，按类型行动：

| 类型 | 行动 |
|---|---|
| `inaccurate` | 修正内容 |
| `wrong` | 彻底重写或从列表删除 |
| `needs_improvement` | 补充细节 |
| `conflicting` | 看同组其他条目，保留正确、删矛盾 |
| `misplaced` | 迁移到目标分类（见第六步） |

**3.2 合并碎片条目**：判断标准——"读者只看一条目能获得完整答案吗？"不能就合并。新 header 用概括性标题，新 content 用 `###` 分节，`last_used` 取**最新**的。

**3.3 去重** → 留信息最全的。**3.4 删除无价值条目**（过期/错误/无意义）。**3.5 保持不动**——大多数条目不需要改，维护是响应式的。

**3.6 组结构健康度**（响应式，只评估当前脏组，不全库扫描）：

- **A. 单组 >50KB（在 `oversized_groups`）→ 拆分**：按主题分成 2-3 组，主组 `note_rewrite` 写回，其他组 `note_write` 建成新组
- **B. 节点超 50 子项（在 `overpopulated_categories`）→ 合并/迁移/归档**：`direct_groups` 按创建时间升序，最旧优先处理；两组合并 = 条目并入 A + `note_rewrite(B, entries=[])`；错分类 = 逐个 `note_write(path=目标路径)` 到目标分类 + 删原组；整组过时 = 让它自然过期到冷存储
- **C. 脏组过小（1-2 条目）→ 顺手并入相邻主题组**；**D. 分类过稀 → 顺手上提合并**——C/D 只在处理脏组时顺手观察到才做，`note_maintain` 不会主动报告

**3.7 名称校准**（响应式，处理脏组时顺手检查）——**组名和分类名是 INDEX 的检索锚**：名字不精准 = 后续检索路由失准。让名字尽可能精准描述其包含的内容：

- **组名**：读脏组时留意 `title` 能否概括条目内容。过泛（“笔记”、“杂项”、“参考”）、过时、或与实际内容不符 → `note_rename_group(path, new_title)` 改名（slug/path 自动重新派生，分类不变；新 path 冲突则先合并再改）
- **分类名**：分类下的组被搬走/合并后，分类名不再能概括其下所有组主题 → `note_rename_category(old, new)` 改名（精确匹配，只改直接组；子分类不受影响），或 `note_move` 把组挪到语义更准的分类
- **为内容命名，不为改而改**：仅在名称明显失真、会误导检索时动手；名称已准确就不动

### 第五步：调用 `note_rewrite` 保存

```
note_rewrite(path="game/br/flow", entries=[{header, content, last_used?}, ...])
```

- `entries` 是**完整最终列表**——不是 diff、不是追加。传什么存什么（事务内先 DELETE 旧 entries 再 INSERT 新的）
- **不要传 `comments`**——重写自动清空
- 合并/迁移/删除的条目不出现在 entries 里
- 整理后组里没有任何条目 → `entries=[]` 删除组（`groups` 表对应行被删）
- `note_rewrite` 自动：`dirty = 0`、`updated` 刷新、FTS 同步

### 第六步：迁移条目（misplaced）

目标分类 `note_write(path=目标路径)` 创建新条目 + 源组 `note_rewrite` 的 entries 里**不包含**它。

### 第七步：终结

处理完所有 `dirty_groups` 后，再跑一次 `note_maintain(force=False)`：确认脏组列表为空、超限告警已清。（INDEX 无需显式重写——下一轮自动实时构建。）

### 第八步：以记忆库为准纠正常驻记忆（最后做）

**时机：必须在最末尾**——此时记忆库已整理完毕，条目内容是权威的，才能作为修正基准。

1. **对比**：将常驻记忆（MEMORY.md / USER.md）与整理后的 sqlite 记忆库逐条对比
2. **冲突判定**：同一事实在两边内容不一致、或常驻记忆已过时/错误 → 以 sqlite 为准
3. **修正**：用 **memory 工具**（`replace` / `remove` / `add`）修正 MEMORY.md / USER.md
   - ⚠️ 不要用 `write_file` / `terminal` 直接编辑记忆 md 文件——必须走 memory 工具（有注入检查、drift guard）
4. 修正时若发现库中反而缺内容 → 回到第一步流程补写（正常情况下不会发生，因为第一步已保证库 ⊇ 记忆）

### 第九步：报告

告诉用户：处理了多少脏组、分类型统计（修正 X 条 / 合并 Y 组 / 迁移 Z 条 / 删除 W 条）、清退冷存储多少条、常驻记忆补写/修正了多少条。

## 常见陷阱

- **❌ 用 `write_file` / `terminal` / SQL 直接操作数据库**——绕过 `dirty` 管理、FTS 同步、事务原子性。只能用工具 API
- **❌ 只处理"有评论"的条目**——脏标记可能来自新增条目（无评论但有重复），逐条目扫
- **❌ 传 diff / 追加式 entries 到 `note_rewrite`**——全量重写：传 3 条就只有 3 条
- **❌ 合并时用旧的 `last_used`**——取三者中最新，否则合并后立刻被冷存储清退
- **❌ `misplaced` 只加不删**——目标分类创建 + 源组排除，两步
- **❌ 主动大规模整理没脏标记的组 / 为合并而合并**——响应式：没信号不动；每个动作要有明确收益
- **❌ 编辑冷存储**——`cold_batches` + `cold_entries` 不参与维护；找回用 `note_recall(entry_header)`（只读）
- **❌ 用 `write_file` / `terminal` 直接编辑常驻记忆 md（MEMORY.md / USER.md）**——必须用 memory 工具（replace/remove/add），否则绕过注入检查与 drift guard
- **❌ 在维护中途纠正常驻记忆**——库还没整理完，内容不一定是权威；同步在最开始、纠正在最末尾
- **❌ 忽视超限告警**——`oversized_groups` / `overpopulated_categories` 非空时只处理评论就了事，超限组下次维护还会出现
- **❌ 放任名称失真**——组/分类名是 INDEX 的检索锚：名字过泛（“笔记”、“杂项”）或与实际内容不符，会让后续检索路由失准。处理脏组时发现名称失真，用 `note_rename_group` / `note_rename_category` 修正（新 path 冲突先合并）
