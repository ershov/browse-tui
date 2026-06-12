"""Streaming input (spec §3.4 / D6): on_stdin framing, pump, loop arming.

Covers the Browser-level mechanics that don't need a real terminal:

  * **Framing** (``_deliver_stdin_chunk`` / ``_end_stdin``, no fds):
    raw-chunk vs record mode, records split across chunks, empty
    records preserved, multi-char delimiters, raw-bytes mode (bytes
    data + bytes delimiter; the delimiter type must match the data
    mode — mismatches and empty delimiters are construction-time
    ``ValueError``), incremental utf-8 decoding (multibyte sequences
    split across reads, invalid bytes → U+FFFD, trailing incomplete
    sequence flushed at EOF), the EOF / error call shape (``is_eof`` /
    ``errno`` / trailing record), and the hook-exception swallow.
  * **Pump** (``_arm_stdin_stream`` / ``_pump_stdin`` against a real
    pipe on fd 0): instant EOF (the tty-stdin /dev/null shape),
    would-block with the stream staying live, the BOUNDED arming drain
    (BufferedReader read-ahead served at arming; kernel-side data left
    to the select loop and arriving on the next wake), and the
    read-error end (monkeypatched ``sys.stdin``).
  * The select-loop contract: ``run()`` passes ``aux_read_fd=0`` to
    ``read_key`` only while the stream is live, a ``'_stdin'`` wake
    pumps one chunk, after the EOF delivery the fd leaves the set
    (``read_key`` reverts to the bare call) with fd 0's blocking mode
    restored at teardown, and ``--tty -`` never arms at all.

The fd-level cases swap a pipe onto fd 0 around the call under test
(restored in ``finally``), mirroring ``test_output_channel``'s fd-1
fixture. End-to-end pipe / pty behaviour (tree growth while the UI
runs, idle CPU after EOF, the composed pre-read hand-off against the
shipped binary) lives in ``test/ui/test_stdin_channel``.
"""

import contextlib
import errno as errno_mod
import os
import sys
import types
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
# wiring as test_output_channel; trimmed to what these tests exercise.)
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


@contextlib.contextmanager
def _pipe_as_fd0(preload=b''):
    """Swap a fresh pipe's read end onto fd 0; yield the writer handle.

    The handle is a one-element list holding the write-end fd so tests
    can close it early through :func:`_end_writer` (producer departs →
    EOF) without a double-close in the cleanup. Restores the original
    fd 0 in ``finally`` — the O_NONBLOCK the arm sets lives on the
    pipe's open file description, not the saved descriptor — so a
    failing assertion can't break the runner's stdin.
    """
    r, w = os.pipe()
    if preload:
        os.write(w, preload)
    saved = os.dup(0)
    os.dup2(r, 0)
    os.close(r)
    handle = [w]
    try:
        yield handle
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        if handle[0] >= 0:
            os.close(handle[0])


def _end_writer(handle):
    """Close the pipe's write end — the app's next read sees EOF."""
    os.close(handle[0])
    handle[0] = -1


def _make_recording_browser(**kw):
    """Headless Browser with a recording ``on_stdin``; returns (b, calls)."""
    calls = []

    def hook(ctx, data, *, delimiter, is_eof, errno):
        calls.append((data, delimiter, is_eof, errno))

    kw.setdefault('_headless', True)
    b = Browser(BrowserConfig(on_stdin=hook, **kw))
    return b, calls


# ---------------------------------------------------------------------------
# Framing — record / raw / bytes modes, incremental decoding, end shape
# ---------------------------------------------------------------------------


