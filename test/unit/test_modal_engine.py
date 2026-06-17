"""Tests for the modal engine — ``run_modal`` (ticket #970).

The engine owns the shared modal lifecycle: geometry at open, painting the
frame through a PRIVATE row cache, the nested read-key loop with the same
channel/background event handling as the main loop, the uniform cancel, and
the cache-poison restore that lets the regular UI repaint flicker-free on
close.

No TTY: an injected ``_read_key`` feeds a deterministic key list (the
zero-arg scripted-callable pattern from ``test_pick.py``), and the modal
module's terminal write helpers are swapped for a StringIO capture (the
``_StdoutCapture`` pattern from ``test_terminal_row_shim.py``). The browser
is a minimal fake exposing only the attrs/methods the loop touches.

``Rect`` / ``PaneCache`` / the terminal primitives live in other numbered
files and are referenced by the modal module by bare name; the per-file
test loader wires them in the same way the concatenated build does.
"""

import io
import types
import unittest

from test.unit._loader import load


_term = load('_browse_tui_term_modaleng', '020-terminal.py')
_state = load('_browse_tui_state_modaleng', '040-state.py')
_render = load('_browse_tui_render_modaleng', '050-render.py')
_modal = load('_browse_tui_modal_engine', '055-modal.py')

# Wire the modal module's bare-name cross-references the way the
# concatenated build's shared namespace does.
# ``_truncate_by_cells`` lives in 050-render and itself calls ``_char_width``
# (defined in 020-terminal) by bare name; in the concatenated build both
# share a namespace, so the isolated load has to wire ``_char_width`` into
# the render module before the modal engine invokes it.
_render._char_width = _term._char_width

_modal.Rect = _render.Rect
_modal.PaneCache = _state.PaneCache
_modal._truncate_by_cells = _render._truncate_by_cells
_modal.begin_row = _term.begin_row
_modal.end_row = _term.end_row
_modal.begin_sync = _term.begin_sync
_modal.end_sync = _term.end_sync
_modal.flush = _term.flush
_modal.set_style = _term.set_style
_modal.reset_style = _term.reset_style
_modal.write = _term.write
_modal.move = _term.move
_modal.read_key = _term.read_key
_modal.term_size = _term.term_size
# Delay-interaction defaults (ticket #971): the engine reads ``time`` and
# ``input_ready`` as bare names when the per-call seams aren't injected.
# Wire both so the production-default path (e.g. delay_interaction=False
# tests that pass no seams) resolves them.
_modal.input_ready = _term.input_ready
import time as _time  # noqa: E402  (after the loader wiring block)
_modal.time = _time


Rect = _render.Rect
PaneCache = _state.PaneCache
run_modal = _modal.run_modal


# --- Output capture --------------------------------------------------------
#
# The modal module emits through its own ``write`` (and helpers route there).
# ``begin_row`` / ``end_row`` capture per-row writes and emit on a cache miss
# via the SAME ``_term.write`` (the modal module's ``write`` IS
# ``_term.write`` after wiring). So pointing ``_term._tty_writer`` at a
# StringIO captures the full emitted byte stream — exactly the
# test_terminal_row_shim pattern.


class _Capture:
    def __enter__(self):
        self._orig = _term._tty_writer
        self.buf = io.StringIO()
        _term._tty_writer = self.buf
        # Defensive: ensure no stale row capture leaks across tests.
        _term._row_capture_active = False
        _term._row_buf = []
        _term._row_meta = None
        return self

    def __exit__(self, *_):
        _term._tty_writer = self._orig

    @property
    def text(self):
        return self.buf.getvalue()


def _scripted(keys):
    """Zero-arg callable yielding successive keys (the picker's seam)."""
    it = iter(keys)
    return lambda: next(it)


class _KeySource:
    """A controllable key source for delay-interaction tests.

    Backs BOTH the engine's ``_read_key`` (via :meth:`read`) and its
    open-time drain poll ``_input_ready`` (via :meth:`pending`). ``drain``
    keys are the ones the open-time drain should eat (``pending()`` reports
    True while any remain); ``loop`` keys are what the read loop then sees.
    Records every key actually read so a test can assert what got drained
    vs. dispatched.
    """

    def __init__(self, drain=(), loop=()):
        self._drain = list(drain)
        self._loop = list(loop)
        self.read_log = []

    def pending(self):
        # Zero-arg poll: True iff the open-time drain still has keys to eat.
        return bool(self._drain)

    def read(self):
        # Zero-arg read (the injected-seam contract): serve drain keys
        # first, then loop keys.
        key = self._drain.pop(0) if self._drain else self._loop.pop(0)
        self.read_log.append(key)
        return key


class _FakeClock:
    """A controllable monotonic clock — a zero-arg callable returning secs.

    Starts at ``start`` and returns the current value WITHOUT advancing, so
    tests step it explicitly via :meth:`advance`. Deterministic, no sleeps.
    """

    def __init__(self, start=0.0):
        self.t = start

    def advance(self, dt):
        self.t += dt

    def __call__(self):
        return self.t


# --- Stub content ----------------------------------------------------------


class _StubContent:
    """Minimal content object implementing the duck-typed protocol.

    Records ``handle_key`` / ``draw_row`` calls so tests can assert what the
    engine routed where. ``measure`` reports a fixed small size (clamped by
    the engine to the caps). ``key_handler`` maps a key to ``(done, result)``;
    unmapped keys continue the loop. ``raise_on`` makes ``handle_key`` raise
    for a given key (to exercise the restore-on-exception path).
    ``measure_raises`` makes the FIRST ``measure`` call raise (to exercise
    the open-time failure path).
    """

    def __init__(self, *, title='Stub', w=10, h=3, key_handler=None,
                 raise_on=None, measure_raises=False):
        self.title = title
        self._w = w
        self._h = h
        self._key_handler = key_handler or {}
        self._raise_on = raise_on
        self._measure_raises = measure_raises
        self.handled = []
        self.drawn = []

    def measure(self, max_w, max_h):
        if self._measure_raises:
            raise RuntimeError('boom from content.measure')
        return min(self._w, max_w), min(self._h, max_h)

    def draw_row(self, row, width):
        self.drawn.append((row, width))
        # Fill exactly ``width`` cells so the engine's frame composes to the
        # full width (mirrors real content's contract).
        _term.write('x' * width)

    def handle_key(self, key):
        self.handled.append(key)
        if self._raise_on is not None and key == self._raise_on:
            raise RuntimeError('boom from content.handle_key')
        return self._key_handler.get(key, (False, None))


