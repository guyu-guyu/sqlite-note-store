"""Dashboard plugin_api 端到端冒烟测试。
用 FastAPI TestClient 对临时 DB 跑全部 8+1 路由。
"""
import os
import sys
import tempfile
from pathlib import Path

# 设置临时 NOTE_ROOT
tmpdir = tempfile.mkdtemp(prefix="notes_smoke_")
os.environ["NOTE_ROOT"] = tmpdir
print(f"NOTE_ROOT={tmpdir}")

# 初始化 schema + 插入测试数据
sys.path.insert(0, "/projects/sqlite-note-store-plugin")
from sqlite_note_store.schema import connect

conn = connect(Path(tmpdir))
conn.execute(
    "INSERT INTO groups (path, category, slug, title, tags, dirty, created, updated) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    ("uncategorized/test", "uncategorized", "test", "测试组",
     '["tag1"]', 0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
)
group_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.execute(
    "INSERT INTO entries (group_id, header, content, comments, order_index) "
    "VALUES (?, ?, ?, ?, ?)",
    (group_id, "第一个条目", "这是测试内容\n多行\n多行", '[]', 0),
)
e1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.execute(
    "INSERT INTO entries (group_id, header, content, comments, order_index) "
    "VALUES (?, ?, ?, ?, ?)",
    (group_id, "第二个条目", "搜索关键词在这里", '[]', 1),
)
e2 = e1 + 1
# FTS5 同步
for eid, hdr, ctnt in [
    (e1, "第一个条目", "这是测试内容\n多行\n多行"),
    (e2, "第二个条目", "搜索关键词在这里"),
]:
    conn.execute(
        "INSERT INTO entries_fts(rowid, header, content, category, group_title, group_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (eid, hdr, ctnt, "uncategorized", "测试组", "uncategorized/test"),
    )
conn.commit()
conn.close()

# 导入 plugin_api 并用 TestClient 测试
sys.path.insert(0, "/projects/sqlite-note-store-plugin/dashboard")
import plugin_api  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(plugin_api.router, prefix="/api/plugins/notes")
client = TestClient(app)

errors = []

# 1. GET /index
r = client.get("/api/plugins/notes/index")
assert r.status_code == 200, f"GET /index failed: {r.text}"
data = r.json()
assert len(data) == 1 and data[0]["category"] == "uncategorized"
assert data[0]["files"][0]["entry_count"] == 2
print(f"  ✓ GET /index — {len(data)} categories, {data[0]['files'][0]['entry_count']} entries")

# 2. GET /stats
r = client.get("/api/plugins/notes/stats")
assert r.status_code == 200, f"GET /stats failed: {r.text}"
stats = r.json()
assert stats["total_entries"] == 2
assert stats["total_groups"] == 1
print(f"  ✓ GET /stats — {stats['total_entries']} entries, {stats['total_groups']} groups")

# 3. GET /files/{id}
r = client.get(f"/api/plugins/notes/files/{group_id}")
assert r.status_code == 200, f"GET /files failed: {r.text}"
assert len(r.json()["entries"]) == 2
print(f"  ✓ GET /files/{group_id} — {len(r.json()['entries'])} entries")

# 4. GET /entries/{id}
r = client.get(f"/api/plugins/notes/entries/{e1}")
assert r.status_code == 200, f"GET /entries failed: {r.text}"
assert r.json()["header"] == "第一个条目"
print(f"  ✓ GET /entries/{e1} — header={r.json()['header']}")

# 5. PUT /entries/{id} — 编辑
r = client.put(f"/api/plugins/notes/entries/{e1}", json={"content": "更新后的内容"})
assert r.status_code == 200, f"PUT /entries failed: {r.text}"
# 验证更新生效
r2 = client.get(f"/api/plugins/notes/entries/{e1}")
assert r2.json()["content"] == "更新后的内容"
print(f"  ✓ PUT /entries/{e1} — content updated")

# 6. POST /entries — 新建
r = client.post("/api/plugins/notes/entries", json={
    "file_id": group_id, "header": "新条目", "content": "新建内容"
})
assert r.status_code == 200, f"POST /entries failed: {r.text}"
new_id = r.json()["id"]
print(f"  ✓ POST /entries — created id={new_id}")

# 7. GET /search — FTS5
r = client.get("/api/plugins/notes/search?q=搜索关键词")
assert r.status_code == 200, f"GET /search failed: {r.text}"
assert r.json()["count"] == 1
print(f"  ✓ GET /search — {r.json()['count']} results for '搜索关键词'")

# 8. GET /cold
r = client.get("/api/plugins/notes/cold")
assert r.status_code == 200, f"GET /cold failed: {r.text}"
assert r.json()["total"] == 0
print(f"  ✓ GET /cold — {r.json()['total']} cold batches")

# 9. DELETE /entries/{id}
r = client.delete(f"/api/plugins/notes/entries/{new_id}")
assert r.status_code == 200, f"DELETE /entries failed: {r.text}"
# 验证删除生效
r2 = client.get(f"/api/plugins/notes/entries/{new_id}")
assert r2.status_code == 404
print(f"  ✓ DELETE /entries/{new_id} — deleted, 404 on re-fetch")

# 10. 404测试
r = client.get("/api/plugins/notes/entries/99999")
assert r.status_code == 404
print(f"  ✓ 404 test — nonexistent entry returns 404")

print(f"\n{'='*40}")
print(f"ALL {10} SMOKE TESTS PASSED ✓")