class TestRecordFraming(unittest.TestCase):

    def test_multi_record_chunk_then_trailing_partial_at_eof(self):
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            b._deliver_stdin_chunk(b'a\nb\nc')
            self.assertEqual(calls, [('a', '\n', False, 0),
                                     ('b', '\n', False, 0)])
            b._end_stdin(0)
            self.assertEqual(calls[-1], ('c', '', True, 0))
        finally:
            b.stop_workers()

    def test_record_split_across_chunks(self):
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            b._deliver_stdin_chunk(b'ab')
            self.assertEqual(calls, [])  # nothing complete yet
            b._deliver_stdin_chunk(b'c\nd')
            self.assertEqual(calls, [('abc', '\n', False, 0)])
            b._end_stdin(0)
            self.assertEqual(calls[-1], ('d', '', True, 0))
        finally:
            b.stop_workers()

    def test_empty_records_preserved(self):
        # Spec example: "a\n\n" delivers record "a", record "", then
        # the EOF call with empty data.
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            b._deliver_stdin_chunk(b'a\n\n')
            b._end_stdin(0)
            self.assertEqual(calls, [('a', '\n', False, 0),
                                     ('', '\n', False, 0),
                                     ('', '', True, 0)])
        finally:
            b.stop_workers()

    def test_multi_char_delimiter_split_across_chunks(self):
        b, calls = _make_recording_browser(stdin_delimiter='--')
        try:
            b._deliver_stdin_chunk(b'a-')
            self.assertEqual(calls, [])  # half a delimiter is no record
            b._deliver_stdin_chunk(b'-b--')
            self.assertEqual(calls, [('a', '--', False, 0),
                                     ('b', '--', False, 0)])
            b._end_stdin(0)
            self.assertEqual(calls[-1], ('', '', True, 0))
        finally:
            b.stop_workers()

    def test_raw_bytes_mode_nul_records(self):
        # Raw-bytes mode: data AND delimiter are bytes — the delimiter
        # is configured as bytes to match the data mode.
        b, calls = _make_recording_browser(stdin_delimiter=b'\0',
                                           stdin_want_bytes=True)
        try:
            self.assertEqual(b._stdin_delim, b'\x00')
            b._deliver_stdin_chunk(b'x\x00y\x00z')
            self.assertEqual(calls, [(b'x', b'\x00', False, 0),
                                     (b'y', b'\x00', False, 0)])
            b._end_stdin(0)
            self.assertEqual(calls[-1], (b'z', b'', True, 0))
        finally:
            b.stop_workers()


class TestRawMode(unittest.TestCase):

    def test_one_call_per_chunk_with_empty_delimiter(self):
        b, calls = _make_recording_browser()
        try:
            b._deliver_stdin_chunk(b'chunk-1')
            b._deliver_stdin_chunk(b'chunk-2')
            b._end_stdin(0)
            self.assertEqual(calls, [('chunk-1', '', False, 0),
                                     ('chunk-2', '', False, 0),
                                     ('', '', True, 0)])
        finally:
            b.stop_workers()

    def test_raw_bytes_mode_passes_chunks_undecoded(self):
        b, calls = _make_recording_browser(stdin_want_bytes=True)
        try:
            b._deliver_stdin_chunk(b'\xff\xfe')  # invalid utf-8: untouched
            b._end_stdin(0)
            self.assertEqual(calls, [(b'\xff\xfe', b'', False, 0),
                                     (b'', b'', True, 0)])
        finally:
            b.stop_workers()


class TestIncrementalDecoding(unittest.TestCase):

    def test_multibyte_split_across_chunks_raw_mode(self):
        # 'é' is b'\xc3\xa9'; byte one alone decodes to nothing (no
        # call), byte two completes the character.
        b, calls = _make_recording_browser()
        try:
            b._deliver_stdin_chunk(b'\xc3')
            self.assertEqual(calls, [])
            b._deliver_stdin_chunk(b'\xa9')
            self.assertEqual(calls, [('é', '', False, 0)])
        finally:
            b.stop_workers()

    def test_multibyte_split_across_chunks_record_mode(self):
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            b._deliver_stdin_chunk(b'caf\xc3')
            b._deliver_stdin_chunk(b'\xa9\n')
            self.assertEqual(calls, [('café', '\n', False, 0)])
        finally:
            b.stop_workers()

    def test_invalid_bytes_become_replacement_chars(self):
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            b._deliver_stdin_chunk(b'\xff\n')
            self.assertEqual(calls, [('�', '\n', False, 0)])
        finally:
            b.stop_workers()

    def test_trailing_incomplete_sequence_flushed_at_eof(self):
        # A stream ending mid-multibyte-sequence: the decoder flush
        # turns the held bytes into U+FFFD on the final call.
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            b._deliver_stdin_chunk(b'a\n\xc3')
            b._end_stdin(0)
            self.assertEqual(calls, [('a', '\n', False, 0),
                                     ('�', '', True, 0)])
        finally:
            b.stop_workers()


