"""记忆笔记 Dashboard 插件 — 后端 API 路由。

挂载在 /api/plugins/notes/ 下。

此层是 SQLite 存储的薄封装层：
  - GET  /index          → INDEX 树（分类 > 组 > 条目头）
  - GET  /files/{id}     → 组详情（含所有条目；路由名沿用历史术语 file，对应存储概念 group）
  - GET  /entries/{id}   → 单条目内容
  - POST /entries        → 新建条目
  - PUT  /entries/{id}   → 编辑条目（header/content/tags）
  - DELETE /entries/{id} → 删除条目
  - GET  /search?q=      → FTS5 全文搜索
  - GET  /cold           → 冷存储浏览

安全说明：所有路由由 Dashboard session-token 中间件保护。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# DB 连接
# ---------------------------------------------------------------------------

_DB_PATH: Optional[Path] = None


def _resolve_db_path() -> Path:
    """定位 notes.sqlite3，优先级：
      1. 环境变量 NOTE_ROOT
      2. <hermes_home>/notes/notes.sqlite3
      3. ~/.hermes/notes/notes.sqlite3
    """
    global _DB_PATH
    if _DB_PATH is not None and _DB_PATH.exists():
        return _DB_PATH

    candidates = []
    import os
    env_root = os.environ.get("NOTE_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home) / "notes")
    candidates.append(Path.home() / ".hermes" / "notes")

    for root in candidates:
        db = root / "notes.sqlite3"
        if db.exists():
            _DB_PATH = db
            return db

    raise HTTPException(
        status_code=404,
        detail=f"notes.sqlite3 未找到。已搜索: {[str(c) for c in candidates]}",
    )


def _conn() -> sqlite3.Connection:
    db_path = _resolve_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------


class EntryUpdate(BaseModel):
    """编辑条目时的请求体。所有字段可选——只传变更的部分。"""
    header: Optional[str] = None
    content: Optional[str] = None
    last_used: Optional[str] = None


class EntryCreate(BaseModel):
    """新建条目时的请求体。"""
    file_id: int = Field(..., description="目标组 ID（路由/字段名沿用历史术语 file，对应存储概念 group）")
    header: str = Field(..., description="条目标题")
    content: str = Field("", description="条目正文")


class FileCreate(BaseModel):
    """新建组时的请求体。"""
    category: str = Field(..., description="分类名（即文件夹名）")
    title: str = Field(..., description="组标题（将用作 markdown 的 title 字段）")


class FileUpdate(BaseModel):
    """编辑组时的请求体。所有字段可选。"""
    title: Optional[str] = None
    category: Optional[str] = None


class CategoryUpdate(BaseModel):
    """重命名分类时的请求体。"""
    old_name: str = Field(..., description="当前分类名")
    new_name: str = Field(..., description="新分类名")


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _entry_dict(row: sqlite3.Row) -> dict:
    d = _row_to_dict(row)
    # 解析 comments JSON
    try:
        d["comments"] = json.loads(d.get("comments") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["comments"] = []
    return d


def _group_dict(row: sqlite3.Row) -> dict:
    d = _row_to_dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# FTS5 同步辅助
# ---------------------------------------------------------------------------

_FTS_COLUMNS = "header, content, category, group_title, group_path"


def _fts_sync_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    """单条目的 FTS5 同步——编辑/新建后调用。"""
    row = conn.execute(
        """SELECT e.id, e.header, e.content, g.category, g.title, g.path
           FROM entries e JOIN groups g ON e.group_id = g.id
           WHERE e.id = ?""",
        (entry_id,),
    ).fetchone()
    if row is None:
        return
    # 先删旧索引
    conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry_id,))
    # 插入新索引
    conn.execute(
        f"INSERT INTO entries_fts(rowid, {_FTS_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], row["header"], row["content"],
         row["category"], row["title"] or "", row["path"]),
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.get("/index")
def get_index():
    """INDEX 树：分类 → 文件 → 条目（id+header+dirty）。

    返回结构：
    ```json
    [
      {
        "category": "uncategorized",
        "files": [
          {
            "id": 1, "path": "uncategorized/foo", "title": "...",
            "dirty": false, "entry_count": 3,
            "entries": [
              {"id": 1, "header": "...", "dirty": false},
              ...
            ]
          },
          ...
        ]
      },
      ...
    ]
    ```
    """
    conn = _conn()
    try:
        files = conn.execute(
            """SELECT g.id, g.path, g.category, g.slug, g.title, g.tags,
                      g.dirty, g.created, g.updated,
                      (SELECT COUNT(*) FROM entries e WHERE e.group_id = g.id) AS entry_count
               FROM groups g
               ORDER BY g.category, g.slug"""
        ).fetchall()

        # 按分类分组
        categories: dict[str, list] = {}
        file_ids = [f["id"] for f in files]
        # 批量取所有条目头
        entries_by_group: dict[int, list] = {}
        if file_ids:
            placeholders = ",".join("?" * len(file_ids))
            all_entries = conn.execute(
                f"""SELECT id, group_id, header, content, order_index
                    FROM entries
                    WHERE group_id IN ({placeholders})
                    ORDER BY group_id, order_index""",
                file_ids,
            ).fetchall()
            for e in all_entries:
                entries_by_group.setdefault(e["group_id"], []).append({
                    "id": e["id"],
                    "header": e["header"],
                    "preview": (e["content"][:120] + "...") if len(e["content"]) > 120 else e["content"],
                })

        result = []
        for f in files:
            cat = f["category"]
            if cat not in categories:
                categories[cat] = []
            fd = _group_dict(f)
            fd["entry_count"] = f["entry_count"]
            fd["entries"] = entries_by_group.get(f["id"], [])
            categories[cat].append(fd)

        for cat, cat_files in sorted(categories.items()):
            result.append({"category": cat, "files": cat_files})

        return result
    finally:
        conn.close()


@router.get("/files/{file_id}")
def get_file(file_id: int):
    """组详情——含完整条目列表（含 content）。路由名沿用历史术语 file。"""
    conn = _conn()
    try:
        f = conn.execute(
            "SELECT * FROM groups WHERE id = ?", (file_id,)
        ).fetchone()
        if f is None:
            raise HTTPException(404, detail=f"组 {file_id} 不存在")
        fd = _group_dict(f)
        entries = conn.execute(
            """SELECT * FROM entries WHERE group_id = ? ORDER BY order_index""",
            (file_id,),
        ).fetchall()
        fd["entries"] = [_entry_dict(e) for e in entries]
        return fd
    finally:
        conn.close()


@router.get("/entries/{entry_id}")
def get_entry(entry_id: int):
    """单条目详情——含完整 content。"""
    conn = _conn()
    try:
        row = conn.execute(
            """SELECT e.*, g.path AS file_path, g.category, g.title AS file_title, g.id AS file_id
               FROM entries e JOIN groups g ON e.group_id = g.id
               WHERE e.id = ?""",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail=f"条目 {entry_id} 不存在")
        return _entry_dict(row)
    finally:
        conn.close()


@router.post("/entries")
def create_entry(body: EntryCreate):
    """新建条目。自动追加到组末尾，更新 updated 时间戳。"""
    conn = _conn()
    try:
        f = conn.execute("SELECT id FROM groups WHERE id = ?", (body.file_id,)).fetchone()
        if f is None:
            raise HTTPException(404, detail=f"组 {body.file_id} 不存在")

        # 获取下一个 order_index
        max_idx = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) FROM entries WHERE group_id = ?",
            (body.file_id,),
        ).fetchone()[0]
        next_idx = max_idx + 1

        now = _now_iso()
        cursor = conn.execute(
            """INSERT INTO entries (group_id, header, content, comments, order_index)
               VALUES (?, ?, ?, '[]', ?)""",
            (body.file_id, body.header, body.content, next_idx),
        )
        entry_id = cursor.lastrowid or 0

        # 更新组 updated
        conn.execute("UPDATE groups SET updated = ? WHERE id = ?", (now, body.file_id))

        if entry_id:
            # FTS5 同步
            _fts_sync_entry(conn, entry_id)

        conn.commit()
        log.info("Created entry %d in group %d", entry_id, body.file_id)
        return {"id": entry_id, "message": "条目已创建"}
    finally:
        conn.close()


@router.post("/files")
def create_file(body: FileCreate):
    """新建组。根据 title 自动生成 slug，组合为 category/slug 路径（导出时拼 .md）。
    如果路径已存在，返回 409 冲突。"""
    conn = _conn()
    try:
        # 从 title 生成 slug: 取最后一个 / 后的部分，去掉扩展名和特殊字符
        slug = body.title.strip()
        if "/" in slug:
            slug = slug.rsplit("/", 1)[-1]
        slug = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', slug).strip("_") or "untitled"

        category = body.category.strip() or "uncategorized"
        path = f"{category}/{slug}"

        existing = conn.execute("SELECT id FROM groups WHERE path = ?", (path,)).fetchone()
        if existing is not None:
            raise HTTPException(409, detail=f"组已存在: {path}")

        now = _now_iso()
        cursor = conn.execute(
            """INSERT INTO groups (path, category, slug, title, tags, dirty, created, updated)
               VALUES (?, ?, ?, ?, '[]', 1, ?, ?)""",
            (path, category, slug, body.title, now, now),
        )
        file_id = cursor.lastrowid or 0
        conn.commit()
        log.info("Created group %d: %s", file_id, path)
        return {"id": file_id, "path": path, "message": "组已创建"}
    finally:
        conn.close()


@router.get("/categories")
def list_categories():
    """返回所有已有分类名（去重）。用于新建文件弹窗的分类下拉框。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT DISTINCT category FROM groups ORDER BY category").fetchall()
        return {"categories": [r["category"] for r in rows]}
    finally:
        conn.close()


