"""Shared helpers for async-test files.

The four ``test/async_/test_*`` files load the same numbered modules and
inject ``Item``/``to_item``/``notify_wake`` into the state module's
globals (the production build concatenates them, but tests load each
file in isolation -- see test/unit/_loader.py).
"""

from test.unit._loader import load

_data = load('_browse_tui_data', '030-data.py')
_term = load('_browse_tui_term', '020-terminal.py')
_state = load('_browse_tui_state', '040-state.py')
_context = load('_browse_tui_context', '060-context.py')

# Production builds resolve these via name shadowing in the concatenated
# source; under tests we have to wire them in by hand.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
# Lifecycle hooks build a Context via ``Browser._make_ctx_for_hook``; the
# concatenated build resolves the bare ``Context`` name, but the isolated
# test load has to inject it (and Context's own ``visible_items`` dep).
_state.Context = _context.Context
_context.visible_items = _state.visible_items
# The run-loop's async-dialog servicing resolves an ``anchor='slot'`` request
# (#1101) by calling ``_modal_anchor_placement`` by bare name; it lives in
# 060-context, so inject it the same way ``Context`` is.
_state._modal_anchor_placement = _context._modal_anchor_placement


# Re-export the names tests want.
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Pending = _state.Pending
State = _state.State
Item = _data.Item
Context = _context.Context
# Op constructors (+ the KEEP_PARENT sentinel) from the SAME state module the
# Browser above uses, so ops built with these interoperate with this Browser's
# ``apply_ops`` (sentinel identity matters — a separate isolated load of
# 040-state.py has its own distinct ``KEEP_PARENT``).
mod = _state.mod
upsert = _state.upsert
remove = _state.remove
KEEP_PARENT = _state.KEEP_PARENT
# Default row-format handlers (design sec A) — tests assert the hooks bind
# to these in ``Browser.__init__`` when left unset.
default_row_chrome = _state.default_row_chrome
default_row_content = _state.default_row_content
default_row = _state.default_row


def make_browser(**kw):
    """Construct a headless Browser with workers started.

    Tests call ``b.stop_workers()`` in tearDown to keep thread leakage
    out of subsequent tests.
    """
    kw.setdefault('_headless', True)
    b = Browser(BrowserConfig(**kw))
    b.start_workers()
    return b


def get_preview_text(b, id_):
    """Return the cached preview text for ``id_`` or ``None``.

    Replaces the legacy ``b._state._preview.get(id_)`` lookup now that
    preview text lives on ``Item.preview`` (ticket #422).
    """
    item = b._state._items_by_id.get(id_)
    return item.preview if item is not None else None
