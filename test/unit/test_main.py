"""Tests for the main loop integration (ticket #13).

Most of the main-loop UX (cursor jiggle, key dispatch, render
side-effects) is covered by the layer-3 tmux tests in ticket #14. This
file just pins down the wiring contract:

  * ``run_tui(args)`` constructs a Browser correctly given the
    ``--root-cmd`` / ``--children-cmd`` / ``--action`` / ``--no-preview``
    surface and forwards the exit code from ``Browser.run``.
  * ``run_tui`` rejects invocations without a data source (exit 2).
  * ``Browser.run`` is callable in headless mode, drains its queues, and
    flushes ``_quit_output`` to stdout on the way out.

Cross-module symbol wiring mirrors what the concatenated build resolves
naturally — see the assignments below.
"""

import argparse
import io
import os
import sys
import unittest
from unittest.mock import patch

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')
_context = load('_browse_tui_context', '060-context.py')
_actions = load('_browse_tui_actions', '070-actions.py')
_cli = load('_browse_tui_cli', '080-cli.py')

# Inject cross-module names: production concatenates these into one
# namespace, but the test loader keeps each module isolated.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.mark_cursor_changed = _state.mark_cursor_changed
_cli.Action = _actions.Action
_cli.Browser = _state.Browser
# Action templates run as ``bash -c`` in production with a real terminal
# suspend/resume around them; the tests don't go through that path, but
# we provide stubs so a caller that does won't blow up.
_cli.term_suspend = lambda: None
_cli.term_resume = lambda: None

# For Browser.run() — inject the names it references at runtime.
_state.term_init = lambda: None
_state.term_restore = lambda: None
_state.read_key = lambda: 'q'  # tests override per-case
_state.g_resize_flag = False
_state.g_screen_lost_flag = False
_state.Context = _context.Context
_state.dispatch_key = _actions.dispatch_key
_state.render_full = lambda *a, **kw: None
_state.render_partial = lambda *a, **kw: None

# Context references back into terminal/render — wire those too so that
# the headless paths don't accidentally hit a missing symbol.
_context.term_size = _term.term_size
_context.layout_panes = _render.layout_panes
_context.move = _term.move
_context.clear_line = _term.clear_line
_context.set_style = _term.set_style
_context.write = _term.write
_context.flush = _term.flush
_context.read_key = _term.read_key
_context.term_suspend = _term.term_suspend
_context.term_resume = _term.term_resume
_context.visible_items = _state.visible_items

Browser = _state.Browser
Item = _data.Item


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**overrides):
    """Construct a fresh argparse.Namespace populated with all CLI defaults.

    ``run_tui`` reads many attributes off the Namespace; rather than
    listing them in every test, we synthesise the defaults via the real
    parser and let callers override only what they care about.
    """
    args, _ = _cli.parse_args([])
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


# ---------------------------------------------------------------------------
# run_tui — args plumbed onto Browser
# ---------------------------------------------------------------------------


class TestRunTuiArgsToBrowser(unittest.TestCase):
    """Verify run_tui assembles the right Browser from the CLI surface.

    Each test patches ``Browser.run`` (which would otherwise block on
    ``read_key``) and inspects the Browser construction afterwards.
    """

    def setUp(self):
        # Capture the most recently constructed Browser so the assertions
        # can poke at it. We wrap ``Browser`` rather than patching it so
        # ``from_flat_tree`` etc. still work.
        self._captured = []

        original_run = Browser.run
        def fake_run(self_browser):
            self._captured.append(self_browser)
            return 42
        self._patcher = patch.object(Browser, 'run', fake_run)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_root_cmd_builds_from_flat_tree(self):
        # ``--root-cmd echo`` with TSV input gives us three rows.
        args = _make_args(root_cmd='printf "a\\nb\\nc\\n"')
        rc = _cli.run_tui(args)
        self.assertEqual(rc, 42)
        self.assertEqual(len(self._captured), 1)
        b = self._captured[0]
        self.assertEqual(b.title, 'browse-tui')
        # from_flat_tree pre-populates _children for root_id (default '').
        self.assertIn('', b._state._children)
        items = b._state._children['']
        self.assertEqual([it.id for it in items], ['a', 'b', 'c'])

    def test_root_cmd_cat_reads_stdin(self):
        # The --root-cmd cat shortcut: read directly from stdin.
        fake_stdin = io.BytesIO(b'x\ny\n')
        original = sys.stdin
        try:
            sys.stdin = type('S', (), {'buffer': fake_stdin})()
            args = _make_args(root_cmd='cat')
            rc = _cli.run_tui(args)
        finally:
            sys.stdin = original
        self.assertEqual(rc, 42)
        b = self._captured[0]
        items = b._state._children['']
        self.assertEqual([it.id for it in items], ['x', 'y'])

    def test_children_cmd_builds_lazy_browser(self):
        # --children-cmd plumbs a callable that runs bash; from_flat_tree
        # is not used so _children should be empty until refresh runs.
        args = _make_args(children_cmd='printf "a\\nb\\n"')
        rc = _cli.run_tui(args)
        self.assertEqual(rc, 42)
        b = self._captured[0]
        self.assertTrue(callable(b.get_children))
        self.assertEqual(b._state._children, {})
        # Calling get_children manually returns parsed dict rows; the
        # children worker is what runs ``to_item`` on each. So the
        # lazy callback's contract is "yield dicts/strings/Items".
        result = list(b.get_children(''))
        self.assertEqual([row['id'] for row in result], ['a', 'b'])

    def test_actions_registered(self):
        args = _make_args(
            root_cmd='printf "a\\n"',
            action=['e:Edit:echo edit', 'd:Del:echo del'],
        )
        rc = _cli.run_tui(args)
        self.assertEqual(rc, 42)
        b = self._captured[0]
        keys = [a.key for a in b.actions]
        self.assertIn('e', keys)
        self.assertIn('d', keys)
        labels = {a.key: a.label for a in b.actions}
        self.assertEqual(labels['e'], 'Edit')
        self.assertEqual(labels['d'], 'Del')

    def test_no_preview_flag_propagates(self):
        args = _make_args(root_cmd='printf "a\\n"', no_preview=True)
        rc = _cli.run_tui(args)
        self.assertEqual(rc, 42)
        b = self._captured[0]
        self.assertFalse(b.show_preview)

    def test_show_ids_default_is_auto(self):
        args = _make_args(root_cmd='printf "a\\n"')
        rc = _cli.run_tui(args)
        self.assertEqual(rc, 42)
        self.assertEqual(self._captured[0].show_ids, 'auto')

    def test_show_ids_flag_propagates(self):
        for mode in ('always', 'auto', 'never'):
            with self.subTest(mode=mode):
                self._captured.clear()
                args = _make_args(root_cmd='printf "a\\n"', show_ids=mode)
                rc = _cli.run_tui(args)
                self.assertEqual(rc, 42)
                self.assertEqual(self._captured[0].show_ids, mode)

    def test_show_ids_lazy_path_propagates(self):
        # --children-cmd path also forwards the flag.
        args = _make_args(children_cmd='printf "a\\n"', show_ids='never')
        rc = _cli.run_tui(args)
        self.assertEqual(rc, 42)
        self.assertEqual(self._captured[0].show_ids, 'never')


