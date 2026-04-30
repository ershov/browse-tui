"""Helper to load numbered ``src-tui/`` modules into tests by file path.

The numbered-file source layout (``030-data.py``, ``080-cli.py``, …) is not
importable as Python packages, so each test file loads what it needs via
``importlib.util.spec_from_file_location``. This helper centralises that
incantation so subsequent test files (parsers, state, render, …) don't each
re-derive it.
"""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / 'src-tui'


def load(stem, filename):
    """Load ``src-tui/<filename>`` as a module named ``stem``."""
    spec = importlib.util.spec_from_file_location(stem, _SRC / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
