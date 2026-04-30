"""Tests for browse-tui input format parsers (tsv, csv, json, json-array,
ifs, split, match)."""

import argparse
import json
import re
import unittest

from test.unit._loader import load

_data = load('_browse_tui_data', '030-data.py')
_cli = load('_browse_tui_cli', '080-cli.py')

Item = _data.Item
to_item = _data.to_item
parse_input = _cli.parse_input
parse_tsv = _cli.parse_tsv
parse_csv = _cli.parse_csv
parse_ifs = _cli.parse_ifs
parse_split = _cli.parse_split
parse_match = _cli.parse_match
parse_json_lines = _cli.parse_json_lines
parse_json_array = _cli.parse_json_array
coerce_has_children = _cli.coerce_has_children
_validate_input_format = _cli._validate_input_format


class TestCoerceHasChildren(unittest.TestCase):
    """Coerce raw values to bool for the has_children field."""

    def test_truthy_one(self):
        self.assertTrue(coerce_has_children('1'))

    def test_truthy_true_lower(self):
        self.assertTrue(coerce_has_children('true'))

    def test_truthy_yes_upper(self):
        self.assertTrue(coerce_has_children('YES'))

    def test_truthy_y_lower(self):
        self.assertTrue(coerce_has_children('y'))

    def test_truthy_on_upper(self):
        self.assertTrue(coerce_has_children('ON'))

    def test_truthy_bool_true(self):
        self.assertTrue(coerce_has_children(True))

    def test_falsy_zero(self):
        self.assertFalse(coerce_has_children('0'))

    def test_falsy_false_lower(self):
        self.assertFalse(coerce_has_children('false'))

    def test_falsy_no(self):
        self.assertFalse(coerce_has_children('no'))

    def test_falsy_n_upper(self):
        self.assertFalse(coerce_has_children('N'))

    def test_falsy_off(self):
        self.assertFalse(coerce_has_children('off'))

    def test_falsy_empty_string(self):
        self.assertFalse(coerce_has_children(''))

    def test_falsy_none(self):
        self.assertFalse(coerce_has_children(None))

    def test_falsy_bool_false(self):
        self.assertFalse(coerce_has_children(False))

    def test_unknown_string_maybe_returns_false(self):
        self.assertFalse(coerce_has_children('maybe'))

    def test_unknown_string_two_returns_false(self):
        # '2' is not a recognised truthy/falsy token in phase 1; falsy.
        self.assertFalse(coerce_has_children('2'))


class TestParseTsv(unittest.TestCase):
    """parse_tsv: TSV records → dicts using positional fields."""

    def test_simple_two_records(self):
        data = b'a\tA\nb\tB\n'
        out = list(parse_tsv(data, fields=['id', 'title']))
        self.assertEqual(out, [
            {'id': 'a', 'title': 'A'},
            {'id': 'b', 'title': 'B'},
        ])

    def test_empty_field_value(self):
        data = b'a\t\nc\tC\n'
        out = list(parse_tsv(data, fields=['id', 'title']))
        self.assertEqual(out, [
            {'id': 'a', 'title': ''},
            {'id': 'c', 'title': 'C'},
        ])

    def test_fewer_columns_than_fields(self):
        # Row has fewer columns than declared fields — missing fields are
        # absent from the dict (not stored as empty strings).
        data = b'a\nb\tB\n'
        out = list(parse_tsv(data, fields=['id', 'title']))
        self.assertEqual(out, [
            {'id': 'a'},
            {'id': 'b', 'title': 'B'},
        ])

    def test_more_columns_than_fields_dropped(self):
        # Extras beyond len(fields) are silently dropped.
        data = b'a\tA\textra1\textra2\n'
        out = list(parse_tsv(data, fields=['id', 'title']))
        self.assertEqual(out, [{'id': 'a', 'title': 'A'}])

    def test_crlf_tolerated(self):
        data = b'a\tA\r\nb\tB\r\n'
        out = list(parse_tsv(data, fields=['id', 'title']))
        self.assertEqual(out, [
            {'id': 'a', 'title': 'A'},
            {'id': 'b', 'title': 'B'},
        ])

    def test_trailing_newline_no_empty_record(self):
        data = b'a\tA\n'
        out = list(parse_tsv(data, fields=['id', 'title']))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], {'id': 'a', 'title': 'A'})

    def test_default_fields_id_title(self):
        data = b'x\tX\n'
        out = list(parse_tsv(data))  # no fields kwarg
        self.assertEqual(out, [{'id': 'x', 'title': 'X'}])

    def test_has_children_coerced_from_string(self):
        data = b'x\t1\n'
        out = list(parse_tsv(data, fields=['id', 'has_children']))
        self.assertEqual(out, [{'id': 'x', 'has_children': True}])
        self.assertIs(out[0]['has_children'], True)

    def test_has_children_coerced_falsy(self):
        data = b'x\t0\ny\tno\nz\t\n'
        out = list(parse_tsv(data, fields=['id', 'has_children']))
        self.assertEqual([d['has_children'] for d in out], [False, False, False])

    def test_record_sep_null(self):
        # NUL-separated records (find -print0 style).
        data = b'a\tA\x00b\tB\x00'
        out = list(parse_tsv(data, fields=['id', 'title'], record_sep=b'\0'))
        self.assertEqual(out, [
            {'id': 'a', 'title': 'A'},
            {'id': 'b', 'title': 'B'},
        ])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(list(parse_tsv(b'')), [])


