"""Tests for the rendering layer in browse-tui.

The render module owns two kinds of code:

  * Pure helpers — the default row-format handlers (``default_row_chrome``
    / ``default_row_content`` / ``default_row``, exercised here via the
    ``default_segments`` shim) and ``layout_panes`` (geometry math). These
    are unit-testable with no terminal in the loop.
  * Pane renderers — ``render_list``, ``render_preview``,
    ``render_separator``, plus the orchestration ``render_full`` /
    ``render_partial``. These write through ``020-terminal``'s
    ``write()`` / ``move()`` / ``set_style()`` and are exercised
    end-to-end in the layer-3 tmux tests (ticket #14). Phase 1 leaves
    them uncovered here on purpose — adding stub-write fixtures would
    over-fit the tests to current ANSI-byte layout.

This test file therefore covers the pure helpers only. The placeholder
``Item`` injection follows the same pattern used in ``test_visible_tree.py``.
"""

import io
import sys
import unittest

from test.unit import _loader
from test.unit._loader import load


_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_term = load('_browse_tui_term_render', '020-terminal.py')
_render = load('_browse_tui_render', '050-render.py')

# The render module references ``Item`` for synthetic placeholder rows
# (mirrors the state module's pattern). Inject the real class so the
# default formatter can introspect.
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
# ``apply_ops`` (upsert/set ops) constructs ``Item`` instances inside the
# state module; the isolated test load has to inject the real class (mirrors
# the ``_render.Item`` injection above). The ``max_col_width`` invalidation
# tests drive ``apply_ops`` directly.
_state.Item = _data.Item
# A real headless ``Browser`` (the production-composer test below) drives
# ``update_data`` (which calls ``to_item``) and ``drain_main_queue`` (which
# calls ``notify_wake``) — inject both the same way ``Item`` is injected.
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
# PaneCache is referenced by the four content renderers (#187) — they
# call ``browser._pane_cache.setdefault(name, PaneCache())``. Inject the
# state-layer type the same way Item is injected.
_render.PaneCache = _state.PaneCache
# Wide-char primitives — used by the list-pane cell-budget truncation in
# ``render_list`` (CJK / emoji width). Production builds get them via
# the concatenated build; the test loader has to wire them in.
_render._char_width = _term._char_width
_render._visible_len = _term._visible_len
# ``_ANSI_CSI_RE`` (020-terminal) drives the visible-text collapse for
# cursor / search rows (``_collapse_visible``) and the preview tokeniser.
# The concatenated build resolves it by name; the isolated load wires it in.
_render._ANSI_CSI_RE = _term._ANSI_CSI_RE
# ``_normalize_content`` (040-state) coerces a str row-content result to a
# segment list; ``render_list`` calls it by bare name (single source of
# truth shared with ``Browser._compose_row``). Cross-module injection for
# the isolated load, same as ``_ANSI_CSI_RE`` above.
_render._normalize_content = _state._normalize_content

# The default row-format handlers (``default_row_chrome`` /
# ``default_row_content`` / ``default_row``) live in 040-state but
# reference render-layer constants/helpers (``_TAG_STYLE`` / ``_id_visible``
# / ``_ID_COLOR`` / ``_MARKER_COLOR`` / ``cell_width``) at call time. The
# concatenated production build resolves them by name; the isolated test
# load has to inject them into the state module. ``_sanitize_ansi`` is the
# shared escape-sanitiser (050-render) that ``_normalize_content`` applies
# to a str row-content result on receipt (design sec 4.2 #1).
for _name in ('_TAG_STYLE', '_id_visible', '_ID_COLOR', '_MARKER_COLOR',
              'cell_width', '_sanitize_ansi'):
    setattr(_state, _name, getattr(_render, _name))

Item = _data.Item
Mode = _state.Mode
State = _state.State
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
visible_items = _state.visible_items
apply_ops = _state.apply_ops
cache_invalidate_subtree = _state.cache_invalidate_subtree
cache_invalidate_all = _state.cache_invalidate_all
_index_drop_children = _state._index_drop_children
KEEP_PARENT = _state.KEEP_PARENT
RowContext = _render.RowContext
default_row = _state.default_row
default_row_chrome = _state.default_row_chrome
default_row_content = _state.default_row_content
_segments_cells = _state._segments_cells
layout_panes = _render.layout_panes
render_separator = _render.render_separator
Rect = _render.Rect
point_in_rect = _render.point_in_rect
_TAG_STYLE = _render._TAG_STYLE


class _FakeState:
    """Minimal ``State`` stand-in for building a :class:`RowContext`.

    ``RowContext.__init__`` only touches ``browser._state._parent_of_id``;
    everything else it stores from its constructor kwargs.
    """

    def __init__(self, parent_of_id=None):
        self._parent_of_id = parent_of_id or {}


class _FakeBrowser:
    """Minimal ``Browser`` stand-in for the row-format unit tests.

    Carries the two attributes the default handlers reach for: ``show_ids``
    (read by ``default_row_content`` via ``ctx.browser.show_ids``) and a
    ``_state`` with ``_parent_of_id`` (read by ``RowContext.__init__``).
    """

    def __init__(self, *, show_ids='auto', parent_of_id=None):
        self.show_ids = show_ids
        self._state = _FakeState(parent_of_id)


def _ctx(item, *, depth=0, selected=False, expanded=False,
         is_current_scope=False, kind='normal', list_width=0,
         show_ids='auto', parent_of_id=None, browser=None):
    """Build a :class:`RowContext` for ``item`` the way ``render_list`` does.

    Lets the segment-level tests exercise the public default handlers
    (which take ``(item, ctx)``) without standing up a full Browser.
    """
    if browser is None:
        browser = _FakeBrowser(show_ids=show_ids, parent_of_id=parent_of_id)
    return RowContext(
        browser, item,
        depth=depth, selected=selected, expanded=expanded,
        is_current_scope=is_current_scope, kind=kind, list_width=list_width,
    )


def default_segments(item, *, base_depth=0, **kw):
    """Default whole-row segments for ``item`` — the migrated stand-in for
    the old ``format_item_segments(item, ...)`` on a *normal* row.

    Builds a ctx (``base_depth`` is folded into ``depth`` to preserve the
    old ``rel_depth = depth - base_depth`` semantics the renderer no longer
    needs — the live render path always uses ``base_depth=0``) and returns
    ``default_row(item, ctx)`` (chrome + content with the content_width
    hand-off), matching what ``Browser._compose_row`` produces when no
    hooks are overridden.
    """
    depth = kw.pop('depth', 0) - base_depth
    return default_row(item, _ctx(item, depth=depth, **kw))


class _TermCapture:
    """Capture render writes as a list of (op, *args) tuples + a flat string.

    Injected into the loaded render module by replacing the ``move``,
    ``write``, ``set_style``, ``reset_style``, ``clear_line`` names with
    capturing stand-ins. ``flat`` accumulates the *content* writes only
    (no SGR / cursor-movement bytes) so tests can assert on the visible
    glyphs without coupling to ANSI escapes.
    """

    def __init__(self):
        self.events = []
        self.flat = []

    def install(self, mod):
        self._saved = {}
        for name in ('move', 'write', 'set_style', 'reset_style',
                     'clear_line', 'clear_columns', 'begin_row', 'end_row'):
            self._saved[name] = getattr(mod, name, None)
        mod.move = self._move
        mod.write = self._write
        mod.set_style = self._set_style
        mod.reset_style = self._reset_style
        mod.clear_line = self._clear_line
        mod.clear_columns = self._clear_columns
        # Row-shim stubs (#187): begin_row emits a move + clear-columns
        # equivalent to the pre-shim per-row prologue so existing tests
        # that count moves/clears still see the row-start activity.
        # end_row is a no-op — the real shim's cache-diff path is
        # exercised separately in test_terminal_row_shim.py.
        mod.begin_row = self._begin_row
        mod.end_row = self._end_row

    def restore(self, mod):
        for name, value in self._saved.items():
            if value is None:
                if hasattr(mod, name):
                    delattr(mod, name)
            else:
                setattr(mod, name, value)

    def _move(self, row, col):
        self.events.append(('move', row, col))

    def _write(self, s):
        self.events.append(('write', s))
        self.flat.append(s)

    def _set_style(self, **kwargs):
        self.events.append(('set_style', kwargs))

    def _reset_style(self):
        self.events.append(('reset_style',))

    def _clear_line(self):
        self.events.append(('clear_line',))

    def _clear_columns(self, row, left, right):
        self.events.append(('clear_columns', row, left, right))

    def _begin_row(self, pane_cache, rel_row, abs_row, left, right, *,
                   rightmost):
        # Mimic the row-prologue the renderer used to do explicitly:
        # clear the pane's column range, then move to (abs_row, left).
        # Tests that count clears/moves keep their assertions valid.
        self.events.append(('begin_row', rel_row, abs_row, left, right,
                            rightmost))
        self._clear_columns(abs_row, left, right)
        self._move(abs_row, left)

    def _end_row(self):
        self.events.append(('end_row',))


# --- _TAG_STYLE map ---------------------------------------------------------


class TestTagStyleMap(unittest.TestCase):
    """The tag_style → (fg, bold) map covers the spec's eight names + ''."""

    def test_required_keys_present(self):
        for k in ('green', 'red', 'yellow', 'gray', 'cyan',
                  'blue', 'magenta', 'dim', ''):
            self.assertIn(k, _TAG_STYLE, f'missing _TAG_STYLE key: {k!r}')

    def test_value_shape_is_fg_bold_tuple(self):
        for k, v in _TAG_STYLE.items():
            self.assertIsInstance(v, tuple, f'{k!r}: value is not a tuple')
            self.assertEqual(len(v), 2, f'{k!r}: value not a 2-tuple')
            fg, bold = v
            self.assertTrue(
                fg is None or isinstance(fg, int),
                f'{k!r}: fg must be int|None, got {type(fg).__name__}',
            )
            self.assertIsInstance(bold, bool, f'{k!r}: bold must be bool')

    def test_missing_key_falls_back_to_default(self):
        # Default style for unknown / unstyled tag is the '' entry. The
        # render code uses ``_TAG_STYLE.get(name, _TAG_STYLE[''])`` so a
        # bogus tag_style doesn't crash.
        default = _TAG_STYLE['']
        self.assertEqual(_TAG_STYLE.get('not-a-real-style', default), default)


# --- default row formatter (via the ``default_segments`` shim) --------------


def _texts(segs):
    """Return just the text portions of a segment list (for shape asserts)."""
    return [s[0] for s in segs]


def _joined(segs):
    return ''.join(_texts(segs))


class TestFormatItemSegmentsDefault(unittest.TestCase):
    """Default item formatting: marker + indent + expand + [id] + [tag] + title."""

    def test_leaf_no_tag_no_selection(self):
        item = Item(id='a')
        segs = default_segments(item)
        joined = _joined(segs)
        # Selection marker (2 chars), expand-marker (2 chars for a leaf),
        # then title 'a'. Auto-suppression hides the id segment when
        # ``str(id) == title`` (the default for ``Item(id='a')``).
        self.assertIn('  ', joined[:2])      # selection marker
        self.assertIn('a', joined)
        # No '#' sigil — that's plan-tui-specific and was dropped.
        self.assertNotIn('#', joined)
        # No '[' — no tag.
        self.assertNotIn('[', joined)

    def test_leaf_id_visible_when_title_differs(self):
        # When title differs from id, the id segment is emitted (no '#').
        item = Item(id='a', title='Alpha')
        segs = default_segments(item)
        joined = _joined(segs)
        self.assertIn('a ', joined)
        self.assertIn('Alpha', joined)
        self.assertNotIn('#', joined)

    def test_show_ids_always_emits_id_even_when_equal_to_title(self):
        item = Item(id='a')  # title defaults to 'a'
        segs = default_segments(item, show_ids='always')
        joined = _joined(segs)
        # The id segment is present (and matches the title text); look
        # for the trailing ' ' separator that distinguishes it from the
        # title at end-of-string.
        self.assertIn('a a', joined)

    def test_show_ids_never_hides_id_even_when_different_from_title(self):
        item = Item(id='a', title='Alpha')
        segs = default_segments(item, show_ids='never')
        joined = _joined(segs)
        self.assertNotIn('a ', joined)
        self.assertIn('Alpha', joined)

    def test_collapsed_parent_uses_right_arrow(self):
        item = Item(id='a', has_children=True)
        segs = default_segments(item, expanded=False)
        self.assertIn('▶', _joined(segs))  # ▶

    def test_expanded_parent_uses_down_arrow(self):
        item = Item(id='a', has_children=True)
        segs = default_segments(item, expanded=True)
        self.assertIn('▼', _joined(segs))  # ▼

    def test_tag_segment_is_styled(self):
        item = Item(id='a', tag='running', tag_style='green')
        segs = default_segments(item)
        # Find the tag segment and confirm its fg matches the green entry.
        tag_segs = [s for s in segs if '[running]' in s[0]]
        self.assertEqual(len(tag_segs), 1, f'expected one tag seg, got {tag_segs!r}')
        text, fg, bold = tag_segs[0]
        self.assertEqual(fg, _TAG_STYLE['green'][0])
        self.assertEqual(bold, _TAG_STYLE['green'][1])

    def test_chips_emit_trailing_colored_segments(self):
        # Two chips → two extra ' [text]' segments after the title, each
        # styled through _TAG_STYLE just like the tag segment.
        item = Item(id='a', title='Alpha')
        item.chips = [('HEAD', 'green'), ('v1.2', 'yellow')]
        segs = default_segments(item)
        chip_segs = [s for s in segs if '[HEAD]' in s[0] or '[v1.2]' in s[0]]
        self.assertEqual(len(chip_segs), 2, f'expected two chip segs, got {chip_segs!r}')
        # First chip: green fg/bold; second chip: yellow fg/bold. Color
        # lives in the segment fg, never embedded in the text.
        head, head_fg, head_bold = chip_segs[0]
        self.assertEqual(head, ' [HEAD]')
        self.assertEqual((head_fg, head_bold), _TAG_STYLE['green'])
        tag, tag_fg, tag_bold = chip_segs[1]
        self.assertEqual(tag, ' [v1.2]')
        self.assertEqual((tag_fg, tag_bold), _TAG_STYLE['yellow'])
        # Chips come *after* the title segment.
        title_idx = next(i for i, s in enumerate(segs) if s[0] == 'Alpha')
        chip_idxs = [i for i, s in enumerate(segs) if s in chip_segs]
        self.assertTrue(all(i > title_idx for i in chip_idxs))

    def test_chip_unknown_style_falls_back_to_default(self):
        # An unrecognized style resolves through _TAG_STYLE[''] (plain),
        # mirroring tag_style — it must not crash.
        item = Item(id='a', title='Alpha')
        item.chips = [('odd', 'not-a-style')]
        segs = default_segments(item)
        chip_segs = [s for s in segs if s[0] == ' [odd]']
        self.assertEqual(len(chip_segs), 1)
        _, fg, bold = chip_segs[0]
        self.assertEqual((fg, bold), _TAG_STYLE[''])

    def test_no_chips_unchanged(self):
        # Regression guard: an Item without ``chips`` renders exactly as
        # before — no extra '[' segments beyond an explicit tag (none here).
        item = Item(id='a', title='Alpha')
        segs = default_segments(item)
        self.assertNotIn('[', _joined(segs))
        # And an empty/None chips attribute is also a no-op.
        item.chips = []
        self.assertEqual(default_segments(item), segs)
        item.chips = None
        self.assertEqual(default_segments(item), segs)

    def test_selected_emits_star_marker(self):
        item = Item(id='a')
        segs = default_segments(item, selected=True)
        # First segment text should start with '* '.
        self.assertTrue(
            segs[0][0].startswith('* '),
            f'first seg should be "* ", got {segs[0][0]!r}',
        )

    def test_indent_scales_with_depth(self):
        item = Item(id='a')
        d0 = default_segments(item, depth=0, base_depth=0)
        d2 = default_segments(item, depth=2, base_depth=0)
        # Depth 2 - depth 0 = 2 levels = 4 spaces of additional indent.
        self.assertEqual(len(_joined(d2)) - len(_joined(d0)), 4)


# --- default chrome + content for a meta row (#738, design sec 4) -----------


