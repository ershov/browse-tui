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
* ``get_preview``                     (TestGetPreview)
* ``_action_toggle_md``               (TestToggleMd)
* ``_resolve_md_pager``               (TestResolveMdPager)

These are the work of tickets #519 (parser), #520 (list walker +
tree linking + Item construction), #521 (line lookup + anchor
resolution), and #522 (preview pipeline + ``m`` / ``M`` toggles).
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


def _load_recipe(include_lists=True):
    """Load (or reload) the browse-md recipe; returns the module.

    ``recipes/browse-md`` has no ``.py`` extension; importlib's
    default loader-from-extension lookup returns None, so we use the
    source loader explicitly. A fresh module instance is created on
    every call so tests that mutate module-level state (notably
    ``_BY_ID`` for ``get_children``) don't bleed into each other.

    The recipe defaults ``_INCLUDE_LISTS`` to ``False`` (production
    behaviour: headings only unless ``-l`` is passed). The majority of
    parser-level tests assume list items show up as tree rows, so
    this loader flips the flag back to ``True`` after module
    execution. Tests that need to exercise the lists-off path pass
    ``include_lists=False``.
    """
    _stub_browse_tui()
    name = '_browse_md_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod._INCLUDE_LISTS = include_lists
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
        # Titles are pre-stripped of the leading ``#``s and whitespace;
        # the kind is already conveyed by the ``[h1]`` tag.
        self.assertEqual(kids[0].title, 'H1')
        self.assertEqual(kids[1].title, 'H1b')

    def test_first_h1_has_two_h2_children(self):
        h1 = self.root._children[0]
        kids = h1._children
        self.assertEqual(len(kids), 2)
        self.assertEqual([k.tag for k in kids], ['h2', 'h2'])
        self.assertEqual(kids[0].title, 'H2a')
        self.assertEqual(kids[1].title, 'H2b')

    def test_h2a_has_two_list_items(self):
        h2a = self.root._children[0]._children[0]
        kids = h2a._children
        self.assertEqual(len(kids), 2)
        self.assertEqual([k.tag for k in kids], ['ul', 'ul'])
        # List-item titles are pre-stripped of the marker + whitespace.
        self.assertEqual(kids[0].title, 'a')
        self.assertEqual(kids[1].title, 'b')

    def test_a_has_one_nested_child(self):
        h2a = self.root._children[0]._children[0]
        a = h2a._children[0]
        self.assertEqual(len(a._children), 1)
        self.assertEqual(a._children[0].title, 'a1')
        self.assertTrue(a.has_children)

    def test_h2b_has_one_ol_child(self):
        h2b = self.root._children[0]._children[1]
        self.assertEqual(len(h2b._children), 1)
        self.assertEqual(h2b._children[0].tag, 'ol')
        # ``1. one`` → ``one`` after marker stripping.
        self.assertEqual(h2b._children[0].title, 'one')

    def test_second_h1_has_one_list_child(self):
        h1b = self.root._children[1]
        self.assertEqual(len(h1b._children), 1)
        self.assertEqual(h1b._children[0].title, 'top')

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

    def test_title_strips_marker_prefix(self):
        # Headings drop leading ``#``s + whitespace; the source line
        # ``## H2a`` becomes the title ``H2a``. The kind is already
        # conveyed by the ``[h2]`` tag, so storing the sigil again
        # would be redundant.
        h2a = self.root._children[0]._children[0]
        self.assertEqual(h2a.title, 'H2a')

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


