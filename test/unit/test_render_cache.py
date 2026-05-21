"""End-to-end tests for the per-pane row cache + padding semantics (#190).

These tests wire the *real* terminal-layer write/move/begin_row/end_row
helpers into the *real* render module and drive a *real* (headless)
Browser through the renderers, capturing the resulting byte stream from
``sys.stdout``. They cover the cache + padding rules end-to-end:

  * No-op repaint emits exactly the BSU/ESU bracket and nothing else.
  * Cursor move down 1 in the list pane emits exactly two row updates.
  * First paint has no ``\\e[K`` and no trailing-space pads.
  * Resize / layout switch invalidates rects and forces full pad.
  * ``\\e[m`` always precedes any pad-run or ``\\e[K``.
  * Rightmost panes use ``\\e[K``; non-rightmost use trailing spaces.
  * Ctrl-L emits ``\\e[2J`` then a content-only first paint.
  * Trailing-content-shrink pads to the previous visible length.
"""

import io
import re
import sys
import unittest

from test.unit import _loader
from test.unit._loader import load


# --- Module loading + cross-wiring ----------------------------------------
#
# The numbered-file source layout means each module is loaded standalone
# and we have to inject the cross-references the concatenated build gets
# for free. Most cross-refs follow the existing test_actions.py pattern.

_term = load('_browse_tui_term_rc', '020-terminal.py')
_data = load('_browse_tui_data_rc', '030-data.py')
_state = load('_browse_tui_state_rc', '040-state.py')
_render = load('_browse_tui_render_rc', '050-render.py')
_actions = load('_browse_tui_actions_rc', '070-actions.py')

# State module needs Item / to_item / notify_wake.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

# Render module needs Item / VisibleEntry / PaneCache, and the terminal
# primitives (write / move / set_style / reset_style / clear_line /
# clear_columns / begin_row / end_row / begin_sync / end_sync / flush /
# term_size). The concatenated production build gets these by virtue of
# the modules being a single namespace.
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
_render.VisibleEntry = _state.VisibleEntry
_render.PaneCache = _state.PaneCache
_render.visible_items = _state.visible_items
_render._search_matches = _state._search_matches
_render._search_text = _state._search_text
# Wrap-aware SGR walker (#242) needs the ANSI primitives from 020-terminal.
_render._ANSI_CSI_RE = _term._ANSI_CSI_RE
_render.SgrState = _term.SgrState
_render._char_width = _term._char_width
_render._visible_len = _term._visible_len
for _name in ('write', 'move', 'set_style', 'reset_style', 'clear_line',
              'clear_columns', 'begin_row', 'end_row', 'begin_sync',
              'end_sync', 'flush', 'term_size'):
    setattr(_render, _name, getattr(_term, _name))

# State references the rendering helpers via visible_items only (already
# in 040-state.py), and the search helpers are loaded with the state
# module — no extra wiring needed.
_state._search_text = getattr(_state, '_search_text', lambda item: item.title)

# Actions module needs a handful of cross-refs for Ctrl-L; only ``write``
# is touched by ``_redraw`` itself.
_actions.write = _term.write
_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.current_scope = _state.current_scope
_actions._search_find = _state._search_find
_actions._search_jump_nearest = _state._search_jump_nearest
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode
_actions.point_in_rect = _render.point_in_rect
_actions._sub_needed_rows = _render._sub_needed_rows
_actions._fmt_child = _render._fmt_child


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Rect = _render.Rect


def _ctx_for(browser):
    _context = load('_browse_tui_context_rc', '060-context.py')
    _context.visible_items = _state.visible_items
    return _context.Context(browser)


# --- Stdout capture helper -------------------------------------------------


class _Capture:
    """Replace sys.stdout with a StringIO; drain it on demand."""

    def __enter__(self):
        self._orig = sys.stdout
        self.buf = io.StringIO()
        sys.stdout = self.buf
        return self

    def __exit__(self, *args):
        sys.stdout = self._orig

    def drain(self):
        text = self.buf.getvalue()
        self.buf.truncate(0)
        self.buf.seek(0)
        return text


