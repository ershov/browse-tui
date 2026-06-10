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
import os
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-md'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    Always installs a fresh module so we don't inherit a stub from
    another test file (e.g. ``test_browse_claude_render`` installs its
    own ``_Stub`` for ``Browser`` which lacks the ``config`` /
    ``expand_calls`` attributes the browse-md tests inspect). The
    recipe is reloaded via ``SourceFileLoader`` in ``_load_recipe``,
    so its ``from browse_tui import ...`` re-reads the freshly stubbed
    module on every recipe load.
    """
    mod = types.ModuleType('browse_tui')

    class _Stub:
        def __init__(self, *a, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class _BrowserStub(_Stub):
        """Browser stub that records ``expand(...)`` calls.

        ``main()`` calls ``Browser(BrowserConfig(...))`` and then
        ``b.expand(id)`` for the single-file no-anchor auto-expand
        path (ticket #566). The stub stashes the config arg on
        ``self.config`` so tests can inspect ``initial_scope``, and
        records ``expand`` calls on ``self.expand_calls``.
        """
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            # ``Browser`` is constructed positionally with a single
            # ``BrowserConfig`` instance; stash it so tests can read
            # ``self.config.initial_scope``.
            self.config = a[0] if a else None
            self.expand_calls = []

        def expand(self, id, *a, **kw):
            self.expand_calls.append((id, a, kw))

    mod.Action = _Stub
    mod.Browser = _BrowserStub
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

    ``recipes/`` is put on ``sys.path`` (idempotently) so the recipe's
    now-hard ``from md2ansi_lib import ...`` resolves to the real
    library — the same thing ``--run-py`` does at runtime by prepending
    the recipe's directory. Without this the import would silently fail
    under test and ``_parse`` would have no scanner to call.
    """
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
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
        # md2ansi_lib's ``_MD_LIST`` is strict: every line of the block
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
        # and one ``ol``. (If md2ansi_lib's rule ever tightens, this is the
        # observed-behavior canary — feel free to update.)
        text = '- foo\n1. bar\n'
        events = self.r._parse(text)
        kinds = self._kinds(events)
        # Two events — one for each marker line, with their respective
        # kinds (``ul`` for ``-``, ``ol`` for ``1.``).
        self.assertEqual(kinds, ['ul', 'ol'])

    def test_heading_events_identical_lists_off_vs_on(self):
        # The kind set ``_parse`` requests from ``md2ansi_scan`` is
        # picked by ``_INCLUDE_LISTS``, but the full grammar runs either
        # way — so heading events must be byte-for-byte identical whether
        # or not list spans are also yielded. Guards the flag-driven
        # ``_SCAN_KINDS`` selection.
        text = (
            '# H1\n'
            '- a\n'
            '- b\n'
            '## H2\n'
            '1. one\n'
            '### H3\n'
        )
        try:
            self.r._INCLUDE_LISTS = True
            with_lists = self.r._parse(text)
            self.r._INCLUDE_LISTS = False
            without_lists = self.r._parse(text)
        finally:
            # ``_load_recipe`` left the flag True for this class; restore.
            self.r._INCLUDE_LISTS = True
        headings_on = [e for e in with_lists if e[0] in self.r._HEADING_KINDS]
        # With lists off, every event is a heading already.
        self.assertEqual(without_lists, headings_on)
        # And the flag really did change list emission (sanity).
        self.assertNotEqual(with_lists, without_lists)


class TestWalkList(unittest.TestCase):
    """``_walk_list`` fans one list match out into per-line events."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _events_for(self, text):
        # ``_walk_list`` now takes ``(base, text, line_starts)`` fed from
        # an ``md2ansi_scan`` list span. The span text is the list block
        # WITHOUT its trailing newline (matching what the scanner yields,
        # and what the old ``_MD_LIST`` match also excluded), so strip it
        # here; the per-line assertions stay valid.
        line_starts = self.r._line_starts(text)
        span_text = text.rstrip('\n')
        return self.r._walk_list(0, span_text, line_starts)

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
        self.assertEqual(self.root.id, ('file', self.path))
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
        # Non-root ids: ``('content', path, line_offset)``.
        h1 = self.root._children[0]
        self.assertEqual(h1.id, ('content', self.path, 0))
        h2a = h1._children[0]
        self.assertEqual(h2a.id, ('content', self.path, 1))
        h2b = h1._children[1]
        self.assertEqual(h2b.id, ('content', self.path, 5))
        h1b = self.root._children[1]
        self.assertEqual(h1b.id, ('content', self.path, 7))

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
        self.assertIn(('file', self.path), self.by_id)
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
        # md2ansi_lib's heading patterns don't special-case a trailing
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
        # An unknown content id (no matching node in ``_BY_ID``) and an
        # unknown file id (no ``_FILES`` entry, not in ``_BY_ID``) both
        # yield ``[]`` — a stale scope/cursor id mustn't traceback.
        self.assertEqual(self.r.get_children(('content', '/some/path', 999)), [])
        self.assertEqual(self.r.get_children(('file', '/no/such/path.md')), [])

    def test_non_tuple_id_returns_empty_not_traceback(self):
        # A stale NON-tuple id (e.g. a bare string left over from before the
        # tuple migration, or an empty tuple) must return ``[]`` rather than
        # tracebacking on the ``node_id[0]`` dispatch — the guard generalizes
        # the ``None`` probe.
        self.assertEqual(self.r.get_children('stale-string-id'), [])
        self.assertEqual(self.r.get_children(()), [])
        self.assertEqual(self.r.get_children(42), [])

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
        return self.r._resolve_anchor(anchor, self.path)

    def test_empty_anchor_returns_root(self):
        self.assertEqual(self._resolve(''), ('file', self.path))

    def test_digit_anchor_exact_line(self):
        # ``#0`` resolves to ``# Intro`` (line 0).
        self.assertEqual(self._resolve('0'), ('content', self.path, 0))
        # ``#3`` resolves to ``## Details`` (line 3).
        self.assertEqual(self._resolve('3'), ('content', self.path, 3))

    def test_digit_anchor_inexact_falls_back_to_previous(self):
        # No node sits exactly on line 999999 → ``_node_at_line``
        # returns the last node; ``_resolve_anchor`` returns that id.
        self.assertEqual(self._resolve('999999'), ('content', self.path, 4))

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
        self.assertEqual(self.r._resolve_anchor('0', '/p.md'), ('file', '/p.md'))

    def test_exact_match_heading(self):
        # ``Overview`` matches ``## Overview`` exactly (display_title).
        self.assertEqual(self._resolve('Overview'), ('content', self.path, 1))

    def test_prefix_match_heading(self):
        # ``Det`` matches ``## Details`` as a prefix; no exact match.
        self.assertEqual(self._resolve('Det'), ('content', self.path, 3))

    def test_substring_match_heading(self):
        # ``clus`` matches ``# Conclusion`` as a substring; no exact /
        # prefix match in the heading set.
        self.assertEqual(self._resolve('clus'), ('content', self.path, 4))

    def test_no_match_warns_and_returns_root(self):
        from io import StringIO
        from contextlib import redirect_stderr
        buf = StringIO()
        with redirect_stderr(buf):
            result = self._resolve('xyzzy-nonexistent')
        self.assertEqual(result, ('file', self.path))
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
        self.assertEqual(
            self.r._resolve_anchor('Foo', '/t.md'), ('content', '/t.md', 1))

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
        self.assertEqual(
            self.r._resolve_anchor('Dup', '/d.md'), ('content', '/d.md', 0))

    def test_anchor_skips_list_items(self):
        # ``bullet`` matches the list-item ``- bullet`` text but
        # ``_resolve_anchor`` only scans headings; no match → warning
        # + root fall-through.
        from io import StringIO
        from contextlib import redirect_stderr
        buf = StringIO()
        with redirect_stderr(buf):
            result = self._resolve('bullet')
        self.assertEqual(result, ('file', self.path))
        self.assertIn('bullet', buf.getvalue())

    def _build_goals_fixture(self):
        # Single ``## Goals`` heading at line 0 so the three tiers can
        # be exercised independently — stored title for ``## Goals`` is
        # ``'Goals'`` after #542 stripped the marker.
        text = '## Goals\n'
        line_starts = self.r._line_starts(text)
        events = self.r._parse(text)
        root, by_id = self.r._build_nodes(events, text, line_starts, '/g.md')
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = (
            self.r._build_line_index(by_id))
        return root

    def test_case_insensitive_exact_match(self):
        # Lower-case anchor ``goals`` matches stored title ``Goals``
        # via the exact tier (both sides lowered for comparison).
        self._build_goals_fixture()
        self.assertEqual(
            self.r._resolve_anchor('goals', '/g.md'), ('content', '/g.md', 0))

    def test_case_insensitive_all_caps_exact_match(self):
        # All-caps anchor still hits the exact tier — lowering both
        # sides means ``GOALS`` == ``goals`` == display ``Goals``.
        self._build_goals_fixture()
        self.assertEqual(
            self.r._resolve_anchor('GOALS', '/g.md'), ('content', '/g.md', 0))

    def test_case_insensitive_prefix_match(self):
        # ``GOA`` is not an exact match for ``Goals`` but is a prefix
        # (after both sides are lowered to ``goa`` / ``goals``).
        self._build_goals_fixture()
        self.assertEqual(
            self.r._resolve_anchor('GOA', '/g.md'), ('content', '/g.md', 0))

    def test_case_insensitive_substring_match(self):
        # ``OAL`` is neither exact nor prefix; substring tier matches
        # ``goals`` (lowered display title) contains ``oal``.
        self._build_goals_fixture()
        self.assertEqual(
            self.r._resolve_anchor('OAL', '/g.md'), ('content', '/g.md', 0))

    def test_no_match_warning_preserves_anchor_case(self):
        # The stderr warning echoes the user's anchor string verbatim
        # (including casing) — only the comparison key is lowered.
        self._build_goals_fixture()
        from io import StringIO
        from contextlib import redirect_stderr
        buf = StringIO()
        with redirect_stderr(buf):
            result = self.r._resolve_anchor('noMatch', '/g.md')
        self.assertEqual(result, ('file', '/g.md'))
        # ``'noMatch'`` (preserved casing) appears in the warning —
        # repr-quoted because the recipe uses ``{anchor!r}``.
        self.assertIn("'noMatch'", buf.getvalue())


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
        # ``('file', path)`` root id → full file body comes back.
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
        # An id that classifies as neither file-root nor content (here a
        # bare unknown tuple) → empty preview, no crash.
        self.assertEqual(self.r.get_preview(('mystery', '/some/path')), '')

    def test_non_tuple_id_returns_empty_not_traceback(self):
        # A stale NON-tuple id (bare string / empty tuple / scalar) must
        # return '' rather than tracebacking on the ``node_id[0]`` dispatch —
        # the guard generalizes the ``None`` pseudo-row case.
        self.assertEqual(self.r.get_preview('stale-string-id'), '')
        self.assertEqual(self.r.get_preview(()), '')
        self.assertEqual(self.r.get_preview(42), '')

    def test_line_with_no_node_below_returns_empty(self):
        # Line ``-1`` is before every parsed node — _BY_LINE miss,
        # _node_at_line returns None → empty.
        out = self.r.get_preview(('content', self.path, -1))
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

    def test_md_color_default_on_renders_via_real_library(self):
        # md2ansi_lib is a hard dependency, so a freshly loaded recipe
        # defaults ``_MD_COLOR`` on and binds ``_md2ansi_fn`` to the
        # real library function (``recipes/`` is on ``sys.path``).
        fresh = _load_recipe()
        self.assertTrue(fresh._MD_COLOR)  # module-load default
        self.assertIsNotNone(fresh._md2ansi_fn)
        # With ``_MD_COLOR`` on and no monkeypatch, ``get_preview`` runs
        # the slice through the library: the output differs from the raw
        # slice and carries the ANSI escape introducer the library emits.
        self.r._MD_COLOR = True
        h1 = self.root._children[0]
        raw = self.FIXTURE[h1.byte_offset:h1.byte_offset + h1.byte_size]
        out = self.r.get_preview(h1.id)
        self.assertNotEqual(out, raw)
        self.assertIn('\x1b[', out)


class _FakeCtx:
    """Recorder for ``ctx`` interactions used by action handlers."""

    def __init__(self):
        self.dropped = 0
        self.flashes = []
        self.errors = []

    def drop_preview_cache(self, id_=None):
        self.dropped += 1

    def flash(self, text, log=False):
        self.flashes.append(text)

    def error(self, text):
        self.errors.append(text)


class TestToggleMd(unittest.TestCase):
    """``_action_toggle_md`` flips ``_MD_COLOR`` and notifies ctx."""

    def setUp(self):
        self.r = _load_recipe()
        # The toggle only flips ``_MD_COLOR``; it never calls the render
        # function, so an identity stub keeps the test independent of the
        # real library's output. Start from a known-on state.
        self.r._md2ansi_fn = lambda text, line_width: text
        self.r._MD_COLOR = True
        self.ctx = _FakeCtx()

    def test_flip_true_to_false(self):
        self.r._action_toggle_md(self.ctx)
        self.assertFalse(self.r._MD_COLOR)
        self.assertEqual(self.ctx.dropped, 1)
        self.assertEqual(self.ctx.flashes, ['md preview: raw'])

    def test_flip_back_round_trip(self):
        self.r._action_toggle_md(self.ctx)  # True -> False
        self.r._action_toggle_md(self.ctx)  # False -> True
        self.assertTrue(self.r._MD_COLOR)
        self.assertEqual(self.ctx.dropped, 2)
        self.assertEqual(self.ctx.flashes,
                         ['md preview: raw', 'md preview: colored'])

    def test_flip_from_false(self):
        # Starting from False — flash reports the new state ("colored").
        self.r._MD_COLOR = False
        self.r._action_toggle_md(self.ctx)
        self.assertTrue(self.r._MD_COLOR)
        self.assertEqual(self.ctx.flashes, ['md preview: colored'])


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
        self.flashes = []

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

    def flash(self, text, log=False):
        self.flashes.append(text)


class _SrcItem:
    """Bare Item stand-in for ``_run_source_command`` tests."""
    def __init__(self, *, id, kind, byte_offset=0, byte_size=0):
        self.id = id
        self.kind = kind
        self.byte_offset = byte_offset
        self.byte_size = byte_size


class _ScopeRootPseudoItem:
    """Framework's scope-root pseudo-Item stand-in (#552).

    Mirrors what ``visible_items`` fabricates for a scoped session
    whose scope id isn't in any cached children list: an Item with
    ``id`` / ``title`` / ``has_children`` / ``synthetic`` but none of
    our recipe-added hidden attrs (``kind``, ``byte_offset``,
    ``byte_size``). Accessing those attrs raises ``AttributeError`` —
    exactly the surface that #552 crashed on.
    """
    def __init__(self, *, id):
        self.id = id
        self.title = str(id)
        self.has_children = True
        self.synthetic = True


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
        # Snapshot module state we might patch (so the scope-root
        # pseudo-item branch can be exercised). ``_load_recipe`` gives a
        # fresh module per test, but we restore explicitly for clarity
        # and to honour the ticket's "restore _BY_ID / _ROOT_PATH"
        # request (#552).
        self._mod_saved = {
            '_ROOT_PATH': self.r._ROOT_PATH,
            '_BY_ID': dict(self.r._BY_ID),
        }
        self.r._ROOT_PATH = self.path
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
        # Restore patched module state.
        self.r._ROOT_PATH = self._mod_saved['_ROOT_PATH']
        self.r._BY_ID = self._mod_saved['_BY_ID']

    def test_empty_targets_noop(self):
        ctx = _SrcCmdCtx(targets=[])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [])

    def test_root_only_opens_original_path(self):
        # ``root.id`` is the absolute path; the command should be the
        # default split + that path. No tempfile.
        root = _SrcItem(id=('file', self.path), kind='root')
        ctx = _SrcCmdCtx(targets=[root])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [['less', '-R', self.path]])

    def test_single_root_cursor_only_opens_file(self):
        # #572: cursor on a file-root with NO selection → single
        # target → opens the file directly (no tempfile). This is
        # the same shape as ``test_root_only_opens_original_path``
        # but pinned with the post-#572 name to document the
        # single-target shortcut contract.
        import os
        root = _SrcItem(id=('file', self.path), kind='root')
        ctx = _SrcCmdCtx(targets=[root])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [['less', '-R', self.path]])
        # No tempfile was produced — last argv is the original path.
        argv = ctx.calls[0]
        self.assertFalse(argv[-1].endswith('.md') and argv[-1] != self.path)

    def test_single_root_marked_alone_opens_file(self):
        # #572: one file-root space-marked, nothing else in the
        # selection → ``ctx.targets`` returns just that root → single
        # target → opens the file directly. Same outcome as
        # cursor-only; this asserts the contract still holds when
        # the single target came from a space-mark rather than the
        # cursor.
        root = _SrcItem(id=('file', self.path), kind='root')
        ctx = _SrcCmdCtx(targets=[root])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [['less', '-R', self.path]])

    def test_root_mixed_with_non_root_combines_into_tempfile(self):
        # #572: Mixed targets no longer trigger root-wins. The
        # file-root expands to the whole file range and merges with
        # the heading's sub-range (the whole-file range absorbs it),
        # producing a tempfile that contains the entire file body.
        root = _SrcItem(id=('file', self.path), kind='root')
        leaf = _SrcItem(id=('content', self.path, 3), kind='heading',
                        byte_offset=0, byte_size=5)
        ctx = _SrcCmdCtx(targets=[leaf, root])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        # Exactly one call; last argv is the tempfile path with .md.
        self.assertEqual(len(ctx.calls), 1)
        argv = ctx.calls[0]
        self.assertEqual(argv[:2], ['less', '-R'])
        self.assertTrue(argv[2].endswith('.md'))
        # Single-file output → no header. Whole-file range absorbs
        # the heading's sub-range.
        self.assertEqual(ctx.last_tmp_contents, self.r._FILE_TEXT)

    def test_single_non_root_writes_temp_and_runs(self):
        leaf = _SrcItem(id=('content', self.path, 2), kind='heading',
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
        a = _SrcItem(id=('content', self.path, 0), kind='heading',
                     byte_offset=0, byte_size=5)   # 'AAAA\n'
        b = _SrcItem(id=('content', self.path, 2), kind='heading',
                     byte_offset=10, byte_size=5)  # 'CCCC\n'
        c = _SrcItem(id=('content', self.path, 4), kind='heading',
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
        a = _SrcItem(id=('content', self.path, 0), kind='heading',
                     byte_offset=0, byte_size=10)   # 'AAAA\nBBBB\n'
        b = _SrcItem(id=('content', self.path, 1), kind='heading',
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
        leaf = _SrcItem(id=('content', self.path, 0), kind='heading',
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
            leaf = _SrcItem(id=('content', self.path, 0), kind='heading',
                            byte_offset=0, byte_size=5)
            ctx = _SrcCmdCtx(targets=[leaf])
            self.r._run_source_command(ctx, 'PAGER', 'less -R')
            argv = ctx.calls[0]
            self.assertEqual(argv[:2], ['bat', '--paging=always'])
        finally:
            os.environ.pop('PAGER', None)

    def test_multi_select_two_non_root_same_file_both_ranges_in_tempfile(self):
        # #568 regression: ``V`` / ``E`` with a multi-select of two
        # non-root targets in the SAME file must hand PAGER / EDITOR a
        # tempfile containing BOTH targets' byte slices — not just the
        # cursor's. This guards the consumer-code path: given a ctx
        # whose ``targets`` returns both items (per the framework's
        # ``selected if non-empty, else [cursor]`` contract), the
        # recipe MUST process every target and the tempfile MUST
        # include every selected section. The ticket cites a
        # symptom where only the cursor's section appears even
        # though both items are marked.
        a = _SrcItem(id=('content', self.path, 0), kind='heading',
                     byte_offset=0, byte_size=5)    # 'AAAA\n'
        b = _SrcItem(id=('content', self.path, 2), kind='heading',
                     byte_offset=10, byte_size=5)   # 'CCCC\n'
        ctx = _SrcCmdCtx(targets=[a, b])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        # Exactly one PAGER invocation on a tempfile (last argv).
        self.assertEqual(len(ctx.calls), 1)
        argv = ctx.calls[0]
        self.assertTrue(argv[-1].endswith('.md'))
        # Both ranges present in the tempfile contents (captured by
        # ``_SrcCmdCtx.run_external`` synchronously before the
        # ``finally`` unlinks the path).
        self.assertIn('AAAA\n', ctx.last_tmp_contents)
        self.assertIn('CCCC\n', ctx.last_tmp_contents)

    def test_scope_root_pseudo_item_takes_root_path(self):
        # #552: when the framework hands us its synthetic scope-root
        # pseudo-Item (no ``kind`` / ``byte_offset`` attrs but
        # ``id == _ROOT_PATH``), classify it as root and open the
        # original file directly — no tempfile, no AttributeError.
        pseudo = _ScopeRootPseudoItem(id=('file', self.path))
        ctx = _SrcCmdCtx(targets=[pseudo])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [['less', '-R', self.path]])

    def test_scope_root_pseudo_item_mixed_with_non_root_combines(self):
        # #572: mixed targets no longer trigger root-wins. The
        # scope-root pseudo-Item, like a real root, expands to the
        # whole-file range and merges with the heading's sub-range.
        # Result: a tempfile containing the whole file body.
        pseudo = _ScopeRootPseudoItem(id=('file', self.path))
        leaf = _SrcItem(id=('content', self.path, 3), kind='heading',
                        byte_offset=0, byte_size=5)
        ctx = _SrcCmdCtx(targets=[leaf, pseudo])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(len(ctx.calls), 1)
        argv = ctx.calls[0]
        self.assertEqual(argv[:2], ['less', '-R'])
        self.assertTrue(argv[2].endswith('.md'))
        # Single-file output → no header; whole-file range absorbs
        # the heading's sub-range.
        self.assertEqual(ctx.last_tmp_contents, self.r._FILE_TEXT)


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

    def test_reload_only_at_top_level_probe(self):
        # Post-#559: ``BrowserConfig(root_id=None)`` means Ctrl-R
        # always calls ``get_children(None, reload=True)``. Reload
        # requests at any other node-id (including ``_ROOT_PATH`` —
        # which is now just a per-file root, not the Browser root)
        # short-circuit to the cached branch.
        self._write('# Delta\n')
        self.r.get_children(('file', self.r._ROOT_PATH), reload=True)
        # Stale content survives — no reparse ran.
        self.assertEqual(self._heading_titles(), ['Alpha', 'Sub-Alpha'])
        # Reload at ``None`` does reparse.
        self.r.get_children(None, reload=True)
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
        # The usage line documents the optional ``-l`` flag and the
        # file/dir positionals (with the anchor syntax detailed below).
        self.assertIn('browse-md [-l] [FILE.md', self.r._HELP_INTRO)

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


class TestArgvErrors(unittest.TestCase):
    """End-to-end argv validation in ``main()`` — #550 + #551.

    Exercises the leftover-arg / file-not-found error paths by patching
    the recipe's ``sys.argv`` and invoking ``main()`` directly. Each
    case captures stderr via ``contextlib.redirect_stderr`` and asserts
    on the ``SystemExit`` code + the emitted message. ``main()`` doesn't
    reach the Browser construction in the error paths (``sys.exit(2)``
    fires before that), so the stub ``Browser`` from ``_stub_browse_tui``
    is never invoked.
    """

    def setUp(self):
        # Fresh recipe per test so each case starts with a clean module
        # (``_pop_flag`` mutates ``sys.argv`` in place — restoring isn't
        # enough since other globals also get touched on the success
        # path; we never hit the success path here anyway).
        self.r = _load_recipe(include_lists=False)
        self._saved_argv = list(self.r.sys.argv)

    def tearDown(self):
        self.r.sys.argv[:] = self._saved_argv

    def _set_argv(self, argv):
        self.r.sys.argv[:] = argv

    def _run_main_capture(self):
        """Invoke ``main()``; return ``(exit_code, stderr_text)``."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                self.r.main()
        code = cm.exception.code
        return code, buf.getvalue()

    def test_no_args_defaults_to_current_directory(self):
        # No positionals ⇒ ``browse-md .`` — the current directory is
        # expanded to its markdown files rather than dying.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            md = os.path.join(tmp, 'doc.md')
            with open(md, 'w') as f:
                f.write('# H\n')
            saved = os.getcwd()
            os.chdir(tmp)
            try:
                self._set_argv(['browse-md'])
                # Builds a Browser over the cwd's files (no SystemExit).
                # The stubbed Browser has no ``run`` method, so ``main()``
                # raises ``AttributeError`` just past construction — by
                # which point ``_INPUT_FILES`` is populated.
                with self.assertRaises(AttributeError):
                    self.r.main()
                self.assertEqual(self.r._INPUT_FILES, [(md, '')])
            finally:
                os.chdir(saved)

    def test_missing_file_path_reports_path_without_usage(self):
        self._set_argv(['browse-md', '/no/such/path.md'])
        code, err = self._run_main_capture()
        self.assertEqual(code, 2)
        self.assertIn('no such file or directory: /no/such/path.md', err)
        # ``with_usage=False`` for the file-not-found path — the user
        # already got the positional shape right syntactically.
        self.assertNotIn(self.r._USAGE, err)

    def test_unknown_long_option(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
                'w', suffix='.md', delete=False) as tmp:
            tmp.write('# H\n')
            tmp_path = tmp.name
        try:
            self._set_argv(['browse-md', '--bogus', tmp_path])
            code, err = self._run_main_capture()
            self.assertEqual(code, 2)
            self.assertIn('unrecognised option: --bogus', err)
        finally:
            import os as _os
            _os.unlink(tmp_path)

    def test_unknown_short_option(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
                'w', suffix='.md', delete=False) as tmp:
            tmp.write('# H\n')
            tmp_path = tmp.name
        try:
            self._set_argv(['browse-md', '-x', tmp_path])
            code, err = self._run_main_capture()
            self.assertEqual(code, 2)
            self.assertIn('unrecognised option: -x', err)
        finally:
            import os as _os
            _os.unlink(tmp_path)

    def test_extra_positional_nonexistent_file(self):
        # After #553 the recipe accepts multiple file positionals, so
        # an "extra" token is treated as another file. A non-existent
        # extra positional therefore surfaces as "no such file: extra"
        # rather than "unexpected argument".
        import tempfile
        with tempfile.NamedTemporaryFile(
                'w', suffix='.md', delete=False) as tmp:
            tmp.write('# H\n')
            tmp_path = tmp.name
        try:
            self._set_argv(['browse-md', tmp_path, 'extra'])
            code, err = self._run_main_capture()
            self.assertEqual(code, 2)
            self.assertIn('no such file or directory: extra', err)
        finally:
            import os as _os
            _os.unlink(tmp_path)

    def test_bogus_and_extra_reports_one(self):
        # When both a bogus flag and an extra positional are present,
        # the recipe surfaces ONE of them (whichever its validation
        # walk hits first — currently the flag). We just assert exit 2
        # and that at least one of the bad tokens is named.
        import tempfile
        with tempfile.NamedTemporaryFile(
                'w', suffix='.md', delete=False) as tmp:
            tmp.write('# H\n')
            tmp_path = tmp.name
        try:
            self._set_argv(['browse-md', '--bogus', tmp_path, 'extra'])
            code, err = self._run_main_capture()
            self.assertEqual(code, 2)
            self.assertTrue(
                '--bogus' in err or 'extra' in err,
                f'expected at least one bad token named in stderr, got: {err!r}',
            )
        finally:
            import os as _os
            _os.unlink(tmp_path)


class TestMissingDependencyGate(unittest.TestCase):
    """``main()`` fails fast when a hard dependency couldn't be imported.

    The tree is built by ``md_doc.build_doc_tree``, which in turn derives
    structure from ``md2ansi_scan`` — so both ``md2ansi_lib`` and
    ``md_doc`` are hard dependencies. ``main()`` checks ``_md2ansi_scan``
    and then ``md_doc`` before any parsing and exits 2 via ``_die`` when
    either is ``None`` — simulating an environment where the import
    failed. The stub ``browse_tui`` is already installed by the loader,
    and the gate fires before the Browser is ever constructed.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._saved_argv = list(self.r.sys.argv)

    def tearDown(self):
        self.r.sys.argv[:] = self._saved_argv

    def _real_md_file(self):
        """Write a throwaway ``.md`` so the gate (not argv checks) fires."""
        import tempfile
        with tempfile.NamedTemporaryFile(
                'w', suffix='.md', delete=False, encoding='utf-8') as tmp:
            tmp.write('# H1\n')
            return tmp.name

    def _run_main_capture(self, tmp_path):
        """Drive ``main()`` on ``tmp_path``; return ``(exit_code, stderr)``."""
        import contextlib
        import io
        self.r.sys.argv[:] = ['browse-md', tmp_path]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                self.r.main()
        return cm.exception.code, buf.getvalue()

    def test_missing_scanner_exits_two_with_message(self):
        import os
        # A real .md file so the gate is exercised, not the argv checks
        # (the gate runs first regardless, but a valid file ensures the
        # only reason main() can exit is the missing-dependency gate).
        tmp_path = self._real_md_file()
        try:
            self.r._md2ansi_scan = None
            code, err = self._run_main_capture(tmp_path)
            self.assertEqual(code, 2)
            self.assertIn('requires the md2ansi_lib module', err)
            # ``_die`` prefixes the recipe name.
            self.assertIn('browse-md:', err)
            # ``with_usage=False`` — the usage line is NOT printed.
            self.assertNotIn(self.r._USAGE, err)
        finally:
            os.unlink(tmp_path)

    def test_missing_md_doc_exits_two_with_message(self):
        import os
        # Mirror the scanner gate for the ``md_doc`` hard dependency.
        # Leave ``_md2ansi_scan`` intact so the FIRST gate passes and we
        # exercise the ``md_doc is None`` branch specifically.
        tmp_path = self._real_md_file()
        try:
            self.r.md_doc = None
            code, err = self._run_main_capture(tmp_path)
            self.assertEqual(code, 2)
            self.assertIn('requires the md_doc module', err)
            # ``_die`` prefixes the recipe name.
            self.assertIn('browse-md:', err)
            # ``with_usage=False`` — the usage line is NOT printed.
            self.assertNotIn(self.r._USAGE, err)
        finally:
            os.unlink(tmp_path)


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
        # Heading ids are ``('content', path, line_offset)`` — they depend on
        # the source line numbers, which the flag doesn't affect.
        h1 = self.root._children[0]
        h2a, h2b = h1._children
        h1b = self.root._children[1]
        self.assertEqual(h1.id, ('content', self.path, 0))
        self.assertEqual(h2a.id, ('content', self.path, 1))
        self.assertEqual(h2b.id, ('content', self.path, 5))
        self.assertEqual(h1b.id, ('content', self.path, 7))

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


class TestTextNodes(unittest.TestCase):
    """Loose body-text ``[text]`` leaves in the headings-only eager tree.

    ``build_doc_tree`` (lists OFF — browse-md's default ``_INCLUDE_LISTS``)
    synthesises a dim ``[text]`` leaf for the body run preceding a scope's
    first heading, inserted as that scope's FIRST child. ``_build_nodes`` /
    ``_to_item`` must map it onto a leaf ``Item`` (``[text]`` tag, ``dim``
    style, no children), its preview must slice to just that run, and the
    anchor flow must keep treating it correctly (title scan skips it; a
    line anchor resolves to it). The same run surfaces in the lazy ``#md:``
    ref subtree via ``_md_node_item``.

    Fixture has a loose run before BOTH the root's first heading and a
    nested heading, so the root-level and nested cases are both exercised::

        intro paragraph   <- line 0  (before the root's first heading)
        # Top             <- line 1
        body before sub   <- line 2  (before ``# Top``'s first sub-heading)
        ## Sub            <- line 3
        sub body          <- line 4
    """

    FIXTURE = (
        'intro paragraph\n'   # line 0
        '# Top\n'             # line 1
        'body before sub\n'   # line 2
        '## Sub\n'            # line 3
        'sub body\n'          # line 4
    )

    def _build(self, include_lists):
        r = _load_recipe(include_lists=include_lists)
        path = '/tmp/text-nodes.md'
        line_starts = r._line_starts(self.FIXTURE)
        events = r._parse(self.FIXTURE)
        root, by_id = r._build_nodes(events, self.FIXTURE, line_starts, path)
        return r, path, root, by_id

    # --- eager primary-file tree -----------------------------------------

    def test_root_first_child_is_dim_text_leaf(self):
        # Headings-only: the loose run before ``# Top`` is the root's FIRST
        # child — a leaf tagged ``[text]`` with the dim style, sitting ahead
        # of the heading it precedes.
        _r, path, root, _by_id = self._build(include_lists=False)
        kids = root._children
        self.assertEqual([(k.tag, k.title) for k in kids],
                         [('text', 'intro paragraph'), ('h1', 'Top')])
        text_leaf = kids[0]
        self.assertEqual(text_leaf.kind, 'text')
        self.assertEqual(text_leaf.tag_style, 'dim')
        self.assertFalse(text_leaf.has_children)
        self.assertEqual(text_leaf._children, [])
        self.assertEqual(text_leaf.id, ('content', path, 0))

    def test_nested_scope_also_gets_text_leaf(self):
        # The run before ``# Top``'s first sub-heading is ``# Top``'s first
        # child, ahead of ``## Sub``.
        _r, _path, root, _by_id = self._build(include_lists=False)
        top = root._children[1]
        self.assertEqual([(k.tag, k.title) for k in top._children],
                         [('text', 'body before sub'), ('h2', 'Sub')])
        self.assertEqual(top._children[0].kind, 'text')

    def test_lists_mode_has_no_text_leaves(self):
        # With ``-l`` (lists on) ``build_doc_tree`` synthesises NO text
        # nodes, so the tree is heading-only at the top and nothing carries
        # the ``[text]`` tag anywhere.
        _r, _path, root, _by_id = self._build(include_lists=True)
        self.assertEqual([(k.tag, k.title) for k in root._children],
                         [('h1', 'Top')])
        seen = []

        def walk(node):
            for kid in node._children:
                seen.append(kid.tag)
                walk(kid)

        walk(root)
        self.assertNotIn('text', seen)

    def test_text_leaf_carries_contract_fields(self):
        # ``_to_item`` stamps the hidden contract fields on a text node just
        # like a heading: byte_offset/byte_size verbatim from the MdNode, the
        # boundary-rule line_size, and the file back-reference.
        _r, path, root, _by_id = self._build(include_lists=False)
        text_leaf = root._children[0]
        self.assertEqual(text_leaf.byte_offset, 0)
        # The run is just its own line ``intro paragraph\n`` (16 chars).
        self.assertEqual(text_leaf.byte_size, len('intro paragraph\n'))
        self.assertEqual(text_leaf.line_offset, 0)
        # Section ends at the heading it precedes (line 1) → one line.
        self.assertEqual(text_leaf.line_size, 1)
        self.assertEqual(text_leaf.file_path, path)

    def test_by_id_indexes_text_leaves(self):
        # ``flat`` (and thus ``by_id``) carries the text nodes even though
        # ``events`` does not — root + 2 headings + 2 text runs = 5 entries,
        # while only 2 heading events exist. (Confirms ``event_kinds`` is not
        # indexed with a shifted position — headings still tag correctly.)
        _r, _path, root, by_id = self._build(include_lists=False)
        self.assertEqual(len(by_id), 5)
        # Both heading rows kept their correct level tags despite the offset
        # between ``flat`` and ``events``.
        self.assertEqual(root._children[1].tag, 'h1')
        self.assertEqual(root._children[1]._children[1].tag, 'h2')

    # --- preview ----------------------------------------------------------

    def test_text_leaf_preview_is_the_run(self):
        # ``get_preview`` slices the text node's own byte window — exactly
        # the loose run, nothing more.
        r, path, root, by_id = self._build(include_lists=False)
        r._FILE_TEXT = self.FIXTURE
        r._BY_ID = by_id
        r._BY_LINE, r._LINES_SORTED = r._build_line_index(by_id)
        r._MD_COLOR = False
        r._BROWSER = None
        text_leaf = root._children[0]
        self.assertEqual(r.get_preview(text_leaf.id), 'intro paragraph\n')

    # --- anchor resolution ------------------------------------------------

    def test_title_anchor_skips_text_leaves(self):
        # A non-digit anchor scans HEADINGS only. ``intro`` is a substring of
        # the text leaf's title but matches no heading, so the lookup falls
        # through to the file root (not the text node).
        r, path, root, by_id = self._build(include_lists=False)
        r._BY_ID = by_id
        r._BY_LINE, r._LINES_SORTED = r._build_line_index(by_id)
        self.assertEqual(r._resolve_anchor('intro', path), ('file', path))
        # A real heading title still resolves to its heading.
        self.assertEqual(r._resolve_anchor('Top', path), ('content', path, 1))

    def test_line_anchor_resolves_to_text_leaf(self):
        # A digit anchor goes through ``_node_at_line``, whose by-line index
        # DOES include text nodes. Line 0 is the text run, so the anchor
        # resolves to it (a valid id whose preview is the run) rather than
        # falling back to root.
        r, path, root, by_id = self._build(include_lists=False)
        r._BY_ID = by_id
        r._BY_LINE, r._LINES_SORTED = r._build_line_index(by_id)
        self.assertEqual(r._resolve_anchor('0', path), ('content', path, 0))
        self.assertEqual(r._node_at_line(0).kind, 'text')

    def test_lone_text_child_does_not_auto_expand(self):
        # ``_lone_heading_child_id`` only cascades a sole HEADING child, so a
        # file-root whose only child is a text leaf never auto-expands. (Real
        # markdown can't actually produce a heading/root with a SOLE text
        # child — a text run only exists ahead of a sibling heading — so the
        # ``kind == 'heading'`` guard is the safety net; assert it directly on
        # a synthetic single-text-child root.)
        r, path, root, _by_id = self._build(include_lists=False)
        text_leaf = root._children[0]
        self.assertEqual(text_leaf.kind, 'text')
        fake_root = type('FR', (), {'_children': [text_leaf]})()
        fake_fs = type('FS', (), {'file_root': fake_root})()
        r._FILES = {path: fake_fs}
        self.assertIsNone(r._lone_heading_child_id(('file', path)))


# ====================================================================
# Multi-file support (#553)
# ====================================================================
#
# These suites exercise the post-#553 multi-file pipeline: argv with
# more than one positional, the synthetic multi-root, per-file root
# subtrees, anchor resolution across files, preview dispatch by
# ``_classify_id``, ``V``/``E`` semantics on multi-root and cross-file
# selections, and Ctrl-R reparse of every input file.
#
# Module globals touched by these tests (``_FILES``, ``_INPUT_FILES``,
# ``_BY_ID``, ``_FILE_TEXT``, ``_BY_LINE``, ``_LINES_SORTED``,
# ``_ROOT_PATH``) are restored to safe defaults in ``tearDown`` so
# state doesn't leak across tests.


class _MultiCaseBase(unittest.TestCase):
    """Common setUp/tearDown for the multi-file suites.

    Writes two on-disk markdown fixtures with distinguishable heading
    sets, snapshots every module global the suites touch, and exposes
    a ``_load_multi(...)`` helper that calls ``_reparse`` after
    populating ``_INPUT_FILES`` — same code path ``main()`` uses at
    startup, minus the argv parse.
    """

    A_TEXT = (
        '# A1\n'        # line 0
        '## A2\n'       # line 1
        'body of A2\n'  # line 2
        '# A1b\n'       # line 3
    )

    B_TEXT = (
        '# B1\n'        # line 0
        '## B2\n'       # line 1
        '## B2b\n'      # line 2
        '# B1b\n'       # line 3
    )

    def setUp(self):
        import os
        import tempfile
        self.r = _load_recipe()
        # Snapshot every module global the suites might scribble on.
        # ``tearDown`` restores them so a failing assert doesn't bleed
        # into a sibling suite.
        self._saved = {
            '_FILES': dict(self.r._FILES),
            '_INPUT_FILES': list(self.r._INPUT_FILES),
            '_BY_ID': dict(self.r._BY_ID),
            '_FILE_TEXT': self.r._FILE_TEXT,
            '_BY_LINE': dict(self.r._BY_LINE),
            '_LINES_SORTED': list(self.r._LINES_SORTED),
            '_ROOT_PATH': self.r._ROOT_PATH,
            '_ANCHOR': self.r._ANCHOR,
        }
        # Two on-disk fixtures so ``_reparse`` reads actual files.
        # ``delete=False`` + manual unlink in tearDown so the file
        # exists for the duration of the test.
        fa = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8')
        fa.write(self.A_TEXT)
        fa.close()
        fb = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8')
        fb.write(self.B_TEXT)
        fb.close()
        self.path_a = fa.name
        self.path_b = fb.name

    def tearDown(self):
        import os
        # Restore module globals.
        self.r._FILES = self._saved['_FILES']
        self.r._INPUT_FILES = self._saved['_INPUT_FILES']
        self.r._BY_ID = self._saved['_BY_ID']
        self.r._FILE_TEXT = self._saved['_FILE_TEXT']
        self.r._BY_LINE = self._saved['_BY_LINE']
        self.r._LINES_SORTED = self._saved['_LINES_SORTED']
        self.r._ROOT_PATH = self._saved['_ROOT_PATH']
        self.r._ANCHOR = self._saved['_ANCHOR']
        # Clean up on-disk fixtures.
        for p in (self.path_a, self.path_b):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _load_multi(self, *files):
        """Populate ``_INPUT_FILES`` with the given paths and reparse.

        ``files`` is a sequence of ``(abs_path, anchor)`` tuples — or
        bare paths, in which case the anchor defaults to ``''``.
        Returns ``None`` (post-#559 ``_reparse`` has no root to
        return — files ARE the top-level entries).
        """
        normalised = []
        for f in files:
            if isinstance(f, tuple):
                normalised.append(f)
            else:
                normalised.append((f, ''))
        self.r._INPUT_FILES = normalised
        return self.r._reparse()


class TestArgvMulti(_MultiCaseBase):
    """``main()`` argv parsing with multiple positionals."""

    def _run_main_capture(self, argv):
        import contextlib
        import io
        self.r.sys.argv[:] = argv
        buf = io.StringIO()
        # ``main()`` reaches the Browser construction on the success
        # path; the stubbed ``Browser`` from ``_stub_browse_tui`` has
        # no ``run`` method, which raises ``AttributeError``. That's
        # fine — by that point all the argv-parsing side effects we
        # want to assert on (``_INPUT_FILES``, ``_ANCHOR``,
        # ``_INCLUDE_LISTS``) have already landed. Catch both
        # ``SystemExit`` (error paths) and ``AttributeError`` (success
        # path past Browser construction).
        with contextlib.redirect_stderr(buf):
            try:
                self.r.main()
            except (SystemExit, AttributeError):
                pass
        return buf.getvalue()

    def test_two_files_recorded_in_argv_order(self):
        self._run_main_capture(['browse-md', self.path_a, self.path_b])
        self.assertEqual(
            self.r._INPUT_FILES,
            [(self.path_a, ''), (self.path_b, '')],
        )

    def test_three_files_recorded_in_argv_order(self):
        # A third on-disk fixture for this case only.
        import os
        import tempfile
        fc = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8')
        fc.write('# C\n')
        fc.close()
        try:
            self._run_main_capture(
                ['browse-md', self.path_a, self.path_b, fc.name])
            self.assertEqual(
                self.r._INPUT_FILES,
                [(self.path_a, ''), (self.path_b, ''), (fc.name, '')],
            )
        finally:
            os.unlink(fc.name)

    def test_lists_flag_before_files(self):
        self._run_main_capture(
            ['browse-md', '-l', self.path_a, self.path_b])
        self.assertTrue(self.r._INCLUDE_LISTS)
        self.assertEqual(len(self.r._INPUT_FILES), 2)

    def test_lists_flag_after_files(self):
        self._run_main_capture(
            ['browse-md', self.path_a, self.path_b, '-l'])
        self.assertTrue(self.r._INCLUDE_LISTS)
        self.assertEqual(len(self.r._INPUT_FILES), 2)

    def test_lists_flag_between_files(self):
        # ``_pop_flag`` removes ``-l`` from anywhere in argv; the
        # remaining positionals are processed in left-to-right order.
        self._run_main_capture(
            ['browse-md', self.path_a, '-l', self.path_b])
        self.assertTrue(self.r._INCLUDE_LISTS)
        self.assertEqual(
            [p for p, _ in self.r._INPUT_FILES],
            [self.path_a, self.path_b],
        )

    def test_anchor_on_first_file_only(self):
        # First positional has an anchor, second doesn't. Both files
        # get loaded; ``_ANCHOR`` is the first positional's anchor.
        self._run_main_capture(
            ['browse-md', f'{self.path_a}#A2', self.path_b])
        self.assertEqual(self.r._INPUT_FILES,
                         [(self.path_a, 'A2'), (self.path_b, '')])
        self.assertEqual(self.r._ANCHOR, 'A2')

    def test_anchor_on_second_file_only(self):
        # Anchor on the second positional → first-anchor-wins still
        # makes B's anchor the winner because A has none.
        self._run_main_capture(
            ['browse-md', self.path_a, f'{self.path_b}#B2'])
        self.assertEqual(self.r._INPUT_FILES,
                         [(self.path_a, ''), (self.path_b, 'B2')])
        self.assertEqual(self.r._ANCHOR, 'B2')

    def test_anchor_on_both_files_first_wins(self):
        # Both files anchored — ``_ANCHOR`` records the FIRST anchor
        # in argv order. The second anchor is stored on the tuple but
        # ignored by the initial-scope resolver.
        self._run_main_capture(
            ['browse-md', f'{self.path_a}#A2', f'{self.path_b}#B2'])
        self.assertEqual(self.r._INPUT_FILES,
                         [(self.path_a, 'A2'), (self.path_b, 'B2')])
        self.assertEqual(self.r._ANCHOR, 'A2')

    def test_missing_file_in_middle_dies(self):
        # Non-existent positional between two real files surfaces the
        # MISSING path verbatim in the error — pre-expanduser /
        # pre-abspath user-input.
        import contextlib
        import io
        self.r.sys.argv[:] = [
            'browse-md', self.path_a, '/no/such/middle.md', self.path_b,
        ]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                self.r.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('no such file or directory: /no/such/middle.md',
                      buf.getvalue())


class TestBuildMulti(_MultiCaseBase):
    """Per-file roots constructed by ``_reparse`` as top-level entries.

    Post-#559: there is no synthetic multi-root. ``get_children(None)``
    returns the per-file roots in argv order; files ARE the top-level
    rows.
    """

    def test_get_children_none_returns_per_file_roots_in_argv_order(self):
        # The framework's ``get_children(None)`` probe is the only
        # path to the top-level rows — files in argv order.
        self._load_multi(self.path_a, self.path_b)
        top = self.r.get_children(None)
        self.assertEqual([c.id for c in top],
                         [('file', self.path_a), ('file', self.path_b)])

    def test_per_file_root_titles_match_md_ref_label(self):
        # Per-file root titles always come from ``_md_ref_label`` against
        # the cwd's git root (else cwd) — the same general anchoring used
        # for links. A file directly in the project root collapses to its
        # bare basename; one in a subdir keeps its subdir path. The
        # ``/tmp`` fixtures here aren't under the worktree, so the title is
        # whatever ``_md_ref_label`` computes — assert that equality.
        import os
        self._load_multi(self.path_a, self.path_b)
        top = self.r.get_children(None)
        titles = [c.title for c in top]
        cwd = os.getcwd()
        project_root = self.r.md_doc.find_git_root(cwd) or cwd
        self.assertEqual(titles, [
            self.r._md_ref_label(self.path_a, cwd, project_root),
            self.r._md_ref_label(self.path_b, cwd, project_root),
        ])

    def test_per_file_root_titles_relative_when_spanning_dirs(self):
        # Per-file root titles are project-root-relative: ``_reparse``
        # labels each root via its ``_md_ref_label`` (relative to the
        # cwd's git root / cwd) so same-named files across dirs
        # disambiguate. Build a two-dir fixture under a git root and drive
        # it through the real reparse path.
        import os
        import shutil
        import tempfile
        root = tempfile.mkdtemp()
        try:
            os.mkdir(os.path.join(root, '.git'))
            os.makedirs(os.path.join(root, 'sub'))
            pa = os.path.join(root, 'top.md')
            pb = os.path.join(root, 'sub', 'nested.md')
            with open(pa, 'w', encoding='utf-8') as f:
                f.write(self.A_TEXT)
            with open(pb, 'w', encoding='utf-8') as f:
                f.write(self.B_TEXT)
            saved = os.getcwd()
            os.chdir(root)
            try:
                self._load_multi(pa, pb)
                titles = [c.title for c in self.r.get_children(None)]
            finally:
                os.chdir(saved)
            self.assertEqual(titles, ['top.md', os.path.join('sub', 'nested.md')])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_per_file_root_has_expected_headings(self):
        # Each per-file root carries its own headings (h1/h2) — the
        # exact tree shape from single-file ``_build_nodes``.
        self._load_multi(self.path_a, self.path_b)
        a_root, b_root = self.r.get_children(None)
        a_tags = [c.tag for c in a_root._children]
        # A has two h1s (A1, A1b).
        self.assertEqual(a_tags, ['h1', 'h1'])
        b_tags = [c.tag for c in b_root._children]
        # B has two h1s (B1, B1b).
        self.assertEqual(b_tags, ['h1', 'h1'])

    def test_no_synthetic_multi_root_in_by_id(self):
        # No ``(multi)`` / ``multi-root`` Item lives in the aggregate
        # index. Every Item is either a per-file root (id == path) or
        # a per-file content node (id == ``<path>#<line>``).
        self._load_multi(self.path_a, self.path_b)
        for item_id, item in self.r._BY_ID.items():
            self.assertNotEqual(
                getattr(item, 'kind', None), 'multi-root',
                f'unexpected multi-root item: {item_id}')

    def test_per_file_root_kind_is_root(self):
        # Per-file roots keep the ``'root'`` kind — same shape as the
        # pre-multi-file single root, so root-detection logic
        # (e.g. ``_run_source_command``) classifies them correctly.
        self._load_multi(self.path_a, self.path_b)
        for c in self.r.get_children(None):
            self.assertEqual(c.kind, 'root')

    def test_aggregate_by_id_contains_per_file_ids(self):
        self._load_multi(self.path_a, self.path_b)
        self.assertIn(('file', self.path_a), self.r._BY_ID)
        self.assertIn(('file', self.path_b), self.r._BY_ID)
        # And the per-file headings — at least one from each file.
        self.assertIn(('content', self.path_a, 0), self.r._BY_ID)
        self.assertIn(('content', self.path_b, 0), self.r._BY_ID)

    def test_single_file_top_level_has_one_row(self):
        # With one file in ``_INPUT_FILES``, the top-level row count
        # is 1 — that file's per-file root. No synthetic multi-root.
        self._load_multi(self.path_a)
        top = self.r.get_children(None)
        self.assertEqual([c.id for c in top], [('file', self.path_a)])

    def test_items_carry_file_path_back_reference(self):
        # Every Item built by ``_build_nodes`` carries ``file_path``
        # so ``get_preview`` can find its owning file's text.
        self._load_multi(self.path_a, self.path_b)
        a_root, b_root = self.r.get_children(None)
        self.assertEqual(a_root.file_path, self.path_a)
        self.assertEqual(b_root.file_path, self.path_b)
        # Per-file content items inherit their file's path.
        a_h1 = a_root._children[0]
        self.assertEqual(a_h1.file_path, self.path_a)


class TestAnchorMulti(_MultiCaseBase):
    """Initial-scope resolution across one or more files.

    Post-#566 the rules are:
      * Multi-file, no anchor → ``initial_scope is None`` (browser
        starts at the top-level list of files); no auto-expand.
      * Multi-file, first anchor on file X → resolve against X; no
        auto-expand.
      * Single-file, no anchor → ``initial_scope is None`` PLUS an
        auto-expand on the file row (so the file's headings are
        visible without scoping into the file — alt-up from a
        heading then lands on the file row, not an empty list).
      * Single-file, anchor → resolve against the file; no
        auto-expand (the anchor drill-in already shows the heading).
    """

    def _initial_scope(self, *files):
        """Re-run the argv-to-initial-scope flow without invoking ``main()``.

        Mirrors the logic in ``main()``: walk ``_INPUT_FILES``, pick
        the first anchored file, resolve via ``_resolve_anchor``.
        Returns just the ``initial_scope`` value — the auto-expand
        side-effect is tested separately via ``_run_main``.
        """
        self.r._INPUT_FILES = list(files)
        self.r._reparse()
        first_anchor = ''
        first_anchor_path = None
        for path, anchor in files:
            if anchor and first_anchor_path is None:
                first_anchor = anchor
                first_anchor_path = path
        if first_anchor_path is not None:
            return self.r._resolve_anchor(first_anchor, first_anchor_path)
        # Single-file no-anchor and multi-file no-anchor both leave
        # ``initial_scope`` at ``None``; the single-file case is
        # handled instead via the auto-expand call asserted in
        # ``_run_main`` below.
        return None

    def _run_main(self, argv):
        """Drive ``main()`` through Browser construction and return ``_BROWSER``.

        The stubbed ``Browser`` from ``_stub_browse_tui`` has no
        ``run`` method, so ``main()`` raises ``AttributeError`` just
        past the auto-expand call. By that point we've captured
        ``initial_scope`` (on the Browser's BrowserConfig) and any
        ``expand(...)`` invocations.
        """
        import contextlib
        import io
        self.r.sys.argv[:] = argv
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                self.r.main()
            except (SystemExit, AttributeError):
                pass
        return self.r._BROWSER

    def test_multi_file_no_anchor_returns_none(self):
        # Two files, neither anchored → initial scope is ``None``
        # (browser shows the top-level list of files).
        scope = self._initial_scope(
            (self.path_a, ''), (self.path_b, ''))
        self.assertIsNone(scope)

    def test_single_file_no_anchor_returns_none(self):
        # One file, no anchor → ``initial_scope`` is ``None`` (post
        # #566). The "show the file's headings immediately" visual
        # is delivered via ``b.expand(file_root.id)`` instead — see
        # ``test_single_file_no_anchor_auto_expands_file_row``.
        scope = self._initial_scope((self.path_a, ''))
        self.assertIsNone(scope)

    def test_anchor_on_second_file_resolves_in_that_file(self):
        # File A unanchored, file B carries ``#B2`` → scope drills
        # into B's H2 heading.
        scope = self._initial_scope(
            (self.path_a, ''), (self.path_b, 'B2'))
        self.assertEqual(scope, ('content', self.path_b, 1))

    def test_anchor_on_first_file_resolves_in_that_file(self):
        # File A carries ``#A2``, file B unanchored → scope drills
        # into A's H2 heading. Confirms anchor-on-first-file works
        # symmetrically with anchor-on-second.
        scope = self._initial_scope(
            (self.path_a, 'A2'), (self.path_b, ''))
        self.assertEqual(scope, ('content', self.path_a, 1))

    def test_both_anchored_first_wins(self):
        # Both files anchored → the FIRST one in argv order wins
        # (matches the ticket's "first anchored file" rule). B's
        # anchor is recorded in ``_INPUT_FILES`` but ignored here.
        scope = self._initial_scope(
            (self.path_a, 'A2'), (self.path_b, 'B2'))
        self.assertEqual(scope, ('content', self.path_a, 1))

    def test_digit_anchor_resolves_against_named_file(self):
        # Digit anchors are 0-based line numbers — resolution is
        # per-file. ``#0`` on B should hit B's first heading, not A's.
        scope = self._initial_scope(
            (self.path_a, ''), (self.path_b, '0'))
        self.assertEqual(scope, ('content', self.path_b, 0))

    def test_single_file_anchor_resolves(self):
        # Single file with anchor → resolves via ``_resolve_anchor``
        # against that file's per-file root.
        scope = self._initial_scope((self.path_a, 'A2'))
        self.assertEqual(scope, ('content', self.path_a, 1))

    def test_single_file_no_anchor_auto_expands_file_row(self):
        # Single-file no-anchor: ``initial_scope`` is ``None`` AND
        # ``main()`` calls ``b.expand(file_root.id)`` so the file's
        # headings are visible from startup without scoping into
        # the file (ticket #566).
        b = self._run_main(['browse-md', self.path_a])
        self.assertIsNone(b.config.initial_scope)
        self.assertEqual(len(b.expand_calls), 1)
        expanded_id, _, _ = b.expand_calls[0]
        # The file's per-file-root id is ``('file', abspath)``.
        self.assertEqual(expanded_id, ('file', self.path_a))

    def test_single_file_with_anchor_does_not_auto_expand(self):
        # Single-file WITH anchor: ``initial_scope`` resolves to the
        # anchored heading and no auto-expand is issued (the anchor
        # drill-in already reveals the heading).
        b = self._run_main(['browse-md', f'{self.path_a}#A2'])
        self.assertEqual(b.config.initial_scope, ('content', self.path_a, 1))
        self.assertEqual(b.expand_calls, [])

    def test_multi_file_no_anchor_does_not_auto_expand(self):
        # Multi-file no-anchor: ``initial_scope`` is ``None`` and no
        # auto-expand — the user picks a file from the top-level
        # list.
        b = self._run_main(['browse-md', self.path_a, self.path_b])
        self.assertIsNone(b.config.initial_scope)
        self.assertEqual(b.expand_calls, [])


class TestGetPreviewMulti(_MultiCaseBase):
    """``get_preview`` dispatch across the multi-file id space."""

    def test_preview_at_none_is_empty(self):
        # Files ARE the top-level entries; the framework asking for
        # ``get_preview(None)`` (no row selected) returns the empty
        # string — there is no aggregate preview to show.
        self._load_multi(self.path_a, self.path_b)
        self.r._MD_COLOR = False
        self.assertEqual(self.r.get_preview(None), '')

    def test_per_file_root_preview_is_full_file_text(self):
        self._load_multi(self.path_a, self.path_b)
        self.r._MD_COLOR = False
        self.assertEqual(self.r.get_preview(('file', self.path_a)), self.A_TEXT)
        self.assertEqual(self.r.get_preview(('file', self.path_b)), self.B_TEXT)

    def test_per_file_heading_preview_is_file_slice(self):
        # Heading id is ``('content', path, line)``; preview is the byte-slice
        # of that file's text. Confirms ``get_preview`` routes to the
        # right file via the ``_classify_id('content', ...)`` branch.
        self._load_multi(self.path_a, self.path_b)
        self.r._MD_COLOR = False
        # Slice for ``# A1`` (line 0) — runs to ``# A1b`` at line 3.
        a_h1_id = ('content', self.path_a, 0)
        out = self.r.get_preview(a_h1_id)
        self.assertTrue(out.startswith('# A1\n'))
        self.assertIn('body of A2', out)
        # And the slice doesn't bleed into file B.
        self.assertNotIn('# B1', out)

    def test_unknown_id_returns_empty(self):
        self._load_multi(self.path_a, self.path_b)
        self.r._MD_COLOR = False
        # A file id for a path not loaded, and a content id for a missing
        # file, both preview empty (no ``_FILES`` entry → shim miss).
        self.assertEqual(self.r.get_preview(('file', '/no/such/path.md')), '')
        self.assertEqual(
            self.r.get_preview(('content', '/no/such/path.md', 0)), '')


class TestRunSourceCommandMulti(_MultiCaseBase):
    """``_run_source_command`` semantics on multi-file selections.

    Post-#559: no synthetic multi-root, so no "open first file"
    short-circuit. Per-file root rows open that file directly;
    non-root selections — including ones spanning multiple files —
    are honoured by grouping ranges per file and concatenating the
    per-file slices into one temp file with a header separator.
    """

    def setUp(self):
        super().setUp()
        # Snapshot env so per-test PAGER/EDITOR overrides don't leak.
        import os
        self._env_saved = {k: os.environ.get(k) for k in ('PAGER', 'EDITOR')}
        os.environ.pop('PAGER', None)
        os.environ.pop('EDITOR', None)

    def tearDown(self):
        import os
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_per_file_root_opens_that_file(self):
        # Per-file root target → opens that specific file.
        self._load_multi(self.path_a, self.path_b)
        b_root = self.r._FILES[self.path_b].file_root
        ctx = _SrcCmdCtx(targets=[b_root])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(ctx.calls, [['less', '-R', self.path_b]])

    def test_two_roots_combines_with_headers(self):
        # #572: two per-file roots selected → no longer root-wins.
        # Each root expands to its whole-file range; output is a
        # tempfile containing both files concatenated with the
        # ``===== <basename> =====`` header before EACH group
        # (including the first).
        import os
        self._load_multi(self.path_a, self.path_b)
        a_root = self.r._FILES[self.path_a].file_root
        b_root = self.r._FILES[self.path_b].file_root
        # Selection order reversed — argv order is what matters.
        ctx = _SrcCmdCtx(targets=[b_root, a_root])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(len(ctx.calls), 1)
        argv = ctx.calls[0]
        self.assertEqual(argv[:2], ['less', '-R'])
        self.assertTrue(argv[2].endswith('.md'))
        out = ctx.last_tmp_contents
        # Headers before EACH group, including the first.
        a_sep = f'===== {os.path.basename(self.path_a)} ====='
        b_sep = f'===== {os.path.basename(self.path_b)} ====='
        self.assertIn(a_sep, out)
        self.assertIn(b_sep, out)
        # Argv order: A's group precedes B's.
        self.assertLess(out.find(a_sep), out.find(b_sep))
        # Each group contains the whole file body.
        self.assertIn(self.A_TEXT, out)
        self.assertIn(self.B_TEXT, out)
        # Header before the first group → output starts with A's sep.
        self.assertTrue(out.startswith(a_sep + '\n'))

    def test_same_file_non_root_targets_merge_in_one_tempfile(self):
        # Two non-root targets from the SAME file → one tempfile, the
        # merged byte ranges in file order.
        self._load_multi(self.path_a, self.path_b)
        a_h1 = self.r._BY_ID[('content', self.path_a, 0)]
        a_h1b = self.r._BY_ID[('content', self.path_a, 3)]
        ctx = _SrcCmdCtx(targets=[a_h1b, a_h1])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(len(ctx.calls), 1)
        # File-order: A1 slice first, then A1b slice. Together they
        # cover the whole A file (A1 spans to A1b, A1b spans to EOF).
        self.assertEqual(ctx.last_tmp_contents, self.A_TEXT)

    def test_cross_file_groups_by_file_in_argv_order(self):
        # Targets span both files → temp file contains BOTH files'
        # slices, grouped per file with a ``===== <basename> =====``
        # header before EACH group (including the first, post-#572).
        # Files appear in argv order (A before B) regardless of
        # selection order.
        import os
        self._load_multi(self.path_a, self.path_b)
        a_h1 = self.r._BY_ID[('content', self.path_a, 0)]
        b_h1 = self.r._BY_ID[('content', self.path_b, 0)]
        ctx = _SrcCmdCtx(targets=[b_h1, a_h1])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(len(ctx.calls), 1)
        out = ctx.last_tmp_contents
        # Both files' headings are present.
        self.assertIn('# A1', out)
        self.assertIn('# B1', out)
        # Argv order: A's slice precedes B's.
        a_idx = out.find('# A1')
        b_idx = out.find('# B1')
        self.assertLess(a_idx, b_idx)
        # #572: Header appears before EACH group, including the first.
        a_sep = f'===== {os.path.basename(self.path_a)} ====='
        b_sep = f'===== {os.path.basename(self.path_b)} ====='
        self.assertIn(a_sep, out)
        self.assertIn(b_sep, out)
        # A's header must come before A's body, and B's after A's.
        self.assertLess(out.find(a_sep), a_idx)
        self.assertLess(a_idx, out.find(b_sep))
        # Output starts with A's header (first group gets one now).
        self.assertTrue(out.startswith(a_sep + '\n'))

    def test_cross_file_argv_order_independent_of_selection_order(self):
        # Even when B's target is listed FIRST in the selection, the
        # groups in the temp file appear in argv order (A then B).
        self._load_multi(self.path_a, self.path_b)
        a_h1 = self.r._BY_ID[('content', self.path_a, 0)]
        b_h1 = self.r._BY_ID[('content', self.path_b, 0)]
        ctx = _SrcCmdCtx(targets=[b_h1, a_h1])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        out = ctx.last_tmp_contents
        # A's content comes first.
        self.assertLess(out.find('# A1'), out.find('# B1'))

    def test_root_plus_content_same_file_combines(self):
        # #572: file-root A space-marked + heading from file A → temp
        # file with the whole-file range absorbing the heading's
        # sub-range. Single-file output → NO header.
        import os
        self._load_multi(self.path_a, self.path_b)
        a_root = self.r._FILES[self.path_a].file_root
        a_h1 = self.r._BY_ID[('content', self.path_a, 0)]
        ctx = _SrcCmdCtx(targets=[a_root, a_h1])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(len(ctx.calls), 1)
        argv = ctx.calls[0]
        self.assertTrue(argv[-1].endswith('.md'))
        # Single-file group → no ``=====`` header. Whole-file range
        # absorbs the heading's sub-range → output is the whole A
        # body.
        out = ctx.last_tmp_contents
        a_sep = f'===== {os.path.basename(self.path_a)} ====='
        self.assertNotIn(a_sep, out)
        self.assertEqual(out, self.A_TEXT)

    def test_root_A_plus_content_B_combines_with_headers(self):
        # #572: file-root A + heading from file B → temp file with
        # two groups; BOTH groups get a ``===== <basename> =====``
        # header (including the first). A's group is the whole-file
        # range; B's group is the heading's slice.
        import os
        self._load_multi(self.path_a, self.path_b)
        a_root = self.r._FILES[self.path_a].file_root
        b_h1 = self.r._BY_ID[('content', self.path_b, 0)]
        ctx = _SrcCmdCtx(targets=[a_root, b_h1])
        self.r._run_source_command(ctx, 'PAGER', 'less -R')
        self.assertEqual(len(ctx.calls), 1)
        argv = ctx.calls[0]
        self.assertTrue(argv[-1].endswith('.md'))
        out = ctx.last_tmp_contents
        a_sep = f'===== {os.path.basename(self.path_a)} ====='
        b_sep = f'===== {os.path.basename(self.path_b)} ====='
        # Header before EACH group, including the first.
        self.assertIn(a_sep, out)
        self.assertIn(b_sep, out)
        # Output starts with A's header.
        self.assertTrue(out.startswith(a_sep + '\n'))
        # A's group contains the whole A body; B's group contains B1.
        self.assertIn(self.A_TEXT, out)
        self.assertIn('# B1', out)
        # Argv order: A's header precedes B's.
        self.assertLess(out.find(a_sep), out.find(b_sep))


class TestReloadMulti(_MultiCaseBase):
    """``_reparse`` re-slurps every file in ``_INPUT_FILES``."""

    def _write(self, path, text):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_both_files_reparsed_after_disk_mutation(self):
        # Initial parse: both fixtures' headings are in the aggregate
        # index. Mutate both, reload via the public ``get_children``
        # Ctrl-R contract, confirm both files' new headings appear.
        self._load_multi(self.path_a, self.path_b)
        # Sanity: pre-mutation state.
        self.assertIn(('content', self.path_a, 0), self.r._BY_ID)
        self.assertIn(('content', self.path_b, 0), self.r._BY_ID)
        # Overwrite both files with new content.
        self._write(self.path_a, '# NewA\n')
        self._write(self.path_b, '# NewB\n## NewB2\n')
        # Trigger reparse via the public Ctrl-R contract.
        self.r.get_children(None, reload=True)
        # Both files' new headings landed in the aggregate index.
        a_titles = [
            it.title for it in self.r._FILES[self.path_a].by_id.values()
            if it.kind == 'heading'
        ]
        b_titles = sorted(
            it.title for it in self.r._FILES[self.path_b].by_id.values()
            if it.kind == 'heading'
        )
        self.assertEqual(a_titles, ['NewA'])
        self.assertEqual(b_titles, ['NewB', 'NewB2'])

    def test_reload_at_none_reparses(self):
        # Post-#559: ``BrowserConfig(root_id=None)`` means Ctrl-R
        # always calls ``get_children(None, reload=True)``. Reload
        # should re-slurp every file.
        self._load_multi(self.path_a, self.path_b)
        self._write(self.path_a, '# Mutated\n')
        self.r.get_children(None, reload=True)
        a_titles = [
            it.title for it in self.r._FILES[self.path_a].by_id.values()
            if it.kind == 'heading'
        ]
        self.assertEqual(a_titles, ['Mutated'])

    def test_per_file_root_input_files_preserved(self):
        # ``_reparse`` doesn't mutate ``_INPUT_FILES`` — Ctrl-R needs
        # to find the same file list on every call.
        self._load_multi(self.path_a, self.path_b)
        before = list(self.r._INPUT_FILES)
        self.r.get_children(None, reload=True)
        self.assertEqual(self.r._INPUT_FILES, before)


class TestClassifyId(_MultiCaseBase):
    """``_classify_id`` — single source of truth for id shape dispatch.

    Post-#559: three classifications — ``'file-root'``, ``'content'``,
    ``'unknown'``. No synthetic multi-root case.
    """

    def test_per_file_root_id(self):
        self._load_multi(self.path_a, self.path_b)
        self.assertEqual(
            self.r._classify_id(('file', self.path_a)),
            ('file-root', self.path_a))
        self.assertEqual(
            self.r._classify_id(('file', self.path_b)),
            ('file-root', self.path_b))

    def test_content_id(self):
        self._load_multi(self.path_a, self.path_b)
        self.assertEqual(
            self.r._classify_id(('content', self.path_a, 0)),
            ('content', self.path_a))
        self.assertEqual(
            self.r._classify_id(('content', self.path_b, 1)),
            ('content', self.path_b))

    def test_unknown_id(self):
        self._load_multi(self.path_a, self.path_b)
        # Tags ``_classify_id`` doesn't own (the markdown-ref tuples and any
        # stray shape) classify as unknown — ``get_preview`` routes ``md`` /
        # ``refs`` separately before reaching here.
        self.assertEqual(
            self.r._classify_id(('md', ('file', self.path_a), (), None)),
            ('unknown', None))
        self.assertEqual(
            self.r._classify_id(('refs', ('file', self.path_a), ())),
            ('unknown', None))
        self.assertEqual(
            self.r._classify_id(('mystery', 'garbage')), ('unknown', None))


class _SingleFileBase(unittest.TestCase):
    """Write one on-disk markdown fixture and reparse it through the recipe.

    Snapshots the module globals ``_reparse`` scribbles on and restores
    them in ``tearDown`` so a failing assert doesn't bleed into a
    sibling suite — same discipline as ``_MultiCaseBase``.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._saved = {
            '_FILES': dict(self.r._FILES),
            '_INPUT_FILES': list(self.r._INPUT_FILES),
            '_BY_ID': dict(self.r._BY_ID),
            '_FILE_TEXT': self.r._FILE_TEXT,
            '_BY_LINE': dict(self.r._BY_LINE),
            '_LINES_SORTED': list(self.r._LINES_SORTED),
            '_ROOT_PATH': self.r._ROOT_PATH,
            '_ANCHOR': self.r._ANCHOR,
        }
        self._paths = []

    def tearDown(self):
        import os
        for k, v in self._saved.items():
            setattr(self.r, k, v)
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _load(self, text):
        """Write ``text`` to a temp .md, reparse, return its abs path."""
        import tempfile
        f = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8')
        f.write(text)
        f.close()
        self._paths.append(f.name)
        self.r._INPUT_FILES = [(f.name, '')]
        self.r._reparse()
        return f.name


class TestLoneHeadingChildId(_SingleFileBase):
    """``_lone_heading_child_id`` — file-root sole-heading-child detection."""

    def test_single_h1_child_returns_its_id(self):
        # ``# Title`` wraps a single ``## Section`` — the file root has
        # exactly one child (the h1), so the helper returns the h1 id.
        path = self._load('# Title\n## Section\nbody\n')
        self.assertEqual(
            self.r._lone_heading_child_id(('file', path)),
            ('content', path, 0))

    def test_single_h2_child_returns_its_id(self):
        # Any heading level qualifies, not just h1 — a file whose sole
        # child is an ``## h2`` still gets the cascade.
        path = self._load('## Only\nbody\n')
        self.assertEqual(
            self.r._lone_heading_child_id(('file', path)),
            ('content', path, 0))

    def test_two_top_level_headings_returns_none(self):
        path = self._load('# A\n# B\n')
        self.assertIsNone(self.r._lone_heading_child_id(('file', path)))

    def test_single_list_item_child_returns_none(self):
        # A lone non-heading child (list item) does not qualify.
        path = self._load('- only item\n')
        root = self.r._FILES[path].file_root
        self.assertEqual(len(root._children), 1)
        self.assertEqual(root._children[0].kind, 'list-item')
        self.assertIsNone(self.r._lone_heading_child_id(('file', path)))

    def test_no_children_returns_none(self):
        path = self._load('plain body, no headings\n')
        self.assertIsNone(self.r._lone_heading_child_id(('file', path)))

    def test_unknown_id_returns_none(self):
        self._load('# Title\n## Section\n')
        self.assertIsNone(
            self.r._lone_heading_child_id(('file', '/no/such.md')))


class _CascadeCtx:
    """Recorder for ``ctx`` in ``_on_expand`` unit tests.

    The ``on_expand`` hook only ever calls ``ctx.expand(cascade_id)``;
    this stub records those calls so the per-id cascade decision can be
    asserted in isolation, without standing up a real Browser. The
    full-stack verification (that the recursive fire actually lands the
    heading in ``state.expanded``) lives in ``TestOnExpandCascadeLive``.
    """

    def __init__(self):
        self.expand_calls = []

    def expand(self, id, on_complete=None, autoscroll=False):
        self.expand_calls.append((id, autoscroll))


class TestOnExpand(_SingleFileBase):
    """``_on_expand(ctx, ids)`` — the lone-heading cascade hook.

    Unit-level coverage of the per-id decision. The hook replaced the
    old ``_action_expand`` right-arrow override: the expand itself (and
    the already-expanded step-into-first-child gesture) is now the
    framework default ``_nav_right``; only the lone-heading auto-expand
    is the recipe's, and it rides ``on_expand`` so it fires for every
    expansion source (keyboard, programmatic, startup).
    """

    def test_lone_heading_id_cascades_to_child(self):
        # Expanding a file whose sole child is a heading expands that
        # heading too. The cascade expand uses the default autoscroll
        # (False) so it doesn't park a scroll goal.
        path = self._load('# Title\n## Section\nbody\n')
        ctx = _CascadeCtx()
        self.r._on_expand(ctx, [('file', path)])
        self.assertEqual(ctx.expand_calls, [(('content', path, 0), False)])

    def test_two_top_level_headings_no_cascade(self):
        # A file with two top-level headings has no lone-heading child.
        path = self._load('# A\n# B\n')
        ctx = _CascadeCtx()
        self.r._on_expand(ctx, [('file', path)])
        self.assertEqual(ctx.expand_calls, [])

    def test_non_file_id_no_cascade(self):
        # An id that just expanded but is a heading (not a file root)
        # never qualifies, so nothing cascades.
        path = self._load('# Title\n## Section\nbody\n')
        ctx = _CascadeCtx()
        self.r._on_expand(ctx, [('content', path, 0)])
        self.assertEqual(ctx.expand_calls, [])

    def test_cascade_id_does_not_re_cascade(self):
        # The follow-on expand of the lone heading re-fires on_expand
        # with that heading's id; its own child is a section, not a
        # lone-heading file, so the cascade terminates (no further
        # expand). This is what bounds the recursion.
        path = self._load('# Title\n## Section\nbody\n')
        ctx = _CascadeCtx()
        self.r._on_expand(ctx, [('content', path, 0)])  # the cascade target
        self.assertEqual(ctx.expand_calls, [])

    def test_batch_cascades_each_qualifying_id(self):
        # ``ids`` is a list (a multi-node expand burst). Each qualifying
        # file root in the batch cascades independently. Load both files
        # in one reparse so ``_FILES`` holds both roots at once.
        import tempfile
        paths = []
        for body in ('# One\n## S1\n', '# Two\n## S2\n'):
            f = tempfile.NamedTemporaryFile(
                'w', suffix='.md', delete=False, encoding='utf-8')
            f.write(body)
            f.close()
            self._paths.append(f.name)
            paths.append(f.name)
        self.r._INPUT_FILES = [(p, '') for p in paths]
        self.r._reparse()
        p1, p2 = paths
        ctx = _CascadeCtx()
        self.r._on_expand(ctx, [('file', p1), ('file', p2)])
        self.assertEqual(
            set(ctx.expand_calls),
            {(('content', p1, 0), False), (('content', p2, 0), False)})


class TestStartupAutoExpand(unittest.TestCase):
    """``main()`` posts the single-file startup auto-expand.

    ``main()`` issues exactly one ``b.expand(file_root)`` for the
    single-file no-anchor case. The lone-heading cascade is no longer
    duplicated here — it rides the ``on_expand`` hook, which the real
    Browser fires for this very expand. (The test stub does not run
    hooks, so only the file-root expand is recorded here; the cascade's
    end result is verified against a real Browser in
    ``TestOnExpandCascadeLive``.)
    """

    def setUp(self):
        self.r = _load_recipe()

    def _write(self, text):
        import os
        import tempfile
        f = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8')
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def _run_main(self, path):
        import contextlib
        import io
        self.r.sys.argv[:] = ['browse-md', path]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                self.r.main()
            except (SystemExit, AttributeError):
                pass
        return self.r._BROWSER

    def test_single_file_posts_file_root_expand(self):
        path = self._write('# Title\n## Section\nbody\n')
        b = self._run_main(path)
        ids = [c[0] for c in b.expand_calls]
        # Just the file-root expand; the cascade is the hook's job now.
        self.assertEqual(ids, [('file', path)])
        # And on_expand is wired into the config so the hook can run.
        self.assertIs(b.config.on_expand, self.r._on_expand)

    def test_multi_heading_file_posts_file_root_expand(self):
        path = self._write('# A\n# B\n')
        b = self._run_main(path)
        ids = [c[0] for c in b.expand_calls]
        self.assertEqual(ids, [('file', path)])


def _load_framework():
    """Load + wire the real ``src-tui`` modules for a live Browser.

    Mirrors the cross-module name injection in
    ``test/unit/test_lifecycle_hooks.py`` — the production single-file
    build resolves these names by concatenation, so a per-file load has
    to staple them together by hand. Returns the loaded ``term`` /
    ``data`` / ``state`` / ``render`` / ``context`` / ``actions``
    modules. Loaded lazily (inside the live test) so the rest of this
    file, which deliberately stubs ``browse_tui``, is untouched.
    """
    from test.unit._loader import load
    term = load('_md_live_term', '020-terminal.py')
    data = load('_md_live_data', '030-data.py')
    state = load('_md_live_state', '040-state.py')
    render = load('_md_live_render', '050-render.py')
    context = load('_md_live_context', '060-context.py')
    actions = load('_md_live_actions', '070-actions.py')

    state.Item = data.Item
    state.to_item = data.to_item
    state.notify_wake = term.notify_wake
    state.Context = context.Context          # hooks build a Context
    render.Item = data.Item
    render.PreviewRender = data.PreviewRender
    render.VisibleEntry = state.VisibleEntry
    context.visible_items = state.visible_items
    # Names the keyboard handlers (dispatch_key / _nav_right / …) resolve
    # at run-time by concatenation in the production build.
    actions.write = term.write
    actions.visible_items = state.visible_items
    actions.mark_visible_dirty = state.mark_visible_dirty
    actions.current_scope = state.current_scope
    actions.mark_cursor_changed = state.mark_cursor_changed
    actions._resolve_landing = state._resolve_landing
    actions.Mode = state.Mode
    actions.scope_into = state.scope_into
    actions.scope_out = state.scope_out
    return term, data, state, render, context, actions


class TestOnExpandCascadeLive(unittest.TestCase):
    """End-to-end: the ``on_expand`` cascade against a real Browser.

    Builds an actual framework ``Browser`` wired with the recipe's
    ``get_children`` and ``on_expand=_on_expand``, then drives an expand
    headlessly (post-queue drain + the ``_fire_expand_collapse_if_pending``
    settle pass, the way ``test_lifecycle_hooks`` does). Verifies the
    cascade's whole point: expanding a file-root whose sole child is a
    heading lands BOTH the file-root and that heading in
    ``state.expanded`` — the duplicated startup logic and the right-arrow
    override are gone, yet the behaviour survives via the single hook.
    """

    def setUp(self):
        self.r = _load_recipe()
        (self._term, self._data, self._fwstate, self._render,
         self._fwcontext, self._fwactions) = _load_framework()
        self.Browser = self._fwstate.Browser
        self.BrowserConfig = self._fwstate.BrowserConfig
        self.Context = self._fwcontext.Context

    def _load_md(self, text):
        """Write ``text`` to a temp .md, reparse the recipe, return path."""
        import os
        import tempfile
        f = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8')
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.r._INPUT_FILES = [(f.name, '')]
        self.r._reparse()
        return f.name

    def _browser_for(self, path):
        """Real Browser over the recipe tree with the cascade hook wired.

        Children are pre-seeded into the Browser's cache (from the
        recipe's own ``get_children``) so every expand resolves
        synchronously without a worker — the cascade then completes
        purely through the drain / fire pumping below.
        """
        b = self.Browser(self.BrowserConfig(
            _headless=True,
            root_id=None,
            get_children=self.r.get_children,
            on_expand=self.r._on_expand,
        ))
        # Seed the framework cache from the recipe tree so expansion is
        # synchronous (top-level probe + every node with children).
        s = b._state
        s._children[None] = list(self.r.get_children(None))
        for node_id, item in self.r._BY_ID.items():
            if getattr(item, 'has_children', False):
                s._children[node_id] = list(self.r.get_children(node_id))
        b.drain_main_queue()
        return b

    def _pump(self, b):
        """Drain + fire until the expanded set stops growing.

        The cascade needs two cycles: drain N fires ``on_expand`` for the
        file-root, whose handler posts ``ctx.expand(heading)``; drain N+1
        applies that and fires ``on_expand`` for the heading (which does
        not cascade further). Loop until a cycle adds nothing.
        """
        for _ in range(8):
            before = set(b._state.expanded)
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            if set(b._state.expanded) == before:
                break

    def test_expanding_file_root_cascades_to_lone_heading(self):
        path = self._load_md('# Title\n## Section\nbody\n')
        b = self._browser_for(path)
        try:
            self.assertEqual(b._state.expanded, set())   # clean baseline
            b.expand(('file', path))                     # user/programmatic
            self._pump(b)
            # BOTH the file-root AND its lone heading end up expanded.
            self.assertIn(('file', path), b._state.expanded)
            self.assertIn(('content', path, 0), b._state.expanded)
            self.assertEqual(b._state.expanded,
                             {('file', path), ('content', path, 0)})
        finally:
            b.stop_workers()

    def test_startup_expand_before_run_cascades(self):
        # The startup path: ``b.expand(file_root)`` issued before the
        # loop runs is seen by the first drain (``_last_expanded`` starts
        # empty) and the cascade fires from there — the single code path
        # that replaced the duplicated startup block.
        path = self._load_md('# Title\n## Section\nbody\n')
        b = self._browser_for(path)
        try:
            self.assertEqual(b._last_expanded, set())
            b.expand(('file', path))
            self._pump(b)
            self.assertEqual(b._state.expanded,
                             {('file', path), ('content', path, 0)})
        finally:
            b.stop_workers()

    def test_two_headings_no_cascade(self):
        # A file with two top-level headings: the file-root expands, but
        # there is no lone heading to cascade into.
        path = self._load_md('# A\n# B\n')
        b = self._browser_for(path)
        try:
            b.expand(('file', path))
            self._pump(b)
            self.assertEqual(b._state.expanded, {('file', path)})
        finally:
            b.stop_workers()

    def test_already_expanded_step_into_first_child(self):
        # The step-into-first-child gesture is the framework default
        # ``_nav_right`` now (the recipe no longer overrides ``→``).
        # Re-pressing ``→`` on an already-expanded row moves the cursor
        # onto the first child row and fires NO further expand.
        actions = self._fwactions
        path = self._load_md('# Title\n## Section\nbody\n')
        b = self._browser_for(path)
        try:
            # Start from the fully-cascaded state, cursor on the file row.
            b.expand(('file', path))
            self._pump(b)
            b._state.cursor = 0
            self._fwstate.mark_cursor_changed(b)
            b.drain_main_queue()
            expanded_before = set(b._state.expanded)
            ctx = self.Context(b)
            self.assertTrue(actions.dispatch_key(b, ctx, 'right'))
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            # Cursor advanced to the first child; expanded set unchanged.
            self.assertEqual(b._state.cursor, 1)
            self.assertEqual(b._state.expanded, expanded_before)
        finally:
            b.stop_workers()


class TestMdRefFollowing(unittest.TestCase):
    """Markdown reference-following — ``('md', …)`` ref children under a
    ``[links]`` References umbrella (tickets #664, #698, #703).

    A markdown FILE references other ``.md`` files; the EXISTING referenced files
    are grouped under ONE ``[links]`` References umbrella child of the document
    (per-file root or referenced-file doc), each an expandable ``[md]`` node
    drilling into ITS headings + ITS refs recursively (mirrors browse-claude's
    #702). Built on an on-disk fixture driven through ``_reparse`` (the real
    ``main()`` startup path) since reference resolution and ``md_doc.get_doc``
    read from disk.

    Fixtures (in a private temp dir, so the git-root walk-up finds no ``.git``
    and ``project_root`` falls back to that dir — labels are bare basenames):
      * ``A.md`` references ``B.md`` (existing) and ``C.md`` (non-existent),
        and carries its own headings.
      * ``B.md`` references ``A.md`` (a CYCLE — must expand in place, never loop)
        and carries its own headings.
    """

    def setUp(self):
        import os
        import tempfile
        # Fresh module per test — module-level state (``_FILES`` / ``_BY_ID`` /
        # ``_INPUT_FILES`` / the md_doc parse cache) mustn't bleed across tests.
        self.r = _load_recipe(include_lists=False)
        # Drop any cached referenced docs left over from a sibling test (the
        # md_doc cache is process-wide, not per recipe-module instance).
        self.r.md_doc.clear_cache()
        self.r._MD_COLOR = False
        self.r._BROWSER = None
        self.dir = tempfile.mkdtemp()
        self.A = os.path.join(self.dir, 'A.md')
        self.B = os.path.join(self.dir, 'B.md')
        # ``# A title`` owns the whole file (no sibling h1), so a heading-slice
        # preview against it would equal the whole body; ``B.md`` has two h1s so
        # its first heading's section is a strict sub-slice (preview test below).
        self._write(self.A,
                    '# A title\n'
                    'See B.md and also a missing C.md here.\n'
                    '## A sub\n'
                    'alpha body\n')
        self._write(self.B,
                    '# B one\n'
                    'back to A.md again\n'
                    '## B sub\n'
                    'beta body\n'
                    '# B two\n'
                    'gamma body\n')
        self.r._INPUT_FILES = [(self.A, '')]
        self.r._ROOT_PATH = self.A
        self.r._reparse()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        # Leave no cached docs behind for the next test class.
        self.r.md_doc.clear_cache()

    def _write(self, path, text):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def _umbrella(self, kids):
        # The single ``[links]`` References umbrella among a row's children.
        umbrellas = [k for k in kids if k.tag == 'links']
        self.assertEqual(len(umbrellas), 1,
                         'expected exactly one References umbrella')
        return umbrellas[0]

    def _refs(self, parent_id):
        # The ``[md]`` ref file-docs grouped under ``parent_id``'s umbrella —
        # expanding the parent, finding its umbrella, then expanding THAT.
        # A bare path is wrapped into its per-file root id ``('file', path)``
        # for convenience (the md-ref tuple ids are passed through as-is).
        if isinstance(parent_id, str):
            parent_id = ('file', parent_id)
        umbrella = self._umbrella(self.r.get_children(parent_id))
        return self.r.get_children(umbrella.id)

    # --- per-file root: References umbrella -------------------------------

    def test_root_groups_existing_ref_under_umbrella(self):
        # A's root children = its h1 heading + one ``[links]`` References
        # umbrella (refs are NO LONGER direct children). Expanding the umbrella
        # yields one ``[md]`` node for B.md; C.md is non-existent (existing-only).
        kids = self.r.get_children(('file', self.A))
        # A heading row is present, refs grouped after it under the umbrella.
        self.assertEqual(kids[0].tag, 'h1')
        self.assertEqual(kids[0].title, 'A title')
        # No bare ``[md]`` ref nodes hang directly off the root anymore.
        self.assertFalse(any(k.tag == 'md' for k in kids))
        umbrella = self._umbrella(kids)
        # The umbrella sits after the heading rows.
        self.assertIs(kids[-1], umbrella)
        self.assertEqual(umbrella.title, 'References')
        self.assertEqual(umbrella.kind, 'md-refs')
        self.assertFalse(umbrella.boundary)
        self.assertTrue(umbrella.has_children)
        # Its id = ``('refs', anchor, ())`` — the primary file's refs, the
        # anchor being the per-file root ``('file', A)`` (empty chain).
        self.assertEqual(umbrella.id, ('refs', ('file', self.A), ()))
        # Expanding it yields the ref file-docs.
        refs = self.r.get_children(umbrella.id)
        self.assertEqual([k.title for k in refs], ['B.md'])

    def test_nonexistent_ref_absent(self):
        # C.md never resolves, so no ``[md]`` child names it anywhere — neither
        # at the root nor under the umbrella.
        refs = self._refs(self.A)
        self.assertNotIn('C.md', [k.title for k in refs])

    def test_ref_node_is_boundary(self):
        # The referenced-file node under the umbrella carries the boundary flag.
        bnode = self._refs(self.A)[0]
        self.assertTrue(getattr(bnode, 'boundary', False))

    def test_ref_label_is_relative(self):
        # Label is project_root-relative (here: bare basename, no leading
        # slash and no temp-dir prefix).
        bnode = self._refs(self.A)[0]
        self.assertEqual(bnode.title, 'B.md')
        self.assertFalse(bnode.title.startswith('/'))
        self.assertNotIn(self.dir, bnode.title)

    def test_ref_children_deduped_and_sorted(self):
        # A file that references the SAME target twice (different tokens that
        # resolve to one path) yields ONE child; multiple distinct refs sort
        # by label. Rewrite A to reference B.md twice plus a second file.
        import os
        D = os.path.join(self.dir, 'D.md')
        self._write(D, '# D\n')
        self._write(self.A,
                    '# A\n'
                    'first B.md, then ./B.md again, and D.md\n')
        self.r.md_doc.clear_cache()
        self.r._reparse()
        refs = self._refs(self.A)
        titles = [k.title for k in refs]
        # B.md deduped to one; D.md present; sorted by label.
        self.assertEqual(titles, ['B.md', 'D.md'])

    def test_ref_node_id_is_md_tuple(self):
        # The ref child id is the tuple ``('md', anchor, chain, line)`` — the
        # anchor is A's per-file root ``('file', A)``, the chain is just the
        # referenced file (off A directly), line is None (a doc root). The
        # umbrella is a grouping parent, not a chain hop, so it doesn't appear.
        bnode = self._refs(self.A)[0]
        tag, anchor, chain, line = bnode.id
        self.assertEqual(tag, 'md')
        self.assertEqual(anchor, ('file', self.A))
        self.assertEqual(chain, (os.path.realpath(self.B),))
        self.assertIsNone(line)

    def test_umbrella_always_wraps_single_ref(self):
        # Even a single ref is grouped under the umbrella — never hung directly
        # under the document. A references only B.md (C.md is absent), so the
        # umbrella has exactly one ``[md]`` child.
        refs = self._refs(self.A)
        self.assertEqual([k.title for k in refs], ['B.md'])

    # --- expanding a referenced file -------------------------------------

    def test_expand_ref_yields_headings_and_back_ref_umbrella(self):
        # Expanding B's node yields B's top-level headings PLUS its own
        # References umbrella (the cycle back to A is grouped, not direct).
        bnode = self._refs(self.A)[0]
        bkids = self.r.get_children(bnode.id)
        heading_titles = [k.title for k in bkids if k.tag.startswith('h')]
        self.assertEqual(heading_titles, ['B one', 'B two'])
        # No bare ``[md]`` rows under the file doc — the ref is under B's
        # own umbrella, whose id chains onto bnode.id.
        self.assertFalse(any(k.tag == 'md' for k in bkids))
        b_umbrella = self._umbrella(bkids)
        # B's own umbrella shares A's anchor; its chain is B's chain (the
        # umbrella groups, it is not a new hop).
        _, b_anchor, b_chain, _ = bnode.id
        self.assertEqual(b_umbrella.id, ('refs', b_anchor, b_chain))
        back = self.r.get_children(b_umbrella.id)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].title, 'A.md')
        self.assertTrue(getattr(back[0], 'boundary', False))

    def test_cycle_expands_in_place_one_more_level(self):
        # Drilling the cycle A→B→A is finite per manual drill: expanding the
        # A-under-B node yields A's headings + A's References umbrella again
        # (no crash, no loop).
        bnode = self._refs(self.A)[0]
        a_under_b = self._refs(bnode.id)[0]
        a_kids = self.r.get_children(a_under_b.id)
        heading_titles = [k.title for k in a_kids if k.tag.startswith('h')]
        self.assertEqual(heading_titles, ['A title'])
        # And B shows up again under this A's umbrella — the cycle just keeps
        # materialising lazily, one drill at a time.
        self.assertEqual([k.title for k in self._refs(a_under_b.id)], ['B.md'])
        # The chain has grown by one segment each level.
        _, _anchor, chain, _line = a_under_b.id
        self.assertEqual(chain,
                         (os.path.realpath(self.B), os.path.realpath(self.A)))

    def test_ref_file_heading_node_has_subheading_children(self):
        # A heading inside a referenced file expands to its sub-structure only
        # (no ref children / no umbrella under a heading — refs live at the
        # document level). ``# B one`` opens with a loose body run
        # (``back to A.md again``) before ``## B sub``, so that surfaces as a
        # leading dim ``[text]`` leaf, followed by the sub-heading.
        bnode = self._refs(self.A)[0]
        b_one = next(k for k in self.r.get_children(bnode.id)
                     if k.tag.startswith('h') and k.title == 'B one')
        sub = self.r.get_children(b_one.id)
        self.assertEqual([(k.tag, k.title) for k in sub],
                         [('text', 'back to A.md again'), ('h2', 'B sub')])
        self.assertFalse(any(k.tag in ('md', 'links') for k in sub))

    def test_ref_document_node_shows_leading_text_leaf(self):
        # A referenced file that OPENS with a loose body run (before its first
        # heading) surfaces that run as a dim ``[text]`` leaf at the top of its
        # file-doc rows, ahead of the headings — same rule as the in-file tree,
        # routed through the lazy ``_md_subtree_children`` / ``_md_node_item``.
        self._write(self.B,
                    'lead-in before any heading\n'
                    '# B one\n'
                    'beta body\n')
        self.r.md_doc.clear_cache()
        self.r._reparse()
        bnode = self._refs(self.A)[0]
        rows = self.r.get_children(bnode.id)
        # First row is the leading text leaf; the heading follows.
        self.assertEqual(rows[0].tag, 'text')
        self.assertEqual(rows[0].title, 'lead-in before any heading')
        self.assertEqual(rows[0].kind, 'md-text')
        self.assertFalse(rows[0].has_children)
        self.assertEqual([k.tag for k in rows if k.tag.startswith('h')], ['h1'])
        # The text leaf previews to just its own run (its byte slice).
        self.assertEqual(self.r.get_preview(rows[0].id),
                         'lead-in before any heading\n')

    # --- preview routing --------------------------------------------------

    def test_umbrella_preview_is_plain_label_list(self):
        # The References umbrella preview is a PLAIN list of the ref labels
        # (one per line, a count header) — NOT routed through md2ansi (no ANSI
        # even with the color toggle ON), and never the file documents' bodies.
        umbrella = self._umbrella(self.r.get_children(('file', self.A)))
        self.r._MD_COLOR = True   # would colorise a markdown preview
        out = self.r.get_preview(umbrella.id)
        self.assertNotIn('\x1b', out, 'umbrella preview must be plain text')
        self.assertIn('B.md', out)               # the ref label is listed
        self.assertIn('1 referenced file', out)  # the count header
        # NOT the referenced file's own body (that is the file-doc preview).
        self.assertNotIn('B one', out)
        self.assertNotIn('beta body', out)

    def test_umbrella_preview_empty_when_unreadable(self):
        # A ``('refs', …)`` id whose document is gone yields '' (no crash,
        # no header).
        refs_id = ('refs', ('file', self.A),
                   (os.path.join(self.dir, 'gone.md'),))
        self.assertEqual(self.r.get_preview(refs_id), '')

    def test_preview_ref_document_node_is_full_text(self):
        # A referenced-file document id previews that file's FULL text.
        bnode = self._refs(self.A)[0]
        with open(self.B, encoding='utf-8') as f:
            b_text = f.read()
        self.assertEqual(self.r.get_preview(bnode.id), b_text)

    def test_preview_ref_heading_node_is_section_slice(self):
        # A referenced-file heading id previews just that heading's section
        # slice (boundary rule: ``# B one`` stops before ``# B two``).
        bnode = self._refs(self.A)[0]
        b_one = next(k for k in self.r.get_children(bnode.id)
                     if k.tag.startswith('h') and k.title == 'B one')
        out = self.r.get_preview(b_one.id)
        self.assertTrue(out.startswith('# B one\n'))
        self.assertIn('## B sub', out)
        self.assertNotIn('# B two', out)

    def test_preview_unreadable_ref_doc_is_empty(self):
        # An id naming a non-existent file previews '' (no crash).
        bad = ('md', ('file', self.A),
               (os.path.join(self.dir, 'gone.md'),), None)
        self.assertEqual(self.r.get_preview(bad), '')

    # --- has_children / heading-less file --------------------------------

    def test_root_has_children_with_heading_and_ref(self):
        # A has both a heading and an existing ref — root is expandable.
        self.assertTrue(self.r._FILES[self.A].file_root.has_children)

    def test_heading_less_file_with_ref_is_expandable(self):
        # A file with NO headings that references an existing ``.md`` must still
        # carry an expansion arrow (has_children True). Its only child is the
        # lazy References umbrella, which groups the ref.
        import os
        H = os.path.join(self.dir, 'H.md')
        self._write(H, 'Just prose, no headings, but see B.md for details.\n')
        self.r._INPUT_FILES = [(H, '')]
        self.r._ROOT_PATH = H
        self.r.md_doc.clear_cache()
        self.r._reparse()
        root = self.r._FILES[H].file_root
        self.assertTrue(root.has_children)
        # Eager heading tree is empty; the only child is the lazy umbrella.
        self.assertEqual(root._children, [])
        kids = self.r.get_children(('file', H))
        self.assertEqual([(k.tag, k.title) for k in kids], [('links', 'References')])
        refs = self.r.get_children(kids[0].id)
        self.assertEqual([(k.tag, k.title) for k in refs], [('md', 'B.md')])

    def test_heading_less_file_without_ref_not_expandable(self):
        # Control: prose with no headings and no resolvable ref is a leaf —
        # the in-file behaviour for ref-less files is unchanged.
        import os
        P = os.path.join(self.dir, 'P.md')
        self._write(P, 'Just prose. A missing nope.md reference only.\n')
        self.r._INPUT_FILES = [(P, '')]
        self.r._ROOT_PATH = P
        self.r.md_doc.clear_cache()
        self.r._reparse()
        self.assertFalse(self.r._FILES[P].file_root.has_children)
        self.assertEqual(self.r.get_children(('file', P)), [])

    # --- unchanged in-file behaviour -------------------------------------

    def test_in_file_heading_unaffected_for_refless_file(self):
        # A ref-less file's heading children are exactly its sub-headings —
        # no stray ``[md]`` rows leak into the in-file tree.
        import os
        K = os.path.join(self.dir, 'K.md')
        self._write(K, '# K1\n## K1a\n## K1b\n# K2\n')
        self.r._INPUT_FILES = [(K, '')]
        self.r._ROOT_PATH = K
        self.r.md_doc.clear_cache()
        self.r._reparse()
        kids = self.r.get_children(('file', K))
        self.assertEqual([(k.tag, k.title) for k in kids],
                         [('h1', 'K1'), ('h1', 'K2')])
        k1 = kids[0]
        self.assertEqual([(c.tag, c.title) for c in self.r.get_children(k1.id)],
                         [('h2', 'K1a'), ('h2', 'K1b')])

    # --- markdown-link references (ticket #698) ---------------------------

    def test_markdown_link_ref_surfaces_under_umbrella(self):
        # Regression for #698: a file that references an EXISTING ``.md`` via a
        # standard markdown LINK ``[label](other.md)`` (the COMMON case) must
        # still surface that file — now grouped under the References umbrella.
        # Before the #698 fix the link delimiters polluted the captured token
        # (``[B.md](B.md``), resolution returned None, and the ref was silently
        # dropped — leaving the file with no ref at all.
        import os
        L = os.path.join(self.dir, 'L.md')
        self._write(L, 'Prose that links to [the B doc](B.md) for details.\n')
        self.r._INPUT_FILES = [(L, '')]
        self.r._ROOT_PATH = L
        self.r.md_doc.clear_cache()
        self.r._reparse()
        root = self.r._FILES[L].file_root
        self.assertTrue(root.has_children)
        # The link target B.md resolves and appears exactly once as a [md] ref
        # under the umbrella (the label + target both capture 'B.md', deduped).
        refs = self._refs(L)
        self.assertEqual([k.title for k in refs], ['B.md'])
        self.assertTrue(getattr(refs[0], 'boundary', False))


class TestRootLabelMap(unittest.TestCase):
    """``_root_label_map`` — always project-root-relative labeling (ticket #735).

    Top-level rows are labeled via ``_md_ref_label`` against the cwd's git
    root (else cwd) — the SAME general anchoring used for auto-discovered
    ``.md`` links, so a link and a top-level row to the same file read
    identically. A file directly in the project root still renders as its
    bare basename (relpath from root); a file in a subdir shows its
    subdir-relative path, even when every input shares that one subdir.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_file_in_root_is_bare_basename(self):
        # A file directly in the project root → relpath collapses to the
        # bare basename (the common case is unchanged).
        import os
        import tempfile
        root = os.path.realpath(tempfile.mkdtemp())
        try:
            os.mkdir(os.path.join(root, '.git'))
            a = os.path.join(root, 'README.md')
            saved = os.getcwd()
            os.chdir(root)
            try:
                labels = self.r._root_label_map([a])
            finally:
                os.chdir(saved)
            self.assertEqual(labels, {a: 'README.md'})
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_single_subdir_uses_root_relative_not_basename(self):
        # Multiple files all in ONE subdir under a git root: labels must be
        # project-root-relative (include the subdir), NOT bare basenames —
        # matching how a link to either file would be labeled.
        import os
        import tempfile
        root = os.path.realpath(tempfile.mkdtemp())
        try:
            os.mkdir(os.path.join(root, '.git'))
            os.makedirs(os.path.join(root, 'sub'))
            a = os.path.join(root, 'sub', 'a.md')
            b = os.path.join(root, 'sub', 'b.md')
            saved = os.getcwd()
            os.chdir(root)
            try:
                labels = self.r._root_label_map([a, b])
            finally:
                os.chdir(saved)
            self.assertEqual(labels, {
                a: os.path.join('sub', 'a.md'),
                b: os.path.join('sub', 'b.md'),
            })
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_multi_dir_labels_relative_to_git_root(self):
        # Two dirs under one git root → labels are project_root-relative
        # (the disambiguating path, not a bare basename).
        import os
        import tempfile
        root = os.path.realpath(tempfile.mkdtemp())
        try:
            os.mkdir(os.path.join(root, '.git'))
            os.makedirs(os.path.join(root, 'sub'))
            a = os.path.join(root, 'guide.md')
            b = os.path.join(root, 'sub', 'guide.md')
            saved = os.getcwd()
            os.chdir(root)
            try:
                labels = self.r._root_label_map([a, b])
            finally:
                os.chdir(saved)
            self.assertEqual(labels, {
                a: 'guide.md',
                b: os.path.join('sub', 'guide.md'),
            })
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_labels_match_md_ref_label(self):
        # The map delegates to ``_md_ref_label`` verbatim — assert the
        # computed labels equal a direct call with the same cwd /
        # project_root anchors (no git root → falls back to cwd).
        import os
        import tempfile
        base = os.path.realpath(tempfile.mkdtemp())
        try:
            os.makedirs(os.path.join(base, 'x'))
            os.makedirs(os.path.join(base, 'y'))
            a = os.path.join(base, 'x', 'one.md')
            b = os.path.join(base, 'y', 'two.md')
            saved = os.getcwd()
            os.chdir(base)
            try:
                labels = self.r._root_label_map([a, b])
                cwd = os.getcwd()
                project_root = self.r.md_doc.find_git_root(cwd) or cwd
                self.assertEqual(labels, {
                    a: self.r._md_ref_label(a, cwd, project_root),
                    b: self.r._md_ref_label(b, cwd, project_root),
                })
            finally:
                os.chdir(saved)
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class TestDirMdFiles(unittest.TestCase):
    """``_dir_md_files`` — non-recursive ``.md``/``.MD`` listing."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_returns_only_markdown_sorted_abs_nonrecursive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            # Markdown files (both extension casings), out of name order.
            for name in ('b.md', 'a.MD', 'c.md'):
                with open(os.path.join(tmp, name), 'w') as f:
                    f.write('# x\n')
            # Non-markdown siblings that must be ignored.
            for name in ('notes.txt', 'readme.markdown', 'plain', 'd.mdx'):
                with open(os.path.join(tmp, name), 'w') as f:
                    f.write('x\n')
            # A subdirectory containing a .md — must NOT be recursed into.
            sub = os.path.join(tmp, 'sub')
            os.mkdir(sub)
            with open(os.path.join(sub, 'deep.md'), 'w') as f:
                f.write('# deep\n')

            got = self.r._dir_md_files(tmp)
            self.assertEqual(
                got,
                [os.path.join(tmp, 'a.MD'),
                 os.path.join(tmp, 'b.md'),
                 os.path.join(tmp, 'c.md')],
            )
            # Every entry is an absolute path.
            for p in got:
                self.assertTrue(os.path.isabs(p))

    def test_empty_dir_returns_empty_list(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.r._dir_md_files(tmp), [])


class TestCollectInputFiles(unittest.TestCase):
    """``_collect_input_files`` — directory expansion, dedup, anchors."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write(self, path, body='# h\n'):
        with open(path, 'w') as f:
            f.write(body)

    def test_single_dir_expansion(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            a = os.path.join(tmp, 'a.md')
            b = os.path.join(tmp, 'b.md')
            self._write(a)
            self._write(b)
            files, anchor, anchor_path = self.r._collect_input_files([tmp])
            self.assertEqual(files, [(a, ''), (b, '')])
            self.assertEqual(anchor, '')
            self.assertIsNone(anchor_path)

    def test_mixed_file_and_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            d = os.path.join(tmp, 'd')
            os.mkdir(d)
            da = os.path.join(d, 'a.md')
            self._write(da)
            solo = os.path.join(tmp, 'solo.md')
            self._write(solo)
            files, anchor, anchor_path = self.r._collect_input_files([solo, d])
            self.assertEqual(files, [(solo, ''), (da, '')])
            self.assertEqual(anchor, '')
            self.assertIsNone(anchor_path)

    def test_dedup_repeated_path_first_seen_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            a = os.path.join(tmp, 'a.md')
            b = os.path.join(tmp, 'b.md')
            self._write(a)
            self._write(b)
            files, _anchor, _ap = self.r._collect_input_files([a, b, a])
            self.assertEqual(files, [(a, ''), (b, '')])

    def test_directory_with_hash_treated_as_path_no_anchor(self):
        # A directory positional carrying a ``#`` is treated as a path
        # (no anchor split): the dir is expanded and no anchor recorded.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            sub = os.path.join(tmp, 'sec#tion')
            os.mkdir(sub)
            a = os.path.join(sub, 'a.md')
            self._write(a)
            files, anchor, anchor_path = self.r._collect_input_files([sub])
            self.assertEqual(files, [(a, '')])
            self.assertEqual(anchor, '')
            self.assertIsNone(anchor_path)

    def test_first_anchor_wins_across_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            a = os.path.join(tmp, 'a.md')
            b = os.path.join(tmp, 'b.md')
            self._write(a)
            self._write(b)
            files, anchor, anchor_path = self.r._collect_input_files(
                [a + '#first', b + '#second'])
            self.assertEqual(files, [(a, 'first'), (b, 'second')])
            self.assertEqual(anchor, 'first')
            self.assertEqual(anchor_path, a)

    def test_empty_aggregate_raises(self):
        # An empty directory expands to nothing → SystemExit.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.r._collect_input_files([tmp])

    def test_nonexistent_path_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, 'nope.md')
            with self.assertRaises(SystemExit):
                self.r._collect_input_files([missing])


class TestOnResize(unittest.TestCase):
    """#830: the recipe registers ``on_resize`` -> ``drop_preview_cache``."""

    def test_on_resize_drops_preview_cache(self):
        # #830: the recipe registers an on_resize handler that drops the
        # whole preview cache, so a pane-layout change (terminal resize OR
        # split/ratio — the broadened on_resize, #828) triggers a refetch
        # and ``get_preview`` re-lays width-dependent previews (md2ansi
        # tables / wrapped markdown prose) at the new ctx.preview_width.
        # We can't run main() under unit test (it touches argv / the
        # md2ansi dependency gate), so confirm (a) the exact registration
        # is present in source, and (b) that handler shape actually calls
        # ``drop_preview_cache()`` when invoked.
        source = _RECIPE.read_text()
        self.assertIn(
            'on_resize=lambda ctx, cols, rows: ctx.drop_preview_cache(),',
            source,
            'browse-md must register on_resize -> drop_preview_cache so '
            'width-dependent previews refetch on a layout change')
        # Behavioural check on the registered handler shape: a spy ctx
        # records the drop call. (The end-to-end re-render is covered by
        # test/ui/test_recipe_browse_md.py.)
        drops = []

        class _SpyCtx:
            def drop_preview_cache(self, id_=None):
                drops.append(id_)

        on_resize = lambda ctx, cols, rows: ctx.drop_preview_cache()
        on_resize(_SpyCtx(), 120, 40)
        self.assertEqual(
            drops, [None],
            'on_resize must drop the entire preview cache (id=None) so the '
            'framework re-fetches the cursor preview at the new width')


if __name__ == '__main__':
    unittest.main()
