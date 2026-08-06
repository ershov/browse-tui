"""Unit tests for the parser + tree builder in ``recipes/browse-md``.

The recipe is a single-file ``--run-py`` script that imports
``browse_tui`` (only available when the binary loads it). To exercise
the parser / tree-builder helpers directly we stub ``browse_tui`` in
``sys.modules`` and load the extension-less recipe via the
``SourceFileLoader`` — same pattern as
``test/unit/test_browse_claude_render.py``.

Coverage focuses on the helpers exported at module scope:

* ``_build_nodes``                    (TestBuildNodes / TestTextNodes)
* ``get_children``                    (TestGetChildren)
* ``_node_at_line``                   (TestNodeAtLine)
* ``_display_title``                  (TestDisplayTitle)
* ``_resolve_anchor``                 (TestResolveAnchor)
* ``get_preview``                     (TestGetPreview)
* ``_action_toggle_md``               (TestToggleMd)
* ``_resolve_md_pager``               (TestResolveMdPager)

The tree is built from ``md2ansi_lib.md2ansi_doc`` (``M2A_Doc`` /
``M2A_Node``) — headings only, plus the ``#text`` row-set rule for
loose body runs; titles are the library's stripped display form.
"""

import contextlib
import importlib.util
import os
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-md'


# ``main()`` auto-detects a piped stdin via ``os.isatty(0)``: a non-tty fd 0
# synthesizes the lone ``-`` (stdin mode). The test runner's fd 0 is itself a
# pipe (non-tty), which would spuriously trip that auto-detect for the
# bare/FILE-mode cases below. Pin the whole module to an INTERACTIVE tty so
# those tests keep exercising bare/FILE mode (the historical default); the
# dedicated auto-detect tests opt back into a pipe via ``_piped_stdin``.
_isatty_patch = None


def setUpModule():
    global _isatty_patch
    _isatty_patch = mock.patch('os.isatty', return_value=True)
    _isatty_patch.start()


def tearDownModule():
    if _isatty_patch is not None:
        _isatty_patch.stop()


@contextlib.contextmanager
def _piped_stdin():
    """Within the block, ``os.isatty(0)`` is False (a piped/redirected stdin).

    Restores the module-wide interactive default on exit, so the auto-detect
    tests can simulate ``cmd | browse-md`` without leaking the False into
    neighbouring bare/FILE-mode cases."""
    with mock.patch('os.isatty', return_value=False):
        yield


def _stub_recipe_argv(argv=None):
    """Stub of the framework's ``recipe_argv`` (mirrors 040-state.py):
    ``sys.argv[1:]`` (or ``argv``) minus the framework's ``--tty VALUE`` /
    ``--tty=VALUE`` flag. Tests patch ``sys.argv`` before driving ``main()``,
    so reading it here matches what the recipe sees."""
    if argv is None:
        argv = sys.argv[1:]
    out, skip_next = [], False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == '--tty':
            skip_next = True
            continue
        if arg.startswith('--tty='):
            continue
        out.append(arg)
    return out


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
    mod.recipe_argv = _stub_recipe_argv
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-md recipe; returns the module.

    ``recipes/browse-md`` has no ``.py`` extension; importlib's
    default loader-from-extension lookup returns None, so we use the
    source loader explicitly. A fresh module instance is created on
    every call so tests that mutate module-level state (notably
    ``_BY_ID`` for ``get_children``) don't bleed into each other.

    ``recipes/`` is put on ``sys.path`` (idempotently) so the recipe's
    now-hard ``from md2ansi_lib import ...`` resolves to the real
    library — the same thing ``--run-py`` does at runtime by prepending
    the recipe's directory. Without this the import would silently fail
    under test and ``_build_nodes`` would have no document model to map.
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
    return mod


def _build_tree(r, text, path):
    """Run the recipe's build pipeline on ``text``: ``(root, by_id)``.

    The same two steps ``_reparse`` performs per file — the library
    document model, then the recipe's Item mapping.
    """
    return r._build_nodes(r._md2ansi_doc(text), path)


class TestHeadingMasking(unittest.TestCase):
    """Grammar-level masking flows through ``md2ansi_doc`` into the tree.

    The document model runs the library's full block grammar, so a
    ``#``-led line inside a fence / blockquote / table never becomes a
    heading row, and frontmatter is consumed silently. Asserted on the
    built Item tree — the recipe consumes the model, not raw spans.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _heading_rows(self, text):
        """``(tag, title)`` for every HEADING row, in source order.

        Text rows are skipped — a masked block (fence / quote / table)
        legitimately surfaces as a preamble ``[text]`` row; what these
        tests pin is that it never surfaces as a *heading*.
        """
        root, _ = _build_tree(self.r, text, '/tmp/mask.md')
        out = []

        def walk(node):
            for kid in node._children:
                if kid.kind == 'heading':
                    out.append((kid.tag, kid.title))
                walk(kid)

        walk(root)
        return out

    def test_each_heading_level(self):
        text = '# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6\n'
        self.assertEqual(
            self._heading_rows(text),
            [(f'h{n}', f'H{n}') for n in range(1, 7)])

    def test_frontmatter_consumed_silently(self):
        text = '---\ntitle: foo\n---\n# H1\n'
        self.assertEqual(self._heading_rows(text), [('h1', 'H1')])

    def test_hr_mid_document_not_frontmatter(self):
        # ``---`` not at offset 0 is an HR (no heading row), NOT
        # frontmatter (frontmatter has \A anchor). The headings on
        # either side show.
        text = '# H1\n\n---\n\n## H2\n'
        self.assertEqual(self._heading_rows(text),
                         [('h1', 'H1'), ('h2', 'H2')])

    def test_heading_inside_fenced_code_block_masked(self):
        text = '```\n# fake heading\n```\n# real heading\n'
        self.assertEqual(self._heading_rows(text), [('h1', 'real heading')])

    def test_heading_inside_blockquote_masked(self):
        # The blockquote rule starts with ``>`` so the ``> # fake``
        # line is consumed by the blockquote rule rather than emitting.
        text = '> # fake heading\n> still in quote\n\n# real heading\n'
        self.assertEqual(self._heading_rows(text), [('h1', 'real heading')])

    def test_heading_inside_table_masked(self):
        # Lines starting with ``|`` are absorbed by the table rule.
        text = '| col |\n| # fake heading |\n\n# real heading\n'
        self.assertEqual(self._heading_rows(text), [('h1', 'real heading')])


class TestBuildNodes(unittest.TestCase):
    """``_build_nodes`` maps the ``M2A_Doc`` model onto a tree of Items.

    Headings only — the fixture's list lines are leaf-section body text
    (they end up inside their heading's byte span, not as rows; the
    zero-width / leaf-chapter ``#text`` runs are filtered by the row-set
    rule, see ``TestTextNodes`` for the text-row cases).
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
        cls.r = _load_recipe()
        cls.path = '/tmp/fake.md'
        cls.doc = cls.r._md2ansi_doc(cls.FIXTURE)
        cls.root, cls.by_id = cls.r._build_nodes(cls.doc, cls.path)

    def test_root_id_and_kind(self):
        self.assertEqual(self.root.id, ('file', self.path))
        self.assertEqual(self.root.kind, 'root')
        self.assertEqual(self.root.level, 0)
        self.assertTrue(self.root.has_children)

    def test_root_has_two_h1_children(self):
        kids = self.root._children
        self.assertEqual(len(kids), 2)
        self.assertEqual([k.tag for k in kids], ['h1', 'h1'])
        # Titles are the library's stripped display form; the kind is
        # already conveyed by the ``[h1]`` tag.
        self.assertEqual(kids[0].title, 'H1')
        self.assertEqual(kids[1].title, 'H1b')

    def test_first_h1_has_two_h2_children(self):
        h1 = self.root._children[0]
        kids = h1._children
        self.assertEqual(len(kids), 2)
        self.assertEqual([k.tag for k in kids], ['h2', 'h2'])
        self.assertEqual(kids[0].title, 'H2a')
        self.assertEqual(kids[1].title, 'H2b')

    def test_no_list_items_anywhere(self):
        # List lines are section body, never rows: no ``ul`` / ``ol``
        # tag exists in the tree or in ``by_id``.
        seen_tags = []

        def walk(node):
            for kid in node._children:
                seen_tags.append(kid.tag)
                walk(kid)

        walk(self.root)
        self.assertNotIn('ul', seen_tags)
        self.assertNotIn('ol', seen_tags)
        for item in self.by_id.values():
            self.assertIn(item.kind, ('root', 'heading', 'text'))

    def test_leaf_headings_have_no_children(self):
        # H2a's body is list lines only — a leaf-chapter body, filtered
        # by the row-set rule. Same for H2b and H1b.
        h1 = self.root._children[0]
        h2a, h2b = h1._children
        self.assertEqual(h2a._children, [])
        self.assertFalse(h2a.has_children)
        self.assertEqual(h2b._children, [])
        self.assertFalse(h2b.has_children)
        h1b = self.root._children[1]
        self.assertEqual(h1b._children, [])
        self.assertFalse(h1b.has_children)

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
        # The second H1's section extends through ``- top`` to EOF.
        h1b = self.root._children[1]
        self.assertEqual(h1b.byte_offset + h1b.byte_size, len(self.FIXTURE))
        self.assertEqual(h1b.line_offset + h1b.line_size,
                         len(self.doc.line_starts))
        sliced = self.FIXTURE[h1b.byte_offset:h1b.byte_offset + h1b.byte_size]
        self.assertIn('- top', sliced)

    def test_h2a_byte_span_stops_at_h2b(self):
        # ``## H2a`` ends where ``## H2b`` begins (sibling-or-shallower);
        # the list lines in between belong to H2a's section slice.
        h1 = self.root._children[0]
        h2a, h2b = h1._children
        self.assertEqual(h2a.byte_offset + h2a.byte_size, h2b.byte_offset)
        sliced = self.FIXTURE[h2a.byte_offset:h2a.byte_offset + h2a.byte_size]
        self.assertIn('- a', sliced)
        self.assertIn('  - a1', sliced)
        self.assertIn('- b', sliced)

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

    def test_byte_offsets_are_source_positions(self):
        # Heading byte offsets are the literal positions of the ``#``
        # in the source.
        h1 = self.root._children[0]
        h2a, h2b = h1._children
        h1b = self.root._children[1]
        self.assertEqual(h1.byte_offset, self.FIXTURE.index('# H1\n'))
        self.assertEqual(h2a.byte_offset, self.FIXTURE.index('## H2a'))
        self.assertEqual(h2b.byte_offset, self.FIXTURE.index('## H2b'))
        self.assertEqual(h1b.byte_offset, self.FIXTURE.index('# H1b'))

    def test_kind_field(self):
        # Every Item has a ``kind`` of root | heading (| text, absent
        # in this fixture).
        self.assertEqual(self.root.kind, 'root')
        for h1 in self.root._children:
            self.assertEqual(h1.kind, 'heading')
        h2a = self.root._children[0]._children[0]
        self.assertEqual(h2a.kind, 'heading')

    def test_level_field(self):
        # Headings: 1..6. Root: 0.
        self.assertEqual(self.root.level, 0)
        h1 = self.root._children[0]
        self.assertEqual(h1.level, 1)
        h2a = h1._children[0]
        self.assertEqual(h2a.level, 2)

    def test_tag_style_per_heading_level(self):
        h1 = self.root._children[0]
        self.assertEqual(h1.tag_style, 'red')
        h2a = h1._children[0]
        self.assertEqual(h2a.tag_style, 'yellow')

    def test_by_id_contains_root_and_every_row(self):
        # Root + 4 headings (2 H1 + 2 H2); the filtered text runs and
        # the list lines never enter the index.
        self.assertEqual(len(self.by_id), 5)
        self.assertIn(('file', self.path), self.by_id)
        # Each non-root id resolves to an Item.
        h1 = self.root._children[0]
        self.assertIs(self.by_id[h1.id], h1)