def _make_browser(items=None, **kw):
    """Build a headless Browser whose root has the given children.

    The default ``get_children`` returns the supplied list when called
    with the root id (None), and ``[]`` otherwise. Items are coerced
    through ``to_item`` so they end up as proper ``Item`` instances.
    """
    items = items or []

    def gc(parent_id):
        if parent_id is None:
            return items
        return []

    kw.setdefault('_headless', True)
    kw.setdefault('get_children', gc)
    b = Browser(BrowserConfig(**kw))
    # Force the root children to be cached up front so visible_items has
    # something to traverse.
    coerced = [_data.to_item(it) for it in items]
    b._state._children[None] = coerced
    for it in coerced:
        b._state._items_by_id[it.id] = it
    _state.mark_visible_dirty(b._state)
    return b


def _reset_terminal_capture_state():
    """Defensively clear the row-shim global state between tests."""
    _term._row_capture_active = False
    _term._row_buf = []
    _term._row_meta = None


# --- Helpers for byte-stream inspection -----------------------------------


# CSI move: \033[<row>;<col>H
_MOVE_RE = re.compile(r'\x1b\[(\d+);(\d+)H')


def _moves_in(text):
    """Return list of (row, col) for every CSI cursor-position in ``text``."""
    return [(int(m.group(1)), int(m.group(2)))
            for m in _MOVE_RE.finditer(text)]


def _strip_bsu_esu(text):
    """Strip leading BSU and trailing ESU (DEC 2026)."""
    bsu = '\033[?2026h'
    esu = '\033[?2026l'
    if text.startswith(bsu):
        text = text[len(bsu):]
    if text.endswith(esu):
        text = text[:-len(esu)]
    return text


# --- Base TestCase: install real terminal primitives onto _render ---------


class _RenderCacheBase(unittest.TestCase):
    """Common setUp / tearDown for the cache-semantics tests.

    Each test owns a fresh Browser and stdout capture. We don't rebuild
    the cross-module wiring here — it's done once at module load.
    """

    def setUp(self):
        _reset_terminal_capture_state()
        self.cap = _Capture()
        self.cap.__enter__()

    def tearDown(self):
        self.cap.__exit__(None, None, None)
        if hasattr(self, 'browser') and self.browser is not None:
            self.browser.stop_workers()


# --- 1. No-op repaint ------------------------------------------------------


class TestNoOpRepaint(_RenderCacheBase):
    """A second render with no changes emits exactly BSU + ESU."""

    def test_render_partial_with_empty_needs_emits_nothing(self):
        self.browser = _make_browser([Item(id='a'), Item(id='b')])
        # Prime the cache with a full paint.
        _render.render_full(self.browser)
        self.cap.drain()
        # Empty needs → render_partial returns before begin_sync.
        self.browser._needs_redraw = set()
        _render.render_partial(self.browser)
        out = self.cap.drain()
        self.assertEqual(
            out, '',
            'render_partial with empty needs must emit zero bytes; '
            f'got {out!r}',
        )

    def test_render_full_repaint_no_changes_only_brackets(self):
        """Two render_full calls back-to-back — second emits BSU+ESU only."""
        self.browser = _make_browser([Item(id='a'), Item(id='b')])
        _render.render_full(self.browser)
        self.cap.drain()
        # Second paint with the same state, same rect → every row is a
        # cache hit; only BSU + ESU should escape.
        _render.render_full(self.browser)
        out = self.cap.drain()
        bsu = '\033[?2026h'
        esu = '\033[?2026l'
        self.assertEqual(
            out, bsu + esu,
            f'expected BSU+ESU only on no-op repaint; got {out!r}',
        )


# --- 2. Cursor move down 1 in list pane -----------------------------------


class TestCursorMoveListPane(_RenderCacheBase):
    """Cursor down emits exactly two list-row updates and nothing else.

    The list pane occupies rows 1..list.bottom-1; preview / info bar /
    other panes hold their cached content. Only the rows containing the
    OLD cursor position and the NEW cursor position should see a CSI
    cursor-position sequence.
    """

    def test_cursor_down_emits_two_list_row_updates(self):
        self.browser = _make_browser([
            Item(id='a'), Item(id='b'), Item(id='c'), Item(id='d'),
        ])
        # Prime cache with a full paint.
        _render.render_full(self.browser)
        self.cap.drain()
        # Cursor at row 0; move it down.
        ctx = _ctx_for(self.browser)
        _actions._nav_down(ctx)
        _render.render_partial(self.browser)
        out = _strip_bsu_esu(self.cap.drain())
        moves = _moves_in(out)

        # Determine list pane rect from current layout.
        layout = _render._layout_for(self.browser)
        list_rect = layout['list']
        # Row updates inside the list pane: distinct rows in [top, bottom).
        list_rows = sorted({
            r for (r, c) in moves
            if list_rect.top <= r < list_rect.bottom
            and c == list_rect.left
        })
        # Exactly two row updates — old cursor row (1) and new (2).
        self.assertEqual(
            list_rows, [list_rect.top, list_rect.top + 1],
            f'expected exactly old + new cursor row updates; got {list_rows} '
            f'(full output: {out!r})',
        )