class TestTitleStripping(unittest.TestCase):
    """Title construction in ``_build_nodes`` — marker prefix removal.

    The kind/tag of a tree row is already conveyed by the ``[h1]`` /
    ``[ul]`` / ... tag (with colour), so the title text drops the
    redundant ``#`` sigil or list marker. Only the leading prefix +
    its immediate whitespace is stripped — inline formatting and any
    trailing decoration stay intact.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _build(self, text, path='/tmp/strip.md'):
        ls = self.r._line_starts(text)
        events = self.r._parse(text)
        return self.r._build_nodes(events, text, ls, path)

    def _titles_in_source_order(self, text):
        # Walk the tree depth-first in source order so we get every
        # parsed node's title regardless of nesting.
        root, _ = self._build(text)
        out = []

        def walk(node):
            for kid in node._children:
                out.append(kid.title)
                walk(kid)

        walk(root)
        return out

    def test_heading_inline_formatting_preserved(self):
        # ``## My **bold** heading`` → ``My **bold** heading``. The
        # ``**`` markers are part of the title text, not the sigil
        # prefix, so they survive.
        titles = self._titles_in_source_order('## My **bold** heading\n')
        self.assertEqual(titles, ['My **bold** heading'])

    def test_heading_trailing_hash_preserved(self):
        # md2ansi's heading patterns don't special-case a trailing
        # ``#``, so it stays in the source line — and therefore in
        # the stored title — after we strip only the *leading*
        # ``#``s + whitespace.
        titles = self._titles_in_source_order(
            '### Heading with trailing #\n')
        self.assertEqual(titles, ['Heading with trailing #'])

    def test_list_item_dash_stripped(self):
        # ``- foo bar`` → ``foo bar``.
        titles = self._titles_in_source_order('- foo bar\n')
        self.assertEqual(titles, ['foo bar'])

    def test_list_item_asterisk_stripped(self):
        # ``* item`` → ``item``.
        titles = self._titles_in_source_order('* item\n')
        self.assertEqual(titles, ['item'])

    def test_list_item_plus_stripped(self):
        # ``+ item`` → ``item``.
        titles = self._titles_in_source_order('+ item\n')
        self.assertEqual(titles, ['item'])

    def test_list_item_ordered_single_digit(self):
        # ``1. item one`` → ``item one``.
        titles = self._titles_in_source_order('1. item one\n')
        self.assertEqual(titles, ['item one'])

    def test_list_item_ordered_multi_digit(self):
        # ``42. wat`` → ``wat``.
        titles = self._titles_in_source_order('42. wat\n')
        self.assertEqual(titles, ['wat'])

    def test_heading_with_bold_italic_leading_asterisks(self):
        # ``## *** bold-italic ***`` — the leading ``##`` matches the
        # heading rule before the list rule gets a chance, so the
        # title still gets the heading-prefix treatment and the
        # ``***`` markers (which are inline formatting, not a list
        # marker) survive. This is a parser-precedence sanity check.
        titles = self._titles_in_source_order('## *** bold-italic ***\n')
        self.assertEqual(titles, ['*** bold-italic ***'])

    def test_heading_extra_internal_whitespace_preserved(self):
        # Only the *immediate* whitespace after the ``#`` run is
        # consumed by the strip — internal double-spaces survive.
        titles = self._titles_in_source_order('## Foo  bar\n')
        self.assertEqual(titles, ['Foo  bar'])


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
        # Line 0 is the ``# H1`` heading; the stored title drops the
        # leading ``#`` + whitespace.
        node = self.r._node_at_line(0)
        self.assertIsNotNone(node)
        self.assertEqual(node.title, 'H1')

    def test_exact_match_on_list_item_line(self):
        # Line 2 is the ``- a`` list item; the stored title drops the
        # leading marker + whitespace.
        node = self.r._node_at_line(2)
        self.assertEqual(node.title, 'a')
        # Line 3 is its nested ``- a1`` child.
        node = self.r._node_at_line(3)
        self.assertEqual(node.title, 'a1')

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
        # (line 3); they should fall back to ``# H1`` (stored as
        # ``H1`` after marker stripping).
        self.assertEqual(self.r._node_at_line(1).title, 'H1')
        self.assertEqual(self.r._node_at_line(2).title, 'H1')

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
        # Line 8 is the last node (``- top`` under ``# H1b``); stored
        # title drops the marker.
        node = self.r._node_at_line(8)
        self.assertEqual(node.title, 'top')

    def test_line_past_last_node_returns_last_containing(self):
        # Past EOF — should fall back to the last node (``- top``
        # subsumes any imaginary later lines under it).
        node = self.r._node_at_line(99999)
        self.assertEqual(node.title, 'top')

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
    """``_display_title`` — strip surrounding whitespace for matching.

    Stored titles are pre-stripped of ``#`` / list markers at
    ``_build_nodes`` time, so this helper has collapsed to a thin
    ``title.strip()`` wrapper. We keep a couple of sanity checks
    here; the substantive marker-stripping coverage lives in
    ``TestBuildNodes`` and ``TestTitleStripping``.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _heading(self, title):
        """Build a minimal stand-in Item with the right shape."""
        it = self.r.Item(title=title)
        it.kind = 'heading'
        return it

    def test_returns_title_unchanged_when_clean(self):
        # The post-#542 contract: stored titles are already free of
        # ``#`` sigils, so a clean ``Foo`` round-trips verbatim.
        self.assertEqual(self.r._display_title(self._heading('Foo')), 'Foo')

    def test_strips_surrounding_whitespace(self):
        # Defensive: any stray padding is trimmed so anchor matching
        # uses a stable key.
        self.assertEqual(
            self.r._display_title(self._heading('  Foo Bar  ')),
            'Foo Bar',
        )

    def test_inline_markers_preserved(self):
        # ``**bold**`` markers are NOT stripped — they're part of the
        # title text. Only surrounding whitespace is trimmed.
        self.assertEqual(
            self.r._display_title(self._heading('**bold**')), '**bold**')


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