def _make_slug(title: str) -> str:
    """从标题生成 slug（与 POST /files 一致的逻辑）。"""
    slug = title.strip()
    if "/" in slug:
        slug = slug.rsplit("/", 1)[-1]
    slug = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', slug).strip("_") or "untitled"
    return slug


@router.put("/files/{file_id}")
def update_file(file_id: int, body: FileUpdate):
    """编辑组——改名（更新 title/slug/path）和/或移分类（更新 category/path）。
    非空组（含条目）也可以改名——改名不删除内容。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, path, category, slug, title FROM groups WHERE id = ?",
            (file_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail=f"组 {file_id} 不存在")

        new_title = body.title if body.title is not None else row["title"]
        new_category = body.category.strip() if body.category is not None else row["category"]
        if not new_category:
            new_category = "uncategorized"

        new_slug = _make_slug(new_title)
        new_path = f"{new_category}/{new_slug}"

        # path 冲突检测（排除自身）
        clash = conn.execute(
            "SELECT id FROM groups WHERE path = ? AND id != ?", (new_path, file_id)
        ).fetchone()
        if clash is not None:
            raise HTTPException(409, detail=f"路径冲突: {new_path}")

        now = _now_iso()
        conn.execute(
            "UPDATE groups SET title = ?, category = ?, slug = ?, path = ?, updated = ? WHERE id = ?",
            (new_title, new_category, new_slug, new_path, now, file_id),
        )
        conn.commit()
        log.info("Updated group %d: %s → %s", file_id, row["path"], new_path)
        return {"id": file_id, "path": new_path, "message": "组已更新"}
    finally:
        conn.close()


@router.delete("/files/{file_id}")
def delete_file(file_id: int):
    """删除组。仅允许删除空组（无条目）——非空组返回 409。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, path FROM groups WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail=f"组 {file_id} 不存在")

        entry_count = conn.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE group_id = ?", (file_id,)
        ).fetchone()["n"]
        if entry_count > 0:
            raise HTTPException(
                409,
                detail=f"组非空（含 {entry_count} 个条目），请先清空条目后再删除",
            )

        conn.execute("DELETE FROM groups WHERE id = ?", (file_id,))
        conn.commit()
        log.info("Deleted empty group %d: %s", file_id, row["path"])
        return {"id": file_id, "message": "组已删除"}
    finally:
        conn.close()