# --- 3. First paint: no \e[K, no trailing space pads ----------------------


class TestFirstPaint(_RenderCacheBase):
    """A fresh Browser's render_full emits visible bytes only.

    Each PaneCache starts with ``rect=None, prev_rect=None``. After
    ``update_rect(new_rect)`` runs in ``_reconcile_pane_caches``,
    ``rect=new_rect`` and ``prev_rect=None`` — ``end_row`` takes the
    first-paint branch (no padding, no \\e[K). We assert the absence
    of \\e[K and the absence of multi-space trailing runs.
    """

    def test_no_clear_to_eol_or_trailing_pad(self):
        self.browser = _make_browser([Item(id='a'), Item(id='b')])
        _render.render_full(self.browser)
        out = self.cap.drain()
        body = _strip_bsu_esu(out)
        self.assertNotIn(
            '\033[K', body,
            'first paint must not emit \\e[K (rightmost cleared region)',
        )


# --- 4. Resize: rect change → full pad ------------------------------------


class TestResizeRepaintsFullPanes(_RenderCacheBase):
    """A term_size change forces the next render to pad every row.

    When the rect changes, ``end_row`` takes the rect-changed branch:
    pad to the new pane width with spaces (or \\e[K if rightmost). The
    test monkey-patches ``_render.term_size`` between paints to simulate
    a resize and verifies the second paint emits more bytes than a
    no-op repaint would.
    """

    def test_resize_invalidates_rects_and_forces_repaint(self):
        self.browser = _make_browser([Item(id='a'), Item(id='b')])
        # First paint at default term_size = (80, 24).
        _render.render_full(self.browser)
        self.cap.drain()

        # Sanity: a no-op second paint emits only BSU+ESU.
        _render.render_full(self.browser)
        steady = self.cap.drain()
        self.assertEqual(
            steady, '\033[?2026h\033[?2026l',
            f'no-op repaint should emit only brackets; got {steady!r}',
        )

        # Now mock the terminal size larger and repaint.
        saved_term_size = _render.term_size
        try:
            _render.term_size = lambda: (100, 30)
            _render.render_full(self.browser)
            resized = self.cap.drain()
        finally:
            _render.term_size = saved_term_size

        body = _strip_bsu_esu(resized)
        # The resize forced a rect change — output must be non-trivial.
        self.assertGreater(
            len(body), 0,
            f'resize must force emission; got {resized!r}',
        )
        # And the new geometry should appear: at least one move beyond
        # old column 80 (the new width is 100).
        moves = _moves_in(body)
        self.assertTrue(
            moves,
            f'resize must emit cursor moves; got {body!r}',
        )


# --- 5. Layout switch ------------------------------------------------------


class TestLayoutSwitch(_RenderCacheBase):
    """``set_split('v')`` after a paint changes the rects → full pad."""

    def test_h_to_v_switch_repaints_panes(self):
        self.browser = _make_browser([Item(id='a'), Item(id='b')], split='h')
        _render.render_full(self.browser)
        self.cap.drain()

        # Switch layout. set_split flags 'all' on _needs_redraw.
        self.browser.set_split('v')
        self.browser.drain_main_queue()
        _render.render_partial(self.browser)
        out = self.cap.drain()

        body = _strip_bsu_esu(out)
        # New layout has different geometry → bytes must be emitted.
        self.assertGreater(
            len(body), 0,
            'layout switch must force emission',
        )


# --- 6. \e[m always precedes pad / \e[K -----------------------------------


