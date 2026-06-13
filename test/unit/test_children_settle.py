"""Children pane settles with the preview (#959).

The children pane renders ``Browser._children_displayed_id`` — not the
live cursor. The main loop's per-tick rule
(``_advance_children_displayed_id``) advances it to the cursored row
only once the preview visit has settled (``_preview_visit_delivered``),
immediately for meta rows, and never for pending placeholders / no
cursor row. The invariant under test: the children pane always
describes the same row the preview pane does — old/old before settle;
new/new (or new + ``⧗ loading…`` hint) in the settle paint; never
new-preview/old-children.

Covers:

  * Hold-then-advance: a cursor move leaves the displayed id (and the
    painted pane) on the old branch until the preview delivery lands;
    the advance flags a full repaint exactly once per settle.
  * Both delivery orderings — children faster (cached by settle time:
    one paint swaps preview + pane) and children slower (the settle
    paint shows the new preview + the loading hint for the NEW branch,
    which grows on delivery). No new wiring: the hint is the existing
    cached-is-``None`` rendering, the growth the existing delivery
    repaint.
  * Leaf-hide deferred to settle; pending placeholder and empty
    visible list hold; meta rows advance immediately.
  * ``show_preview=False`` degrade: the per-visit bit keeps its
    ``True`` init (the preview helper returns before clearing it), so
    the rule advances every tick — the pre-#959 immediate pane.
  * Displayed parent removed mid-hold → resolves to ``None`` → pane
    hides honestly; the next settle re-advances.
  * ``clear_children`` on the settled cursor row → hint reappears
    (displayed id unchanged, cache ``None``) and the pane regrows on
    the re-delivery.

Deliveries are injected synchronously (``_deliver_preview`` /
``set_children`` + the main-loop drain helpers); no live worker
threads, so orderings are deterministic — the same discipline as
test_preview_stale_hold.py. ``preview_debounce`` is pinned far past
the assertion window everywhere so a future ``start_workers`` in a
helper can't let the real worker race the injected settles. Real
worker timing is exercised by the UI/pty layer.

See docs/superpowers/specs/2026-06-12-preview-flicker-design.md (§D).
"""

import io
import unittest

from test.unit._loader import load


# --- Module loading + cross-wiring ----------------------------------------