@router.put("/categories")
def rename_category(body: CategoryUpdate):
    """重命名分类——批量更新该分类下所有组的 category + path。
    支持多段路径（精确匹配整个分类路径，如 game/br → game/card）；
    子分类（更深的路径）不受影响。"""
    conn = _conn()
    try:
        old_name = body.old_name.strip()
        new_name = body.new_name.strip() or "uncategorized"
        if old_name == new_name:
            return {"message": "分类名未变更"}

        rows = conn.execute(
            "SELECT id, path, slug, title FROM groups WHERE category = ? ORDER BY id",
            (old_name,),
        ).fetchall()
        if not rows:
            raise HTTPException(404, detail=f"分类 '{old_name}' 不存在或为空")

        # 逐组更新 path，检测冲突
        now = _now_iso()
        renamed = 0
        for r in rows:
            new_path = f"{new_name}/{r['slug']}"
            clash = conn.execute(
                "SELECT id FROM groups WHERE path = ? AND id != ?", (new_path, r["id"])
            ).fetchone()
            if clash is not None:
                raise HTTPException(
                    409,
                    detail=f"路径冲突: {new_path}（可能与已有组撞名）",
                )
            conn.execute(
                "UPDATE groups SET category = ?, path = ?, updated = ? WHERE id = ?",
                (new_name, new_path, now, r["id"]),
            )
            renamed += 1

        conn.commit()
        log.info("Renamed category '%s' → '%s' (%d groups)", old_name, new_name, renamed)
        return {"old_name": old_name, "new_name": new_name, "files_updated": renamed}
    finally:
        conn.close()