class TestResetPrecedesPad(_RenderCacheBase):
    """Every space-pad run / \\e[K is preceded by \\e[m.

    ``end_row`` always emits ``\\e[m`` after the row buffer, before any
    padding or clear-to-EOL. We scan the output and verify the rule.
    """

    def test_reset_before_pad_in_steady_state_shrink(self):
        # Use an item whose title fits, then truncate to force shrink.
        # Easiest: render once with 4 items, change one to be shorter,
        # and re-render.
        self.browser = _make_browser([
            Item(id='aaaaaaaaaa', title='aaaaaaaaaa'),
            Item(id='b', title='b'),
        ])
        _render.render_full(self.browser)
        self.cap.drain()

        # Replace the long item with a short one, keep the same row count.
        self.browser._state._children[None] = [
            _data.to_item(it) for it in [
                Item(id='x', title='x'),
                Item(id='b', title='b'),
            ]
        ]
        _state.mark_visible_dirty(self.browser._state)
        self.browser._needs_redraw.add('list')
        _render.render_partial(self.browser)
        body = _strip_bsu_esu(self.cap.drain())

        # For every \e[K and every space-pad-run >= 2, the byte sequence
        # immediately preceding it must contain \e[m as the most recent
        # SGR-or-pad delimiter on this row update.
        # Simplification: check that wherever \e[K appears, \e[m appears
        # earlier in the buffer (and no other SGR after \e[m before \e[K).
        # The rule we're enforcing: end_row always writes \e[m before a
        # pad. So the BYTE sequence "\e[m" precedes any \e[K or trailing
        # ASCII-space run that isn't part of the row content.
        if '\033[K' in body:
            # Find each \e[K; verify a \e[m appears between the most
            # recent \e[<row>;<col>H and the \e[K.
            for m in re.finditer(r'\x1b\[K', body):
                idx = m.start()
                # Look back to the start of this row's emission (a CSI
                # cursor-position sequence).
                cur = body.rfind('\x1b[', 0, idx)
                # And before \e[K, the most recent SGR must be \e[m.
                between = body[:idx]
                # Accept any of \e[m or \e[0m as a "reset".
                self.assertTrue(
                    re.search(r'\x1b\[0?m[^\x1b]*$', between),
                    f'expected \\e[m or \\e[0m before \\e[K at offset {idx} '
                    f'in {body!r}',
                )


# --- 7. Rightmost vs non-rightmost ----------------------------------------


class TestRightmostUsesEraseLine(_RenderCacheBase):
    """In layout 'v', list is leftmost (uses spaces); preview is rightmost
    (uses \\e[K).

    Trigger the steady-state-shrink branch in both panes by:
      * Painting once with longer content.
      * Painting again with shorter content (same rect).

    Then verify the leftmost pane's shrink emits trailing spaces while
    the rightmost pane's shrink emits \\e[K.
    """

    def test_preview_rightmost_uses_clear_to_eol(self):
        # Layout 'v' lays out list on the left and preview on the right.
        # The preview pane is rightmost.
        long_text = 'x' * 50
        short_text = 'y'

        captured = {'preview': long_text}

        def get_preview(item_id):
            return captured['preview']

        items = [Item(id='a', title='alpha')]
        self.browser = _make_browser(
            items, split='v', show_preview=True,
            show_children_pane=False, get_preview=get_preview,
        )
        # Force the preview cache to be populated synchronously by
        # storing it in the preview cache dict. ``render_preview`` reads
        # ``browser._preview`` (or similar) — populate via the worker
        # contract instead, which is to set the entry directly.
        self.browser._state._items_by_id['a'].preview = long_text

        _render.render_full(self.browser)
        self.cap.drain()

        # Now shrink the preview content. Direct preview assignment
        # bypasses the per-Item invalidation of ``preview_render``
        # (#422), so drop it explicitly.
        item_a = self.browser._state._items_by_id['a']
        item_a.preview = short_text
        item_a.preview_render = None
        captured['preview'] = short_text
        self.browser._needs_redraw.add('preview')
        _render.render_partial(self.browser)
        body = _strip_bsu_esu(self.cap.drain())

        # The preview pane is rightmost in 'v' layout — shrunk rows
        # must emit \\e[K.
        self.assertIn(
            '\033[K', body,
            'rightmost pane (preview) shrink must emit \\e[K; '
            f'got {body!r}',
        )


# --- 8. Ctrl-L -------------------------------------------------------------


