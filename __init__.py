"""sqlite-note-store — SQLite-backed Hermes memory provider.

The plugin root doubles as a Python package when loaded by Hermes
(`_hermes_user_memory.sqlite-note-store`). The MemoryProvider surface is
`provider.SQLiteNoteStoreProvider`; `register(ctx)` wires it into Hermes.
"""

# Re-export so Hermes plugin discovery finds register() and the
# MemoryProvider subclass on the top-level module.  Package mode uses
# relative imports (Hermes); standalone/CLI/pytest mode falls back to
# absolute imports with the plugin root on sys.path.
try:
    from .provider import SQLiteNoteStoreProvider, register  # noqa: F401
except ImportError:  # top-level import (CLI, pytest collection)
    import sys
    from pathlib import Path

    _ROOT = Path(__file__).resolve().parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from provider import SQLiteNoteStoreProvider, register  # noqa: F401,E402