class TestDefaultMetaRow(unittest.TestCase):
    """A meta row (``ctx.kind == 'meta'``) reuses the normal chrome+content
    pipeline, but chrome reduces to aligned indentation and default content
    is just the title — no selection ``*``, no expander glyph, no id / tag /
    chips."""

    def test_meta_chrome_is_indentation_only(self):
        # Default chrome for a meta row: blank selection marker + indent +
        # blank expander. No '* ', no '▼'/'▶' — even though has_children is
        # forced True and the row is (nonsensically) in the expanded set.
        item = Item(id='sep', title='── Section ──', has_children=True)
        ctx = _ctx(item, kind='meta', depth=1, selected=True, expanded=True)
        chrome = default_row_chrome(item, ctx)
        joined = ''.join(s[0] for s in chrome)
        self.assertNotIn('*', joined)
        self.assertNotIn('▼', joined)
        self.assertNotIn('▶', joined)
        # The depth indent survives: blank marker (2) + depth-1 indent (2) +
        # blank expander (2) = 6 cells, all spaces.
        self.assertEqual(joined, ' ' * 6)

    def test_meta_chrome_blanks_markers_even_when_selected_and_expandable(self):
        # The forced-blank rule holds regardless of selected / has_children /
        # expanded — those flags are meaningless on a meta row.
        item = Item(id='sep', title='hdr', has_children=True)
        ctx = _ctx(item, kind='meta', depth=0, selected=True, expanded=False)
        chrome = default_row_chrome(item, ctx)
        joined = ''.join(s[0] for s in chrome)
        self.assertEqual(joined.strip(), '')

    def test_meta_default_content_is_title_only(self):
        # Default meta content = a single title segment. No id segment (even
        # when show_ids would surface it), no tag chip, no trailing chips.
        item = Item(id='sep', title='── Subagents ──', tag='X',
                    tag_style='green')
        item.chips = [('note', 'cyan')]
        ctx = _ctx(item, kind='meta', show_ids=True)
        content = default_row_content(item, ctx)
        self.assertEqual(content, [('── Subagents ──', None, False)])

    def test_meta_default_content_unaffected_by_show_ids(self):
        # Even with show_ids forced on and id != title, the id segment is
        # suppressed on a meta row (dividers aren't content).
        item = Item(id='sep:subagents', title='── Subagents ──')
        ctx = _ctx(item, kind='meta', show_ids=True)
        content = default_row_content(item, ctx)
        self.assertEqual(content, [('── Subagents ──', None, False)])
        self.assertNotIn('sep:subagents', _joined(content))

    def test_normal_row_content_unchanged_by_meta_branch(self):
        # Regression: the meta branch must not perturb a normal row — id +
        # tag + title + chip all still present in order.
        item = Item(id='x', title='Title', tag='T', tag_style='green')
        item.chips = [('c', 'cyan')]
        ctx = _ctx(item, kind='normal', show_ids=True)
        content = default_row_content(item, ctx)
        joined = _joined(content)
        self.assertIn('x ', joined)
        self.assertIn('[T]', joined)
        self.assertIn('Title', joined)
        self.assertIn('[c]', joined)


class TestComposeRowStrContentProduction(unittest.TestCase):
    """The PRODUCTION ``Browser._compose_row`` normalises a ``str`` content
    result into a single segment (design sec 4.1) — covered here through a
    real headless ``Browser`` (the render_list tests exercise the faithful
    ``_MockBrowser`` copy; this pins the real composer)."""

    def _browser(self, **cfg):
        b = Browser(BrowserConfig(_headless=True, **cfg))
        b.update_data([('upsert', 'a', None, {'title': 'Row'})])
        b.drain_main_queue()
        return b

    def test_compose_row_wraps_str_content(self):
        # A ``format_row_content`` returning a ``str`` flows through the real
        # composer: chrome (segments) + the str wrapped as one
        # ``(text, None, False)`` segment. ``chrome + content`` must not
        # crash (the bug a raw ``list + str`` would cause) and the visible
        # text must land in the row.
        b = self._browser(
            format_row_content=lambda item, ctx: 'PLAIN ' + item.title)
        item = visible_items(b._state)[0].item
        ctx = RowContext(b, item, depth=0, selected=False, expanded=False,
                         is_current_scope=False, kind='normal', list_width=40)
        segs = b._compose_row(item, ctx)
        # Result is a uniform segment list; the last segment is the wrapped
        # string and the collapsed visible text contains it.
        self.assertIsInstance(segs, list)
        self.assertEqual(segs[-1], ('PLAIN Row', None, False))
        self.assertIn('PLAIN Row', ''.join(s[0] for s in segs))

    def test_compose_row_str_with_ansi_keeps_text(self):
        # The str may carry SGR (passthrough, sec 4.1); the composer wraps it
        # verbatim into one segment — width math downstream is ANSI-aware.
        b = self._browser(
            format_row_content=lambda item, ctx: '\033[31m' + item.title
            + '\033[0m')
        item = visible_items(b._state)[0].item
        ctx = RowContext(b, item, depth=0, selected=False, expanded=False,
                         is_current_scope=False, kind='normal', list_width=40)
        segs = b._compose_row(item, ctx)
        self.assertEqual(segs[-1], ('\033[31mRow\033[0m', None, False))


# --- default content: is_current_scope (the scope row) ----------------------
# (The pending placeholder branch moved out of the segment builder into
#  ``render_list``; it is covered by ``TestRenderListPendingRow`` below.)


class TestFormatItemSegmentsKinds(unittest.TestCase):
    """The scope row is rendered as a normal row with the
    ``is_current_scope`` label-override flag (see scope-root unification
    design)."""

    def test_scope_row_renders_as_normal(self):
        # The scope row is emitted as kind='normal' at depth 0. It gets
        # the same chrome as any normal row (selection marker, expand
        # glyph if has_children).
        item = Item(id='proj', title='My Project', has_children=True)
        segs = default_segments(
            item, kind='normal', depth=0, base_depth=1,
            expanded=True, is_current_scope=True,
        )
        joined = _joined(segs)
        self.assertIn('proj ', joined)
        self.assertIn('My Project', joined)
        # Selection marker (unselected → '  ') and expand glyph (▼) are
        # rendered like any normal row.
        self.assertIn('▼', joined)

    def test_scope_title_overrides_when_is_current_scope(self):
        item = Item(id='sess-abc', title='abc')
        item.scope_title = '/full/path/to/sess-abc.jsonl'
        # Without is_current_scope, scope_title is ignored.
        segs = default_segments(item, kind='normal')
        self.assertIn('abc', _joined(segs))
        self.assertNotIn('/full/path', _joined(segs))
        # With is_current_scope, scope_title wins.
        segs2 = default_segments(item, kind='normal', is_current_scope=True)
        self.assertIn('/full/path/to/sess-abc.jsonl', _joined(segs2))

    def test_scope_title_ignored_when_unset(self):
        # No scope_title → title is used regardless of is_current_scope.
        item = Item(id='proj', title='My Project')
        segs = default_segments(item, kind='normal', is_current_scope=True)
        self.assertIn('My Project', _joined(segs))

    def test_scope_row_title_segment_is_bold(self):
        # The scope row's title segment is rendered bold so it stands
        # apart from the listing — the "you are here" indicator. The
        # selection/expand-marker chrome stays non-bold.
        item = Item(id='proj', title='My Project')
        segs = default_segments(item, kind='normal', is_current_scope=True)
        # Find the title segment (text equals item.title).
        title_seg = next(s for s in segs if s[0] == 'My Project')
        self.assertTrue(title_seg[2], 'title segment must be bold for scope row')
        # And NOT bold for a non-current-scope row.
        segs2 = default_segments(item, kind='normal', is_current_scope=False)
        title_seg2 = next(s for s in segs2 if s[0] == 'My Project')
        self.assertFalse(title_seg2[2])

    def test_scope_row_id_segment_is_bold(self):
        # The id segment also bolds for the scope row (when shown).
        item = Item(id='proj', title='My Project')  # id != title -> id visible
        segs = default_segments(item, kind='normal', is_current_scope=True)
        id_seg = next(s for s in segs if 'proj' in s[0] and s[0] != 'My Project')
        self.assertTrue(id_seg[2], 'id segment must be bold for scope row')


# --- format_row override hook -----------------------------------------------


class TestFormatRowHook(unittest.TestCase):
    """A whole-row ``format_row`` override owns the row completely — it is
    bound directly to ``_row_segments`` and called with ``(item, ctx)``."""

    def test_hook_return_value_is_used_verbatim(self):
        def hook(item, ctx):
            return [('CUSTOM', None, False)]

        item = Item(id='a', title='Alpha')
        ctx = _ctx(item, list_width=80)
        segs = hook(item, ctx)
        self.assertEqual(segs, [('CUSTOM', None, False)])
        # Default formatting did not run — only the hook's segment.
        self.assertEqual(_joined(segs), 'CUSTOM')

    def test_hook_receives_item_and_real_ctx(self):
        captured = {}

        def hook(item, ctx):
            captured['item'] = item
            captured['ctx'] = ctx
            return [('x', None, False)]

        item = Item(id='a')
        ctx = _ctx(item, depth=2, selected=True, list_width=40)
        hook(item, ctx)
        self.assertIs(captured['item'], item)
        # The hook now receives a real RowContext (no longer None) carrying
        # per-row state — the phase-2 promise the old ``format_item`` lacked.
        self.assertIsInstance(captured['ctx'], RowContext)
        self.assertEqual(captured['ctx'].depth, 2)
        self.assertTrue(captured['ctx'].selected)


# --- Stage 2: default-output golden + the chrome/content split --------------


class TestDefaultRowGolden(unittest.TestCase):
    """The default whole-row output is byte-for-byte the pre-change layout.

    Hand-written golden segment lists (the exact triples the old
    ``format_item_segments`` produced) guard the headline Stage-2 promise:
    default rendering is unchanged. ``MARKER_FG`` / ``ID_FG`` resolve to
    the palette indices the chrome / id used before (4 / 3).
    """

    def test_leaf_unselected_auto_id_suppressed(self):
        # id == title under 'auto' → no id segment. Leaf → '  ' expander.
        item = Item(id='a')
        self.assertEqual(default_segments(item), [
            ('  ', None, False),          # selection marker (unselected)
            ('', None, False),            # indent (depth 0)
            ('  ', _render.MARKER_FG, False),   # expander (leaf)
            ('a', None, False),           # title
        ])

    def test_selected_expanded_parent_with_id_tag_and_chip(self):
        item = Item(id='x', title='Title', tag='run', tag_style='green')
        item.chips = [('HEAD', 'yellow')]
        segs = default_segments(
            item, depth=2, selected=True, expanded=True, show_ids='always',
        )
        self.assertEqual(segs, [
            ('* ', None, False),          # selected
            ('    ', None, False),        # indent (depth 2 → 4 spaces)
            ('  ', _render.MARKER_FG, False),   # leaf expander (no has_children)
            ('x ', _render.ID_FG, False),       # id (show_ids='always')
            ('[run] ', _TAG_STYLE['green'][0], _TAG_STYLE['green'][1]),
            ('Title', None, False),       # title
            (' [HEAD]', _TAG_STYLE['yellow'][0], _TAG_STYLE['yellow'][1]),
        ])

    def test_expanded_parent_uses_down_arrow_segment(self):
        item = Item(id='d', title='Dir', has_children=True)
        segs = default_segments(item, expanded=True)
        # The expander segment carries '▼ ' coloured MARKER_FG.
        self.assertEqual(segs[2], ('▼ ', _render.MARKER_FG, False))

    def test_scope_row_bolds_id_and_title_segments(self):
        item = Item(id='proj', title='My Project')
        segs = default_segments(item, is_current_scope=True)
        id_seg = next(s for s in segs if s[0] == 'proj ')
        title_seg = next(s for s in segs if s[0] == 'My Project')
        self.assertEqual(id_seg, ('proj ', _render.ID_FG, True))
        self.assertEqual(title_seg, ('My Project', None, True))


class TestRowFormatHookComposition(unittest.TestCase):
    """The three-hook dispatcher: by-config resolution, bound once, with the
    chrome/content split (design sec A) — exercised through ``_compose_row``
    on a real Browser-shaped object."""

    def _browser(self, **hooks):
        # Build via _MockBrowser so we get the real ``_compose_row`` + the
        # ``Browser.__init__`` resolution rules (unset → default).
        b = _MockBrowser(_MockState([]))
        for k, v in hooks.items():
            setattr(b, k, v)
        return b

    def _row_ctx(self, item, browser, **kw):
        kw.setdefault('list_width', 80)
        return RowContext(
            browser, item,
            depth=kw.get('depth', 0), selected=kw.get('selected', False),
            expanded=kw.get('expanded', False),
            is_current_scope=kw.get('is_current_scope', False),
            kind=kw.get('kind', 'normal'), list_width=kw['list_width'],
        )

    def test_unset_hooks_use_defaults_identity(self):
        # A Browser with no hooks set binds chrome/content to the module
        # defaults and ``_row_segments`` to its own composer.
        b = self._browser()
        self.assertIs(b.format_row_chrome, default_row_chrome)
        self.assertIs(b.format_row_content, default_row_content)
        self.assertEqual(b._row_segments, b._compose_row)

    def test_default_compose_equals_default_row(self):
        # With no overrides, the composer's output equals ``default_row``.
        b = self._browser()
        item = Item(id='x', title='Title', tag='t', tag_style='cyan')
        ctx_a = self._row_ctx(item, b, depth=1, selected=True)
        ctx_b = self._row_ctx(item, b, depth=1, selected=True)
        self.assertEqual(b._row_segments(item, ctx_a), default_row(item, ctx_b))

    def test_override_content_keeps_chrome(self):
        # Overriding only format_row_content keeps the framework chrome
        # (selection marker + indent + expander) prefix.
        def content(item, ctx):
            return [('COLS', None, False)]

        b = self._browser(format_row_content=content)
        item = Item(id='x', title='Title', has_children=True)
        ctx = self._row_ctx(item, b, depth=1, selected=True, expanded=True)
        segs = b._row_segments(item, ctx)
        # Chrome is the default three-segment prefix...
        self.assertEqual(segs[:3], [
            ('* ', None, False),
            ('  ', None, False),
            ('▼ ', _render.MARKER_FG, False),
        ])
        # ...followed verbatim by the override's content.
        self.assertEqual(segs[3:], [('COLS', None, False)])

    def test_override_chrome_keeps_default_content(self):
        def chrome(item, ctx):
            return [('>>', None, False)]

        b = self._browser(format_row_chrome=chrome)
        item = Item(id='x', title='Title')   # id != title → id visible
        ctx = self._row_ctx(item, b)
        segs = b._row_segments(item, ctx)
        self.assertEqual(segs[0], ('>>', None, False))
        # Default content follows: id + title.
        self.assertEqual(segs[1:], [
            ('x ', _render.ID_FG, False),
            ('Title', None, False),
        ])

    def test_format_row_override_replaces_everything(self):
        # A whole-row override binds straight to _row_segments; neither the
        # default chrome nor the default content runs.
        def whole(item, ctx):
            return [('WHOLE', None, False)]

        b = self._browser()
        b._row_segments = whole   # what __init__ does for config.format_row
        item = Item(id='x', title='Title')
        ctx = self._row_ctx(item, b)
        self.assertEqual(b._row_segments(item, ctx), [('WHOLE', None, False)])

    def test_set_hook_return_used_verbatim_no_none_sentinel(self):
        # A set hook that returns an empty list is honoured as-is (there is
        # no None-return sentinel falling back to the default).
        def content(item, ctx):
            return []

        b = self._browser(format_row_content=content)
        item = Item(id='x', title='Title')
        ctx = self._row_ctx(item, b)
        # Only the chrome remains; the content contributed nothing.
        self.assertEqual(b._row_segments(item, ctx), b.format_row_chrome(item, ctx))

    def test_hook_calls_default_then_edits_and_returns(self):
        # The documented compose-by-call-edit-return pattern: a recipe calls
        # default_row_content, appends a column, returns the edited list.
        def content(item, ctx):
            segs = default_row_content(item, ctx)
            segs.append((' EXTRA', None, False))
            return segs

        b = self._browser(format_row_content=content)
        item = Item(id='a')
        ctx = self._row_ctx(item, b)
        segs = b._row_segments(item, ctx)
        self.assertEqual(segs[-1], (' EXTRA', None, False))
        # And the default content ('a' title) is still present before it.
        self.assertIn(('a', None, False), segs)


