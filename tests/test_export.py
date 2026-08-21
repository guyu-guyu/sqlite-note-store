"""Export / import round-trip tests.

These tests are the executable form of the plugin's core promise:

    SQLite is the source of truth; markdown is a lossless projection.

Concretely:
    * A DB → export → filesystem produces the expected on-disk shape.
    * A filesystem tree → import → DB → export → filesystem is byte-
      stable (round-trip idempotent).
    * DB values survive the trip: title/tags/dirty/created/updated,
      entry header/content/last_used/comments, cold-storage per-file
      grouping and category-of-origin metadata.
"""

from __future__ import annotations

from sqlite_note_store import export, markdown_io, schema, storage


def _seed_conn(tmp_path):
    """Build a representative fixture DB: two active groups + 1 cold batch."""
    conn = schema.connect(tmp_path)

    # Group 1: category with tags and comments.
    gid1 = storage.upsert_group(
        conn, path="game/br.md", category="game", slug="br", title="卡牌BR",
        tags=["game", "br"], dirty=True,
        created="2026-08-01T00:00:00+00:00",
        updated="2026-08-20T00:00:00+00:00",
    )
    storage.replace_entries(conn, gid1, [
        markdown_io.ParsedEntry(
            header="战斗流程", content="出牌 → 结算",
            last_used="2026-08-15T00:00:00+00:00",
        ),
        markdown_io.ParsedEntry(
            header="Bug 反馈", content="合服 crash",
            last_used="2026-08-19T00:00:00+00:00",
            comments=[{
                "type": "needs_improvement",
                "text": "add repro steps",
                "timestamp": "2026-08-20T00:00:00+00:00",
            }],
        ),
    ])

    # Group 2: uncategorized single-entry group, no tags.
    gid2 = storage.upsert_group(
        conn, path="uncategorized/notes.md", category="uncategorized",
        slug="notes", title="Notes", tags=[], dirty=False,
        created="2026-08-01T00:00:00+00:00",
        updated="2026-08-10T00:00:00+00:00",
    )
    storage.append_entry(conn, gid2, header="lone entry", content="just this")

    # Cold storage batch.
    conn.execute(
        "INSERT INTO cold_batches(filename, created) VALUES "
        "('2026-07-01.md', '2026-07-01T00:00:00+00:00')"
    )
    batch_id = conn.execute("SELECT id FROM cold_batches").fetchone()["id"]
    conn.execute(
        "INSERT INTO cold_entries(cold_batch_id, header, content, last_used, "
        "original_category, order_index) VALUES (?, ?, ?, ?, ?, ?)",
        (batch_id, "old header", "old body", "2026-06-01T00:00:00+00:00",
         "game", 0),
    )
    conn.commit()
    return conn


def test_export_writes_expected_shape(tmp_path):
    conn = _seed_conn(tmp_path / "db")
    out = tmp_path / "out"
    stats = export.export_to_directory(conn, out)
    conn.close()

    assert stats == {"groups": 2, "entries": 3, "cold_batches": 1, "cold_entries": 1}

    # Directory layout matches markdown-note-store convention.
    assert (out / "INDEX.md").exists()
    assert (out / "game" / "br.md").exists()
    assert (out / "uncategorized" / "notes.md").exists()
    assert (out / "cold-storage" / "2026-07-01.md").exists()

    # Group 1: YAML block + inline metadata suffix survived.
    br_text = (out / "game" / "br.md").read_text(encoding="utf-8")
    assert "title: 卡牌BR" in br_text
    assert "tags: [game, br]" in br_text
    assert "dirty: true" in br_text
    assert "## 战斗流程" in br_text
    assert '"last_used": "2026-08-15T00:00:00+00:00"' in br_text
    assert '"needs_improvement"' in br_text

    # Cold-storage file has NO YAML block (matches ref plugin).
    cold_text = (out / "cold-storage" / "2026-07-01.md").read_text(encoding="utf-8")
    assert not cold_text.startswith("---")
    assert cold_text.startswith("## old header")