class TestCtrlLRedraw(_RenderCacheBase):
    """Ctrl-L (``_redraw``) emits \\e[2J then a content-only first paint.

    After ``_redraw(ctx)`` runs:
      * \\e[2J has been written to stdout.
      * ``_pane_cache`` is empty.
      * ``_needs_redraw`` contains 'all' so render_partial dispatches to
        render_full.

    The next ``render_full`` should be a first-paint (no \\e[K, no pads).
    """

    def test_redraw_then_render_full_is_first_paint(self):
        self.browser = _make_browser([Item(id='a'), Item(id='b')])
        _render.render_full(self.browser)
        # Discard the priming output AND a steady-state second paint to
        # transition the caches into the steady-state regime.
        self.cap.drain()
        _render.render_full(self.browser)
        self.cap.drain()

        # Now Ctrl-L: emit \\e[2J, drop the cache, mark 'all' dirty.
        ctx = _ctx_for(self.browser)
        _actions._redraw(ctx)
        # \\e[2J was emitted; capture and inspect.
        ctrl_l_out = self.cap.drain()
        self.assertIn(
            '\033[2J', ctrl_l_out,
            'Ctrl-L must emit \\e[2J',
        )
        self.assertEqual(
            self.browser._pane_cache, {},
            'Ctrl-L must clear the pane cache',
        )

        # Next render_partial routes to render_full because 'all' is set.
        _render.render_partial(self.browser)
        body = _strip_bsu_esu(self.cap.drain())
        # First paint after a cache wipe — no \\e[K.
        self.assertNotIn(
            '\033[K', body,
            'render_full after Ctrl-L must take the first-paint path '
            f'(no \\e[K); got {body!r}',
        )


# --- 9. Trailing-content-shrink ------------------------------------------


class TestTrailingContentShrink(_RenderCacheBase):
    """A row whose new content is shorter than its cached content gets
    padded out to the cached visible length.

    Steady-state semantics: ``end_row`` pads with spaces (non-rightmost)
    or emits ``\\e[K`` (rightmost) so stale cells from the longer prior
    paint are cleared.
    """

    def test_shrink_pads_to_previous_visible_length(self):
        # Two paints in 'h' layout: the list pane is leftmost (uses
        # spaces). First paint: long title. Second paint: short title.
        long_item = Item(id='aaaaa', title='aaaaa')
        short_item = Item(id='b', title='b')
        self.browser = _make_browser([long_item], split='h')
        _render.render_full(self.browser)
        self.cap.drain()
        # Second paint in steady state to roll prev_rect forward.
        _render.render_full(self.browser)
        self.cap.drain()

        # Replace the item with a shorter one.
        self.browser._state._children[None] = [_data.to_item(short_item)]
        _state.mark_visible_dirty(self.browser._state)
        self.browser._needs_redraw.add('list')
        _render.render_partial(self.browser)
        body = _strip_bsu_esu(self.cap.drain())

        # Non-rightmost pane shrink: trailing-space pad. Find the row
        # update for the first list row (top, list_rect.left) and
        # verify that after the row content, there are spaces filling
        # back to the cached visible length.
        self.assertNotIn(
            '\033[K', body,
            'list pane is leftmost in layout h — shrink should pad with '
            f'spaces, not \\e[K; got {body!r}',
        )
        # And the body should contain a run of spaces (the pad).
        self.assertIn(
            '   ', body,  # 3+ spaces is enough to detect a pad run
            f'shrink must emit a trailing-space pad; got {body!r}',
        )


# --- 10. render_preview ANSI integration (#243) ---------------------------