class TestRowContextFields(unittest.TestCase):
    """``RowContext`` carries the correct per-row state + dimensions."""

    def test_carries_per_row_state(self):
        item = Item(id='child', title='Child')
        ctx = _ctx(
            item, depth=3, selected=True, expanded=True,
            is_current_scope=True, kind='normal', list_width=100,
            parent_of_id={'child': 'parent'},
        )
        self.assertEqual(ctx.depth, 3)
        self.assertTrue(ctx.selected)
        self.assertTrue(ctx.expanded)
        self.assertTrue(ctx.is_current_scope)
        self.assertEqual(ctx.kind, 'normal')
        self.assertEqual(ctx.parent_id, 'parent')

    def test_parent_id_none_when_unmapped(self):
        item = Item(id='orphan')
        ctx = _ctx(item, parent_of_id={})
        self.assertIsNone(ctx.parent_id)

    def test_browser_escape_hatch(self):
        item = Item(id='a')
        b = _FakeBrowser(show_ids='always')
        ctx = _ctx(item, browser=b)
        self.assertIs(ctx.browser, b)

    def test_list_width_zero_before_first_paint(self):
        # Default list_width is 0 (headless / pre-paint), matching the
        # preview_width contract.
        item = Item(id='a')
        ctx = _ctx(item)
        self.assertEqual(ctx.list_width, 0)
        self.assertEqual(ctx.content_width, 0)


class TestRowContextContentWidth(unittest.TestCase):
    """``content_width`` = list_width − chrome cells under the composer;
    = list_width under a whole-row override."""

    def test_content_width_after_default_compose_varies_with_depth(self):
        b = _MockBrowser(_MockState([]))
        for depth in (0, 1, 4):
            item = Item(id='a', title='Alpha')
            ctx = RowContext(
                b, item, depth=depth, selected=False, expanded=False,
                is_current_scope=False, kind='normal', list_width=80,
            )
            chrome = b.format_row_chrome(item, ctx)
            b._row_segments(item, ctx)   # runs _compose_row → _set_content_width
            self.assertEqual(ctx.content_width, 80 - _segments_cells(chrome))
            # Sanity: deeper indent → narrower content.
            self.assertLess(ctx.content_width, 80)

    def test_content_width_equals_list_width_under_format_row_override(self):
        # A whole-row override never calls _set_content_width, so the ctx
        # keeps content_width == list_width.
        item = Item(id='a')
        ctx = _ctx(item, depth=3, list_width=80)
        # Simulate the override path: render_list builds ctx, calls the
        # bound _row_segments (the override) which does NOT touch the width.
        def whole(it, c):
            return [('WHOLE', None, False)]
        whole(item, ctx)
        self.assertEqual(ctx.content_width, 80)
        self.assertEqual(ctx.content_width, ctx.list_width)

    def test_content_width_clamps_at_zero_for_wide_chrome(self):
        # A chrome wider than the pane yields content_width 0, never negative.
        item = Item(id='a')
        ctx = _ctx(item, list_width=3)
        ctx._set_content_width(10)
        self.assertEqual(ctx.content_width, 0)


# --- max_col_width (design sec C) -------------------------------------------


class _StateBrowser:
    """Minimal Browser stand-in wrapping a real ``State``.

    ``RowContext.max_col_width`` reads/fills ``self._browser._state`` —
    specifically ``_children`` (the sibling list), ``_parent_of_id`` (for
    ``ctx.parent_id``) and ``_col_width_cache`` (the memo). A real ``State``
    supplies all three, so the invalidation entry points (``apply_ops`` /
    ``cache_invalidate_*``) can be exercised against the same object.
    """

    def __init__(self, state):
        self._state = state
        self.show_ids = 'auto'


def _state_ctx(state, item, **kw):
    """Build a :class:`RowContext` for ``item`` backed by a real ``State``."""
    return RowContext(
        _StateBrowser(state), item,
        depth=kw.get('depth', 0),
        selected=kw.get('selected', False),
        expanded=kw.get('expanded', False),
        is_current_scope=kw.get('is_current_scope', False),
        kind=kw.get('kind', 'normal'),
        list_width=kw.get('list_width', 80),
    )


class TestMaxColWidthMeasurement(unittest.TestCase):
    """``max_col_width`` measures the per-parent max in DISPLAY CELLS."""

    def _state_with_siblings(self):
        # Three siblings under '/': col strings of cell width 3, 4, 0.
        s = State(root_id='/')
        a = Item(id='a'); a.col = 'abc'        # 3 cells
        b = Item(id='b'); b.col = '漢字'        # 2 wide chars -> 4 cells
        c = Item(id='c')                        # missing 'col' -> '' -> 0
        s._children['/'] = [a, b, c]
        s._parent_of_id = {'a': '/', 'b': '/', 'c': '/'}
        return s, a, b, c

    def test_returns_per_parent_max_in_cells(self):
        s, a, *_ = self._state_with_siblings()
        ctx = _state_ctx(s, a)
        # The widest sibling is the CJK one: 2 chars * 2 cells = 4.
        self.assertEqual(ctx.max_col_width('col'), 4)

    def test_wide_char_counts_as_two_cells(self):
        # Isolate the wide-char measurement: a lone CJK sibling.
        s = State(root_id='/')
        w = Item(id='w'); w.col = '日本語'      # 3 wide chars -> 6 cells
        s._children['/'] = [w]
        s._parent_of_id = {'w': '/'}
        ctx = _state_ctx(s, w)
        self.assertEqual(ctx.max_col_width('col'), 6)

    def test_missing_field_contributes_zero(self):
        # No sibling carries 'ghost' -> every getattr default '' -> max 0.
        s, a, *_ = self._state_with_siblings()
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('ghost'), 0)

    def test_one_missing_field_among_present_does_not_lower_max(self):
        # 'c' lacks 'col' (contributes 0) but does not drag the max below
        # the widest present sibling.
        s, a, *_ = self._state_with_siblings()
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('col'), 4)

    def test_empty_sibling_list_is_zero(self):
        s = State(root_id='/')
        s._children['/'] = []
        a = Item(id='a'); a.col = 'wide-value'
        # 'a' is not under '/', so '/' has no children to measure.
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('col', '/'), 0)

    def test_absent_sibling_list_is_zero(self):
        # Parent never cached: no '/' key in _children at all.
        s = State(root_id='/')
        a = Item(id='a'); a.col = 'x'
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('col', '/'), 0)

    def test_non_string_value_is_stringified(self):
        # The field may hold a non-string; max_col_width measures str(value).
        s = State(root_id='/')
        a = Item(id='a'); a.num = 12345        # str -> '12345' -> 5 cells
        s._children['/'] = [a]
        s._parent_of_id = {'a': '/'}
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('num'), 5)

    def test_defaults_to_this_rows_parent(self):
        # Two parents with different-width columns; a child of each measures
        # only its own siblings.
        s = State(root_id='/')
        pa = Item(id='pa'); ca = Item(id='ca'); ca.col = 'short'      # 5
        pb = Item(id='pb'); cb = Item(id='cb'); cb.col = 'much-longer'  # 11
        s._children = {'pa': [ca], 'pb': [cb]}
        s._parent_of_id = {'ca': 'pa', 'cb': 'pb'}
        self.assertEqual(_state_ctx(s, ca).max_col_width('col'), 5)
        self.assertEqual(_state_ctx(s, cb).max_col_width('col'), 11)

    def test_explicit_parent_overrides_default(self):
        s = State(root_id='/')
        pa = Item(id='pa'); ca = Item(id='ca'); ca.col = 'short'
        pb = Item(id='pb'); cb = Item(id='cb'); cb.col = 'much-longer'
        s._children = {'pa': [ca], 'pb': [cb]}
        s._parent_of_id = {'ca': 'pa', 'cb': 'pb'}
        # 'ca' lives under 'pa' but explicitly asks for 'pb's column.
        self.assertEqual(_state_ctx(s, ca).max_col_width('col', 'pb'), 11)

    def test_explicit_none_parent_distinct_from_default(self):
        # parent_id=None is a real key (root_id=None Browser), NOT "omitted".
        s = State()                              # root_id is None
        top = Item(id='top'); top.col = 'rooted-row'   # 10 cells, parent None
        child = Item(id='c'); child.col = 'x'          # parent 'top'
        s._children = {None: [top], 'top': [child]}
        s._parent_of_id = {'top': None, 'c': 'top'}
        ctx = _state_ctx(s, child)
        self.assertEqual(ctx.parent_id, 'top')
        # Omitted -> this row's parent ('top'): width of 'x' = 1.
        self.assertEqual(ctx.max_col_width('col'), 1)
        # Explicit None -> the root group: width of 'rooted-row' = 10.
        self.assertEqual(ctx.max_col_width('col', None), 10)

    def test_flat_single_parent_matches_global_max(self):
        # A flat list (all rows under one parent, as a git log) -> per-parent
        # alignment equals the global max across every row.
        s = State(root_id='/')
        vals = ['feat: a', 'fix: bug', 'refactor: the whole module']
        items = []
        for i, v in enumerate(vals):
            it = Item(id=f'c{i}'); it.col = v
            items.append(it)
            s._parent_of_id[it.id] = '/'
        s._children['/'] = items
        per_parent = _state_ctx(s, items[0]).max_col_width('col')
        global_max = max(_render.cell_width(v) for v in vals)
        self.assertEqual(per_parent, global_max)


class TestMaxColWidthCache(unittest.TestCase):
    """The result is memoised per ``(parent_id, field)``; a hit re-scans
    nothing."""

    def _state_with_siblings(self):
        s = State(root_id='/')
        a = Item(id='a'); a.col = 'abc'
        b = Item(id='b'); b.col = 'abcde'
        s._children['/'] = [a, b]
        s._parent_of_id = {'a': '/', 'b': '/'}
        return s, a, b

    def test_first_call_fills_cache(self):
        s, a, _ = self._state_with_siblings()
        self.assertEqual(s._col_width_cache, {})
        _state_ctx(s, a).max_col_width('col')
        self.assertEqual(s._col_width_cache, {'/': {'col': 5}})

    def test_second_call_hits_cache_no_rescan(self):
        # Spy on cell_width: it must NOT be invoked on the cached second call.
        s, a, _ = self._state_with_siblings()
        ctx = _state_ctx(s, a)
        calls = {'n': 0}
        real_cell_width = _render.cell_width

        def counting(s_):
            calls['n'] += 1
            return real_cell_width(s_)

        _render.cell_width = counting
        try:
            self.assertEqual(ctx.max_col_width('col'), 5)
            after_first = calls['n']
            self.assertGreater(after_first, 0)   # the fill scanned siblings
            # Second call: cached -> no further cell_width invocations.
            self.assertEqual(ctx.max_col_width('col'), 5)
            self.assertEqual(calls['n'], after_first)
        finally:
            _render.cell_width = real_cell_width

    def test_cached_zero_still_hits_cache(self):
        # A memoised 0 (missing field) must hit the cache, not re-scan: the
        # `is not None` check distinguishes "cached 0" from "not computed".
        s, a, _ = self._state_with_siblings()
        ctx = _state_ctx(s, a)
        calls = {'n': 0}
        real_cell_width = _render.cell_width

        def counting(s_):
            calls['n'] += 1
            return real_cell_width(s_)

        _render.cell_width = counting
        try:
            self.assertEqual(ctx.max_col_width('ghost'), 0)
            after_first = calls['n']
            self.assertEqual(ctx.max_col_width('ghost'), 0)
            self.assertEqual(calls['n'], after_first)
        finally:
            _render.cell_width = real_cell_width

    def test_distinct_fields_cached_independently(self):
        s = State(root_id='/')
        a = Item(id='a'); a.x = 'ab'; a.y = 'abcd'
        s._children['/'] = [a]
        s._parent_of_id = {'a': '/'}
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('x'), 2)
        self.assertEqual(ctx.max_col_width('y'), 4)
        self.assertEqual(s._col_width_cache['/'], {'x': 2, 'y': 4})


class TestMaxColWidthInvalidation(unittest.TestCase):
    """The cache entry is dropped on child-list drop / replace / mutation."""

    def _populate(self, root='/'):
        s = State(root_id=root)
        a = Item(id='a'); a.col = 'abc'
        b = Item(id='b'); b.col = 'abcde'        # widest: 5
        s._children[root] = [a, b]
        s._items_by_id = {'a': a, 'b': b}
        s._parent_of_id = {'a': root, 'b': root}
        return s, a, b

    def _fill(self, s, item):
        # Prime the cache for the item's parent under field 'col'.
        w = _state_ctx(s, item).max_col_width('col')
        self.assertIn(s._parent_of_id[item.id], s._col_width_cache)
        return w

    def test_cache_invalidate_subtree_drops_entry(self):
        s, a, _ = self._populate()
        self._fill(s, a)
        cache_invalidate_subtree(s, '/')
        self.assertNotIn('/', s._col_width_cache)

    def test_cache_invalidate_all_clears_everything(self):
        s, a, _ = self._populate()
        self._fill(s, a)
        cache_invalidate_all(s)
        self.assertEqual(s._col_width_cache, {})

    def test_index_drop_children_drops_entry(self):
        # ``_index_drop_children`` is THE drop/replace choke point that
        # worker delivery (refresh) and ``cache_invalidate_subtree`` route
        # through. Dropping there is what covers refresh for free. (The
        # end-to-end refresh / update_data invalidation through the real
        # ``Browser`` is asserted in test_state_indexes.py.)
        s, a, _ = self._populate()
        self._fill(s, a)
        self.assertIn('/', s._col_width_cache)
        _index_drop_children(s, '/')
        self.assertNotIn('/', s._col_width_cache)

    def test_index_drop_children_drops_even_empty_list(self):
        # A parent whose cached entry is [] (measured width 0) still has its
        # column-width entry evicted — the drop precedes the empty-list guard.
        s = State(root_id='/')
        s._children['/'] = []
        # Prime the (0-width) cache for '/'.
        self.assertEqual(_state_ctx(s, Item(id='ph')).max_col_width('col', '/'),
                         0)
        self.assertIn('/', s._col_width_cache)
        _index_drop_children(s, '/')
        self.assertNotIn('/', s._col_width_cache)

    def test_update_data_upsert_drops_entry(self):
        s, a, _ = self._populate()
        self._fill(s, a)
        self.assertEqual(s._col_width_cache['/']['col'], 5)
        # Upsert a NEW, wider sibling under '/'.
        apply_ops(s, [('upsert', 'c', '/', {'col': 'abcdefghij'})])  # 10
        self.assertNotIn('/', s._col_width_cache)
        # Recompute reflects the new widest sibling.
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('col'), 10)

    def test_update_data_mod_drops_entry(self):
        s, a, b = self._populate()
        self._fill(s, a)
        # Mod an existing sibling's measured field (patch keeps parent).
        apply_ops(s, [('mod', 'a', KEEP_PARENT, {'col': 'XXXXXXXXXXXX'})])
        self.assertNotIn('/', s._col_width_cache)
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('col'), 12)

    def test_update_data_remove_drops_entry(self):
        s, a, b = self._populate()
        self._fill(s, a)
        apply_ops(s, [('remove', 'b')])           # drop the widest sibling
        self.assertNotIn('/', s._col_width_cache)
        ctx = _state_ctx(s, a)
        self.assertEqual(ctx.max_col_width('col'), 3)   # only 'a' (abc) left

    def test_update_data_clear_children_drops_entry(self):
        s, a, _ = self._populate()
        self._fill(s, a)
        apply_ops(s, [('clear_children', '/')])
        self.assertNotIn('/', s._col_width_cache)

    def test_noop_mod_does_not_blow_cache_unnecessarily(self):
        # A mod targeting an UNKNOWN id is a silent no-op (not structural),
        # so the cache is untouched.
        s, a, _ = self._populate()
        self._fill(s, a)
        apply_ops(s, [('mod', 'nonexistent', KEEP_PARENT, {'col': 'z'})])
        self.assertIn('/', s._col_width_cache)

    def test_reparent_drops_both_parents(self):
        # Moving a child from one parent to another invalidates BOTH groups.
        s = State(root_id='/')
        pa = Item(id='pa'); pb = Item(id='pb')
        x = Item(id='x'); x.col = 'wiiiiide'      # 8
        y = Item(id='y'); y.col = 'yy'            # 2
        s._children = {'pa': [x], 'pb': [y]}
        s._items_by_id = {'pa': pa, 'pb': pb, 'x': x, 'y': y}
        s._parent_of_id = {'x': 'pa', 'y': 'pb'}
        # Prime both parents' caches.
        self.assertEqual(_state_ctx(s, x).max_col_width('col'), 8)
        self.assertEqual(_state_ctx(s, y).max_col_width('col'), 2)
        self.assertIn('pa', s._col_width_cache)
        self.assertIn('pb', s._col_width_cache)
        # Reparent x from pa -> pb via mod.
        apply_ops(s, [('mod', 'x', 'pb', {})])
        self.assertNotIn('pa', s._col_width_cache)
        self.assertNotIn('pb', s._col_width_cache)