class TestEndShapeAndHookSafety(unittest.TestCase):

    def test_error_end_carries_errno_and_trailing_record(self):
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            b._deliver_stdin_chunk(b'done\npart')
            b._end_stdin(errno_mod.EIO)
            self.assertEqual(calls, [('done', '\n', False, 0),
                                     ('part', '', True, errno_mod.EIO)])
        finally:
            b.stop_workers()

    def test_hook_exception_swallowed_and_surfaced(self):
        def hook(ctx, data, *, delimiter, is_eof, errno):
            raise ValueError('boom')

        b = Browser(BrowserConfig(on_stdin=hook, stdin_delimiter='\n',
                                  _headless=True))
        try:
            b._deliver_stdin_chunk(b'x\n')  # must not raise
            b._end_stdin(0)                 # must not raise either
            self.assertFalse(b._stdin_live)
            b.drain_main_queue()            # error() routes through post
            self.assertIn('on_stdin: ValueError: boom', b._notice.text)
        finally:
            b.stop_workers()

    def test_invalid_delimiters_rejected_at_construction(self):
        # The delimiter type must match the data mode (no implicit
        # encoding), and empty delimiters of either type are invalid —
        # raw-chunk mode is spelled None.
        cases = [
            ('', False),       # empty str (text mode)
            (b'', True),       # empty bytes (raw-bytes mode)
            (b'\n', False),    # bytes delimiter in text mode
            ('\n', True),      # str delimiter in raw-bytes mode
        ]
        for delim, raw in cases:
            with self.subTest(delimiter=delim, stdin_want_bytes=raw):
                with self.assertRaises(ValueError):
                    Browser(BrowserConfig(stdin_delimiter=delim,
                                          stdin_want_bytes=raw,
                                          _headless=True))


# ---------------------------------------------------------------------------
# Pump — arm + read against a real pipe on fd 0
# ---------------------------------------------------------------------------


class TestPumpStdin(unittest.TestCase):

    def test_instant_eof_never_joins_the_select_set(self):
        # The tty-stdin shape after fd hygiene (fd 0 → /dev/null) is the
        # same as a producer-less pipe: the arming drain reads instant
        # EOF, the EOF call fires, and the stream is over before the
        # first select.
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            with _pipe_as_fd0() as h:
                _end_writer(h)
                b._arm_stdin_stream()
                self.assertEqual(calls, [('', '', True, 0)])
                self.assertFalse(b._stdin_live)
                # Run-phase O_NONBLOCK was applied, and teardown undoes it.
                self.assertTrue(b._stdin_nonblock_set)
                self.assertFalse(os.get_blocking(0))
                b._teardown_stdin()
                self.assertTrue(os.get_blocking(0))
                self.assertFalse(b._stdin_nonblock_set)
        finally:
            b.stop_workers()

    def test_would_block_keeps_stream_live_one_chunk_per_wake(self):
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            with _pipe_as_fd0() as h:
                b._arm_stdin_stream()       # empty open pipe: no data yet
                self.assertEqual(calls, [])
                self.assertTrue(b._stdin_live)
                os.write(h[0], b'a\nb')
                b._pump_stdin()             # one wake: one chunk
                self.assertEqual(calls, [('a', '\n', False, 0)])
                b._pump_stdin()             # no new data: no-op, still live
                self.assertEqual(len(calls), 1)
                self.assertTrue(b._stdin_live)
                _end_writer(h)
                b._pump_stdin()             # EOF flushes the partial
                self.assertEqual(calls[-1], ('b', '', True, 0))
                self.assertFalse(b._stdin_live)
                b._pump_stdin()             # ended: guard makes it a no-op
                self.assertEqual(len(calls), 2)
        finally:
            b.stop_workers()

    def test_arming_serves_buffer_residue_before_kernel_data(self):
        # The composed slurp-then-stream hand-off: a bounded pre-run
        # ``sys.stdin.buffer.read`` drags the rest of the preload into
        # BufferedReader read-ahead, invisible to select. Bytes written
        # after the pre-read sit in the kernel buffer. The arming drain
        # serves the residue and then STOPS — kernel-side data wakes
        # select on its own and arrives on the next pump (per-wake reads
        # go through read1 too, so nothing is lost and order holds);
        # an unbounded drain would synchronously ingest whole files /
        # saturated pipes.
        b, calls = _make_recording_browser(stdin_delimiter='\n')
        try:
            with _pipe_as_fd0(preload=b'HDR\nrest\n') as h:
                self.assertEqual(sys.stdin.buffer.read(4), b'HDR\n')
                os.write(h[0], b'kern\n')
                b._arm_stdin_stream()
                self.assertEqual(calls, [('rest', '\n', False, 0)])
                self.assertTrue(b._stdin_live)
                b._pump_stdin()  # the '_stdin' wake the kernel data earns
                self.assertEqual(calls, [('rest', '\n', False, 0),
                                         ('kern', '\n', False, 0)])
                _end_writer(h)
                b._pump_stdin()
                self.assertEqual(calls[-1], ('', '', True, 0))
        finally:
            b.stop_workers()

    def test_read_error_ends_stream_with_numeric_errno(self):
        b, calls = _make_recording_browser(stdin_delimiter='\n')

        def raising_read1(n):
            raise OSError(errno_mod.EIO, 'I/O error')

        fake = types.SimpleNamespace(
            buffer=types.SimpleNamespace(read1=raising_read1))
        try:
            b._stdin_live = True  # as if armed; avoids touching real fd 0
            with patch('sys.stdin', fake):
                b._pump_stdin()
            self.assertEqual(calls, [('', '', True, errno_mod.EIO)])
            self.assertFalse(b._stdin_live)
        finally:
            b.stop_workers()

    def test_read_error_without_errno_falls_back_to_eio(self):
        # ``OSError`` with ``errno=None`` must still deliver a numeric
        # errno (the contract: never None / never falsy on error end).
        b, calls = _make_recording_browser()

        def raising_read1(n):
            raise OSError('no errno attached')

        fake = types.SimpleNamespace(
            buffer=types.SimpleNamespace(read1=raising_read1))
        try:
            b._stdin_live = True
            with patch('sys.stdin', fake):
                b._pump_stdin()
            self.assertEqual(calls, [('', '', True, errno_mod.EIO)])
        finally:
            b.stop_workers()


