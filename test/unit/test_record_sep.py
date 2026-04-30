"""Tests for --record-sep handling, especially NUL safety (find -print0).

Ticket #24: validate that ``--record-sep null`` round-trips raw bytes from
``find -print0`` even when paths contain literal newlines, tabs, or other
metacharacters that would break newline-delimited input.

Phase-1 ticket #4 already implemented ``decode_record_sep`` and the
bytes-level ``_split_records`` helper; these tests harden them with
realistic fixtures and document the known tab-in-tsv-name limitation
(workaround: a single-column ``plain`` parser is on the phase-3 roadmap).
"""

import os
import subprocess
import tempfile
import unittest

from test.unit._loader import load

_cli = load('_browse_tui_cli', '080-cli.py')

decode_record_sep = _cli.decode_record_sep
parse_input = _cli.parse_input


class TestDecodeRecordSep(unittest.TestCase):
    def test_nl_decodes_to_newline(self):
        self.assertEqual(decode_record_sep('nl'), b'\n')

    def test_null_decodes_to_nul_byte(self):
        self.assertEqual(decode_record_sep('null'), b'\x00')

    def test_arbitrary_string_decoded_as_utf8_bytes(self):
        self.assertEqual(decode_record_sep('|||'), b'|||')

    def test_unicode_record_sep(self):
        self.assertEqual(decode_record_sep('▸'), '▸'.encode('utf-8'))


class TestNullRecordSep(unittest.TestCase):
    def test_simple_nul_separated_records(self):
        data = b'a\x00b\x00c\x00'
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        self.assertEqual([r['id'] for r in rows], ['a', 'b', 'c'])

    def test_no_trailing_nul_works(self):
        # find -print0 always appends a trailing NUL, but other producers
        # may not — graceful degradation is required.
        data = b'a\x00b\x00c'  # no terminator after 'c'
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        self.assertEqual([r['id'] for r in rows], ['a', 'b', 'c'])

    def test_paths_with_embedded_newlines(self):
        # The whole point of NUL separation: paths/values can contain \n.
        data = b'foo\nbar\x00baz\nqux\x00'
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        self.assertEqual([r['id'] for r in rows], ['foo\nbar', 'baz\nqux'])

    def test_paths_with_carriage_returns(self):
        # CR is *not* the record sep, so it must be preserved verbatim
        # inside a NUL-delimited record (parse_tsv only strips a *trailing*
        # \r per record, not embedded ones).
        data = b'has\rcr\x00ok\x00'
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        self.assertEqual([r['id'] for r in rows], ['has\rcr', 'ok'])

    def test_paths_with_quotes_and_backslashes(self):
        # tsv treats these as plain UTF-8; no escaping is performed.
        data = b'a"b\\c\x00d\'e\x00'
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        self.assertEqual([r['id'] for r in rows], ['a"b\\c', "d'e"])

    def test_paths_with_tabs_known_limitation(self):
        # With tsv + single-column --fields=id, embedded tabs are still
        # split (because tsv field separator is tab). Document the
        # limitation: for paths with tabs, phase 3 will add a single-column
        # 'plain' parser; for now the tab truncates the field.
        data = b'with\there\x00ok\x00'
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        # First record splits on tab; we asked for one field, so it captures
        # 'with' and drops 'here'. This is a known, documented limitation.
        self.assertEqual([r['id'] for r in rows], ['with', 'ok'])

    def test_empty_input(self):
        rows = list(parse_input(b'', fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        self.assertEqual(rows, [])

    def test_single_record_no_terminator(self):
        # Bare single record (no NUL anywhere) yields one item.
        rows = list(parse_input(b'just-one', fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        self.assertEqual([r['id'] for r in rows], ['just-one'])

    def test_unicode_paths(self):
        # NUL is a single byte; all-UTF-8 paths must round-trip.
        data = 'café\x00naïve\x00日本\x00'.encode('utf-8')
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'\x00'))
        self.assertEqual([r['id'] for r in rows], ['café', 'naïve', '日本'])


class TestFindPrint0Roundtrip(unittest.TestCase):
    """Realistic test: tempdir with files having weird names."""

    def test_find_print0_with_newline_in_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create files with a variety of weird-but-legal names. Tabs
            # are skipped from the assertion below because tsv field
            # splitting truncates them (documented limitation).
            names = [
                'simple.txt',
                'with space.txt',
                'with\nlines.txt',
                'with"quote.txt',
                "with'apostrophe.txt",
                'with\\backslash.txt',
            ]
            for n in names:
                open(os.path.join(tmp, n), 'w').close()
            proc = subprocess.run(
                ['find', tmp, '-maxdepth', '1', '-mindepth', '1', '-print0'],
                capture_output=True, check=True,
            )
            # Parse via the public CLI surface.
            rows = list(parse_input(proc.stdout, fmt='tsv',
                                    fields=['id'], record_sep=b'\x00'))
            ids = {os.path.basename(r['id']) for r in rows}
            for n in names:
                self.assertIn(n, ids, f'{n!r} not in {ids!r}')
            # And no extras crept in (count matches what we wrote).
            self.assertEqual(len(rows), len(names))

    def test_find_print0_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ['find', tmp, '-maxdepth', '1', '-mindepth', '1', '-print0'],
                capture_output=True, check=True,
            )
            rows = list(parse_input(proc.stdout, fmt='tsv',
                                    fields=['id'], record_sep=b'\x00'))
            self.assertEqual(rows, [])


class TestArbitraryRecordSep(unittest.TestCase):
    def test_pipe_separated(self):
        data = b'a|b|c'
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'|'))
        self.assertEqual([r['id'] for r in rows], ['a', 'b', 'c'])

    def test_multibyte_record_sep(self):
        data = 'a‖b‖c'.encode('utf-8')
        rs = '‖'.encode('utf-8')
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=rs))
        self.assertEqual([r['id'] for r in rows], ['a', 'b', 'c'])

    def test_nl_record_sep_unchanged(self):
        # Regression: phase-1 default behaviour must still work.
        data = b'a\nb\nc\n'
        rows = list(parse_input(data, fmt='tsv',
                                fields=['id'], record_sep=b'\n'))
        self.assertEqual([r['id'] for r in rows], ['a', 'b', 'c'])


if __name__ == '__main__':
    unittest.main()