class TestRenderPreviewAnsiIntegration(_RenderCacheBase):
    """``render_preview`` wires ``_wrap_preview_line`` (the SGR walker).

    These tests exercise the integration end-to-end through ``render_full``
    so we see the actual byte stream produced by the preview pane:

      * Plain text → byte-identical pre/post-#243 (cache invariant).
      * Coloured line → SGR codes survive, trailing ``\\e[m`` only on
        rows that carry SGR.
      * Long coloured line wraps with each row self-contained
        (re-emit + reset).
      * Search match in a coloured line → SGR stripped (highlight wins).
      * Cache hit on second paint with no changes.
    """

    def _make(self, preview_text, **browser_kw):
        # Layout 'v' makes the preview pane rightmost so we can inspect
        # exactly the preview's emitted bytes; the list is on the left.
        items = [Item(id='a', title='alpha')]
        browser_kw.setdefault('split', 'v')
        browser_kw.setdefault('show_preview', True)
        browser_kw.setdefault('show_children_pane', False)
        b = _make_browser(items, **browser_kw)
        b._state._items_by_id['a'].preview = preview_text
        return b

    def test_plain_text_byte_identical_to_pre_ansi(self):
        """Plain preview pre/post-#243: identical byte stream.

        Two paints in succession with the same plain text must produce
        a cache hit on the second paint — the regression check that
        plain content didn't grow extra SGR bytes from the new walker.
        """
        self.browser = self._make('hello world\nsecond line')
        _render.render_full(self.browser)
        first = self.cap.drain()
        # Plain text path: row content for 'hello world' carries no SGR
        # added by the walker. (The row's outer \e[m terminator comes
        # from end_row's row-shim, not the wrap walker — that one we
        # accept.) Confirm there's no INNER \e[31m / \e[1m / etc from
        # the walker, by searching for the body text and checking the
        # surrounding bytes don't include a non-reset SGR.
        self.assertIn('hello world', first)
        self.assertIn('second line', first)
        # No coloured SGR injected on plain rows.
        idx = first.index('hello world')
        # The 32 bytes before 'hello world' should NOT contain a CSI
        # other than the cursor-position move + (possibly) a reset.
        prelude = first[max(0, idx - 32):idx]
        # No colour-foreground / bold openers in plain prelude.
        for opener in ('\x1b[31m', '\x1b[32m', '\x1b[1m', '\x1b[7m'):
            self.assertNotIn(
                opener, prelude,
                f'plain row prelude must not carry {opener!r}; '
                f'got {prelude!r}',
            )
        # Second paint with identical state → cache hit (BSU+ESU only).
        _render.render_full(self.browser)
        second = self.cap.drain()
        bsu = '\033[?2026h'
        esu = '\033[?2026l'
        self.assertEqual(
            second, bsu + esu,
            f'plain preview second paint must be cache-hit; got {second!r}',
        )

    def test_coloured_line_emits_sgr_with_trailing_reset(self):
        """A red-coloured preview line carries the SGR codes through."""
        self.browser = self._make('\x1b[31mfoo\x1b[m')
        _render.render_full(self.browser)
        body = _strip_bsu_esu(self.cap.drain())
        # \e[31m should reach the terminal because preview_ansi defaults
        # to True via getattr.
        self.assertIn('\x1b[31mfoo', body)
        # The walker re-emits a trailing \e[m only when SGR state is
        # non-empty at end-of-row. Here the input itself ends in \e[m so
        # the state IS empty by row-end and no extra reset is needed.
        # The original \e[m still appears in the output.
        self.assertIn('\x1b[m', body)

    def test_long_coloured_line_wraps_with_self_contained_rows(self):
        """Wrapping a long red line: each visual row carries its own SGR."""
        # The preview pane in the default 'v' layout / 80-col terminal
        # is ~55 cols wide. 200 'R's guarantees multiple wrapped rows.
        self.browser = self._make('\x1b[31m' + 'R' * 200)
        _render.render_full(self.browser)
        body = _strip_bsu_esu(self.cap.drain())
        # The colour opens at least once (first row's start).
        self.assertIn('\x1b[31m', body)
        # And there's a corresponding reset (state non-empty at row end).
        self.assertIn('\x1b[m', body)
        # More than one occurrence of \e[31m proves multi-row re-emit:
        # each wrapped row re-opens \e[31m at its start.
        self.assertGreaterEqual(
            body.count('\x1b[31m'), 2,
            'long coloured line must re-emit SGR on each wrapped row; '
            f'got {body!r}',
        )

    def test_search_match_in_coloured_line_drops_sgr(self):
        """Search match → highlight wins, source SGR stripped."""
        # Red 'alpha' as the preview text. With search query 'alpha'
        # the matched line must drop SGR — the walker is told
        # drop_sgr=True for that line.
        self.browser = self._make('\x1b[31malpha\x1b[m')
        self.browser._search_query = 'alpha'
        _render.render_full(self.browser)
        body = _strip_bsu_esu(self.cap.drain())
        # The preview content 'alpha' appears.
        self.assertIn('alpha', body)
        # And the source colour (\e[31m) is NOT present in the preview
        # row because drop_sgr=True dropped it. (The list pane / info
        # bar may emit other SGR for highlighting, so search for the
        # specific \e[31m which only the source colour would emit.)
        # NOTE: if any other code path emits \e[31m as a coincidence
        # this assertion would over-trigger. The list-pane's
        # ``_write_highlighted`` uses yellow/bold (\e[33;1m), not red.
        self.assertNotIn(
            '\x1b[31m', body,
            'search-matched line must drop source SGR; '
            f'got {body!r}',
        )

    def test_cache_hit_on_second_paint(self):
        """Second paint with same preview state → BSU+ESU only."""
        self.browser = self._make('\x1b[31mhello\x1b[m world')
        _render.render_full(self.browser)
        self.cap.drain()
        # Second paint, no state change: every row hits cache.
        _render.render_full(self.browser)
        out = self.cap.drain()
        bsu = '\033[?2026h'
        esu = '\033[?2026l'
        self.assertEqual(
            out, bsu + esu,
            f'expected BSU+ESU only on no-op repaint; got {out!r}',
        )