# ---------------------------------------------------------------------------
# run() select-loop integration — aux_read_fd handed to read_key only while
# the stream is live; '_stdin' triggers the pump; EOF retires the fd
# ---------------------------------------------------------------------------


class TestRunLoopStdinIntegration(unittest.TestCase):

    def test_stdin_wake_pumps_and_fd_retires_after_eof(self):
        # Non-headless run with the terminal layer stubbed out (module
        # wiring above): with the stream live the loop must call
        # read_key(aux_read_fd=0); answering '_stdin' must pump fd 0;
        # once the EOF call is delivered, read_key reverts to the bare
        # call and the teardown restores fd 0's blocking mode.
        calls = []

        def hook(ctx, data, *, delimiter, is_eof, errno):
            calls.append((data, delimiter, is_eof, errno))

        b = Browser(BrowserConfig(
            get_children=lambda _id, *, reload=False: [],
            on_stdin=hook, stdin_delimiter='\n', _headless=False))

        seen = []
        with _pipe_as_fd0() as h:
            def fake_read_key(write_fd=None, aux_read_fd=None):
                seen.append(aux_read_fd)
                if aux_read_fd == 0:
                    if h[0] >= 0:
                        os.write(h[0], b'one\n')
                        _end_writer(h)
                    return '_stdin'
                return 'q'

            original = _state.read_key
            _state.read_key = fake_read_key
            try:
                rc = b.run()
            finally:
                _state.read_key = original
            # run()'s teardown restored fd 0 (the pipe) to blocking.
            self.assertTrue(os.get_blocking(0))
        self.assertEqual(rc, 1)  # 'q' quits with the cancel code
        self.assertEqual(calls, [('one', '\n', False, 0),
                                 ('', '', True, 0)])
        self.assertEqual(
            seen, [0, 0, None],
            'fd 0 must be in the read-set exactly while the stream is '
            'live: data wake, EOF wake, then the bare call')

    def test_tty_dash_never_arms(self):
        # ``--tty -``: fd 0 IS the UI device, so the hook must never
        # arm — no O_NONBLOCK on fd 0, no fd in the read-set, no hook
        # calls; read_key stays on the bare path throughout.
        calls = []

        def hook(ctx, data, *, delimiter, is_eof, errno):
            calls.append(data)

        b = Browser(BrowserConfig(
            get_children=lambda _id, *, reload=False: [],
            on_stdin=hook, stdin_delimiter='\n', _headless=False))

        seen = []

        def fake_read_key(write_fd=None, aux_read_fd=None):
            seen.append(aux_read_fd)
            return 'q'

        original = _state.read_key
        _state.read_key = fake_read_key
        try:
            with patch.object(sys, 'argv', [sys.argv[0], '--tty', '-']):
                rc = b.run()
        finally:
            _state.read_key = original
        self.assertEqual(rc, 1)
        self.assertEqual(seen, [None])
        self.assertEqual(calls, [])
        self.assertFalse(b._stdin_live)
        self.assertFalse(b._stdin_nonblock_set)


if __name__ == '__main__':
    unittest.main()