class _ListBackedContent:
    """A content whose ``draw_row`` indexes a fixed-length list.

    Mirrors how a real list/choice content would back rows with a list:
    ``draw_row(row, …)`` would ``IndexError`` if the engine ever asked for
    ``row >= len(rows_text)``. ``measure`` reports the list length so the
    engine's ``content_h`` equals it, letting the tiny-terminal blank-fill
    test prove the engine never over-reads.
    """

    def __init__(self, *, title='T', rows_text=None, key_handler=None):
        self.title = title
        self._rows = rows_text or []
        self._key_handler = key_handler or {}
        self.handled = []
        self.drawn_rows = []

    def measure(self, max_w, max_h):
        w = min(max(len(t) for t in self._rows) if self._rows else 0, max_w)
        return w, min(len(self._rows), max_h)

    def draw_row(self, row, width):
        self.drawn_rows.append(row)
        text = self._rows[row]  # IndexError if engine over-reads
        _term.write(text.ljust(width)[:width])

    def handle_key(self, key):
        self.handled.append(key)
        return self._key_handler.get(key, (False, None))


def _first_row_visible_cells(text):
    """Visible cell count of the FIRST painted row in captured output.

    ``end_row`` emits ``\\e[<row>;<col>H`` + bytes + ``\\e[m`` (+ pad) per
    row; the first such cursor-move marks the top border. Slice from after
    that first move to just before the NEXT cursor move (the second row),
    strip SGR/CSI, and measure wide-aware via ``_visible_len``.
    """
    import re
    move_re = re.compile(r'\033\[\d+;\d+H')
    moves = list(move_re.finditer(text))
    if not moves:
        return 0
    start = moves[0].end()
    end = moves[1].start() if len(moves) > 1 else len(text)
    return _term._visible_len(text[start:end])


def _frame_top_left(text):
    """``(top, left)`` of the painted frame, from the FIRST cursor-move.

    The engine paints the frame top-down; the first row emitted is the top
    border at the frame's ``(top, left)`` (``begin_row`` does ``move(abs_row,
    left)``). Parsing that first ``\\e[<row>;<col>H`` therefore recovers where
    ``_modal_place`` put the box — used to assert anchored/side placement
    end-to-end through ``run_modal`` rather than re-deriving the geometry.
    """
    import re
    m = re.search(r'\033\[(\d+);(\d+)H', text)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2))


# --- Fake browser ----------------------------------------------------------


class _FakeBrowser:
    """Exposes only the attrs/methods ``run_modal`` touches.

    Channel flags default to the "no channel" state (no buffered output, no
    armed stdin) so the real-``read_key`` fd computation resolves to
    ``(None, None)`` — though the engine tests drive an injected zero-arg
    ``_read_key`` and never reach that branch. The drain/pump/queue methods
    are counters so tests can assert dispatch.
    """

    def __init__(self):
        self._modal_open = False
        self._pane_cache = {}
        self._needs_redraw = set()
        self._out_stream_live = False
        self._out_dead = False
        self._out_buf = bytearray()
        self._stdin_live = False
        self.calls = {
            'drain_output': 0, 'pump_stdin': 0,
            'drain_main_queue': 0, 'apply_children_results': 0,
        }

    def _drain_output(self):
        self.calls['drain_output'] += 1

    def _pump_stdin(self):
        self.calls['pump_stdin'] += 1

    def drain_main_queue(self):
        self.calls['drain_main_queue'] += 1

    def apply_children_results(self):
        self.calls['apply_children_results'] += 1


class _FixedTermSize:
    def __init__(self, cols=80, rows=24):
        self._sz = (cols, rows)

    def __enter__(self):
        self._orig = _modal.term_size
        _modal.term_size = lambda: self._sz
        return self

    def __exit__(self, *_):
        _modal.term_size = self._orig


# --- Tests -----------------------------------------------------------------


class TestFrameDraw(unittest.TestCase):
    """The first paint emits the full frame with borders + title."""

    def test_frame_borders_and_title_present(self):
        b = _FakeBrowser()
        content = _StubContent(title='Confirm', w=20, h=2,
                               key_handler={'enter': (True, 'OK')})
        with _FixedTermSize(), _Capture() as cap:
            run_modal(b, content, _read_key=_scripted(['enter']))
        text = cap.text
        for ch in ('╔', '╗', '╚', '╝', '║', '═'):
            self.assertIn(ch, text, f'missing border glyph {ch!r}')
        # Title shown, bold (set_style with bold emits the '1' SGR param).
        self.assertIn('Confirm', text)
        self.assertIn('\033[0;1m', text)
        # Sync brackets wrap the paint.
        self.assertIn('\033[?2026h', text)
        self.assertIn('\033[?2026l', text)

    def test_no_title_solid_top_border(self):
        b = _FakeBrowser()
        content = _StubContent(title=None, w=12, h=2,
                               key_handler={'enter': (True, 'X')})
        with _FixedTermSize(), _Capture() as cap:
            run_modal(b, content, _read_key=_scripted(['enter']))
        # Top border is a solid run between corners — at least one
        # ``╔═══`` style sequence with no title text injected.
        self.assertIn('╔', cap.text)
        self.assertIn('╗', cap.text)

    def test_draw_row_called_with_inner_width(self):
        # content measures (10, 3) → inner_w = frame.width - 4 = 10.
        b = _FakeBrowser()
        content = _StubContent(title='T', w=10, h=3,
                               key_handler={'enter': (True, None)})
        with _FixedTermSize(), _Capture():
            run_modal(b, content, _read_key=_scripted(['enter']))
        # Three content rows drawn, each at inner width 10, indices 0..2.
        self.assertEqual([r for r, _ in content.drawn], [0, 1, 2])
        self.assertEqual({w for _, w in content.drawn}, {10})

    def test_wide_glyph_title_does_not_overflow_frame_width(self):
        # FIX 2: a long CJK/fullwidth title clipped by code points would
        # emit up to 2× the cell budget and overflow the frame. The
        # top-border row's visible cells must be <= frame width.
        b = _FakeBrowser()
        # Narrow frame so the title needs clipping: content width 8 →
        # frame width 12, leaving avail = 12 - 6 = 6 title cells (3 wide
        # glyphs). The title is 10 wide glyphs = 20 cells, well over.
        cols, rows = 80, 24
        content = _StubContent(title='東' * 10, w=8, h=2,
                               key_handler={'enter': (True, None)})
        with _FixedTermSize(cols, rows), _Capture() as cap:
            run_modal(b, content, _read_key=_scripted(['enter']))
        # The first painted row is the top border. Its visible cells (SGR
        # stripped, wide-aware) must not exceed the frame width.
        frame_w = _modal._frame_size(8, 2)[0]  # = 12
        top_cells = _first_row_visible_cells(cap.text)
        self.assertLessEqual(
            top_cells, frame_w,
            f'top border {top_cells} cells > frame width {frame_w}')

    def test_tiny_terminal_blank_fills_extra_interior_rows(self):
        # FIX 3: on a tiny terminal _modal_place returns a full-screen
        # frame whose interior exceeds the (capped) content_h. The engine
        # must NOT ask content for rows >= content_h, and must blank-fill
        # the extra interior rows to the full inner width.
        b = _FakeBrowser()
        # Tiny: cols 18 < 20 → full-screen frame 18x6 (rows-? ); content
        # measures small. With cols=18, rows=6: caps = (14, 2); content
        # asks (10, 2) → clamped to (10, 2) so content_h = 2. The frame is
        # full-screen 18x6 → interior rows = 6 - 2 = 4 > content_h.
        content = _ListBackedContent(title='T', rows_text=['aa', 'bb'],
                                     key_handler={'enter': (True, None)})
        with _FixedTermSize(18, 6), _Capture() as cap:
            run_modal(b, content, _read_key=_scripted(['enter']))
        # content_h is 2 → draw_row called only for rows 0 and 1, never
        # for an out-of-range index (which _ListBackedContent would
        # IndexError on).
        self.assertEqual(content.drawn_rows, [0, 1])
        # Frame is full-screen (tiny rule); its interior has more rows than
        # the content, so blank interior rows were composed. Sanity: the
        # painted frame spans all 6 screen rows.
        self.assertIn('\033[1;1H', cap.text)   # top border at row 1
        self.assertIn('\033[6;1H', cap.text)   # bottom border at row 6