class TestTitleStripping(unittest.TestCase):
    """Row titles are the library's display form (``M2A_Node.title``).

    The heading sigil is stripped (the ``[h1]`` tag already conveys the
    kind) AND inline markup is stripped via the renderer's own inline
    grammar, with whitespace collapsed — ``## My **bold** heading``
    shows as ``My bold heading``.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _titles_in_source_order(self, text):
        # Walk the tree depth-first in source order so we get every
        # row's title regardless of nesting.
        root, _ = _build_tree(self.r, text, '/tmp/strip.md')
        out = []

        def walk(node):
            for kid in node._children:
                out.append(kid.title)
                walk(kid)

        walk(root)
        return out

    def test_heading_inline_formatting_stripped(self):
        # ``## My **bold** heading`` → ``My bold heading``: the ``**``
        # markers are inline markup, stripped from the display title
        # (the rendered preview still shows the bold, of course).
        titles = self._titles_in_source_order('## My **bold** heading\n')
        self.assertEqual(titles, ['My bold heading'])

    def test_heading_backticks_stripped(self):
        # Inline code markers go the same way: ``## `code` span`` →
        # ``code span``.
        titles = self._titles_in_source_order('## `code` span\n')
        self.assertEqual(titles, ['code span'])

    def test_heading_trailing_hash_preserved(self):
        # md2ansi_lib's heading grammar doesn't special-case a trailing
        # ``#`` — only the *leading* sigil is a sigil — so it stays in
        # the display title.
        titles = self._titles_in_source_order(
            '### Heading with trailing #\n')
        self.assertEqual(titles, ['Heading with trailing #'])

    def test_heading_with_bold_italic_asterisks_stripped(self):
        # ``## *** bold-italic ***`` — the ``***`` runs are inline
        # emphasis markers, stripped along with the sigil.
        titles = self._titles_in_source_order('## *** bold-italic ***\n')
        self.assertEqual(titles, ['bold-italic'])

    def test_heading_internal_whitespace_collapsed(self):
        # Display titles are whitespace-collapsed: internal double
        # spaces fold to one.
        titles = self._titles_in_source_order('## Foo  bar\n')
        self.assertEqual(titles, ['Foo bar'])


class TestGetChildren(unittest.TestCase):
    """``get_children`` reads cached ``_children`` off of ``_BY_ID``."""

    def setUp(self):
        # Fresh module per test — ``_BY_ID`` is module-level state.
        self.r = _load_recipe()
        fixture = (
            '# H1\n'
            '## H2\n'
            'body\n'
        )
        self.path = '/tmp/getchildren.md'
        self.root, by_id = _build_tree(self.r, fixture, self.path)
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
    """Empty / paragraph-only / leading-body / no-trailing-newline."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _build(self, text, path='/tmp/edge.md'):
        return _build_tree(self.r, text, path)

    def test_empty_file(self):
        root, by_id = self._build('')
        self.assertEqual(root._children, [])
        self.assertFalse(root.has_children)
        # ``by_id`` still carries the root.
        self.assertIn(root.id, by_id)

    def test_paragraphs_only_no_children(self):
        # A non-empty preamble with NO heading sibling is filtered by
        # the row-set rule — the whole body reads through the root's
        # preview instead.
        text = 'Just a paragraph.\n\nAnd another one.\n'
        root, _ = self._build(text)
        self.assertEqual(root._children, [])
        self.assertFalse(root.has_children)

    def test_body_before_any_heading_attaches_to_root(self):
        # A body run ahead of the first heading (here list lines — just
        # text to the headings-only tree) becomes ONE dim ``[text]`` row
        # before the heading it precedes.
        text = '- top1\n- top2\n# After\n'
        root, _ = self._build(text)
        self.assertEqual([k.tag for k in root._children], ['text', 'h1'])
        text_row = root._children[0]
        self.assertEqual(text_row.kind, 'text')
        # The run spans both list lines, up to ``# After``.
        self.assertEqual(text_row.byte_offset, 0)
        self.assertEqual(text_row.byte_size, len('- top1\n- top2\n'))

    def test_file_without_trailing_newline(self):
        text = '# Only\nlast body'  # no trailing newline
        root, _ = self._build(text)
        kids = root._children
        self.assertEqual(len(kids), 1)
        h1 = kids[0]
        # ``# Only`` spans through to len(file_text).
        self.assertEqual(h1.byte_offset + h1.byte_size, len(text))
        # Leaf-chapter body → no child rows.
        self.assertEqual(h1._children, [])


class TestNodeAtLine(unittest.TestCase):
    """``_node_at_line`` — line → deepest containing Item lookup."""

    FIXTURE = (
        '# H1\n'            # line 0
        'intro for H1\n'    # line 1 — a [text] row (heading sibling below)
        '## H2\n'           # line 2
        'body of H2\n'      # line 3 — leaf-chapter body, NOT a row
        '# H1b\n'           # line 4
    )

    def setUp(self):
        # Fresh module per test — ``_BY_LINE`` / ``_LINES_SORTED``
        # are module-level state populated by ``main()``; we have
        # to mirror that wiring here.
        self.r = _load_recipe()
        self.path = '/tmp/lookup.md'
        self._index(self.FIXTURE, self.path)

    def _index(self, text, path):
        self.root, by_id = _build_tree(self.r, text, path)
        self.r._BY_ID = by_id
        self.r._BY_LINE, self.r._LINES_SORTED = self.r._build_line_index(by_id)

    def test_exact_match_on_heading_line(self):
        # Line 0 is the ``# H1`` heading; the stored title drops the
        # leading ``#`` + whitespace.
        node = self.r._node_at_line(0)
        self.assertIsNotNone(node)
        self.assertEqual(node.title, 'H1')

    def test_exact_match_on_text_run_line(self):
        # Line 1 is the [text] run preceding ``## H2``.
        node = self.r._node_at_line(1)
        self.assertEqual(node.kind, 'text')
        self.assertEqual(node.title, 'intro for H1')

    def test_inexact_falls_back_to_previous_node(self):
        # Line 3 (H2's leaf body — not a row) falls back to the most
        # recent node, ``## H2`` at line 2.
        self.assertEqual(self.r._node_at_line(3).title, 'H2')

    def test_line_before_any_node_returns_none(self):
        # A blank first line has no node (the preamble run starts at
        # the first NON-blank line), so line 0 precedes every node.
        text = (
            '\n'                # line 0 — blank, no node
            '# H1\n'            # line 1
        )
        self._index(text, '/y.md')
        self.assertIsNone(self.r._node_at_line(0))

    def test_exact_match_on_last_node(self):
        # Line 4 is the last node (``# H1b``).
        node = self.r._node_at_line(4)
        self.assertEqual(node.title, 'H1b')

    def test_line_past_last_node_returns_last_containing(self):
        # Past EOF — should fall back to the last node (``# H1b``
        # subsumes any imaginary later lines under it).
        node = self.r._node_at_line(99999)
        self.assertEqual(node.title, 'H1b')

    def test_empty_file_returns_none(self):
        # No parsed nodes → every lookup is ``None``.
        self._index('', '/e.md')
        self.assertIsNone(self.r._node_at_line(0))
        self.assertIsNone(self.r._node_at_line(42))


class TestDisplayTitle(unittest.TestCase):
    """``_display_title`` — strip surrounding whitespace for matching.

    Stored titles are the library's display form (sigil + inline markup
    already stripped at build time), so this helper has collapsed to a
    thin ``title.strip()`` wrapper. We keep a couple of sanity checks
    here; the substantive stripping coverage lives in
    ``TestTitleStripping``.
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
        # Stored titles are already the stripped display form, so a
        # clean ``Foo`` round-trips verbatim.
        self.assertEqual(self.r._display_title(self._heading('Foo')), 'Foo')

    def test_strips_surrounding_whitespace(self):
        # Defensive: any stray padding is trimmed so anchor matching
        # uses a stable key.
        self.assertEqual(
            self.r._display_title(self._heading('  Foo Bar  ')),
            'Foo Bar',
        )

    def test_passes_stored_title_through_verbatim(self):
        # The helper does no stripping of its own beyond whitespace —
        # whatever is stored (the library already removed markup at
        # build time) is the matching key.
        self.assertEqual(
            self.r._display_title(self._heading('My bold heading')),
            'My bold heading')


class TestResolveAnchor(unittest.TestCase):
    """``_resolve_anchor`` — anchor string → ``initial_scope`` id."""

    FIXTURE = (
        '# Intro\n'             # line 0
        '## Overview\n'         # line 1
        '- bullet\n'            # line 2 — leaf body of Overview, not a row
        '## Details\n'          # line 3
        '# Conclusion\n'        # line 4
    )

    def setUp(self):
        self.r = _load_recipe()
        self.path = '/tmp/anchor.md'
        self._index(self.FIXTURE, self.path)

    def _index(self, text, path):
        self.root, by_id = _build_tree(self.r, text, path)
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
        # Build a fixture where line 0 has no node (a blank line — the
        # preamble run starts at the first NON-blank line); the digit
        # anchor ``0`` lands ahead of every node and falls back to root.
        self._index('\n# After\n', '/p.md')
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
        self._index(text, '/t.md')
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
        self._index(text, '/d.md')
        self.assertEqual(
            self.r._resolve_anchor('Dup', '/d.md'), ('content', '/d.md', 0))

    def test_anchor_scans_headings_only(self):
        # ``bullet`` matches only body text (``- bullet`` — not even a
        # row here) and ``_resolve_anchor`` only scans headings; no
        # match → warning + root fall-through.
        from io import StringIO
        from contextlib import redirect_stderr
        buf = StringIO()
        with redirect_stderr(buf):
            result = self._resolve('bullet')
        self.assertEqual(result, ('file', self.path))
        self.assertIn('bullet', buf.getvalue())

    def _build_goals_fixture(self):
        # Single ``## Goals`` heading at line 0 so the three tiers can
        # be exercised independently — the stored display title for
        # ``## Goals`` is ``'Goals'``.
        self._index('## Goals\n', '/g.md')
        return self.root

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

    # Fixture chosen so headings, the text run, and root all resolve to
    # different byte windows. Trailing newline so byte_size on the
    # final node ends cleanly at len(text).
    FIXTURE = (
        '# H1\n'        # line 0, bytes 0..5
        'preamble\n'    # line 1, bytes 5..14 — [text] row (sibling ## H2)
        '## H2\n'       # line 2, bytes 14..20
        '- a\n'         # line 3, bytes 20..24 — leaf body of H2
        '- b\n'         # line 4, bytes 24..28
        '# H1b\n'       # line 5, bytes 28..34
    )

    def setUp(self):
        # Fresh module per test — globals (``_FILE_TEXT``, ``_BY_ID``,
        # ``_BY_LINE``, ``_LINES_SORTED``, ``_MD_COLOR``,
        # ``_md2ansi_fn``, ``_BROWSER``) mustn't bleed across tests.
        self.r = _load_recipe()
        self.path = '/tmp/preview.md'
        self.root, by_id = _build_tree(self.r, self.FIXTURE, self.path)
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
        # ``## H2`` is a child of ``# H1`` (after the [text] run); its
        # section runs from its own offset to the next
        # sibling-or-shallower boundary (here the next ``# H1b`` since
        # no other ``## H*`` follows).
        h1 = self.root._children[0]
        h2 = h1._children[1]
        self.assertEqual(h2.tag, 'h2')
        out = self.r.get_preview(h2.id)
        self.assertEqual(out,
                         self.FIXTURE[h2.byte_offset:
                                      h2.byte_offset + h2.byte_size])
        self.assertTrue(out.startswith('## H2\n'))

    def test_text_run_returns_its_slice(self):
        # The [text] run at line 1 — its byte window is just the run
        # (up to the ``## H2`` it precedes).
        h1 = self.root._children[0]
        text_run = h1._children[0]
        self.assertEqual(text_run.tag, 'text')
        out = self.r.get_preview(text_run.id)
        self.assertEqual(out, 'preamble\n')

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
    """``_resolve_md_pager`` walks ``$MDCAT`` / ``mdcat+less`` in order."""

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
        restore = self._with_env(MDCAT='mdcat')
        try:
            self.assertEqual(self.r._resolve_md_pager(), ['mdcat'])
        finally:
            restore()

    def test_env_var_shlex_splits(self):
        # ``shlex.split`` keeps the flag separate from the binary name.
        restore = self._with_env(MDCAT='my-md-cmd --flag')
        try:
            self.assertEqual(self.r._resolve_md_pager(),
                             ['my-md-cmd', '--flag'])
        finally:
            restore()

    def test_env_pipeline_uses_bash_dash_c(self):
        # ``|`` in $MDCAT → bash wrapper so the pipe runs.
        restore = self._with_env(MDCAT='mdcat | less -R')
        try:
            cmd = self.r._resolve_md_pager()
            self.assertEqual(cmd[0], 'bash')
            self.assertEqual(cmd[1], '-c')
            self.assertIn('mdcat | less -R', cmd[2])
        finally:
            restore()

    def test_mdcat_plus_less_pipes_to_less_rs(self):
        # Default fallback when both mdcat and less exist: pipe
        # mdcat output through ``less -RS`` via bash.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._scratch_bin(tmp, ['mdcat', 'less'])
            saved_path = os.environ['PATH']
            restore = self._with_env(MDCAT=None)
            try:
                os.environ['PATH'] = tmp
                cmd = self.r._resolve_md_pager()
                self.assertEqual(cmd[0], 'bash')
                self.assertEqual(cmd[1], '-c')
                self.assertIn('mdcat', cmd[2])
                self.assertIn('less -RS', cmd[2])
            finally:
                os.environ['PATH'] = saved_path
                restore()

    def test_mdcat_alone_no_pipe(self):
        # Without ``less`` on PATH, fall back to bare ``mdcat``.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._scratch_bin(tmp, ['mdcat'])
            saved_path = os.environ['PATH']
            restore = self._with_env(MDCAT=None)
            try:
                os.environ['PATH'] = tmp
                self.assertEqual(self.r._resolve_md_pager(), ['mdcat'])
            finally:
                os.environ['PATH'] = saved_path
                restore()

    def test_none_when_nothing_resolves(self):
        # No env var, no binaries on PATH → ``None``.
        import os
        restore = self._with_env(MDCAT=None)
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
        self.r._run_source_command(ctx)
        self.assertEqual(ctx.calls, [])

    def test_root_only_opens_original_path(self):
        # ``root.id`` is the absolute path; the command should be the
        # default split + that path. No tempfile.
        root = _SrcItem(id=('file', self.path), kind='root')
        ctx = _SrcCmdCtx(targets=[root])
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
            self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
    """``_HELP_INTRO`` / ``_HELP_USAGE`` — recipe-level help prose.

    The command-line usage / flags block lives in ``_HELP_USAGE`` (shown
    only by ``--help``); the description, in-app key list and
    context-menu paragraph live in ``_HELP_INTRO`` (shown by both
    ``--help`` and the in-app ``?``).
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_is_non_empty_string(self):
        self.assertIsInstance(self.r._HELP_INTRO, str)
        self.assertTrue(self.r._HELP_INTRO.strip())
        self.assertIsInstance(self.r._HELP_USAGE, str)
        self.assertTrue(self.r._HELP_USAGE.strip())

    def test_contains_usage_form(self):
        # The usage line documents the repeatable ``--root DIR`` flag and
        # the file/dir positionals (with the anchor syntax detailed
        # below). It belongs to ``_HELP_USAGE`` (the ``--help`` flags
        # block), not the in-app intro.
        self.assertIn(
            'browse-md [--root DIR ...] [FILE.md', self.r._HELP_USAGE)
        # The flags block must NOT leak into the in-app help intro.
        self.assertNotIn('Usage:', self.r._HELP_INTRO)

    def test_mentions_help_flag(self):
        # ``-h`` / ``--help`` is a real flag now — discoverable from the
        # usage block.
        self.assertIn('-h, --help', self.r._HELP_USAGE)

    def test_no_lists_flag_leftovers(self):
        # The dropped ``-l`` / ``--list`` / ``--lists`` flag must not
        # linger in either help block.
        for block in (self.r._HELP_USAGE, self.r._HELP_INTRO):
            self.assertNotIn('--list', block)
            self.assertNotIn('list item', block)

    def test_mentions_root_flag(self):
        # The repeatable ``--root DIR`` reference-resolution flag should be
        # discoverable from the usage block alongside a brief description.
        self.assertIn('--root DIR', self.r._HELP_USAGE)

    def test_mentions_anchor(self):
        # Anchor syntax is a load-bearing feature; document it.
        self.assertIn('#anchor', self.r._HELP_USAGE)

    def test_mentions_each_custom_action(self):
        # Every custom action key should be discoverable from the
        # intro (the keys are bound on the action rows below it, but
        # the intro gives the one-liner). ``a`` covers the ``a / i``
        # insert pair (one shared line).
        for key in ('m', 'M', 'V', 'E', 'a', 'x'):
            with self.subTest(key=key):
                # Bound as a word in the format ``  m`` / ``  M`` etc.
                # at start of an indented line — search for "  KEY ".
                self.assertRegex(self.r._HELP_INTRO, rf'(?m)^\s+{key}\b')

    def test_mentions_reload(self):
        # Ctrl-R is the framework keybinding; document its effect.
        self.assertIn('Ctrl-R', self.r._HELP_INTRO)

    def test_compact_size(self):
        # browse-fs-style compact help — kept tight even with the
        # ``--root`` entry and the context-menu paragraph added; a
        # high-30s line budget still fits a screen comfortably.
        self.assertLessEqual(self.r._HELP_INTRO.count('\n'), 38)


