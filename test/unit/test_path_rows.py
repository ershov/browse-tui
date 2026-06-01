"""Tests for expand_path_rows — path-like ids → synthesized node rows."""

import unittest

from test.unit._loader import load

_data = load('_browse_tui_data', '030-data.py')

Item = _data.Item
expand_path_rows = _data.expand_path_rows


def _by_id(rows):
    """Index the returned node dicts by id for convenient assertions."""
    return {r['id']: r for r in rows}


def _children_of(rows, parent):
    """Ids of nodes whose parent is ``parent``, in first-seen order."""
    return [r['id'] for r in rows if r['parent'] == parent]


class TestBasicNesting(unittest.TestCase):
    """Basic nesting plus intermediate-node synthesis."""

    def test_docs_api_family(self):
        rows = expand_path_rows([
            'docs/api/auth.md',
            'docs/api/users.md',
            'docs/README.md',
            '/etc/passwd',
        ], '/')
        idx = _by_id(rows)
        # Intermediate nodes are synthesized for every prefix.
        self.assertIn('docs', idx)
        self.assertIn('docs/api', idx)
        self.assertIn('/etc', idx)
        # Top-level nodes get parent=None.
        self.assertIsNone(idx['docs']['parent'])
        self.assertIsNone(idx['/etc']['parent'])
        # Parents are the immediate prefix path.
        self.assertEqual(idx['docs/api']['parent'], 'docs')
        self.assertEqual(idx['docs/api/auth.md']['parent'], 'docs/api')
        self.assertEqual(idx['docs/README.md']['parent'], 'docs')
        self.assertEqual(idx['/etc/passwd']['parent'], '/etc')
        # Titles are the last segment.
        self.assertEqual(idx['docs']['title'], 'docs')
        self.assertEqual(idx['docs/api']['title'], 'api')
        self.assertEqual(idx['docs/api/auth.md']['title'], 'auth.md')
        self.assertEqual(idx['/etc']['title'], 'etc')
        self.assertEqual(idx['/etc/passwd']['title'], 'passwd')

    def test_no_has_children_emitted(self):
        rows = expand_path_rows(['a/b'], '/')
        for r in rows:
            self.assertNotIn('has_children', r)


class TestExplicitPrefixMerge(unittest.TestCase):
    """A prefix that is also an explicit input row merges its fields."""

    def test_explicit_prefix_after_child(self):
        # 'docs/api' first appears synthesized (from the child), then as
        # an explicit row carrying a title + metadata — explicit wins.
        rows = expand_path_rows([
            'docs/api/auth.md',
            {'id': 'docs/api', 'title': 'API Reference', 'tag': 'dir'},
        ], '/')
        idx = _by_id(rows)
        self.assertEqual(idx['docs/api']['title'], 'API Reference')
        self.assertEqual(idx['docs/api']['tag'], 'dir')
        # The node is still in its original first-seen position (no dup).
        self.assertEqual(len(_children_of(rows, 'docs')), 1)
        self.assertEqual([r['id'] for r in rows].count('docs/api'), 1)
        # Structural parent link is preserved through the merge.
        self.assertEqual(idx['docs/api']['parent'], 'docs')

    def test_explicit_prefix_before_child(self):
        # Explicit row first, child later — the leaf metadata stays.
        rows = expand_path_rows([
            {'id': 'docs/api', 'title': 'API', 'size': 7},
            'docs/api/auth.md',
        ], '/')
        idx = _by_id(rows)
        self.assertEqual(idx['docs/api']['title'], 'API')
        self.assertEqual(idx['docs/api']['size'], 7)
        self.assertEqual(idx['docs/api/auth.md']['parent'], 'docs/api')

    def test_carried_parent_column_does_not_clobber_structure(self):
        # A stray 'parent' column on a leaf must not override the
        # synthesized structural link.
        rows = expand_path_rows([
            {'id': 'a/b', 'parent': 'bogus'},
        ], '/')
        idx = _by_id(rows)
        self.assertEqual(idx['a/b']['parent'], 'a')