@router.delete("/categories/{category_name}")
def delete_category(category_name: str):
    """删除分类——仅允许删除空分类（无组）。非空返回 409。"""
    conn = _conn()
    try:
        group_count = conn.execute(
            "SELECT COUNT(*) AS n FROM groups WHERE category = ?", (category_name,)
        ).fetchone()["n"]
        if group_count > 0:
            raise HTTPException(
                409,
                detail=f"分类非空（含 {group_count} 个组），请先清空组后再删除",
            )

        # 空分类在 groups 表中没有行，无需 DELETE
        conn.commit()
        log.info("Deleted empty category '%s'", category_name)
        return {"category": category_name, "message": "分类已删除"}
    finally:
        conn.close()


@router.put("/entries/{entry_id}")
def update_entry(entry_id: int, body: EntryUpdate):
    """编辑条目。只更新传入的字段，自动更新所属组 updated 时间戳。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, group_id FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail=f"条目 {entry_id} 不存在")

        updates: list[str] = []
        params: list[Any] = []
        if body.header is not None:
            updates.append("header = ?")
            params.append(body.header)
        if body.content is not None:
            updates.append("content = ?")
            params.append(body.content)
        if body.last_used is not None:
            updates.append("last_used = ?")
            params.append(body.last_used)

        if not updates:
            return {"id": entry_id, "message": "无变更"}

        params.append(entry_id)
        conn.execute(
            f"UPDATE entries SET {', '.join(updates)} WHERE id = ?",
            params,
        )

        # 更新所属组 updated
        conn.execute(
            "UPDATE groups SET updated = ? WHERE id = ?",
            (_now_iso(), row["group_id"]),
        )

        # FTS5 同步
        _fts_sync_entry(conn, entry_id)

        conn.commit()
        log.info("Updated entry %d: %s", entry_id, ", ".join(updates))
        return {"id": entry_id, "message": "条目已更新"}
    finally:
        conn.close()


@router.delete("/entries/{entry_id}")
def delete_entry(entry_id: int):
    """删除条目。自动重排组内剩余条目的 order_index。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, group_id, order_index FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail=f"条目 {entry_id} 不存在")

        group_id = row["group_id"]
        removed_idx = row["order_index"]

        # FTS5 索引删除
        conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry_id,))

        # 删除条目
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

        # 重排后续条目
        conn.execute(
            """UPDATE entries SET order_index = order_index - 1
               WHERE group_id = ? AND order_index > ?""",
            (group_id, removed_idx),
        )

        # 更新所属组 updated
        conn.execute(
            "UPDATE groups SET updated = ? WHERE id = ?",
            (_now_iso(), group_id),
        )

        conn.commit()
        log.info("Deleted entry %d from group %d", entry_id, group_id)
        return {"id": entry_id, "message": "条目已删除"}
    finally:
        conn.close()


