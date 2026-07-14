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


# --- dialog control (is_dialog_open / close_dialog; ticket #1041) ----------


class TestDialogControl(unittest.TestCase):
    """``is_dialog_open`` / ``close_dialog`` on Browser + the ctx pass-throughs.

    The engine's break logic lives in ``run_modal`` (see test_modal_engine.py);
    here we verify the thread-safe Browser surface and the Context
    pass-throughs: ``is_dialog_open()`` reflects ``_modal_open``, and
    ``close_dialog(value)`` arms ``_modal_force = (value,)`` (the 1-tuple, so
    ``None`` is "force-close with None", not "not armed").
    """

    def test_is_dialog_open_default_false(self):
        b = _make_browser()
        try:
            self.assertFalse(b.is_dialog_open())
            self.assertFalse(Context(b).is_dialog_open())
        finally:
            b.stop_workers()

    def test_is_dialog_open_reflects_modal_open(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            b._modal_open = True
            self.assertTrue(b.is_dialog_open())
            self.assertTrue(ctx.is_dialog_open())
            b._modal_open = False
            self.assertFalse(b.is_dialog_open())
            self.assertFalse(ctx.is_dialog_open())
        finally:
            b.stop_workers()

    def test_close_dialog_arms_force_tuple(self):
        b = _make_browser()
        try:
            self.assertIsNone(b._modal_force)
            b.close_dialog('chosen')
            self.assertEqual(b._modal_force, ('chosen',))
        finally:
            b.stop_workers()

    def test_close_dialog_default_arms_none_tuple(self):
        # The 1-tuple distinguishes "force-close with None" from "not armed":
        # close_dialog() (no value) arms (None,), not None.
        b = _make_browser()
        try:
            b.close_dialog()
            self.assertEqual(b._modal_force, (None,))
        finally:
            b.stop_workers()

    def test_ctx_close_dialog_passes_through(self):
        b = _make_browser()
        try:
            Context(b).close_dialog(42)
            self.assertEqual(b._modal_force, (42,))
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


class TestRunExternalSigint(unittest.TestCase):
    """Ctrl-C while an external command runs must not kill the session.

    While shelled out the terminal is in cooked mode, so Ctrl-C sends
    SIGINT to the whole foreground process group — the parent included
    (the child is spawned into the same group). run_external / page must
    shield the parent for the duration; what SIGINT means for the child
    is the child's own business, so it must exec with SIG_DFL.
    """

    def test_parent_survives_sigint_during_wait(self):
        # The child sends SIGINT to the parent (as the tty driver would)
        # while the parent is blocked waiting on it, then exits cleanly.
        b = _make_browser()
        try:
            ctx = Context(b)
            try:
                rc = ctx.run_external(['sh', '-c', 'kill -INT $PPID; exit 0'])
            except KeyboardInterrupt:
                self.fail('KeyboardInterrupt escaped run_external')
            self.assertEqual(rc, 0)
        finally:
            b.stop_workers()

    def test_child_execs_with_default_sigint(self):
        # An inherited SIG_IGN would make a pager un-interruptible, so
        # the shield must not leak into the child across exec: a child
        # that SIGINTs itself must die of it (rc == -SIGINT), not
        # survive to exit 0.
        import signal
        b = _make_browser()
        try:
            ctx = Context(b)
            rc = ctx.run_external(['sh', '-c', 'kill -INT $$; exit 0'])
            self.assertEqual(rc, -signal.SIGINT)
        finally:
            b.stop_workers()

    def test_parent_handler_restored_after_run(self):
        import signal
        b = _make_browser()
        try:
            ctx = Context(b)
            before = signal.getsignal(signal.SIGINT)
            ctx.run_external(['true'])
            self.assertIs(signal.getsignal(signal.SIGINT), before)
        finally:
            b.stop_workers()

    def test_page_survives_sigint_during_wait(self):
        # Same shield for the pipe-pager path. A stand-in $PAGER sends
        # SIGINT to the parent, drains stdin, exits 0 (bat/batcat lookup
        # is disabled so the $PAGER fallback runs).
        import os
        import tempfile
        b = _make_browser()
        try:
            ctx = Context(b)
            with tempfile.TemporaryDirectory() as d:
                pager = os.path.join(d, 'pager')
                with open(pager, 'w') as f:
                    f.write('#!/bin/sh\nkill -INT $PPID\n'
                            'cat >/dev/null\nexit 0\n')
                os.chmod(pager, 0o755)
                with mock.patch.object(_context.shutil, 'which',
                                       lambda _c: None), \
                     mock.patch.dict(os.environ, {'PAGER': pager}):
                    try:
                        ctx.page('hello\n')
                    except KeyboardInterrupt:
                        self.fail('KeyboardInterrupt escaped page')
            # ctx.error posts to the main queue; drain it so a surfaced
            # error would actually land in ``_notice`` before we assert.
            b.drain_main_queue()
            self.assertIsNone(b._notice)
        finally:
            b.stop_workers()


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


# --- ctx.menu threads the list-pane bounds (#1051) -------------------------


class TestMenuBoundsThreading(unittest.TestCase):
    """``ctx.menu`` derives the list-pane column span and threads it through
    to ``modal_menu`` as ``bounds`` (#1051).

    The menu leans toward screen center but clamps its footprint to the LIST
    pane's columns, so the span ``[L, R]`` must REACH placement. ``ctx.menu``
    is the one spot that knows the layout (it already computes
    ``_list_cursor_cell`` for the anchor), so it derives ``bounds`` there.
    These tests assert the derivation (``_list_pane_bounds``) and that
    ``ctx.menu`` actually forwards that exact span — by spying on the bare-name
    ``modal_menu`` the context module calls.
    """

    def _expected_bounds(self, b):
        """Independently derive the list pane's inclusive ``(L, R)`` columns.

        Re-runs ``layout_panes`` the way ``_list_pane_bounds`` does so the
        assertion checks the THREADING (the span reaches ``modal_menu``), not a
        hard-coded column pair — ``Rect.right`` is exclusive, so the last column
        is ``right - 1``.
        """
        cols, rows = _term.term_size()
        layout = _render.layout_panes(
            cols, rows, split=getattr(b, 'split', 'h'),
            show_preview=b.show_preview, list_ratio=b.list_ratio)
        lr = layout['list']
        return (lr.left, lr.right - 1)

    def test_list_pane_bounds_matches_layout(self):
        # ``_list_pane_bounds`` returns the list pane's [L, R] (R = right - 1).
        b = _make_browser(split='v', show_preview=True)   # v-split: list pane on the left
        try:
            self.assertEqual(_context._list_pane_bounds(b),
                             self._expected_bounds(b))
        finally:
            b.stop_workers()

    def test_menu_passes_list_span_as_bounds(self):
        # The end-to-end threading: a non-headless ``ctx.menu`` (anchored at the
        # list cursor) forwards the list pane's [L, R] as ``bounds``. Spy on the
        # bare-name ``modal_menu`` the context module invokes so no real dialog
        # opens; assert the captured ``bounds`` equals the independently-derived
        # list span, and that it's a left-of-center pane (so the bound differs
        # from full-screen centering — the case #1051 exists for).
        b = _make_browser(split='v', show_preview=True)
        captured = {}

        def _spy_modal_menu(browser, items, *, anchor=None, bounds=None,
                            delay_interaction=False, _read_key=None):
            captured['anchor'] = anchor
            captured['bounds'] = bounds
            return None

        orig = getattr(_context, 'modal_menu', None)
        _context.modal_menu = _spy_modal_menu
        b._headless = False     # bypass the headless short-circuit
        try:
            Context(b).menu(['a', 'b', 'c'])
        finally:
            if orig is None:
                del _context.modal_menu
            else:
                _context.modal_menu = orig
            b._headless = True
            b.stop_workers()

        expected = self._expected_bounds(b)
        self.assertEqual(captured['bounds'], expected)
        self.assertIsNotNone(captured['anchor'])   # anchored at the list cursor
        # The pane is genuinely left of screen center, so the bound is NOT the
        # whole screen — this is the regime #1051 changes behavior in.
        L, R = expected
        cols, _rows = _term.term_size()
        self.assertLess(R, cols)

    def test_centered_menu_passes_no_bounds(self):
        # With NO anchor resolvable (cursor scrolled off the list pane), the
        # menu centers and must pass ``bounds=None`` so it keeps full-screen
        # centering — the bound only applies to an anchored menu.
        b = _make_browser(split='v', show_preview=True)
        captured = {}

        def _spy_modal_menu(browser, items, *, anchor=None, bounds=None,
                            delay_interaction=False, _read_key=None):
            captured['anchor'] = anchor
            captured['bounds'] = bounds
            return None

        orig = getattr(_context, 'modal_menu', None)
        _context.modal_menu = _spy_modal_menu
        b._headless = False
        # Force ``_list_cursor_cell`` to return None: park the cursor far below
        # the list pane's visible span so ``rel`` is out of range.
        b._state.cursor = 10_000
        b._list_scroll = 0
        try:
            Context(b).menu(['a', 'b'])
        finally:
            if orig is None:
                del _context.modal_menu
            else:
                _context.modal_menu = orig
            b._headless = True
            b.stop_workers()

        self.assertIsNone(captured['anchor'])      # no anchor -> centered
        self.assertIsNone(captured['bounds'])      # and no bound


if __name__ == '__main__':
    unittest.main()
