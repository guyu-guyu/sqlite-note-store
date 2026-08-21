"""Shared pytest fixtures for the sqlite_note_store test suite."""

from __future__ import annotations

import sys
from pathlib import Path

# Make the plugin importable in-tree without requiring `pip install -e .`
_PLUGIN_ROOT = Path(__file__).parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