class TestKeyDispatch(unittest.TestCase):
    """handle_key dispatch, cancel, and event routing."""

    def test_handle_key_result_returned(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'chosen')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, _read_key=_scripted(['enter']))
        self.assertEqual(res, 'chosen')
        self.assertEqual(content.handled, ['enter'])

    def test_continue_then_done(self):
        # 'a' continues (not in handler → (False, None)), 'enter' closes.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'done')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, _read_key=_scripted(['a', 'enter']))
        self.assertEqual(res, 'done')
        self.assertEqual(content.handled, ['a', 'enter'])

    def test_repaints_after_nonterminal_key(self):
        # A non-terminal key (moved selection, edited filter, typed char)
        # must trigger a repaint so its effect is visible. Without it the
        # dialog is frozen at the first paint — the bug behind "arrow keys
        # don't change the selection" (the cursor moves internally, so
        # ``enter`` still returns the moved item and outcome-based tests
        # pass, but the screen never updates). The private cache makes the
        # repaint differential, but it must be CALLED. Each full paint draws
        # content row 0 exactly once, so the row-0 draw count == paint count.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'done')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, _read_key=_scripted(['a', 'enter']))
        self.assertEqual(res, 'done')
        paints = sum(1 for (row, _w) in content.drawn if row == 0)
        # One paint at open, one after the non-terminal 'a'.
        self.assertEqual(paints, 2)

    def test_esc_cancels_to_none(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'x')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, _read_key=_scripted(['esc']))
        self.assertIsNone(res)
        # esc never reaches content.
        self.assertEqual(content.handled, [])

    def test_ctrl_c_cancels_to_none(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'x')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, _read_key=_scripted(['ctrl-c']))
        self.assertIsNone(res)
        self.assertEqual(content.handled, [])

    def test_mouse_events_swallowed(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'r')})
        keys = ['mouse-click:5:5', 'scroll-up:1:1', 'scroll-down:2:2', 'enter']
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, _read_key=_scripted(keys))
        self.assertEqual(res, 'r')
        # Only 'enter' reached the content; mouse events were swallowed.
        self.assertEqual(content.handled, ['enter'])

    def test_notify_drains_without_dispatch(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'r')})
        with _FixedTermSize(), _Capture():
            run_modal(b, content, _read_key=_scripted(['_notify', 'enter']))
        self.assertEqual(b.calls['drain_main_queue'], 1)
        self.assertEqual(b.calls['apply_children_results'], 1)
        # _notify did NOT reach content.
        self.assertEqual(content.handled, ['enter'])

    def test_writable_drains_output(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'r')})
        with _FixedTermSize(), _Capture():
            run_modal(b, content, _read_key=_scripted(['_writable', 'enter']))
        self.assertEqual(b.calls['drain_output'], 1)
        self.assertEqual(content.handled, ['enter'])

    def test_stdin_pumps(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'r')})
        with _FixedTermSize(), _Capture():
            run_modal(b, content, _read_key=_scripted(['_stdin', 'enter']))
        self.assertEqual(b.calls['pump_stdin'], 1)
        self.assertEqual(content.handled, ['enter'])


class TestCancelGestures(unittest.TestCase):
    """``cancel_keys`` / ``cancel_on_right_click`` close the dialog with None
    BEFORE the key reaches ``content.handle_key`` (#1039 — the ctx.menu path
    passes these so a repeated trigger toggles the menu shut). Defaults are
    off, so every non-menu modal is unaffected (asserted last)."""

    _CANCEL_KEYS = frozenset({'\\', 'f1'})

    def test_cancel_key_closes_to_none_before_content(self):
        # '\' is in cancel_keys → close with None; content never sees it.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'x')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, cancel_keys=self._CANCEL_KEYS,
                            _read_key=_scripted(['\\']))
        self.assertIsNone(res)
        self.assertEqual(content.handled, [])

    def test_f1_in_cancel_keys_closes(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'x')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, cancel_keys=self._CANCEL_KEYS,
                            _read_key=_scripted(['f1']))
        self.assertIsNone(res)
        self.assertEqual(content.handled, [])

    def test_right_click_closes_when_enabled(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'x')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, cancel_on_right_click=True,
                            _read_key=_scripted(['right-click:7:3']))
        self.assertIsNone(res)
        self.assertEqual(content.handled, [])

    def test_non_cancel_key_still_reaches_content(self):
        # A key NOT in cancel_keys is unaffected — reaches content as usual.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, cancel_keys=self._CANCEL_KEYS,
                            cancel_on_right_click=True,
                            _read_key=_scripted(['a', 'enter']))
        self.assertEqual(res, 'ok')
        self.assertEqual(content.handled, ['a', 'enter'])

    def test_modifier_right_click_not_a_close_gesture(self):
        # Only the BARE ``right-click:`` closes; a modifier-prefixed variant
        # (``alt-right-click:`` …) is NOT a close gesture — the dialog stays
        # open and the later ``enter`` decides the result (matching how the
        # open path #1027 only fires on a bare right-click).
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, cancel_on_right_click=True,
                            _read_key=_scripted(
                                ['alt-right-click:7:3', 'enter']))
        self.assertEqual(res, 'ok')
        # The modifier variant did not close the dialog.
        self.assertIn('enter', content.handled)

    def test_defaults_off_cancel_key_reaches_content(self):
        # No cancel args (the pick/confirm/input/alert default): '\' is just
        # another key handed to content, NOT a close gesture.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, _read_key=_scripted(['\\', 'enter']))
        self.assertEqual(res, 'ok')
        self.assertEqual(content.handled, ['\\', 'enter'])

    def test_defaults_off_right_click_swallowed_not_close(self):
        # No cancel_on_right_click: a right-click is swallowed (the existing
        # mouse-swallow), neither closing nor reaching content.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content,
                            _read_key=_scripted(['right-click:7:3', 'enter']))
        self.assertEqual(res, 'ok')
        self.assertEqual(content.handled, ['enter'])


