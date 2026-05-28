"""Unit tests for the parser + tree builder in ``recipes/browse-md``.

The recipe is a single-file ``--run-py`` script that imports
``browse_tui`` (only available when the binary loads it). To exercise
the parser / tree-builder helpers directly we stub ``browse_tui`` in
``sys.modules`` and load the extension-less recipe via the
``SourceFileLoader`` — same pattern as
``test/unit/test_browse_claude_render.py``.

Coverage focuses on the helpers exported at module scope:

* ``_line_starts`` / ``_line_of``    (TestLineIndex)
* ``_parse``                          (TestParse)
* ``_walk_list``                      (TestWalkList)
* ``_build_nodes``                    (TestBuildNodes)
* ``get_children``                    (TestGetChildren)
* ``_node_at_line``                   (TestNodeAtLine)
* ``_display_title``                  (TestDisplayTitle)
* ``_resolve_anchor``                 (TestResolveAnchor)

These are the work of tickets #519 (parser), #520 (list walker +
tree linking + Item construction), and #521 (line lookup + anchor
resolution).
"""

import importlib.util
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-md'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import."""
    if 'browse_tui' in sys.modules:
        return
    mod = types.ModuleType('browse_tui')

    class _Stub:
        def __init__(self, *a, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    mod.Action = _Stub
    mod.Browser = _Stub
    mod.BrowserConfig = _Stub
    mod.Item = _Stub
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-md recipe; returns the module.

    ``recipes/browse-md`` has no ``.py`` extension; importlib's
    default loader-from-extension lookup returns None, so we use the
    source loader explicitly. A fresh module instance is created on
    every call so tests that mutate module-level state (notably
    ``_BY_ID`` for ``get_children``) don't bleed into each other.
    """
    _stub_browse_tui()
    name = '_browse_md_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestLineIndex(unittest.TestCase):
    """``_line_starts`` + ``_line_of`` — byte → line conversion."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_empty_string(self):
        self.assertEqual(self.r._line_starts(''), [0])

    def test_no_trailing_newline(self):
        self.assertEqual(self.r._line_starts('abc'), [0])

    def test_trailing_newline(self):
        self.assertEqual(self.r._line_starts('abc\n'), [0, 4])

    def test_two_lines_no_trailing(self):
        self.assertEqual(self.r._line_starts('abc\ndef'), [0, 4])

    def test_two_lines_trailing(self):
        self.assertEqual(self.r._line_starts('abc\ndef\n'), [0, 4, 8])

    def test_only_newlines(self):
        self.assertEqual(self.r._line_starts('\n\n\n'), [0, 1, 2, 3])

    def test_line_of_basic(self):
        # 'abc\ndef\n' — offsets 0..3 are line 0, 4..7 are line 1.
        starts = self.r._line_starts('abc\ndef\n')
        self.assertEqual(self.r._line_of(0, starts), 0)
        self.assertEqual(self.r._line_of(3, starts), 0)
        self.assertEqual(self.r._line_of(4, starts), 1)
        self.assertEqual(self.r._line_of(7, starts), 1)
        # Past EOF still resolves to the last line index.
        self.assertEqual(self.r._line_of(8, starts), 2)

    def test_line_of_only_newlines(self):
        starts = self.r._line_starts('\n\n\n')  # [0, 1, 2, 3]
        self.assertEqual(self.r._line_of(0, starts), 0)
        self.assertEqual(self.r._line_of(1, starts), 1)
        self.assertEqual(self.r._line_of(2, starts), 2)
        self.assertEqual(self.r._line_of(3, starts), 3)

    def test_line_of_empty(self):
        starts = self.r._line_starts('')  # [0]
        self.assertEqual(self.r._line_of(0, starts), 0)


class TestParse(unittest.TestCase):
    """``_parse`` emits ordered ``(kind, payload)`` events."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _kinds(self, events):
        return [k for k, _ in events]

    def test_each_heading_level(self):
        text = '# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6\n'
        events = self.r._parse(text)
        self.assertEqual(self._kinds(events),
                         ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        # Each heading carries a payload with the contract fields.
        for kind, payload in events:
            self.assertIn('byte_offset', payload)
            self.assertIn('line_offset', payload)
            self.assertIn('source', payload)
        # H1 starts at byte 0 / line 0.
        self.assertEqual(events[0][1]['byte_offset'], 0)
        self.assertEqual(events[0][1]['line_offset'], 0)
        self.assertEqual(events[0][1]['source'], '# H1')
        # H2 is on line 1 (after '# H1\n').
        self.assertEqual(events[1][1]['line_offset'], 1)
        self.assertEqual(events[1][1]['byte_offset'], 5)

    def test_frontmatter_consumed_silently(self):
        text = '---\ntitle: foo\n---\n# H1\n'
        events = self.r._parse(text)
        self.assertEqual(self._kinds(events), ['h1'])
        # The trailing H1 carries the correct post-frontmatter offsets.
        bo = events[0][1]['byte_offset']
        self.assertEqual(text[bo:bo + 4], '# H1')

    def test_hr_mid_document_not_frontmatter(self):
        # ``---`` not at offset 0 is an HR (no event), NOT frontmatter
        # (frontmatter has \A anchor). The H1s on either side emit.
        text = '# H1\n\n---\n\n## H2\n'
        events = self.r._parse(text)
        self.assertEqual(self._kinds(events), ['h1', 'h2'])

    def test_heading_inside_fenced_code_block_masked(self):
        text = '```\n# fake heading\n```\n# real heading\n'
        events = self.r._parse(text)
        # Only the real heading emits.
        self.assertEqual(self._kinds(events), ['h1'])
        self.assertEqual(events[0][1]['source'], '# real heading')

    def test_heading_inside_blockquote_masked(self):
        # The blockquote rule starts with ``>`` so the ``> # fake``
        # line is consumed by the blockquote rule rather than emitting.
        text = '> # fake heading\n> still in quote\n\n# real heading\n'
        events = self.r._parse(text)
        # Only the real heading should emit; the quoted one is masked.
        kinds = self._kinds(events)
        self.assertEqual(kinds.count('h1'), 1)
        self.assertEqual(events[-1][1]['source'], '# real heading')

    def test_heading_inside_table_masked(self):
        # Lines starting with ``|`` are absorbed by the table rule.
        text = '| col |\n| # fake heading |\n\n# real heading\n'
        events = self.r._parse(text)
        kinds = self._kinds(events)
        # The pipe-bordered ``# fake heading`` line is INSIDE the table
        # block and never emits a heading event.
        self.assertEqual(kinds.count('h1'), 1)
        self.assertEqual(events[-1][1]['source'], '# real heading')

    def test_ul_list(self):
        text = '- foo\n- bar\n'
        events = self.r._parse(text)
        self.assertEqual(self._kinds(events), ['ul', 'ul'])
        # Levels for unindented markers are 0.
        self.assertEqual(events[0][1]['level'], 0)
        self.assertEqual(events[1][1]['level'], 0)

    def test_ol_list(self):
        text = '1. one\n2. two\n'
        events = self.r._parse(text)
        self.assertEqual(self._kinds(events), ['ol', 'ol'])

    def test_list_with_continuation_strict(self):
        # md2ansi's ``_MD_LIST`` is strict: every line of the block
        # MUST be a marker line. A continuation line breaks the match,
        # which produces TWO separate list matches (one per side of
        # the continuation). We don't get a continuation event — only
        # the marker-lines emit.
        text = '- foo\n  more text\n- bar\n'
        events = self.r._parse(text)
        kinds = self._kinds(events)
        # Two ``ul`` events total.
        self.assertEqual(kinds, ['ul', 'ul'])
        # Both list items recover their original ``source`` line.
        self.assertEqual(events[0][1]['source'], '- foo')
        self.assertEqual(events[1][1]['source'], '- bar')

    def test_mixed_ul_ol_markers_brittle(self):
        # ``_MD_LIST`` requires uniform leading-marker lines: a single
        # ``_MD_LIST`` match accepts mixed ``-`` and ``\d+.`` lines
        # because the alternation is per-line. So this whole block is
        # one match and the walker fans it into two events: one ``ul``
        # and one ``ol``. (If md2ansi's rule ever tightens, this is the
        # observed-behavior canary — feel free to update.)
        text = '- foo\n1. bar\n'
        events = self.r._parse(text)
        kinds = self._kinds(events)
        # Two events — one for each marker line, with their respective
        # kinds (``ul`` for ``-``, ``ol`` for ``1.``).
        self.assertEqual(kinds, ['ul', 'ol'])


class TestWalkList(unittest.TestCase):
    """``_walk_list`` fans one list match out into per-line events."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _events_for(self, text):
        line_starts = self.r._line_starts(text)
        m = self.r._PARSER_RE.search(text)
        self.assertIsNotNone(m, f'no match in {text!r}')
        return self.r._walk_list(m, line_starts)

    def test_single_level_indent_zero(self):
        events = self._events_for('- a\n- b\n- c\n')
        self.assertEqual([e[0] for e in events], ['ul', 'ul', 'ul'])
        self.assertEqual([e[1]['level'] for e in events], [0, 0, 0])

    def test_nested_two_space_indent(self):
        # ``- a / 2-space ``  - b`` / 2-space ``  - c``. Two spaces of
        # indent → ``len(indent)//2 = 1``.
        events = self._events_for('- a\n  - b\n  - c\n')
        self.assertEqual([e[1]['level'] for e in events], [0, 1, 1])

    def test_tab_indent_expanded_to_four_spaces(self):
        # A leading tab expands to 4 spaces; 4 // 2 == 2.
        events = self._events_for('\t- foo\n')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 'ul')
        self.assertEqual(events[0][1]['level'], 2)

    def test_marker_kinds(self):
        # ``-``, ``*``, ``+`` → ``ul``; trailing ``.`` (digit + dot) → ``ol``.
        for marker, kind in (('-', 'ul'), ('*', 'ul'), ('+', 'ul'),
                             ('1.', 'ol')):
            text = f'{marker} foo\n'
            events = self._events_for(text)
            self.assertEqual(len(events), 1, f'{marker!r}: {events}')
            self.assertEqual(events[0][0], kind, f'{marker!r}')