class TestGetPreview(unittest.TestCase):
    """``get_preview`` — slice ``_FILE_TEXT`` per node, optionally md2ansi-render."""

    # Fixture chosen so headings, list items, and root all resolve to
    # different byte windows. Trailing newline so byte_size on the
    # final node ends cleanly at len(text).
    FIXTURE = (
        '# H1\n'        # line 0, bytes 0..5
        'preamble\n'    # line 1, bytes 5..14
        '## H2\n'       # line 2, bytes 14..20
        '- a\n'         # line 3, bytes 20..24
        '- b\n'         # line 4, bytes 24..28
        '# H1b\n'       # line 5, bytes 28..34
    )

    def setUp(self):
        # Fresh module per test — globals (``_FILE_TEXT``, ``_BY_ID``,
        # ``_BY_LINE``, ``_LINES_SORTED``, ``_MD_COLOR``,
        # ``_md2ansi_fn``, ``_BROWSER``) mustn't bleed across tests.
        self.r = _load_recipe()
        self.path = '/tmp/preview.md'
        line_starts = self.r._line_starts(self.FIXTURE)
        events = self.r._parse(self.FIXTURE)
        self.root, by_id = self.r._build_nodes(
            events, self.FIXTURE, line_starts, self.path,
        )
        # Wire the module-level state ``get_preview`` reads from. This
        # mirrors what ``main()`` does at startup; we skip the actual
        # Browser construction.
        self.r._FILE_TEXT = self.FIXTURE
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))
        # Default the colored-render gate off so the raw-slice tests
        # don't accidentally exercise the md2ansi path. Each rendering
        # test re-enables it explicitly.
        self.r._MD_COLOR = False
        self.r._BROWSER = None

    def test_root_id_returns_whole_file(self):
        # Root id has no ``#`` — full file body comes back.
        self.assertEqual(self.r.get_preview(self.root.id), self.FIXTURE)

    def test_heading_returns_section_slice(self):
        # ``# H1`` at line 0 owns everything up to ``# H1b`` at line 5.
        h1 = self.root._children[0]
        self.assertEqual(h1.tag, 'h1')
        out = self.r.get_preview(h1.id)
        # Slice = [byte_offset, byte_offset + byte_size).
        self.assertEqual(out,
                         self.FIXTURE[h1.byte_offset:
                                      h1.byte_offset + h1.byte_size])
        # Sanity: starts with ``# H1\n`` and stops before ``# H1b``.
        self.assertTrue(out.startswith('# H1\n'))
        self.assertNotIn('# H1b', out)

    def test_nested_heading_slice(self):
        # ``## H2`` is a child of ``# H1``; its section runs from its
        # own offset to the next sibling-or-shallower boundary (here
        # the next ``# H1b`` since no other ``## H*`` follows).
        h1 = self.root._children[0]
        h2 = h1._children[0]
        self.assertEqual(h2.tag, 'h2')
        out = self.r.get_preview(h2.id)
        self.assertEqual(out,
                         self.FIXTURE[h2.byte_offset:
                                      h2.byte_offset + h2.byte_size])
        self.assertTrue(out.startswith('## H2\n'))

    def test_list_item_returns_leaf_slice(self):
        # ``- a`` at line 3 — leaf list item; its byte_size spans only
        # its own line (next sibling is ``- b`` at line 4).
        h1 = self.root._children[0]
        h2 = h1._children[0]
        li_a = h2._children[0]
        self.assertIn(li_a.tag, ('ul', 'ol'))
        out = self.r.get_preview(li_a.id)
        self.assertEqual(out, '- a\n')

    def test_unknown_id_returns_empty(self):
        # ``/path#999`` resolves to no node; ``_node_at_line`` will
        # return the last node ≤ 999 (which is fine — but a
        # non-integer suffix has nothing to fall back to).
        self.assertEqual(self.r.get_preview('/some/path#notanumber'), '')

    def test_line_with_no_node_below_returns_empty(self):
        # Line ``-1`` is before every parsed node — _BY_LINE miss,
        # _node_at_line returns None → empty.
        out = self.r.get_preview(f'{self.path}#-1')
        self.assertEqual(out, '')

    def test_md_color_off_returns_raw(self):
        # Already the default, but assert explicitly — colored mode
        # off means the raw slice flows through untouched.
        self.r._MD_COLOR = False
        h1 = self.root._children[0]
        out = self.r.get_preview(h1.id)
        self.assertNotIn('RENDERED', out)

    def test_md_color_on_runs_md2ansi(self):
        # Install a stub renderer and flip the gate. ``get_preview``
        # should hand the slice to the stub and return its output.
        calls = []
        def stub(text, line_width):
            calls.append((text, line_width))
            return 'RENDERED'
        self.r._md2ansi_fn = stub
        self.r._MD_COLOR = True
        self.r._BROWSER = None  # exercises the ``or 80`` width fallback
        h1 = self.root._children[0]
        out = self.r.get_preview(h1.id)
        self.assertEqual(out, 'RENDERED')
        self.assertEqual(len(calls), 1)
        # The stub received the raw heading slice + a 80-col default.
        self.assertTrue(calls[0][0].startswith('# H1\n'))
        self.assertEqual(calls[0][1], 80)

    def test_md_color_uses_browser_preview_width(self):
        # When ``_BROWSER`` is set, ``get_preview`` reads its
        # ``preview_width`` for the line_width arg.
        class _FakeBrowser:
            preview_width = 42
        widths = []
        def stub(text, line_width):
            widths.append(line_width)
            return text
        self.r._md2ansi_fn = stub
        self.r._MD_COLOR = True
        self.r._BROWSER = _FakeBrowser()
        self.r.get_preview(self.root.id)
        self.assertEqual(widths, [42])

    def test_md_color_zero_width_falls_back_to_80(self):
        # ``preview_width`` of 0 is the framework's "not yet sized"
        # sentinel — our ``or 80`` guard kicks in.
        class _FakeBrowser:
            preview_width = 0
        widths = []
        def stub(text, line_width):
            widths.append(line_width)
            return text
        self.r._md2ansi_fn = stub
        self.r._MD_COLOR = True
        self.r._BROWSER = _FakeBrowser()
        self.r.get_preview(self.root.id)
        self.assertEqual(widths, [80])

    def test_md_color_renderer_raises_falls_back_to_raw(self):
        # md2ansi blow-ups must not propagate. We expect the raw slice.
        def boom(text, line_width):
            raise RuntimeError('bad markdown')
        self.r._md2ansi_fn = boom
        self.r._MD_COLOR = True
        h1 = self.root._children[0]
        out = self.r.get_preview(h1.id)
        # Raw slice, untouched.
        self.assertEqual(out,
                         self.FIXTURE[h1.byte_offset:
                                      h1.byte_offset + h1.byte_size])

    def test_md_color_on_but_fn_none_returns_raw(self):
        # Defensive: if a test leaves ``_MD_COLOR`` on but the import
        # never resolved, the gate inside ``get_preview`` skips the
        # render path rather than crashing on ``None(...)``.
        self.r._md2ansi_fn = None
        self.r._MD_COLOR = True
        h1 = self.root._children[0]
        out = self.r.get_preview(h1.id)
        self.assertEqual(out,
                         self.FIXTURE[h1.byte_offset:
                                      h1.byte_offset + h1.byte_size])


class _FakeCtx:
    """Recorder for ``ctx`` interactions used by action handlers."""

    def __init__(self):
        self.dropped = 0
        self.messages = []
        self.errors = []

    def drop_preview_cache(self, id_=None):
        self.dropped += 1

    def message(self, text):
        self.messages.append(text)

    def error(self, text):
        self.errors.append(text)