class TestRestorePoison(unittest.TestCase):
    """Close-time cache poisoning + 'all' redraw flag."""

    def _seed_cache(self, browser, rect):
        cache = PaneCache()
        cache.invalidate(rect)
        # Populate every line slot with a plausible prior entry.
        cache.lines = [(rect.width, 'old') for _ in range(rect.height)]
        # Simulate a steady-state cache (a full paint happened).
        cache.prev_rect = rect
        browser._pane_cache['list'] = cache
        return cache

    def test_intersecting_rows_poisoned_and_all_flagged(self):
        b = _FakeBrowser()
        # Frame at 80x24, centered, content (20,2) → frame ~24x4 around
        # the screen center (rows ~11..14). Seed a full-screen-ish list
        # pane cache that surely intersects.
        pane_rect = Rect(1, 1, 81, 25)  # cols 1..80, rows 1..24
        cache = self._seed_cache(b, pane_rect)
        content = _StubContent(title='Confirm', w=20, h=2,
                               key_handler={'enter': (True, 'OK')})
        with _FixedTermSize(80, 24), _Capture():
            run_modal(b, content, _read_key=_scripted(['enter']))

        self.assertIn('all', b._needs_redraw)
        # The rows the frame covered must be poisoned with full pane width
        # + the poison marker; rows it didn't cover stay as 'old'.
        poisoned = [i for i, ln in enumerate(cache.lines)
                    if ln == (pane_rect.width, _modal._MODAL_POISON)]
        self.assertTrue(poisoned, 'expected some poisoned rows')
        # Every poisoned entry carries the NUL marker.
        for i in poisoned:
            self.assertEqual(cache.lines[i],
                             (pane_rect.width, _modal._MODAL_POISON))
        # Some rows outside the frame remain untouched.
        untouched = [i for i, ln in enumerate(cache.lines) if ln == (80, 'old')]
        self.assertTrue(untouched, 'expected some untouched rows')

    def test_poison_marker_contains_nul(self):
        # Contract: the marker must contain NUL so no sanitizer can produce
        # it (guaranteed cache miss).
        self.assertIn('\x00', _modal._MODAL_POISON)

    def test_subsequent_end_row_repaints_poisoned_row(self):
        # A poisoned row must MISS the cache on the next end_row pass and
        # pad out to the full pane width (the flicker-free restore lever).
        b = _FakeBrowser()
        pane_rect = Rect(1, 1, 41, 25)  # width 40
        cache = self._seed_cache(b, pane_rect)
        content = _StubContent(title='T', w=10, h=2,
                               key_handler={'enter': (True, None)})
        with _FixedTermSize(80, 24), _Capture():
            run_modal(b, content, _read_key=_scripted(['enter']))
        # Find a poisoned row.
        poisoned_idx = next(
            i for i, ln in enumerate(cache.lines)
            if ln == (pane_rect.width, _modal._MODAL_POISON))
        abs_row = pane_rect.top + poisoned_idx
        # Repaint that row with short content: it must emit (cache miss)
        # and pad to the planted full width (40).
        with _Capture() as cap:
            _term.begin_row(cache, poisoned_idx, abs_row,
                            pane_rect.left, pane_rect.right, rightmost=False)
            _term.write('hi')
            _term.end_row()
        # visible('hi')=2, planted cached visible=40 → pad 38 spaces.
        self.assertEqual(cap.text, f'\033[{abs_row};1Hhi\033[m' + ' ' * 38)
        self.assertEqual(cache.lines[poisoned_idx], (40, 'hi'))

    def test_non_intersecting_cache_untouched(self):
        b = _FakeBrowser()
        # A tiny pane parked in the top-left corner the centered frame
        # can't reach at 80x24.
        pane_rect = Rect(1, 1, 3, 3)  # cols 1..2, rows 1..2
        cache = self._seed_cache(b, pane_rect)
        content = _StubContent(title='T', w=20, h=2,
                               key_handler={'enter': (True, None)})
        with _FixedTermSize(80, 24), _Capture():
            run_modal(b, content, _read_key=_scripted(['enter']))
        # No row poisoned — frame doesn't intersect the corner pane.
        self.assertTrue(all(ln == (2, 'old') for ln in cache.lines))
        # 'all' is still flagged regardless.
        self.assertIn('all', b._needs_redraw)

    def test_restore_runs_when_content_raises(self):
        b = _FakeBrowser()
        pane_rect = Rect(1, 1, 81, 25)
        cache = self._seed_cache(b, pane_rect)
        content = _StubContent(title='T', w=20, h=2, raise_on='boom')
        with _FixedTermSize(80, 24), _Capture():
            with self.assertRaises(RuntimeError):
                run_modal(b, content, _read_key=_scripted(['boom']))
        # Restore still ran: rows poisoned, 'all' flagged, _modal_open clear.
        self.assertIn('all', b._needs_redraw)
        self.assertTrue(any(
            ln == (pane_rect.width, _modal._MODAL_POISON)
            for ln in cache.lines))
        self.assertFalse(b._modal_open)

    def test_modal_open_cleared_on_normal_close(self):
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'r')})
        with _FixedTermSize(), _Capture():
            run_modal(b, content, _read_key=_scripted(['enter']))
        self.assertFalse(b._modal_open)