class TestBuildNodes(unittest.TestCase):
    """``_build_nodes`` materialises events into a tree of Items."""

    FIXTURE = (
        '# H1\n'        # line 0
        '## H2a\n'      # line 1
        '- a\n'         # line 2
        '  - a1\n'      # line 3
        '- b\n'         # line 4
        '## H2b\n'      # line 5
        '1. one\n'      # line 6
        '# H1b\n'       # line 7
        '- top\n'       # line 8
    )

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()
        cls.path = '/tmp/fake.md'
        cls.line_starts = cls.r._line_starts(cls.FIXTURE)
        cls.events = cls.r._parse(cls.FIXTURE)
        cls.root, cls.by_id = cls.r._build_nodes(
            cls.events, cls.FIXTURE, cls.line_starts, cls.path,
        )

    def test_root_id_and_kind(self):
        self.assertEqual(self.root.id, self.path)
        self.assertEqual(self.root.kind, 'root')
        self.assertEqual(self.root.level, 0)
        self.assertTrue(self.root.has_children)

    def test_root_has_two_h1_children(self):
        kids = self.root._children
        self.assertEqual(len(kids), 2)
        self.assertEqual([k.tag for k in kids], ['h1', 'h1'])
        self.assertEqual(kids[0].title, '# H1')
        self.assertEqual(kids[1].title, '# H1b')

    def test_first_h1_has_two_h2_children(self):
        h1 = self.root._children[0]
        kids = h1._children
        self.assertEqual(len(kids), 2)
        self.assertEqual([k.tag for k in kids], ['h2', 'h2'])
        self.assertEqual(kids[0].title, '## H2a')
        self.assertEqual(kids[1].title, '## H2b')

    def test_h2a_has_two_list_items(self):
        h2a = self.root._children[0]._children[0]
        kids = h2a._children
        self.assertEqual(len(kids), 2)
        self.assertEqual([k.tag for k in kids], ['ul', 'ul'])
        self.assertEqual(kids[0].title, '- a')
        self.assertEqual(kids[1].title, '- b')

    def test_a_has_one_nested_child(self):
        h2a = self.root._children[0]._children[0]
        a = h2a._children[0]
        self.assertEqual(len(a._children), 1)
        self.assertEqual(a._children[0].title, '- a1')
        self.assertTrue(a.has_children)

    def test_h2b_has_one_ol_child(self):
        h2b = self.root._children[0]._children[1]
        self.assertEqual(len(h2b._children), 1)
        self.assertEqual(h2b._children[0].tag, 'ol')
        self.assertEqual(h2b._children[0].title, '1. one')

    def test_second_h1_has_one_list_child(self):
        h1b = self.root._children[1]
        self.assertEqual(len(h1b._children), 1)
        self.assertEqual(h1b._children[0].title, '- top')

    def test_byte_size_spans_to_next_sibling_or_shallower(self):
        # First H1 covers lines 0..6 (up to '# H1b' on line 7).
        h1 = self.root._children[0]
        h1b = self.root._children[1]
        self.assertEqual(h1.byte_offset + h1.byte_size, h1b.byte_offset)
        self.assertEqual(h1.line_offset + h1.line_size, h1b.line_offset)
        # Slicing into file_text reconstructs the section.
        sliced = self.FIXTURE[h1.byte_offset:h1.byte_offset + h1.byte_size]
        self.assertTrue(sliced.startswith('# H1\n'))
        # Section ends right before the next top-level heading.
        self.assertNotIn('# H1b', sliced)

    def test_last_nodes_byte_size_runs_to_eof(self):
        # The second H1's section extends to len(file_text).
        h1b = self.root._children[1]
        self.assertEqual(h1b.byte_offset + h1b.byte_size, len(self.FIXTURE))
        self.assertEqual(h1b.line_offset + h1b.line_size,
                         len(self.line_starts))
        # And so does its sole child (the ``- top`` list item).
        top = h1b._children[0]
        self.assertEqual(top.byte_offset + top.byte_size, len(self.FIXTURE))

    def test_h2a_byte_span_stops_at_h2b(self):
        # ``## H2a`` ends where ``## H2b`` begins (sibling-or-shallower).
        h1 = self.root._children[0]
        h2a, h2b = h1._children
        self.assertEqual(h2a.byte_offset + h2a.byte_size, h2b.byte_offset)

    def test_list_scope_resets_on_heading_boundary(self):
        # The ``- a`` item's scope ends at ``## H2b`` (next heading),
        # not at ``- top`` later in the file.
        h2a = self.root._children[0]._children[0]
        a = h2a._children[0]  # ``- a``
        h2b = self.root._children[0]._children[1]
        self.assertEqual(a.byte_offset + a.byte_size <= h2b.byte_offset, True)

    def test_ids_for_non_root(self):
        # Non-root ids: ``<path>#<line_offset>``.
        h1 = self.root._children[0]
        self.assertEqual(h1.id, f'{self.path}#0')
        h2a = h1._children[0]
        self.assertEqual(h2a.id, f'{self.path}#1')
        h2b = h1._children[1]
        self.assertEqual(h2b.id, f'{self.path}#5')
        h1b = self.root._children[1]
        self.assertEqual(h1b.id, f'{self.path}#7')

    def test_kind_field(self):
        # Every Item has a ``kind`` of root | heading | list-item.
        self.assertEqual(self.root.kind, 'root')
        for h1 in self.root._children:
            self.assertEqual(h1.kind, 'heading')
        h2a = self.root._children[0]._children[0]
        self.assertEqual(h2a.kind, 'heading')
        a = h2a._children[0]
        self.assertEqual(a.kind, 'list-item')

    def test_level_field(self):
        # Headings: 1..6. Lists: indent level. Root: 0.
        self.assertEqual(self.root.level, 0)
        h1 = self.root._children[0]
        self.assertEqual(h1.level, 1)
        h2a = h1._children[0]
        self.assertEqual(h2a.level, 2)
        # ``- a`` is at indent 0.
        a = h2a._children[0]
        self.assertEqual(a.level, 0)
        # ``- a1`` is at indent 1 (2 spaces → 2//2 = 1).
        a1 = a._children[0]
        self.assertEqual(a1.level, 1)

    def test_tag_style_per_heading_level(self):
        h1 = self.root._children[0]
        self.assertEqual(h1.tag_style, 'red')
        h2a = h1._children[0]
        self.assertEqual(h2a.tag_style, 'yellow')
        # Lists carry ``dim`` regardless of level.
        a = h2a._children[0]
        self.assertEqual(a.tag_style, 'dim')

    def test_tag_field(self):
        h1 = self.root._children[0]
        self.assertEqual(h1.tag, 'h1')
        h2a = h1._children[0]
        self.assertEqual(h2a.tag, 'h2')
        a = h2a._children[0]
        self.assertEqual(a.tag, 'ul')
        h2b = h1._children[1]
        self.assertEqual(h2b._children[0].tag, 'ol')

    def test_title_no_hash_stripping(self):
        # ``## H2a`` title is the source line verbatim (rstripped + lstripped).
        h2a = self.root._children[0]._children[0]
        self.assertEqual(h2a.title, '## H2a')

    def test_has_children_matches_tree_shape(self):
        # Root, h1, h2a, ``- a`` all have children; leaves don't.
        self.assertTrue(self.root.has_children)
        h1 = self.root._children[0]
        self.assertTrue(h1.has_children)
        h2a = h1._children[0]
        self.assertTrue(h2a.has_children)
        a = h2a._children[0]
        self.assertTrue(a.has_children)
        a1 = a._children[0]
        self.assertFalse(a1.has_children)
        # ``- b`` (sibling of ``- a``) is a leaf.
        b = h2a._children[1]
        self.assertFalse(b.has_children)

    def test_by_id_contains_root_and_every_node(self):
        # Root + 9 events (2 H1 + 2 H2 + 4 list items + 1 ol).
        self.assertEqual(len(self.by_id), 1 + len(self.events))
        self.assertIn(self.path, self.by_id)
        # Each non-root id resolves to an Item.
        h1 = self.root._children[0]
        self.assertIs(self.by_id[h1.id], h1)