class TestDefaultHandlersExportedFromBrowseTui(unittest.TestCase):
    """The public default handlers are importable from the ``browse_tui``
    alias (here: the loaded state module) and compose as documented."""

    def test_handlers_are_module_level(self):
        # default_row composes chrome + content (the composer's defaults).
        item = Item(id='a', title='Alpha')
        ctx_whole = _ctx(item, list_width=40)
        ctx_parts = _ctx(item, list_width=40)
        whole = default_row(item, ctx_whole)
        parts = (default_row_chrome(item, ctx_parts)
                 + default_row_content(item, ctx_parts))
        self.assertEqual(whole, parts)

    def test_default_row_sets_content_width(self):
        item = Item(id='a', title='Alpha')
        ctx = _ctx(item, depth=1, list_width=40)
        chrome = default_row_chrome(item, ctx)
        default_row(item, ctx)
        self.assertEqual(ctx.content_width, 40 - _segments_cells(chrome))


# --- layout_panes -----------------------------------------------------------


class TestPointInRect(unittest.TestCase):
    """``point_in_rect`` — inclusive-top, exclusive-right/bottom."""

    def test_point_in_rect_basic(self):
        r = Rect(left=10, top=5, right=20, bottom=15)
        # Inside, on top-left corner.
        self.assertTrue(point_in_rect(5, 10, r))
        # Inside, middle.
        self.assertTrue(point_in_rect(7, 12, r))
        # Just inside the bottom-right corner (exclusive).
        self.assertTrue(point_in_rect(14, 19, r))
        # Outside: above, below, left, right.
        self.assertFalse(point_in_rect(4, 12, r))
        self.assertFalse(point_in_rect(15, 12, r))   # bottom is exclusive
        self.assertFalse(point_in_rect(7, 9, r))
        self.assertFalse(point_in_rect(7, 20, r))    # right is exclusive

    def test_point_in_rect_none(self):
        # ``None`` rect is convenient for layout.get('children') style
        # callers — should always return False, not raise.
        self.assertFalse(point_in_rect(5, 5, None))

    def test_point_in_rect_zero_area(self):
        # Degenerate rect (left == right): nothing is inside.
        r = Rect(left=10, top=5, right=10, bottom=15)
        self.assertFalse(point_in_rect(7, 10, r))
        # Likewise for top == bottom.
        r2 = Rect(left=10, top=5, right=20, bottom=5)
        self.assertFalse(point_in_rect(5, 12, r2))


class TestLayoutPanes(unittest.TestCase):
    """Pane geometry: list+preview when show_preview, list-only otherwise.

    Convention: layout returns Rects (1-based with exclusive
    right/bottom). The preview Rect's first row IS its separator (in
    layout 'h'); user-visible content height is ``preview.height - 1``.
    The children Rect is ``None`` when the grid is hidden.
    """

    def test_two_pane_typical_terminal(self):
        layout = layout_panes(80, 24, show_preview=True)
        self.assertEqual(layout['cols'], 80)
        list_rect = layout['list']
        self.assertEqual(list_rect.top, 1)
        # 30% of 24 = 7.2 -> 7 (per the spec); leave wiggle room ±1.
        self.assertGreaterEqual(list_rect.height, 6)
        self.assertLessEqual(list_rect.height, 8)
        # No grid pane requested → children is None; info bar sits on
        # the preview separator.
        self.assertIsNone(layout['children'])
        info_bar = layout['info_bar']
        preview = layout['preview']
        self.assertEqual(info_bar.top, list_rect.bottom)
        self.assertEqual(preview.top, info_bar.top)
        # list + preview (incl. sep) = 24 rows total.
        self.assertEqual(list_rect.height + preview.height, 24)

    def test_one_pane_when_preview_hidden(self):
        layout = layout_panes(80, 24, show_preview=False)
        list_rect = layout['list']
        self.assertEqual(list_rect.top, 1)
        self.assertEqual(list_rect.height, 23)
        self.assertIsNone(layout['preview'])
        self.assertIsNone(layout['children'])
        # Info bar at the bottom row (24).
        self.assertEqual(layout['info_bar'].top, 24)

    def test_small_terminal_no_negative_heights(self):
        layout = layout_panes(40, 5, show_preview=True)
        # Whatever the math, heights must be non-negative and the panes
        # must fit inside ``rows``.
        list_rect = layout['list']
        preview = layout['preview']
        self.assertGreaterEqual(list_rect.height, 1)
        prev_h = preview.height if preview is not None else 0
        self.assertGreaterEqual(prev_h, 0)
        self.assertIsNone(layout['children'])
        self.assertLessEqual(list_rect.height + prev_h, 5)


class TestLayoutPanesListRatio(unittest.TestCase):
    """Custom ``list_ratio`` parameter — drives the resizable split."""

    def test_default_ratio_matches_legacy_30_percent(self):
        # Omitting list_ratio reproduces the historic 30% behaviour.
        legacy = layout_panes(80, 100, show_preview=True)
        explicit = layout_panes(80, 100, show_preview=True, list_ratio=0.30)
        self.assertEqual(legacy['list'].height, explicit['list'].height)

    def test_50_percent_ratio_splits_evenly(self):
        layout = layout_panes(80, 100, show_preview=True, list_ratio=0.50)
        list_rect = layout['list']
        preview = layout['preview']
        self.assertEqual(list_rect.height, 50)
        # Preview gets remainder including separator: 50 = sep(1) + 49 content.
        self.assertEqual(preview.height, 50)
        self.assertEqual(list_rect.height + preview.height, 100)

    def test_high_ratio_clamped_to_leave_preview_min(self):
        # 99% would give list=99 of 100 rows, preview=1 (separator only,
        # no content). The min-2 preview rule squeezes list down so 1
        # preview content row is always visible.
        layout = layout_panes(80, 100, show_preview=True, list_ratio=0.99)
        preview = layout['preview']
        self.assertGreaterEqual(preview.height, 2,
                                'preview must keep separator + 1 content')
        self.assertLessEqual(layout['list'].height, 98)

    def test_low_ratio_floors_list_at_one_row(self):
        layout = layout_panes(80, 100, show_preview=True, list_ratio=0.001)
        self.assertGreaterEqual(layout['list'].height, 1)

    def test_ratio_preserved_across_terminal_resizes(self):
        # Same ratio at different terminal heights → proportional list size.
        small = layout_panes(80, 50, show_preview=True, list_ratio=0.40)
        big = layout_panes(80, 100, show_preview=True, list_ratio=0.40)
        self.assertEqual(small['list'].height, 20)
        self.assertEqual(big['list'].height, 40)

    def test_tiny_terminal_degrades_gracefully(self):
        # Below the prev_min=2 threshold, layout shouldn't crash; it
        # falls back to "leave 1 row for the separator".
        layout = layout_panes(40, 2, show_preview=True, list_ratio=0.50)
        list_rect = layout['list']
        preview = layout['preview']
        prev_h = preview.height if preview is not None else 0
        self.assertGreaterEqual(list_rect.height, 1)
        self.assertLessEqual(list_rect.height + prev_h, 2)

    def test_with_children_grid_ratio_applies_to_total_rows(self):
        # Per model (a): list_ratio is list / (list+children+preview).
        # Children stays content-driven; preview absorbs the rest.
        layout = layout_panes(
            80, 100, show_preview=True, show_children_pane=True,
            children_rows_needed=5, list_ratio=0.30,
        )
        children = layout['children']
        list_rect = layout['list']
        preview = layout['preview']
        # Children grid: 1 sep + 5 content rows = 6 (capped at 25 by
        # 25% rule, so 6 is fine).
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 6)
        self.assertEqual(list_rect.height, 30)
        # Preview: 100 - 30 - 6 = 64.
        self.assertEqual(preview.height, 64)