class TestArgvFlag(unittest.TestCase):
    """``_pop_flag`` — extract ``-h`` / ``--help`` from argv.

    The flag is popped before the leftover-option scan in ``main()`` so
    order in argv is flexible; the helper is verified directly by
    patching ``sys.argv`` on the recipe module.
    """

    def setUp(self):
        # Fresh module per test — ``sys.argv`` is patched on the
        # recipe's own ``sys`` reference, so we restore the original
        # argv in ``tearDown`` to keep test isolation tight.
        self.r = _load_recipe()
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
        self.assertFalse(self.r._pop_flag('-h', alts=('--help',)))
        # The positional survives the pop pass.
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_short_flag_before_positional(self):
        self._set_argv(['browse-md', '-h', 'FILE.md'])
        self.assertTrue(self.r._pop_flag('-h', alts=('--help',)))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_short_flag_after_positional(self):
        self._set_argv(['browse-md', 'FILE.md', '-h'])
        self.assertTrue(self.r._pop_flag('-h', alts=('--help',)))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_long_alias(self):
        self._set_argv(['browse-md', '--help', 'FILE.md'])
        self.assertTrue(self.r._pop_flag('-h', alts=('--help',)))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_repeated_flags_all_removed(self):
        # Multiple instances of the flag (mixing aliases) should ALL
        # be removed so the positional remains at ``sys.argv[1]``.
        self._set_argv(['browse-md', '-h', '--help', 'FILE.md'])
        self.assertTrue(self.r._pop_flag('-h', alts=('--help',)))
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_pop_flag_does_not_consume_unrelated_args(self):
        # Unknown args are left alone — they'll surface downstream if
        # bogus, but ``_pop_flag`` itself only touches its own keys.
        self._set_argv(['browse-md', '--frobnicate', 'FILE.md'])
        self.assertFalse(self.r._pop_flag('-h', alts=('--help',)))
        self.assertEqual(
            self.r.sys.argv, ['browse-md', '--frobnicate', 'FILE.md'])


class TestHelpFlag(unittest.TestCase):
    """``main()`` with ``-h`` / ``--help`` — full help to stdout, exit 0.

    The flag is popped BEFORE the leftover-option scan (which would
    otherwise die ``unrecognised option: -h``) and before any file is
    read, so it works with or without positionals.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._saved_argv = list(self.r.sys.argv)

    def tearDown(self):
        self.r.sys.argv[:] = self._saved_argv

    def _run_help(self, argv):
        import contextlib
        import io
        self.r.sys.argv[:] = argv
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                self.r.main()
        return cm.exception.code, out.getvalue()

    def test_short_flag_prints_help_and_exits_zero(self):
        code, out = self._run_help(['browse-md', '-h'])
        self.assertEqual(code, 0)
        # Both blocks land on STDOUT: the usage/flags block and the
        # keys intro.
        self.assertIn(self.r._HELP_USAGE, out)
        self.assertIn(self.r._HELP_INTRO, out)

    def test_long_flag_prints_help_and_exits_zero(self):
        code, out = self._run_help(['browse-md', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('Usage: browse-md', out)

    def test_help_wins_over_positionals(self):
        # ``browse-md --help FILE`` prints help without touching the
        # (non-existent) file — no "no such file" death.
        code, out = self._run_help(
            ['browse-md', '--help', '/no/such/file.md'])
        self.assertEqual(code, 0)
        self.assertIn('Usage: browse-md', out)


class TestArgvValueFlag(unittest.TestCase):
    """``_pop_value_flag`` — extract the value-taking ``--root DIR`` from argv.

    The value-taking sibling of ``_pop_flag``: like the ``-h`` extraction it
    runs before the positionals are read and mutates ``sys.argv`` in place,
    but each occurrence consumes a VALUE — either the following token
    (``--root DIR``) or an inline ``--root=DIR`` — and the values are returned
    in argv order. Verified directly by patching ``sys.argv`` on the recipe.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._saved_argv = list(self.r.sys.argv)

    def tearDown(self):
        self.r.sys.argv[:] = self._saved_argv

    def _set_argv(self, argv):
        self.r.sys.argv[:] = argv

    def test_absent_returns_empty(self):
        self._set_argv(['browse-md', 'FILE.md'])
        self.assertEqual(self.r._pop_value_flag('--root'), [])
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_space_form_single(self):
        self._set_argv(['browse-md', '--root', '/a', 'FILE.md'])
        self.assertEqual(self.r._pop_value_flag('--root'), ['/a'])
        # Both the flag AND its value are removed; the positional survives.
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_equals_form_single(self):
        self._set_argv(['browse-md', '--root=/a', 'FILE.md'])
        self.assertEqual(self.r._pop_value_flag('--root'), ['/a'])
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_repeatable_mixed_forms_in_order(self):
        # Multiple occurrences (mixing both spellings) all pop; values come
        # back in argv order and the positional is left in place.
        self._set_argv(
            ['browse-md', '--root', '/a', '--root=/b', 'FILE.md',
             '--root', '/c'])
        self.assertEqual(self.r._pop_value_flag('--root'), ['/a', '/b', '/c'])
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_equals_empty_value_kept_for_caller(self):
        # ``--root=`` yields an empty-string value verbatim — rejecting it is
        # ``_resolve_roots``'s job (see TestResolveRoots), not this helper's.
        self._set_argv(['browse-md', '--root=', 'FILE.md'])
        self.assertEqual(self.r._pop_value_flag('--root'), [''])
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_trailing_bare_flag_dropped_no_value(self):
        # A trailing ``--root`` with no following token contributes no value
        # and is simply removed (no IndexError).
        self._set_argv(['browse-md', 'FILE.md', '--root'])
        self.assertEqual(self.r._pop_value_flag('--root'), [])
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])

    def test_does_not_consume_unrelated_args(self):
        self._set_argv(['browse-md', '--frobnicate', 'FILE.md'])
        self.assertEqual(self.r._pop_value_flag('--root'), [])
        self.assertEqual(
            self.r.sys.argv, ['browse-md', '--frobnicate', 'FILE.md'])

    def test_value_after_flag_taken_even_if_dash_prefixed(self):
        # ``--root`` consumes the very next token as its value unconditionally
        # (matching ``recipe_argv``'s ``--tty VALUE`` consumption), so a
        # ``-``-looking value isn't re-scanned as another option.
        self._set_argv(['browse-md', '--root', '--weird', 'FILE.md'])
        self.assertEqual(self.r._pop_value_flag('--root'), ['--weird'])
        self.assertEqual(self.r.sys.argv, ['browse-md', 'FILE.md'])


class TestResolveRoots(unittest.TestCase):
    """``_resolve_roots`` — raw ``--root`` values → abs existing directories.

    Each raw value is ``expanduser`` + ``abspath``-ed against the startup cwd;
    a non-directory is reported with ONE non-fatal stderr warning and dropped,
    the survivors kept in order (the resolution-priority order).
    """

    def setUp(self):
        self.r = _load_recipe()

    def _resolve(self, raws):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = self.r._resolve_roots(raws)
        return out, buf.getvalue()

    def test_existing_dir_kept_absolute(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            # ``d`` is already absolute; ``_resolve_roots`` abspaths (not
            # realpaths) so it is preserved verbatim.
            out, err = self._resolve([d])
            self.assertEqual(out, [os.path.abspath(d)])
            self.assertEqual(err, '')

    def test_empty_value_warns_and_is_dropped(self):
        # ``--root=`` must not silently become the cwd (``abspath('')`` IS
        # the cwd, which isdir accepts) — it warns and is dropped like any
        # other bad value.
        out, err = self._resolve([''])
        self.assertEqual(out, [])
        self.assertEqual(err, 'browse-md: --root: empty value\n')

    def test_nonexistent_warns_once_and_dropped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            bogus = os.path.join(d, 'nope')
            out, err = self._resolve([bogus])
            self.assertEqual(out, [])
            self.assertEqual(err.count('\n'), 1)
            self.assertIn('--root: not a directory: ' + bogus, err)

    def test_file_is_not_a_directory_warns(self):
        # A path that exists but is a FILE (not a dir) is rejected too.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, 'f.md')
            with open(f, 'w') as fh:
                fh.write('x')
            out, err = self._resolve([f])
            self.assertEqual(out, [])
            self.assertIn('--root: not a directory: ' + f, err)

    def test_mix_keeps_good_drops_bad_in_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, 'a')
            b = os.path.join(d, 'b')
            os.mkdir(a)
            os.mkdir(b)
            bogus = os.path.join(d, 'nope')
            out, err = self._resolve([a, bogus, b])
            self.assertEqual(out, [a, b])
            self.assertEqual(err.count('\n'), 1)

    def test_tilde_expanded(self):
        import tempfile
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {'HOME': home}):
                out, err = self._resolve(['~'])
            self.assertEqual(out, [os.path.abspath(home)])
            self.assertEqual(err, '')

    def test_relative_resolved_against_cwd(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, 'sub')
            os.mkdir(sub)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                out, err = self._resolve(['sub'])
            finally:
                os.chdir(cwd)
            self.assertEqual(out, [sub])
            self.assertEqual(err, '')


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
        self.r = _load_recipe()
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

    def test_dropped_lists_flag_is_unrecognised(self):
        # The ``-l`` / ``--list`` / ``--lists`` flag was removed with
        # list-item support — it now dies like any other unknown option.
        import tempfile
        with tempfile.NamedTemporaryFile(
                'w', suffix='.md', delete=False) as tmp:
            tmp.write('# H\n')
            tmp_path = tmp.name
        try:
            for flag in ('-l', '--list', '--lists'):
                with self.subTest(flag=flag):
                    self.r = _load_recipe()
                    self._set_argv(['browse-md', flag, tmp_path])
                    code, err = self._run_main_capture()
                    self.assertEqual(code, 2)
                    self.assertIn(f'unrecognised option: {flag}', err)
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

    The tree is built from ``md2ansi_lib.md2ansi_doc`` and the reference
    subtrees ride on ``md_doc`` — so both are hard dependencies.
    ``main()`` checks ``_md2ansi_doc`` and then ``md_doc`` before any
    parsing and exits 2 via ``_die`` when either is ``None`` — simulating
    an environment where the import failed. The stub ``browse_tui`` is
    already installed by the loader, and the gate fires before the
    Browser is ever constructed.
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

    def test_missing_doc_model_exits_two_with_message(self):
        import os
        # A real .md file so the gate is exercised, not the argv checks
        # (the gate runs first regardless, but a valid file ensures the
        # only reason main() can exit is the missing-dependency gate).
        tmp_path = self._real_md_file()
        try:
            self.r._md2ansi_doc = None
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
        # Mirror the doc-model gate for the ``md_doc`` hard dependency.
        # Leave ``_md2ansi_doc`` intact so the FIRST gate passes and we
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


