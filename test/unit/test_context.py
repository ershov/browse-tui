"""Tests for the Context wrapper (060-context.py).

Context is the surface action handlers see — a thin wrapper around
Browser that adds main-thread-only sub-flows. These unit tests exercise
the selection helpers (cursor / selected / targets), the thread-safe
pass-throughs (refresh / message / error), and the headless-mode
contract for run_external / input / confirm.

The full TTY paths for input, confirm, run_external (suspending the
real terminal) are deferred to ticket #14's UI tests.
"""

import unittest
from unittest import mock

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_context = load('_browse_tui_context', '060-context.py')

# Production builds resolve these via concatenation; under tests we
# wire them in by hand.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_context.visible_items = _state.visible_items
# Terminal helpers are only used in non-headless paths but we still
# inject them so the module loads cleanly.
_context.term_suspend = _term.term_suspend
_context.term_resume = _term.term_resume
_context.term_size = _term.term_size
_context.move = _term.move
_context.clear_line = _term.clear_line
_context.clear_columns = _term.clear_columns
_context.set_style = _term.set_style
_context.reset_style = _term.reset_style
_context.write = _term.write
_context.flush = _term.flush
_context.read_key = _term.read_key
# layout_panes lives in 050-render.py; load and inject if needed.
_render = load('_browse_tui_render', '050-render.py')
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
_context.layout_panes = _render.layout_panes


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Pending = _state.Pending
Context = _context.Context


def _make_browser(**kw):
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


# --- Construction ---------------------------------------------------------


class TestContextConstruction(unittest.TestCase):

    def test_stores_browser_reference(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIs(ctx._browser, b)
        finally:
            b.stop_workers()


# --- cursor property ------------------------------------------------------


class TestCursorProperty(unittest.TestCase):

    def test_empty_visible_list_returns_none(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.cursor)
        finally:
            b.stop_workers()

    def test_normal_entry_returns_item(self):
        b = _make_browser()
        items = [Item(id='A'), Item(id='B')]
        b._state._children[None] = items
        b._state.cursor = 1
        try:
            ctx = Context(b)
            self.assertIs(ctx.cursor, items[1])
        finally:
            b.stop_workers()

    def test_pending_placeholder_yields_none(self):
        # Expanded-but-uncached parent gives a 'pending' kind row.
        b = _make_browser()
        b._state._children[None] = [Item(id='A', has_children=True)]
        b._state.expanded.add('A')
        # Visible list now has [normal A, pending placeholder].
        b._state.cursor = 1  # placeholder
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.cursor)
        finally:
            b.stop_workers()


# --- selected property ----------------------------------------------------