class TestOuterMargin(unittest.TestCase):
    """#1043: a blank-space column just outside each vertical border.

    A centered frame at 80x24 with content (20, 2) measures to a 24-wide,
    4-tall box at left=29 (cols 29..52), rows 11..14. The left margin
    column is 28, the right is 53 (just past the box). Each painted row
    overdraws a single blank space in those two columns.
    """

    # Frame geometry for content (20, 2) on an 80x24 screen, derived the
    # same way the engine does (caps clamp 20 well under, +4 frame, center).
    BOX_LEFT = 29
    BOX_RIGHT = 53          # exclusive — box owns cols 29..52
    TOP = 11
    BOTTOM = 15             # exclusive — rows 11..14
    LM = BOX_LEFT - 1       # 28
    RM = BOX_RIGHT          # 53

    def _run(self, browser=None):
        b = browser or _FakeBrowser()
        content = _StubContent(title='Confirm', w=20, h=2,
                               key_handler={'enter': (True, 'OK')})
        with _FixedTermSize(80, 24), _Capture() as cap:
            run_modal(b, content, _read_key=_scripted(['enter']))
        return b, cap.text

    def test_margin_columns_painted_blank_each_row(self):
        # Every painted row gets a reset + single space at the left margin
        # (col 28) and the right margin (col 53). ``move`` emits
        # ``\e[<row>;<col>H``; the margin then writes ``\e[0m`` + ' '.
        _b, text = self._run()
        for abs_row in range(self.TOP, self.BOTTOM):
            self.assertIn(f'\033[{abs_row};{self.LM}H\033[0m ', text,
                          f'left margin not blank at row {abs_row}')
            self.assertIn(f'\033[{abs_row};{self.RM}H\033[0m ', text,
                          f'right margin not blank at row {abs_row}')

    def test_margins_inside_the_open_sync(self):
        # The margins are part of the single synchronized paint — they land
        # between the sync open and close, not after the frame flushed.
        _b, text = self._run()
        open_i = text.index('\033[?2026h')
        close_i = text.rindex('\033[?2026l')
        margin_i = text.index(f'\033[{self.TOP};{self.LM}H\033[0m ')
        self.assertTrue(open_i < margin_i < close_i)

    def test_margin_columns_outside_the_box(self):
        # The margins must NOT be inside the box: no margin write targets a
        # box column (29..52). They sit strictly at 28 and 53.
        _b, text = self._run()
        for col in range(self.BOX_LEFT, self.BOX_RIGHT):
            self.assertNotIn(f'\033[{self.TOP};{col}H\033[0m ', text,
                             f'a margin landed inside the box at col {col}')

    def test_close_poisons_pane_holding_the_left_margin(self):
        # A pane split EXACTLY at the box's left edge (cols 1..28) does not
        # overlap the frame proper, but it OWNS the left-margin column (28).
        # The close-time restore widens its poisoned region by one column
        # per side, so this pane IS poisoned — without that widening the
        # blank margin cell it overdrew would survive on close.
        b = _FakeBrowser()
        left_pane = Rect(1, 1, self.BOX_LEFT, 25)   # cols 1..28
        cache = PaneCache()
        cache.invalidate(left_pane)
        cache.lines = [(left_pane.width, 'old') for _ in range(left_pane.height)]
        cache.prev_rect = left_pane
        b._pane_cache['left'] = cache
        self._run(b)
        poisoned = [i for i, ln in enumerate(cache.lines)
                    if ln == (left_pane.width, _modal._MODAL_POISON)]
        self.assertTrue(
            poisoned,
            'pane owning the left-margin column was not poisoned — its '
            'blank margin cell would leak on close')
        # The poisoned rows are exactly the box's row span (11..14 -> rel
        # 10..13 in this top=1 pane).
        self.assertEqual(poisoned, list(range(self.TOP - 1, self.BOTTOM - 1)))

    def test_close_poisons_pane_holding_the_right_margin(self):
        # Symmetric: a pane starting just past the box (col 53) owns the
        # right-margin column and must be poisoned by the widened restore.
        b = _FakeBrowser()
        right_pane = Rect(self.BOX_RIGHT, 1, 81, 25)   # cols 53..80
        cache = PaneCache()
        cache.invalidate(right_pane)
        cache.lines = [(right_pane.width, 'old')
                       for _ in range(right_pane.height)]
        cache.prev_rect = right_pane
        b._pane_cache['right'] = cache
        self._run(b)
        poisoned = [i for i, ln in enumerate(cache.lines)
                    if ln == (right_pane.width, _modal._MODAL_POISON)]
        self.assertTrue(
            poisoned,
            'pane owning the right-margin column was not poisoned')

    def test_tiny_full_screen_omits_margins(self):
        # On a tiny terminal the frame spans the whole screen (cols 1..18),
        # so both margin columns would fall off-screen (col 0 / col 19).
        # The engine omits them — no off-screen move, no crash.
        b = _FakeBrowser()
        content = _ListBackedContent(title='T', rows_text=['aa', 'bb'],
                                     key_handler={'enter': (True, None)})
        with _FixedTermSize(18, 6), _Capture() as cap:
            run_modal(b, content, _read_key=_scripted(['enter']))
        text = cap.text
        # No write addressed column 0 or column 19 (1 past the 18-col edge).
        self.assertNotIn('\033[1;0H', text)
        self.assertNotIn('\033[1;19H', text)
        # And the frame still painted full-screen (sanity).
        self.assertIn('\033[1;1H', text)


class TestOpenFailure(unittest.TestCase):
    """A failure on the FIRST measure/paint must not brick the modal."""

    def test_measure_raises_propagates_and_clears_modal_open(self):
        # FIX 1: ``frame`` is bound to None before the try, so the finally's
        # restore loop is a clean no-op and the ORIGINAL exception
        # propagates (not an UnboundLocalError), with ``_modal_open``
        # cleared so a subsequent run_modal does not falsely raise
        # 'already open'.
        b = _FakeBrowser()
        # Populate a pane cache the way production would before a modal
        # opens — this is exactly the state that made the old finally hit
        # an unbound ``frame``.
        cache = PaneCache()
        cache.invalidate(Rect(1, 1, 81, 25))
        cache.lines = [(80, 'old') for _ in range(cache.rect.height)]
        b._pane_cache['list'] = cache

        content = _StubContent(measure_raises=True)
        with _FixedTermSize(80, 24), _Capture():
            with self.assertRaises(RuntimeError) as exc:
                run_modal(b, content, _read_key=_scripted([]))
        # The real measure error — NOT an UnboundLocalError masking it.
        self.assertIn('content.measure', str(exc.exception))
        self.assertNotIsInstance(exc.exception, UnboundLocalError)
        # _modal_open was cleared by the finally.
        self.assertFalse(b._modal_open)
        # No row poisoned (frame was None → restore loop was a no-op).
        self.assertTrue(all(ln == (80, 'old') for ln in cache.lines))

        # And a subsequent run_modal does NOT spuriously raise 'already
        # open' — the whole point of FIX 1.
        content2 = _StubContent(key_handler={'enter': (True, 'ok')})
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, content2, _read_key=_scripted(['enter']))
        self.assertEqual(res, 'ok')


class TestReentryGuard(unittest.TestCase):
    """A modal already open is a hard RuntimeError."""

    def test_reentry_raises(self):
        b = _FakeBrowser()
        b._modal_open = True  # pretend a modal is already open
        content = _StubContent(key_handler={'enter': (True, 'r')})
        with _FixedTermSize(), _Capture():
            with self.assertRaises(RuntimeError):
                run_modal(b, content, _read_key=_scripted(['enter']))
        # The guard must not have consumed any key.
        self.assertEqual(content.handled, [])
        # And the flag is left as the caller set it (the guard fires before
        # the try/finally that would clear it).
        self.assertTrue(b._modal_open)