def test_import_roundtrip_is_idempotent(tmp_path):
    """Import the export of an export — DB state must match position-for-position."""
    src_conn = _seed_conn(tmp_path / "src_db")
    export_dir = tmp_path / "export1"
    export.export_to_directory(src_conn, export_dir)
    src_conn.close()

    # Fresh DB → import from the exported dir.
    dst_conn = schema.connect(tmp_path / "dst_db")
    stats = export.import_from_directory(dst_conn, export_dir, replace=True)
    assert stats["groups"] == 2
    assert stats["entries"] == 3
    assert stats["cold_batches"] == 1
    assert stats["cold_entries"] == 1

    # DB state matches: group rows.
    groups = storage.list_groups(dst_conn)
    paths = {g.path: g for g in groups}
    assert set(paths) == {"game/br.md", "uncategorized/notes.md"}

    br = paths["game/br.md"]
    assert br.title == "卡牌BR"
    assert br.tags == ["game", "br"]
    assert br.dirty is True
    assert br.created == "2026-08-01T00:00:00+00:00"

    # Entry-level fidelity.
    br_entries = storage.list_entries(dst_conn, br.id)
    assert [e.header for e in br_entries] == ["战斗流程", "Bug 反馈"]
    assert br_entries[0].last_used == "2026-08-15T00:00:00+00:00"
    assert br_entries[1].comments[0]["type"] == "needs_improvement"

    # Cold-storage fidelity.
    cold_batches = storage.list_cold_batches(dst_conn)
    assert len(cold_batches) == 1
    assert cold_batches[0]["filename"] == "2026-07-01.md"
    cold_entries = storage.list_cold_entries(dst_conn, cold_batches[0]["id"])
    assert cold_entries[0]["header"] == "old header"
    assert cold_entries[0]["content"] == "old body"

    # Re-export the imported DB → the two export dirs must agree file-for-file
    # (ignoring INDEX.md, which embeds a generation timestamp).
    export_dir2 = tmp_path / "export2"
    export.export_to_directory(dst_conn, export_dir2)
    dst_conn.close()

    for rel in ["game/br.md", "uncategorized/notes.md", "cold-storage/2026-07-01.md"]:
        a = (export_dir / rel).read_text(encoding="utf-8")
        b = (export_dir2 / rel).read_text(encoding="utf-8")
        assert a == b, f"round-trip drift in {rel}\n--- first ---\n{a}\n--- second ---\n{b}"


def test_import_creates_uncategorized_for_root_md(tmp_path):
    """A .md dropped at the root should land in 'uncategorized/'."""
    in_root = tmp_path / "in"
    in_root.mkdir()
    (in_root / "loose.md").write_text(
        "---\ntitle: Loose\ndirty: false\n"
        "created: 2026-01-01T00:00:00+00:00\nupdated: 2026-01-01T00:00:00+00:00\n---\n\n"
        "## a\nA\n",
        encoding="utf-8",
    )

    conn = schema.connect(tmp_path / "db")
    stats = export.import_from_directory(conn, in_root)
    assert stats["groups"] == 1
    row = storage.get_group_by_path(conn, "uncategorized/loose.md")
    assert row is not None
    assert row.title == "Loose"
    conn.close()


def test_export_clean_removes_stale_files(tmp_path):
    """clean=True should delete category dirs from a previous DB state."""
    out = tmp_path / "out"
    # Simulate a previous export that had an 'old' category:
    (out / "old").mkdir(parents=True)
    (out / "old" / "stale.md").write_text("stale", encoding="utf-8")

    conn = _seed_conn(tmp_path / "db")
    # Force the DB to include 'old' so clean sweeps it.
    conn.execute(
        "INSERT INTO groups(path, category, slug, title, tags, dirty, created, updated) "
        "VALUES ('old/stale.md', 'old', 'stale', 'Stale', '[]', 0, "
        "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
    )
    conn.commit()

    export.export_to_directory(conn, out, clean=True)
    conn.close()

    # 'old/stale.md' rewritten from the DB (empty body since we didn't
    # add entries), but no stale sibling files remain.
    assert (out / "old" / "stale.md").exists()
    assert not (out / "old" / "orphan.md").exists()


def test_import_from_reference_plugin_shape(tmp_path):
    """Import a hand-authored file whose bytes match the reference plugin.

    This is the practical migration path: point our importer at an
    existing markdown-note-store repository and confirm every field
    lands where we claim.
    """
    in_root = tmp_path / "note-repo"
    (in_root / "productivity").mkdir(parents=True)
    (in_root / "cold-storage").mkdir(parents=True)

    (in_root / "productivity" / "workflows.md").write_text(
        "---\n"
        "title: Workflows\n"
        "tags: [work, ops]\n"
        "dirty: false\n"
        "created: 2026-05-01T00:00:00+00:00\n"
        "updated: 2026-08-10T00:00:00+00:00\n"
        "---\n"
        "\n"
        '## daily-standup {"last_used": "2026-08-01T00:00:00+00:00"}\n'
        "prepare agenda, join 10:00.\n"
        "\n"
        "## code-review\n"
        "small PRs, fast turnaround.\n",
        encoding="utf-8",
    )
    (in_root / "cold-storage" / "2026-06-15.md").write_text(
        "## archived thing\nold content\n", encoding="utf-8"
    )
    (in_root / "INDEX.md").write_text("# ignored\n## nope\n", encoding="utf-8")

    conn = schema.connect(tmp_path / "db")
    stats = export.import_from_directory(conn, in_root)
    assert stats["groups"] == 1  # INDEX.md skipped
    assert stats["entries"] == 2
    assert stats["cold_entries"] == 1

    row = storage.get_group_by_path(conn, "productivity/workflows.md")
    assert row.tags == ["work", "ops"]
    entries = storage.list_entries(conn, row.id)
    assert entries[0].header == "daily-standup"
    assert entries[0].last_used == "2026-08-01T00:00:00+00:00"
    assert entries[1].header == "code-review"
    assert entries[1].last_used is None
    conn.close()


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