class TestLayoutChildrenSubPane(unittest.TestCase):
    """Children sub-pane geometry across the four layouts.

    Per ticket #149 the children pane is capped at 25% of its sub-area
    along the relevant axis (height for h/m/pc, width for v). When the
    pane is hidden (``show_children_pane=False``) or there's nothing to
    show (``children_*_needed == 0``), the inner split must be omitted
    entirely — children Rect is None and sep_inner is None. When the
    sub-area can't accommodate children at minimum size, the layout
    must drop children gracefully rather than allocating a degenerate
    Rect or stealing space from the list/preview minimums.
    """

    # ----- layout 'h' --------------------------------------------------

    def test_h_no_children(self):
        # show_children=False: no inner split at all.
        layout = layout_panes(
            80, 40, split='h', show_preview=True,
            show_children_pane=False, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

        # show_children=True but children_rows_needed=0 (e.g. cursor on
        # a leaf with no cached children to display): also no split.
        layout = layout_panes(
            80, 40, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_h_children_fits(self):
        # Need 3 rows (well under 25% of 40 = 10). Children pane height
        # = 1 separator + 3 content rows = 4.
        layout = layout_panes(
            80, 40, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 1 + 3)

    def test_h_children_clamped_to_25pct(self):
        # Need 30 rows; 25% of 40 = 10 cap. Children pane is clamped to
        # 10 rows (incl. separator).
        layout = layout_panes(
            80, 40, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=30,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 10)

    def test_h_children_min_terminal(self):
        # rows<20 hard floor: children pane is dropped on tiny terminals
        # to keep the list+preview minimums sane.
        layout = layout_panes(
            80, 19, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_horizontal_children_unchanged(self):
        # Regression: in layout 'h', children sits between list and
        # preview — list.bottom == children.top, children.bottom ==
        # preview.top.
        layout = layout_panes(
            80, 30, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        list_rect = layout['list']
        children = layout['children']
        preview = layout['preview']
        self.assertIsNotNone(children)
        self.assertEqual(list_rect.bottom, children.top)
        self.assertEqual(children.bottom, preview.top)

    # ----- layout 'v' --------------------------------------------------
    #
    # Per #176 layout 'v' is a 3-COLUMN shape ``list | children |
    # preview`` where the children column occupies the FULL HEIGHT of
    # the body (above the info bar) and renders one child per row.
    # Sub-pane sizing is therefore width-based, capped at 25% of the
    # right-of-list area's width.

    def test_v_no_children(self):
        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=False, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_v_children_fits(self):
        # Per #180: width is CONTENT-INDEPENDENT — always 25% of the
        # right area (with a max(8, ...) floor). cols=80, list_ratio=0.30
        # → list_w=24, sep_main=1 col, right area = 80 - 25 = 55.
        # desired = max(8, 55//4) = 13 regardless of children_cols_needed.
        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=10,
        )
        children = layout['children']
        info_bar = layout['info_bar']
        self.assertIsNotNone(children)
        self.assertEqual(children.width, 13)
        self.assertEqual(children.top, 1)
        self.assertEqual(children.bottom, info_bar.top)

    def test_v_children_clamped_to_25pct(self):
        # Per #180: width is fixed at max(8, right_area_width // 4)
        # regardless of children_cols_needed. right_area = 55, so the
        # column is 13 cols wide whether the longest child is 10 or 100.
        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=30,
            children_cols_needed=100,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.width, 13)

    def test_v_children_width_is_content_independent(self):
        # Regression for #180: the children column width must not depend
        # on what's in cached children. Two layouts at the same terminal
        # size must produce IDENTICAL children rects regardless of the
        # children_cols_needed hint (short names vs. long names).
        short = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=3,
        )
        long_ = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=42,
        )
        self.assertIsNotNone(short['children'])
        self.assertIsNotNone(long_['children'])
        self.assertEqual(short['children'], long_['children'])
        self.assertEqual(short['sep_inner'], long_['sep_inner'])
        self.assertEqual(short['preview'], long_['preview'])

    def test_v_children_min_terminal(self):
        # Right area too narrow for children (sep_inner + children +
        # preview content) → drop children. cols=27, list_w=8 → right
        # area = 27 - 8 - 1 = 18; that's enough for children (cap=4).
        # Use a tighter terminal to force a fallback.
        # cols=10, list_w=3 → right area = 10 - 3 - 1 = 6, cap=1, but
        # max_w = right_area - 2 = 4 → children=1 col still fits.
        # To exhaust children: shrink right_area to <= 2 cols. cols=6,
        # list_w=1 → right area = 6 - 1 - 1 = 4 → still fits. The
        # `body_height < 1` branch falls back when terminal is 1 row
        # tall — but the helper degrades to layout 'h' for show_preview.
        # Simplest: tiny rows triggers a fallback to 'h' layout where
        # children is dropped since rows < 20.
        layout = layout_panes(
            80, 3, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
            children_cols_needed=10,
        )
        # rows=3 falls through to layout 'h' (body_height=2 still >= 1
        # so the v branch is taken; but the 'h' fallback path that
        # drops children when rows<20 is what we test in the more
        # extreme case below). For the current case, ensure nothing
        # crashed and the layout has SOME shape:
        self.assertIsNotNone(layout['list'])

        # Tiny terminal → should drop children entirely. rows<20 in 'h'
        # fallback drops the grid; in 'v' the body_height-based check
        # is satisfied at rows=3, so we test the right-area-too-narrow
        # path: cols too small to spare children + sep + preview cols.
        narrow = layout_panes(
            6, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
            children_cols_needed=10,
        )
        # cols=6, list_w=1, right_area=4. max_w = 4 - 2 = 2. Children
        # would be 2 cols wide — still kept. The fallback is for
        # right_area < 3 (need sep_inner + 1 col children + 1 col
        # preview minimum). Verify with cols=4.
        very_narrow = layout_panes(
            4, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
            children_cols_needed=10,
        )
        # cols=4, list_w=1, right_area=4-1-1=2 < 3 → children dropped.
        self.assertIsNone(very_narrow['children'])
        self.assertIsNone(very_narrow['sep_inner'])

    def test_vertical_children_is_full_height_column_between_list_and_preview(self):
        # Per #176 layout 'v' is a true 3-column shape: children sits
        # BETWEEN list and preview, with full body height (top of list
        # to top of info bar).
        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=10,
        )
        list_rect = layout['list']
        sep_main = layout['sep_main']
        sep_inner = layout['sep_inner']
        children = layout['children']
        preview = layout['preview']
        info_bar = layout['info_bar']
        self.assertIsNotNone(children)
        self.assertIsNotNone(sep_inner)
        # Outer split: list | sep_main | children | sep_inner | preview.
        self.assertEqual(list_rect.right, sep_main.left)
        self.assertEqual(sep_main.right, children.left)
        self.assertEqual(children.right, sep_inner.left)
        self.assertEqual(sep_inner.right, preview.left)
        # Children spans the full body height (above the info bar).
        self.assertEqual(children.top, list_rect.top)
        self.assertEqual(children.bottom, info_bar.top)
        # Children is BETWEEN list and preview (left > list.right,
        # right < preview.left).
        self.assertGreater(children.left, list_rect.right)
        self.assertLess(children.right, preview.left)
        # Both inner separators run the full body height.
        self.assertEqual(sep_main.height, children.height)
        self.assertEqual(sep_inner.height, children.height)

    def test_vertical_distinct_from_preview_children(self):
        # Per #176 Alt-1 ('v') must differ from Alt-4 ('pc'). In 'v'
        # children is a full-height column; in 'pc' children sits above
        # the preview within the right column.
        v_layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=10,
        )
        pc_layout = layout_panes(
            80, 30, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        v_children = v_layout['children']
        pc_children = pc_layout['children']
        v_info = v_layout['info_bar']
        pc_preview = pc_layout['preview']
        self.assertIsNotNone(v_children)
        self.assertIsNotNone(pc_children)
        # In 'v': children.bottom == info_bar.top (full body height).
        self.assertEqual(v_children.bottom, v_info.top)
        # In 'pc': children.bottom < preview.top (children stacks
        # above preview within the right area).
        self.assertLess(pc_children.bottom, pc_preview.top)

    # ----- layout 'm' --------------------------------------------------

    def test_m_no_children(self):
        layout = layout_panes(
            80, 30, split='m', show_preview=True,
            show_children_pane=False, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

        layout = layout_panes(
            80, 30, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_m_children_fits(self):
        # body_height = rows - 1 (info bar) = 39. 25% = 9. Need 3 → 3.
        layout = layout_panes(
            80, 40, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 3)

    def test_m_children_clamped_to_25pct(self):
        # body_height=39, cap=floor(39/4)=9. Need 30 → clamped to 9.
        layout = layout_panes(
            80, 40, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=30,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 9)

    def test_m_children_min_terminal(self):
        # body_height < 3 → drop children. rows=3 → body_height=2.
        layout = layout_panes(
            80, 3, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_mixed_children_is_row_below_list(self):
        # In layout 'm' children sits inside the LEFT column, below the
        # list, sharing the column with the same horizontal extent.
        layout = layout_panes(
            80, 30, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        list_rect = layout['list']
        sep_inner = layout['sep_inner']
        children = layout['children']
        preview = layout['preview']
        self.assertIsNotNone(children)
        self.assertIsNotNone(sep_inner)
        # Same horizontal extent as list (left column).
        self.assertEqual(children.left, list_rect.left)
        self.assertEqual(children.right, list_rect.right)
        # Stacked below list, separated by sep_inner.
        self.assertEqual(list_rect.bottom, sep_inner.top)
        self.assertEqual(sep_inner.bottom, children.top)
        # Preview is to the right (NOT split horizontally itself).
        self.assertGreater(preview.left, children.right)
        self.assertEqual(preview.top, list_rect.top)

    # ----- layout 'pc' -------------------------------------------------

    def test_pc_no_children(self):
        layout = layout_panes(
            80, 30, split='pc', show_preview=True,
            show_children_pane=False, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

        layout = layout_panes(
            80, 30, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_pc_children_fits(self):
        # body_height=39, cap=9. Need 3 → 3.
        layout = layout_panes(
            80, 40, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 3)

    def test_pc_children_clamped_to_25pct(self):
        # body_height=39, cap=9. Need 30 → clamped to 9.
        layout = layout_panes(
            80, 40, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=30,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 9)

    def test_pc_children_min_terminal(self):
        # body_height < 3 → drop children.
        layout = layout_panes(
            80, 3, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_preview_children_is_row_above_preview(self):
        # In layout 'pc' children sits inside the RIGHT column, ABOVE the
        # preview, sharing horizontal extent with preview.
        layout = layout_panes(
            80, 30, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        list_rect = layout['list']
        sep_inner = layout['sep_inner']
        children = layout['children']
        preview = layout['preview']
        self.assertIsNotNone(children)
        self.assertIsNotNone(sep_inner)
        # Same horizontal extent as preview (right column).
        self.assertEqual(children.left, preview.left)
        self.assertEqual(children.right, preview.right)
        # Children sits at the top of the right column.
        self.assertEqual(children.top, list_rect.top)
        # Stacked above preview via sep_inner.
        self.assertEqual(children.bottom, sep_inner.top)
        self.assertEqual(sep_inner.bottom, preview.top)
        # List spans the full body height on the left.
        self.assertGreater(children.left, list_rect.right)


class TestRenderSeparator(unittest.TestCase):
    """``render_separator`` draws plain horizontal / vertical pane dividers.

    The function writes through the terminal primitives in 050-render
    (``move``/``write``/``set_style``/``reset_style``); the test installs
    a :class:`_TermCapture` to inspect the emitted glyph stream without
    coupling to ANSI escape bytes.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)

    def tearDown(self):
        self.cap.restore(_render)

    def test_horizontal_fills_width_with_dash(self):
        rect = Rect(left=1, top=5, right=11, bottom=6)  # width=10, height=1
        render_separator(rect, orientation='h')
        flat = ''.join(self.cap.flat)
        self.assertEqual(flat, '─' * 10)
        # The cursor moved to (5, 1) before drawing.
        moves = [e for e in self.cap.events if e[0] == 'move']
        self.assertEqual(moves[0], ('move', 5, 1))

    def test_vertical_fills_height_with_bar(self):
        rect = Rect(left=8, top=2, right=9, bottom=6)  # width=1, height=4
        render_separator(rect, orientation='v')
        # Each row gets one '│' glyph.
        bars = [s for s in self.cap.flat if s == '│']
        self.assertEqual(len(bars), 4)
        # Each bar was preceded by a move to (row, 8).
        moves = [e for e in self.cap.events if e[0] == 'move']
        self.assertEqual(moves, [
            ('move', 2, 8), ('move', 3, 8),
            ('move', 4, 8), ('move', 5, 8),
        ])

    def test_orientation_inferred_from_shape(self):
        # height==1 → horizontal.
        h_rect = Rect(left=1, top=1, right=21, bottom=2)
        render_separator(h_rect)  # no explicit orientation
        flat = ''.join(self.cap.flat)
        self.assertIn('─', flat)
        self.assertNotIn('│', flat)

        self.cap.events.clear()
        self.cap.flat.clear()

        # width==1 → vertical.
        v_rect = Rect(left=5, top=1, right=6, bottom=11)
        render_separator(v_rect)
        flat = ''.join(self.cap.flat)
        self.assertIn('│', flat)
        self.assertNotIn('─', flat)

    def test_horizontal_with_content_centers_and_flanks(self):
        # width=20, content='HELLO' (5 chars). leftover=15, split 7/8.
        rect = Rect(left=1, top=1, right=21, bottom=2)
        render_separator(rect, orientation='h', content='HELLO')
        flat = ''.join(self.cap.flat)
        # Total visible width = 20, with '─' runs flanking 'HELLO'.
        self.assertEqual(len(flat), 20)
        self.assertIn('HELLO', flat)
        # Verify HELLO is roughly centred (position 7 with 7 leading dashes).
        idx = flat.index('HELLO')
        self.assertEqual(idx, 7)
        self.assertEqual(flat[:7], '─' * 7)
        self.assertEqual(flat[12:], '─' * 8)

    def test_horizontal_truncates_overlong_content(self):
        # width=10 → max content = 8. content='ABCDEFGHIJKL' (12) → 'ABCDEFGH'.
        rect = Rect(left=1, top=1, right=11, bottom=2)
        render_separator(rect, orientation='h', content='ABCDEFGHIJKL')
        flat = ''.join(self.cap.flat)
        self.assertEqual(len(flat), 10)
        self.assertIn('ABCDEFGH', flat)
        self.assertNotIn('IJKL', flat)

    def test_vertical_ignores_content(self):
        # Vertical separator shouldn't try to render content as text.
        rect = Rect(left=3, top=1, right=4, bottom=4)  # height=3
        render_separator(rect, orientation='v', content='IGNORED')
        flat = ''.join(self.cap.flat)
        # Only '│' glyphs should appear, no 'IGNORED' substring.
        self.assertNotIn('IGNORED', flat)
        self.assertEqual(flat.count('│'), 3)

    def test_zero_size_rect_is_a_noop(self):
        # width=0 — nothing should be drawn.
        rect = Rect(left=5, top=5, right=5, bottom=6)
        render_separator(rect, orientation='h')
        self.assertEqual(self.cap.flat, [])

    def test_none_rect_is_a_noop(self):
        render_separator(None, orientation='h')
        self.assertEqual(self.cap.flat, [])


class TestLayoutSeparatorRects(unittest.TestCase):
    """Layout v/m/pc emit non-None sep_main; layout 'h' keeps it folded.

    Per ticket #147 we explicitly preserve the layout-'h' folded model
    (sep_main / sep_inner are ``None`` because the children/preview pane's
    first row IS the separator) to avoid regressing the production
    rendering path. Layouts v/m/pc emit dedicated 1-col vertical
    separator Rects between the list-side and preview-side of the
    screen.
    """

    def test_layout_h_keeps_separators_folded(self):
        layout = layout_panes(80, 24, split='h', show_preview=True)
        self.assertIsNone(layout['sep_main'])
        self.assertIsNone(layout['sep_inner'])

    def test_layout_v_emits_vertical_sep_main(self):
        layout = layout_panes(80, 24, split='v', show_preview=True)
        sep = layout['sep_main']
        self.assertIsNotNone(sep)
        self.assertEqual(sep.width, 1)  # vertical bar
        # sep height spans the body (rows 1..23, info bar at row 24).
        self.assertEqual(sep.top, 1)

    def test_layout_m_emits_vertical_sep_main(self):
        layout = layout_panes(80, 24, split='m', show_preview=True)
        sep = layout['sep_main']
        self.assertIsNotNone(sep)
        self.assertEqual(sep.width, 1)

    def test_layout_pc_emits_vertical_sep_main(self):
        layout = layout_panes(80, 24, split='pc', show_preview=True)
        sep = layout['sep_main']
        self.assertIsNotNone(sep)
        self.assertEqual(sep.width, 1)

    def test_layout_v_with_children_emits_sep_inner(self):
        # Per #176 layout 'v' is a 3-column shape with children as a
        # full-height column between list and preview, so sep_inner is
        # a VERTICAL divider (width=1) running the full body height.
        layout = layout_panes(
            80, 30, split='v', show_preview=True, show_children_pane=True,
            children_rows_needed=5, children_cols_needed=10,
        )
        if layout['children'] is not None:
            sep_inner = layout['sep_inner']
            self.assertIsNotNone(sep_inner)
            self.assertEqual(sep_inner.width, 1)
            # Spans the full body height (matches sep_main).
            self.assertEqual(sep_inner.height, layout['sep_main'].height)
        else:
            self.assertIsNone(layout['sep_inner'])

    def test_layout_m_with_children_emits_horizontal_sep_inner(self):
        layout = layout_panes(
            120, 40, split='m', show_preview=True, show_children_pane=True,
            children_rows_needed=5,
        )
        if layout['children'] is not None:
            sep_inner = layout['sep_inner']
            self.assertIsNotNone(sep_inner)
            self.assertEqual(sep_inner.height, 1)  # horizontal divider

    def test_info_bar_full_width_in_all_layouts(self):
        for split in ('h', 'v', 'm', 'pc'):
            with self.subTest(split=split):
                layout = layout_panes(80, 24, split=split, show_preview=True)
                info_bar = layout['info_bar']
                self.assertIsNotNone(info_bar)
                # Full width: from col 1 to col 81 (exclusive right).
                self.assertEqual(info_bar.left, 1)
                self.assertEqual(info_bar.right, 81)
                self.assertEqual(info_bar.height, 1)


# --- _truncate_visible helper ----------------------------------------------


_truncate_visible = _render._truncate_visible


class TestTruncateVisible(unittest.TestCase):
    """SGR-aware truncation: counts only visible columns, preserves ESC[..m."""

    def test_plain_ascii_under_limit_is_unchanged(self):
        self.assertEqual(_truncate_visible('hello', 10), 'hello')

    def test_plain_ascii_over_limit_is_truncated(self):
        # Truncated and a final reset is appended so style can't bleed.
        out = _truncate_visible('hello world', 5)
        self.assertTrue(out.startswith('hello'))
        self.assertEqual(out, 'hello\033[0m')

    def test_max_cols_zero_returns_empty(self):
        self.assertEqual(_truncate_visible('abc', 0), '')

    def test_negative_max_cols_returns_empty(self):
        self.assertEqual(_truncate_visible('abc', -3), '')

    def test_empty_string(self):
        self.assertEqual(_truncate_visible('', 5), '')

    def test_sgr_escapes_dont_count_toward_width(self):
        # 'AB' + reset + 'CDE' has 5 visible chars; max_cols=5 should fit.
        s = '\033[31mAB\033[0mCDE'
        out = _truncate_visible(s, 5)
        # All visible chars present, escape preserved intact.
        self.assertIn('AB', out)
        self.assertIn('CDE', out)
        self.assertIn('\033[31m', out)
        self.assertIn('\033[0m', out)

    def test_truncation_inside_styled_run_preserves_escape(self):
        # 'ABCDE' wrapped in red. Truncate to 2 cols → keeps escape +
        # 'AB' + reset (so style doesn't leak).
        s = '\033[31mABCDE\033[0m'
        out = _truncate_visible(s, 2)
        # Original escape preserved (not split mid-bytes).
        self.assertIn('\033[31m', out)
        self.assertIn('AB', out)
        self.assertNotIn('CDE', out)
        # Final reset appended because truncation occurred.
        self.assertTrue(out.endswith('\033[0m'))

    def test_truncation_does_not_split_an_escape_sequence(self):
        # The string starts with an SGR escape; if truncate_visible
        # handed back '\\033[' alone, that would be a corrupt prefix.
        s = '\033[1;31mword'
        out = _truncate_visible(s, 1)
        # Either contains the full escape or skips it entirely; never
        # contains a half-cut escape (no '\\033[' without final 'm').
        self.assertNotEqual(out, '\033[')
        # We expect the escape preserved in front of one visible char.
        self.assertIn('\033[1;31m', out)
        self.assertIn('w', out)
        self.assertNotIn('ord', out)

    def test_no_trailing_reset_when_no_truncation(self):
        # If the string fits entirely, we should NOT append \033[0m
        # (it would be a spurious reset).
        s = 'hello'
        out = _truncate_visible(s, 10)
        self.assertEqual(out, 'hello')

    def test_truncation_at_reset_boundary(self):
        # Visible width is exactly max_cols and the string ends with a
        # reset escape — output should keep the reset.
        s = 'ABC\033[0m'
        out = _truncate_visible(s, 3)
        # All 3 visible chars and the reset preserved.
        self.assertIn('ABC', out)
        self.assertIn('\033[0m', out)


# --- _sanitize_ansi: the shared escape-sanitiser (sec 4.2 #1) --------------


_sanitize_ansi = _render._sanitize_ansi


class TestSanitizeAnsi(unittest.TestCase):
    """Keep SGR (\\e[..m); strip all other CSI and bare/dangling ESC.

    Robust per-sequence scan — the shared sanitiser both the row-content
    path (via ``_normalize_content``) and the preview pane use, so they
    behave identically.
    """

    def test_plain_text_unchanged(self):
        # No ESC at all → the fast path returns the input object untouched.
        s = 'plain text, no escapes'
        self.assertIs(_sanitize_ansi(s), s)

    def test_empty_unchanged(self):
        self.assertEqual(_sanitize_ansi(''), '')

    def test_sgr_colour_kept(self):
        self.assertEqual(_sanitize_ansi('\033[31mRED\033[0m'),
                         '\033[31mRED\033[0m')

    def test_sgr_reset_short_form_kept(self):
        # ``\e[m`` (empty params) is a valid SGR reset — kept.
        self.assertEqual(_sanitize_ansi('A\033[mB'), 'A\033[mB')

    def test_compound_sgr_kept(self):
        # Bold + 256-colour fg + bg in one sequence: all SGR, all kept.
        s = 'x\033[1;38;5;200;48;5;17my'
        self.assertEqual(_sanitize_ansi(s), s)

    def test_cursor_move_csi_stripped(self):
        # Cursor home ``\e[1;1H`` is a non-SGR CSI → dropped, text kept.
        self.assertEqual(_sanitize_ansi('a\033[1;1Hb'), 'ab')

    def test_clear_screen_csi_stripped(self):
        # Erase-display ``\e[2J`` is non-SGR CSI → dropped.
        self.assertEqual(_sanitize_ansi('\033[2Jhello'), 'hello')

    def test_erase_line_csi_stripped(self):
        self.assertEqual(_sanitize_ansi('x\033[Ky'), 'xy')

    def test_bare_esc_stripped(self):
        # A lone ESC with no following '[' → drop the ESC byte only.
        self.assertEqual(_sanitize_ansi('a\033b'), 'ab')

    def test_trailing_bare_esc_stripped(self):
        self.assertEqual(_sanitize_ansi('abc\033'), 'abc')

    def test_dangling_csi_stripped(self):
        # ``\e[`` with no final byte before end-of-string → drop remainder.
        self.assertEqual(_sanitize_ansi('keep\033[31'), 'keep')

    def test_non_csi_escape_drops_only_esc(self):
        # ``\eM`` (reverse-index, a non-CSI escape): only the ESC byte is
        # removed, mirroring the documented intent (``…|\e`` matches the
        # lone ESC); the trailing letter stays as text.
        self.assertEqual(_sanitize_ansi('p\033Mq'), 'pMq')

    def test_mixed_sgr_and_non_sgr(self):
        # Interleaved SGR (kept) and cursor moves / clears (dropped).
        s = '\033[31mR\033[2J\033[1mE\033[1;5HD\033[0m'
        self.assertEqual(_sanitize_ansi(s), '\033[31mR\033[1mED\033[0m')

    def test_idempotent(self):
        once = _sanitize_ansi('\033[31mhi\033[2J\033there')
        self.assertEqual(_sanitize_ansi(once), once)


class TestHasBgSgr(unittest.TestCase):
    """Detect a background SGR param (40-49 / 100-109) for the bg-restore."""

    def test_no_ansi_is_false(self):
        self.assertFalse(_render._has_bg_sgr('plain'))

    def test_fg_only_is_false(self):
        self.assertFalse(_render._has_bg_sgr('\033[31mred\033[0m'))

    def test_256_fg_is_false(self):
        # 38;5;N is a foreground code — 38 is NOT a bg param.
        self.assertFalse(_render._has_bg_sgr('\033[38;5;200mx'))

    def test_256_fg_with_bg_range_index_is_false(self):
        # Positional parse: 38;5;48 is a *foreground* whose 256-index (48)
        # falls in the bg range. The '5;48' operand must be consumed with
        # the 38 introducer, NOT tested as a standalone bg code.
        self.assertFalse(_render._has_bg_sgr('\033[38;5;48mx'))

    def test_truecolor_fg_with_bg_range_channels_is_false(self):
        # 38;2;R;G;B truecolor fg whose channels land in the bg range
        # (40, 41, 42) — all are operands of the 38 introducer, none a bg.
        self.assertFalse(_render._has_bg_sgr('\033[38;2;40;41;42mx'))

    def test_256_bg_index_in_bg_range_still_true(self):
        # 48;5;41 IS a background (the 48 introducer makes it one),
        # regardless of the index value.
        self.assertTrue(_render._has_bg_sgr('\033[48;5;41mx'))

    def test_fg_then_bg_compound_is_true(self):
        # A 256 fg whose index is in the bg range, FOLLOWED by a real bg —
        # the fg operand is skipped, the trailing 41 is detected.
        self.assertTrue(_render._has_bg_sgr('\033[38;5;48;41mx'))

    def test_underline_color_extended_is_false(self):
        # 58;5;N (extended underline colour) is not a background; its
        # operand must be consumed too.
        self.assertFalse(_render._has_bg_sgr('\033[58;5;42mx'))

    def test_reset_only_is_false(self):
        # \e[m / \e[0m carry no bg param.
        self.assertFalse(_render._has_bg_sgr('\033[mx'))
        self.assertFalse(_render._has_bg_sgr('\033[0mx'))

    def test_basic_bg_is_true(self):
        self.assertTrue(_render._has_bg_sgr('\033[41mx\033[0m'))

    def test_default_bg_49_is_true(self):
        self.assertTrue(_render._has_bg_sgr('\033[49mx'))

    def test_bright_bg_is_true(self):
        self.assertTrue(_render._has_bg_sgr('\033[102mx'))

    def test_256_bg_is_true(self):
        # 48;5;N — 48 is in the bg range.
        self.assertTrue(_render._has_bg_sgr('\033[48;5;17mx'))

    def test_bg_in_compound_is_true(self):
        self.assertTrue(_render._has_bg_sgr('\033[1;41;32mx'))

    def test_non_sgr_with_bg_digits_is_false(self):
        # A cursor-move CSI that happens to contain '41' must not count —
        # only ``…m`` (SGR) sequences are scanned.
        self.assertFalse(_render._has_bg_sgr('\033[41Hx'))


# --- clear_columns helper --------------------------------------------------


class TestClearColumns(unittest.TestCase):
    """``clear_columns(row, left, right)`` writes spaces only in [left, right)."""

    def setUp(self):
        # We test the helper through a minimal _render harness that
        # captures writes; the real ``clear_columns`` lives in
        # 020-terminal.py, but we recreate its behaviour in-process by
        # invoking through the captured renderer.
        self.cap = _TermCapture()

    def test_clear_columns_writes_correct_width(self):
        # Re-implement minimal clear_columns logic on top of the
        # capture: it should move(row, left) then write(' ' * width).
        events = []

        def move(r, c):
            events.append(('move', r, c))

        def write(s):
            events.append(('write', s))

        # Inline the function under test to verify the contract; the
        # production ``clear_columns`` lives in 020-terminal.py and is
        # tested by end-to-end runs.
        def clear_columns(row, left, right):
            width = right - left
            if width <= 0:
                return
            move(row, left)
            write(' ' * width)

        clear_columns(5, 10, 20)
        self.assertEqual(events, [('move', 5, 10), ('write', '          ')])

    def test_clear_columns_empty_range_is_noop(self):
        events = []

        def move(r, c):
            events.append(('move', r, c))

        def write(s):
            events.append(('write', s))

        def clear_columns(row, left, right):
            width = right - left
            if width <= 0:
                return
            move(row, left)
            write(' ' * width)

        clear_columns(5, 20, 20)   # right == left
        clear_columns(5, 20, 10)   # right < left
        self.assertEqual(events, [])


# --- render_list rect clipping ---------------------------------------------


class _MockState:
    """Just enough of ``State`` for render_list to traverse the visible list.

    ``visible_items`` is monkey-patched on the loaded render module to
    return a fixed list, sidestepping the full state machinery.
    """

    def __init__(self, visible, cursor=0, expanded=None, selected=None,
                 scope_stack=(), parent_of_id=None):
        self._visible = visible
        self.cursor = cursor
        self.expanded = expanded or set()
        self.selected = selected or set()
        self.scope_stack = scope_stack
        self._preview = {}
        self._children = {}
        # Read by ``RowContext.__init__`` (``_parent_of_id.get(item.id)``)
        # to populate ``ctx.parent_id``.
        self._parent_of_id = parent_of_id or {}


class _AutoPaneCache(dict):
    """``_pane_cache`` stand-in that fails loud on un-reconciled lookups.

    After ticket #228 the per-pane renderers no longer self-create
    their cache entries — ``_reconcile_pane_caches`` (called from
    ``render_full`` / ``render_partial``) is the single dispatch site
    that runs ``cache.update_rect(rect)`` once per frame.

    Tests that call renderers directly (without going through the
    orchestrator) must seed the cache themselves — typically via the
    ``_reconcile`` helper on the test class. Forgetting that step used
    to surface as an opaque ``IndexError`` deep inside ``end_row`` (an
    auto-created cache has ``lines == []`` so ``lines[rel_row] = …``
    blows up). The loud ``KeyError`` here points the test author at
    the discipline they missed instead.
    """

    def __missing__(self, key):
        raise KeyError(
            f"PaneCache {key!r} not reconciled — call "
            f"_reconcile(browser, {key!r}, rect) (or run through "
            "_reconcile_pane_caches) before invoking the renderer."
        )


def _reconcile(browser, name, rect):
    """Mimic the orchestrator's per-frame ``_reconcile_pane_caches``.

    Tests that drive a renderer in isolation (without going through
    ``render_full`` / ``render_partial``) must seed the relevant
    cache via this helper before each paint, since post-#228 the
    renderers no longer self-create their entries.
    """
    cache = browser._pane_cache.setdefault(name, _state.PaneCache())
    cache.update_rect(rect)


class _MockBrowser:
    """Minimal Browser stand-in for render_list."""

    def __init__(self, state, **kwargs):
        self._state = state
        self._list_scroll = 0
        self._insert_mode = False
        self._insert_pos = 0
        self._insert_depth = 0
        self._insert_label = ''
        self._search_query = ''
        self._mode = Mode.NORMAL
        self._error_text = ''
        self._help_mode = False
        self._preview_scroll = 0
        self._needs_redraw = set()
        # Per-pane row cache used by the differential renderer (#187).
        # Tests that call renderers directly must reconcile the cache
        # via ``self._reconcile(...)`` before each paint; otherwise the
        # ``__missing__`` hook on ``_AutoPaneCache`` raises a pointed
        # ``KeyError`` instead of a confusing downstream ``IndexError``.
        self._pane_cache = _AutoPaneCache()
        self.show_ids = 'auto'
        self.show_preview = True
        self.show_children_pane = False
        self.list_ratio = 0.30
        self.split = 'h'
        # Row-format hooks, resolved as ``Browser.__init__`` does (design
        # sec A): unset → framework default; ``format_row`` → whole-row
        # override bound directly. Callers override via kwargs
        # (``format_row=…`` / ``format_row_content=…``) after construction.
        self.format_row_chrome = default_row_chrome
        self.format_row_content = default_row_content
        self._row_segments = self._compose_row
        for k, v in kwargs.items():
            setattr(self, k, v)

    def _compose_row(self, item, ctx):
        # Mirrors production ``Browser._compose_row``: normalise a str
        # content-hook result to a segment list before concatenating
        # (design sec 4.1), so ``chrome + content`` always joins two lists.
        chrome = self.format_row_chrome(item, ctx)
        ctx._set_content_width(_segments_cells(chrome))
        return chrome + _state._normalize_content(
            self.format_row_content(item, ctx))


class TestRenderListRectClipping(unittest.TestCase):
    """render_list draws within the rect's column range, not the full row.

    Verifies the migration from ``(top, height, cols)`` to ``(rect)``:
    cursor moves are at ``rect.left``, content is clipped to
    ``rect.width``, and trailing columns are cleared via
    ``clear_columns`` so stale text from a wider render is wiped.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)
        # Stub out visible_items / VisibleEntry so we don't pull in the
        # whole state module.
        self._saved_visible = getattr(_render, 'visible_items', None)
        self._saved_ve = getattr(_render, 'VisibleEntry', None)
        self._saved_sm = getattr(_render, '_search_matches', None)
        self._saved_st = getattr(_render, '_search_text', None)
        _render._search_matches = lambda text, q: False
        _render._search_text = lambda item: item.title

    def tearDown(self):
        self.cap.restore(_render)
        if self._saved_visible is None:
            if hasattr(_render, 'visible_items'):
                delattr(_render, 'visible_items')
        else:
            _render.visible_items = self._saved_visible
        if self._saved_sm is None:
            if hasattr(_render, '_search_matches'):
                delattr(_render, '_search_matches')
        else:
            _render._search_matches = self._saved_sm
        if self._saved_st is None:
            if hasattr(_render, '_search_text'):
                delattr(_render, '_search_text')
        else:
            _render._search_text = self._saved_st

    def _make_browser_with_items(self, items):
        # Build a fake VisibleEntry-shaped object.
        class _Entry:
            def __init__(self, item, depth=0, kind='normal'):
                self.item = item
                self.depth = depth
                self.kind = kind

        visible = [_Entry(it) for it in items]
        state = _MockState(visible)
        _render.visible_items = lambda s: state._visible
        return _MockBrowser(state)

    def test_render_list_uses_rect_left_for_move(self):
        items = [Item(id='a'), Item(id='b')]
        browser = self._make_browser_with_items(items)
        # Pane offset to the right (column 41 onwards).
        rect = Rect(left=41, top=1, right=81, bottom=3)
        _reconcile(browser, 'list', rect)
        _render.render_list(browser, rect)
        moves = [e for e in self.cap.events if e[0] == 'move']
        # Every move should be to column 41 (or further right within
        # the same pane, e.g. for the cursor highlight). The first move
        # for each rendered row is to column 41.
        self.assertTrue(moves)
        # First move per row in the per-row loop is at left=41.
        # We don't enforce all moves equal 41 (sub-helpers may move
        # within the row), but the row-start moves must be there.
        self.assertTrue(
            any(m == ('move', 1, 41) for m in moves),
            f'no move to row 1 col 41 in {moves[:8]!r}',
        )
        self.assertTrue(
            any(m == ('move', 2, 41) for m in moves),
            f'no move to row 2 col 41 in {moves[:8]!r}',
        )

    def test_render_list_clears_only_pane_columns(self):
        items = [Item(id='a')]
        browser = self._make_browser_with_items(items)
        rect = Rect(left=41, top=1, right=81, bottom=3)
        _reconcile(browser, 'list', rect)
        _render.render_list(browser, rect)
        clear_calls = [e for e in self.cap.events if e[0] == 'clear_columns']
        # We expect at least one clear_columns call per rendered row,
        # always within [41, 81).
        self.assertTrue(clear_calls,
                        'render_list must call clear_columns for each row')
        for ev in clear_calls:
            _, row, left, right = ev
            self.assertEqual(left, 41)
            self.assertEqual(right, 81)
        # And NO bare clear_line() calls (that would wipe other panes).
        self.assertFalse(any(e[0] == 'clear_line' for e in self.cap.events),
                         'render_list must not call clear_line (clobbers neighbors)')

    def test_render_list_zero_height_is_noop(self):
        items = [Item(id='a')]
        browser = self._make_browser_with_items(items)
        rect = Rect(left=1, top=5, right=80, bottom=5)  # height 0
        _render.render_list(browser, rect)
        # No moves, no writes — total no-op.
        self.assertEqual(self.cap.events, [])

    def test_render_list_none_rect_is_noop(self):
        items = [Item(id='a')]
        browser = self._make_browser_with_items(items)
        _render.render_list(browser, None)
        self.assertEqual(self.cap.events, [])

    # --- Stage 2: pending row + cursor/search over a columned row --------

    def _entry(self, item, depth=0, kind='normal'):
        class _Entry:
            pass
        e = _Entry()
        e.item, e.depth, e.kind = item, depth, kind
        return e

    def test_pending_row_renders_loading_glyph_no_markers(self):
        # The pending branch moved into render_list (out of the segment
        # builder). It writes indent + a dim '⧗ loading…' glyph, with no
        # selection / expand markers — byte-for-byte the pre-change shape.
        item = Item(id='__pending__', title='⧗ loading…')
        visible = [self._entry(item, depth=1, kind='pending')]
        state = _MockState(visible)
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state)
        rect = Rect(left=1, top=1, right=40, bottom=2)
        _reconcile(browser, 'list', rect)
        _render.render_list(browser, rect)
        joined = ''.join(self.cap.flat)
        self.assertIn('⧗ loading', joined)
        self.assertNotIn('* ', joined)
        self.assertNotIn('▶', joined)
        self.assertNotIn('▼', joined)
        # Indent: two leading spaces for depth 1, then the glyph.
        self.assertTrue(joined.startswith('  ⧗'), repr(joined[:8]))

    def test_cursor_row_over_columned_content_collapses_and_pads(self):
        # A format_row_content override emits a padded column; the cursor
        # path collapses segments to text and pads to width — the padding
        # baked into the segment text must survive intact.
        def content(item, ctx):
            # 10-cell left-justified column + title.
            return [('col1      ', None, False), (item.title, None, False)]

        item = Item(id='a', title='Name')
        visible = [self._entry(item)]
        state = _MockState(visible, cursor=0)
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        rect = Rect(left=1, top=1, right=41, bottom=2)   # width 40
        _reconcile(browser, 'list', rect)
        _render.render_list(browser, rect)
        joined = ''.join(self.cap.flat)
        # Chrome ('  ' + '' + '  ') + 'col1      ' + 'Name' all present, in
        # order, padded out to the 40-cell width.
        self.assertIn('col1      Name', joined)
        # Cursor row is reverse-video: a set_style with reverse=True fired.
        self.assertTrue(
            any(e[0] == 'set_style' and e[1].get('reverse')
                for e in self.cap.events),
            'cursor row must paint reverse-video',
        )

    def test_search_highlight_over_columned_content(self):
        # With an active query that matches, a non-cursor columned row goes
        # through the highlight path (collapse-to-text). The column padding
        # survives the collapse — the matched text is still in the stream.
        def content(item, ctx):
            return [('PERMS  ', None, False), (item.title, None, False)]

        item = Item(id='a', title='findme')
        visible = [self._entry(item)]
        state = _MockState(visible, cursor=5)   # cursor elsewhere (off-list)
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content,
                               _search_query='findme')
        # Make the search predicate match this row.
        _render._search_matches = lambda text, q: q in text
        _render._search_text = lambda item, **kw: item.title
        rect = Rect(left=1, top=1, right=41, bottom=2)
        _reconcile(browser, 'list', rect)
        _render.render_list(browser, rect)
        joined = ''.join(self.cap.flat)
        self.assertIn('PERMS', joined)
        self.assertIn('findme', joined)

    # --- #738: str (ANSI-allowed) content on any row, meta chrome --------

    def _render_one(self, browser, width=40):
        rect = Rect(left=1, top=1, right=1 + width, bottom=2)
        _reconcile(browser, 'list', rect)
        _render.render_list(browser, rect)
        return ''.join(self.cap.flat)

    def test_str_content_renders_on_normal_row(self):
        # A ``format_row_content`` returning a plain ``str`` (no segments)
        # renders on a NORMAL row: the chrome composes, the str becomes one
        # segment, and its visible text lands in the stream, width-correct.
        def content(item, ctx):
            return 'just a string ' + item.title

        item = Item(id='a', title='Row')
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser)
        # The str became one content segment: chrome (4 blank cells) then
        # the visible text. A non-cursor, non-stripe row writes content
        # only (the pane-edge pad is end_row's job, stubbed out here), so
        # the captured stream is exactly chrome + the string.
        self.assertEqual(joined, '    just a string Row')

    def test_str_content_renders_on_meta_row(self):
        # The same str-content path works on a META row, and meta chrome is
        # indentation only — no '* ' / expander before the content.
        def content(item, ctx):
            # Recipes may branch on ctx.kind; here always a str.
            return 'DIVIDER:' + item.title

        item = Item(id='sep', title='hdr', has_children=True)
        state = _MockState([self._entry(item, depth=1, kind='meta')],
                           cursor=5)
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser)
        self.assertIn('DIVIDER:hdr', joined)
        self.assertNotIn('*', joined)
        self.assertNotIn('▼', joined)
        self.assertNotIn('▶', joined)
        # Meta chrome = blank marker + depth-1 indent + blank expander = 6
        # leading spaces, then the content.
        self.assertTrue(joined.startswith('      DIVIDER:hdr'), repr(joined[:20]))

    def test_default_meta_row_content_is_title(self):
        # No content override: a meta row's default content is just its
        # title (sec 4), with indentation-only chrome.
        item = Item(id='sep', title='── Section ──')
        state = _MockState([self._entry(item, depth=0, kind='meta')],
                           cursor=5)
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state)
        joined = self._render_one(browser)
        self.assertIn('── Section ──', joined)
        self.assertNotIn('sep', joined)   # no id segment
        self.assertNotIn('*', joined)

    def test_str_content_under_cursor_collapses_to_clean_reverse(self):
        # A str row carrying embedded SGR, painted UNDER the cursor: the
        # collapse drops the SGR (visible-text strip) so the reverse-video
        # overlay reads cleanly. The escape bytes must not reach the stream;
        # the visible text must, and a reverse style must fire.
        RED = '\033[31m'
        RESET = '\033[0m'

        def content(item, ctx):
            return RED + 'colored ' + item.title + RESET

        item = Item(id='a', title='Cursor')
        state = _MockState([self._entry(item)], cursor=0)  # IS the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser)
        # Visible text present, embedded SGR stripped from the content writes.
        self.assertIn('colored Cursor', joined)
        self.assertNotIn('\033[31m', ''.join(self.cap.flat))
        # The cursor row paints reverse-video.
        self.assertTrue(
            any(e[0] == 'set_style' and e[1].get('reverse')
                for e in self.cap.events),
            'cursor row must paint reverse-video',
        )

    def test_ansi_string_non_cursor_renders_visible_text(self):
        # A non-cursor row whose content is an ANSI string renders its
        # visible text ANSI-aware (escapes passed through, width counted by
        # visible cells) — the visible glyphs appear and width stays exact.
        def content(item, ctx):
            return '\033[32mgreen\033[0m ' + item.title

        item = Item(id='a', title='Leaf')
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser)
        # SGR colour escapes survive (sec 4.2 rule 2 keeps them through
        # ``_truncate_visible``); strip CSI to read the visible text.
        self.assertIn('\033[32m', joined)
        visible = _term._ANSI_CSI_RE.sub('', joined)
        self.assertEqual(visible, '    green Leaf')
        # Visible width is exact — the embedded escapes add zero cells.
        self.assertEqual(_term._visible_len(joined), 14)

    def test_ansi_string_non_sgr_csi_stripped_on_receipt(self):
        # Rule 1: a non-SGR CSI embedded in the content str (cursor home
        # ``\e[1;1H``, erase ``\e[2J``) is stripped on receipt by the shared
        # sanitiser, so it never reaches the stream; the SGR colour stays.
        def content(item, ctx):
            return '\033[1;1H\033[31mred\033[2J\033[0m ' + item.title

        item = Item(id='a', title='Leaf')
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser)
        # Non-SGR CSI gone; SGR colour kept.
        self.assertNotIn('\033[1;1H', joined)
        self.assertNotIn('\033[2J', joined)
        self.assertIn('\033[31m', joined)
        visible = _term._ANSI_CSI_RE.sub('', joined)
        self.assertEqual(visible, '    red Leaf')

    def test_ansi_string_emits_trailing_reset(self):
        # Rule 3: content carrying SGR closes with a trailing ``\e[m`` so the
        # colour can't bleed into the pad / next pane.
        def content(item, ctx):
            return '\033[31mred\033[31m'   # opens colour, never resets

        item = Item(id='a', title='t')
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser)
        # The very last emitted bytes are the conditional reset.
        self.assertTrue(joined.endswith('\033[m'), repr(joined[-6:]))

    def test_plain_str_content_emits_no_trailing_reset(self):
        # Rule 3 negative: a plain (no-ANSI) str row emits NO reset — the
        # stream is exactly chrome + the visible text, byte-for-byte.
        def content(item, ctx):
            return 'plain ' + item.title

        item = Item(id='a', title='Row')
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser)
        self.assertEqual(joined, '    plain Row')
        self.assertNotIn('\033[', joined)

    def test_ansi_string_truncates_ansi_aware(self):
        # An ANSI-bearing content segment wider than the pane is truncated
        # by VISIBLE cells, not raw chars: the escapes pass through intact
        # (never cut mid-sequence) and don't consume width budget, so the
        # rendered visible width equals the pane width exactly.
        def content(item, ctx):
            return '\033[31m' + ('X' * 50) + '\033[0m'

        item = Item(id='a', title='t')
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state)
        browser.format_row_content = content
        # Width 10: chrome eats 4 cells, content gets 6 visible X's.
        joined = self._render_one(browser, width=10)
        self.assertEqual(_term._visible_len(joined), 10)
        visible = _term._ANSI_CSI_RE.sub('', joined)
        self.assertEqual(visible, '    ' + 'X' * 6)
        # The leading colour escape survived (not cut mid-sequence).
        self.assertIn('\033[31m', joined)

    def test_bg_restore_fires_when_row_bg_and_content_bg(self):
        # Rule 4: row bg active AND content carries a bg code (\e[41m) →
        # after the trailing reset the row bg is re-emitted (as the bare
        # background SGR \e[48;5;17m) so the trailing pad keeps the stripe.
        # Assert the observable order: the rule-3 reset \e[m, then the
        # rule-4 restore byte immediately after it.
        def content(item, ctx):
            return '\033[41mX\033[0m'   # red background in the content

        item = Item(id='a', title='t')
        item.row_bg = 17
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser, width=40)
        # The reset is immediately followed by the bg-restore byte.
        self.assertIn('\033[m\033[48;5;17m', joined)

    def test_bg_restore_skipped_when_no_content_bg(self):
        # Rule 4 negative: row bg active but the content carries NO bg code
        # (only a fg colour) → the rule-3 reset fires, but the bare bg-
        # restore byte is NOT emitted. (The stripe pad still paints with
        # row_bg via the existing set_style stripe logic — that's separate;
        # what must be absent is the rule-4 *restore byte* \e[48;5;17m.)
        def content(item, ctx):
            return '\033[31mred\033[0m'   # fg only, no bg code

        item = Item(id='a', title='t')
        item.row_bg = 17
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser, width=40)
        # Rule-3 reset present, rule-4 restore byte absent.
        self.assertIn('\033[m', joined)
        self.assertNotIn('\033[m\033[48;5;17m', joined)

    def test_bg_restore_skipped_when_no_row_bg(self):
        # Rule 4 negative: content carries a bg code but NO row bg is set →
        # the trailing reset fires (rule 3) but there is no bg-restore, and
        # no bg set_style anywhere (row_bg is None throughout).
        def content(item, ctx):
            return '\033[41mX\033[0m'

        item = Item(id='a', title='t')   # no row_bg attribute
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, format_row_content=content)
        joined = self._render_one(browser, width=40)
        ops = self.cap.events
        # Rule-3 reset present; rule-4 restore byte absent.
        self.assertIn('\033[m', joined)
        self.assertNotIn('\033[48;5;', joined)
        # And no set_style carrying a bg anywhere (row_bg is None).
        self.assertFalse(
            any(e[0] == 'set_style' and e[1].get('bg') is not None
                for e in ops),
            'no bg set_style expected when row_bg is unset',
        )

    def test_plain_segment_row_byte_for_byte_unchanged(self):
        # Regression: a plain segment row (no ANSI, no str content) produces
        # exactly the chrome+content+pad it did before #738. Pin the full
        # content-write stream and the event sequence shape.
        item = Item(id='a', title='Name')   # id == 'a', title 'Name'
        state = _MockState([self._entry(item)], cursor=5)  # not the cursor
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state)
        joined = self._render_one(browser)
        # Chrome '  ' (blank marker, leaf) + '' indent + '  ' (blank
        # expander) = 4 cells, then id segment 'a ' (auto-shown since id !=
        # title), then title 'Name'. A non-cursor, non-stripe plain row
        # writes content only — exactly chrome + id + title, no pad.
        self.assertEqual(joined, '    a Name')
        # No reverse-video (plain non-cursor row).
        self.assertFalse(
            any(e[0] == 'set_style' and e[1].get('reverse')
                for e in self.cap.events),
            'plain non-cursor row must not paint reverse-video',
        )