class TestTextNodes(unittest.TestCase):
    """Loose body-text ``[text]`` leaves and the ``#text`` row-set rule.

    ``md2ansi_doc`` gives EVERY heading a ``#text`` first child
    (zero-width when the body is blank) plus a preamble node at
    ``tree[0]``; ``_build_nodes`` shows a text run only when it is
    non-empty AND has at least one heading sibling. The visible run maps
    onto a leaf ``Item`` (``[text]`` tag, ``dim`` style, no children),
    its preview slices to just that run, and the anchor flow keeps
    treating it correctly (title scan skips it; a line anchor resolves
    to it).

    Fixture has a loose run before BOTH the root's first heading and a
    nested heading, plus a leaf-chapter body that must NOT become a
    row::

        intro paragraph   <- line 0  (before the root's first heading)
        # Top             <- line 1
        body before sub   <- line 2  (before ``# Top``'s first sub-heading)
        ## Sub            <- line 3
        sub body          <- line 4  (leaf-chapter body — filtered)
    """

    FIXTURE = (
        'intro paragraph\n'   # line 0
        '# Top\n'             # line 1
        'body before sub\n'   # line 2
        '## Sub\n'            # line 3
        'sub body\n'          # line 4
    )

    def _build(self, text=None):
        r = _load_recipe()
        path = '/tmp/text-nodes.md'
        root, by_id = _build_tree(r, text or self.FIXTURE, path)
        return r, path, root, by_id

    # --- eager primary-file tree -----------------------------------------

    def test_root_first_child_is_dim_text_leaf(self):
        # The loose run before ``# Top`` is the root's FIRST child — a
        # leaf tagged ``[text]`` with the dim style, sitting ahead of
        # the heading it precedes.
        _r, path, root, _by_id = self._build()
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
        _r, _path, root, _by_id = self._build()
        top = root._children[1]
        self.assertEqual([(k.tag, k.title) for k in top._children],
                         [('text', 'body before sub'), ('h2', 'Sub')])
        self.assertEqual(top._children[0].kind, 'text')

    def test_leaf_chapter_body_is_not_a_row(self):
        # ``sub body`` under ``## Sub`` has no heading sibling — the
        # row-set rule filters it, so a leaf heading stays a leaf.
        _r, _path, root, _by_id = self._build()
        sub = root._children[1]._children[1]
        self.assertEqual(sub.title, 'Sub')
        self.assertEqual(sub._children, [])
        self.assertFalse(sub.has_children)

    def test_zero_width_run_is_not_a_row(self):
        # A heading with a blank body followed directly by a sub-heading
        # gets a ZERO-WIDTH text child from the library — filtered even
        # though it has a heading sibling.
        _r, _path, root, _by_id = self._build('# A\n## B\nbody\n')
        a = root._children[0]
        self.assertEqual([(k.tag, k.title) for k in a._children],
                         [('h2', 'B')])

    def test_text_leaf_carries_contract_fields(self):
        # ``_to_item`` stamps the hidden contract fields on a text node just
        # like a heading: byte_offset/byte_size verbatim from the M2A_Node,
        # the boundary-rule line_size, and the file back-reference.
        _r, path, root, _by_id = self._build()
        text_leaf = root._children[0]
        self.assertEqual(text_leaf.byte_offset, 0)
        # The run is just its own line ``intro paragraph\n`` (16 chars).
        self.assertEqual(text_leaf.byte_size, len('intro paragraph\n'))
        self.assertEqual(text_leaf.line_offset, 0)
        # Section ends at the heading it precedes (line 1) → one line.
        self.assertEqual(text_leaf.line_size, 1)
        self.assertEqual(text_leaf.file_path, path)

    def test_by_id_indexes_text_leaves(self):
        # ``by_id`` carries the visible text rows alongside the headings:
        # root + 2 headings + 2 visible text runs = 5 entries (the
        # filtered leaf-chapter run never enters the index).
        _r, _path, root, by_id = self._build()
        self.assertEqual(len(by_id), 5)
        self.assertEqual(root._children[1].tag, 'h1')
        self.assertEqual(root._children[1]._children[1].tag, 'h2')

    # --- preview ----------------------------------------------------------

    def test_text_leaf_preview_is_the_run(self):
        # ``get_preview`` slices the text node's own byte window — exactly
        # the loose run, nothing more.
        r, path, root, by_id = self._build()
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
        r, path, root, by_id = self._build()
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
        r, path, root, by_id = self._build()
        r._BY_ID = by_id
        r._BY_LINE, r._LINES_SORTED = r._build_line_index(by_id)
        self.assertEqual(r._resolve_anchor('0', path), ('content', path, 0))
        self.assertEqual(r._node_at_line(0).kind, 'text')

    def test_lone_text_child_does_not_auto_expand(self):
        # ``_lone_heading_child_id`` only cascades a sole HEADING child, so a
        # file-root whose only child is a text leaf never auto-expands. (Real
        # markdown can't actually produce a heading/root with a SOLE text
        # child — a text run only shows ahead of a sibling heading — so the
        # ``kind == 'heading'`` guard is the safety net; assert it directly on
        # a synthetic single-text-child root.)
        r, path, root, _by_id = self._build()
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
        # want to assert on (``_INPUT_FILES``, ``_ANCHOR``) have
        # already landed. Catch both ``SystemExit`` (error paths) and
        # ``AttributeError`` (success path past Browser construction).
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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
        self.r._run_source_command(ctx)
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

    def test_intro_text_plus_heading_returns_none(self):
        # A file with a [text] intro row AND one heading has TWO
        # children — no lone heading, no cascade.
        path = self._load('intro run\n# Only\n## Sub\n')
        root = self.r._FILES[path].file_root
        self.assertEqual([k.kind for k in root._children],
                         ['text', 'heading'])
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
        self.r = _load_recipe()
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


class TestMdRefFollowingWithRoot(unittest.TestCase):
    """``--root DIR`` extends reference resolution to extra base directories.

    Drives the real ``_reparse`` / ``get_children`` path (refs read from disk)
    with the recipe's ``_ROOTS`` global populated as ``main()`` would after
    parsing ``--root``. The flagship case: a document references ``target.md``
    by a bare relative token, but that file lives ONLY under a supplied root
    (not the document's own directory / cwd / git-root) — so without ``--root``
    it would silently fail to resolve, and with it the ref surfaces under the
    ``[links]`` References umbrella and expands into the referenced file.

    Fixtures live in a private temp dir (no ``.git``, so ``project_root`` falls
    back to the doc's own dir — labels are bare basenames):
      * ``docdir/main.md`` — references ``target.md`` (bare relative token),
        which does NOT exist beside it.
      * ``rootA/target.md`` — the only copy of ``target.md`` (carries a heading
        so the expanded ref shows structure).
    """

    def setUp(self):
        import os
        import tempfile
        self.r = _load_recipe()
        self.r.md_doc.clear_cache()
        self.r._MD_COLOR = False
        self.r._BROWSER = None
        self.dir = tempfile.mkdtemp()
        self.docdir = os.path.join(self.dir, 'docdir')
        self.rootA = os.path.join(self.dir, 'rootA')
        os.makedirs(self.docdir)
        os.makedirs(self.rootA)
        self.main_md = os.path.join(self.docdir, 'main.md')
        self._write(self.main_md,
                    '# Main\n'
                    'see target.md for the rest\n')
        self._write(os.path.join(self.rootA, 'target.md'),
                    '# Target heading\nbody\n')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        self.r.md_doc.clear_cache()

    def _write(self, path, text):
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def _reparse_with_roots(self, roots):
        # Populate the recipe state exactly as ``main()`` does before the first
        # ``_reparse``: the input file list, the resolved ``_ROOTS``, a clear
        # parse cache.
        self.r._INPUT_FILES = [(self.main_md, '')]
        self.r._ROOT_PATH = self.main_md
        self.r._ROOTS = list(roots)
        self.r.md_doc.clear_cache()
        self.r._reparse()

    def _umbrella(self, kids):
        umbrellas = [k for k in kids if k.tag == 'links']
        self.assertEqual(len(umbrellas), 1,
                         'expected exactly one References umbrella')
        return umbrellas[0]

    def _refs(self, file_path):
        umbrella = self._umbrella(self.r.get_children(('file', file_path)))
        return self.r.get_children(umbrella.id)

    # --- without --root: today's behavior (ref unresolvable) -------------

    def test_without_root_ref_does_not_resolve(self):
        # No ``--root``: ``target.md`` exists nowhere the defaults search, so
        # the file has no resolvable ref — no umbrella, no expansion arrow.
        self._reparse_with_roots([])
        root = self.r._FILES[self.main_md].file_root
        # The only structure is the lone h1; no References umbrella.
        kids = self.r.get_children(('file', self.main_md))
        self.assertFalse(any(k.tag == 'links' for k in kids),
                         'no umbrella without a resolvable ref')

    # --- with --root: the ref resolves via the supplied root -------------

    def test_root_only_ref_resolves_and_expands(self):
        # With ``--root rootA``, ``target.md`` resolves there: the file gains a
        # References umbrella, and expanding it yields the referenced file,
        # which itself drills into its heading.
        self._reparse_with_roots([self.rootA])
        root = self.r._FILES[self.main_md].file_root
        self.assertTrue(root.has_children)
        refs = self._refs(self.main_md)
        # Exactly one ref, resolved to rootA's copy (the only one on disk). The
        # label is project-anchored display text (here the abspath, since the
        # file lives outside the doc's project_root); ``md_abspath`` is the
        # canonical resolved path the resolution itself produced.
        self.assertEqual(len(refs), 1)
        self.assertEqual(
            refs[0].md_abspath,
            os.path.realpath(os.path.join(self.rootA, 'target.md')))
        # The ref node is a boundary doc; expanding it shows target's heading.
        target_node = refs[0]
        self.assertTrue(getattr(target_node, 'boundary', False))
        sub = self.r.get_children(target_node.id)
        self.assertEqual([(k.tag, k.title) for k in sub],
                         [('h1', 'Target heading')])

    def test_has_existing_ref_flips_with_root(self):
        # The cheap existence probe used by ``_reparse`` for the arrow flips
        # from False (no root) to True (root supplied) for the same file/text.
        text = '# Main\nsee target.md for the rest\n'
        self.r._ROOTS = []
        self.assertFalse(
            self.r._file_has_existing_ref(text, self.main_md))
        self.r._ROOTS = [self.rootA]
        self.assertTrue(
            self.r._file_has_existing_ref(text, self.main_md))

    # --- precedence: the document's own directory wins over a root -------

    def test_doc_dir_beats_root(self):
        # Same relative ref exists in BOTH the doc's own dir and a supplied
        # root → the doc's own directory wins (today's resolution order is
        # unchanged; the root is only an ADDED, lower-priority candidate).
        self._write(os.path.join(self.docdir, 'target.md'),
                    '# Local target\nlocal body\n')
        self._reparse_with_roots([self.rootA])
        refs = self._refs(self.main_md)
        # Resolved to the LOCAL copy beside the doc, not rootA's.
        self.assertEqual(len(refs), 1)
        self.assertEqual(
            refs[0].md_abspath,
            os.path.realpath(os.path.join(self.docdir, 'target.md')))
        # ...confirmed by its heading.
        sub = self.r.get_children(refs[0].id)
        self.assertEqual([(k.tag, k.title) for k in sub],
                         [('h1', 'Local target')])

    # --- precedence: first-listed root wins ------------------------------

    def test_first_root_wins_when_both_have_ref(self):
        # Two roots both contain the ref → the FIRST-listed root wins.
        rootB = os.path.join(self.dir, 'rootB')
        self._write(os.path.join(rootB, 'target.md'),
                    '# B target\nb body\n')
        # rootA already holds ``# Target heading``; list rootA first.
        self._reparse_with_roots([self.rootA, rootB])
        refs = self._refs(self.main_md)
        # Resolved to rootA (first-listed), not rootB.
        self.assertEqual(len(refs), 1)
        self.assertEqual(
            refs[0].md_abspath,
            os.path.realpath(os.path.join(self.rootA, 'target.md')))
        sub = self.r.get_children(refs[0].id)
        self.assertEqual([(k.tag, k.title) for k in sub],
                         [('h1', 'Target heading')])

    def test_second_root_used_when_first_lacks_ref(self):
        # The first root lacks the ref; resolution falls through to the second.
        empty_root = os.path.join(self.dir, 'empty')
        os.makedirs(empty_root)
        self._reparse_with_roots([empty_root, self.rootA])
        refs = self._refs(self.main_md)
        self.assertEqual(len(refs), 1)
        self.assertEqual(
            refs[0].md_abspath,
            os.path.realpath(os.path.join(self.rootA, 'target.md')))
        sub = self.r.get_children(refs[0].id)
        self.assertEqual([(k.tag, k.title) for k in sub],
                         [('h1', 'Target heading')])

    # --- nonexistent root is non-fatal (handled at parse time) -----------

    def test_nonexistent_root_does_not_break_resolution(self):
        # A bogus root never reaches ``_ROOTS`` (``_resolve_roots`` drops it),
        # so resolution against the surviving real root still works. We model
        # the post-parse state: only the valid root is in ``_ROOTS``.
        self._reparse_with_roots([self.rootA])  # bogus already filtered out
        refs = self._refs(self.main_md)
        self.assertEqual(len(refs), 1)
        self.assertEqual(
            refs[0].md_abspath,
            os.path.realpath(os.path.join(self.rootA, 'target.md')))

    # --- preview of the ref under the umbrella ---------------------------

    def test_ref_preview_renders_target_body(self):
        # The referenced file's preview (slice of its body) is available once
        # resolved through the root.
        self._reparse_with_roots([self.rootA])
        refs = self._refs(self.main_md)
        preview = self.r.get_preview(refs[0].id)
        self.assertIn('Target heading', preview)


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