class TestGetChildren(unittest.TestCase):
    """``get_children`` reads cached ``_children`` off of ``_BY_ID``."""

    def setUp(self):
        # Fresh module per test — ``_BY_ID`` is module-level state.
        self.r = _load_recipe()
        fixture = (
            '# H1\n'
            '## H2\n'
            '- a\n'
            '- b\n'
        )
        self.path = '/tmp/getchildren.md'
        line_starts = self.r._line_starts(fixture)
        events = self.r._parse(fixture)
        self.root, by_id = self.r._build_nodes(
            events, fixture, line_starts, self.path,
        )
        # Populate the module-level index ``get_children`` reads from.
        self.r._BY_ID = by_id

    def test_root_children(self):
        kids = self.r.get_children(self.root.id)
        self.assertEqual(len(kids), 1)
        self.assertEqual(kids[0].tag, 'h1')

    def test_heading_children(self):
        h1 = self.root._children[0]
        kids = self.r.get_children(h1.id)
        self.assertEqual(len(kids), 1)
        self.assertEqual(kids[0].tag, 'h2')

    def test_unknown_id_returns_empty(self):
        self.assertEqual(self.r.get_children('nonexistent'), [])
        self.assertEqual(self.r.get_children('/some/path#999'), [])

    def test_returned_list_is_a_copy(self):
        # Mutating the returned list MUST NOT corrupt the cached
        # ``_children`` on the underlying Item.
        h1 = self.root._children[0]
        kids = self.r.get_children(h1.id)
        original_len = len(h1._children)
        kids.append('JUNK')
        kids.clear()
        self.assertEqual(len(h1._children), original_len)
        # And a subsequent call still returns the canonical kids.
        kids_again = self.r.get_children(h1.id)
        self.assertEqual(len(kids_again), original_len)


