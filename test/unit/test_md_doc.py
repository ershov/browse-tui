"""Unit tests for ``recipes/md_doc`` — the shared markdown-structure module.

Unlike the recipe test files, ``md_doc`` imports no ``browse_tui`` (it is the
framework-agnostic half of the markdown work), so no stub is needed: we just
put ``recipes/`` on ``sys.path`` — which also resolves ``md_doc``'s own
``from md2ansi_lib import md2ansi_scan`` to the real library, the same thing
``--run-py`` does at runtime — and import it directly.

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
* ``compose_md_id`` / ``parse_md_id`` — exact round-trip incl. awkward paths
                        and the line-offset suffix (TestIdCodec).
* ``get_doc`` / ``clear_cache`` — cache hit + clear (TestCache).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Put ``recipes/`` on the path so ``import md_doc`` (and its own
# ``from md2ansi_lib import ...``) resolve to the real files.
_RECIPES = str(Path(__file__).resolve().parents[2] / 'recipes')
if _RECIPES not in sys.path:
    sys.path.insert(0, _RECIPES)

import md_doc  # noqa: E402  (path insert must precede import)


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
        self.assertEqual(len(roots), 1)
        top = roots[0]
        self.assertEqual((top.kind, top.level, top.title), ('heading', 1, 'Top'))
        self.assertEqual([c.title for c in top.children], ['A', 'B'])
        a, b = top.children
        self.assertEqual([c.title for c in a.children], ['A1'])
        self.assertEqual(b.children, [])
        # line offsets are 0-based document lines.
        self.assertEqual(top.line_offset, 0)
        self.assertEqual(a.line_offset, 2)
        self.assertEqual(a.children[0].line_offset, 4)
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
        a, b = top.children
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
        self.assertEqual([c.title for c in roots[0].children], ['Real2'])
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
        # A line offset that is NOT a node's own line (it falls on body text
        # between headings) matches nothing — the lookup is exact, not a
        # containing-range search.
        tree = md_doc.build_doc_tree(self._TEXT)
        self.assertIsNone(md_doc.node_at_line(tree, 3))

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


class TestIdCodec(unittest.TestCase):
    """``compose_md_id`` / ``parse_md_id`` — exact round-trip."""

    def _roundtrip(self, base, abspaths, line_offset):
        cid = md_doc.compose_md_id(base, abspaths, line_offset)
        self.assertEqual(
            md_doc.parse_md_id(cid), (base, abspaths, line_offset))
        return cid

    def test_inline_document(self):
        cid = self._roundtrip('sess#3', [], None)
        self.assertEqual(cid, 'sess#3#md:')

    def test_inline_heading(self):
        cid = self._roundtrip('sess#3', [], 12)
        self.assertEqual(cid, 'sess#3#md:#12')

    def test_inline_heading_zero_offset(self):
        # A 0 line-offset must round-trip (not be confused with None).
        cid = self._roundtrip('sess#3', [], 0)
        self.assertEqual(cid, 'sess#3#md:#0')

    def test_file_document(self):
        cid = self._roundtrip('sess#3', ['/a/x.md'], None)
        # No raw '#' in the encoded segment.
        self.assertNotIn('#', cid[cid.index('#md:') + len('#md:'):])

    def test_file_heading(self):
        self._roundtrip('sess#3', ['/a/x.md'], 4)

    def test_nested_chain(self):
        self._roundtrip('sess#3', ['/a/x.md', '/b/y.md'], None)

    def test_nested_chain_with_offset(self):
        self._roundtrip('sess#3', ['/a/x.md', '/b/y.md'], 7)

    def test_path_with_hash(self):
        # A '#' in a path must survive — it encodes to %23, never a raw '#'.
        self._roundtrip('sess#3', ['/a/weird#name.md'], 5)

    def test_path_with_tilde_question_space(self):
        self._roundtrip('sess#3', ['/home/u/a b?x~y.md'], 2)

    def test_path_with_md_selector_lookalike(self):
        # A path that literally contains '#md:' must not be mis-split — it is
        # encoded inside the segment.
        self._roundtrip('sess#3', ['/a/has#md:inside.md'], None)

    def test_base_is_left_untouched(self):
        # The base keeps its own raw '#<n>' suffix verbatim.
        base = '/p/session.jsonl#42'
        cid = md_doc.compose_md_id(base, ['/a/x.md'], 3)
        got_base, paths, lo = md_doc.parse_md_id(cid)
        self.assertEqual(got_base, base)
        self.assertEqual(paths, ['/a/x.md'])
        self.assertEqual(lo, 3)

    def test_parse_non_md_id_raises(self):
        with self.assertRaises(ValueError):
            md_doc.parse_md_id('/p/session.jsonl#42')


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
            self.assertEqual([c.title for c in tree[0].children], ['Sub'])
            self.assertIn('�', text)  # the bad byte was replaced


if __name__ == '__main__':
    unittest.main()
