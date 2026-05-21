"""Tests for the Item.row_fg renderer attribute.

Mirrors ``row_bg``: per-row foreground colour applied to segments
that don't specify their own ``fg``. Segments with explicit colours
keep them.
"""

import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
# render module needs Item and state helpers
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render._char_width = _term._char_width
_render._visible_len = _term._visible_len


class _Capture:
    """Capture write() / set_style() / reset_style() invocations."""

    def __init__(self):
        self.writes = []
        self.styles = []

    def install(self, mod):
        self._saved = {}
        for name in ('write', 'set_style', 'reset_style'):
            self._saved[name] = getattr(mod, name, None)
        mod.write = lambda s: self.writes.append(s)
        mod.set_style = lambda **kw: self.styles.append(kw)
        mod.reset_style = lambda: self.styles.append({'__reset__': True})

    def restore(self, mod):
        for name, value in self._saved.items():
            if value is None:
                if hasattr(mod, name):
                    delattr(mod, name)
            else:
                setattr(mod, name, value)


class TestRowFg(unittest.TestCase):

    def setUp(self):
        self.cap = _Capture()
        self.cap.install(_render)

    def tearDown(self):
        self.cap.restore(_render)

    def test_row_fg_inherited_by_uncolored_segments(self):
        # Segment without its own fg should pick up row_fg=12.
        _render._write_segments(
            [('hello', None, False)], 20, row_fg=12,
        )
        applied = [s for s in self.cap.styles if s != {'__reset__': True}]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].get('fg'), 12)

    def test_segment_fg_wins_over_row_fg(self):
        # Segment with explicit fg=3 keeps 3, even when row_fg=12 is set.
        _render._write_segments(
            [('hello', 3, False)], 20, row_fg=12,
        )
        applied = [s for s in self.cap.styles if s != {'__reset__': True}]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].get('fg'), 3)

    def test_no_row_fg_no_inheritance(self):
        # Without row_fg, a plain segment writes without a set_style call.
        _render._write_segments(
            [('hello', None, False)], 20,
        )
        applied = [s for s in self.cap.styles if s != {'__reset__': True}]
        self.assertEqual(applied, [])
        # The text is still written.
        self.assertIn('hello', self.cap.writes)

    def test_composes_with_row_bg(self):
        # Both row_fg and row_bg apply; segment without own fg gets fg=12,
        # segment with own fg keeps it, both get bg=4.
        _render._write_segments(
            [('a', None, False), ('b', 3, False)], 20,
            row_fg=12, row_bg=4,
        )
        applied = [s for s in self.cap.styles if s != {'__reset__': True}]
        # Two segment styles + one trailing pad style.
        self.assertGreaterEqual(len(applied), 2)
        # First segment inherits row_fg.
        self.assertEqual(applied[0].get('fg'), 12)
        self.assertEqual(applied[0].get('bg'), 4)
        # Second segment keeps its own fg, still gets row bg.
        self.assertEqual(applied[1].get('fg'), 3)
        self.assertEqual(applied[1].get('bg'), 4)


if __name__ == '__main__':
    unittest.main()