class TestParseJsonLines(unittest.TestCase):
    """parse_json_lines: one JSON object per record."""

    def test_simple_two_records(self):
        data = b'{"id":"a"}\n{"id":"b"}\n'
        out = list(parse_json_lines(data))
        self.assertEqual(out, [{'id': 'a'}, {'id': 'b'}])

    def test_nested_objects_preserved(self):
        data = b'{"id":"a","data":{"k":1,"n":[1,2,3]}}\n'
        out = list(parse_json_lines(data))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], 'a')
        self.assertEqual(out[0]['data'], {'k': 1, 'n': [1, 2, 3]})

    def test_empty_lines_skipped(self):
        data = b'{"id":"a"}\n\n{"id":"b"}\n\n'
        out = list(parse_json_lines(data))
        self.assertEqual(out, [{'id': 'a'}, {'id': 'b'}])

    def test_malformed_line_skipped_when_not_strict(self):
        data = b'{"id":"a"\n{"id":"b"}\n'
        out = list(parse_json_lines(data, strict=False))
        self.assertEqual(out, [{'id': 'b'}])

    def test_malformed_line_raises_when_strict(self):
        data = b'{"id":"a"\n{"id":"b"}\n'
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            list(parse_json_lines(data, strict=True))

    def test_has_children_coerced_when_string(self):
        # JSON has has_children as a string '1' — should still coerce to bool.
        data = b'{"id":"a","has_children":"1"}\n'
        out = list(parse_json_lines(data))
        self.assertEqual(out[0]['id'], 'a')
        self.assertIs(out[0]['has_children'], True)

    def test_has_children_already_bool_passthrough(self):
        data = b'{"id":"a","has_children":true}\n'
        out = list(parse_json_lines(data))
        self.assertIs(out[0]['has_children'], True)

    def test_record_sep_null(self):
        data = b'{"id":"a"}\x00{"id":"b"}\x00'
        out = list(parse_json_lines(data, record_sep=b'\0'))
        self.assertEqual(out, [{'id': 'a'}, {'id': 'b'}])

    def test_non_object_record_skipped_when_not_strict(self):
        # A JSON value that isn't an object (e.g. an array) is skipped silently.
        data = b'[1,2,3]\n{"id":"a"}\n'
        out = list(parse_json_lines(data))
        self.assertEqual(out, [{'id': 'a'}])

    def test_non_object_record_raises_when_strict(self):
        data = b'[1,2,3]\n'
        with self.assertRaises(ValueError):
            list(parse_json_lines(data, strict=True))


class TestParseJsonArray(unittest.TestCase):
    """parse_json_array: whole bytes are one JSON array."""

    def test_simple_two_elements(self):
        data = b'[{"id":"a"},{"id":"b"}]'
        out = list(parse_json_array(data))
        self.assertEqual(out, [{'id': 'a'}, {'id': 'b'}])

    def test_empty_array_yields_nothing(self):
        out = list(parse_json_array(b'[]'))
        self.assertEqual(out, [])

    def test_non_array_input_skipped_when_not_strict(self):
        # Whole input is a JSON object, not an array — non-strict yields nothing.
        out = list(parse_json_array(b'{"id":"a"}', strict=False))
        self.assertEqual(out, [])

    def test_non_array_input_raises_when_strict(self):
        with self.assertRaises(ValueError):
            list(parse_json_array(b'{"id":"a"}', strict=True))

    def test_whitespace_around_array_ok(self):
        data = b'  \n  [{"id":"a"},{"id":"b"}]\n  '
        out = list(parse_json_array(data))
        self.assertEqual(out, [{'id': 'a'}, {'id': 'b'}])

    def test_record_sep_ignored(self):
        # Even with a non-default record_sep arg via parse_input, the parser
        # treats the whole input as a single JSON array.
        data = b'[{"id":"a"},{"id":"b"}]'
        out_nl = list(parse_input(data, fmt='json-array', record_sep=b'\n'))
        out_nul = list(parse_input(data, fmt='json-array', record_sep=b'\0'))
        self.assertEqual(out_nl, out_nul)
        self.assertEqual(out_nl, [{'id': 'a'}, {'id': 'b'}])

    def test_has_children_coerced(self):
        data = b'[{"id":"a","has_children":"yes"}]'
        out = list(parse_json_array(data))
        self.assertIs(out[0]['has_children'], True)

    def test_malformed_json_skipped_when_not_strict(self):
        out = list(parse_json_array(b'not json at all', strict=False))
        self.assertEqual(out, [])

    def test_malformed_json_raises_when_strict(self):
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            list(parse_json_array(b'not json at all', strict=True))