class TestBrowserShowIdsValidation(unittest.TestCase):
    """Browser rejects invalid show_ids values at construction."""

    def test_invalid_value_raises_value_error(self):
        with self.assertRaises(ValueError):
            Browser(show_ids='sometimes', _headless=True)

    def test_default_is_auto(self):
        b = Browser(_headless=True)
        self.assertEqual(b.show_ids, 'auto')


class TestRunTuiNeedsDataSource(unittest.TestCase):
    """run_tui exits 2 with a clear error when no data source is given."""

    def test_no_source_returns_2(self):
        args = _make_args()  # neither root_cmd nor children_cmd
        buf = io.StringIO()
        with patch('sys.stderr', buf):
            rc = _cli.run_tui(args)
        self.assertEqual(rc, 2)
        self.assertIn('required', buf.getvalue())


class TestRunTuiBadAction(unittest.TestCase):
    """A malformed --action spec returns 2 with a clear error."""

    def test_bad_action_returns_2(self):
        # Two-colon format expected; one-colon should fail.
        args = _make_args(root_cmd='printf "a\\n"', action=['nope-no-colons'])
        buf = io.StringIO()
        with patch('sys.stderr', buf):
            rc = _cli.run_tui(args)
        self.assertEqual(rc, 2)
        self.assertIn('error', buf.getvalue())


# ---------------------------------------------------------------------------
# Browser.run — headless smoke
# ---------------------------------------------------------------------------


class TestBrowserRunHeadlessSmoke(unittest.TestCase):
    """Browser.run is callable in headless mode; quit/output flow works.

    Rather than driving the whole loop we exercise the immediate-quit
    path by setting ``_quit_requested=True`` before calling run, or by
    routing read_key to return 'q'. The renderer is stubbed at module
    load (see top of file) so no terminal bytes are emitted.
    """

    def test_run_exits_immediately_when_already_quit(self):
        b = Browser.from_flat_tree(['a', 'b'], _headless=True)
        b._quit_requested = True
        b._quit_code = 7
        rc = b.run()
        self.assertEqual(rc, 7)

    def test_run_quits_via_q_key(self):
        b = Browser.from_flat_tree(['a', 'b'], _headless=True)
        # default read_key returns 'q' — dispatcher fires _quit which
        # posts the quit; the loop drains and exits with code 1.
        rc = b.run()
        self.assertEqual(rc, 1)

    def test_run_flushes_quit_output(self):
        b = Browser.from_flat_tree(['only'], _headless=True)
        b._quit_requested = True
        b._quit_code = 0
        b._quit_output = 'hello\n'
        out = io.StringIO()
        with patch('sys.stdout', out):
            rc = b.run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), 'hello\n')

    def test_run_returns_zero_on_enter_print_exit(self):
        # Enter on the first row in print-exit mode should quit with 0
        # and emit the cursor's id. We feed a key sequence via a small
        # closure-backed read_key. The leading ``_notify`` keys give the
        # children worker time to repopulate the cache that ``refresh``
        # cleared at startup.
        import time
        b = Browser.from_flat_tree(['alpha', 'beta'], _headless=True)

        def fake_read_key():
            # Wait until the worker has refilled the cache (refresh()
            # at run() startup invalidates it). The headless main loop
            # doesn't include the run_until_idle wait, so we synthesise
            # one here by delaying inside read_key — that's what a real
            # blocking read_key would do anyway.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if '' in b._state._children and b._state._children['']:
                    break
                time.sleep(0.01)
            return 'enter'

        original = _state.read_key
        try:
            _state.read_key = fake_read_key
            out = io.StringIO()
            with patch('sys.stdout', out):
                rc = b.run()
        finally:
            _state.read_key = original
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), 'alpha\n')


if __name__ == '__main__':
    unittest.main()