class TestToggleMd(unittest.TestCase):
    """``_action_toggle_md`` flips ``_MD_COLOR`` and notifies ctx."""

    def setUp(self):
        self.r = _load_recipe()
        # Pretend md2ansi was importable so the toggle is meaningful;
        # the action itself only depends on ``_MD_COLOR``, not on the
        # function being callable, so an identity stub is fine.
        self.r._md2ansi_fn = lambda text, line_width: text
        self.r._MD_COLOR = True
        self.ctx = _FakeCtx()

    def test_flip_true_to_false(self):
        self.r._action_toggle_md(self.ctx)
        self.assertFalse(self.r._MD_COLOR)
        self.assertEqual(self.ctx.dropped, 1)
        self.assertEqual(self.ctx.messages, ['md preview: raw'])

    def test_flip_back_round_trip(self):
        self.r._action_toggle_md(self.ctx)  # True -> False
        self.r._action_toggle_md(self.ctx)  # False -> True
        self.assertTrue(self.r._MD_COLOR)
        self.assertEqual(self.ctx.dropped, 2)
        self.assertEqual(self.ctx.messages,
                         ['md preview: raw', 'md preview: colored'])

    def test_flip_from_false(self):
        # Starting from False — message reports the new state ("colored").
        self.r._MD_COLOR = False
        self.r._action_toggle_md(self.ctx)
        self.assertTrue(self.r._MD_COLOR)
        self.assertEqual(self.ctx.messages, ['md preview: colored'])


class TestResolveMdPager(unittest.TestCase):
    """``_resolve_md_pager`` walks ``$MD2ANSI`` / ``md2ansi+less`` in order."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _with_env(self, **kw):
        """Snapshot env, override per kw, return a restore-fn."""
        import os
        saved = {k: os.environ.get(k) for k in kw}
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return restore

    def _scratch_bin(self, tmp, names):
        """Create executable stubs in ``tmp`` for each name in ``names``."""
        import os
        import stat
        for name in names:
            path = os.path.join(tmp, name)
            with open(path, 'w') as f:
                f.write('#!/bin/sh\ncat "$1"\n')
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def test_env_var_wins(self):
        restore = self._with_env(MD2ANSI='md2ansi')
        try:
            self.assertEqual(self.r._resolve_md_pager(), ['md2ansi'])
        finally:
            restore()

    def test_env_var_shlex_splits(self):
        # ``shlex.split`` keeps the flag separate from the binary name.
        restore = self._with_env(MD2ANSI='my-md-cmd --flag')
        try:
            self.assertEqual(self.r._resolve_md_pager(),
                             ['my-md-cmd', '--flag'])
        finally:
            restore()

    def test_env_pipeline_uses_bash_dash_c(self):
        # ``|`` in $MD2ANSI → bash wrapper so the pipe runs.
        restore = self._with_env(MD2ANSI='md2ansi | less -R')
        try:
            cmd = self.r._resolve_md_pager()
            self.assertEqual(cmd[0], 'bash')
            self.assertEqual(cmd[1], '-c')
            self.assertIn('md2ansi | less -R', cmd[2])
        finally:
            restore()

    def test_md2ansi_plus_less_pipes_to_less_rs(self):
        # Default fallback when both md2ansi and less exist: pipe
        # md2ansi output through ``less -RS`` via bash.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._scratch_bin(tmp, ['md2ansi', 'less'])
            saved_path = os.environ['PATH']
            restore = self._with_env(MD2ANSI=None)
            try:
                os.environ['PATH'] = tmp
                cmd = self.r._resolve_md_pager()
                self.assertEqual(cmd[0], 'bash')
                self.assertEqual(cmd[1], '-c')
                self.assertIn('md2ansi', cmd[2])
                self.assertIn('less -RS', cmd[2])
            finally:
                os.environ['PATH'] = saved_path
                restore()

    def test_md2ansi_alone_no_pipe(self):
        # Without ``less`` on PATH, fall back to bare ``md2ansi``.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._scratch_bin(tmp, ['md2ansi'])
            saved_path = os.environ['PATH']
            restore = self._with_env(MD2ANSI=None)
            try:
                os.environ['PATH'] = tmp
                self.assertEqual(self.r._resolve_md_pager(), ['md2ansi'])
            finally:
                os.environ['PATH'] = saved_path
                restore()

    def test_none_when_nothing_resolves(self):
        # No env var, no binaries on PATH → ``None``.
        import os
        restore = self._with_env(MD2ANSI=None)
        saved_path = os.environ.get('PATH', '')
        try:
            os.environ['PATH'] = '/nonexistent-' + str(os.getpid())
            self.assertIsNone(self.r._resolve_md_pager())
        finally:
            os.environ['PATH'] = saved_path
            restore()


class TestMergeRanges(unittest.TestCase):
    """``_merge_ranges`` — sort + dedupe + adjacency-merge ``(bo, bs)``."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_empty_list(self):
        self.assertEqual(self.r._merge_ranges([]), [])

    def test_single_range(self):
        self.assertEqual(self.r._merge_ranges([(10, 5)]), [(10, 5)])

    def test_two_disjoint_in_order(self):
        # Disjoint and already sorted — passthrough.
        self.assertEqual(
            self.r._merge_ranges([(0, 5), (10, 3)]),
            [(0, 5), (10, 3)],
        )

    def test_two_disjoint_out_of_order(self):
        # Same disjoint ranges but reversed input — sorted in output.
        self.assertEqual(
            self.r._merge_ranges([(10, 3), (0, 5)]),
            [(0, 5), (10, 3)],
        )

    def test_two_overlapping(self):
        # (0..5) overlaps (3..13) → (0..13).
        self.assertEqual(
            self.r._merge_ranges([(0, 5), (3, 10)]),
            [(0, 13)],
        )

    def test_two_adjacent(self):
        # (0..5) followed by (5..3) — adjacent, no gap. Merge to (0..8).
        self.assertEqual(
            self.r._merge_ranges([(0, 5), (5, 3)]),
            [(0, 8)],
        )

    def test_range_fully_contained(self):
        # (0..20) contains (5..3) — result is just the outer.
        self.assertEqual(
            self.r._merge_ranges([(0, 20), (5, 3)]),
            [(0, 20)],
        )

    def test_three_two_merge_one_disjoint(self):
        # (0..5)+(4..3) merge into (0..7); (20..5) stays separate.
        self.assertEqual(
            self.r._merge_ranges([(0, 5), (4, 3), (20, 5)]),
            [(0, 7), (20, 5)],
        )

    def test_identical_ranges_deduped(self):
        self.assertEqual(
            self.r._merge_ranges([(10, 5), (10, 5)]),
            [(10, 5)],
        )


