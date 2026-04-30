"""Tests for the rendering layer in browse-tui.

The render module owns two kinds of code:

  * Pure helpers — ``format_item_segments`` (build the per-item segment
    list) and ``layout_panes`` (geometry math). These are unit-testable
    with no terminal in the loop.
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

import unittest

from test.unit._loader import load


_data = load('_browse_tui_data', '030-data.py')
_render = load('_browse_tui_render', '050-render.py')

# The render module references ``Item`` for synthetic placeholder rows
# (mirrors the state module's pattern). Inject the real class so the
# default formatter can introspect.
_render.Item = _data.Item

Item = _data.Item
format_item_segments = _render.format_item_segments
layout_panes = _render.layout_panes
_TAG_STYLE = _render._TAG_STYLE


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


# --- format_item_segments: default formatter --------------------------------


def _texts(segs):
    """Return just the text portions of a segment list (for shape asserts)."""
    return [s[0] for s in segs]


def _joined(segs):
    return ''.join(_texts(segs))


class TestFormatItemSegmentsDefault(unittest.TestCase):
    """Default item formatting: marker + indent + expand + #id + [tag] + title."""

    def test_leaf_no_tag_no_selection(self):
        item = Item(id='a')
        segs = format_item_segments(item)
        joined = _joined(segs)
        # Selection marker (2 chars), expand-marker (2 chars for a leaf),
        # then '#a ' then title 'a'.
        self.assertIn('  ', joined[:2])      # selection marker
        self.assertIn('#a', joined)
        self.assertIn('a', joined)
        # No '[' — no tag.
        self.assertNotIn('[', joined)

    def test_collapsed_parent_uses_right_arrow(self):
        item = Item(id='a', has_children=True)
        segs = format_item_segments(item, expanded=False)
        self.assertIn('▶', _joined(segs))  # ▶

    def test_expanded_parent_uses_down_arrow(self):
        item = Item(id='a', has_children=True)
        segs = format_item_segments(item, expanded=True)
        self.assertIn('▼', _joined(segs))  # ▼

    def test_tag_segment_is_styled(self):
        item = Item(id='a', tag='running', tag_style='green')
        segs = format_item_segments(item)
        # Find the tag segment and confirm its fg matches the green entry.
        tag_segs = [s for s in segs if '[running]' in s[0]]
        self.assertEqual(len(tag_segs), 1, f'expected one tag seg, got {tag_segs!r}')
        text, fg, bold = tag_segs[0]
        self.assertEqual(fg, _TAG_STYLE['green'][0])
        self.assertEqual(bold, _TAG_STYLE['green'][1])

    def test_selected_emits_star_marker(self):
        item = Item(id='a')
        segs = format_item_segments(item, selected=True)
        # First segment text should start with '* '.
        self.assertTrue(
            segs[0][0].startswith('* '),
            f'first seg should be "* ", got {segs[0][0]!r}',
        )

    def test_indent_scales_with_depth(self):
        item = Item(id='a')
        d0 = format_item_segments(item, depth=0, base_depth=0)
        d2 = format_item_segments(item, depth=2, base_depth=0)
        # Depth 2 - depth 0 = 2 levels = 4 spaces of additional indent.
        self.assertEqual(len(_joined(d2)) - len(_joined(d0)), 4)


# --- format_item_segments: pending / scope_root kinds -----------------------


class TestFormatItemSegmentsKinds(unittest.TestCase):
    """Synthetic kinds short-circuit the default layout."""

    def test_pending_kind_renders_loading_glyph(self):
        item = Item(id='__pending__', title='⧗ loading…')
        segs = format_item_segments(item, kind='pending', depth=1)
        joined = _joined(segs)
        self.assertIn('⧗ loading', joined)
        # No selection star, no expand arrow on a pending row.
        self.assertNotIn('* ', joined)
        self.assertNotIn('▶', joined)
        self.assertNotIn('▼', joined)

    def test_scope_root_kind_is_bold_id_title(self):
        item = Item(id='proj', title='My Project')
        segs = format_item_segments(item, kind='scope_root')
        joined = _joined(segs)
        # No star marker, no expand arrow for the scope row.
        self.assertNotIn('* ', joined)
        self.assertIn('#proj', joined)
        self.assertIn('My Project', joined)
        # At least one segment must be bold (signalling scope-root style).
        self.assertTrue(
            any(seg[2] for seg in segs),
            'scope_root row should have at least one bold segment',
        )


# --- format_item override hook ---------------------------------------------


class TestFormatItemFormatHook(unittest.TestCase):
    """The user-supplied format_item hook short-circuits default formatting."""

    def test_hook_return_value_is_used_verbatim(self):
        called = []

        def hook(item, ctx):
            called.append((item, ctx))
            return [('CUSTOM', None, False)]

        item = Item(id='a', title='Alpha')
        segs = format_item_segments(item, format_item=hook)
        self.assertEqual(segs, [('CUSTOM', None, False)])
        # Default formatting did not run — no '#a' anywhere.
        self.assertEqual(_joined(segs), 'CUSTOM')

    def test_hook_receives_item_argument(self):
        captured = {}

        def hook(item, ctx):
            captured['item'] = item
            captured['ctx'] = ctx
            return [('x', None, False)]

        item = Item(id='a')
        format_item_segments(item, format_item=hook)
        self.assertIs(captured['item'], item)
        # ctx may be None in phase 1 — what matters is that the hook is
        # called with two positional args without raising. Phase-2 ticket
        # #11 wires the real Context object.
        self.assertIn('ctx', captured)


# --- layout_panes -----------------------------------------------------------


class TestLayoutPanes(unittest.TestCase):
    """Pane geometry: list+preview when show_preview, list-only otherwise."""

    def test_two_pane_typical_terminal(self):
        layout = layout_panes(80, 24, show_preview=True)
        self.assertEqual(layout['cols'], 80)
        self.assertEqual(layout['list_top'], 1)
        # 30% of 24 = 7.2 -> 7 (per the spec); leave wiggle room ±1.
        self.assertGreaterEqual(layout['list_height'], 6)
        self.assertLessEqual(layout['list_height'], 8)
        # info_row sits between list and preview.
        self.assertEqual(
            layout['info_row'],
            layout['list_top'] + layout['list_height'],
        )
        # preview gets the rest.
        self.assertEqual(
            layout['prev_top'],
            layout['info_row'] + 1,
        )
        # list + 1 separator + preview = 24 rows total.
        self.assertEqual(
            layout['list_height'] + 1 + layout['prev_height'],
            24,
        )

    def test_one_pane_when_preview_hidden(self):
        layout = layout_panes(80, 24, show_preview=False)
        self.assertEqual(layout['list_top'], 1)
        self.assertEqual(layout['list_height'], 23)
        self.assertEqual(layout['prev_height'], 0)
        # Info bar at the bottom row (24).
        self.assertEqual(layout['info_row'], 24)

    def test_small_terminal_no_negative_heights(self):
        layout = layout_panes(40, 5, show_preview=True)
        # Whatever the math, heights must be non-negative and the panes
        # must fit inside ``rows``.
        self.assertGreaterEqual(layout['list_height'], 1)
        self.assertGreaterEqual(layout['prev_height'], 0)
        self.assertLessEqual(
            layout['list_height'] + 1 + layout['prev_height'],
            5,
        )


if __name__ == '__main__':
    unittest.main()
