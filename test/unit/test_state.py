"""Tests for Browser-level state on the state module (040-state.py).

Currently focused on the ``split`` attribute introduced in ticket #146:
clamp helper, constructor param, and ``set_split`` redraw side effect.
Other Browser-level state (list_ratio, expanded, …) is exercised
indirectly by the renderer / actions tests; this module is the natural
home for narrow state-bit tests that would otherwise need a full UI
fixture.
"""

import unittest

from test.unit._loader import load

_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

# Inject Item so any state helper that needs it (e.g. the pending
# placeholder) keeps working when tests use the loader.
_state.Item = _data.Item

Browser = _state.Browser
_clamp_split = _state._clamp_split
_VALID_SPLITS = _state._VALID_SPLITS


class TestClampSplit(unittest.TestCase):
    """``_clamp_split`` is the input gate for both __init__ and set_split."""

    def test_valid_codes_pass_through(self):
        for code in _VALID_SPLITS:
            self.assertEqual(_clamp_split(code), code)

    def test_unknown_code_defaults_to_h(self):
        self.assertEqual(_clamp_split('zz'), 'h')
        self.assertEqual(_clamp_split(''), 'h')

    def test_auto_shorthand_resolves_via_term_size(self):
        # 'auto'/'a' resolve against term_size: <230 cols → 'h', else 'v'.
        # In headless tests term_size falls back to 80, so 'a'/'auto' → 'h'.
        self.assertEqual(_clamp_split('a'), 'h')
        self.assertEqual(_clamp_split('auto'), 'h')
        self.assertEqual(_clamp_split('Auto'), 'h')

    def test_none_defaults_to_h(self):
        self.assertEqual(_clamp_split(None), 'h')

    def test_non_string_defaults_to_h(self):
        self.assertEqual(_clamp_split(42), 'h')
        self.assertEqual(_clamp_split(['v']), 'h')
        self.assertEqual(_clamp_split(object()), 'h')


class TestBrowserSplitConstructor(unittest.TestCase):
    """Browser.__init__ accepts ``split=`` and stores it (clamped)."""

    def test_default_split_resolves_auto(self):
        # Default is 'auto' which resolves via term_size; in headless
        # 80-col tests that yields 'h'.
        b = Browser(_headless=True)
        self.assertEqual(b.split, 'h')

    def test_all_valid_splits_stick(self):
        for code in _VALID_SPLITS:
            b = Browser(split=code, _headless=True)
            self.assertEqual(b.split, code)

    def test_invalid_split_defaults_to_h(self):
        for bad in ('zz', '', 'horizontal'):
            b = Browser(split=bad, _headless=True)
            self.assertEqual(b.split, 'h')

    def test_none_split_defaults_to_h(self):
        b = Browser(split=None, _headless=True)
        self.assertEqual(b.split, 'h')

    def test_non_string_split_defaults_to_h(self):
        b = Browser(split=42, _headless=True)
        self.assertEqual(b.split, 'h')


class TestBrowserSetSplit(unittest.TestCase):
    """``set_split`` clamps + flags the screen for redraw."""

    def test_set_split_stores_valid_value(self):
        b = Browser(_headless=True)
        b._needs_redraw.clear()
        b.set_split('v')
        self.assertEqual(b.split, 'v')

    def test_set_split_marks_full_redraw(self):
        b = Browser(_headless=True)
        b._needs_redraw.clear()
        b.set_split('m')
        self.assertIn('all', b._needs_redraw)

    def test_set_split_clamps_invalid(self):
        b = Browser(split='v', _headless=True)
        b.set_split('garbage')
        self.assertEqual(b.split, 'h')

    def test_set_split_clamps_none(self):
        b = Browser(split='v', _headless=True)
        b.set_split(None)
        self.assertEqual(b.split, 'h')

    def test_set_split_each_valid_round_trip(self):
        b = Browser(_headless=True)
        for code in _VALID_SPLITS:
            b.set_split(code)
            self.assertEqual(b.split, code)


if __name__ == '__main__':
    unittest.main()