class TestWriteRangeExcerpts(unittest.TestCase):
    """``_write_range_excerpts`` — concatenate slices into a temp .md file."""

    def setUp(self):
        self.r = _load_recipe()
        # The recipe slices ``_FILE_TEXT``; install our own buffer.
        self.r._FILE_TEXT = (
            'AAAA\n'      # bytes  0..4  (line ends in \n)
            'BBBB\n'      # bytes  5..9
            'CCCC\n'      # bytes 10..14
            'DDDD'        # bytes 15..18 (no trailing newline)
        )
        self.tmp_paths = []

    def tearDown(self):
        import os
        for p in self.tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _read(self, path):
        with open(path, 'r', encoding='utf-8', errors='surrogateescape') as f:
            return f.read()

    def test_single_range(self):
        path = self.r._write_range_excerpts([(0, 5)])
        self.tmp_paths.append(path)
        self.assertEqual(self._read(path), 'AAAA\n')

    def test_two_ranges_first_ends_with_newline(self):
        # First slice ends in \n → no extra separator inserted.
        path = self.r._write_range_excerpts([(0, 5), (10, 5)])
        self.tmp_paths.append(path)
        self.assertEqual(self._read(path), 'AAAA\nCCCC\n')

    def test_two_ranges_first_lacks_newline(self):
        # First slice is 'DDDD' (no \n); separator \n inserted before next.
        path = self.r._write_range_excerpts([(15, 4), (0, 5)])
        # Note: caller is responsible for merging/sorting; this helper
        # writes whatever it's given in order. We pass ranges in the
        # given order to exercise the "needs separator" branch.
        self.tmp_paths.append(path)
        self.assertEqual(self._read(path), 'DDDD\nAAAA\n')

    def test_three_ranges_mixed_newline_endings(self):
        # First two end in \n (no separator inserted between them);
        # third slice ('DDDD') has no trailing newline → file ends raw.
        path = self.r._write_range_excerpts([(0, 5), (5, 5), (15, 4)])
        self.tmp_paths.append(path)
        self.assertEqual(self._read(path), 'AAAA\nBBBB\nDDDD')

    def test_empty_input(self):
        path = self.r._write_range_excerpts([])
        self.tmp_paths.append(path)
        self.assertEqual(self._read(path), '')

    def test_path_has_md_suffix(self):
        path = self.r._write_range_excerpts([(0, 1)])
        self.tmp_paths.append(path)
        self.assertTrue(path.endswith('.md'))


class _SrcCmdCtx:
    """Recorder for ``ctx`` in ``_run_source_command`` tests."""

    def __init__(self, targets):
        self.targets = list(targets)
        self.calls = []
        self.errors = []
        self.messages = []

    def run_external(self, cmd):
        # Snapshot the argv list. The tempfile path (if any) needs to
        # still exist for the test to read it — we rely on the test
        # inspecting the file *during* the call rather than after the
        # finally clause unlinks it.
        self.calls.append(list(cmd))
        # Read the tempfile contents synchronously so the assertion
        # can run after ``_run_source_command`` returns (which
        # unlinks).
        if len(cmd) > 0:
            import os
            last = cmd[-1]
            if os.path.exists(last):
                try:
                    with open(last, 'r', encoding='utf-8',
                              errors='surrogateescape') as f:
                        self.last_tmp_contents = f.read()
                except OSError:
                    pass

    def error(self, text):
        self.errors.append(text)

    def message(self, text):
        self.messages.append(text)


class _SrcItem:
    """Bare Item stand-in for ``_run_source_command`` tests."""
    def __init__(self, *, id, kind, byte_offset=0, byte_size=0):
        self.id = id
        self.kind = kind
        self.byte_offset = byte_offset
        self.byte_size = byte_size