class TestStdinDocument(unittest.TestCase):
    """``browse-md -`` reads ONE document from stdin (spec §3.3 / §3.7).

    ``main()`` slurps ``sys.stdin`` before the UI starts and parses the
    text through the same ``_reparse`` pipeline as a file, stamping the
    sentinel path ``_STDIN_PATH`` and a ``-`` root title. These
    tests drive ``main()`` with a stubbed stdin and the no-op
    ``browse_tui`` stub (so the run loop is never reached — construction
    raises ``AttributeError`` past which all the state we assert on has
    already landed), then inspect the module globals / the constructed
    Browser config exactly like the file-mode argv tests above.
    """

    def setUp(self):
        self.r = _load_recipe()

    def _run_main(self, stdin, argv=('browse-md', '-')):
        """Drive ``main()`` with ``stdin`` piped in; return Browser.

        ``stdin`` is either the text to slurp (wrapped in a minimal
        object whose ``.read()`` yields it, matching the recipe's
        ``sys.stdin.read()`` call) OR a ready-made stdin stand-in (any
        object that already has a ``.read`` method — e.g.
        ``_RaiseOnRead`` for the modes that must NOT touch stdin).
        ``SystemExit`` (error paths) and ``AttributeError`` (the stubbed
        Browser lacking ``run``) are swallowed; the returned value is
        ``self.r._BROWSER`` (set just before ``b.run()``), or ``None`` if
        main exited earlier.
        """
        import contextlib
        import io

        class _FakeStdin:
            def __init__(self, text):
                self._text = text

            def read(self):
                return self._text

        fake = stdin if hasattr(stdin, 'read') else _FakeStdin(stdin)
        saved_stdin = self.r.sys.stdin
        self.r.sys.argv[:] = list(argv)
        buf = io.StringIO()
        try:
            self.r.sys.stdin = fake
            with contextlib.redirect_stderr(buf):
                try:
                    self.r.main()
                except (SystemExit, AttributeError):
                    pass
        finally:
            self.r.sys.stdin = saved_stdin
        self._stderr = buf.getvalue()
        return self.r._BROWSER

    # -- piped document: tree + preview + title -----------------------

    def test_input_files_is_the_stdin_sentinel(self):
        self._run_main('# Title\n## Section\nbody\n')
        self.assertEqual(self.r._INPUT_FILES, [(self.r._STDIN_PATH, '')])
        self.assertEqual(self.r._STDIN_TEXT, '# Title\n## Section\nbody\n')
        # No anchor for a lone ``-``.
        self.assertEqual(self.r._ANCHOR, '')

    def test_root_title_is_dash(self):
        self._run_main('# Title\n## Section\nbody\n')
        fs = self.r._FILES[self.r._STDIN_PATH]
        self.assertEqual(fs.file_root.title, '-')

    def test_heading_tree_built_from_piped_text(self):
        self._run_main('# Title\n## Section\nbody\n')
        root_id = ('file', self.r._STDIN_PATH)
        # Top-level entries: exactly the one stdin doc.
        tops = self.r.get_children(None)
        self.assertEqual([t.id for t in tops], [root_id])
        # The file root's children: the single h1.
        kids = self.r.get_children(root_id)
        self.assertEqual([k.tag for k in kids], ['h1'])
        # ...and the h1's child: the h2.
        sub = self.r.get_children(kids[0].id)
        self.assertEqual([s.tag for s in sub], ['h2'])

    def test_preview_of_stdin_root_is_full_text(self):
        # md2ansi colouring off so the preview is the raw body verbatim.
        self.r._MD_COLOR = False
        text = '# Title\n## Section\nbody\n'
        self._run_main(text)
        preview = self.r.get_preview(('file', self.r._STDIN_PATH))
        self.assertEqual(preview, text)

    def test_preview_of_heading_is_its_section_slice(self):
        self.r._MD_COLOR = False
        self._run_main('# Title\n## Section\nbody\n')
        kids = self.r.get_children(('file', self.r._STDIN_PATH))
        h1_id = kids[0].id
        # The h1 spans the whole document (it's the only top-level
        # heading), so its slice is the full body.
        self.assertEqual(self.r.get_preview(h1_id), '# Title\n## Section\nbody\n')

    def test_single_doc_auto_expands_like_single_file(self):
        # One stdin doc behaves like a single file: main() posts exactly
        # one expand on the file root (the lone-heading cascade is the
        # on_expand hook's job, not recorded by the stub).
        b = self._run_main('# Title\n## Section\nbody\n')
        ids = [c[0] for c in b.expand_calls]
        self.assertEqual(ids, [('file', self.r._STDIN_PATH)])
        # initial_scope stays None — we auto-expand rather than scope in.
        self.assertIsNone(b.config.initial_scope)
        self.assertIsNone(b.config.root_id)

    # -- empty stdin behaves like an empty .md file -------------------

    def test_empty_stdin_is_an_empty_document(self):
        # Match an empty file: a root with no children, has_children
        # False (same assertions as TestEdgeCases.test_empty_file, but
        # reached through the stdin path).
        self._run_main('')
        self.assertEqual(self.r._STDIN_TEXT, '')
        fs = self.r._FILES[self.r._STDIN_PATH]
        self.assertEqual(fs.file_root._children, [])
        self.assertFalse(fs.file_root.has_children)
        self.assertEqual(self.r.get_children(('file', self.r._STDIN_PATH)), [])

    # -- sentinel collision is harmless --------------------------------

    def test_piped_content_wins_over_same_named_disk_file(self):
        # Interception-before-open: with a REAL file sitting at the
        # sentinel path (a file literally named like the sentinel in
        # cwd), stdin mode still serves the piped text — the sentinel
        # never reaches ``open()``. Pins the seam's "the sentinel is
        # intercepted before any filesystem call" guarantee.
        import os
        import tempfile
        self.r._MD_COLOR = False
        piped = '# Piped\nbody\n'
        with tempfile.TemporaryDirectory() as tmp:
            disk = os.path.join(tmp, os.path.basename(self.r._STDIN_PATH))
            with open(disk, 'w', encoding='utf-8') as f:
                f.write('# Disk\nWRONG\n')
            saved = self.r._STDIN_PATH
            self.r._STDIN_PATH = disk
            try:
                self._run_main(piped)
                fs = self.r._FILES[disk]
                self.assertEqual(fs.file_root.title, '-')
                self.assertEqual(
                    self.r.get_preview(('file', disk)), piped)
            finally:
                self.r._STDIN_PATH = saved

    # -- bare / FILE modes untouched ----------------------------------

    def test_bare_invocation_still_browses_cwd(self):
        # No ``-``: the recipe browses ``.`` and never touches stdin.
        # We drive a no-positional argv in a temp dir holding one .md so
        # the directory expansion has something to find.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'a.md'), 'w') as f:
                f.write('# A\n')
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                # ``read()`` would raise if main() touched stdin in this
                # mode — it must not.
                self._run_main(
                    _RaiseOnRead(), argv=('browse-md',))
            finally:
                os.chdir(cwd)
        # stdin was never consumed; the input is the cwd's file, not the
        # sentinel.
        self.assertIsNone(self.r._STDIN_TEXT)
        self.assertNotIn(self.r._STDIN_PATH,
                         [p for p, _ in self.r._INPUT_FILES])
        self.assertEqual(len(self.r._INPUT_FILES), 1)

    def test_file_mode_untouched(self):
        # ``browse-md FILE`` never touches stdin and never stamps the
        # sentinel.
        import os
        import tempfile
        f = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8')
        f.write('# F\n## S\n')
        f.close()
        self.addCleanup(os.unlink, f.name)
        self._run_main(_RaiseOnRead(), argv=('browse-md', f.name))
        self.assertIsNone(self.r._STDIN_TEXT)
        self.assertEqual(self.r._INPUT_FILES, [(f.name, '')])

    # -- auto-detect: a piped (non-tty) stdin synthesizes ``-`` --------

    def test_piped_no_positional_engages_stdin_without_dash(self):
        # ``cmd | browse-md`` (no explicit ``-``): a non-tty fd 0 makes
        # main() synthesize ``-`` and browse the piped document, exactly
        # as if ``-`` had been typed.
        with _piped_stdin():
            self._run_main('# Title\n## Section\nbody\n', argv=('browse-md',))
        self.assertEqual(self.r._INPUT_FILES, [(self.r._STDIN_PATH, '')])
        self.assertEqual(self.r._STDIN_TEXT, '# Title\n## Section\nbody\n')
        self.assertEqual(self.r._ANCHOR, '')

    def test_piped_with_file_positional_errors(self):
        # ``cmd | browse-md FILE``: the synthesized ``-`` joins the
        # positional list, so it falls into the FILE branch where ``-``
        # fails to resolve as a path and main() dies (exit 2). stdin is
        # never read (the lone-``-`` slurp is only the ``== ['-']`` case).
        import contextlib
        import io
        import os
        import tempfile
        f = tempfile.NamedTemporaryFile(
            'w', suffix='.md', delete=False, encoding='utf-8')
        f.write('# F\n')
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.r.sys.argv[:] = ['browse-md', f.name]
        self.r.sys.stdin = _RaiseOnRead()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                with _piped_stdin():
                    self.r.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('no such file or directory: -', buf.getvalue())
        self.assertIsNone(self.r._STDIN_TEXT)

    def test_piped_empty_is_an_empty_document(self):
        # A non-tty empty stdin synthesizes ``-`` and flows into the
        # existing empty handling: an empty document, no crash. No
        # emptiness special-casing.
        with _piped_stdin():
            self._run_main('', argv=('browse-md',))
        self.assertEqual(self.r._STDIN_TEXT, '')
        fs = self.r._FILES[self.r._STDIN_PATH]
        self.assertEqual(fs.file_root._children, [])
        self.assertFalse(fs.file_root.has_children)

    # -- '-#anchor' is out of scope: rejected, never half-works -------

    def test_dash_anchor_rejected_as_unrecognised_option(self):
        # ``-#section`` is NOT a lone ``-`` and starts with ``-``, so the
        # leftover-option scan rejects it (exit 2) before any stdin read.
        # It must never half-work as a stdin deep-link.
        import contextlib
        import io
        self.r.sys.argv[:] = ['browse-md', '-#section']
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                self.r.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('unrecognised option: -#section', buf.getvalue())
        # Stdin was never stamped.
        self.assertIsNone(self.r._STDIN_TEXT)

    # -- --tty - is the framework UI flag, not stdin / not an option --

    def test_tty_dash_value_is_not_an_option_or_positional(self):
        # ``--tty -`` / ``--tty=-`` is the framework's UI-over-std-streams
        # flag (auto-detected by Browser.run(), left in sys.argv). The
        # recipe must drop it before the leftover-option scan — NOT die
        # "unrecognised option: --tty" — and must not read stdin: with no
        # real positional it falls through to bare mode (browse cwd). A
        # ``--tty /dev/pts/N`` device path is dropped too, not taken as a
        # FILE positional.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'a.md'), 'w') as f:
                f.write('# A\n')
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                for argv in (('browse-md', '--tty', '-'),
                             ('browse-md', '--tty=-'),
                             ('browse-md', '--tty', '/dev/pts/9')):
                    self.r = _load_recipe()
                    # _RaiseOnRead fires if main() touched stdin here.
                    self._run_main(_RaiseOnRead(), argv=argv)
                    self.assertEqual(self._stderr, '', argv)
                    self.assertIsNone(self.r._STDIN_TEXT, argv)
                    # Bare mode: the cwd's file is the input, not the
                    # sentinel; the device path never became a positional.
                    self.assertNotIn(
                        self.r._STDIN_PATH,
                        [p for p, _ in self.r._INPUT_FILES], argv)
                    self.assertEqual(len(self.r._INPUT_FILES), 1, argv)
            finally:
                os.chdir(cwd)

    def test_tty_dash_does_not_disarm_real_stdin_dash(self):
        # Stripping ``--tty -`` must leave a genuine positional ``-``
        # reading stdin: ``browse-md --tty - -`` still slurps the doc.
        self._run_main('# Piped\n## S\n', argv=('browse-md', '--tty', '-', '-'))
        self.assertEqual(self.r._INPUT_FILES, [(self.r._STDIN_PATH, '')])
        self.assertEqual(self.r._STDIN_TEXT, '# Piped\n## S\n')

    # -- V degrades gracefully on the stdin document -------------------

    def test_view_source_flashes_on_stdin_document(self):
        # With the stdin doc active, ``V`` (on-disk source) has no file
        # to page: it flashes and runs nothing. Built-in v/e (preview
        # text) are unaffected, and ``E`` edits the in-memory copy
        # through its own pipeline (TestEditSectionStdin).
        self._run_main('# Title\n## Section\nbody\n')
        root = _SrcItem(id=('file', self.r._STDIN_PATH), kind='root')
        ctx = _SrcCmdCtx(targets=[root])
        self.r._run_source_command(ctx)
        self.assertEqual(ctx.calls, [],
                         'no external tool should run for stdin')
        self.assertEqual(len(ctx.flashes), 1)
        self.assertIn('stdin', ctx.flashes[0])

    # -- stdin doc reference suppression: on without --root, off with it --

    def test_stdin_without_root_suppresses_references(self):
        # Without ``--root``, a piped document's relative ``.md`` refs are
        # fully suppressed (today's behavior): the stdin root has no
        # References umbrella even when the referenced file exists in cwd.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'target.md'), 'w') as f:
                f.write('# Target\n')
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                # Reload so ``_STDIN_PATH`` is anchored under this cwd (where
                # ``target.md`` exists) — proving suppression, not a failed
                # resolve, is what keeps the umbrella away.
                self.r = _load_recipe()
                self._run_main('# Piped\nsee target.md\n')
                root_id = ('file', self.r._STDIN_PATH)
                kids = self.r.get_children(root_id)
                self.assertFalse(any(k.tag == 'links' for k in kids),
                                 'stdin refs must stay suppressed without --root')
                fs = self.r._FILES[self.r._STDIN_PATH]
                # The arrow comes only from the h1, never from a ref.
                self.assertEqual([k.tag for k in kids], ['h1'])
            finally:
                os.chdir(cwd)

    def test_stdin_with_root_lifts_suppression_and_resolves(self):
        # With ``--root DIR``, the piped doc's refs resolve against the
        # supplied root: the References umbrella appears and expanding it
        # yields the referenced file (cross-file expansion works).
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'root')
            os.mkdir(root)
            with open(os.path.join(root, 'target.md'), 'w') as f:
                f.write('# Target heading\nbody\n')
            # ``target.md`` is NOT in cwd (the repo root): only the supplied
            # root can resolve it, so this exercises the root, not a fallback.
            self.r = _load_recipe()
            self._run_main(
                '# Piped\nsee target.md\n',
                argv=('browse-md', '--root', root, '-'))
            # The supplied root landed in ``_ROOTS``.
            self.assertEqual(self.r._ROOTS, [root])
            root_id = ('file', self.r._STDIN_PATH)
            kids = self.r.get_children(root_id)
            umbrellas = [k for k in kids if k.tag == 'links']
            self.assertEqual(len(umbrellas), 1,
                             'stdin doc should gain a References umbrella')
            refs = self.r.get_children(umbrellas[0].id)
            # The ref resolved against the supplied root (its abspath; the
            # display label may be the abspath since it lies outside cwd/root).
            self.assertEqual(len(refs), 1)
            self.assertEqual(
                refs[0].md_abspath,
                os.path.realpath(os.path.join(root, 'target.md')))
            # Cross-file expansion: the ref drills into target's heading.
            sub = self.r.get_children(refs[0].id)
            self.assertEqual([(k.tag, k.title) for k in sub],
                             [('h1', 'Target heading')])

    def test_auto_detected_stdin_composes_with_root(self):
        # ``cmd | browse-md --root DIR`` (no explicit ``-``): the
        # behavior flag is popped first, fd 0 is non-tty, so ``-`` is
        # synthesized and the piped doc is browsed with the root flag
        # still in effect.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'root')
            os.mkdir(root)
            with open(os.path.join(root, 'target.md'), 'w') as f:
                f.write('# Target heading\nbody\n')
            self.r = _load_recipe()
            with _piped_stdin():
                self._run_main(
                    '# Piped\nsee target.md\n',
                    argv=('browse-md', '--root', root))
            # Auto-detected stdin mode AND the flag composed.
            self.assertEqual(self.r._INPUT_FILES, [(self.r._STDIN_PATH, '')])
            self.assertEqual(self.r._ROOTS, [root])
            # The root resolved the piped doc's ref (umbrella present).
            kids = self.r.get_children(('file', self.r._STDIN_PATH))
            self.assertTrue(any(k.tag == 'links' for k in kids),
                            '--root must still lift ref suppression')

    def test_stdin_with_root_preview_of_ref(self):
        # The referenced file's preview is reachable from the stdin doc once a
        # root resolves it.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'root')
            os.mkdir(root)
            with open(os.path.join(root, 'target.md'), 'w') as f:
                f.write('# Target heading\ntarget body\n')
            self.r = _load_recipe()
            self.r._MD_COLOR = False
            self._run_main(
                '# Piped\nsee target.md\n',
                argv=('browse-md', '--root', root, '-'))
            root_id = ('file', self.r._STDIN_PATH)
            umbrella = [k for k in self.r.get_children(root_id)
                        if k.tag == 'links'][0]
            ref = self.r.get_children(umbrella.id)[0]
            self.assertIn('Target heading', self.r.get_preview(ref.id))

    def test_stdin_with_nonexistent_root_warns_non_fatal(self):
        # A bogus ``--root`` warns to stderr but does not abort: the stdin
        # document still loads. With no valid root the refs stay suppressed.
        self.r = _load_recipe()
        self._run_main(
            '# Piped\nsee target.md\n',
            argv=('browse-md', '--root', '/no/such/dir/here', '-'))
        self.assertIn('--root: not a directory: /no/such/dir/here',
                      self._stderr)
        # Non-fatal: the stdin doc loaded normally.
        self.assertEqual(self.r._INPUT_FILES, [(self.r._STDIN_PATH, '')])
        self.assertEqual(self.r._ROOTS, [])
        root_id = ('file', self.r._STDIN_PATH)
        kids = self.r.get_children(root_id)
        self.assertFalse(any(k.tag == 'links' for k in kids))

    def test_root_equals_form_resolved_relative_to_cwd(self):
        # ``--root=DIR`` (the inline form) is accepted, and a RELATIVE DIR is
        # resolved against the startup cwd.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, 'root'))
            with open(os.path.join(tmp, 'root', 'target.md'), 'w') as f:
                f.write('# T\n')
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.r = _load_recipe()
                self._run_main(
                    '# Piped\nsee target.md\n',
                    argv=('browse-md', '--root=root', '-'))
            finally:
                os.chdir(cwd)
            self.assertEqual(self.r._ROOTS, [os.path.join(tmp, 'root')])