class TestParseInputDispatch(unittest.TestCase):
    """parse_input dispatches to the right parser based on fmt."""

    def test_dispatch_tsv(self):
        out = list(parse_input(b'a\tA\n', fmt='tsv', fields=['id', 'title']))
        self.assertEqual(out, [{'id': 'a', 'title': 'A'}])

    def test_dispatch_json(self):
        out = list(parse_input(b'{"id":"a"}\n', fmt='json'))
        self.assertEqual(out, [{'id': 'a'}])

    def test_dispatch_json_array(self):
        out = list(parse_input(b'[{"id":"a"}]', fmt='json-array'))
        self.assertEqual(out, [{'id': 'a'}])

    def test_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            list(parse_input(b'x', fmt='nope'))

    def test_dispatch_tsv_default_fields(self):
        # No fields kwarg — defaults to ['id', 'title'].
        out = list(parse_input(b'x\tX\n', fmt='tsv'))
        self.assertEqual(out, [{'id': 'x', 'title': 'X'}])


class TestFieldMapping(unittest.TestCase):
    """--fields maps positional columns; extras land as Item attrs via to_item."""

    def test_extra_columns_become_dict_keys(self):
        data = b'x\tX\t1024\t0755\n'
        out = list(parse_input(
            data,
            fmt='tsv',
            fields=['id', 'title', 'size', 'mode'],
        ))
        self.assertEqual(out, [
            {'id': 'x', 'title': 'X', 'size': '1024', 'mode': '0755'},
        ])

    def test_round_trip_to_item(self):
        gen = parse_input(b'a\tA\n', fmt='tsv', fields=['id', 'title'])
        item = to_item(next(gen))
        self.assertIsInstance(item, Item)
        self.assertEqual(item.id, 'a')
        self.assertEqual(item.title, 'A')

    def test_round_trip_with_extras_lands_as_attrs(self):
        # Extras dict-keys must land as arbitrary Item attrs through to_item.
        gen = parse_input(
            b'x\tX\t1024\t0755\n',
            fmt='tsv',
            fields=['id', 'title', 'size', 'mode'],
        )
        item = to_item(next(gen))
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.title, 'X')
        self.assertEqual(item.size, '1024')
        self.assertEqual(item.mode, '0755')


class TestParseCsv(unittest.TestCase):
    """parse_csv: RFC 4180 CSV records → dicts using positional fields."""

    def test_simple_two_records(self):
        data = b'a,A\nb,B\n'
        out = list(parse_csv(data, fields=['id', 'title']))
        self.assertEqual(out, [
            {'id': 'a', 'title': 'A'},
            {'id': 'b', 'title': 'B'},
        ])

    def test_quoted_field_with_embedded_comma(self):
        data = b'a,"hello, world"\n'
        out = list(parse_csv(data, fields=['id', 'title']))
        self.assertEqual(out, [{'id': 'a', 'title': 'hello, world'}])

    def test_quoted_field_with_embedded_quote(self):
        # RFC 4180 escapes a literal quote by doubling it.
        data = b'a,"she said ""hi"""\n'
        out = list(parse_csv(data, fields=['id', 'title']))
        self.assertEqual(out, [{'id': 'a', 'title': 'she said "hi"'}])

    def test_embedded_newline_breaks_record(self):
        # Limitation: bytes-level record splitting fires before CSV parsing,
        # so a quoted field containing the record separator splits the
        # logical row into two physical records. The first record opens a
        # quote that's never closed, so the csv module raises csv.Error
        # which we swallow in non-strict mode (no record yielded). The
        # second record starts with the orphan tail and parses as a normal
        # row. Document this for users who need multi-line CSV support.
        data = b'a,"line1\nline2"\nb,B\n'
        out = list(parse_csv(data, fields=['id', 'title']))
        # The third record (b,B) is well-formed; earlier broken halves
        # may parse as best-effort rows.
        self.assertIn({'id': 'b', 'title': 'B'}, out)

    def test_mixed_widths(self):
        # Row 1 short, row 2 wider than fields — extras dropped.
        data = b'a\nb,B,extra1,extra2\n'
        out = list(parse_csv(data, fields=['id', 'title']))
        self.assertEqual(out, [
            {'id': 'a'},
            {'id': 'b', 'title': 'B'},
        ])

    def test_has_children_coerced_from_csv_cell(self):
        data = b'x,1\n'
        out = list(parse_csv(data, fields=['id', 'has_children']))
        self.assertEqual(out, [{'id': 'x', 'has_children': True}])
        self.assertIs(out[0]['has_children'], True)


