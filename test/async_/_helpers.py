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

# Production builds resolve these via name shadowing in the concatenated
# source; under tests we have to wire them in by hand.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake


# Re-export the names tests want.
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Pending = _state.Pending
State = _state.State
Item = _data.Item


def make_browser(**kw):
    """Construct a headless Browser with workers started.

    Tests call ``b.stop_workers()`` in tearDown to keep thread leakage
    out of subsequent tests.
    """
    kw.setdefault('_headless', True)
    b = Browser(BrowserConfig(**kw))
    b.start_workers()
    return b