_term = load('_browse_tui_term_cs', '020-terminal.py')
_data = load('_browse_tui_data_cs', '030-data.py')
_state = load('_browse_tui_state_cs', '040-state.py')
_render = load('_browse_tui_render_cs', '050-render.py')
_context = load('_browse_tui_context_cs', '060-context.py')
_actions = load('_browse_tui_actions_cs', '070-actions.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
_render.DEFAULT_HINT = _state.DEFAULT_HINT
_render.VisibleEntry = _state.VisibleEntry
_render.PaneCache = _state.PaneCache
_render.visible_items = _state.visible_items
_render._search_matches = _state._search_matches
_render._search_text = _state._search_text
_render._ANSI_CSI_RE = _term._ANSI_CSI_RE
_render.SgrState = _term.SgrState
_render._char_width = _term._char_width
_render._visible_len = _term._visible_len
_render._normalize_content = _state._normalize_content
for _name in ('write', 'move', 'set_style', 'reset_style', 'clear_line',
              'clear_columns', 'begin_row', 'end_row', 'begin_sync',
              'end_sync', 'flush', 'term_size'):
    setattr(_render, _name, getattr(_term, _name))
for _name in ('_TAG_STYLE', '_id_visible', '_ID_COLOR', '_MARKER_COLOR',
              'cell_width'):
    setattr(_state, _name, getattr(_render, _name))
# Children-pane layout sizing: ``_layout_for`` routes cached children
# through ``Browser.children_grid_layout`` (state) which calls the
# render-layer ``_sub_layout`` and returns the data-layer namedtuple.
_state._sub_layout = _render._sub_layout
_state._sub_total_rows = _render._sub_total_rows
_state.ChildrenGridLayout = _data.ChildrenGridLayout
_render.ChildrenGridLayout = _data.ChildrenGridLayout

_context.visible_items = _state.visible_items
_render.default_actions = _actions.default_actions


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig


def _make_browser(specs, **kw):
    """Non-headless Browser with root items per ``specs``.

    Each spec is ``(id, has_children)``; branches get two children
    ``<id>1``/``<id>2`` pre-registered in ``_items_by_id`` but cached
    only when ``cache=True`` rides in a third slot. Non-headless so
    ``_layout_for`` sizes the children pane for real; rendering goes
    through a captured writer (no terminal needed).

    No previews are pre-cached and no workers are started — each visit
    settles only when the test injects ``_deliver_preview``. The 5s
    debounce pin keeps any future live worker far past the assertion
    window (#939 discipline).
    """
    kw.setdefault('_headless', False)
    kw.setdefault('get_preview', lambda _id: '')
    kw.setdefault('get_children', lambda _pid, *, reload=False: [])
    kw.setdefault('preview_debounce', 5.0)
    kw.setdefault('show_preview', True)
    kw.setdefault('show_children_pane', True)
    kw.setdefault('split', 'h')
    b = Browser(BrowserConfig(**kw))
    roots = []
    for spec in specs:
        id_, has_children = spec[0], spec[1]
        cache = spec[2] if len(spec) > 2 else False
        item = _data.to_item(Item(id=id_, has_children=has_children))
        b._state._items_by_id[id_] = item
        roots.append(item)
        if has_children:
            kids = [_data.to_item(Item(id=f'{id_}{n}')) for n in (1, 2)]
            for kid in kids:
                b._state._items_by_id[kid.id] = kid
            if cache:
                b._state._children[id_] = kids
    b._state._children[None] = roots
    _state.mark_visible_dirty(b._state)
    return b


def _deliver_children(b, parent_id):
    """Inject a children delivery for ``parent_id`` (worker stand-in).

    Routes through the public thread-safe ``set_children`` lane and the
    main-loop drain (``apply_children_results``), so the delivery flags
    the same redraws a live fetch would.
    """
    kids = [b._state._items_by_id[f'{parent_id}{n}'] for n in (1, 2)]
    b.set_children(parent_id, kids)
    b.apply_children_results()


def _tick(b):
    """One main-loop maintenance pass, in ``Browser.run`` order."""
    b.drain_main_queue()
    b.apply_children_results()
    b._update_preview_for_cursor()
    b._update_children_for_cursor()
    b._flag_preview_loading_if_changed()
    b._advance_children_displayed_id()


def _move_cursor(b, index):
    b._state.cursor = index
    _tick(b)


def _settle(b, id_, text=None):
    """End the cursored visit the way the worker would, then tick."""
    b._deliver_preview(id_, f'pv-{id_}' if text is None else text)
    _tick(b)


def _paint(b):
    """One render pass, exactly as the main loop dispatches it."""
    orig = _term._tty_writer
    _term._tty_writer = io.StringIO()
    try:
        if 'all' in b._needs_redraw:
            _render.render_full(b)
        elif b._needs_redraw:
            _render.render_partial(b)
    finally:
        _term._tty_writer = orig


def _children_pane_text(b):
    """Content rows of the children pane as last painted ('' if hidden).

    Reads the differential renderer's row cache — it always holds what
    is on screen, even when a repeat paint emitted zero bytes. Row 0 of
    the 'h'-layout children rect is the ``Children`` header.
    """
    cache = b._pane_cache.get('children')
    if cache is None or cache.rect is None:
        return ''
    return '\n'.join(
        entry[1] if entry is not None else '' for entry in cache.lines[1:]
    )


def _preview_pane_text(b):
    cache = b._pane_cache['preview']
    return '\n'.join(
        entry[1] if entry is not None else '' for entry in cache.lines[1:]
    )


def _children_rect(b):
    return _render._layout_for(b)['children']


class _SettleBase(unittest.TestCase):
    """Pin terminal geometry; stop (never-started) workers on exit."""

    def setUp(self):
        self._saved_term_size = _term.term_size
        _term.term_size = lambda: (100, 30)
        _render.term_size = _term.term_size
        self.browser = None

    def tearDown(self):
        _term.term_size = self._saved_term_size
        _render.term_size = self._saved_term_size
        if self.browser is not None:
            self.browser.stop_workers()

    def _settled_on_a(self, specs=None, **kw):
        """Browser settled + painted on branch 'A' (children cached)."""
        b = _make_browser(
            specs or [('A', True, True), ('B', True), ('L', False)], **kw)
        self.browser = b
        _move_cursor(b, 0)
        _settle(b, 'A')
        _paint(b)
        self.assertEqual(b._children_displayed_id, 'A')
        self.assertIn('A1', _children_pane_text(b))
        self.assertIn('pv-A', _preview_pane_text(b))
        return b


# --- The advance rule ------------------------------------------------------


class TestAdvanceRule(_SettleBase):

    def test_holds_until_settle_then_advances_and_flags_all(self):
        b = self._settled_on_a()
        _move_cursor(b, 1)
        # Visit open, nothing delivered — the displayed id holds.
        self.assertFalse(b._preview_visit_delivered)
        self.assertEqual(b._children_displayed_id, 'A')

        b._needs_redraw.clear()
        _settle(b, 'B')
        self.assertEqual(b._children_displayed_id, 'B')
        # Geometry derives from the displayed item — an id change can
        # reshape the layout, so the advance flags a full repaint (the
        # same 'all' a children delivery flags).
        self.assertIn('all', b._needs_redraw)

    def test_advance_flags_once_per_settle_not_per_tick(self):
        b = self._settled_on_a()
        b._needs_redraw.clear()
        _tick(b)
        _tick(b)
        self.assertNotIn('all', b._needs_redraw,
                         'parked cursor must not re-flag full repaints')

    def test_meta_row_advances_immediately(self):
        b = self._settled_on_a(
            [('A', True, True), ('M', False), ('B', True)])
        b._state._items_by_id['M'].meta = True
        _state.mark_visible_dirty(b._state)
        _move_cursor(b, 1)
        # No preview is ever requested for meta rows (the visit bit is
        # False and stays so) — yet the pane must match the preview,
        # which paints meta rows honestly right away.
        self.assertFalse(b._preview_visit_delivered)
        self.assertEqual(b._children_displayed_id, 'M')
        # A meta divider is a leaf — the pane hides with it.
        self.assertIsNone(_children_rect(b))

    def test_pending_placeholder_holds(self):
        b = self._settled_on_a()
        # Expand B without caching its children — visible_items emits a
        # synthetic ``⧗ loading…`` row (kind='pending') under it.
        b._state.expanded.add('B')
        _state.mark_visible_dirty(b._state)
        vis = _state.visible_items(b._state)
        idx = next(i for i, e in enumerate(vis) if e.kind == 'pending')
        _move_cursor(b, idx)
        self.assertEqual(b._children_displayed_id, 'A')
        _paint(b)
        self.assertIn('A1', _children_pane_text(b))

    def test_no_cursor_row_holds(self):
        b = self._settled_on_a()
        b._state._children[None] = []
        _state.mark_visible_dirty(b._state)
        _tick(b)
        self.assertEqual(b._children_displayed_id, 'A')

    def test_pane_disabled_rule_is_inert(self):
        b = _make_browser([('A', True, True)], show_children_pane=False)
        self.browser = b
        _move_cursor(b, 0)
        _settle(b, 'A')
        self.assertIsNone(b._children_displayed_id)

    def test_show_preview_off_advances_every_move(self):
        # The #954 bit inits True and ``_update_preview_for_cursor``
        # returns before ever clearing it, so the settle gate is always
        # open — today's immediate pane, no special case.
        b = _make_browser([('A', True, True), ('B', True, True)],
                          show_preview=False)
        self.browser = b
        _move_cursor(b, 0)
        self.assertTrue(b._preview_visit_delivered)
        self.assertEqual(b._children_displayed_id, 'A')
        _move_cursor(b, 1)
        self.assertTrue(b._preview_visit_delivered)
        self.assertEqual(b._children_displayed_id, 'B')


# --- Joint swap: both delivery orderings -----------------------------------


class TestJointSwap(_SettleBase):

    def test_children_faster_one_paint_swaps_both(self):
        b = self._settled_on_a()
        _move_cursor(b, 1)
        # Children for B land DURING the hold (the fast ordering).
        _deliver_children(b, 'B')
        _tick(b)
        _paint(b)
        # Mid-hold paint: still old/old — no pane churn from the
        # delivery, no early preview swap.
        self.assertEqual(b._children_displayed_id, 'A')
        self.assertIn('A1', _children_pane_text(b))
        self.assertNotIn('B1', _children_pane_text(b))
        self.assertIn('pv-A', _preview_pane_text(b))

        # Settle: the SAME paint swaps preview and pane to B.
        _settle(b, 'B')
        _paint(b)
        self.assertIn('pv-B', _preview_pane_text(b))
        self.assertIn('B1', _children_pane_text(b))
        self.assertIn('B2', _children_pane_text(b))
        self.assertNotIn('A1', _children_pane_text(b))

    def test_children_slower_settle_paint_shows_hint_then_grows(self):
        b = self._settled_on_a()
        _move_cursor(b, 1)
        _paint(b)
        # Held paint: the OLD branch's grid — never a loading hint for
        # a row the cursor merely skimmed.
        self.assertIn('A1', _children_pane_text(b))
        self.assertNotIn('loading', _children_pane_text(b))

        # Settle with B's children still in flight: the settle paint
        # shows the new preview + the loading hint for the NEW branch
        # (the existing cached-is-None rendering; layout reserves one
        # row). Never new-preview/old-children.
        _settle(b, 'B')
        _paint(b)
        self.assertIn('pv-B', _preview_pane_text(b))
        self.assertIn('loading', _children_pane_text(b))
        self.assertNotIn('A1', _children_pane_text(b))

        # The late delivery grows the hint into the grid — the existing
        # delivery repaint, no new wiring.
        _deliver_children(b, 'B')
        _tick(b)
        _paint(b)
        self.assertIn('B1', _children_pane_text(b))
        self.assertIn('B2', _children_pane_text(b))
        self.assertNotIn('loading', _children_pane_text(b))

    def test_leaf_hide_deferred_to_settle(self):
        b = self._settled_on_a()
        _move_cursor(b, 2)  # leaf L
        _paint(b)
        # Mid-hold: the pane is still up, showing the old branch.
        self.assertIsNotNone(_children_rect(b))
        self.assertIn('A1', _children_pane_text(b))

        _settle(b, 'L')
        # The pane hides in the settle paint, not mid-scroll.
        self.assertEqual(b._children_displayed_id, 'L')
        self.assertIsNone(_children_rect(b))
        _paint(b)
        self.assertIn('pv-L', _preview_pane_text(b))


# --- Displayed-parent lifecycle --------------------------------------------


class TestDisplayedParentLifecycle(_SettleBase):

    def test_displayed_parent_removed_mid_hold_hides_pane(self):
        b = self._settled_on_a()
        _move_cursor(b, 1)  # hold on A
        b.update_data([_state.remove('A')])
        b.drain_main_queue()
        # The displayed id no longer resolves — the pane hides rather
        # than paint a ghost branch (honest; the next settle re-advances).
        self.assertIsNone(_children_rect(b))
        # Removing row 0 shifted the list; keep the cursor on B by
        # identity, as the run loop's anchor re-snap would.
        vis = _state.visible_items(b._state)
        _move_cursor(b, next(
            i for i, e in enumerate(vis) if e.item.id == 'B'))
        _settle(b, 'B')
        self.assertEqual(b._children_displayed_id, 'B')

    def test_clear_children_on_settled_row_hint_reappears_and_regrows(self):
        b = self._settled_on_a()
        b.update_data([_state.clear_children('A')])
        b.drain_main_queue()
        _tick(b)
        _paint(b)
        # Displayed id unchanged; the cache is ``None`` again, so the
        # hint is honest for the settled row. (The cursor-based re-fire
        # in ``_update_children_for_cursor`` re-requests the fetch —
        # request pipeline untouched.)
        self.assertEqual(b._children_displayed_id, 'A')
        self.assertIn('loading', _children_pane_text(b))

        b._state._items_by_id['A1'] = _data.to_item(Item(id='A1'))
        b._state._items_by_id['A2'] = _data.to_item(Item(id='A2'))
        _deliver_children(b, 'A')
        _tick(b)
        _paint(b)
        self.assertIn('A1', _children_pane_text(b))
        self.assertNotIn('loading', _children_pane_text(b))


if __name__ == '__main__':
    unittest.main()