class TestParseIfs(unittest.TestCase):
    """parse_ifs: split each record on any IFS character (bash-style)."""

    def test_etc_passwd_style(self):
        data = b'root:x:0:0:root:/root:/bin/bash\n'
        out = list(parse_ifs(
            data,
            fields=['user', 'pw', 'uid', 'gid', 'gecos', 'home', 'shell'],
            ifs_chars=':',
        ))
        self.assertEqual(out, [{
            'user': 'root', 'pw': 'x', 'uid': '0', 'gid': '0',
            'gecos': 'root', 'home': '/root', 'shell': '/bin/bash',
        }])

    def test_whitespace_collapses_runs(self):
        # IFS=' \t': consecutive whitespace acts like one delimiter.
        # Leading/trailing whitespace stripped — no empty fields produced.
        data = b'   foo \t  bar\t\tbaz   \n'
        out = list(parse_ifs(
            data, fields=['a', 'b', 'c'], ifs_chars=' \t',
        ))
        self.assertEqual(out, [{'a': 'foo', 'b': 'bar', 'c': 'baz'}])

    def test_consecutive_delimiters_yield_empty_field(self):
        # IFS=':' with '::' produces an empty middle field (not collapsed).
        data = b'a::c\n'
        out = list(parse_ifs(
            data, fields=['a', 'b', 'c'], ifs_chars=':',
        ))
        self.assertEqual(out, [{'a': 'a', 'b': '', 'c': 'c'}])

    def test_leading_delimiter_yields_empty_first(self):
        data = b':b:c\n'
        out = list(parse_ifs(
            data, fields=['a', 'b', 'c'], ifs_chars=':',
        ))
        self.assertEqual(out, [{'a': '', 'b': 'b', 'c': 'c'}])

    def test_trailing_delimiter_yields_empty_last(self):
        data = b'a:b:\n'
        out = list(parse_ifs(
            data, fields=['a', 'b', 'c'], ifs_chars=':',
        ))
        self.assertEqual(out, [{'a': 'a', 'b': 'b', 'c': ''}])

    def test_empty_ifs_chars_raises(self):
        with self.assertRaises(ValueError):
            list(parse_ifs(b'x\n', fields=['id'], ifs_chars=''))


class TestParseSplit(unittest.TestCase):
    """parse_split: split each record on a regex pattern."""

    def test_whitespace_runs(self):
        # Awk-style \s+ split.
        data = b'  hello   world\tfoo\n'
        out = list(parse_split(
            data, fields=['a', 'b', 'c'], pattern=r'\s+',
        ))
        # Leading whitespace yields a leading empty field with re.split.
        self.assertEqual(out[0]['a'], '')
        self.assertEqual(out[0]['b'], 'hello')
        self.assertEqual(out[0]['c'], 'world')

    def test_complex_regex(self):
        # Split on comma OR semicolon, optionally followed by spaces.
        data = b'apple, banana; cherry,date\n'
        out = list(parse_split(
            data, fields=['a', 'b', 'c', 'd'], pattern=r'[,;]\s*',
        ))
        self.assertEqual(out, [{
            'a': 'apple', 'b': 'banana', 'c': 'cherry', 'd': 'date',
        }])

    def test_compiled_pattern_accepted(self):
        compiled = re.compile(r'\s+')
        data = b'foo bar\n'
        out = list(parse_split(
            data, fields=['a', 'b'], pattern=compiled,
        ))
        self.assertEqual(out, [{'a': 'foo', 'b': 'bar'}])