class TestAbsoluteVsRelative(unittest.TestCase):
    """A leading separator is preserved; absolute != relative."""

    def test_absolute_relative_distinct(self):
        rows = expand_path_rows(['/etc/x', 'etc/x'], '/')
        ids = {r['id'] for r in rows}
        self.assertEqual(ids, {'/etc', '/etc/x', 'etc', 'etc/x'})
        idx = _by_id(rows)
        self.assertIsNone(idx['/etc']['parent'])
        self.assertIsNone(idx['etc']['parent'])
        self.assertEqual(idx['/etc/x']['parent'], '/etc')
        self.assertEqual(idx['etc/x']['parent'], 'etc')
        # Titles drop the leading separator.
        self.assertEqual(idx['/etc']['title'], 'etc')


class TestEmptySegmentCollapse(unittest.TestCase):
    """Doubled and trailing separators collapse away."""

    def test_doubled_separator(self):
        rows = expand_path_rows(['a//b'], '/')
        idx = _by_id(rows)
        self.assertEqual(set(idx), {'a', 'a/b'})
        self.assertIsNone(idx['a']['parent'])
        self.assertEqual(idx['a/b']['parent'], 'a')

    def test_trailing_separator(self):
        rows = expand_path_rows(['a/b/'], '/')
        idx = _by_id(rows)
        self.assertEqual(set(idx), {'a', 'a/b'})
        self.assertEqual(idx['a/b']['parent'], 'a')

    def test_doubled_and_trailing_match_clean(self):
        clean = _by_id(expand_path_rows(['a/b'], '/'))
        doubled = _by_id(expand_path_rows(['a//b'], '/'))
        trailing = _by_id(expand_path_rows(['a/b/'], '/'))
        self.assertEqual(set(clean), set(doubled))
        self.assertEqual(set(clean), set(trailing))


class TestSkippedEntries(unittest.TestCase):
    """Empty / all-separator entries produce no node."""

    def test_empty_string_skipped(self):
        self.assertEqual(expand_path_rows([''], '/'), [])

    def test_all_separator_skipped(self):
        self.assertEqual(expand_path_rows(['///'], '/'), [])

    def test_lone_separator_skipped(self):
        self.assertEqual(expand_path_rows(['/'], '/'), [])

    def test_skipped_among_valid(self):
        rows = expand_path_rows(['a/b', '', 'c'], '/')
        self.assertEqual({r['id'] for r in rows}, {'a', 'a/b', 'c'})


class TestSingleSegment(unittest.TestCase):
    """A single-segment entry is a top-level leaf."""

    def test_single_segment_top_level(self):
        rows = expand_path_rows(['README.md'], '/')
        self.assertEqual(len(rows), 1)
        node = rows[0]
        self.assertEqual(node['id'], 'README.md')
        self.assertIsNone(node['parent'])
        self.assertEqual(node['title'], 'README.md')

    def test_single_segment_absolute(self):
        rows = expand_path_rows(['/etc'], '/')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], '/etc')
        self.assertIsNone(rows[0]['parent'])
        self.assertEqual(rows[0]['title'], 'etc')


class TestMultiCharSeparator(unittest.TestCase):
    """Multi-character and dot separators."""

    def test_double_colon(self):
        rows = expand_path_rows(['std::io::Read'], '::')
        idx = _by_id(rows)
        self.assertEqual(set(idx), {'std', 'std::io', 'std::io::Read'})
        self.assertIsNone(idx['std']['parent'])
        self.assertEqual(idx['std::io']['parent'], 'std')
        self.assertEqual(idx['std::io::Read']['parent'], 'std::io')
        self.assertEqual(idx['std::io::Read']['title'], 'Read')

    def test_dot_separator(self):
        rows = expand_path_rows(['os.path.join'], '.')
        idx = _by_id(rows)
        self.assertEqual(set(idx), {'os', 'os.path', 'os.path.join'})
        self.assertEqual(idx['os.path']['parent'], 'os')
        self.assertEqual(idx['os.path.join']['parent'], 'os.path')
        self.assertEqual(idx['os.path.join']['title'], 'join')