class TestChannelFdComputation(unittest.TestCase):
    """The real-read_key branch computes the same (wfd, rfd) the main loop
    does (040-state.py): wfd=1 only while ``_out_stream_live and not
    _out_dead and _out_buf``; rfd=0 only while ``_stdin_live``.

    Exercised by monkeypatching the modal module's real ``read_key`` with a
    capturing fake and calling ``run_modal`` WITHOUT ``_read_key`` (so the
    engine takes the real-path fd branch rather than the zero-arg seam).
    """

    def _run_capturing_fds(self, browser):
        captured = {}

        def fake_read_key(*, write_fd=None, aux_read_fd=None):
            captured['wfd'] = write_fd
            captured['rfd'] = aux_read_fd
            return 'enter'  # close immediately

        orig = _modal.read_key
        _modal.read_key = fake_read_key
        try:
            content = _StubContent(key_handler={'enter': (True, None)})
            with _FixedTermSize(80, 24), _Capture():
                run_modal(browser, content)  # no _read_key → real path
        finally:
            _modal.read_key = orig
        return captured

    def test_no_channels_both_none(self):
        b = _FakeBrowser()  # no buffered output, stdin not armed
        self.assertEqual(self._run_capturing_fds(b), {'wfd': None, 'rfd': None})

    def test_buffered_output_arms_write_fd(self):
        b = _FakeBrowser()
        b._out_stream_live = True
        b._out_buf = bytearray(b'pending')
        self.assertEqual(self._run_capturing_fds(b), {'wfd': 1, 'rfd': None})

    def test_dead_output_keeps_write_fd_none(self):
        b = _FakeBrowser()
        b._out_stream_live = True
        b._out_dead = True  # channel dead → never offer fd 1
        b._out_buf = bytearray(b'pending')
        self.assertEqual(self._run_capturing_fds(b), {'wfd': None, 'rfd': None})

    def test_empty_buffer_keeps_write_fd_none(self):
        b = _FakeBrowser()
        b._out_stream_live = True
        b._out_buf = bytearray()  # nothing to drain
        self.assertEqual(self._run_capturing_fds(b), {'wfd': None, 'rfd': None})

    def test_armed_stdin_arms_read_fd(self):
        b = _FakeBrowser()
        b._stdin_live = True
        self.assertEqual(self._run_capturing_fds(b), {'wfd': None, 'rfd': 0})


class TestResizeRepaint(unittest.TestCase):
    """A resize flag mid-loop clears the screen + pane caches and repaints."""

    def setUp(self):
        # Ensure flags start clear; the modal module reads them via
        # globals().get on its own module dict.
        _modal.__dict__.pop('g_resize_flag', None)
        _modal.__dict__.pop('g_screen_lost_flag', None)

    def tearDown(self):
        _modal.__dict__.pop('g_resize_flag', None)
        _modal.__dict__.pop('g_screen_lost_flag', None)

    def test_resize_clears_screen_and_pane_cache(self):
        b = _FakeBrowser()
        # Seed a pane cache; the resize path must clear it.
        cache = PaneCache()
        cache.invalidate(Rect(1, 1, 81, 25))
        b._pane_cache['list'] = cache

        content = _StubContent(title='T', w=20, h=2,
                               key_handler={'enter': (True, 'done')})

        # A read_key seam that sets the resize flag on its first call, then
        # returns 'enter'. The engine checks the flag after each read.
        state = {'n': 0}

        def rk():
            state['n'] += 1
            if state['n'] == 1:
                _modal.__dict__['g_resize_flag'] = True
                return '_notify'  # any key; resize flag is checked first
            return 'enter'

        with _FixedTermSize(80, 24), _Capture() as cap:
            res = run_modal(b, content, _read_key=rk)

        self.assertEqual(res, 'done')
        # Screen was cleared at least once (the resize path emits \e[2J).
        self.assertIn('\033[2J', cap.text)
        # The seeded pane cache was cleared during the resize repaint, so by
        # close time it's a fresh dict — nothing to poison, but 'all' set.
        self.assertIn('all', b._needs_redraw)
        # _notify drain did NOT run for that iteration: the resize check
        # consumes the wake before the event dispatch.
        self.assertEqual(b.calls['drain_main_queue'], 0)

    def test_resized_close_blanks_screen_again(self):
        """After a resize-while-open, close blanks the screen a SECOND time.

        Cache-poisoning can't clear the dialog's own cells once the caches
        were cleared by the resize (the next ``render_full`` rebuilds them
        fresh and first-paints with no padding). So the close-time restore
        must blank the screen again, leaving the genuinely-empty precondition
        that first-paint assumes. Without the resize the close emits no
        ``\\e[2J`` at all (it poisons instead) — assert both: the resized
        close emits a close-time clear AFTER the dialog's last paint, and the
        non-resized close emits none.
        """
        # Resized close: two \e[2J total — one at the resize repaint, one at
        # close — and the close-time one comes after the final frame paint.
        b = _FakeBrowser()
        b._pane_cache['list'] = PaneCache()
        b._pane_cache['list'].invalidate(Rect(1, 1, 81, 25))
        content = _StubContent(title='T', w=20, h=2,
                               key_handler={'enter': (True, 'done')})
        state = {'n': 0}

        def rk():
            state['n'] += 1
            if state['n'] == 1:
                _modal.__dict__['g_resize_flag'] = True
                return '_notify'
            return 'enter'

        with _FixedTermSize(80, 24), _Capture() as cap:
            run_modal(b, content, _read_key=rk)
        self.assertEqual(cap.text.count('\033[2J'), 2,
                         'expected a resize clear AND a close clear')
        # The close-time clear is the LAST \e[2J, emitted after the dialog's
        # final repaint (so it wipes the box the resize repaint drew back).
        self.assertGreater(cap.text.rfind('\033[2J'), cap.text.find('T'),
                           'close-time clear must follow the dialog paint')

        # No-resize close: the restore poisons caches and emits NO \e[2J.
        b2 = _FakeBrowser()
        b2._pane_cache['list'] = PaneCache()
        b2._pane_cache['list'].invalidate(Rect(1, 1, 81, 25))
        content2 = _StubContent(title='T', w=20, h=2,
                                key_handler={'enter': (True, 'done')})
        with _FixedTermSize(80, 24), _Capture() as cap2:
            run_modal(b2, content2, _read_key=_scripted(['enter']))
        self.assertNotIn('\033[2J', cap2.text,
                         'a non-resized close must not blank the screen')


