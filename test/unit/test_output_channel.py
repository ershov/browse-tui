"""Output channel (spec §3.2 / D7): ctx.print + FIFO buffer + delivery.

Covers the Browser-level mechanics that don't need a real terminal:

  * ``Browser.print`` / ``Context.print`` append utf-8
    (``surrogateescape``) bytes to the shared FIFO buffer, mirror builtin
    ``print`` (``end`` override, ``str()`` coercion), and no-op once the
    channel is dead.
  * ``_drain_output`` against a real pipe on fd 1: full drain, partial
    drain under backpressure (payload larger than the pipe capacity),
    and the EPIPE death contract (buffer dropped, channel permanently
    dead, no exception escapes).
  * ``_teardown_output`` delivery per stdout kind: blocking remainder +
    quit output to a pipe (prints-then-quit FIFO), the saved-fd dump +
    ``term_release_result_fd`` hand-back for a tty stdout, and the
    ``sys.stdout`` compatibility path for headless runs.
  * The select-loop contract: ``run()`` passes ``write_fd=1`` to
    ``read_key`` only while buffered output is pending, and a
    ``'_writable'`` wake drains to fd 1.

The fd-level cases swap a pipe onto fd 1 around the call under test
(restored in ``finally``), mirroring the real-fd approach of
``test_terminal_fd_hygiene`` without needing a fork. End-to-end pipe /
pty / tmux behaviour (slow consumer, EPIPE mid-session against the
shipped binary, tty scrollback) lives in ``test/ui/test_output_channel``.
"""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')
_context = load('_browse_tui_context', '060-context.py')
_actions = load('_browse_tui_actions', '070-actions.py')

# Inject cross-module names: production concatenates these into one
# namespace, but the test loader keeps each module isolated. (Same
# wiring as test_main; trimmed to what these tests exercise.)
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions._resolve_landing = _state._resolve_landing
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode

# For Browser.run() — names it resolves at runtime.
_state.term_init = lambda tty_path=None: None
_state.term_restore = lambda: None
_state.term_stdout_was_tty = _term.term_stdout_was_tty
_state.term_result_fd = _term.term_result_fd
_state.term_release_result_fd = _term.term_release_result_fd
_state.read_key = lambda: 'q'  # tests override per-case
_state.input_ready = lambda: False
_state.g_resize_flag = False
_state.g_screen_lost_flag = False
_state.Context = _context.Context
_state.dispatch_key = _actions.dispatch_key
_state.render_full = lambda *a, **kw: None
_state.render_partial = lambda *a, **kw: None
_state._layout_for = _render._layout_for
_render.term_size = _term.term_size
_render.visible_items = _state.visible_items
_context.visible_items = _state.visible_items

Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context


@contextlib.contextmanager
def _pipe_as_fd1():
    """Swap a fresh pipe's write end onto fd 1; yield the read end.

    Restores the original fd 1 (and its blocking mode, untouched — the
    O_NONBLOCK the drain sets lives on the pipe's open file description,
    not the saved descriptor) in ``finally`` so a failing assertion can't
    break the runner's stdout. The read end is closed here too unless the
    test already closed it (the EPIPE case).
    """
    r, w = os.pipe()
    saved = os.dup(1)
    os.dup2(w, 1)
    os.close(w)
    try:
        yield r
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        with contextlib.suppress(OSError):
            os.close(r)


def _drain_fd(fd):
    """Read everything currently buffered in *fd* (non-blocking)."""
    os.set_blocking(fd, False)
    out = b''
    with contextlib.suppress(OSError):
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            out += chunk
    return out


def _make_browser(**kw):
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


# ---------------------------------------------------------------------------
# Browser.print / Context.print — buffering semantics
# ---------------------------------------------------------------------------


