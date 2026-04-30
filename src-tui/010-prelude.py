#!/usr/bin/env python3
"""browse-tui — generic hierarchical browser TUI.

Single-file binary built by concatenating the numbered ``src-tui/*.py``
files via ``build-tui.sh``. The runtime target is Python 3.10+ (PEP 604
union syntax and built-in generics like ``list[int]`` are used throughout).

Public API: see ``docs/api.md``. Entry point: ``main`` (in ``080-cli.py``),
invoked from ``090-main.py``.
"""

__version__ = '0.1.0'