class TestRunSourceCommand(unittest.TestCase):
    """``_run_source_command`` — root vs non-root dispatch + tempfile flow."""

    def setUp(self):
        import os
        self.r = _load_recipe()
        self.r._FILE_TEXT = (
            'AAAA\n'      # bytes  0..4
            'BBBB\n'      # bytes  5..9
            'CCCC\n'      # bytes 10..14
            'DDDD\n'      # bytes 15..19
            'EEEE\n'      # bytes 20..24
        )
        self.path = '/tmp/src.md'
        # Snapshot env so per-test PAGER/EDITOR overrides don't leak.
        self._env_saved = {k: os.environ.get(k) for k in ('PAGER', 'EDITOR')}
        # Force defaults so we don't pick up host PAGER/EDITOR.
        os.environ.pop('PAGER', None)
        os.environ.pop('EDITOR', None)

    def tearDown(self):
        import os
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_empty_targets_noop(self):
        ctx = _SrcCmdCtx(targets=[])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [])

    def test_root_only_opens_original_path(self):
        # ``root.id`` is the absolute path; the command should be the
        # default split + that path. No tempfile.
        root = _SrcItem(id=self.path, kind='root')
        ctx = _SrcCmdCtx(targets=[root])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [['less', '-R', self.path]])

    def test_root_mixed_with_non_root_root_wins(self):
        # Spec: "If any target is root → behave as if only root was
        # selected". Mixed input still goes through the root branch.
        root = _SrcItem(id=self.path, kind='root')
        leaf = _SrcItem(id=self.path + '#3', kind='heading',
                        byte_offset=0, byte_size=5)
        ctx = _SrcCmdCtx(targets=[leaf, root])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [['less', '-R', self.path]])

    def test_single_non_root_writes_temp_and_runs(self):
        leaf = _SrcItem(id=self.path + '#2', kind='heading',
                        byte_offset=10, byte_size=5)
        ctx = _SrcCmdCtx(targets=[leaf])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        # Exactly one call; last argv is the tempfile path with .md.
        self.assertEqual(len(ctx.calls), 1)
        argv = ctx.calls[0]
        self.assertEqual(argv[:2], ['less', '-R'])
        self.assertTrue(argv[2].endswith('.md'))
        # Tempfile contents = the leaf's byte slice.
        self.assertEqual(ctx.last_tmp_contents, 'CCCC\n')

    def test_three_non_root_out_of_order_merged_file_order(self):
        # Three non-root targets handed in out of file order; the
        # produced temp file should contain merged ranges in file
        # order with no duplication. We use disjoint ranges so the
        # output is just concatenation (no slice loss).
        a = _SrcItem(id=self.path + '#0', kind='heading',
                     byte_offset=0, byte_size=5)   # 'AAAA\n'
        b = _SrcItem(id=self.path + '#2', kind='heading',
                     byte_offset=10, byte_size=5)  # 'CCCC\n'
        c = _SrcItem(id=self.path + '#4', kind='heading',
                     byte_offset=20, byte_size=5)  # 'EEEE\n'
        # Out of file order.
        ctx = _SrcCmdCtx(targets=[c, a, b])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(len(ctx.calls), 1)
        # Expected: file order, no duplication.
        self.assertEqual(ctx.last_tmp_contents, 'AAAA\nCCCC\nEEEE\n')

    def test_overlapping_ranges_deduped(self):
        # Two overlapping non-root targets → merged into one range,
        # no slice duplication in the temp file.
        a = _SrcItem(id=self.path + '#0', kind='heading',
                     byte_offset=0, byte_size=10)   # 'AAAA\nBBBB\n'
        b = _SrcItem(id=self.path + '#1', kind='heading',
                     byte_offset=5, byte_size=10)   # 'BBBB\nCCCC\n'
        ctx = _SrcCmdCtx(targets=[a, b])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        # Merged range covers bytes 0..15 — one contiguous slice.
        self.assertEqual(ctx.last_tmp_contents, 'AAAA\nBBBB\nCCCC\n')

    def test_tempfile_is_unlinked_after_run(self):
        # The tempfile path captured by the ctx call should not exist
        # on disk after _run_source_command returns (unlinked in
        # ``finally``).
        import os
        leaf = _SrcItem(id=self.path + '#0', kind='heading',
                        byte_offset=0, byte_size=5)
        ctx = _SrcCmdCtx(targets=[leaf])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        argv = ctx.calls[0]
        self.assertFalse(os.path.exists(argv[2]))

    def test_env_var_override(self):
        # When the env var is set, it wins over the default. Used by
        # both ``$PAGER`` (V) and ``$EDITOR`` (E) — exercise here once.
        import os
        os.environ['PAGER'] = 'bat --paging=always'
        try:
            leaf = _SrcItem(id=self.path + '#0', kind='heading',
                            byte_offset=0, byte_size=5)
            ctx = _SrcCmdCtx(targets=[leaf])
            self.r._run_source_command(ctx, 'PAGER', 'less -R')
            argv = ctx.calls[0]
            self.assertEqual(argv[:2], ['bat', '--paging=always'])
        finally:
            os.environ.pop('PAGER', None)


class TestReload(unittest.TestCase):
    """``_reparse`` + ``get_children(..., reload=True)`` — Ctrl-R path.

    Builds an on-disk fixture, runs the recipe's parse/build pipeline
    via ``_reparse``, mutates the file, then reloads via the public
    ``get_children`` Ctrl-R contract and confirms every parser-derived
    index reflects the new content.
    """

    def setUp(self):
        import os
        import tempfile
        # Fresh module per test — ``_BY_ID`` / ``_BY_LINE`` etc are
        # module-level state that other tests scribble on.
        self.r = _load_recipe()
        self.tmp = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8',
        )
        self.tmp.close()
        self.path = self.tmp.name
        self._write(
            '# Alpha\n'
            '## Sub-Alpha\n'
            '- item\n'
        )
        # Point ``_reparse`` at the fixture and run the first parse —
        # same code path ``main()`` uses at startup.
        self.r._ROOT_PATH = self.path
        self.r._reparse()

    def tearDown(self):
        import os
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _write(self, text):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text)

    def _heading_titles(self):
        # ``_display_title`` strips the leading ``#`` so we can compare
        # plain section names. Filters to heading-kind nodes only.
        return sorted(
            self.r._display_title(it)
            for it in self.r._BY_ID.values()
            if getattr(it, 'kind', None) == 'heading'
        )

    def test_initial_parse_state(self):
        # Sanity: the setUp parse picked up both headings.
        self.assertEqual(self._heading_titles(), ['Alpha', 'Sub-Alpha'])
        self.assertEqual(self.r._FILE_TEXT.count('# Alpha'), 1)

    def test_reparse_picks_up_new_heading(self):
        # Modify the file on disk; the running tree still reflects the
        # old content until reparse runs.
        self._write(
            '# Alpha\n'
            '## Sub-Alpha\n'
            '- item\n'
            '\n'
            '# Beta\n'
            '## Sub-Beta\n'
        )
        # Before reparse: tree is still stale.
        self.assertNotIn('Beta', self._heading_titles())
        # Trigger reparse via the public Ctrl-R contract.
        self.r.get_children(None, reload=True)
        self.assertEqual(self._heading_titles(),
                         ['Alpha', 'Beta', 'Sub-Alpha', 'Sub-Beta'])

    def test_reparse_drops_removed_headings(self):
        # Shrink the file — old headings must disappear from the index.
        self._write('# Gamma only\n')
        self.r.get_children(None, reload=True)
        self.assertEqual(self._heading_titles(), ['Gamma only'])

    def test_reparse_rebuilds_line_index(self):
        # The line index is independent of ``_BY_ID``; confirm it tracks.
        old_lines = sorted(self.r._BY_LINE)
        self._write(
            '\n'   # blank line shifts every subsequent line offset.
            '# Alpha\n'
            '# Beta\n'
        )
        self.r.get_children(None, reload=True)
        new_lines = sorted(self.r._BY_LINE)
        self.assertNotEqual(old_lines, new_lines)
        # ``_LINES_SORTED`` is the bisect view — must stay in sync.
        self.assertEqual(self.r._LINES_SORTED, new_lines)

    def test_reload_reparses_when_node_id_matches_root_path(self):
        # The framework's Ctrl-R hook can pass either ``None`` or the
        # current root's id; ``_ROOT_PATH`` is the id we registered.
        self._write('# Delta\n')
        self.r.get_children(self.r._ROOT_PATH, reload=True)
        self.assertEqual(self._heading_titles(), ['Delta'])

    def test_reload_false_does_not_reparse(self):
        # Default call (no reload kw) must NOT re-read the file even if
        # it's been mutated underneath us.
        self._write('# CompletelyDifferent\n')
        self.r.get_children(None)  # default reload=False
        # Old content still in the index.
        self.assertEqual(self._heading_titles(), ['Alpha', 'Sub-Alpha'])

    def test_reload_on_non_root_id_returns_cached_without_reparse(self):
        # Non-root reload requests are short-circuited to the cached
        # branch — full reparse would be wasted on a node-id that
        # doesn't even own the file as a whole.
        h1 = next(
            it for it in self.r._BY_ID.values()
            if getattr(it, 'kind', None) == 'heading'
            and self.r._display_title(it) == 'Alpha'
        )
        self._write('# CompletelyDifferent\n')
        result = self.r.get_children(h1.id, reload=True)
        # Cached children survived (no reparse ran).
        self.assertEqual(self._heading_titles(), ['Alpha', 'Sub-Alpha'])
        # And the call still returned the heading's cached children.
        self.assertEqual([c.tag for c in result], ['h2'])