class TestPrintBuffering(unittest.TestCase):

    def test_appends_newline_terminated_utf8(self):
        b = _make_browser()
        try:
            b.print('héllo')
            self.assertEqual(bytes(b._out_buf), 'héllo\n'.encode())
        finally:
            b.stop_workers()

    def test_end_override_and_fifo_order(self):
        b = _make_browser()
        try:
            b.print('a', end='')
            b.print('b', end='|')
            b.print('c')
            self.assertEqual(bytes(b._out_buf), b'ab|c\n')
        finally:
            b.stop_workers()

    def test_non_str_coerced_like_builtin_print(self):
        b = _make_browser()
        try:
            b.print(42)
            self.assertEqual(bytes(b._out_buf), b'42\n')
        finally:
            b.stop_workers()

    def test_surrogateescape_round_trips_raw_bytes(self):
        # Text decoded from non-utf-8 input with surrogateescape must
        # re-encode to the original bytes on the channel.
        b = _make_browser()
        try:
            b.print(b'caf\xe9'.decode('utf-8', 'surrogateescape'))
            self.assertEqual(bytes(b._out_buf), b'caf\xe9\n')
        finally:
            b.stop_workers()

    def test_dead_channel_makes_print_a_noop(self):
        b = _make_browser()
        try:
            b._out_dead = True
            b.print('lost')
            self.assertEqual(bytes(b._out_buf), b'')
        finally:
            b.stop_workers()

    def test_context_print_passes_through(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.print('via-ctx', end='!')
            self.assertEqual(bytes(b._out_buf), b'via-ctx!')
        finally:
            b.stop_workers()


# ---------------------------------------------------------------------------
# _drain_output — non-blocking drain to a real pipe on fd 1
# ---------------------------------------------------------------------------


class TestDrainOutput(unittest.TestCase):

    def _live_browser(self):
        b = _make_browser()
        b._out_stream_live = True  # what run() sets for a pipe/file stdout
        return b

    def test_full_drain_empties_buffer(self):
        b = self._live_browser()
        try:
            with _pipe_as_fd1() as r:
                b.print('one')
                b.print('two')
                b._drain_output()
                self.assertEqual(bytes(b._out_buf), b'')
                self.assertFalse(b._out_dead)
                self.assertEqual(_drain_fd(r), b'one\ntwo\n')
        finally:
            b.stop_workers()

    def test_backpressure_keeps_remainder_without_blocking(self):
        # A payload larger than any default pipe capacity (Linux: 64 KiB)
        # with a consumer that reads nothing: the drain must return with
        # the unsent remainder still buffered and the channel alive.
        payload = 'x' * (128 * 1024)
        b = self._live_browser()
        try:
            with _pipe_as_fd1() as r:
                b.print(payload, end='')
                b._drain_output()
                self.assertTrue(b._out_buf, 'remainder must stay buffered')
                self.assertFalse(b._out_dead)
                # Consumer catches up -> the next drain finishes the job.
                got = _drain_fd(r)
                b._drain_output()
                got += _drain_fd(r)
                self.assertEqual(bytes(b._out_buf), b'')
                self.assertEqual(got, payload.encode())
        finally:
            b.stop_workers()

    def test_epipe_kills_channel_permanently(self):
        b = self._live_browser()
        try:
            with _pipe_as_fd1() as r:
                b.print('doomed')
                os.close(r)  # consumer goes away
                b._drain_output()  # EPIPE — must not raise
                self.assertTrue(b._out_dead)
                self.assertEqual(bytes(b._out_buf), b'')
                # Subsequent prints no-op; a later drain is a no-op too.
                b.print('after-death')
                self.assertEqual(bytes(b._out_buf), b'')
                b._drain_output()
        finally:
            b.stop_workers()


# ---------------------------------------------------------------------------
# _teardown_output — delivery per stdout kind
# ---------------------------------------------------------------------------


class TestTeardownOutputPipe(unittest.TestCase):

    def test_remainder_then_quit_output_fifo(self):
        b = _make_browser()
        b._out_stream_live = True
        b._quit_output = 'QUIT\n'
        try:
            with _pipe_as_fd1() as r:
                b.print('early')
                b._drain_output()           # streams 'early' live
                b.print('late')             # still buffered at teardown
                b._teardown_output()
                self.assertEqual(_drain_fd(r), b'early\nlate\nQUIT\n')
                # The drain's O_NONBLOCK is undone for the blocking write.
                self.assertTrue(os.get_blocking(1))
                self.assertFalse(b._out_nonblock_set)
        finally:
            b.stop_workers()

    def test_dead_channel_skips_quit_output_cleanly(self):
        b = _make_browser()
        b._out_stream_live = True
        b._quit_output = 'never-delivered\n'
        try:
            with _pipe_as_fd1() as r:
                b.print('x')
                os.close(r)
                b._drain_output()           # dies on EPIPE
                self.assertTrue(b._out_dead)
                b._teardown_output()        # must not raise / write
        finally:
            b.stop_workers()


class TestTeardownOutputTty(unittest.TestCase):
    """The held (tty-stdout) branch, with a pipe standing in for the
    saved real-stdout fd: the dump goes to ``term_result_fd()`` in FIFO
    order and the fd is handed back via ``term_release_result_fd``."""

    def test_dump_to_saved_fd_then_release(self):
        r, w = os.pipe()
        b = _make_browser()
        b._quit_output = 'selection\n'
        # Simulate the fd hygiene a tty stdout gets at term_init.
        _term._stdout_was_tty = True
        _term._saved_stdout_fd = w
        try:
            b.print('print-1')
            b.print('print-2')
            b._teardown_output()
            self.assertEqual(_drain_fd(r), b'print-1\nprint-2\nselection\n')
            # Saved fd closed + hygiene state reset by the release.
            with self.assertRaises(OSError):
                os.fstat(w)
            self.assertFalse(_term.term_stdout_was_tty())
            self.assertEqual(_term.term_result_fd(), 1)
        finally:
            _term._stdout_was_tty = False
            _term._saved_stdout_fd = -1
            with contextlib.suppress(OSError):
                os.close(w)
            os.close(r)
            b.stop_workers()


class TestHeadlessTeardownFlush(unittest.TestCase):
    """Headless run(): prints + quit output flush via ``sys.stdout``
    (StringIO-patchable), prints first — same path ``--tty -`` takes."""

    def test_prints_then_quit_output_via_sys_stdout(self):
        b = Browser.from_flat_tree(['only'], _headless=True)
        b.print('first')
        b.print('second')
        b._quit_requested = True
        b._quit_code = 0
        b._quit_output = 'chosen\n'
        out = io.StringIO()
        with patch('sys.stdout', out):
            rc = b.run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), 'first\nsecond\nchosen\n')

    def test_on_quit_hook_print_still_delivered(self):
        # on_quit fires after terminal restore, before the teardown
        # flush — a print from it must still reach the channel, ahead
        # of the quit output.
        def hook(ctx, code):
            ctx.print('from-hook')

        b = _make_browser(get_children=lambda *a, **kw: [], on_quit=hook)
        b._quit_requested = True
        b._quit_output = 'bye\n'
        out = io.StringIO()
        with patch('sys.stdout', out):
            rc = b.run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), 'from-hook\nbye\n')


