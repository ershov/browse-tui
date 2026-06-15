"""Unit tests for ``recipes/md_doc`` — the shared markdown-structure module.

``md_doc``'s STRUCTURAL half imports no ``browse_tui`` (it is the
framework-agnostic part of the markdown work): we just put ``recipes/`` on
``sys.path`` — which also resolves ``md_doc``'s own ``from md2ansi_lib import
md2ansi_scan`` to the real library, the same thing ``--run-py`` does at runtime
— and import it. Its framework-AWARE launcher block is guarded behind
``from browse_tui import Item`` (like a recipe), so to cover that API we import
``md_doc`` under a temporary ``browse_tui`` stub and then restore ``sys.modules``
(see ``_import_md_doc_with_launcher``); the structural tests below use the same
real module.

Coverage mirrors the design spec's ``md_doc`` testing strategy:

* ``build_doc_tree``  — heading nesting + boundary byte-range slicing on
                        fixtures, incl. a fenced ``#`` that is NOT a heading,
                        and the ``include_lists`` flag (TestBuildDocTree).
* ``node_at_line``    — exact line-offset lookup over a built tree: top-level
                        + deeply-nested match, no-match → ``None``
                        (TestNodeAtLine).
* ``find_git_root``   — nearest ``.git`` (dir or file) walk-up, none → ``None``,
                        terminates at the filesystem root (TestFindGitRoot).
* ``md_heading_trigger`` / ``find_md_refs`` — true/false gates and the ref
                        regex exclusions (TestTriggersAndRefs).
* ``resolve_md_ref``  — base precedence, first-existing, ``None``
                        (TestResolveMdRef).
* ``get_doc`` / ``clear_cache`` — cache hit + clear (TestCache).
* launcher API       — ``ref_label`` anchoring; ``resolve_refs`` dedup / sort /
                        drop-nonexistent; the ``Item`` builders' id + tag shape;
                        and ``launch`` path-arg vs stdin-content invocation with
                        ``run_external`` stubbed (TestLauncherApi).
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# Put ``recipes/`` on the path so ``import md_doc`` (and its own
# ``from md2ansi_lib import ...``) resolve to the real files.
_RECIPES = str(Path(__file__).resolve().parents[2] / 'recipes')
if _RECIPES not in sys.path:
    sys.path.insert(0, _RECIPES)


def _import_md_doc_with_launcher():
    """Import ``md_doc`` with its framework-aware launcher block defined.

    ``md_doc``'s structural half needs no ``browse_tui``, but its launcher block
    (``launcher_row`` / ``references_umbrella`` / ``launch``) is guarded behind
    ``from browse_tui import Item`` — exactly like a recipe — so it only exists
    when ``browse_tui`` is importable at import time. The builders capture that
    ``Item`` into the module namespace, so once defined they keep working even
    if the stub is later removed.

    To exercise the launcher API we import ``md_doc`` under a TEMPORARY stub
    whose ``Item`` keeps its kwargs as attributes (the builder tests read
    ``.id`` / ``.tag`` / ``.has_children``), then RESTORE ``sys.modules`` to its
    prior state. The restore is what keeps this module a good citizen: a leftover
    lean ``browse_tui`` stub would bleed into the recipe test modules (whose own
    stubs early-return when ``browse_tui`` is already present) and break their
    richer imports — the order-sensitivity TESTING.md documents. We also drop a
    stubless ``md_doc`` already cached by another module so the re-import under
    the stub actually defines the launcher block.

    Returns the imported ``md_doc`` module (also left in ``sys.modules`` for the
    structural tests — that is the real library, not a stub).
    """
    saved_bt = sys.modules.get('browse_tui')
    # If md_doc was already imported WITHOUT the launcher block (some other
    # module imported it stubless), drop it so the re-import below redefines it.
    cached = sys.modules.get('md_doc')
    if cached is not None and not hasattr(cached, 'launcher_row'):
        del sys.modules['md_doc']

    if saved_bt is None:
        stub = types.ModuleType('browse_tui')

        class _Item:
            def __init__(self, *a, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

        stub.Item = _Item
        stub.PluginConfig = lambda **kw: None
        stub.register_plugin = lambda cfg: None
        sys.modules['browse_tui'] = stub
    try:
        import md_doc as _m
    finally:
        # Restore the prior browse_tui (remove our stub) so it never bleeds
        # into another test module's load. The launcher builders have already
        # captured ``Item``, so they keep working without the stub present.
        if saved_bt is None:
            sys.modules.pop('browse_tui', None)
        else:
            sys.modules['browse_tui'] = saved_bt
    return _m


md_doc = _import_md_doc_with_launcher()  # noqa: E402 (path insert precedes import)


def _headings(nodes):
    """The heading children of a node list, dropping synthesised 'text' nodes.

    ``build_doc_tree`` (headings-only) now prepends a dim ``'text'`` node to any
    scope that has body text before its first heading. The heading-structure
    tests below assert nesting/boundaries over the HEADINGS, so they filter the
    text nodes out here; the text nodes themselves are covered by
    ``TestBuildDocTreeTextNodes``.
    """
    return [n for n in nodes if n.kind == 'heading']


class TestBuildDocTree(unittest.TestCase):
    """``build_doc_tree`` — nesting, boundary byte-ranges, fences, lists."""

    def test_empty(self):
        self.assertEqual(md_doc.build_doc_tree(''), [])

    def test_no_headings(self):
        self.assertEqual(md_doc.build_doc_tree('just prose\nmore prose\n'), [])

    def test_nesting(self):
        text = (
            '# Top\n'        # line 0, offset 0
            'intro\n'        # line 1
            '## A\n'         # line 2
            'aaa\n'          # line 3
            '### A1\n'       # line 4
            'deep\n'         # line 5
            '## B\n'         # line 6
            'bbb\n'          # line 7
        )
        roots = md_doc.build_doc_tree(text)
        # ``# Top`` sits on line 0 so the root scope has no leading run; the
        # only top-level node is the heading itself.
        self.assertEqual(len(roots), 1)
        top = roots[0]
        self.assertEqual((top.kind, top.level, top.title), ('heading', 1, 'Top'))
        self.assertEqual([c.title for c in _headings(top.children)], ['A', 'B'])
        a, b = _headings(top.children)
        self.assertEqual([c.title for c in _headings(a.children)], ['A1'])
        self.assertEqual(_headings(b.children), [])
        # line offsets are 0-based document lines.
        self.assertEqual(top.line_offset, 0)
        self.assertEqual(a.line_offset, 2)
        self.assertEqual(_headings(a.children)[0].line_offset, 4)
        self.assertEqual(b.line_offset, 6)

    def test_title_strips_sigil_keeps_inline(self):
        roots = md_doc.build_doc_tree('## My **bold** heading\n')
        self.assertEqual(roots[0].title, 'My **bold** heading')

    def test_byte_range_slicing(self):
        # The boundary rule: a heading's [byte_offset : +byte_size] section
        # runs from its own start to the start of the next sibling-or-shallower
        # heading (or EOF), and INCLUDES its descendant subheadings.
        text = (
            '# Top\n'
            'intro\n'
            '## A\n'
            'aaa\n'
            '## B\n'
            'bbb\n'
        )
        roots = md_doc.build_doc_tree(text)
        top = roots[0]
        a, b = _headings(top.children)  # skip the 'intro' text node
        # Top spans the whole document (only h1, no shallower-or-equal after).
        self.assertEqual(text[top.byte_offset:top.byte_offset + top.byte_size], text)
        # A runs from '## A' up to (not including) '## B'.
        self.assertEqual(
            text[a.byte_offset:a.byte_offset + a.byte_size],
            '## A\naaa\n',
        )
        # B runs from '## B' to EOF.
        self.assertEqual(
            text[b.byte_offset:b.byte_offset + b.byte_size],
            '## B\nbbb\n',
        )
        # Slicing offsets agree with md2ansi_scan: the slice starts with the
        # literal heading line.
        self.assertTrue(text[a.byte_offset:].startswith('## A'))

    def test_fenced_hash_is_not_a_heading(self):
        # A '#' inside a fenced code block is NOT a heading — md2ansi_scan
        # masks the fence body, so build_doc_tree must not surface it.
        text = (
            '# Real\n'
            'text\n'
            '```\n'
            '# fake heading inside fence\n'
            '## also fake\n'
            '```\n'
            '## Real2\n'
        )
        roots = md_doc.build_doc_tree(text)
        self.assertEqual([r.title for r in roots], ['Real'])
        # 'Real2' is Real's only heading child (the 'text' run before it becomes
        # a text node; the fenced '#'/'##' lines are still not headings).
        self.assertEqual([c.title for c in _headings(roots[0].children)], ['Real2'])
        # Real's section spans through the fence to EOF (h1, nothing shallower
        # after), so the fence text lives inside Real's byte-range.
        top = roots[0]
        self.assertIn('# fake heading inside fence',
                      text[top.byte_offset:top.byte_offset + top.byte_size])

    def test_include_lists_off_by_default(self):
        text = '# H\n- one\n- two\n'
        roots = md_doc.build_doc_tree(text)  # default: headings only
        self.assertEqual(roots[0].children, [])

    def test_include_lists_on(self):
        text = (
            '# H\n'
            '- one\n'
            '  - nested\n'
            '- two\n'
        )
        roots = md_doc.build_doc_tree(text, include_lists=True)
        h = roots[0]
        self.assertEqual([c.title for c in h.children], ['one', 'two'])
        one, two = h.children
        self.assertEqual(one.kind, 'list-item')
        self.assertEqual([c.title for c in one.children], ['nested'])
        self.assertEqual(two.children, [])

    def test_list_before_first_heading_is_top_level(self):
        # An orphan list item with no heading above it becomes a top-level
        # node (no synthetic root in the structural model).
        roots = md_doc.build_doc_tree('- alpha\n- beta\n', include_lists=True)
        self.assertEqual([r.title for r in roots], ['alpha', 'beta'])
        self.assertTrue(all(r.kind == 'list-item' for r in roots))


class TestBuildDocTreeTextNodes(unittest.TestCase):
    """``build_doc_tree`` pass 4 — leading ``'text'`` nodes (headings-only).

    A scope with >=1 heading child gets a dim ``'text'`` node for the body run
    preceding its first heading, inserted as the scope's first child. Gated on
    ``include_lists=False`` (with lists the tree is byte-for-byte unchanged).
    """

    # Three nested scopes exercise every branch in one fixture:
    #   * the root scope has a leading run (``intro one``) before ``# H1``;
    #   * ``# H1``'s scope has a leading run (``h1 intro``) before ``## H2``;
    #   * ``## H2`` is a leaf (no heading child), so its body gets NO text node.
    _TEXT = (
        'intro one\n'    # line 0  -> text1 (root scope)
        '\n'             # line 1  (blank gap line, skipped)
        '# H1\n'         # line 2  H1
        'h1 intro\n'     # line 3  -> text2 (H1 scope)
        '## H2\n'        # line 4  H2 (leaf)
        'h2 body\n'      # line 5  (leaf scope -> no text3)
    )

    def test_text1_top_level_before_first_heading(self):
        roots = md_doc.build_doc_tree(self._TEXT)
        # text1 is prepended before H1 at the top level.
        self.assertEqual([(r.kind, r.title) for r in roots],
                         [('text', 'intro one'), ('heading', 'H1')])
        text1 = roots[0]
        self.assertEqual(text1.level, 1)        # borrows H1's level
        self.assertEqual(text1.line_offset, 0)
        self.assertEqual(text1.children, [])

    def test_text2_first_child_of_h1_before_h2(self):
        roots = md_doc.build_doc_tree(self._TEXT)
        h1 = roots[1]
        # text2 is H1's first child, before H2.
        self.assertEqual([(c.kind, c.title) for c in h1.children],
                         [('text', 'h1 intro'), ('heading', 'H2')])
        text2 = h1.children[0]
        self.assertEqual(text2.level, 2)        # borrows H2's level
        self.assertEqual(text2.line_offset, 3)

    def test_text3_under_leaf_h2_no_node(self):
        roots = md_doc.build_doc_tree(self._TEXT)
        h2 = roots[1].children[1]
        self.assertEqual((h2.kind, h2.title), ('heading', 'H2'))
        # H2 is a leaf heading: its body run yields NO text node.
        self.assertEqual(h2.children, [])

    def test_run_slices_back_to_text(self):
        # ``text[byte_offset : byte_offset + byte_size]`` is the body run, from
        # the first non-blank line up to (not including) the heading it precedes.
        roots = md_doc.build_doc_tree(self._TEXT)
        text1 = roots[0]
        self.assertEqual(
            self._TEXT[text1.byte_offset:text1.byte_offset + text1.byte_size],
            'intro one\n\n')        # run includes the trailing blank gap line
        text2 = roots[1].children[0]
        self.assertEqual(
            self._TEXT[text2.byte_offset:text2.byte_offset + text2.byte_size],
            'h1 intro\n')

    def test_node_at_line_finds_text_node(self):
        # The text node is reachable by its own line_offset (the codec selector).
        tree = md_doc.build_doc_tree(self._TEXT)
        self.assertEqual(md_doc.node_at_line(tree, 0).title, 'intro one')
        found = md_doc.node_at_line(tree, 3)
        self.assertEqual((found.kind, found.title), ('text', 'h1 intro'))

    def test_prose_only_stays_empty(self):
        # A heading-less document has no heading scope, so no text node — the
        # tree stays ``[]`` (browse-claude's markdown-node gate is preserved).
        self.assertEqual(md_doc.build_doc_tree('just prose\nmore prose\n'), [])

    def test_all_blank_gap_no_node(self):
        # ``# h1\n\n## h2`` — the only gap line before ## h2 is blank, so no
        # text node is synthesised for H1's scope.
        roots = md_doc.build_doc_tree('# h1\n\n## h2\n')
        h1 = roots[0]
        self.assertEqual([c.kind for c in h1.children], ['heading'])
        self.assertEqual(h1.children[0].title, 'h2')

    def test_multiline_run_title_is_first_nonblank_line(self):
        # The run can span several lines; the title is just the FIRST non-blank
        # line, and the run slice covers every line up to the heading.
        text = (
            '\n'             # line 0  (blank, skipped)
            'first line\n'   # line 1  -> title
            'second line\n'  # line 2  (part of the run, not the title)
            '# H\n'          # line 3  H
            'body\n'         # line 4
        )
        node = md_doc.build_doc_tree(text)[0]
        self.assertEqual((node.kind, node.title), ('text', 'first line'))
        self.assertEqual(node.line_offset, 1)
        self.assertEqual(
            text[node.byte_offset:node.byte_offset + node.byte_size],
            'first line\nsecond line\n')

    def test_title_strips_outer_whitespace_keeps_markup(self):
        # The title is ``.strip()``-ed (leading indent gone) but inline markup
        # is preserved — it is body text, with no sigil to remove.
        node = md_doc.build_doc_tree('   **bold** intro\n# H\n')[0]
        self.assertEqual(node.title, '**bold** intro')

    def test_include_lists_true_leaves_list_trees_unchanged(self):
        # With lists on, pass 4 is skipped: no text nodes appear and the tree is
        # byte-for-byte the headings+lists structure.
        text = (
            'intro one\n'
            '# H1\n'
            'h1 intro\n'
            '- one\n'
            '- two\n'
        )
        roots = md_doc.build_doc_tree(text, include_lists=True)
        # No top-level text node: H1 is still the only root.
        self.assertEqual([r.kind for r in roots], ['heading'])
        h1 = roots[0]
        # H1's children are the two list items — no synthesised 'text' node.
        self.assertEqual([(c.kind, c.title) for c in h1.children],
                         [('list-item', 'one'), ('list-item', 'two')])

    def test_include_lists_true_unchanged_vs_baseline_fixture(self):
        # The pre-existing nested-list fixture must produce an identical tree
        # with the pass-4 code present (no 'text' kinds anywhere).
        text = (
            '# H\n'
            '- one\n'
            '  - nested\n'
            '- two\n'
        )
        roots = md_doc.build_doc_tree(text, include_lists=True)
        h = roots[0]
        self.assertEqual([c.title for c in h.children], ['one', 'two'])
        one, two = h.children
        self.assertEqual([c.title for c in one.children], ['nested'])
        self.assertEqual(two.children, [])
        # Assert no 'text' node leaked anywhere in the tree.
        self._assert_no_text_kind(roots)

    def _assert_no_text_kind(self, nodes):
        for n in nodes:
            self.assertNotEqual(n.kind, 'text')
            self._assert_no_text_kind(n.children)


class TestNodeAtLine(unittest.TestCase):
    """``node_at_line`` — exact line-offset lookup over a built tree."""

    # A tree with a deeply nested heading so the DFS recursion is exercised.
    _TEXT = (
        '# Top\n'        # line 0
        'intro\n'        # line 1
        '## A\n'         # line 2
        'aaa\n'          # line 3
        '### A1\n'       # line 4
        'deep\n'         # line 5
        '## B\n'         # line 6
        'bbb\n'          # line 7
    )

    def test_top_level_match(self):
        tree = md_doc.build_doc_tree(self._TEXT)
        node = md_doc.node_at_line(tree, 0)
        self.assertIsNotNone(node)
        self.assertEqual(node.title, 'Top')

    def test_deeply_nested_match(self):
        # The DFS reaches a node nested two levels down by its exact offset.
        tree = md_doc.build_doc_tree(self._TEXT)
        node = md_doc.node_at_line(tree, 4)
        self.assertIsNotNone(node)
        self.assertEqual((node.title, node.level), ('A1', 3))

    def test_mid_level_match(self):
        tree = md_doc.build_doc_tree(self._TEXT)
        self.assertEqual(md_doc.node_at_line(tree, 6).title, 'B')

    def test_no_match_between_nodes_returns_none(self):
        # A line offset that is NOT a node's own line matches nothing — the
        # lookup is exact, not a containing-range search. Line 5 ('deep') is
        # body under the LEAF heading A1, so it gets no synthesised text node
        # (text nodes only precede a scope's first heading), hence no match.
        tree = md_doc.build_doc_tree(self._TEXT)
        self.assertIsNone(md_doc.node_at_line(tree, 5))

    def test_before_first_node_returns_none(self):
        # An offset before the first node's line yields None (no synthetic
        # root, no containing fallback).
        tree = md_doc.build_doc_tree('## Only\nbody\n')  # first node at line 0
        self.assertIsNone(md_doc.node_at_line(tree, -1))

    def test_offset_past_end_returns_none(self):
        tree = md_doc.build_doc_tree(self._TEXT)
        self.assertIsNone(md_doc.node_at_line(tree, 999))

    def test_empty_tree_returns_none(self):
        self.assertIsNone(md_doc.node_at_line([], 0))


class TestFindGitRoot(unittest.TestCase):
    """``find_git_root`` — nearest ``.git`` (dir or file) ancestor walk-up."""

    def test_git_dir_found_at_self(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, '.git'))
            self.assertEqual(md_doc.find_git_root(d), os.path.abspath(d))

    def test_git_dir_found_in_ancestor(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, '.git'))
            sub = os.path.join(d, 'a', 'b')
            os.makedirs(sub)
            self.assertEqual(md_doc.find_git_root(sub), os.path.abspath(d))

    def test_git_file_found(self):
        # A ``.git`` *file* (worktree / submodule gitdir pointer) counts too.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, '.git'), 'w') as f:
                f.write('gitdir: /elsewhere\n')
            sub = os.path.join(d, 'nested')
            os.makedirs(sub)
            self.assertEqual(md_doc.find_git_root(sub), os.path.abspath(d))

    def test_none_when_no_git(self):
        # No ``.git`` anywhere up to the fs root → None (the walk terminates at
        # root rather than looping). A tempdir under /tmp has no .git ancestor.
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(md_doc.find_git_root(d))

    def test_falsy_start_returns_none(self):
        self.assertIsNone(md_doc.find_git_root(''))
        self.assertIsNone(md_doc.find_git_root(None))

    def test_terminates_at_fs_root(self):
        # The filesystem root is its own parent; the loop must stop there and
        # return None rather than spin (the root has no .git in the test env).
        self.assertIsNone(md_doc.find_git_root('/'))


class TestTriggersAndRefs(unittest.TestCase):
    """``md_heading_trigger`` + ``find_md_refs`` — the cheap detection gates."""

    def test_trigger_real_heading(self):
        self.assertTrue(md_doc.md_heading_trigger('# Heading'))
        self.assertTrue(md_doc.md_heading_trigger('intro\n## later\n'))
        self.assertTrue(md_doc.md_heading_trigger('   # indented'))

    def test_trigger_code_fence_only(self):
        # Optimistic: a '#' that lives only inside a fenced block still
        # triggers (only build_doc_tree can tell it's not a real heading).
        self.assertTrue(md_doc.md_heading_trigger('```\n# x\n```'))

    def test_trigger_none(self):
        self.assertFalse(md_doc.md_heading_trigger('plain text only'))
        self.assertFalse(md_doc.md_heading_trigger('a # mid-line hash'))
        self.assertFalse(md_doc.md_heading_trigger(''))

    def test_trigger_requires_space_after_hash(self):
        # The space after '#' is mandatory, matching the authoritative grammar
        # (_MD_H1.._MD_H6 require [ \t]+): '#foo' is NOT a heading, '# foo' is.
        self.assertFalse(md_doc.md_heading_trigger('#foo'))
        self.assertFalse(md_doc.md_heading_trigger('intro\n##bar\n'))
        self.assertFalse(md_doc.md_heading_trigger('###'))
        self.assertTrue(md_doc.md_heading_trigger('# foo'))
        self.assertTrue(md_doc.md_heading_trigger('intro\n## bar\n'))
        self.assertTrue(md_doc.md_heading_trigger('#\tfoo'))

    def test_refs_basic_and_order(self):
        text = 'wrote docs/report.md then read NOTES.md'
        self.assertEqual(md_doc.find_md_refs(text), ['docs/report.md', 'NOTES.md'])

    def test_refs_uppercase_extension(self):
        self.assertEqual(md_doc.find_md_refs('see X.MD'), ['X.MD'])

    def test_refs_none(self):
        self.assertEqual(md_doc.find_md_refs('no markdown here, file.txt only'), [])

    def test_refs_exclude_quote(self):
        # A JSON-style quoted path captures the path, not the surrounding ".
        self.assertEqual(md_doc.find_md_refs('"report.md"'), ['report.md'])

    def test_refs_exclude_backslash(self):
        # A backslash (JSON escape / Windows sep) ends the token.
        self.assertEqual(md_doc.find_md_refs(r'a\b.md'), ['b.md'])

    def test_refs_exclude_dollar(self):
        # A shell variable does not pollute the match — capture stops at '$'.
        self.assertEqual(md_doc.find_md_refs('$HOME/x.md'), ['HOME/x.md'])

    def test_refs_exclude_glob_star(self):
        # A '*' is excluded; a bare glob yields no capture.
        self.assertEqual(md_doc.find_md_refs('docs/*.md'), [])

    def test_refs_markdown_inline_link(self):
        # The COMMON case: a markdown inline link surfaces the path, not the
        # unresolvable blob '[docs/cli.md](docs/cli.md'. The '[', ']', '(' and
        # ')' delimiters are all stripped, so both the label and the target are
        # captured as clean tokens. Downstream dedup-by-resolved-abspath
        # collapses the pair to one file; at the find_md_refs level both appear.
        self.assertEqual(
            md_doc.find_md_refs('[docs/cli.md](docs/cli.md)'),
            ['docs/cli.md', 'docs/cli.md'])

    def test_refs_autolink(self):
        # An autolink '<path>' captures the path without the angle brackets.
        self.assertEqual(md_doc.find_md_refs('<docs/api.md>'), ['docs/api.md'])

    def test_refs_inline_code(self):
        # An inline-code span captures the path without the backticks.
        self.assertEqual(md_doc.find_md_refs('`report.md`'), ['report.md'])

    def test_refs_reference_definition(self):
        # A link reference definition '[x]: path' captures only the target.
        self.assertEqual(
            md_doc.find_md_refs('[x]: docs/ref.md'), ['docs/ref.md'])

    def test_refs_wiki_link(self):
        # A wiki-style '[[path]]' captures the path without the doubled
        # brackets.
        self.assertEqual(
            md_doc.find_md_refs('[[docs/wiki.md]]'), ['docs/wiki.md'])

    def test_refs_bare_relative_unchanged_by_link_exclusions(self):
        # A bare relative path with no link/code wrapper is captured exactly —
        # the new ( ) [ ] < > backtick exclusions don't regress it.
        self.assertEqual(md_doc.find_md_refs('docs/p.md'), ['docs/p.md'])

    def test_refs_capture_absolute_path(self):
        # An absolute path keeps its leading '/' — the lookbehind anchors the
        # token at the first non-separator char, not the first word char (a
        # bare \b would drop the '/' and make resolve_md_ref's absolute branch
        # dead code).
        self.assertEqual(
            md_doc.find_md_refs('see /home/u/report.md here'),
            ['/home/u/report.md'])

    def test_refs_capture_tilde_path(self):
        # A '~'-prefixed path keeps its leading '~' (a bare \b would drop it).
        self.assertEqual(
            md_doc.find_md_refs('open ~/notes.md please'), ['~/notes.md'])

    def test_refs_capture_absolute_in_json(self):
        # The primary use case: an absolute file_path inside a raw JSONL line.
        # The leading '/' survives and the surrounding '"' is still excluded.
        self.assertEqual(
            md_doc.find_md_refs('{"file_path": "/abs/x.md"}'), ['/abs/x.md'])

    def test_refs_relative_unchanged_by_lookbehind(self):
        # Relative refs (no leading separator) capture exactly as before.
        self.assertEqual(
            md_doc.find_md_refs('wrote report.md and docs/notes.md'),
            ['report.md', 'docs/notes.md'])

    def test_refs_exclude_mdx_and_trailing_dot(self):
        # The trailing \b is kept: '.mdx' is not a '.md' ref, and a sentence
        # period after '.md' is not captured into the token.
        self.assertEqual(md_doc.find_md_refs('a.mdx'), [])
        self.assertEqual(
            md_doc.find_md_refs('see report.md. done'), ['report.md'])


class TestResolveMdRef(unittest.TestCase):
    """``resolve_md_ref`` — base precedence + first-existing + None."""

    def _write(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(body)

    def test_precedence_doc_cwd_project(self):
        with tempfile.TemporaryDirectory() as d:
            doc = os.path.join(d, 'doc')
            cwd = os.path.join(d, 'cwd')
            proj = os.path.join(d, 'proj')
            self._write(os.path.join(doc, 'report.md'), 'DOC')
            self._write(os.path.join(cwd, 'report.md'), 'CWD')
            self._write(os.path.join(proj, 'report.md'), 'PROJ')

            def resolve():
                return md_doc.resolve_md_ref(
                    'report.md', doc_dir=doc, cwd=cwd, project_root=proj)

            # doc_dir wins first.
            with open(resolve()) as f:
                self.assertEqual(f.read(), 'DOC')
            # remove doc copy -> cwd wins.
            os.remove(os.path.join(doc, 'report.md'))
            with open(resolve()) as f:
                self.assertEqual(f.read(), 'CWD')
            # remove cwd copy -> project_root wins.
            os.remove(os.path.join(cwd, 'report.md'))
            with open(resolve()) as f:
                self.assertEqual(f.read(), 'PROJ')

    def test_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(md_doc.resolve_md_ref(
                'nope.md', doc_dir=d, cwd=d, project_root=d))

    def test_extra_bases_default_empty_unchanged(self):
        # The default ``extra_bases=()`` leaves the candidate order exactly
        # ``[doc_dir, cwd, project_root]`` — same as before the hook existed.
        with tempfile.TemporaryDirectory() as d:
            cwd = os.path.join(d, 'cwd')
            self._write(os.path.join(cwd, 'r.md'), 'CWD')
            # No doc copy, no extra bases -> falls through to cwd.
            got = md_doc.resolve_md_ref(
                'r.md', doc_dir=os.path.join(d, 'doc'),
                cwd=cwd, project_root=os.path.join(d, 'proj'))
            with open(got) as f:
                self.assertEqual(f.read(), 'CWD')

    def test_extra_base_resolves_when_nothing_else_does(self):
        # A ref present ONLY in an extra base resolves there (the flagship
        # ``--root`` case: doc_dir / cwd / project_root all lack it).
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'root')
            self._write(os.path.join(root, 'r.md'), 'ROOT')
            got = md_doc.resolve_md_ref(
                'r.md', doc_dir=os.path.join(d, 'doc'),
                cwd=os.path.join(d, 'cwd'),
                project_root=os.path.join(d, 'proj'),
                extra_bases=[root])
            with open(got) as f:
                self.assertEqual(f.read(), 'ROOT')

    def test_doc_dir_beats_extra_base(self):
        # ``doc_dir`` is tried BEFORE any extra base, so when the ref exists in
        # both the file's own dir and a supplied root, the file's dir wins.
        with tempfile.TemporaryDirectory() as d:
            doc = os.path.join(d, 'doc')
            root = os.path.join(d, 'root')
            self._write(os.path.join(doc, 'r.md'), 'DOC')
            self._write(os.path.join(root, 'r.md'), 'ROOT')
            got = md_doc.resolve_md_ref(
                'r.md', doc_dir=doc, cwd='/nowhere',
                project_root='/nowhere', extra_bases=[root])
            with open(got) as f:
                self.assertEqual(f.read(), 'DOC')

    def test_first_extra_base_wins(self):
        # Multiple extra bases are tried in order: when the ref exists in two
        # of them, the first-listed wins.
        with tempfile.TemporaryDirectory() as d:
            r1 = os.path.join(d, 'r1')
            r2 = os.path.join(d, 'r2')
            self._write(os.path.join(r1, 'r.md'), 'R1')
            self._write(os.path.join(r2, 'r.md'), 'R2')
            got = md_doc.resolve_md_ref(
                'r.md', doc_dir=os.path.join(d, 'doc'), cwd='/nowhere',
                project_root='/nowhere', extra_bases=[r1, r2])
            with open(got) as f:
                self.assertEqual(f.read(), 'R1')

    def test_extra_base_after_doc_before_cwd(self):
        # Full precedence chain: doc_dir, then extra bases, then cwd. With no
        # doc copy but a copy in BOTH an extra base and cwd, the extra base
        # wins (it precedes cwd in the candidate list).
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'root')
            cwd = os.path.join(d, 'cwd')
            self._write(os.path.join(root, 'r.md'), 'ROOT')
            self._write(os.path.join(cwd, 'r.md'), 'CWD')
            got = md_doc.resolve_md_ref(
                'r.md', doc_dir=os.path.join(d, 'doc'), cwd=cwd,
                project_root='/nowhere', extra_bases=[root])
            with open(got) as f:
                self.assertEqual(f.read(), 'ROOT')

    def test_extra_bases_ignored_for_absolute_ref(self):
        # An absolute ref is taken as-is (rule #1); extra bases never join it.
        with tempfile.TemporaryDirectory() as d:
            ap = os.path.join(d, 'sub', 'abs.md')
            self._write(ap, 'ABS')
            got = md_doc.resolve_md_ref(
                ap, doc_dir='/nowhere', cwd='/nowhere',
                project_root='/nowhere', extra_bases=['/also-nowhere'])
            self.assertEqual(got, os.path.realpath(ap))

    def test_absolute_path_used_directly(self):
        with tempfile.TemporaryDirectory() as d:
            ap = os.path.join(d, 'sub', 'abs.md')
            self._write(ap, 'X')
            # Bases all point elsewhere; an absolute ref still resolves.
            got = md_doc.resolve_md_ref(
                ap, doc_dir=d, cwd='/nowhere', project_root='/nowhere')
            self.assertEqual(got, os.path.realpath(ap))

    def test_absolute_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            ap = os.path.join(d, 'missing.md')
            self.assertIsNone(md_doc.resolve_md_ref(
                ap, doc_dir=d, cwd=d, project_root=d))

    def test_tilde_expansion(self):
        # A '~'-prefixed ref is expanduser'd and used as an absolute path.
        with tempfile.TemporaryDirectory() as home:
            self._write(os.path.join(home, 'note.md'), 'TILDE')
            with mock.patch.dict(os.environ, {'HOME': home}):
                got = md_doc.resolve_md_ref(
                    '~/note.md', doc_dir='/x', cwd='/y', project_root='/z')
            self.assertEqual(got, os.path.realpath(os.path.join(home, 'note.md')))

    def test_returns_realpath(self):
        # Result is canonicalised (symlinks/.. resolved) so callers can dedup
        # by string compare.
        with tempfile.TemporaryDirectory() as d:
            self._write(os.path.join(d, 'sub', 'x.md'), 'X')
            ref = 'sub/../sub/x.md'
            got = md_doc.resolve_md_ref(ref, doc_dir=d, cwd=d, project_root=d)
            self.assertEqual(got, os.path.realpath(os.path.join(d, 'sub', 'x.md')))
            self.assertNotIn('..', got)

    def test_find_then_resolve_absolute_branch(self):
        # End-to-end: an absolute .md path that flows through find_md_refs now
        # keeps its leading '/' and so resolves via resolve_md_ref's absolute
        # branch (rule #1). Before the lookbehind fix the leading '/' was
        # dropped, leaving a cwd-relative token that did not exist — making the
        # absolute branch unreachable dead code.
        with tempfile.TemporaryDirectory() as d:
            ap = os.path.join(d, 'sub', 'report.md')
            self._write(ap, 'ABS')
            (ref,) = md_doc.find_md_refs(f'wrote {ap} done')
            self.assertEqual(ref, ap)  # leading '/' preserved
            self.assertTrue(os.path.isabs(ref))
            # Bases all point elsewhere; only the absolute branch can resolve it.
            got = md_doc.resolve_md_ref(
                ref, doc_dir='/nowhere', cwd='/nowhere', project_root='/nowhere')
            self.assertEqual(got, os.path.realpath(ap))

    def test_find_then_resolve_tilde_branch(self):
        # End-to-end: a '~' .md path flows through find_md_refs keeping its '~'
        # and resolves via expanduser (the absolute branch after expansion).
        with tempfile.TemporaryDirectory() as home:
            self._write(os.path.join(home, 'note.md'), 'TILDE')
            (ref,) = md_doc.find_md_refs('open ~/note.md please')
            self.assertEqual(ref, '~/note.md')  # leading '~' preserved
            with mock.patch.dict(os.environ, {'HOME': home}):
                got = md_doc.resolve_md_ref(
                    ref, doc_dir='/x', cwd='/y', project_root='/z')
            self.assertEqual(
                got, os.path.realpath(os.path.join(home, 'note.md')))


class TestCache(unittest.TestCase):
    """``get_doc`` cache hit + ``clear_cache``."""

    def setUp(self):
        md_doc.clear_cache()

    def tearDown(self):
        md_doc.clear_cache()

    def test_cache_hit_same_tree_object(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'f.md')
            with open(p, 'w') as f:
                f.write('# H\nbody\n')
            text1, tree1 = md_doc.get_doc(p)
            self.assertEqual(tree1[0].title, 'H')
            # Mutate the file on disk; a cache hit must NOT re-read it.
            with open(p, 'w') as f:
                f.write('# DIFFERENT\n')
            text2, tree2 = md_doc.get_doc(p)
            self.assertIs(tree1, tree2)
            self.assertEqual(text2, text1)  # still the original contents

    def test_clear_cache_forces_reread(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'f.md')
            with open(p, 'w') as f:
                f.write('# H\n')
            _, tree1 = md_doc.get_doc(p)
            md_doc.clear_cache()
            with open(p, 'w') as f:
                f.write('# H2\n')
            _, tree2 = md_doc.get_doc(p)
            self.assertIsNot(tree1, tree2)
            self.assertEqual(tree2[0].title, 'H2')

    def test_invalid_utf8_byte_still_parses_headings(self):
        # A referenced .md with a stray non-UTF-8 byte must not raise
        # UnicodeDecodeError — get_doc decodes with errors='replace' so the
        # headings still parse (the substituted U+FFFD never reads as a #).
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'bad.md')
            with open(p, 'wb') as f:
                f.write(b'# Heading\n\nbody with a bad byte \xff here\n## Sub\n')
            text, tree = md_doc.get_doc(p)
            self.assertEqual([n.title for n in tree], ['Heading'])
            self.assertEqual([c.title for c in _headings(tree[0].children)], ['Sub'])
            self.assertIn('�', text)  # the bad byte was replaced


class _LaunchCtx:
    """A ``ctx`` stand-in for ``md_doc.launch``.

    Records each ``run_external`` call's ``cmd`` (argv list), the merged
    ``env`` dict, the ``keep_screen`` flag, and ``stdin_text`` (the piped
    document, for the content form), so a test can assert both what was
    launched and how. Mirrors the recipe tests' launch ctx.
    """

    def __init__(self):
        self.calls = []

    def run_external(self, cmd, env=None, *, keep_screen=False, stdin_text=None):
        self.calls.append({'cmd': cmd, 'env': env, 'keep_screen': keep_screen,
                           'stdin_text': stdin_text})
        return 0


class TestLauncherApi(unittest.TestCase):
    """The ``browse_tui``-aware launcher API: labels, refs, builders, launch."""

    def _write(self, path, body=''):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(body)

    # ---- ref_label --------------------------------------------------------

    def test_ref_label_inside_project_is_relative(self):
        self.assertEqual(
            md_doc.ref_label('/proj/docs/a.md', '/proj'), 'docs/a.md')
        # The project root itself collapses to its basename via relpath.
        self.assertEqual(md_doc.ref_label('/proj/a.md', '/proj'), 'a.md')

    def test_ref_label_outside_project_collapses_home(self):
        home = os.path.expanduser('~')
        self.assertEqual(
            md_doc.ref_label(os.path.join(home, 'n.md'), '/proj'), '~/n.md')

    def test_ref_label_outside_project_and_home_is_absolute(self):
        # Neither under project_root nor under ~: the bare absolute path.
        self.assertEqual(md_doc.ref_label('/elsewhere/x.md', '/proj'),
                         '/elsewhere/x.md')

    def test_ref_label_prefix_is_path_boundary_not_substring(self):
        # ``/proj2`` is not inside ``/proj`` even though the string starts with
        # it — the boundary check appends a '/'.
        self.assertEqual(md_doc.ref_label('/proj2/a.md', '/proj'), '/proj2/a.md')

    # ---- resolve_refs -----------------------------------------------------

    def test_resolve_refs_dedup_sort_drop_nonexistent(self):
        # References z, a, b (b twice), a missing file, and a non-.md token.
        # Result: existing .md only, deduped by abspath, sorted by label.
        with tempfile.TemporaryDirectory() as d:
            for name in ('a.md', 'b.md', 'z.md'):
                self._write(os.path.join(d, name))
            text = ('see z.md and b.md then b.md again and a.md, '
                    'plus missing nope.md and code conf.txt')
            refs = md_doc.resolve_refs(text, doc_dir=d, cwd=d, project_root=d)
            self.assertEqual([label for _ab, label in refs],
                             ['a.md', 'b.md', 'z.md'])
            # Each pair's abspath is the canonical realpath of an existing file.
            for ab, label in refs:
                self.assertTrue(os.path.exists(ab))
                self.assertEqual(ab, os.path.realpath(ab))
                self.assertEqual(label, md_doc.ref_label(ab, d))

    def test_resolve_refs_none_when_nothing_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                md_doc.resolve_refs('only missing.md here',
                                    doc_dir=d, cwd=d, project_root=d),
                [])

    def test_resolve_refs_two_tokens_one_file_deduped(self):
        # A markdown inline link yields the same token twice (label + target);
        # both resolve to one file, so the result has a single pair.
        with tempfile.TemporaryDirectory() as d:
            self._write(os.path.join(d, 'cli.md'))
            refs = md_doc.resolve_refs('[cli.md](cli.md)',
                                       doc_dir=d, cwd=d, project_root=d)
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0][1], 'cli.md')

    # ---- Item builders ----------------------------------------------------

    def test_launcher_row_id_and_shape(self):
        row = md_doc.launcher_row('/anchor.md', ('md-file', '/t/x.md'), 'x.md')
        # Generic routable id: ('launch', anchor, *spec).
        self.assertEqual(row.id, ('launch', '/anchor.md', 'md-file', '/t/x.md'))
        self.assertEqual(row.title, 'x.md')
        self.assertEqual(row.tag, 'md ↗')
        self.assertEqual(row.tag_style, 'yellow')
        self.assertFalse(row.has_children)

    def test_launcher_row_spec_is_opaque(self):
        # md_doc does not interpret the spec — an arbitrary tuple flows into
        # the id verbatim after the ('launch', anchor) prefix.
        row = md_doc.launcher_row('msg-7', ('md-inline',), 'message markdown')
        self.assertEqual(row.id, ('launch', 'msg-7', 'md-inline'))

    def test_references_umbrella_shape(self):
        u = md_doc.references_umbrella('msg-7')
        self.assertEqual(u.id, ('md-refs', 'msg-7'))
        self.assertEqual(u.title, 'References')
        self.assertEqual(u.tag, 'md')
        self.assertTrue(u.has_children)

    # ---- launch -----------------------------------------------------------

    def test_launch_path_is_plain_argv(self):
        ctx = _LaunchCtx()
        md_doc.launch(ctx, path='/docs/a.md', roots=('/proj', '/cwd'))
        self.assertEqual(len(ctx.calls), 1)
        call = ctx.calls[0]
        cmd = call['cmd']
        self.assertIsInstance(cmd, list)        # argv, not a shell string
        self.assertEqual(cmd[0], 'browse-md')
        # Embedding flags present; target before the --root bases.
        self.assertIn('--no-alt-screen', cmd)
        self.assertIn('--quit-on-scope-up', cmd)
        self.assertIn('/docs/a.md', cmd)
        # Repeatable --root, in order.
        self.assertEqual(cmd[cmd.index('/docs/a.md') + 1:],
                         ['--root', '/proj', '--root', '/cwd'])
        # No stdin env for the path form; handoff keeps the alt screen.
        self.assertIsNone(call['env'])
        self.assertTrue(call['keep_screen'])

    def test_launch_path_no_roots(self):
        ctx = _LaunchCtx()
        md_doc.launch(ctx, path='/docs/a.md')
        cmd = ctx.calls[0]['cmd']
        self.assertEqual(cmd, ['browse-md', '--no-alt-screen',
                               '--quit-on-scope-up', '/docs/a.md'])

    def test_launch_content_pipes_via_stdin(self):
        ctx = _LaunchCtx()
        md_doc.launch(ctx, content='# Inline\nbody\n', roots=('/proj',))
        call = ctx.calls[0]
        cmd = call['cmd']
        # Plain argv (no shell string), reading the document from stdin (`-`).
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[:2], ['browse-md', '-'])
        # The document rides the stdin pipe — NOT argv, NOT env.
        self.assertEqual(call['stdin_text'], '# Inline\nbody\n')
        self.assertIsNone(call['env'])
        self.assertNotIn('# Inline\nbody\n', cmd)
        # Embedding flags + repeatable --root on argv after `-`.
        self.assertIn('--no-alt-screen', cmd)
        self.assertIn('--quit-on-scope-up', cmd)
        self.assertEqual(cmd[-2:], ['--root', '/proj'])
        self.assertTrue(call['keep_screen'])

    def test_launch_content_large_document_not_on_argv_or_env(self):
        # Regression for the E2BIG bug: a big document must not land on argv
        # or env (both ARG_MAX/MAX_ARG_STRLEN-bounded) — only the stdin pipe.
        ctx = _LaunchCtx()
        big = '#x\n' + ('lorem ipsum ' * 50_000)   # ~600 KB, well over 128 KB
        md_doc.launch(ctx, content=big, roots=())
        call = ctx.calls[0]
        self.assertEqual(call['stdin_text'], big)
        self.assertIsNone(call['env'])
        self.assertNotIn(big, call['cmd'])

    def test_launch_content_empty_roots_omits_root_flag(self):
        ctx = _LaunchCtx()
        md_doc.launch(ctx, content='hi')
        cmd = ctx.calls[0]['cmd']
        self.assertNotIn('--root', cmd)
        self.assertEqual(cmd, ['browse-md', '-', '--no-alt-screen',
                               '--quit-on-scope-up'])

    def test_launch_content_none_sends_empty_string(self):
        # Defensive: ``content`` defaulting through (None) still pipes the empty
        # string rather than dropping it / passing None to stdin_text.
        ctx = _LaunchCtx()
        md_doc.launch(ctx, content=None, roots=())
        self.assertEqual(ctx.calls[0]['stdin_text'], '')


if __name__ == '__main__':
    unittest.main()