class TestParseMatch(unittest.TestCase):
    """parse_match: anchored regex with named groups → dict fields."""

    def test_two_field_pattern(self):
        data = b'apple A red fruit\nbanana A yellow fruit\n'
        out = list(parse_match(
            data, pattern=r'^(?P<id>\S+)\s+(?P<title>.+)$',
        ))
        self.assertEqual(out, [
            {'id': 'apple', 'title': 'A red fruit'},
            {'id': 'banana', 'title': 'A yellow fruit'},
        ])

    def test_non_matching_line_skipped_when_not_strict(self):
        # First line lacks the expected second field — silently skipped.
        data = b'apple\nbanana yellow\n'
        out = list(parse_match(
            data, pattern=r'^(?P<id>\S+)\s+(?P<title>.+)$',
        ))
        self.assertEqual(out, [{'id': 'banana', 'title': 'yellow'}])

    def test_non_matching_line_raises_when_strict(self):
        data = b'apple\n'
        with self.assertRaises(ValueError):
            list(parse_match(
                data,
                pattern=r'^(?P<id>\S+)\s+(?P<title>.+)$',
                strict=True,
            ))

    def test_optional_group_excluded_when_none(self):
        # The `(?P<title>...)` group is optional; when absent it's None
        # and must not appear in the dict.
        data = b'apple\n'
        out = list(parse_match(
            data, pattern=r'^(?P<id>\S+)(?:\s+(?P<title>.+))?$',
        ))
        self.assertEqual(out, [{'id': 'apple'}])
        self.assertNotIn('title', out[0])

    def test_ls_lA_style_pattern(self):
        # Mimic the ls -lA pattern from the design spec example.
        data = (
            b'-rw-r--r--  1 root root  1024 Apr 30 12:00 README.md\n'
            b'drwxr-xr-x  2 root root  4096 Apr 30 12:01 src\n'
        )
        pattern = (
            r'^(?P<mode>\S+)\s+\d+\s+(?P<owner>\S+)\s+\S+\s+'
            r'(?P<size>\d+)\s+\S+\s+\S+\s+\S+\s+(?P<id>.+)$'
        )
        out = list(parse_match(data, pattern=pattern))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]['mode'], '-rw-r--r--')
        self.assertEqual(out[0]['owner'], 'root')
        self.assertEqual(out[0]['size'], '1024')
        self.assertEqual(out[0]['id'], 'README.md')
        self.assertEqual(out[1]['id'], 'src')


class TestParseInputDispatchExtended(unittest.TestCase):
    """parse_input dispatches the new formats correctly."""

    def test_dispatch_csv(self):
        out = list(parse_input(
            b'a,A\n', fmt='csv', fields=['id', 'title'],
        ))
        self.assertEqual(out, [{'id': 'a', 'title': 'A'}])

    def test_dispatch_ifs_colon(self):
        out = list(parse_input(
            b'root:0\n', fmt='ifs::', fields=['user', 'uid'],
        ))
        self.assertEqual(out, [{'user': 'root', 'uid': '0'}])

    def test_dispatch_ifs_whitespace(self):
        out = list(parse_input(
            b'foo\tbar\n', fmt='ifs: \t', fields=['a', 'b'],
        ))
        self.assertEqual(out, [{'a': 'foo', 'b': 'bar'}])

    def test_dispatch_split(self):
        out = list(parse_input(
            b'foo bar\n', fmt=r'split:\s+', fields=['a', 'b'],
        ))
        self.assertEqual(out, [{'a': 'foo', 'b': 'bar'}])

    def test_dispatch_match(self):
        out = list(parse_input(
            b'apple red\n',
            fmt=r'match:^(?P<id>\S+)\s+(?P<title>.+)$',
        ))
        self.assertEqual(out, [{'id': 'apple', 'title': 'red'}])

    def test_unknown_format_still_raises(self):
        with self.assertRaises(ValueError):
            list(parse_input(b'x', fmt='nope:foo'))


class TestValidateInputFormat(unittest.TestCase):
    """_validate_input_format: argparse type callback for --input."""

    def test_bare_formats_accepted(self):
        for v in ('tsv', 'csv', 'json', 'json-array'):
            self.assertEqual(_validate_input_format(v), v)

    def test_prefix_formats_accepted(self):
        self.assertEqual(_validate_input_format('ifs::'), 'ifs::')
        self.assertEqual(_validate_input_format('split:re'), 'split:re')
        self.assertEqual(_validate_input_format('match:re'), 'match:re')

    def test_ifs_without_charset_raises(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            _validate_input_format('ifs:')

    def test_unknown_value_raises(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            _validate_input_format('unknown')

    def test_incomplete_value_raises(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            _validate_input_format('json-')


if __name__ == '__main__':
    unittest.main()
