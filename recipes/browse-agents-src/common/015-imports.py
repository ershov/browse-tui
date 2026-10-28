import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading

from browse_tui import (
    Action, Browser, BrowserConfig, Item, mod, recipe_argv, set_preview_op,
    upsert, visible_items,
)

# Optional md2ansi_lib plugin — colors user/assistant voice content inline;
# ``md2ansi_doc`` parses a message's inline markdown for the document subtree
# (whose feature gate is ``_md_doc`` below — md_doc hard-imports md2ansi_lib,
# so ``_md_doc`` being set implies ``_md2ansi_doc`` is too).
try:
    from md2ansi_lib import md2ansi as _md2ansi_fn, md2ansi_doc as _md2ansi_doc
    if not callable(_md2ansi_fn):
        _md2ansi_fn = None
except Exception:
    _md2ansi_fn = _md2ansi_doc = None

# Optional md_doc plugin — turns a message's markdown (its inline content and
# the ``.md`` files it references) into a navigable document subtree. The
# import is guarded exactly like ``md2ansi`` above: when it fails (the sibling
# recipe / its ``md2ansi_lib`` dep isn't importable) ``_md_doc`` is ``None`` and
# the WHOLE feature degrades to a no-op — no message row gains markdown
# children, detection never fires, and ``('md', …)`` routing is dead code.
# Every other behaviour is unchanged.
try:
    import md_doc as _md_doc
except Exception:
    _md_doc = None

# Optional md2ansi_lib internals — frame-less syntax coloring for a JSON
# tool input (the ``_json_color`` helper). Guarded exactly like the
# ``md2ansi`` import above: when md2ansi_lib (or these symbols) aren't
# importable, ``_M2A_JSON`` is None and ``_json_color`` degrades to a
# plain GRAY wrap. We colorize JSON via the JavaScript code context —
# its object/string/number lexer matches JSON closely enough.
try:
    from md2ansi_lib import (
        _md2ansi as _m2a_render,
        M2A_CONTEXT_CODE_JAVASCRIPT as _M2A_JS,
        M2A_CONTEXT_CODE_BASH as _M2A_BASH,
        M2A_DocumentState as _M2A_DocumentState,
    )
    _M2A_JSON = _m2a_render if callable(_m2a_render) else None
except Exception:
    _M2A_JSON = None
    _M2A_BASH = None

_MD_COLOR = _md2ansi_fn is not None  # default on when available
_BROWSER = None  # set in main() — used for preview-width lookup