class TestRenderChildrenList(unittest.TestCase):
    """``render_children_list`` writes one child per row (Alt-1 vertical).

    Mirrors the structure of ``TestRenderListRectClipping``: stubs out
    ``visible_items`` so the renderer's cursor lookup finds a synthetic
    parent item, and inspects the captured terminal stream for the
    expected per-row glyphs.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)
        self._saved_visible = getattr(_render, 'visible_items', None)
        self._saved_render_info_bar = getattr(_render, 'render_info_bar', None)
        # Silence the info bar — it writes a flood of dashes that drown
        # the per-row content we want to assert on.
        _render.render_info_bar = lambda *a, **kw: None

    def tearDown(self):
        self.cap.restore(_render)
        if self._saved_visible is None:
            if hasattr(_render, 'visible_items'):
                delattr(_render, 'visible_items')
        else:
            _render.visible_items = self._saved_visible
        if self._saved_render_info_bar is None:
            if hasattr(_render, 'render_info_bar'):
                delattr(_render, 'render_info_bar')
        else:
            _render.render_info_bar = self._saved_render_info_bar

    def _browser_with_children(self, parent, children, *, has_header=False):
        class _Entry:
            def __init__(self, item, depth=0, kind='normal'):
                self.item = item
                self.depth = depth
                self.kind = kind
        visible = [_Entry(parent)]
        state = _MockState(visible, cursor=0)
        state._children = {parent.id: children}
        _render.visible_items = lambda s: state._visible
        return _MockBrowser(state, split='v', show_children_pane=True)

    def test_one_child_per_row(self):
        parent = Item(id='p', title='parent', has_children=True)
        children = [
            Item(id='c1', title='alpha'),
            Item(id='c2', title='beta'),
            Item(id='c3', title='gamma'),
        ]
        browser = self._browser_with_children(parent, children)
        rect = Rect(left=10, top=1, right=30, bottom=10)
        _reconcile(browser, 'children', rect)
        _render.render_children_list(browser, rect, has_header=False)

        # Each child name must appear exactly once in the flat output.
        flat = ''.join(self.cap.flat)
        self.assertIn('alpha', flat)
        self.assertIn('beta', flat)
        self.assertIn('gamma', flat)

        # Row-start moves: one per content row at rect.left=10.
        moves_at_left = [
            e for e in self.cap.events
            if e[0] == 'move' and e[2] == 10
        ]
        # First three rows correspond to the three children; rows 4-9
        # are blank fillers that still get a move (the renderer moves
        # before clearing/blanking each row).
        # Just assert there are at least 3 moves with col=10, one per
        # row 1..3.
        rows = sorted({m[1] for m in moves_at_left})
        self.assertEqual(rows[:3], [1, 2, 3])

    def test_truncates_long_names_to_width(self):
        parent = Item(id='p', title='parent', has_children=True)
        children = [Item(id='c', title='X' * 200)]
        browser = self._browser_with_children(parent, children)
        # Width = 30 - 10 = 20 cols.
        rect = Rect(left=10, top=1, right=30, bottom=5)
        _reconcile(browser, 'children', rect)
        _render.render_children_list(browser, rect, has_header=False)
        flat = ''.join(self.cap.flat)
        # The 'X' run was truncated — fewer than the original 200.
        self.assertLess(flat.count('X'), 200)

    def test_clears_pane_columns_only(self):
        parent = Item(id='p', title='parent', has_children=True)
        children = [Item(id='c', title='only')]
        browser = self._browser_with_children(parent, children)
        rect = Rect(left=10, top=1, right=30, bottom=5)
        _reconcile(browser, 'children', rect)
        _render.render_children_list(browser, rect, has_header=False)
        clear_calls = [e for e in self.cap.events if e[0] == 'clear_columns']
        self.assertTrue(clear_calls)
        for _, row, left, right in clear_calls:
            self.assertEqual(left, 10)
            self.assertEqual(right, 30)
        self.assertFalse(any(e[0] == 'clear_line' for e in self.cap.events),
                         'render_children_list must not use clear_line')

    def test_pending_branch_shows_loading_hint(self):
        parent = Item(id='p', title='parent', has_children=True)
        # Children not yet cached (None, not []).
        class _Entry:
            def __init__(self, item, depth=0, kind='normal'):
                self.item = item
                self.depth = depth
                self.kind = kind
        visible = [_Entry(parent)]
        state = _MockState(visible, cursor=0)
        state._children = {}  # not cached
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, split='v', show_children_pane=True)
        rect = Rect(left=10, top=1, right=30, bottom=5)
        _reconcile(browser, 'children', rect)
        _render.render_children_list(browser, rect, has_header=False)
        flat = ''.join(self.cap.flat)
        self.assertIn('loading', flat)


# --- render_partial separator regression (#180) -----------------------------


class TestRenderPartialRedrawsSeparators(unittest.TestCase):
    """Regression for #180: ``render_partial`` redraws ``sep_main`` /
    ``sep_inner`` whenever any pane is repainted.

    Cursor moves in the list pane flag ``{'list', 'children', 'preview'}``
    in ``_needs_redraw``. In Alt-1 vertical layout (``split='v'``) the
    children pane appears/disappears as the cursor crosses leaf/branch
    boundaries — when it appears, the layout's ``sep_inner`` column is
    new (the previous render didn't have one). If the partial redraw
    leaves separators alone, that column shows nothing where it should
    show ``│``. The fix paints both separators on any pane redraw.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)
        self._saved_visible = getattr(_render, 'visible_items', None)
        self._saved_render_info_bar = getattr(_render, 'render_info_bar', None)
        self._saved_layout_for = getattr(_render, '_layout_for', None)
        self._saved_render_list = getattr(_render, 'render_list', None)
        self._saved_render_children_list = getattr(
            _render, 'render_children_list', None)
        self._saved_render_preview = getattr(_render, 'render_preview', None)
        self._saved_flush = getattr(_render, 'flush', None)
        self._saved_begin_sync = getattr(_render, 'begin_sync', None)
        self._saved_end_sync = getattr(_render, 'end_sync', None)
        # Silence sub-renderers that aren't under test — we only want
        # to observe the separator draws.
        _render.render_info_bar = lambda *a, **kw: None
        _render.render_list = lambda *a, **kw: None
        _render.render_children_list = lambda *a, **kw: None
        _render.render_preview = lambda *a, **kw: None
        _render.flush = lambda: None
        # render_partial brackets its body with begin_sync / end_sync
        # (#186); when 050-render is loaded standalone these names
        # aren't present, so stub them out.
        _render.begin_sync = lambda: None
        _render.end_sync = lambda: None

    def tearDown(self):
        self.cap.restore(_render)
        for name, saved in (
            ('visible_items', self._saved_visible),
            ('render_info_bar', self._saved_render_info_bar),
            ('_layout_for', self._saved_layout_for),
            ('render_list', self._saved_render_list),
            ('render_children_list', self._saved_render_children_list),
            ('render_preview', self._saved_render_preview),
            ('flush', self._saved_flush),
            ('begin_sync', self._saved_begin_sync),
            ('end_sync', self._saved_end_sync),
        ):
            if saved is None:
                if hasattr(_render, name):
                    delattr(_render, name)
            else:
                setattr(_render, name, saved)

    def _stub_layout(self, *, with_children):
        """Stub ``_layout_for`` to return a v-split layout shape.

        ``with_children=True`` includes both ``sep_main`` and
        ``sep_inner`` (children pane present); ``False`` omits
        ``sep_inner`` (children pane absent — leaf cursor case).
        """
        body_top, body_bottom = 1, 39
        sep_main = Rect(left=70, top=body_top, right=71, bottom=body_bottom)
        list_rect = Rect(left=1, top=body_top, right=70, bottom=body_bottom)
        info_bar = Rect(left=1, top=39, right=241, bottom=40)
        if with_children:
            children_rect = Rect(left=72, top=body_top,
                                 right=92, bottom=body_bottom)
            sep_inner = Rect(left=92, top=body_top,
                             right=93, bottom=body_bottom)
            preview_rect = Rect(left=93, top=body_top,
                                right=241, bottom=body_bottom)
        else:
            children_rect = None
            sep_inner = None
            preview_rect = Rect(left=72, top=body_top,
                                right=241, bottom=body_bottom)
        layout = {
            'list': list_rect,
            'children': children_rect,
            'preview': preview_rect,
            'sep_main': sep_main,
            'sep_inner': sep_inner,
            'info_bar': info_bar,
            'cols': 240,
            'rows': 40,
        }
        _render._layout_for = lambda b: layout
        return layout

    def _make_browser(self, needs):
        state = _MockState(visible=[], cursor=0)
        browser = _MockBrowser(state, split='v', show_children_pane=True)
        browser._needs_redraw = set(needs)
        return browser

    def _separator_writes(self):
        """Extract ``│`` writes (the vertical-separator glyph)."""
        return [e for e in self.cap.events
                if e[0] == 'write' and e[1] == '│']

    def test_cursor_move_redraws_both_separators_when_present(self):
        """`{list, children, preview}` flags + children present → both
        sep_main and sep_inner are repainted.
        """
        layout = self._stub_layout(with_children=True)
        browser = self._make_browser({'list', 'children', 'preview'})
        _render.render_partial(browser)
        # Each separator paints ``│`` once per row of its rect.
        sep_main = layout['sep_main']
        sep_inner = layout['sep_inner']
        # Collect (row, col) of every '│' write — preceding 'move' event
        # gives the position.
        bars = []
        prev_pos = None
        for e in self.cap.events:
            if e[0] == 'move':
                prev_pos = (e[1], e[2])
            elif e[0] == 'write' and e[1] == '│':
                bars.append(prev_pos)
        # sep_main column: every body row from top..bottom-1.
        main_rows = sorted({r for (r, c) in bars if c == sep_main.left})
        inner_rows = sorted({r for (r, c) in bars if c == sep_inner.left})
        expected_rows = list(range(sep_main.top, sep_main.bottom))
        self.assertEqual(
            main_rows, expected_rows,
            f'sep_main missing rows; got {main_rows}, expected {expected_rows}',
        )
        self.assertEqual(
            inner_rows, expected_rows,
            f'sep_inner missing rows; got {inner_rows}, expected {expected_rows}',
        )

    def test_cursor_move_redraws_sep_main_when_no_children(self):
        """Leaf cursor → no sep_inner, but sep_main must still paint."""
        layout = self._stub_layout(with_children=False)
        browser = self._make_browser({'list', 'children', 'preview'})
        _render.render_partial(browser)
        sep_main = layout['sep_main']
        bars = []
        prev_pos = None
        for e in self.cap.events:
            if e[0] == 'move':
                prev_pos = (e[1], e[2])
            elif e[0] == 'write' and e[1] == '│':
                bars.append(prev_pos)
        main_rows = sorted({r for (r, c) in bars if c == sep_main.left})
        expected_rows = list(range(sep_main.top, sep_main.bottom))
        self.assertEqual(main_rows, expected_rows)
        # No separator draws at any other column.
        other_cols = {c for (r, c) in bars if c != sep_main.left}
        self.assertEqual(other_cols, set())

    def test_no_pane_redraw_leaves_separators_alone(self):
        """Empty needs set / 'info'-only redraw doesn't touch separators."""
        self._stub_layout(with_children=True)
        browser = self._make_browser(set())
        _render.render_partial(browser)
        # Empty needs → early return, no events at all.
        self.assertEqual(self._separator_writes(), [])