class TestHelpIntro(unittest.TestCase):
    """``_HELP_INTRO`` — recipe-level prose shown above ``--help``."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_is_non_empty_string(self):
        self.assertIsInstance(self.r._HELP_INTRO, str)
        self.assertTrue(self.r._HELP_INTRO.strip())

    def test_contains_usage_form(self):
        # The usage line documents the optional ``-l`` flag, the
        # required file, and (further down) the anchor syntax.
        self.assertIn('browse-md [-l] FILE.md', self.r._HELP_INTRO)

    def test_mentions_lists_flag(self):
        # The ``-l`` / ``--list`` / ``--lists`` flag toggles list-item
        # emission; all three aliases should be discoverable from the
        # intro alongside a brief description.
        self.assertIn('-l', self.r._HELP_INTRO)
        self.assertIn('--list', self.r._HELP_INTRO)
        self.assertIn('--lists', self.r._HELP_INTRO)

    def test_mentions_anchor(self):
        # Anchor syntax is a load-bearing feature; document it.
        self.assertIn('#anchor', self.r._HELP_INTRO)

    def test_mentions_each_custom_action(self):
        # All four custom action keys should be discoverable from the
        # intro (the keys are bound on the action rows below it, but
        # the intro gives the one-liner).
        for key in ('m', 'M', 'V', 'E'):
            with self.subTest(key=key):
                # Bound as a word in the format ``  m`` / ``  M`` etc.
                # at start of an indented line — search for "  KEY ".
                self.assertRegex(self.r._HELP_INTRO, rf'(?m)^\s+{key}\b')

    def test_mentions_reload(self):
        # Ctrl-R is the framework keybinding; document its effect.
        self.assertIn('Ctrl-R', self.r._HELP_INTRO)

    def test_compact_size(self):
        # browse-fs-style compact help — should comfortably fit under
        # the ~25-line budget noted in the ticket.
        self.assertLessEqual(self.r._HELP_INTRO.count('\n'), 25)


class TestArgvFlag(unittest.TestCase):
    """``_pop_flag`` — extract ``-l`` / ``--list`` / ``--lists`` from argv.

    The flag is parsed before the positional in ``main()`` so order
    in argv is flexible; the helper is verified directly by patching
    ``sys.argv`` on the recipe module. ``setUp`` reloads the recipe
    with lists off (the production default) so the flag's effect on
    the global is observable.
    """

    def setUp(self):
        # Fresh module per test — ``sys.argv`` is patched on the
        # recipe's own ``sys`` reference, so we restore the original
        # argv in ``tearDown`` to keep test isolation tight.
        self.r = _load_recipe(include_lists=False)
        self._saved_argv = list(self.r.sys.argv)

    def tearDown(self):
        # Restore argv on the recipe module so any subsequent test
        # that touches it via the shared interpreter ``sys`` sees the
        # original list — defence-in-depth against cross-test bleed.
        self.r.sys.argv[:] = self._saved_argv

    def _set_argv(self, argv):
        self.r.sys.argv[:] = argv

    def test_flag_absent_returns_false(self):
        self._set_argv(['browse-md', 'FILE.md'])
        self.assertFalse(self.r._pop_flag('-l', alts=('--list', '--lists')))
        # The positional survives the pop pass.
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_short_flag_before_positional(self):
        self._set_argv(['browse-md', '-l', 'FILE.md'])
        self.assertTrue(self.r._pop_flag('-l', alts=('--list', '--lists')))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_short_flag_after_positional(self):
        self._set_argv(['browse-md', 'FILE.md', '-l'])
        self.assertTrue(self.r._pop_flag('-l', alts=('--list', '--lists')))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_long_list_alias(self):
        self._set_argv(['browse-md', '--list', 'FILE.md'])
        self.assertTrue(self.r._pop_flag('-l', alts=('--list', '--lists')))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_long_lists_alias(self):
        self._set_argv(['browse-md', '--lists', 'FILE.md'])
        self.assertTrue(self.r._pop_flag('-l', alts=('--list', '--lists')))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_repeated_flags_all_removed(self):
        # Multiple instances of the flag (mixing aliases) should ALL
        # be removed so the positional remains at ``sys.argv[1]``.
        self._set_argv(['browse-md', '-l', '--list', 'FILE.md'])
        self.assertTrue(self.r._pop_flag('-l', alts=('--list', '--lists')))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_pop_flag_does_not_consume_unrelated_args(self):
        # Unknown args are left alone — they'll surface downstream if
        # bogus, but ``_pop_flag`` itself only touches its own keys.
        self._set_argv(['browse-md', '--frobnicate', 'FILE.md'])
        self.assertFalse(self.r._pop_flag('-l', alts=('--list', '--lists')))
        self.assertEqual(
            self.r.sys.argv, ['browse-md', '--frobnicate', 'FILE.md'])


class TestBuildNodesNoLists(unittest.TestCase):
    """``_build_nodes`` with ``_INCLUDE_LISTS = False`` — headings only.

    Same fixture as ``TestBuildNodes`` (so we can compare against its
    "lists on" expectations), but the recipe is loaded with the flag
    off. Heading shape MUST be unchanged — same ids, same byte/line
    offsets — and list items MUST be absent from the tree and from
    ``_BY_ID``. The section ``byte_size`` widens to include the list
    bytes (since lists no longer act as sub-section boundaries).
    """

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
        cls.r = _load_recipe(include_lists=False)
        cls.path = '/tmp/nolists.md'
        cls.line_starts = cls.r._line_starts(cls.FIXTURE)
        cls.events = cls.r._parse(cls.FIXTURE)
        cls.root, cls.by_id = cls.r._build_nodes(
            cls.events, cls.FIXTURE, cls.line_starts, cls.path,
        )

    def test_events_contain_only_headings(self):
        # Parser-level gate: ``_parse`` skipped list emission entirely.
        kinds = {kind for kind, _ in self.events}
        self.assertEqual(kinds, {'h1', 'h2'})

    def test_root_has_two_h1_children(self):
        # Shape at root is unchanged — two H1s, no list items.
        kids = self.root._children
        self.assertEqual([k.tag for k in kids], ['h1', 'h1'])
        self.assertEqual([k.title for k in kids], ['H1', 'H1b'])

    def test_no_list_items_in_tree(self):
        # Walk every node in the tree; no ``ul`` or ``ol`` shows up.
        seen_tags = []

        def walk(node):
            for kid in node._children:
                seen_tags.append(kid.tag)
                walk(kid)

        walk(self.root)
        for tag in seen_tags:
            self.assertNotIn(tag, ('ul', 'ol'))

    def test_no_list_items_in_by_id(self):
        # ``_BY_ID`` is the flat index — every entry must be a heading
        # or the root.
        for item in self.by_id.values():
            self.assertIn(item.kind, ('root', 'heading'))

    def test_heading_ids_unchanged(self):
        # Heading ids are ``<path>#<line_offset>`` — they depend on
        # the source line numbers, which the flag doesn't affect.
        h1 = self.root._children[0]
        h2a, h2b = h1._children
        h1b = self.root._children[1]
        self.assertEqual(h1.id, f'{self.path}#0')
        self.assertEqual(h2a.id, f'{self.path}#1')
        self.assertEqual(h2b.id, f'{self.path}#5')
        self.assertEqual(h1b.id, f'{self.path}#7')

    def test_heading_byte_offsets_unchanged(self):
        # Heading byte offsets are the literal positions of the ``#``
        # in the source — independent of list emission.
        h1 = self.root._children[0]
        h2a, h2b = h1._children
        h1b = self.root._children[1]
        self.assertEqual(h1.byte_offset, self.FIXTURE.index('# H1\n'))
        self.assertEqual(h2a.byte_offset, self.FIXTURE.index('## H2a'))
        self.assertEqual(h2b.byte_offset, self.FIXTURE.index('## H2b'))
        self.assertEqual(h1b.byte_offset, self.FIXTURE.index('# H1b'))

    def test_h2a_section_subsumes_list_bytes(self):
        # With lists off, the H2a section's byte_size extends to the
        # next heading (H2b) instead of stopping at the first list
        # item. The actual byte boundary is the same as with lists
        # on (boundary rule is heading-driven for headings), but with
        # no list items in the tree, those bytes are now ONLY part of
        # the section — they're not separately enumerated.
        h1 = self.root._children[0]
        h2a, h2b = h1._children
        self.assertEqual(h2a.byte_offset + h2a.byte_size, h2b.byte_offset)
        # And the section text actually contains the list lines.
        sliced = self.FIXTURE[h2a.byte_offset:h2a.byte_offset + h2a.byte_size]
        self.assertIn('- a', sliced)
        self.assertIn('  - a1', sliced)
        self.assertIn('- b', sliced)

    def test_last_section_runs_to_eof(self):
        # The last heading (H1b) extends through ``- top`` to EOF.
        h1b = self.root._children[1]
        self.assertEqual(
            h1b.byte_offset + h1b.byte_size, len(self.FIXTURE))
        # And ``- top`` text appears inside the section slice.
        sliced = self.FIXTURE[h1b.byte_offset:h1b.byte_offset + h1b.byte_size]
        self.assertIn('- top', sliced)

    def test_heading_has_children_reflects_no_list_kids(self):
        # H2a previously had two list-item children; with lists off
        # it's a leaf. H1 (which has H2 children) still reports
        # ``has_children``.
        h1 = self.root._children[0]
        self.assertTrue(h1.has_children)
        h2a = h1._children[0]
        self.assertFalse(h2a.has_children)
        h2b = h1._children[1]
        self.assertFalse(h2b.has_children)
        # Second H1 had only a list item under it — also a leaf now.
        h1b = self.root._children[1]
        self.assertFalse(h1b.has_children)


if __name__ == '__main__':
    unittest.main()