class _RaiseOnRead:
    """A stdin stand-in whose ``read()`` raises — proves stdin is NOT
    consumed in the non-stdin invocation modes (bare / FILE)."""

    def read(self):  # pragma: no cover - only hit on a regression
        raise AssertionError('sys.stdin.read() called outside stdin mode')


# ---- section editing (E): capture + apply chain + action flow --------------


class TestCaptureExtent(unittest.TestCase):
    """``_capture_extent`` — the at-action-time address of a node's extent."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _capture(self, text, line):
        _root, by_id = _build_tree(self.r, text, '/x.md')
        return self.r._capture_extent(text, by_id[('content', '/x.md', line)])

    def test_heading_fields(self):
        import hashlib
        text = '# A\nbody\n# B\nmore\n'
        cap = self._capture(text, 0)
        self.assertEqual(cap.kind, 'heading')
        self.assertEqual(cap.level, 1)
        self.assertEqual(cap.extent, '# A\nbody\n')
        self.assertEqual((cap.a, cap.b), (1, 2))
        self.assertEqual(cap.hash,
                         hashlib.sha256(b'# A\nbody\n').hexdigest())

    def test_range_matches_library_slice(self):
        # The captured 1-based ``a..b`` names EXACTLY the extent slice
        # under the library's ``#a-b`` range query — the invariant the
        # apply chain's range fallback rests on.
        text = '# A\nbody\n## S\nnested\n# B\nmore\n'
        cap = self._capture(text, 0)
        t = self.r._md2ansi_resolve(text, f'#{cap.a}-{cap.b}')
        self.assertEqual(text[t.start:t.end], cap.extent)

    def test_extent_to_eof_without_trailing_newline(self):
        text = '# A\nbody\n# B\nmore'    # no trailing newline
        cap = self._capture(text, 2)
        self.assertEqual(cap.extent, '# B\nmore')
        self.assertEqual((cap.a, cap.b), (3, 4))
        t = self.r._md2ansi_resolve(text, f'#{cap.a}-{cap.b}')
        self.assertEqual(text[t.start:t.end], cap.extent)

    def test_text_run(self):
        text = '# T\nintro\n\n## S\nbody\n'
        cap = self._capture(text, 1)
        self.assertEqual(cap.kind, 'text')
        self.assertEqual(cap.extent, 'intro\n\n')
        self.assertEqual((cap.a, cap.b), (2, 3))

    def test_root_kind_covers_whole_document(self):
        text = 'preamble\n# A\nbody\n'
        _root, by_id = _build_tree(self.r, text, '/x.md')
        cap = self.r._capture_extent(text, by_id[('file', '/x.md')])
        self.assertEqual(cap.kind, 'root')
        self.assertEqual(cap.extent, text)


class TestApplySplice(unittest.TestCase):
    """``_apply_splice`` — the hash→range apply chain (the spec's matrix)."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _cap(self, text, line):
        _root, by_id = _build_tree(self.r, text, '/x.md')
        return self.r._capture_extent(text, by_id[('content', '/x.md', line)])

    def _root_cap(self, text):
        _root, by_id = _build_tree(self.r, text, '/x.md')
        return self.r._capture_extent(text, by_id[('file', '/x.md')])

    # -- edit (where='replace') ---------------------------------------

    def test_hash_hit_unchanged_document(self):
        text = '# A\nbody\n# B\nmore\n'
        cap = self._cap(text, 0)
        self.assertEqual(self.r._apply_splice(text, cap, '# A\nnew\n'),
                         '# A\nnew\n# B\nmore\n')

    def test_other_region_shift_then_hash_hit(self):
        # OTHER regions changed underneath the editor — the section moved
        # down; the full-hash query still re-addresses it transparently.
        text = '# A\nbody\n# B\nmore\n'
        cap = self._cap(text, 2)                 # capture '# B\nmore\n'
        shifted = '# Z\nzzz zzz\n' + text        # B no longer at lines 3-4
        self.assertEqual(
            self.r._apply_splice(shifted, cap, '# B\nnew\n'),
            '# Z\nzzz zzz\n# A\nbody\n# B\nnew\n')

    def test_duplicate_sections_range_fallback(self):
        # Two byte-identical sections: the hash query is ambiguous, but
        # the range fallback proves the ORIGINAL position still holds the
        # captured bytes and edits exactly that occurrence.
        text = '# A\nbody\n# A\nbody\n'
        cap = self._cap(text, 2)                 # the SECOND duplicate
        self.assertEqual(
            self.r._apply_splice(text, cap, '# A2\nnew\n'),
            '# A\nbody\n# A2\nnew\n')

    def test_duplicate_plus_shift_returns_none(self):
        # Duplicates AND a shift: hash ambiguous, range holds different
        # bytes — genuinely unresolvable, the caller's retry dialog.
        text = '# A\nbody\n# A\nbody\n'
        cap = self._cap(text, 0)
        self.assertIsNone(
            self.r._apply_splice('# P\npre\n' + text, cap, '# A2\nX\n'))

    def test_target_edited_underneath_returns_none(self):
        # The captured section ITSELF changed: hash misses, and the range's
        # current bytes hash differently → refuse rather than overwrite.
        text = '# A\nbody\n# B\nmore\n'
        cap = self._cap(text, 0)
        self.assertIsNone(self.r._apply_splice(
            '# A\nCHANGED\n# B\nmore\n', cap, '# A\nnew\n'))

    def test_text_run_range_primary(self):
        # A text run has no hash address — the range check IS its primary.
        text = '# T\nintro\n## S\nbody\n'
        cap = self._cap(text, 1)
        self.assertEqual(cap.kind, 'text')
        self.assertEqual(self.r._apply_splice(text, cap, 'INTRO\n'),
                         '# T\nINTRO\n## S\nbody\n')

    def test_text_run_shifted_returns_none(self):
        text = '# T\nintro\n## S\nbody\n'
        cap = self._cap(text, 1)
        self.assertIsNone(
            self.r._apply_splice('# P\npre\n' + text, cap, 'INTRO\n'))

    def test_root_replace_whole_document(self):
        text = '# A\nbody\n'
        cap = self._root_cap(text)
        self.assertEqual(self.r._apply_splice(text, cap, '# N\nnew\n'),
                         '# N\nnew\n')

    # -- insert relations (the shared surface the insert flow reuses) --

    def test_insert_after_via_hash(self):
        text = '# A\nbody\n# B\nmore\n'
        cap = self._cap(text, 0)
        self.assertEqual(
            self.r._apply_splice(text, cap, '# N\nnew\n', where='after'),
            '# A\nbody\n# N\nnew\n# B\nmore\n')

    def test_insert_before_via_hash_survives_shift(self):
        text = '# A\nbody\n# B\nmore\n'
        cap = self._cap(text, 2)
        self.assertEqual(
            self.r._apply_splice('# Z\nzzz\n' + text, cap, '# N\nnew\n',
                                 where='before'),
            '# Z\nzzz\n# A\nbody\n# N\nnew\n# B\nmore\n')

    def test_insert_first_on_duplicates_uses_hlevel_query(self):
        # Duplicate sections force the range fallback; a child insert can't
        # splice 'first' on a range target, so the chain switches to the
        # (range-verified) unique ``#h<level>:<line>`` query.
        text = '# A\nbody\n# A\nbody\n'
        cap = self._cap(text, 2)                 # the SECOND duplicate
        self.assertEqual(
            self.r._apply_splice(text, cap, '## C\nchild\n', where='first'),
            '# A\nbody\n# A\nbody\n## C\nchild\n')

    def test_insert_first_on_root_lands_after_preamble(self):
        text = 'preamble\n# A\nbody\n'
        cap = self._root_cap(text)
        self.assertEqual(
            self.r._apply_splice(text, cap, '# N\nnew\n', where='first'),
            'preamble\n# N\nnew\n# A\nbody\n')


class _EditCtx:
    """Recorder ctx for the ``E`` / ``a`` (edit / insert) flow tests.

    Mirrors the Context surface the actions touch — ``cursor``,
    ``run_external`` (the ``$EDITOR`` seam: ``editor(path, call_no)``
    plays the user's edit by rewriting the temp file, returning the
    editor's exit code, ``None`` meaning 0), ``confirm`` (scripted
    answers, ``None`` when the script runs out — the headless default),
    ``insert`` (records the marker-mode entry and stashes ``on_confirm``
    for the test to fire), ``cursor_to`` and ``refresh`` (the latter
    invoking ``on_complete`` synchronously, as the real Pending would
    once the reload lands), plus ``flash`` / ``log`` recorders.
    ``selected`` exists (and must stay ignored by ``E``) so the
    selection-ignored test can populate it. ``select`` /
    ``clear_selection`` record the move flow's row highlight.
    """

    def __init__(self, cursor=None, editor=None, confirms=()):
        self.cursor = cursor
        self.selected = []
        self.editor = editor or (lambda path, call_no: None)
        self.confirms = list(confirms)
        self.calls = []              # run_external argv snapshots
        self.flashes = []
        self.logs = []
        self.refreshes = 0
        self.confirm_prompts = []
        self.cursor_tos = []
        self.insert_labels = []
        self.on_confirm = None       # stashed by ``insert``
        self.selects = []            # (ids, replace) from ``select``
        self.clear_selections = 0

    def run_external(self, cmd):
        self.calls.append(list(cmd))
        rc = self.editor(cmd[-1], len(self.calls))
        return 0 if rc is None else rc

    def confirm(self, message, buttons=('&Yes', '&No'), *, title=None,
                delay_interaction=False):
        self.confirm_prompts.append(message)
        return self.confirms.pop(0) if self.confirms else None

    def insert(self, label, on_confirm):
        self.insert_labels.append(label)
        self.on_confirm = on_confirm

    def select(self, ids, replace=False):
        self.selects.append((list(ids), replace))

    def clear_selection(self):
        self.clear_selections += 1

    def flash(self, text, log=False):
        self.flashes.append(text)
        if log:
            self.logs.append(text)

    def log(self, text):
        self.logs.append(text)

    def refresh(self, id=None, on_complete=None):
        self.refreshes += 1
        if on_complete is not None:
            on_complete()

    def cursor_to(self, id, on_complete=None):
        self.cursor_tos.append(id)


def _rewrite(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


class TestEditSectionAction(unittest.TestCase):
    """``E`` (``_action_edit_section``) — the editor + apply-chain flow.

    Real per-file state (an on-disk fixture parsed via ``_reparse``), a
    recorder ctx whose ``run_external`` plays the editor by rewriting the
    temp file. Asserts on the resulting FILE bytes plus the recipe-log /
    flash outcomes.
    """

    _DOC = ('# Alpha\nalpha body\n'
            '## Sub\nsub body\n'
            '# Beta\nbeta body\n')
    # Beta sits at 0-based line 4 → capture lines 5-6.
    _BETA = '# Beta\nbeta body\n'

    def setUp(self):
        import tempfile
        self.r = _load_recipe()
        fd, self.path = tempfile.mkstemp(suffix='.md')
        os.close(fd)
        _rewrite(self.path, self._DOC)
        self.r._INPUT_FILES = [(self.path, '')]
        self.r._reparse()
        self._editor_saved = os.environ.get('EDITOR')
        os.environ['EDITOR'] = 'fake-ed'

    def tearDown(self):
        if self._editor_saved is None:
            os.environ.pop('EDITOR', None)
        else:
            os.environ['EDITOR'] = self._editor_saved
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _item(self, line):
        return self.r._FILES[self.path].by_id[('content', self.path, line)]

    def test_heading_edit_rewrites_file(self):
        payloads = []

        def ed(tmp, n):
            payloads.append(_read(tmp))
            _rewrite(tmp, '# Beta\nEDITED\n')

        ctx = _EditCtx(cursor=self._item(4), editor=ed)
        self.r._action_edit_section(ctx)
        # The editor received exactly the section's extent…
        self.assertEqual(payloads, [self._BETA])
        # …the splice landed on disk, everything else byte-identical…
        self.assertEqual(_read(self.path),
                         '# Alpha\nalpha body\n## Sub\nsub body\n'
                         '# Beta\nEDITED\n')
        # …the editor ran on a temp .md (not the source file), since
        # unlinked; the mutation was logged and the tree refreshed.
        tmp_arg = ctx.calls[0][-1]
        self.assertNotEqual(tmp_arg, self.path)
        self.assertFalse(os.path.exists(tmp_arg))
        self.assertEqual(ctx.calls[0][0], 'fake-ed')
        self.assertEqual(ctx.refreshes, 1)
        self.assertTrue(any('lines 5-6' in line for line in ctx.logs),
                        msg=f'logs: {ctx.logs}')

    def test_unchanged_content_cancels(self):
        ctx = _EditCtx(cursor=self._item(4))     # editor leaves temp as-is
        self.r._action_edit_section(ctx)
        self.assertEqual(_read(self.path), self._DOC)
        self.assertEqual(ctx.refreshes, 0)
        self.assertEqual(ctx.logs, [])
        self.assertIn('edit cancelled (content unchanged)', ctx.flashes)

    def test_empty_content_cancels(self):
        ctx = _EditCtx(cursor=self._item(4),
                       editor=lambda tmp, n: _rewrite(tmp, ' \n'))
        self.r._action_edit_section(ctx)
        self.assertEqual(_read(self.path), self._DOC)
        self.assertIn('edit cancelled (empty content)', ctx.flashes)

    def test_editor_nonzero_exit_cancels(self):
        def ed(tmp, n):
            _rewrite(tmp, '# Beta\nEDITED\n')
            return 1
        ctx = _EditCtx(cursor=self._item(4), editor=ed)
        self.r._action_edit_section(ctx)
        self.assertEqual(_read(self.path), self._DOC)
        self.assertIn('edit cancelled (editor exited non-zero)', ctx.flashes)

    def test_selection_is_ignored(self):
        # A populated multi-select must not widen the edit: only the
        # CURSOR node's extent goes to the editor, only it is replaced.
        payloads = []

        def ed(tmp, n):
            payloads.append(_read(tmp))
            _rewrite(tmp, '# Beta\nEDITED\n')

        ctx = _EditCtx(cursor=self._item(4), editor=ed)
        ctx.selected = [self._item(0), self._item(2), self._item(4)]
        self.r._action_edit_section(ctx)
        self.assertEqual(payloads, [self._BETA])
        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(_read(self.path),
                         '# Alpha\nalpha body\n## Sub\nsub body\n'
                         '# Beta\nEDITED\n')

    def test_shift_underneath_applies_by_hash(self):
        # While the editor is open, OTHER regions of the file change on
        # disk (a new section prepended). The apply chain re-reads the
        # file and hash-addresses the section at its new position.
        def ed(tmp, n):
            _rewrite(self.path, '# Zero\nzero body\n' + self._DOC)
            _rewrite(tmp, '# Beta\nEDITED\n')

        ctx = _EditCtx(cursor=self._item(4), editor=ed)
        self.r._action_edit_section(ctx)
        self.assertEqual(_read(self.path),
                         '# Zero\nzero body\n'
                         '# Alpha\nalpha body\n## Sub\nsub body\n'
                         '# Beta\nEDITED\n')
        self.assertEqual(ctx.refreshes, 1)

    def test_conflict_cancel_leaves_file_alone(self):
        # The target section ITSELF changed on disk underneath the editor
        # → both addressing steps fail → dialog; "Cancel edit" (falsy)
        # discards without splicing.
        conflicted = ('# Alpha\nalpha body\n## Sub\nsub body\n'
                      '# Beta\nDIVERGED\n')

        def ed(tmp, n):
            _rewrite(self.path, conflicted)
            _rewrite(tmp, '# Beta\nEDITED\n')

        ctx = _EditCtx(cursor=self._item(4), editor=ed, confirms=[False])
        self.r._action_edit_section(ctx)
        self.assertEqual(_read(self.path), conflicted)
        self.assertEqual(len(ctx.confirm_prompts), 1)
        self.assertEqual(ctx.refreshes, 0)
        self.assertIn('edit cancelled', ctx.flashes)

    def test_conflict_return_to_editor_reopens_same_temp(self):
        # "Return to editor" loops back into the SAME temp file (the
        # user's work is preserved); once the conflict clears, the retry
        # applies.
        conflicted = ('# Alpha\nalpha body\n## Sub\nsub body\n'
                      '# Beta\nDIVERGED\n')
        seen = []

        def ed(tmp, n):
            seen.append(_read(tmp))
            if n == 1:
                _rewrite(self.path, conflicted)   # conflict under editor
                _rewrite(tmp, '# Beta\nEDITED\n')
            else:
                _rewrite(self.path, self._DOC)    # conflict resolved

        ctx = _EditCtx(cursor=self._item(4), editor=ed, confirms=[True])
        self.r._action_edit_section(ctx)
        self.assertEqual(len(ctx.calls), 2)
        # Same temp path both times; the second visit still holds the
        # user's edited content from the first.
        self.assertEqual(ctx.calls[0][-1], ctx.calls[1][-1])
        self.assertEqual(seen, [self._BETA, '# Beta\nEDITED\n'])
        self.assertEqual(_read(self.path),
                         '# Alpha\nalpha body\n## Sub\nsub body\n'
                         '# Beta\nEDITED\n')
        self.assertEqual(ctx.refreshes, 1)

    def test_file_root_edits_in_place(self):
        fs = self.r._FILES[self.path]
        ctx = _EditCtx(cursor=fs.file_root)
        self.r._action_edit_section(ctx)
        self.assertEqual(ctx.calls, [['fake-ed', self.path]])
        self.assertEqual(ctx.refreshes, 1)

    def test_scope_root_pseudo_item_edits_file_in_place(self):
        # A scoped session's synthetic scope-root row carries only the id
        # (#552) — the file branch must not touch recipe-added attrs.
        ctx = _EditCtx(cursor=_ScopeRootPseudoItem(id=('file', self.path)))
        self.r._action_edit_section(ctx)
        self.assertEqual(ctx.calls, [['fake-ed', self.path]])

    def test_scoped_content_pseudo_item_resolves_via_index(self):
        # Scoped INTO a heading: the scope row is a pseudo-Item with the
        # content id but no byte offsets — the handler resolves the real
        # node through the per-file index.
        def ed(tmp, n):
            _rewrite(tmp, '# Beta\nEDITED\n')
        pseudo = _ScopeRootPseudoItem(id=('content', self.path, 4))
        ctx = _EditCtx(cursor=pseudo, editor=ed)
        self.r._action_edit_section(ctx)
        self.assertEqual(_read(self.path),
                         '# Alpha\nalpha body\n## Sub\nsub body\n'
                         '# Beta\nEDITED\n')

    def test_reference_rows_rejected(self):
        anchor = ('file', self.path)
        for rid in (('md', anchor, ('/other.md',), 0),
                    ('md', anchor, ('/other.md',), None),
                    ('refs', anchor, ())):
            ctx = _EditCtx(cursor=_SrcItem(id=rid, kind='md-doc'))
            self.r._action_edit_section(ctx)
            self.assertEqual(ctx.calls, [])
            self.assertTrue(ctx.flashes and
                            ctx.flashes[0].startswith('E edits the primary'),
                            msg=f'flashes: {ctx.flashes}')

    def test_no_cursor_is_noop(self):
        ctx = _EditCtx(cursor=None)
        self.r._action_edit_section(ctx)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(ctx.flashes, [])

    def test_stale_content_id_is_noop(self):
        ctx = _EditCtx(cursor=_SrcItem(id=('content', '/nope.md', 0),
                                       kind='heading'))
        self.r._action_edit_section(ctx)
        self.assertEqual(ctx.calls, [])


class TestEditSectionStdin(unittest.TestCase):
    """``E`` on the stdin document — the in-memory apply pipeline."""

    _DOC = '# Piped\nintro body\n## Inner\ninner body\n'

    def setUp(self):
        self.r = _load_recipe()
        self.r._STDIN_TEXT = self._DOC
        self.r._INPUT_FILES = [(self.r._STDIN_PATH, '')]
        self.r._reparse()
        self._editor_saved = os.environ.get('EDITOR')
        os.environ['EDITOR'] = 'fake-ed'

    def tearDown(self):
        if self._editor_saved is None:
            os.environ.pop('EDITOR', None)
        else:
            os.environ['EDITOR'] = self._editor_saved

    def _item(self, line):
        path = self.r._STDIN_PATH
        return self.r._FILES[path].by_id[('content', path, line)]

    def test_section_edit_applies_in_memory(self):
        def ed(tmp, n):
            _rewrite(tmp, '## Inner\nEDITED\n')
        ctx = _EditCtx(cursor=self._item(2), editor=ed)
        self.r._action_edit_section(ctx)
        self.assertEqual(self.r._STDIN_TEXT,
                         '# Piped\nintro body\n## Inner\nEDITED\n')
        self.assertIn('applied to in-memory copy - not saved to disk',
                      ctx.flashes)
        self.assertEqual(ctx.refreshes, 1)
        # Nothing materialised at the sentinel path.
        self.assertFalse(os.path.exists(self.r._STDIN_PATH))

    def test_root_edit_replaces_whole_in_memory_body(self):
        payloads = []

        def ed(tmp, n):
            payloads.append(_read(tmp))
            _rewrite(tmp, '# Rewritten\nnew body\n')

        fs = self.r._FILES[self.r._STDIN_PATH]
        ctx = _EditCtx(cursor=fs.file_root, editor=ed)
        self.r._action_edit_section(ctx)
        # The stdin root goes through the TEMP pipeline (no file to edit
        # in place): the payload is the whole in-memory body.
        self.assertEqual(payloads, [self._DOC])
        self.assertNotEqual(ctx.calls[0][-1], self.r._STDIN_PATH)
        self.assertEqual(self.r._STDIN_TEXT, '# Rewritten\nnew body\n')
        self.assertIn('applied to in-memory copy - not saved to disk',
                      ctx.flashes)


class TestInsertSectionAction(unittest.TestCase):
    """``a`` / ``i`` (``_insert_section_at``) — marker insert flow.

    Same fixture + recorder-ctx approach as TestEditSectionAction; the
    marker confirm is driven directly (``_insert_section_at``) or through
    the stashed ``on_confirm`` (``_action_insert_section``), since
    headless marker mode never fires the callback itself.
    """

    _DOC = ('# Alpha\nalpha body\n'
            '## Sub\nsub body\n'
            '# Beta\nbeta body\n')
    _NEW = '## Added\nadded body\n'

    def setUp(self):
        import tempfile
        self.r = _load_recipe()
        fd, self.path = tempfile.mkstemp(suffix='.md')
        os.close(fd)
        _rewrite(self.path, self._DOC)
        self.r._INPUT_FILES = [(self.path, '')]
        self.r._reparse()
        self._editor_saved = os.environ.get('EDITOR')
        os.environ['EDITOR'] = 'fake-ed'

    def tearDown(self):
        if self._editor_saved is None:
            os.environ.pop('EDITOR', None)
        else:
            os.environ['EDITOR'] = self._editor_saved
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _write_new(self, tmp, n):
        _rewrite(tmp, self._NEW)

    def test_action_enters_marker_mode_and_routes_confirm(self):
        # The keybinding half: ``_action_insert_section`` enters marker
        # mode; firing the stashed on_confirm runs the whole pipeline.
        ctx = _EditCtx(editor=self._write_new)
        self.r._action_insert_section(ctx)
        self.assertEqual(ctx.insert_labels, ['insert section'])
        self.assertEqual(ctx.calls, [])          # nothing until confirm
        ctx.on_confirm('after', ('content', self.path, 4))
        self.assertEqual(_read(self.path), self._DOC + self._NEW)

    def test_insert_after_heading(self):
        payloads = []

        def ed(tmp, n):
            payloads.append(_read(tmp))
            _rewrite(tmp, self._NEW)

        ctx = _EditCtx(editor=ed)
        self.r._insert_section_at(ctx, 'after', ('content', self.path, 4))
        # The editor was seeded with the template, not an extent…
        self.assertEqual(payloads, [self.r._INSERT_TEMPLATE])
        # …the new section landed after Beta (EOF), the mutation was
        # logged with its relation, and the cursor lands on the new row.
        self.assertEqual(_read(self.path), self._DOC + self._NEW)
        self.assertTrue(any('inserted section (after)' in line
                            for line in ctx.logs), msg=f'logs: {ctx.logs}')
        self.assertEqual(ctx.refreshes, 1)
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 6)])

    def test_insert_before_heading(self):
        ctx = _EditCtx(editor=self._write_new)
        self.r._insert_section_at(ctx, 'before', ('content', self.path, 4))
        self.assertEqual(
            _read(self.path),
            '# Alpha\nalpha body\n## Sub\nsub body\n'
            '## Added\nadded body\n'
            '# Beta\nbeta body\n')
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 4)])

    def test_insert_first_under_heading(self):
        # 'first' = child position: right after Alpha's text run, before
        # its first sub-heading.
        ctx = _EditCtx(editor=self._write_new)
        self.r._insert_section_at(ctx, 'first', ('content', self.path, 0))
        self.assertEqual(
            _read(self.path),
            '# Alpha\nalpha body\n'
            '## Added\nadded body\n'
            '## Sub\nsub body\n'
            '# Beta\nbeta body\n')
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 2)])

    def test_insert_first_on_file_root_lands_after_preamble(self):
        # The file root maps onto the root query '/'; this doc has a
        # zero-width preamble, so 'first' inserts at the very top.
        ctx = _EditCtx(editor=self._write_new)
        self.r._insert_section_at(ctx, 'first', ('file', self.path))
        self.assertEqual(_read(self.path), self._NEW + self._DOC)
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 0)])

    def test_template_unchanged_cancels(self):
        ctx = _EditCtx()                         # editor leaves temp as-is
        self.r._insert_section_at(ctx, 'after', ('content', self.path, 4))
        self.assertEqual(_read(self.path), self._DOC)
        self.assertEqual(ctx.refreshes, 0)
        self.assertIn('insert cancelled (content unchanged)', ctx.flashes)

    def test_empty_content_cancels(self):
        ctx = _EditCtx(editor=lambda tmp, n: _rewrite(tmp, '\n \n'))
        self.r._insert_section_at(ctx, 'after', ('content', self.path, 4))
        self.assertEqual(_read(self.path), self._DOC)
        self.assertIn('insert cancelled (empty content)', ctx.flashes)

    def test_reference_anchors_rejected(self):
        anchor = ('file', self.path)
        for rid in (('md', anchor, ('/other.md',), 0),
                    ('md', anchor, ('/other.md',), None),
                    ('refs', anchor, ()),
                    None):
            ctx = _EditCtx(editor=self._write_new)
            self.r._insert_section_at(ctx, 'after', rid)
            self.assertEqual(ctx.calls, [], msg=f'rid: {rid}')
            self.assertTrue(
                ctx.flashes and
                ctx.flashes[0].startswith('insert works on the primary'),
                msg=f'rid: {rid}, flashes: {ctx.flashes}')
        self.assertEqual(_read(self.path), self._DOC)

    def test_first_on_text_run_rejected(self):
        # A text run is a leaf — no child position exists under it.
        ctx = _EditCtx(editor=self._write_new)
        self.r._insert_section_at(ctx, 'first', ('content', self.path, 1))
        self.assertEqual(ctx.calls, [])
        self.assertIn('cannot insert a child under a [text] run',
                      ctx.flashes)
        self.assertEqual(_read(self.path), self._DOC)

    def test_text_run_before_after_still_valid(self):
        # before/after ARE valid on a text run (its range addresses it).
        ctx = _EditCtx(editor=self._write_new)
        self.r._insert_section_at(ctx, 'after', ('content', self.path, 1))
        self.assertEqual(
            _read(self.path),
            '# Alpha\nalpha body\n'
            '## Added\nadded body\n'
            '## Sub\nsub body\n'
            '# Beta\nbeta body\n')

    def test_anchor_shifted_underneath_applies_by_hash(self):
        # OTHER regions change while the editor is open — the anchor is
        # re-found by hash and the insert still lands after it.
        def ed(tmp, n):
            _rewrite(self.path, '# Zero\nzero body\n' + self._DOC)
            _rewrite(tmp, self._NEW)

        ctx = _EditCtx(editor=ed)
        self.r._insert_section_at(ctx, 'after', ('content', self.path, 4))
        self.assertEqual(_read(self.path),
                         '# Zero\nzero body\n' + self._DOC + self._NEW)

    def test_conflict_cancel_leaves_file_alone(self):
        # The ANCHOR section itself diverged underneath the editor →
        # dialog; "Cancel insert" discards.
        conflicted = ('# Alpha\nalpha body\n## Sub\nsub body\n'
                      '# Beta\nDIVERGED\n')

        def ed(tmp, n):
            _rewrite(self.path, conflicted)
            _rewrite(tmp, self._NEW)

        ctx = _EditCtx(editor=ed, confirms=[False])
        self.r._insert_section_at(ctx, 'after', ('content', self.path, 4))
        self.assertEqual(_read(self.path), conflicted)
        self.assertEqual(len(ctx.confirm_prompts), 1)
        self.assertIn('insert cancelled', ctx.flashes)