class TestFirstSeenOrdering(unittest.TestCase):
    """Grouping by parent yields first-seen sibling order."""

    def test_sibling_order(self):
        rows = expand_path_rows([
            'docs/api/auth.md',
            'docs/api/users.md',
            'docs/README.md',
        ], '/')
        # Under docs: 'api' (created via first child) precedes README.md.
        self.assertEqual(_children_of(rows, 'docs'), ['docs/api', 'docs/README.md'])
        # Under docs/api: auth.md before users.md.
        self.assertEqual(
            _children_of(rows, 'docs/api'),
            ['docs/api/auth.md', 'docs/api/users.md'],
        )

    def test_intermediate_positioned_at_first_child(self):
        # 'b' is synthesized when 'b/x' first appears, AFTER 'a'.
        rows = expand_path_rows(['a', 'b/x'], '/')
        self.assertEqual(_children_of(rows, None), ['a', 'b'])


class TestExplicitTitleOverride(unittest.TestCase):
    """An explicit leaf title overrides the segment-derived one."""

    def test_dict_title_overrides(self):
        rows = expand_path_rows([{'id': 'a/b.md', 'title': 'Pretty'}], '/')
        idx = _by_id(rows)
        self.assertEqual(idx['a/b.md']['title'], 'Pretty')
        # The intermediate keeps its segment title.
        self.assertEqual(idx['a']['title'], 'a')

    def test_tuple_title_overrides(self):
        rows = expand_path_rows([('a/b', 'Bee')], '/')
        idx = _by_id(rows)
        self.assertEqual(idx['a/b']['title'], 'Bee')

    def test_str_uses_segment_title(self):
        rows = expand_path_rows(['a/b'], '/')
        self.assertEqual(_by_id(rows)['a/b']['title'], 'b')

    def test_item_explicit_title_overrides(self):
        rows = expand_path_rows([Item(id='a/b', title='Bee')], '/')
        self.assertEqual(_by_id(rows)['a/b']['title'], 'Bee')

    def test_item_default_title_uses_segment(self):
        # Item whose title == str(id) is treated as a default → segment wins.
        rows = expand_path_rows([Item(id='a/b')], '/')
        self.assertEqual(_by_id(rows)['a/b']['title'], 'b')


class TestCarriedMetadata(unittest.TestCase):
    """Metadata fields ride along onto the leaf only."""

    def test_dict_extras_on_leaf(self):
        rows = expand_path_rows([{'id': 'a/b', 'size': 99, 'tag': 't'}], '/')
        idx = _by_id(rows)
        self.assertEqual(idx['a/b']['size'], 99)
        self.assertEqual(idx['a/b']['tag'], 't')
        # Intermediate carries no metadata.
        self.assertNotIn('size', idx['a'])
        self.assertNotIn('tag', idx['a'])

    def test_item_tag_carried(self):
        rows = expand_path_rows([Item(id='a/b', tag='x', tag_style='green')], '/')
        idx = _by_id(rows)
        self.assertEqual(idx['a/b']['tag'], 'x')
        self.assertEqual(idx['a/b']['tag_style'], 'green')

    def test_item_extra_attr_carried(self):
        it = Item(id='a/b')
        it.size = 5
        rows = expand_path_rows([it], '/')
        self.assertEqual(_by_id(rows)['a/b']['size'], 5)

    def test_tuple_tag_carried(self):
        rows = expand_path_rows([('a/b', 'B', 'tg', 'red')], '/')
        idx = _by_id(rows)
        self.assertEqual(idx['a/b']['tag'], 'tg')
        self.assertEqual(idx['a/b']['tag_style'], 'red')

    def test_declared_item_fields_do_not_leak(self):
        # Only genuine recipe extras + tag ride onto the leaf; declared
        # dataclass fields (cache/provenance slots) must never leak.
        it = Item(id='a/b', tag='x')
        it.preview = 'cached'
        it.size = 9
        rows = expand_path_rows([it], '/')
        leaf = _by_id(rows)['a/b']
        self.assertEqual(leaf['size'], 9)
        self.assertEqual(leaf['tag'], 'x')
        for leaked in ('preview', 'preview_render', 'synthetic', 'scope_title'):
            self.assertNotIn(leaked, leaf)


if __name__ == '__main__':
    unittest.main()
