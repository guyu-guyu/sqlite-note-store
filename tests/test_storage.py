"""Storage layer tests — CRUD, FTS sync, cold storage moves."""

from __future__ import annotations

from sqlite_note_store import markdown_io, schema, storage


def _conn(tmp_path):
    return schema.connect(tmp_path)


def test_upsert_file_and_read_back(tmp_path):
    conn = _conn(tmp_path)
    fid = storage.upsert_file(
        conn,
        path="cat/a.md", category="cat", slug="a", title="A",
        tags=["x", "y"], dirty=True,
        created="2026-01-01T00:00:00+00:00",
        updated="2026-01-02T00:00:00+00:00",
    )
    conn.commit()
    fetched = storage.get_file_by_path(conn, "cat/a.md")
    assert fetched is not None
    assert fetched.id == fid
    assert fetched.title == "A"
    assert fetched.tags == ["x", "y"]
    assert fetched.dirty is True
    conn.close()


def test_upsert_preserves_created_but_updates_updated(tmp_path):
    conn = _conn(tmp_path)
    storage.upsert_file(
        conn, path="c/n.md", category="c", slug="n", title="N",
        tags=[], dirty=False, created="2026-01-01T00:00:00+00:00",
        updated="2026-01-01T00:00:00+00:00",
    )
    conn.commit()
    storage.upsert_file(
        conn, path="c/n.md", category="c", slug="n", title="N2",
        tags=[], dirty=False, created="2099-12-31T00:00:00+00:00",  # ignored on update
        updated="2026-06-01T00:00:00+00:00",
    )
    conn.commit()
    row = storage.get_file_by_path(conn, "c/n.md")
    assert row.title == "N2"
    # updated changed, created preserved from the initial insert:
    assert row.created == "2026-01-01T00:00:00+00:00"
    assert row.updated == "2026-06-01T00:00:00+00:00"
    conn.close()


def test_replace_entries_wipes_and_reinserts(tmp_path):
    conn = _conn(tmp_path)
    fid = storage.upsert_file(
        conn, path="c/f.md", category="c", slug="f", title="F",
        tags=[], dirty=False,
    )
    storage.append_entry(conn, fid, header="one", content="body-1")
    storage.append_entry(conn, fid, header="two", content="body-2")
    conn.commit()

    assert len(storage.list_entries(conn, fid)) == 2

    new_entries = [
        markdown_io.ParsedEntry(header="alpha", content="A"),
        markdown_io.ParsedEntry(header="beta", content="B", last_used="2026-08-01T00:00:00+00:00"),
    ]
    storage.replace_entries(conn, fid, new_entries)
    conn.commit()

    entries = storage.list_entries(conn, fid)
    assert [e.header for e in entries] == ["alpha", "beta"]
    assert entries[1].last_used == "2026-08-01T00:00:00+00:00"
    # order_index must be sequential.
    assert [e.order_index for e in entries] == [0, 1]
    conn.close()


def test_fts_search_finds_by_header_and_content(tmp_path):
    conn = _conn(tmp_path)
    fid = storage.upsert_file(
        conn, path="game/br.md", category="game", slug="br", title="卡牌BR",
        tags=["game"], dirty=False,
    )
    storage.append_entry(conn, fid, header="战斗流程", content="出牌 结算 胜负判定")
    storage.append_entry(conn, fid, header="Bug 反馈", content="合服问题 分服 crash")
    conn.commit()

    hits = storage.search_fts(conn, "结算", limit=5)
    assert any(h["path"] == "game/br.md" for h in hits)

    hits2 = storage.search_fts(conn, "crash", limit=5)
    assert any("Bug" in h["snippet"] or "crash" in h["snippet"] for h in hits2)
    conn.close()


def test_delete_file_cascades_and_wipes_fts(tmp_path):
    conn = _conn(tmp_path)
    fid = storage.upsert_file(
        conn, path="c/x.md", category="c", slug="x", title="X",
        tags=[], dirty=False,
    )
    storage.append_entry(conn, fid, header="h", content="findable-token")
    conn.commit()

    assert storage.search_fts(conn, "findable-token")

    storage.delete_file(conn, fid)
    conn.commit()

    assert not storage.search_fts(conn, "findable-token")
    assert storage.get_file_by_path(conn, "c/x.md") is None
    conn.close()


def test_comment_marks_file_dirty(tmp_path):
    conn = _conn(tmp_path)
    fid = storage.upsert_file(
        conn, path="c/y.md", category="c", slug="y", title="Y",
        tags=[], dirty=False,
    )
    eid = storage.append_entry(conn, fid, header="h", content="body")
    conn.commit()

    assert storage.get_file_by_path(conn, "c/y.md").dirty is False

    storage.append_comment(
        conn, eid, comment_type="wrong", comment_text="fix this",
    )
    conn.commit()

    row = storage.get_file_by_path(conn, "c/y.md")
    assert row.dirty is True

    entry = storage.list_entries(conn, fid)[0]
    assert len(entry.comments) == 1
    assert entry.comments[0]["type"] == "wrong"
    conn.close()


def test_move_entry_to_cold_removes_from_active(tmp_path):
    conn = _conn(tmp_path)
    fid = storage.upsert_file(
        conn, path="c/z.md", category="c", slug="z", title="Z",
        tags=[], dirty=False,
    )
    eid = storage.append_entry(
        conn, fid, header="stale", content="ancient",
        last_used="2020-01-01T00:00:00+00:00",
    )
    conn.commit()

    cf_id = storage.get_or_create_cold_file_for_today(conn)
    new_ce = storage.move_entry_to_cold(conn, eid, cold_file_id=cf_id)
    conn.commit()

    assert new_ce is not None
    # No longer active:
    assert storage.list_entries(conn, fid) == []
    # Retrievable via cold lookup:
    hit = storage.find_cold_entry(conn, "stale")
    assert hit is not None
    assert hit["content"] == "ancient"
    assert hit["original_category"] == "c"
    conn.close()


def test_cold_file_rollover_and_limit_enforcement(tmp_path):
    conn = _conn(tmp_path)
    # Insert three cold files with distinct 'created' timestamps.
    conn.execute("INSERT INTO cold_files(filename, created) VALUES ('a.md', '2026-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO cold_files(filename, created) VALUES ('b.md', '2026-01-02T00:00:00+00:00')")
    conn.execute("INSERT INTO cold_files(filename, created) VALUES ('c.md', '2026-01-03T00:00:00+00:00')")
    conn.commit()

    deleted = storage.enforce_cold_file_limit(conn, max_files=2)
    conn.commit()
    assert deleted == 1

    remaining = [r["filename"] for r in storage.list_cold_files(conn)]
    # Oldest ('a.md') was pruned.
    assert "a.md" not in remaining
    assert set(remaining) == {"b.md", "c.md"}
    conn.close()