class TestInsertSectionStdin(unittest.TestCase):
    """``a`` / ``i`` on the stdin document — in-memory insert."""

    _DOC = '# Piped\nintro body\n## Inner\ninner body\n'

    def setUp(self):
        self.r = _load_recipe()
        self.r._STDIN_TEXT = self._DOC
        self.r._INPUT_FILES = [(self.r._STDIN_PATH, '')]
        self.r._reparse()
        self._editor_saved = os.environ.get('EDITOR')
        os.environ['EDITOR'] = 'fake-ed'

    def tearDown(self):
        if self._editor_saved is None:
            os.environ.pop('EDITOR', None)
        else:
            os.environ['EDITOR'] = self._editor_saved

    def test_insert_applies_in_memory(self):
        path = self.r._STDIN_PATH
        ctx = _EditCtx(editor=lambda tmp, n: _rewrite(
            tmp, '## Added\nadded body\n'))
        self.r._insert_section_at(ctx, 'after', ('content', path, 2))
        self.assertEqual(self.r._STDIN_TEXT,
                         self._DOC + '## Added\nadded body\n')
        self.assertIn('applied to in-memory copy - not saved to disk',
                      ctx.flashes)
        self.assertFalse(os.path.exists(path))


class TestMoveSectionAction(unittest.TestCase):
    """``x`` (``_action_move_section`` / ``_move_section_to``) — the
    cut + re-insert move flow.

    Same fixture + recorder-ctx approach as the edit / insert tests; the
    marker confirm is driven through the stashed ``on_confirm`` (headless
    marker mode never fires it), so every test exercises the real capture
    → re-address → surgery pipeline against the on-disk fixture.
    """

    _DOC = ('# Alpha\nalpha body\n'    # lines 0-1; extent spans Sub too
            '## Sub\nsub body\n'       # lines 2-3
            '# Beta\nbeta body\n')     # lines 4-5

    def setUp(self):
        import tempfile
        self.r = _load_recipe()
        fd, self.path = tempfile.mkstemp(suffix='.md')
        os.close(fd)
        _rewrite(self.path, self._DOC)
        self.r._INPUT_FILES = [(self.path, '')]
        self.r._reparse()

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _item(self, line):
        return self.r._FILES[self.path].by_id[('content', self.path, line)]

    def _move(self, src_line, relation, dest_id):
        """Run the whole flow: action on ``src_line``, confirm at dest."""
        ctx = _EditCtx(cursor=self._item(src_line))
        self.r._action_move_section(ctx)
        if ctx.on_confirm is not None:
            ctx.on_confirm(relation, dest_id)
        return ctx

    def test_action_highlights_row_and_enters_marker_mode(self):
        ctx = _EditCtx(cursor=self._item(4))
        self.r._action_move_section(ctx)
        # The cursor row became the one-row selection highlight…
        self.assertEqual(ctx.selects,
                         [([('content', self.path, 4)], True)])
        # …and marker mode opened with the move label, nothing written.
        self.assertEqual(ctx.insert_labels, ['move here'])
        self.assertEqual(_read(self.path), self._DOC)

    def test_move_forward_after_heading(self):
        ctx = self._move(2, 'after', ('content', self.path, 4))
        self.assertEqual(
            _read(self.path),
            '# Alpha\nalpha body\n'
            '# Beta\nbeta body\n'
            '## Sub\nsub body\n')
        # Highlight cleared on confirm; mutation logged; cursor lands on
        # the section's new row.
        self.assertEqual(ctx.clear_selections, 1)
        self.assertTrue(any('moved section (after)' in line
                            for line in ctx.logs), msg=f'logs: {ctx.logs}')
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 4)])

    def test_move_backward_before_heading(self):
        # 'before' is synthetic here — the live marker only emits 'after'
        # / 'first' (see ctx.insert) — but the handler supports all three
        # and this drives the backward-surgery branch directly.
        ctx = self._move(4, 'before', ('content', self.path, 0))
        self.assertEqual(
            _read(self.path),
            '# Beta\nbeta body\n'
            '# Alpha\nalpha body\n'
            '## Sub\nsub body\n')
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 0)])

    def test_move_first_under_heading(self):
        # 'first' = child position: right after Alpha's text run, before
        # its first sub-heading.
        ctx = self._move(4, 'first', ('content', self.path, 0))
        self.assertEqual(
            _read(self.path),
            '# Alpha\nalpha body\n'
            '# Beta\nbeta body\n'
            '## Sub\nsub body\n')
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 2)])

    def test_move_after_file_root_is_document_bottom(self):
        ctx = self._move(2, 'after', ('file', self.path))
        self.assertEqual(
            _read(self.path),
            '# Alpha\nalpha body\n'
            '# Beta\nbeta body\n'
            '## Sub\nsub body\n')
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 4)])

    def test_move_into_itself_is_noop(self):
        # STRICTLY INSIDE: Alpha's extent contains Sub — 'before' Sub is
        # an offset in the extent's interior. A no-op, not an error.
        ctx = self._move(0, 'before', ('content', self.path, 2))
        self.assertIn('nothing moved — the destination is within the '
                      'section', ctx.flashes)
        self.assertEqual(_read(self.path), self._DOC)

    def test_move_to_own_boundary_is_noop(self):
        # EXACT START and EXACT END: dest == source row — 'before' is the
        # extent's own start offset, 'after' its own end offset. Both are
        # boundary-inclusive no-ops (re-inserting there reproduces the
        # document byte-for-byte).
        for relation in ('before', 'after'):
            with self.subTest(relation=relation):
                ctx = self._move(4, relation, ('content', self.path, 4))
                self.assertIn('nothing moved — the destination is within '
                              'the section', ctx.flashes)
                self.assertEqual(_read(self.path), self._DOC)
                self.assertEqual(ctx.refreshes, 0)

    def test_move_to_descendant_end_boundary_is_noop(self):
        # EXACT END via a descendant: 'after' Sub — Alpha's last child —
        # is exactly Alpha's own extent end.
        ctx = self._move(0, 'after', ('content', self.path, 2))
        self.assertIn('nothing moved — the destination is within the '
                      'section', ctx.flashes)
        self.assertEqual(_read(self.path), self._DOC)

    def test_shifted_file_still_moves_by_hash(self):
        # The document grows a new first section AFTER the marker went up
        # (captures taken) — both endpoints re-address by content hash and
        # the move lands correctly in the shifted document.
        ctx = _EditCtx(cursor=self._item(4))
        self.r._action_move_section(ctx)
        _rewrite(self.path, '# Zero\nzero body\n' + self._DOC)
        ctx.on_confirm('first', ('content', self.path, 0))
        self.assertEqual(
            _read(self.path),
            '# Zero\nzero body\n'
            '# Alpha\nalpha body\n'
            '# Beta\nbeta body\n'
            '## Sub\nsub body\n')

    def test_lost_target_flashes_and_leaves_file_alone(self):
        # The moved section vanished underneath the marker — no endpoint
        # can be re-addressed: flash, and the changed file stays intact.
        ctx = _EditCtx(cursor=self._item(4))
        self.r._action_move_section(ctx)
        changed = '# Alpha\nalpha body\n## Sub\nsub body\n# Gamma\nnew\n'
        _rewrite(self.path, changed)
        ctx.on_confirm('after', ('content', self.path, 0))
        self.assertTrue(any('could not move' in f for f in ctx.flashes),
                        msg=f'flashes: {ctx.flashes}')
        self.assertEqual(_read(self.path), changed)

    def test_cross_file_destination_rejected(self):
        ctx = self._move(4, 'after', ('content', '/elsewhere.md', 0))
        self.assertIn('cannot move a section to a different file',
                      ctx.flashes)
        self.assertEqual(_read(self.path), self._DOC)

    def test_reference_row_destination_rejected(self):
        ctx = self._move(4, 'after', ('md', 'anchor', ('x',), 0))
        self.assertTrue(any('move works on the primary document' in f
                            for f in ctx.flashes),
                        msg=f'flashes: {ctx.flashes}')
        self.assertEqual(_read(self.path), self._DOC)

    def test_non_content_cursor_rejected(self):
        # A file root is not movable — flash, and marker mode never opens.
        fs = self.r._FILES[self.path]
        ctx = _EditCtx(cursor=fs.file_root)
        self.r._action_move_section(ctx)
        self.assertTrue(any('put the cursor on a heading' in f
                            for f in ctx.flashes),
                        msg=f'flashes: {ctx.flashes}')
        self.assertEqual(ctx.insert_labels, [])
        self.assertEqual(ctx.selects, [])

    def test_first_under_text_run_rejected(self):
        # Alpha's text run (line 1) is a leaf — no child position.
        ctx = self._move(4, 'first', ('content', self.path, 1))
        self.assertIn('cannot insert a child under a [text] run',
                      ctx.flashes)
        self.assertEqual(_read(self.path), self._DOC)

    def test_text_run_is_movable(self):
        # Alpha's intro run relocates after Beta — bytes verbatim.
        ctx = self._move(1, 'after', ('content', self.path, 4))
        self.assertEqual(
            _read(self.path),
            '# Alpha\n'
            '## Sub\nsub body\n'
            '# Beta\nbeta body\n'
            'alpha body\n')
        self.assertEqual(ctx.cursor_tos, [('content', self.path, 5)])


class TestMoveSectionStdin(unittest.TestCase):
    """``x`` on the stdin document — in-memory move."""

    _DOC = '# Piped\nintro body\n## Inner\ninner body\n## Outer\nouter\n'

    def setUp(self):
        self.r = _load_recipe()
        self.r._STDIN_TEXT = self._DOC
        self.r._INPUT_FILES = [(self.r._STDIN_PATH, '')]
        self.r._reparse()

    def test_move_applies_in_memory(self):
        path = self.r._STDIN_PATH
        fs = self.r._FILES[path]
        ctx = _EditCtx(cursor=fs.by_id[('content', path, 4)])
        self.r._action_move_section(ctx)
        # 'before' is synthetic (the live marker emits 'after'/'first');
        # it drives the backward branch through the stdin pipeline.
        ctx.on_confirm('before', ('content', path, 2))
        self.assertEqual(self.r._STDIN_TEXT,
                         '# Piped\nintro body\n'
                         '## Outer\nouter\n'
                         '## Inner\ninner body\n')
        self.assertIn('applied to in-memory copy - not saved to disk',
                      ctx.flashes)
        self.assertFalse(os.path.exists(path))


if __name__ == '__main__':
    unittest.main()