class TestSelectedProperty(unittest.TestCase):

    def test_empty_selection_returns_empty_list(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertEqual(ctx.selected, [])
        finally:
            b.stop_workers()

    def test_selection_with_visible_items(self):
        b = _make_browser()
        items = [Item(id='A'), Item(id='B'), Item(id='C')]
        b._state._children[None] = items
        b._state.selected = {'A', 'C'}
        try:
            ctx = Context(b)
            sel = ctx.selected
            self.assertEqual({it.id for it in sel}, {'A', 'C'})
        finally:
            b.stop_workers()

    def test_selection_in_cache_but_not_visible(self):
        # Item 'X' is cached as a child of 'P' but 'P' is not expanded,
        # so 'X' isn't in the visible list — selected should still surface
        # it from the cache.
        b = _make_browser()
        b._state._children[None] = [Item(id='P', has_children=True)]
        b._state._children['P'] = [Item(id='X')]
        b._state.selected = {'X'}
        try:
            ctx = Context(b)
            sel = ctx.selected
            self.assertEqual([it.id for it in sel], ['X'])
        finally:
            b.stop_workers()


# --- targets property -----------------------------------------------------


class TestTargetsProperty(unittest.TestCase):

    def test_empty_no_cursor_returns_empty(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertEqual(ctx.targets, [])
        finally:
            b.stop_workers()

    def test_no_selection_returns_cursor(self):
        b = _make_browser()
        items = [Item(id='A')]
        b._state._children[None] = items
        try:
            ctx = Context(b)
            t = ctx.targets
            self.assertEqual(len(t), 1)
            self.assertIs(t[0], items[0])
        finally:
            b.stop_workers()

    def test_selection_overrides_cursor(self):
        b = _make_browser()
        items = [Item(id='A'), Item(id='B')]
        b._state._children[None] = items
        b._state.cursor = 1  # cursor on B
        b._state.selected = {'A'}
        try:
            ctx = Context(b)
            t = ctx.targets
            self.assertEqual([it.id for it in t], ['A'])
        finally:
            b.stop_workers()


# --- pass-through ---------------------------------------------------------


class TestPassThrough(unittest.TestCase):

    def test_refresh_returns_pending(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            p = ctx.refresh('id-x')
            self.assertIsInstance(p, Pending)
        finally:
            b.stop_workers()

    def test_flash_sets_flash_notice(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.flash('hello')
            b.drain_main_queue()
            self.assertIsNotNone(b._notice)
            self.assertEqual(b._notice.text, 'hello')
            self.assertEqual(b._notice.kind, 'flash')
        finally:
            b.stop_workers()

    def test_error_sets_error_notice_and_logs(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.error('bad')
            b.drain_main_queue()
            self.assertIsNotNone(b._notice)
            self.assertEqual(b._notice.text, 'bad')
            self.assertEqual(b._notice.kind, 'error')
            self.assertEqual(len(b._log), 1)
            self.assertTrue(b._log[-1].endswith('bad'))
        finally:
            b.stop_workers()

    def test_log_appends_silently(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.log('note')
            b.drain_main_queue()
            self.assertIsNone(b._notice)
            self.assertEqual(len(b._log), 1)
            self.assertTrue(b._log[-1].endswith('note'))
        finally:
            b.stop_workers()

    def test_quit_sets_fields(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.quit(code=3, output='gone')
            b.drain_main_queue()
            self.assertTrue(b._quit_requested)
            self.assertEqual(b._quit_code, 3)
            self.assertEqual(b._quit_output, 'gone')
        finally:
            b.stop_workers()

    def test_select_calls_through(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.select(['a', 'b'])
            b.drain_main_queue()
            self.assertEqual(b._state.selected, {'a', 'b'})
        finally:
            b.stop_workers()


# --- flash / log / error notice primitives (Browser-level) ----------------


class TestNoticePrimitives(unittest.TestCase):

    def test_flash_headless_arms_no_timer(self):
        b = _make_browser()
        try:
            b.flash('hi')
            b.drain_main_queue()
            self.assertEqual(b._notice.kind, 'flash')
            self.assertIsNone(b._flash_timer)
        finally:
            b.stop_workers()

    def test_flash_non_headless_arms_timer(self):
        b = _make_browser()
        # Flip the headless flag — arming a threading.Timer touches no
        # terminal, so this is safe without a real TTY. Cancel it right
        # away so the fire callback never runs during the test.
        b._headless = False
        try:
            b.flash('hi')
            b.drain_main_queue()
            self.assertIsNotNone(b._flash_timer)
        finally:
            if b._flash_timer is not None:
                b._flash_timer.cancel()
            b.stop_workers()

    def test_flash_log_true_appends(self):
        b = _make_browser()
        try:
            b.flash('side effect', log=True)
            b.drain_main_queue()
            self.assertEqual(b._notice.kind, 'flash')
            self.assertEqual(len(b._log), 1)
            self.assertTrue(b._log[-1].endswith('side effect'))
        finally:
            b.stop_workers()

    def test_flash_default_does_not_log(self):
        b = _make_browser()
        try:
            b.flash('ack')
            b.drain_main_queue()
            self.assertEqual(len(b._log), 0)
        finally:
            b.stop_workers()

    def test_log_sets_no_notice_and_no_redraw(self):
        b = _make_browser()
        try:
            b._needs_redraw.clear()
            b.log('note')
            b.drain_main_queue()
            self.assertIsNone(b._notice)
            self.assertEqual(b._needs_redraw, set())
            self.assertEqual(len(b._log), 1)
        finally:
            b.stop_workers()

    def test_error_always_logs_and_no_timer(self):
        b = _make_browser()
        try:
            b.error('boom')
            b.drain_main_queue()
            self.assertEqual(b._notice.kind, 'error')
            self.assertEqual(len(b._log), 1)
            self.assertTrue(b._log[-1].endswith('boom'))
            self.assertIsNone(b._flash_timer)
        finally:
            b.stop_workers()

    def test_log_entry_has_timestamp_prefix(self):
        b = _make_browser()
        try:
            b.log('payload')
            b.drain_main_queue()
            entry = b._log[-1]
            # "HH:MM:SS  payload" — a time prefix then two spaces.
            self.assertRegex(entry, r'^\d\d:\d\d:\d\d  payload$')
        finally:
            b.stop_workers()

    def test_ring_buffer_caps_at_maxlen(self):
        b = _make_browser()
        try:
            for i in range(_state._LOG_MAXLEN + 50):
                b.log(f'line {i}')
            b.drain_main_queue()
            self.assertEqual(len(b._log), _state._LOG_MAXLEN)
            # Oldest entries dropped; newest retained.
            self.assertTrue(b._log[-1].endswith(
                f'line {_state._LOG_MAXLEN + 49}'))
            self.assertTrue(b._log[0].endswith('line 50'))
        finally:
            b.stop_workers()

    def test_last_write_wins_single_slot(self):
        b = _make_browser()
        try:
            b.flash('first')
            b.error('second')
            b.drain_main_queue()
            self.assertEqual(b._notice.kind, 'error')
            self.assertEqual(b._notice.text, 'second')
            # Each set bumped the sequence.
            self.assertGreater(b._notice.seq, 0)
        finally:
            b.stop_workers()

    def test_stale_seq_timer_does_not_clear_newer_notice(self):
        b = _make_browser()
        try:
            b.flash('old')
            b.drain_main_queue()
            old_seq = b._notice.seq
            b.flash('new')
            b.drain_main_queue()
            new_seq = b._notice.seq
            self.assertNotEqual(old_seq, new_seq)
            # Stale timer fires for the old seq — must NOT clear the
            # newer notice.
            b._clear_notice_if_seq(old_seq)
            self.assertIsNotNone(b._notice)
            self.assertEqual(b._notice.text, 'new')
            # The matching-seq clear does remove it and flags a redraw.
            b._needs_redraw.clear()
            b._clear_notice_if_seq(new_seq)
            self.assertIsNone(b._notice)
            self.assertIn('info', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_stop_workers_cancels_flash_timer(self):
        b = _make_browser()
        b._headless = False
        b.flash('hi')
        b.drain_main_queue()
        timer = b._flash_timer
        self.assertIsNotNone(timer)
        b.stop_workers()
        # Timer cancelled (its ``finished`` event is set, so the action
        # never runs) and the slot is cleared.
        self.assertTrue(timer.finished.is_set())
        self.assertIsNone(b._flash_timer)


# --- run_external (headless) ----------------------------------------------


class TestRunExternalHeadless(unittest.TestCase):

    def test_echo_returns_zero(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            rc = ctx.run_external(['true'])
            self.assertEqual(rc, 0)
            # Headless: no terminal suspend/resume, but redraw still flagged.
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_failing_command_returns_nonzero(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            rc = ctx.run_external(['false'])
            self.assertNotEqual(rc, 0)
        finally:
            b.stop_workers()


class TestRunExternalStdinText(unittest.TestCase):
    """run_external feeds ``stdin_text`` to the child via a real pipe."""

    def _run_capturing_stdin(self, text):
        import os as _os
        import tempfile as _tf
        b = _make_browser()   # headless: no terminal, child inherits test fds
        try:
            ctx = Context(b)
            with _tf.TemporaryDirectory() as d:
                out = _os.path.join(d, 'out')
                # 'sh -c "cat > $1" sh OUT' writes the child's stdin to OUT and
                # nothing to stdout — so we read back exactly what was piped.
                rc = ctx.run_external(['sh', '-c', 'cat > "$1"', 'sh', out],
                                      stdin_text=text)
                with open(out, encoding='utf-8') as f:
                    return rc, f.read()
        finally:
            b.stop_workers()

    def test_stdin_text_reaches_child(self):
        rc, got = self._run_capturing_stdin('# Doc\nbody\n')
        self.assertEqual(rc, 0)
        self.assertEqual(got, '# Doc\nbody\n')

    def test_large_stdin_text_no_e2big(self):
        # Regression for the launcher's E2BIG bug: a document well over the
        # 128 KB MAX_ARG_STRLEN limit goes through fine on the pipe (it would
        # have failed had it ridden argv or the environment).
        big = 'lorem ipsum ' * 60_000   # ~720 KB
        rc, got = self._run_capturing_stdin(big)
        self.assertEqual(rc, 0)
        self.assertEqual(got, big)


class TestRunExternalKeepScreen(unittest.TestCase):
    """run_external threads ``keep_screen`` through to ``term_suspend``."""

    def _suspend_kwarg(self, **run_external_kw):
        # Exercise the non-headless path with the terminal handoff stubbed:
        # capture what keep_screen term_suspend is called with.
        b = _make_browser()
        b._headless = False
        captured = {}
        with mock.patch.object(
                _context, 'term_suspend',
                lambda keep_screen=False: captured.__setitem__('ks', keep_screen)), \
             mock.patch.object(_context, 'term_resume', lambda: None), \
             mock.patch.object(_context, 'term_child_fds',
                               lambda: (None, None), create=True), \
             mock.patch.object(_context.subprocess, 'run',
                               return_value=mock.Mock(returncode=0)):
            try:
                rc = ctx_rc = Context(b).run_external(['true'], **run_external_kw)
            finally:
                b._headless = True
                b.stop_workers()
        self.assertEqual(ctx_rc, 0)
        return captured['ks']

    def test_keep_screen_true_threads_through(self):
        self.assertTrue(self._suspend_kwarg(keep_screen=True))

    def test_default_is_false(self):
        self.assertFalse(self._suspend_kwarg())


class TestAltScreenFlag(unittest.TestCase):
    """The --alt-screen / --no-alt-screen flag pair: strip + resolution."""

    def test_recipe_argv_strips_both_forms(self):
        argv = ['--no-alt-screen', 'a.md', '--tty', '/dev/tty',
                '--alt-screen', 'b.md']
        # Framework flags (both alt-screen forms + --tty VALUE) dropped;
        # the recipe's own positionals are left in order.
        self.assertEqual(_state.recipe_argv(argv), ['a.md', 'b.md'])

    def test_resolve_defaults_to_config(self):
        self.assertTrue(_state._resolve_alt_screen(True, []))
        self.assertFalse(_state._resolve_alt_screen(False, []))

    def test_resolve_flag_overrides_config(self):
        self.assertFalse(_state._resolve_alt_screen(True, ['--no-alt-screen']))
        self.assertTrue(_state._resolve_alt_screen(False, ['--alt-screen']))

    def test_resolve_last_occurrence_wins(self):
        self.assertTrue(_state._resolve_alt_screen(
            True, ['--no-alt-screen', '--alt-screen']))
        self.assertFalse(_state._resolve_alt_screen(
            False, ['--alt-screen', '--no-alt-screen']))


class TestQuitOnScopeUpFlag(unittest.TestCase):
    """--quit-on-scope-up / --no-quit-on-scope-up flag pair: strip + resolution."""

    def test_recipe_argv_strips_both_forms(self):
        argv = ['--no-quit-on-scope-up', 'a.md', '--tty', '/dev/tty',
                '--quit-on-scope-up', 'b.md']
        # Framework flags (both quit-on-scope-up forms + --tty VALUE) dropped;
        # the recipe's own positionals are left in order.
        self.assertEqual(_state.recipe_argv(argv), ['a.md', 'b.md'])

    def test_resolve_defaults_to_config(self):
        self.assertTrue(_state._resolve_quit_on_scope_up(True, []))
        self.assertFalse(_state._resolve_quit_on_scope_up(False, []))

    def test_resolve_flag_overrides_config(self):
        self.assertFalse(_state._resolve_quit_on_scope_up(
            True, ['--no-quit-on-scope-up']))
        self.assertTrue(_state._resolve_quit_on_scope_up(
            False, ['--quit-on-scope-up']))

    def test_resolve_last_occurrence_wins(self):
        self.assertTrue(_state._resolve_quit_on_scope_up(
            True, ['--no-quit-on-scope-up', '--quit-on-scope-up']))
        self.assertFalse(_state._resolve_quit_on_scope_up(
            False, ['--quit-on-scope-up', '--no-quit-on-scope-up']))


# --- input (headless) -----------------------------------------------------


class TestInputHeadless(unittest.TestCase):

    def test_returns_default_in_headless(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertEqual(ctx.input('Name? ', default='alice'), 'alice')
        finally:
            b.stop_workers()

    def test_returns_empty_default_when_omitted(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertEqual(ctx.input('Name? '), '')
        finally:
            b.stop_workers()


# --- confirm (headless) ---------------------------------------------------


class TestConfirmHeadless(unittest.TestCase):

    def test_returns_none_in_headless(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            # Contract change: confirm now returns the chosen label or None;
            # headless yields None (the no-open, safe-default outcome).
            self.assertIsNone(ctx.confirm('proceed?'))
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
