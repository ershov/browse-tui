"""Unit tests for the ``recipes/browse-ps`` context menu.

The recipe is a ``--run-py`` script that imports ``browse_tui`` (only a real
module when the binary loads it), so we stub ``browse_tui`` in ``sys.modules``
and load the extension-less recipe via ``SourceFileLoader`` — the same pattern
as ``test/unit/test_browse_git.py`` / ``test_browse_fs.py``.

The pilot context-menu convention (ticket #1033) is that the option list is a
PURE builder, ``context_menu_options(ctx)``, that inspects ``ctx.cursor`` and
returns ``(label, value)`` rows WITHOUT opening a modal. We exercise it against
a REAL headless ``Browser`` / ``Context`` (from ``test.async_._helpers``) with
a known process item under the cursor — not a fake ctx. browse-tui swallows
``on_context_menu`` exceptions and a fake ctx hides bugs, so the real
``Context.cursor`` read path is what we assert against; ``ctx.menu`` itself
short-circuits to ``None`` in headless mode, which is exactly why the builder
is split out and tested directly.
"""

import importlib.util
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from test.async_._helpers import Browser, BrowserConfig, Context, Item, make_browser


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-ps'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    The recipe pulls ``Action`` / ``Browser`` / ``BrowserConfig`` / ``Item``
    from ``browse_tui``; none are exercised by the pure builders under test
    (the cursor item comes from the REAL Browser below), so inert stubs are
    enough to let the module load. A fresh module each call keeps a stub left
    by another recipe's test from bleeding in.
    """
    mod = types.ModuleType('browse_tui')

    class _Stub:
        def __init__(self, *a, **kw):
            self._args = a
            for k, v in kw.items():
                setattr(self, k, v)

    mod.Action = _Stub
    mod.Browser = _Stub
    mod.BrowserConfig = _Stub
    mod.Item = _Stub
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-ps recipe; returns a fresh module."""
    _stub_browse_tui()
    name = '_browse_ps_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _browser_with_proc(pid=4242, title='myproc', user='alice'):
    """A real headless Browser whose cursor sits on a single process item.

    Mirrors what the recipe's ``get_children`` produces for a pid row (id is
    the int pid; ``.pid`` / ``.user`` attributes hung on the Item), so the
    builder reads a faithful cursor. The cursor is parked on the pid row via
    ``cursor_to`` after the root children settle.
    """
    item = Item(id=pid, title=title, tag=f'{user} pid={pid}',
                tag_style='dim', has_children=False)
    item.pid = pid
    item.user = user
    b = make_browser(get_children=lambda _id, *, reload=False: [item])
    b.refresh()
    b.run_until_idle()
    b.cursor_to(pid)
    b.run_until_idle()
    return b


class TestContextMenuOptions(unittest.TestCase):
    """``context_menu_options`` returns the right rows against a real cursor."""

    def setUp(self):
        self.r = _load_recipe()
        self.b = _browser_with_proc()
        self.ctx = Context(self.b)

    def tearDown(self):
        self.b.stop_workers()

    def test_cursor_is_the_real_process_item(self):
        # Sanity: the real Context resolves the parked pid row (not a stub).
        self.assertEqual(self.ctx.cursor.id, 4242)
        self.assertEqual(self.ctx.cursor.title, 'myproc')

    def test_top_level_entries_present_in_order(self):
        opts = self.r.context_menu_options(self.ctx)
        labels = [label for label, _value in opts]
        self.assertEqual(labels, [
            'Send signal…',
            'Show open files',
            'Show sockets',
            'Show environment',
            'Show full status',
            'Renice…',
            'Strace',
        ])

    def test_top_level_values_are_dispatch_tokens(self):
        # Every value token routes to a handler in the dispatch table.
        opts = self.r.context_menu_options(self.ctx)
        values = [value for _label, value in opts]
        self.assertEqual(values, [
            'signal', 'lsof', 'sockets', 'environ', 'status', 'renice', 'strace',
        ])
        for value in values:
            self.assertIn(value, self.r._MENU_ACTIONS)

    def test_no_cursor_yields_empty_list(self):
        # An empty tree → no cursor item → the hook opens nothing.
        empty = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            empty.refresh()
            empty.run_until_idle()
            ctx = Context(empty)
            self.assertIsNone(ctx.cursor)
            self.assertEqual(self.r.context_menu_options(ctx), [])
        finally:
            empty.stop_workers()

    def test_no_clipboard_or_copy_entries(self):
        # Convention (#1028): recipe menus carry no clipboard / Copy rows.
        labels = [l for l, _ in self.r.context_menu_options(self.ctx)]
        for label in labels:
            self.assertNotIn('copy', label.lower())


class TestSignalMenuOptions(unittest.TestCase):
    """The signal submenu lists the six signals; SIGTERM shows the ``(k)`` hint.

    ``signal_menu_options`` is pure and takes no ctx (the signal set is the
    same for every process), so these cases need no Browser — they assert the
    static rows + the ``_SIGNALS`` destructive flags directly.
    """

    def setUp(self):
        self.r = _load_recipe()

    def test_signal_names_and_order(self):
        opts = self.r.signal_menu_options()
        # The value half is the bare signal name; the display may add a hint.
        self.assertEqual([value for _label, value in opts],
                         ['SIGTERM', 'SIGKILL', 'SIGINT',
                          'SIGHUP', 'SIGSTOP', 'SIGCONT'])

    def test_sigterm_row_shows_k_hotkey_hint(self):
        # SIGTERM duplicates the ``k`` action, so its menu row mentions ``(k)``;
        # no other signal row carries a hotkey hint.
        rows = dict((value, label)
                    for label, value in self.r.signal_menu_options())
        self.assertEqual(rows['SIGTERM'], 'SIGTERM (k)')
        for name in ('SIGKILL', 'SIGINT', 'SIGHUP', 'SIGSTOP', 'SIGCONT'):
            self.assertEqual(rows[name], name)
            self.assertNotIn('(k)', rows[name])

    def test_destructive_signals_are_flagged(self):
        # The send path confirms iff the signal is flagged destructive: the
        # strong/disruptive ones do, SIGINT / SIGCONT do not.
        flags = {name: destructive for name, _signum, destructive in self.r._SIGNALS}
        self.assertEqual(flags, {
            'SIGTERM': True, 'SIGKILL': True, 'SIGHUP': True, 'SIGSTOP': True,
            'SIGINT': False, 'SIGCONT': False,
        })


if __name__ == '__main__':
    unittest.main()
