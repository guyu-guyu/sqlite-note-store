"""CLI smoke test — exercise `python -m sqlite_note_store` end-to-end."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "sqlite_note_store", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or PLUGIN_ROOT),
        env={"PYTHONPATH": str(PLUGIN_ROOT), "PATH": "/usr/bin:/bin"},
        check=False,
    )


def _seed_markdown_dir(root: Path) -> None:
    """Create a minimal markdown-note-store-shaped directory for import."""
    cat = root / "game"
    cat.mkdir(parents=True)
    (cat / "battle.md").write_text(
        "---\n"
        "title: Battle Flow\n"
        "tags: [game, br]\n"
        "dirty: false\n"
        "created: 2026-08-01T00:00:00+00:00\n"
        "updated: 2026-08-20T00:00:00+00:00\n"
        "---\n\n"
        "## Draw phase {last_used: 2026-08-20T00:00:00+00:00}\n\n"
        "player draws 5.\n",
        encoding="utf-8",
    )


def test_cli_import_then_status_then_export(tmp_path):
    """end-to-end migration: md dir → SQLite → md dir round-trip via CLI."""
    src = tmp_path / "src-notes"
    _seed_markdown_dir(src)
    db_root = tmp_path / "db-root"

    imp = _run("--db-root", str(db_root), "import", str(src), "--replace")
    assert imp.returncode == 0, imp.stderr
    assert "files=1" in imp.stdout
    assert "entries=1" in imp.stdout

    status = _run("--db-root", str(db_root), "status")
    assert status.returncode == 0
    assert "1 active" in status.stdout

    out = tmp_path / "exported"
    exp = _run("--db-root", str(db_root), "export", str(out), "--clean")
    assert exp.returncode == 0, exp.stderr
    assert (out / "game" / "battle.md").exists()

    original = (src / "game" / "battle.md").read_text(encoding="utf-8")
    exported = (out / "game" / "battle.md").read_text(encoding="utf-8")
    # Round-trip preserves the essentials: title, entries, last_used
    assert "Battle Flow" in exported
    assert "Draw phase" in exported
    assert "player draws 5" in exported


def test_cli_status_on_missing_db_reports_and_exits_nonzero(tmp_path):
    result = _run("--db-root", str(tmp_path / "nope"), "status")
    assert result.returncode == 1
    assert "no SQLite store" in result.stdout


def test_cli_import_missing_dir_errors(tmp_path):
    result = _run("--db-root", str(tmp_path / "db"), "import", str(tmp_path / "nope"))
    assert result.returncode == 2
    assert "source directory not found" in result.stderr
