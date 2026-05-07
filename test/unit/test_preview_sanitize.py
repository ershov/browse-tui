"""Unit tests for ``_sanitize_preview`` (ticket #82).

The preview pane was previously passing raw bytes through
``decode('utf-8', errors='replace')`` straight into ``write()``. Files
containing control chars (binary content, ANSI escapes from a captured
terminal) garbled the screen and were a real injection vector. The
sanitiser replaces every char with code < 32 — except tab (\\x09) and
newline (\\x0a) — plus DEL (0x7f) with '?'.
"""

import unittest

from test.unit._loader import load


_data = load('_browse_tui_data', '030-data.py')
_render = load('_browse_tui_render', '050-render.py')
_render.Item = _data.Item

_sanitize_preview = _render._sanitize_preview


class TestSanitizePreview(unittest.TestCase):

    def test_null_byte_replaced(self):
        self.assertEqual(_sanitize_preview('a\x00b'), 'a?b')

    def test_ansi_escape_preserved(self):
        # ESC (\x1b) is preserved post-#243: the wrap-aware SGR walker
        # in ``_wrap_preview_line`` tokenises CSI sequences and either
        # re-emits them (ANSI-on) or strips them (ANSI-off /
        # search-highlight). Sanitising at this layer would defeat the
        # walker. Non-SGR CSI is dropped by the walker, so the final
        # output is still safe.
        self.assertEqual(
            _sanitize_preview('\x1b[31mRED\x1b[0m'),
            '\x1b[31mRED\x1b[0m',
        )

    def test_tab_preserved(self):
        # Tab is in the keep-set: the renderer expands it to spaces.
        self.assertEqual(_sanitize_preview('col1\tcol2'), 'col1\tcol2')

    def test_newline_preserved(self):
        # Newline is the line separator; sanitiser must not touch it.
        self.assertEqual(_sanitize_preview('line1\nline2'), 'line1\nline2')

    def test_carriage_return_replaced(self):
        # \r (0x0d) is < 32 and not in {tab, LF}, so it must be replaced.
        # CR can rewind the cursor on a real terminal — exactly the
        # injection class we're defending against.
        self.assertEqual(_sanitize_preview('line\rmore'), 'line?more')

    def test_del_byte_replaced(self):
        # DEL (0x7f) is also mapped to '?' as a defensive extension.
        self.assertEqual(_sanitize_preview('a\x7fb'), 'a?b')

    def test_high_unicode_preserved(self):
        # Non-ASCII printable text is left alone.
        self.assertEqual(_sanitize_preview('star unicode tick'),
                         'star unicode tick')
        self.assertEqual(_sanitize_preview('★ hello ✓'),
                         '★ hello ✓')

    def test_empty_string(self):
        self.assertEqual(_sanitize_preview(''), '')

    def test_none_safe_passthrough(self):
        # Defensive: ``''.split('\n')`` works on empty strings; ensure
        # we don't crash on any falsy input the caller might pass.
        self.assertEqual(_sanitize_preview(''), '')

    def test_all_low_control_chars_at_once(self):
        # All 32 codes 0..31 in one string. 29 should become '?'
        # (everything except tab, LF, and ESC), 3 should remain.
        cc = ''.join(chr(i) for i in range(32))
        result = _sanitize_preview(cc)
        self.assertEqual(len(result), 32)
        self.assertEqual(result.count('?'), 29)
        self.assertIn('\t', result)
        self.assertIn('\n', result)
        self.assertIn('\x1b', result)

    def test_form_feed_and_vertical_tab_replaced(self):
        # \x0b (VT) and \x0c (FF) are control chars; renderer column
        # tracking would skew if they reached the terminal.
        self.assertEqual(_sanitize_preview('a\x0bb\x0cc'), 'a?b?c')

    def test_bell_replaced(self):
        # \x07 (BEL) would beep the terminal; defang it.
        self.assertEqual(_sanitize_preview('hello\x07'), 'hello?')

    def test_mixed_content_idempotent(self):
        # Sanitising sanitised text is a no-op (table only matches
        # codes < 32 or 0x7f — none of '?', '\x1b', '[', or ASCII letters
        # are in that set).
        once = _sanitize_preview('\x1b[31mhi\x00\x01')
        twice = _sanitize_preview(once)
        self.assertEqual(once, twice)


if __name__ == '__main__':
    unittest.main()