class TestDelayInteraction(unittest.TestCase):
    """``delay_interaction`` — open-time input drain + threshold gate.

    Deterministic: a ``_KeySource`` backs both the read seam and the
    open-time ``_input_ready`` poll; a ``_FakeClock`` is the monotonic
    source; ``_delay_threshold`` is set explicitly. No real sleeps.
    """

    def setUp(self):
        _modal.__dict__.pop('g_resize_flag', None)
        _modal.__dict__.pop('g_screen_lost_flag', None)

    def tearDown(self):
        _modal.__dict__.pop('g_resize_flag', None)
        _modal.__dict__.pop('g_screen_lost_flag', None)

    def test_pending_keys_drained_at_open_when_true(self):
        # Two keys typed at the previous screen are drained at open; the
        # dialog then dispatches 'enter'. The drained keys never reach
        # content.handle_key.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        src = _KeySource(drain=['x', 'y'], loop=['enter'])
        clock = _FakeClock()
        with _FixedTermSize(), _Capture():
            res = run_modal(
                b, content, delay_interaction=True,
                _read_key=src.read, _input_ready=src.pending,
                _now=clock, _delay_threshold=0.0)
        self.assertEqual(res, 'ok')
        # Drain consumed x, y; loop read enter.
        self.assertEqual(src.read_log, ['x', 'y', 'enter'])
        # content saw only 'enter' (drained keys discarded).
        self.assertEqual(content.handled, ['enter'])

    def test_no_drain_when_false(self):
        # With the default (False), the open-time drain must NOT run even
        # when _input_ready would report pending keys — the poll is never
        # consulted, so the "drain" keys stay unread and 'enter' dispatches.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        polled = {'n': 0}

        def poll():
            polled['n'] += 1
            return True  # would loop forever IF the drain ran

        src = _KeySource(drain=[], loop=['enter'])
        with _FixedTermSize(), _Capture():
            res = run_modal(
                b, content, delay_interaction=False,
                _read_key=src.read, _input_ready=poll,
                _now=_FakeClock(), _delay_threshold=0.0)
        self.assertEqual(res, 'ok')
        self.assertEqual(polled['n'], 0)  # poll never consulted
        self.assertEqual(content.handled, ['enter'])

    def test_keys_inside_window_discarded(self):
        # Threshold 0.5; clock does not advance. Keys 'a','b' arrive inside
        # the window and are discarded; only after we advance past the
        # threshold does 'enter' dispatch and close.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        clock = _FakeClock(start=100.0)

        # The read seam advances the clock past the gate ONLY when it serves
        # 'enter', so 'a' and 'b' are read while now() < gate_until.
        keys = iter(['a', 'b', 'enter'])

        def rk():
            k = next(keys)
            if k == 'enter':
                clock.advance(1.0)  # now past the 0.5 gate
            return k

        with _FixedTermSize(), _Capture():
            res = run_modal(
                b, content, delay_interaction=True,
                _read_key=rk, _input_ready=lambda: False,
                _now=clock, _delay_threshold=0.5)
        self.assertEqual(res, 'ok')
        # 'a' and 'b' were discarded by the gate; only 'enter' dispatched.
        self.assertEqual(content.handled, ['enter'])

    def test_key_after_window_dispatched(self):
        # A single key that arrives AFTER the window dispatches normally.
        # ``gate_until`` is computed from the first-paint time, so the clock
        # must advance past the threshold after open — done in the read seam.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        clock = _FakeClock(start=0.0)

        def rk():
            clock.advance(1.0)  # now past the 0.5 gate (set at first paint)
            return 'enter'

        with _FixedTermSize(), _Capture():
            res = run_modal(
                b, content, delay_interaction=True,
                _read_key=rk, _input_ready=lambda: False,
                _now=clock, _delay_threshold=0.5)
        self.assertEqual(res, 'ok')
        self.assertEqual(content.handled, ['enter'])

    def test_notify_serviced_during_window(self):
        # Inside the window, '_notify' is still drained (background work
        # keeps flowing); the normal key after the clock advances dispatches.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        clock = _FakeClock(start=0.0)
        keys = iter(['_notify', 'enter'])

        def rk():
            k = next(keys)
            if k == 'enter':
                clock.advance(1.0)  # past the gate for the final dispatch
            return k

        with _FixedTermSize(), _Capture():
            res = run_modal(
                b, content, delay_interaction=True,
                _read_key=rk, _input_ready=lambda: False,
                _now=clock, _delay_threshold=0.5)
        self.assertEqual(res, 'ok')
        # _notify drained even though we were inside the window.
        self.assertEqual(b.calls['drain_main_queue'], 1)
        self.assertEqual(b.calls['apply_children_results'], 1)
        self.assertEqual(content.handled, ['enter'])

    def test_channel_event_serviced_during_window(self):
        # '_writable' is serviced inside the window too (a streaming recipe
        # behind the dialog keeps draining output).
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        clock = _FakeClock(start=0.0)
        keys = iter(['_writable', 'enter'])

        def rk():
            k = next(keys)
            if k == 'enter':
                clock.advance(1.0)
            return k

        with _FixedTermSize(), _Capture():
            res = run_modal(
                b, content, delay_interaction=True,
                _read_key=rk, _input_ready=lambda: False,
                _now=clock, _delay_threshold=0.5)
        self.assertEqual(res, 'ok')
        self.assertEqual(b.calls['drain_output'], 1)
        self.assertEqual(content.handled, ['enter'])

    def test_default_false_no_gating(self):
        # Sanity: with no delay-interaction args at all (the production
        # default for everything ctx exposes), the very first key dispatches
        # with no drain and no gate — existing behavior intact.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'ok')})
        with _FixedTermSize(), _Capture():
            res = run_modal(b, content, _read_key=_scripted(['enter']))
        self.assertEqual(res, 'ok')
        self.assertEqual(content.handled, ['enter'])

    def test_esc_gated_during_window(self):
        # Even esc/ctrl-c can't dismiss the dialog inside the window — the
        # gate sits above the cancel branch. esc inside the window is
        # discarded; esc after the clock advances cancels to None.
        b = _FakeBrowser()
        content = _StubContent(key_handler={'enter': (True, 'x')})
        clock = _FakeClock(start=0.0)
        keys = iter(['esc', 'esc'])

        def rk():
            k = next(keys)
            return k

        # First esc inside window (discarded), then advance and second esc
        # cancels. Advance via a side channel: read twice, advancing between.
        reads = {'n': 0}

        def rk2():
            reads['n'] += 1
            if reads['n'] == 2:
                clock.advance(1.0)
            return 'esc'

        with _FixedTermSize(), _Capture():
            res = run_modal(
                b, content, delay_interaction=True,
                _read_key=rk2, _input_ready=lambda: False,
                _now=clock, _delay_threshold=0.5)
        self.assertIsNone(res)  # cancelled by the second esc
        self.assertEqual(reads['n'], 2)  # first esc was gated, not a cancel
        self.assertEqual(content.handled, [])  # esc never reaches content