# ---------------------------------------------------------------------------
# run() select-loop integration — write_fd handed to read_key only while
# output is pending; '_writable' triggers the drain
# ---------------------------------------------------------------------------


class TestRunLoopDrainIntegration(unittest.TestCase):

    def test_writable_wake_drains_pending_output(self):
        # Non-headless run with the terminal layer stubbed out (module
        # wiring above): stdout is "a pipe" (fd 1 swapped below), so
        # run() arms _out_stream_live. With output buffered, the loop
        # must call read_key(write_fd=1); answering '_writable' must
        # drain to fd 1; once empty, read_key reverts to the bare call.
        b = Browser.from_flat_tree(['a'], _headless=False)
        b.print('pending')

        calls = []

        def fake_read_key(write_fd=None, aux_read_fd=None):
            calls.append(write_fd)
            if write_fd is not None:
                return '_writable'
            return 'q'

        original = _state.read_key
        _state.read_key = fake_read_key
        try:
            with _pipe_as_fd1() as r:
                rc = b.run()
                streamed = _drain_fd(r)
        finally:
            _state.read_key = original
        self.assertEqual(rc, 1)  # 'q' quits with the cancel code
        self.assertEqual(streamed, b'pending\n')
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0], 1,
                         'pending output must put fd 1 in the write-set')
        self.assertIsNone(calls[1],
                          'an empty buffer must leave the select set as-is')


if __name__ == '__main__':
    unittest.main()