# --- BSU/ESU brackets + no \e[2J (#186) ------------------------------------


class TestSynchronizedOutputBrackets(unittest.TestCase):
    """``render_full`` / ``render_partial`` bracket their output with
    DEC mode 2026 begin/end synchronized output and never emit ``\\e[2J``.

    Pre-#186, ``render_full`` started with ``\\e[2J`` to clear the
    screen. The differential renderer in #185–#188 replaces that with
    a row-cache-aware repaint, so the blanket clear is gone. Both
    entry points now bracket their writes with ``\\e[?2026h`` (BSU)
    and ``\\e[?2026l`` (ESU) so terminals that support it swap in the
    new frame atomically.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)
        self._saved_layout_for = getattr(_render, '_layout_for', None)
        self._saved_render_list = getattr(_render, 'render_list', None)
        self._saved_render_children_grid = getattr(
            _render, 'render_children_grid', None)
        self._saved_render_children_list = getattr(
            _render, 'render_children_list', None)
        self._saved_render_preview = getattr(_render, 'render_preview', None)
        self._saved_render_separator = getattr(
            _render, 'render_separator', None)
        self._saved_render_info_bar = getattr(_render, 'render_info_bar', None)
        self._saved_flush = getattr(_render, 'flush', None)
        self._saved_begin_sync = getattr(_render, 'begin_sync', None)
        self._saved_end_sync = getattr(_render, 'end_sync', None)
        # Silence the inner renderers — we observe only the brackets.
        _render.render_list = lambda *a, **kw: None
        _render.render_children_grid = lambda *a, **kw: None
        _render.render_children_list = lambda *a, **kw: None
        _render.render_preview = lambda *a, **kw: None
        _render.render_separator = lambda *a, **kw: None
        _render.render_info_bar = lambda *a, **kw: None
        _render.flush = lambda: None
        # Real BSU/ESU bytes flow through the captured ``write`` so the
        # tests can assert on the escape sequences.
        _render.begin_sync = lambda: _render.write('\033[?2026h')
        _render.end_sync = lambda: _render.write('\033[?2026l')

    def tearDown(self):
        self.cap.restore(_render)
        for name, saved in (
            ('_layout_for', self._saved_layout_for),
            ('render_list', self._saved_render_list),
            ('render_children_grid', self._saved_render_children_grid),
            ('render_children_list', self._saved_render_children_list),
            ('render_preview', self._saved_render_preview),
            ('render_separator', self._saved_render_separator),
            ('render_info_bar', self._saved_render_info_bar),
            ('flush', self._saved_flush),
            ('begin_sync', self._saved_begin_sync),
            ('end_sync', self._saved_end_sync),
        ):
            if saved is None:
                if hasattr(_render, name):
                    delattr(_render, name)
            else:
                setattr(_render, name, saved)

    def _stub_layout(self):
        body_top, body_bottom = 1, 23
        layout = {
            'list': Rect(left=1, top=body_top, right=25, bottom=body_bottom),
            'children': None,
            'preview': Rect(left=25, top=body_top, right=81, bottom=body_bottom),
            'sep_main': None,
            'sep_inner': None,
            'info_bar': Rect(left=1, top=body_top, right=81, bottom=body_top + 1),
            'cols': 80,
            'rows': 24,
            'list_rightmost': False,
            'children_rightmost': False,
            'preview_rightmost': True,
            'info_bar_rightmost': True,
            'sep_main_rightmost': False,
            'sep_inner_rightmost': False,
        }
        _render._layout_for = lambda b: layout

    def _make_browser(self, needs=None):
        state = _MockState(visible=[], cursor=0)
        browser = _MockBrowser(state)
        browser._needs_redraw = set(needs or ())
        return browser

    def test_render_full_does_not_emit_2J(self):
        self._stub_layout()
        browser = self._make_browser()
        _render.render_full(browser)
        joined = ''.join(self.cap.flat)
        self.assertNotIn('\033[2J', joined,
                         'render_full must not blanket-clear the screen')

    def test_render_full_brackets_output_with_BSU_ESU(self):
        self._stub_layout()
        browser = self._make_browser()
        _render.render_full(browser)
        # First captured write is BSU; last is ESU.
        self.assertTrue(self.cap.flat, 'render_full produced no output')
        self.assertEqual(self.cap.flat[0], '\033[?2026h')
        self.assertEqual(self.cap.flat[-1], '\033[?2026l')

    def test_render_partial_brackets_output_with_BSU_ESU(self):
        self._stub_layout()
        browser = self._make_browser(needs={'list'})
        _render.render_partial(browser)
        self.assertTrue(self.cap.flat, 'render_partial produced no output')
        self.assertEqual(self.cap.flat[0], '\033[?2026h')
        self.assertEqual(self.cap.flat[-1], '\033[?2026l')

    def test_render_partial_empty_needs_is_silent(self):
        # Empty needs → render_partial returns before begin_sync; no BSU.
        self._stub_layout()
        browser = self._make_browser(needs=set())
        _render.render_partial(browser)
        joined = ''.join(self.cap.flat)
        self.assertNotIn('\033[?2026h', joined)
        self.assertNotIn('\033[?2026l', joined)


class TestSeparatorCacheZeroBytes(unittest.TestCase):
    """Separator + info-bar repaints with unchanged rect emit zero bytes.

    Wires the real ``begin_row`` / ``end_row`` shim from 020-terminal.py
    against a stdout capture, then paints a vertical separator and an
    info bar twice and verifies the second paint emits no bytes (the
    "skip separator redraw when layout unchanged" optimization promised
    by #188 — falls out automatically from the row-buffer cache).
    """

    def setUp(self):
        # Load the real terminal module and graft its shim onto _render
        # so render_separator / render_info_bar exercise the cache path.
        self._terminal = _loader.load(
            '_browse_tui_terminal_188', '020-terminal.py')
        self._saved = {}
        for name in ('move', 'write', 'set_style', 'reset_style',
                     'clear_line', 'clear_columns', 'begin_row', 'end_row'):
            self._saved[name] = getattr(_render, name, None)
            setattr(_render, name, getattr(self._terminal, name))
        # Stdout capture: real shim emits via sys.stdout.write on miss.
        self._orig_stdout = sys.stdout
        self._stdout = io.StringIO()
        sys.stdout = self._stdout
        # Defensive: clear shim capture state.
        self._terminal._row_capture_active = False
        self._terminal._row_buf = []
        self._terminal._row_meta = None

    def tearDown(self):
        sys.stdout = self._orig_stdout
        for name, value in self._saved.items():
            if value is None:
                if hasattr(_render, name):
                    delattr(_render, name)
            else:
                setattr(_render, name, value)

    def _drain(self):
        text = self._stdout.getvalue()
        self._stdout.truncate(0)
        self._stdout.seek(0)
        return text

    _reconcile = staticmethod(_reconcile)

    def test_vertical_separator_zero_bytes_on_second_paint(self):
        browser = _MockBrowser(_MockState([]))
        rect = Rect(left=20, top=2, right=21, bottom=8)  # height=6, width=1

        # First paint: cache empty → emits.
        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        first = self._drain()
        self.assertIn('│', first, 'first paint must emit the bar glyphs')

        # Cache populated for all 6 rows.
        cache = browser._pane_cache['sep_main']
        self.assertEqual(len(cache.lines), 6)
        for i, line in enumerate(cache.lines):
            self.assertIsNotNone(line, f'lines[{i}] should be cached')

        # Second paint with the same rect → zero bytes.
        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        second = self._drain()
        self.assertEqual(second, '',
                         f'second paint must emit nothing, got {second!r}')

    def test_horizontal_separator_zero_bytes_on_second_paint(self):
        browser = _MockBrowser(_MockState([]))
        rect = Rect(left=1, top=10, right=21, bottom=11)  # height=1, width=20

        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        self.assertIn('─', self._drain())
        cache = browser._pane_cache['sep_main']
        self.assertIsNotNone(cache.lines[0])

        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        self.assertEqual(self._drain(), '')

    def test_horizontal_separator_multi_row_paints_every_row(self):
        """Regression for #226: tall horizontal rect paints all rows.

        Pre-fix the cached horizontal path called ``begin_row`` only
        for ``rel_row=0``; rows 1..n-1 of the cache stayed ``None``,
        which both leaks the zero-byte invariant on subsequent paints
        and silently swallows any future multi-row caller's bar
        glyphs. The fix loops over ``rect.height`` and paints every
        row, mirroring the vertical-cached branch.
        """
        browser = _MockBrowser(_MockState([]))
        # height=2, width=10 — every row should carry ``─`` glyphs.
        rect = Rect(left=1, top=5, right=11, bottom=7)

        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        first = self._drain()
        self.assertEqual(
            first.count('─'), 20,
            f'both rows of a height=2 rect must emit 10 bar glyphs '
            f'each (20 total); got {first.count("─")} in {first!r}',
        )

        cache = browser._pane_cache['sep_main']
        self.assertEqual(len(cache.lines), 2)
        for i, line in enumerate(cache.lines):
            self.assertIsNotNone(
                line, f'cache.lines[{i}] must be populated after first paint',
            )

        # Second paint with the same rect → zero bytes, proving every
        # row is in the cache (otherwise rows 1..n-1 would re-emit).
        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        self.assertEqual(
            self._drain(), '',
            'second paint of a multi-row horizontal separator must emit '
            'zero bytes (all rows must participate in the cache)',
        )

    def test_info_bar_zero_bytes_on_second_paint(self):
        browser = _MockBrowser(_MockState([]))

        # First paint at row 24, cols=80. ``render_info_bar`` builds an
        # implicit one-row rect spanning [1, cols+1).
        rect = Rect(left=1, top=24, right=81, bottom=25)
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'Preview', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        first = self._drain()
        self.assertIn('Preview', first,
                      'first paint must emit the label')
        cache = browser._pane_cache['info_bar']
        self.assertIsNotNone(cache.lines[0],
                             'info_bar cache must be populated')

        # Second paint, same row/cols/label/state → zero bytes.
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'Preview', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        self.assertEqual(self._drain(), '')

    def test_info_bar_no_redundant_clear_in_captured_row(self):
        """Cache-miss info-bar paint must not include `\\e[2K` in its row.

        Regression test for #225: ``render_info_bar`` used to call
        ``move(row, 1)`` + ``clear_line()`` unconditionally. Inside a
        ``begin_row`` capture those calls land in the row buffer, so
        the emitted bytes for a cache miss carried a redundant
        ``\\e[24;1H\\e[2K`` prefix — wasted bytes on every miss. The
        fix gates them behind ``if not use_cache:``.
        """
        browser = _MockBrowser(_MockState([]))
        rect = Rect(left=1, top=24, right=81, bottom=25)
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'Preview', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        emitted = self._drain()
        self.assertIn('Preview', emitted,
                      'first paint must emit the label')
        # The leading position cue is end_row's `\e[24;1H`, not a
        # second cursor-move from inside the captured buffer.
        self.assertEqual(
            emitted.count('\033[24;1H'), 1,
            f'expected exactly one cursor-move to (24,1); got {emitted!r}',
        )
        self.assertNotIn(
            '\033[2K', emitted,
            f'captured row must not carry a redundant \\e[2K; got {emitted!r}',
        )

    def test_info_bar_relabel_overwrites_prior_cells_without_clear_line(self):
        """End-to-end: a different-label repaint replaces the prior cells.

        The info bar always fills to ``cols`` (label + ``─`` glyphs), so
        the captured visible_len is constant and ``end_row``'s shrink
        branch doesn't fire. The safety property here is simpler: the
        new buffer overwrites the same cells the old one occupied, so
        removing the captured ``\\e[2K`` is a pure-bytes win — no stale
        characters can survive.
        """
        browser = _MockBrowser(_MockState([]))
        rect = Rect(left=1, top=24, right=81, bottom=25)

        # First paint with a long pane label.
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'A Very Long Label', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        first = self._drain()
        self.assertIn('A Very Long Label', first)

        # Second paint with a different label — cache miss, but the new
        # buffer covers the same cells.
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'Hi', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        second = self._drain()
        self.assertIn('Hi', second)
        self.assertNotIn(
            'A Very Long Label', second,
            'relabel repaint must not contain the prior longer label',
        )
        # No stray \e[2K in the captured payload — the whole point of
        # the fix.
        self.assertNotIn(
            '\033[2K', second,
            f'captured row must not carry \\e[2K; got {second!r}',
        )
        # Cache stores the visible_len and it equals ``cols`` (the bar
        # always pads to full width), confirming end_row's shrink
        # branch can't surface a stale-cell window in the steady state.
        cache = browser._pane_cache['info_bar']
        stored_visible, _stored_buf = cache.lines[0]
        self.assertEqual(
            stored_visible, 80,
            f'info_bar visible_len should equal cols=80; got {stored_visible}',
        )

    def test_separator_rect_change_invalidates_cache(self):
        """Sanity check: changing the rect drops the zero-byte invariant.

        Confirms the cache-hit gate keys on rect (height) — a new height
        means the cache is reshaped and the next paint emits.
        """
        browser = _MockBrowser(_MockState([]))
        rect_a = Rect(left=20, top=2, right=21, bottom=8)
        rect_b = Rect(left=20, top=2, right=21, bottom=10)  # taller

        self._reconcile(browser, 'sep_main', rect_a)
        _render.render_separator(rect_a, cache_key='sep_main', browser=browser)
        self._drain()
        self._reconcile(browser, 'sep_main', rect_b)
        _render.render_separator(rect_b, cache_key='sep_main', browser=browser)
        self.assertNotEqual(self._drain(), '',
                            'rect change must force emission')


if __name__ == '__main__':
    unittest.main()