class TestContextMenuSideSlot(unittest.TestCase):
    """``_measure_frame`` decides / stores / reuses the per-chain menu side.

    For an anchored placement the engine resolves the vertical SIDE through
    ``browser._context_menu_side`` (#1041): unset → decide below-if-fits-else-
    above from the measured frame height and STORE it; set → REUSE it so a
    submenu opened later in the same chain stays on the side the first menu
    picked, shifting (clamping) to fit rather than flipping. The slot is owned
    by ``Browser._fire_context_menu`` (reset per chain, cleared after); here a
    fake Browser stands in for it and is observed directly. ``run_modal``
    paints the frame top-down, so the first cursor-move recovers its placement.
    """

    def _menu(self, browser, *, anchor, cols=80, rows=24, h=4):
        """Open one anchored menu of content height ``h``; return its placed
        ``(top, left)`` from the painted frame."""
        content = _StubContent(title=None, w=20, h=h,
                               key_handler={'enter': (True, None)})
        with _FixedTermSize(cols, rows), _Capture() as cap:
            run_modal(browser, content, placement='anchor', anchor=anchor,
                      _read_key=_scripted(['enter']))
        return _frame_top_left(cap.text)

    def test_first_menu_that_fits_below_stores_below(self):
        # Anchor high on the screen with room beneath: the engine decides
        # 'below', stores it, and drops the box one row under the anchor.
        b = _FakeBrowser()
        b._context_menu_side = None
        top, _left = self._menu(b, anchor=(5, 10), h=4)
        self.assertEqual(b._context_menu_side, 'below')  # decided + stored
        self.assertEqual(top, 6)                         # row + 1, below

    def test_first_menu_near_bottom_stores_above(self):
        # Anchor near the bottom: 'below' would overflow, so the first menu
        # decides + stores 'above' and sits above the anchor row.
        b = _FakeBrowser()
        b._context_menu_side = None
        # 80x24, content h=4 → frame h=6. Anchor at row 22: below = 23..28 >
        # 24, so it flips above → top = 22 - 6 = 16.
        top, _left = self._menu(b, anchor=(22, 10), h=4)
        self.assertEqual(b._context_menu_side, 'above')
        self.assertEqual(top, 16)

    def test_tall_submenu_reuses_below_and_clamps_not_flips(self):
        # THE end-to-end #1041 case. The chain's side is already 'below'
        # (set by the first menu). A TALL submenu (content h=18 → frame
        # h=20, so 20 rows on screen) dropped below row 5 would be 6..25 >
        # 24. It must REUSE
        # 'below' and CLAMP up (top = rows - h + 1 = 24 - 20 + 1 = 5),
        # overlapping the subject row — NOT flip above (which would put top
        # at 1). The stored side is unchanged by the reuse.
        b = _FakeBrowser()
        b._context_menu_side = 'below'           # chain already chose below
        top, _left = self._menu(b, anchor=(5, 10), h=18)
        self.assertEqual(b._context_menu_side, 'below')  # still below
        self.assertEqual(top, 5)                 # clamped down to fit
        self.assertNotEqual(top, 1)              # did NOT flip to the above pos

    def test_submenu_reuses_above(self):
        # Symmetric reuse: chain side 'above', a tall submenu near the top
        # stays above-anchored and clamps to the top edge instead of flipping
        # below.
        b = _FakeBrowser()
        b._context_menu_side = 'above'
        # content h=16 → frame h=18. Anchor row 4: above = 4 - 18 = -14 →
        # clamp to top 1. (Flipping below would be top = 5.)
        top, _left = self._menu(b, anchor=(4, 10), h=16)
        self.assertEqual(b._context_menu_side, 'above')
        self.assertEqual(top, 1)
        self.assertNotEqual(top, 5)

    def test_fresh_anchored_placement_matches_legacy_decision(self):
        # With the slot present-but-None the FIRST anchored menu reproduces
        # today's below-if-fits-else-above rule — the stored side just records
        # which way it went. Sweep the anchor row and assert the placement
        # tracks the overflow predicate (asserting the rule, not a constant).
        rows = 24
        for row in (1, 5, 10, 17, 18, 22):
            with self.subTest(row=row):
                b = _FakeBrowser()
                b._context_menu_side = None
                top, _left = self._menu(b, anchor=(row, 10), rows=rows, h=4)
                fh = 6  # content 4 + frame 2
                if (row + 1) + fh - 1 <= rows:
                    self.assertEqual(top, row + 1)
                    self.assertEqual(b._context_menu_side, 'below')
                else:
                    self.assertEqual(top, max(1, row - fh))
                    self.assertEqual(b._context_menu_side, 'above')

    def test_browser_without_slot_decides_fresh_and_does_not_persist(self):
        # A Browser lacking the slot (any non-context anchored use) must
        # behave like "no preferred side" — decide fresh, never crash, and
        # NOT grow the attribute (so nothing leaks a side onto it).
        b = _FakeBrowser()
        self.assertFalse(hasattr(b, '_context_menu_side'))
        top, _left = self._menu(b, anchor=(5, 10), h=4)
        self.assertEqual(top, 6)                         # fresh: below
        self.assertFalse(hasattr(b, '_context_menu_side'))  # not persisted

    def test_centered_placement_never_touches_slot(self):
        # A centered dialog must not read or write the side slot.
        b = _FakeBrowser()
        b._context_menu_side = None
        content = _StubContent(title=None, w=20, h=3,
                               key_handler={'enter': (True, None)})
        with _FixedTermSize(80, 24), _Capture():
            run_modal(b, content, placement='center',
                      _read_key=_scripted(['enter']))
        self.assertIsNone(b._context_menu_side)          # untouched


class TestRunModalBoundsThreading(unittest.TestCase):
    """``run_modal(..., bounds=(L, R))`` threads the bound to ``_modal_place``.

    End-to-end through the engine: ``bounds`` flows ``run_modal`` →
    ``_measure_frame`` → ``_modal_place``, leaning the anchored menu X toward
    screen center but clamping its footprint into ``[L, R]`` (#1051). The frame
    is painted top-down, so the first cursor-move recovers where the box landed
    — asserted against an independent ``_modal_place`` call (the threading), and
    against the worked-example intent (footprint right edge pinned to R).
    """

    def _menu(self, *, anchor, bounds, cols, rows=24, w=20, h=4):
        """Open one anchored menu with ``bounds``; return its placed ``left``
        plus the frame width (content ``w`` + 4) so callers can locate the
        footprint's right-margin column (``left + frame_w``)."""
        b = _FakeBrowser()
        b._context_menu_side = None
        content = _StubContent(title=None, w=w, h=h,
                               key_handler={'enter': (True, None)})
        with _FixedTermSize(cols, rows), _Capture() as cap:
            run_modal(b, content, placement='anchor', anchor=anchor,
                      bounds=bounds, _read_key=_scripted(['enter']))
        _top, left = _frame_top_left(cap.text)
        return left, w + 4

    def test_left_of_center_bound_pins_footprint_right_edge_to_R(self):
        # The worked example through the engine: list pane [1, 40] on a 300-col
        # screen pins the footprint's right margin column to R = 40 (NOT the
        # ~150 screen-centered position #1040 produced). The box left equals
        # what ``_modal_place`` computes for the same inputs (threading), and
        # left + frame_w == R (the footprint's right edge).
        L, R = 1, 40
        left, frame_w = self._menu(anchor=(5, 1), bounds=(L, R), cols=300)
        expect = _modal._modal_place(300, 24, frame_w, 6, placement='anchor',
                                     anchor=(5, 1), bounds=(L, R))
        self.assertEqual(left, expect.left)          # bound reached _modal_place
        self.assertEqual(left + frame_w, R)          # footprint right edge at R

    def test_no_bounds_keeps_full_screen_centering(self):
        # Same anchor/screen but ``bounds=None``: the menu keeps the #1040
        # full-screen centering (markedly different from the bounded placement),
        # confirming the bound is what changed the position.
        bounded, frame_w = self._menu(anchor=(5, 1), bounds=(1, 40), cols=300)
        unbounded, _ = self._menu(anchor=(5, 1), bounds=None, cols=300)
        centered = _modal._modal_place(300, 24, frame_w, 6,
                                       placement='center', anchor=None)
        self.assertEqual(unbounded, centered.left)   # full-screen centered
        self.assertNotEqual(bounded, unbounded)      # bound moved it


if __name__ == '__main__':
    unittest.main()