class TestEdgeCases(unittest.TestCase):
    """Empty / paragraph-only / leading-list / no-trailing-newline."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _build(self, text, path='/tmp/edge.md'):
        line_starts = self.r._line_starts(text)
        events = self.r._parse(text)
        root, by_id = self.r._build_nodes(events, text, line_starts, path)
        return root, by_id, line_starts

    def test_empty_file(self):
        root, by_id, line_starts = self._build('')
        self.assertEqual(root._children, [])
        self.assertFalse(root.has_children)
        # ``by_id`` still carries the root.
        self.assertIn(root.id, by_id)

    def test_paragraphs_only_no_children(self):
        text = 'Just a paragraph.\n\nAnd another one.\n'
        root, _, _ = self._build(text)
        self.assertEqual(root._children, [])
        self.assertFalse(root.has_children)

    def test_list_before_any_heading_attaches_to_root(self):
        text = '- top1\n- top2\n# After\n'
        root, _, _ = self._build(text)
        # ``- top1`` and ``- top2`` attach to root (no open heading
        # when they're emitted); ``# After`` also attaches to root.
        kids = root._children
        tags = [k.tag for k in kids]
        self.assertIn('ul', tags)
        self.assertIn('h1', tags)
        # Both ``ul`` items appear before the ``h1``.
        ul_indices = [i for i, k in enumerate(kids) if k.tag == 'ul']
        h1_index = next(i for i, k in enumerate(kids) if k.tag == 'h1')
        for ui in ul_indices:
            self.assertLess(ui, h1_index)
        # And there are two list items at root.
        self.assertEqual(len(ul_indices), 2)

    def test_file_without_trailing_newline(self):
        text = '# Only\n- last'  # no trailing newline
        root, _, _ = self._build(text)
        kids = root._children
        self.assertEqual(len(kids), 1)
        h1 = kids[0]
        # ``# Only`` spans through to len(file_text).
        self.assertEqual(h1.byte_offset + h1.byte_size, len(text))
        # The list item's section also runs to EOF.
        li = h1._children[0]
        self.assertEqual(li.byte_offset + li.byte_size, len(text))


class TestNodeAtLine(unittest.TestCase):
    """``_node_at_line`` — line → deepest containing Item lookup."""

    # Same fixture as ``TestBuildNodes`` so we can predict offsets.
    FIXTURE = (
        '# H1\n'        # line 0
        '## H2a\n'      # line 1
        '- a\n'         # line 2
        '  - a1\n'      # line 3
        '- b\n'         # line 4
        '## H2b\n'      # line 5
        '1. one\n'      # line 6
        '# H1b\n'       # line 7
        '- top\n'       # line 8
    )

    def setUp(self):
        # Fresh module per test — ``_BY_LINE`` / ``_LINES_SORTED``
        # are module-level state populated by ``main()``; we have
        # to mirror that wiring here.
        self.r = _load_recipe()
        self.path = '/tmp/lookup.md'
        line_starts = self.r._line_starts(self.FIXTURE)
        events = self.r._parse(self.FIXTURE)
        self.root, by_id = self.r._build_nodes(
            events, self.FIXTURE, line_starts, self.path,
        )
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = self.r._build_line_index(by_id)

    def test_exact_match_on_heading_line(self):
        # Line 0 is the ``# H1`` heading.
        node = self.r._node_at_line(0)
        self.assertIsNotNone(node)
        self.assertEqual(node.title, '# H1')

    def test_exact_match_on_list_item_line(self):
        # Line 2 is the ``- a`` list item.
        node = self.r._node_at_line(2)
        self.assertEqual(node.title, '- a')
        # Line 3 is its nested ``- a1`` child.
        node = self.r._node_at_line(3)
        self.assertEqual(node.title, '- a1')

    def test_inexact_falls_back_to_previous_node(self):
        # If we had blank/paragraph lines in this fixture the lookup
        # would land on the most recent parsed node. The fixture is
        # dense (every line is a node), so swap to a fixture with a
        # gap to exercise this.
        text = (
            '# H1\n'        # line 0
            'paragraph\n'   # line 1 — no node
            'more text\n'   # line 2 — no node
            '## H2\n'       # line 3
        )
        line_starts = self.r._line_starts(text)
        events = self.r._parse(text)
        _root, by_id = self.r._build_nodes(events, text, line_starts, '/x.md')
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))
        # Lines 1 and 2 sit between ``# H1`` (line 0) and ``## H2``
        # (line 3); they should fall back to ``# H1``.
        self.assertEqual(self.r._node_at_line(1).title, '# H1')
        self.assertEqual(self.r._node_at_line(2).title, '# H1')

    def test_line_before_any_node_returns_none(self):
        # Build a fixture with a leading preamble so line 0 has no
        # parsed node.
        text = (
            'preamble line\n'   # line 0 — no node
            '# H1\n'            # line 1
        )
        line_starts = self.r._line_starts(text)
        events = self.r._parse(text)
        _root, by_id = self.r._build_nodes(events, text, line_starts, '/y.md')
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))
        self.assertIsNone(self.r._node_at_line(0))

    def test_exact_match_on_last_node(self):
        # Line 8 is the last node (``- top`` under ``# H1b``).
        node = self.r._node_at_line(8)
        self.assertEqual(node.title, '- top')

    def test_line_past_last_node_returns_last_containing(self):
        # Past EOF — should fall back to the last node (``- top``
        # subsumes any imaginary later lines under it).
        node = self.r._node_at_line(99999)
        self.assertEqual(node.title, '- top')

    def test_empty_file_returns_none(self):
        # No parsed nodes → every lookup is ``None``.
        line_starts = self.r._line_starts('')
        events = self.r._parse('')
        _root, by_id = self.r._build_nodes(events, '', line_starts, '/e.md')
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))
        self.assertIsNone(self.r._node_at_line(0))
        self.assertIsNone(self.r._node_at_line(42))


class TestDisplayTitle(unittest.TestCase):
    """``_display_title`` — strip ``#`` markers + whitespace for matching."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _heading(self, title):
        """Build a minimal stand-in Item with the right shape."""
        it = self.r.Item(title=title)
        it.kind = 'heading'
        return it

    def test_h1(self):
        self.assertEqual(self.r._display_title(self._heading('# Foo')), 'Foo')

    def test_h2(self):
        self.assertEqual(
            self.r._display_title(self._heading('## Foo Bar')), 'Foo Bar')

    def test_h6(self):
        self.assertEqual(
            self.r._display_title(self._heading('###### Deep heading')),
            'Deep heading',
        )

    def test_inline_markers_preserved(self):
        # ``**bold**`` markers are NOT stripped — only leading ``#``s
        # plus surrounding whitespace.
        self.assertEqual(
            self.r._display_title(self._heading('## **bold**')), '**bold**')

    def test_no_hash_prefix_falls_back_to_strip(self):
        # Defensive: a malformed title with no ``#`` prefix still
        # returns the title with surrounding whitespace stripped.
        self.assertEqual(
            self.r._display_title(self._heading('  no markers  ')),
            'no markers',
        )

    def test_trailing_hashes_preserved(self):
        # md2ansi's ``_MD_H*`` patterns don't special-case trailing
        # ``#``s, so the raw source line keeps them; ``_display_title``
        # only touches leading ``#``s + surrounding whitespace.
        self.assertEqual(
            self.r._display_title(self._heading('## Foo ##')), 'Foo ##')