@router.get("/search")
def search_entries(
    q: str = Query(..., description="搜索关键词"),
    limit: int = Query(50, ge=1, le=200),
):
    """全文搜索——双路径策略：
      1. 先用 FTS5 prefix 查询（适合英文、带空格的词组）
      2. 同时用 LIKE 兜底（FTS5 的 unicode61 tokenizer 会把整个中文串
         当一个 token，对中文子串搜索失效，LIKE 是可靠的兜底路径）
    去重后返回。
    """
    conn = _conn()
    try:
        all_ids: set[int] = set()

        # Path 1: FTS5 prefix query
        try:
            safe_q = q.replace('"', '""')
            fts_rows = conn.execute(
                """SELECT e.id FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid
                   WHERE entries_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (f'"{safe_q}"*', limit),
            ).fetchall()
            for r in fts_rows:
                all_ids.add(r["id"])
        except sqlite3.OperationalError:
            pass  # FTS5 语法错误，直接用 LIKE

        # Path 2: LIKE 兜底
        like_pattern = f"%{q}%"
        like_rows = conn.execute(
            """SELECT e.id FROM entries e
               WHERE e.header LIKE ? OR e.content LIKE ?
               LIMIT ?""",
            (like_pattern, like_pattern, limit),
        ).fetchall()
        for r in like_rows:
            all_ids.add(r["id"])

        if not all_ids:
            return {"query": q, "count": 0, "results": []}

        # 取详情
        id_list = list(all_ids)[:limit]
        placeholders = ",".join("?" * len(id_list))
        rows = conn.execute(
            f"""SELECT e.id, e.header, e.content, e.last_used, e.comments,
                      g.path AS file_path, g.category, g.title AS file_title, g.id AS file_id
               FROM entries e JOIN groups g ON e.group_id = g.id
               WHERE e.id IN ({placeholders})
               ORDER BY e.id""",
            id_list,
        ).fetchall()

        # 构造带高亮的 preview
        results = []
        for row in rows:
            d = _entry_dict(row)
            content = d.get("content", "")
            # 简单高亮：截取关键词周围 40 字符
            idx = content.find(q)
            if idx >= 0:
                start = max(0, idx - 40)
                end = min(len(content), idx + len(q) + 40)
                snippet = content[start:end]
                snippet = snippet.replace(q, f"<mark>{q}</mark>")
                d["snippet"] = snippet
            else:
                d["snippet"] = content[:80] + "..." if len(content) > 80 else content
            results.append(d)

        return {"query": q, "count": len(results), "results": results}
    finally:
        conn.close()


@router.get("/cold")
def get_cold():
    """冷存储浏览——列出所有冷存储批次及其条目。"""
    conn = _conn()
    try:
        cold_batches = conn.execute(
            """SELECT cb.id, cb.filename, cb.created,
                      (SELECT COUNT(*) FROM cold_entries ce WHERE ce.cold_batch_id = cb.id) AS entry_count
               FROM cold_batches cb
               ORDER BY cb.created DESC"""
        ).fetchall()

        batch_ids = [cb["id"] for cb in cold_batches]
        entries_by_batch: dict[int, list] = {}
        if batch_ids:
            placeholders = ",".join("?" * len(batch_ids))
            all_entries = conn.execute(
                f"""SELECT * FROM cold_entries
                    WHERE cold_batch_id IN ({placeholders})
                    ORDER BY cold_batch_id, order_index""",
                batch_ids,
            ).fetchall()
            for e in all_entries:
                entries_by_batch.setdefault(e["cold_batch_id"], []).append({
                    "id": e["id"],
                    "header": e["header"],
                    "content": e["content"],
                    "last_used": e["last_used"],
                    "original_category": e["original_category"],
                    "preview": (e["content"][:120] + "...") if len(e["content"]) > 120 else e["content"],
                })

        result = []
        for cb in cold_batches:
            d = _row_to_dict(cb)
            d["entries"] = entries_by_batch.get(cb["id"], [])
            result.append(d)

        # API 键沿用历史术语 files（对应存储概念 batch），前端兼容。
        return {"files": result, "total": len(result)}
    finally:
        conn.close()


@router.get("/stats")
def get_stats():
    """统计信息——用于仪表盘卡片。"""
    conn = _conn()
    try:
        total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        total_groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        dirty_groups = conn.execute(
            "SELECT COUNT(*) FROM groups WHERE dirty = 1"
        ).fetchone()[0]
        cold_entries = conn.execute("SELECT COUNT(*) FROM cold_entries").fetchone()[0]
        cold_batches = conn.execute("SELECT COUNT(*) FROM cold_batches").fetchone()[0]
        categories = conn.execute(
            "SELECT DISTINCT category FROM groups ORDER BY category"
        ).fetchall()

        return {
            "total_entries": total_entries,
            "total_groups": total_groups,
            "dirty_groups": dirty_groups,
            "cold_entries": cold_entries,
            "cold_batches": cold_batches,
            "categories": [r[0] for r in categories],
            "db_path": str(_resolve_db_path()),
        }
    finally:
        conn.close()
