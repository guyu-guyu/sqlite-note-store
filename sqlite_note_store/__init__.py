"""sqlite-note-store — SQLite-backed Hermes memory provider.

Public surface:
    from sqlite_note_store import schema, markdown_io, storage, provider

The plugin's MemoryProvider is `provider.SQLiteNoteStoreProvider`.
"""

# Re-export so Hermes plugin discovery finds register() and the
# MemoryProvider subclass on the top-level module.
from .provider import SQLiteNoteStoreProvider, register  # noqa: F401