class TestResolveAnchor(unittest.TestCase):
    """``_resolve_anchor`` — anchor string → ``initial_scope`` id."""

    FIXTURE = (
        '# Intro\n'             # line 0
        '## Overview\n'         # line 1
        '- bullet\n'            # line 2
        '## Details\n'          # line 3
        '# Conclusion\n'        # line 4
    )

    def setUp(self):
        self.r = _load_recipe()
        self.path = '/tmp/anchor.md'
        line_starts = self.r._line_starts(self.FIXTURE)
        events = self.r._parse(self.FIXTURE)
        self.root, by_id = self.r._build_nodes(
            events, self.FIXTURE, line_starts, self.path,
        )
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))

    def _resolve(self, anchor):
        return self.r._resolve_anchor(anchor, self.root.id)

    def test_empty_anchor_returns_root(self):
        self.assertEqual(self._resolve(''), self.root.id)

    def test_digit_anchor_exact_line(self):
        # ``#0`` resolves to ``# Intro`` (line 0).
        self.assertEqual(self._resolve('0'), f'{self.path}#0')
        # ``#3`` resolves to ``## Details`` (line 3).
        self.assertEqual(self._resolve('3'), f'{self.path}#3')

    def test_digit_anchor_inexact_falls_back_to_previous(self):
        # No node sits exactly on line 999999 → ``_node_at_line``
        # returns the last node; ``_resolve_anchor`` returns that id.
        self.assertEqual(self._resolve('999999'), f'{self.path}#4')

    def test_digit_anchor_past_eof_does_not_warn(self):
        # All-digit anchors fall through silently (the spec says no
        # warning in this path).
        from io import StringIO
        from contextlib import redirect_stderr
        buf = StringIO()
        with redirect_stderr(buf):
            self._resolve('999999')
        self.assertEqual(buf.getvalue(), '')

    def test_digit_anchor_before_any_node(self):
        # Build a fixture where line 0 has no node; the digit anchor
        # ``0`` lands in preamble territory and falls back to root.
        text = 'preamble line\n# After\n'
        line_starts = self.r._line_starts(text)
        events = self.r._parse(text)
        root, by_id = self.r._build_nodes(events, text, line_starts, '/p.md')
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))
        # ``_node_at_line(0)`` is None → fall back to root.
        self.assertEqual(self.r._resolve_anchor('0', root.id), root.id)

    def test_exact_match_heading(self):
        # ``Overview`` matches ``## Overview`` exactly (display_title).
        self.assertEqual(self._resolve('Overview'), f'{self.path}#1')

    def test_prefix_match_heading(self):
        # ``Det`` matches ``## Details`` as a prefix; no exact match.
        self.assertEqual(self._resolve('Det'), f'{self.path}#3')

    def test_substring_match_heading(self):
        # ``clus`` matches ``# Conclusion`` as a substring; no exact /
        # prefix match in the heading set.
        self.assertEqual(self._resolve('clus'), f'{self.path}#4')

    def test_no_match_warns_and_returns_root(self):
        from io import StringIO
        from contextlib import redirect_stderr
        buf = StringIO()
        with redirect_stderr(buf):
            result = self._resolve('xyzzy-nonexistent')
        self.assertEqual(result, self.root.id)
        # Warning mentions the anchor (the exact wording is recipe-
        # owned; we assert the substring contract).
        self.assertIn('xyzzy-nonexistent', buf.getvalue())

    def test_tier_precedence_exact_beats_prefix(self):
        # ``Foo`` exact-matches one heading AND is a prefix of another.
        # Tier 1 (exact) must win even though the prefix-only heading
        # comes earlier in source order — the tiers are scanned in
        # full before falling through to the next tier.
        text = (
            '# Foobar\n'    # line 0 — prefix-only match
            '# Foo\n'       # line 1 — exact match
        )
        line_starts = self.r._line_starts(text)
        events = self.r._parse(text)
        root, by_id = self.r._build_nodes(events, text, line_starts, '/t.md')
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))
        # Exact match on line 1 wins over prefix match on line 0.
        self.assertEqual(self.r._resolve_anchor('Foo', root.id), '/t.md#1')

    def test_source_order_tie_first_wins(self):
        # Two headings with the same exact display_title → first in
        # source order wins.
        text = (
            '# Dup\n'   # line 0
            '# Dup\n'   # line 1
        )
        line_starts = self.r._line_starts(text)
        events = self.r._parse(text)
        root, by_id = self.r._build_nodes(events, text, line_starts, '/d.md')
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))
        self.assertEqual(self.r._resolve_anchor('Dup', root.id), '/d.md#0')

    def test_anchor_skips_list_items(self):
        # ``bullet`` matches the list-item ``- bullet`` text but
        # ``_resolve_anchor`` only scans headings; no match → warning
        # + root fall-through.
        from io import StringIO
        from contextlib import redirect_stderr
        buf = StringIO()
        with redirect_stderr(buf):
            result = self._resolve('bullet')
        self.assertEqual(result, self.root.id)
        self.assertIn('bullet', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