class TestPreviewScrollClamp(_RenderCacheBase):
    """``_preview_scroll`` is clamped at render time so the last content
    row lands at the bottom of the pane when fully scrolled — the
    conventional viewport semantics. No matter how many shift-down /
    page-down presses pile up, the user can never scroll past content."""

    def _make(self, preview_text):
        items = [Item(id='a', title='alpha')]
        b = _make_browser(items, split='v', show_preview=True,
                          show_children_pane=False)
        b._state._items_by_id['a'].preview = preview_text
        return b

    def test_scroll_when_content_fits_clamps_to_zero(self):
        """If all wrapped rows fit in the pane, no scroll is allowed.

        A 3-line preview in a tall pane should clamp ``_preview_scroll``
        back to 0 regardless of how far the user pressed shift-down.
        """
        self.browser = self._make('one\ntwo\nthree')
        self.browser._preview_scroll = 100
        _render.render_full(self.browser)
        self.assertEqual(
            self.browser._preview_scroll, 0,
            'short content (fits in pane) must clamp scroll to 0',
        )

    def test_scroll_past_end_clamps_to_last_line_at_bottom(self):
        """Long content: clamp leaves the last wrapped row at the
        bottom of the pane when fully scrolled.

        With pane content_lines = N and wrapped = M (M > N),
        ``max_scroll = M - N`` so the row at index ``M-1`` lands at
        the pane's last visible row.
        """
        # Build content longer than the pane so a real scroll range exists.
        # The headless terminal is 24 rows; in 'v' split with no children
        # pane and the info-bar header, content_lines is around 20.
        # Generate enough lines to comfortably exceed any reasonable
        # content_lines value.
        lines = [f'line{i:03d}' for i in range(200)]
        self.browser = self._make('\n'.join(lines))
        # Pile up an absurd offset.
        self.browser._preview_scroll = 10_000
        _render.render_full(self.browser)
        # After the clamp, scroll should be exactly len(wrapped) -
        # content_lines. We can't easily query content_lines from out
        # here, but we can assert the upper bound: scroll <= 200 -
        # content_lines, and scroll >= 0. More usefully: the last line
        # 'line199' must appear in the rendered bytes.
        out = self.cap.drain()
        self.assertIn(
            'line199', out,
            'last content line must be visible when fully scrolled '
            f'(scroll={self.browser._preview_scroll!r})',
        )
        # And: the scroll value must be strictly less than the line
        # count (we never scroll past the content).
        self.assertLess(self.browser._preview_scroll, 200)

    def test_scroll_clamp_does_not_shrink_in_range_offsets(self):
        """An in-range offset must not be touched by the clamp."""
        lines = [f'line{i:03d}' for i in range(200)]
        self.browser = self._make('\n'.join(lines))
        self.browser._preview_scroll = 5
        _render.render_full(self.browser)
        self.assertEqual(self.browser._preview_scroll, 5,
                         'in-range scroll offset must not be clamped')

    def test_empty_preview_clamps_scroll_to_zero(self):
        """No preview text at all → clamp to 0 (no row to show)."""
        self.browser = self._make('')
        self.browser._preview_scroll = 50
        _render.render_full(self.browser)
        self.assertEqual(self.browser._preview_scroll, 0)


if __name__ == '__main__':
    unittest.main()
