"""Unit tests for the ``recipes/browse-git`` helpers.

The recipe is a single-file ``--run-py`` script that imports
``browse_tui`` (only available when the binary loads it). To exercise
the id parser, the diff colorizer, and the positional classifier
directly we stub ``browse_tui`` in ``sys.modules`` and load the
extension-less recipe via ``SourceFileLoader`` — the same pattern as
``test/unit/test_browse_md.py``.

Coverage (ticket #616 — structural backbone):

* ``_parse_id``            every kind, incl. colon paths / slash refnames
* ``_colorize_diff``       ANSI on both the delta and git-fallback paths
* ``_classify_positionals``  path / rev / ``--`` / unknown(→exit)

Coverage (ticket #617 — commits mode end-to-end):

* ``_parse_decorations``   ``%D`` → ref chips (HEAD/branch/remote/tag)
* ``_parse_name_status``   A/M/D letters + rename → status + new path

Coverage (ticket #618 — reflog mode):

* ``_reflog_row``          NUL record → reflog Item (id/chips), malformed→None

Coverage (ticket #619 — status mode):

* ``_parse_porcelain_z``   NUL porcelain → (XY, path), incl. rename
* ``_status_tag``          XY → one-letter tag (X-or-Y, ``?`` for ``??``)
* ``_status_diff_plan``    XY → staged/unstaged/untracked diff command(s)

Coverage (ticket #620 — stash mode):

* ``_stash_index``         ``stash@{n}`` → ``n`` (or None)
* ``_stash_row``           NUL record → stash Item (id/tag/title/chips)

Coverage (ticket #621 — branches mode):

* ``_parse_for_each_ref_line``  full+short refname → (kind, short),
  kind classified from the refs/heads|remotes|tags prefix

Coverage (ticket #662 — commits columnar list):

* ``_commit_log_items``    stores ``col_sha`` / ``col_author`` /
  ``col_date`` and NO sha ``tag``; ``chips`` is the ``%D`` decorations
  only (no author·date chip)
* ``git_row_content``      commit rows → padded sha/author/date columns,
  decoration chips, then the subject LAST; rows of differing lengths
  align per-column; a non-commit row (no ``col_sha``) falls back to
  exactly ``default_row_content`` and never measures a column

Coverage (ticket #701 — tree-mode commit graph):

* ``_graph_translate``     maps the ``*|_`` git art glyphs to their box/block
  glyphs (diagonals ``/`` ``\\`` pass through), preserves internal spacing,
  rstrips trailing pad
* ``_commit_graph_items``  ``git log --graph`` lines → commit Items (with
  ``col_graph``) interleaved with inert filler Items (``filler:<n>``,
  ``has_children`` False, no ``col_sha``); git line order preserved
* ``_log_items``           routes to the graph builder when ``_tree_mode``
  else the plain ``_commit_log_items`` (off-path unchanged)
* ``git_row_content``      commit row inserts the graph after the date
  column; filler row = blank pad (sha+author+date span) then the art;
  a tree-off commit row (no ``col_graph``) is byte-identical to before
* ``_pop_tree_arg``        pops ``--tree`` / ``--no-tree`` (last wins)
* ``toggle_tree``          flips ``_tree_mode`` and refreshes

Coverage (ticket #702 — skip filler rows on up/down):

* ``_skip_fillers``        on_cursor_change hook: bounces up/down off a
  filler to the next commit in the travel direction (inferred from the
  ``cursor_index`` delta sign), reversing at a top/bottom run; no-op when
  tree-off / non-filler / None; loop-safe across the cursor_to re-fire
* ``_commit_graph_items``  records its ordered ``(id, is_filler)`` list
  under the build's namespace in ``_graph_rows_by_ns``; filler ids are
  namespaced ``filler:<ns>:<n>`` so two drilled-in refs never collide and
  a bounce resolves into the right ref's commit (concurrent ns entries
  coexist)
"""

import importlib.util
import shutil
import subprocess
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-git'


# Sentinel the stub ``style('dim')`` / ``style('yellow')`` return; the
# columns in ``git_row_content`` must carry these exact (fg, bold) pairs.
_DIM = (242, False)
_YELLOW = (3, False)


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    Always installs a fresh module so a stub left behind by another
    recipe's unit test doesn't bleed in. ``Item`` keeps its kwargs as
    attributes so the children-builder tests can read ``.id`` / ``.tag``
    if needed; ``Browser`` / ``BrowserConfig`` / ``Action`` are inert.

    The column helpers (``cell_ljust`` / ``style`` / ``default_row_content``)
    are functional-but-minimal — the test data is plain ASCII so
    ``str.ljust`` measures the same as the real cell-aware helper, which is
    enough to prove ``git_row_content`` wires them correctly. They mirror
    the stub in ``test_browse_fs.py``.
    """
    mod = types.ModuleType('browse_tui')

    class _Stub:
        def __init__(self, *a, **kw):
            self._args = a
            for k, v in kw.items():
                setattr(self, k, v)

    mod.Action = _Stub
    mod.Browser = _Stub
    mod.BrowserConfig = _Stub
    mod.Item = _Stub

    mod.cell_ljust = lambda s, width, fill=' ': s.ljust(width, fill)

    def _style(name):
        if name == 'dim':
            return _DIM
        if name == 'yellow':
            return _YELLOW
        return (None, False)

    mod.style = _style

    def _default_row_content(item, ctx):
        # A recognisable sentinel so the fallback path is unambiguous.
        return [('DEFAULT', getattr(item, 'id', None), getattr(item, 'title', None))]

    mod.default_row_content = _default_row_content
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-git recipe; returns a fresh module.

    ``recipes/`` is put on ``sys.path`` so the recipe's optional
    ``from md2ansi_lib import ...`` resolves to the real library, just
    as ``--run-py`` does by prepending the recipe directory at runtime.
    A fresh module is built on every call so tests that mutate
    module-level globals (``_revs`` / ``_paths`` / ``_MD_COLOR``) stay
    isolated.
    """
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
    _stub_browse_tui()
    name = '_browse_git_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _FakeCtx:
    """A ``RowContext`` stand-in: ``max_col_width(field)`` → fixed width.

    Records every field measured in ``calls`` so a test can assert the
    fallback path never touches a column.
    """

    def __init__(self, widths):
        self._widths = widths
        self.calls = []

    def max_col_width(self, field, parent_id=None):
        self.calls.append(field)
        return self._widths[field]


class TestParseId(unittest.TestCase):
    """``_parse_id`` classifies every prefixed id shape."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_root(self):
        self.assertEqual(self.r._parse_id(None), ('root',))

    def test_non_string(self):
        self.assertEqual(self.r._parse_id(42), ('other', 42))

    def test_error_row(self):
        self.assertEqual(self.r._parse_id('__error__'), ('err', ''))

    def test_commit(self):
        self.assertEqual(self.r._parse_id('commit:abc123'),
                         ('commit', 'abc123'))

    def test_file(self):
        self.assertEqual(self.r._parse_id('file:abc123:src/main.py'),
                         ('file', 'abc123', 'src/main.py'))

    def test_file_path_with_colon(self):
        # A path may itself contain a colon — only the first ':' after the
        # sha splits.
        self.assertEqual(self.r._parse_id('file:abc123:a/b:c.txt'),
                         ('file', 'abc123', 'a/b:c.txt'))

    def test_status(self):
        self.assertEqual(self.r._parse_id('status:M :src/x.py'),
                         ('status', 'M ', 'src/x.py'))

    def test_status_path_with_colon(self):
        self.assertEqual(self.r._parse_id('status:??:weird:name'),
                         ('status', '??', 'weird:name'))

    def test_ref_simple(self):
        self.assertEqual(self.r._parse_id('ref:main'), ('ref', 'main'))

    def test_ref_with_slashes(self):
        # Refnames carry '/' (origin/feature/x) — kept verbatim.
        self.assertEqual(self.r._parse_id('ref:origin/feature/x'),
                         ('ref', 'origin/feature/x'))

    def test_ref_with_colon(self):
        # A refname may (rarely) contain ':'; ref keeps all of rest.
        self.assertEqual(self.r._parse_id('ref:weird:ref'),
                         ('ref', 'weird:ref'))

    def test_reflog(self):
        self.assertEqual(self.r._parse_id('reflog:3:deadbeef'),
                         ('reflog', '3', 'deadbeef'))

    def test_stash_node(self):
        self.assertEqual(self.r._parse_id('stash:0'), ('stash', '0'))

    def test_stash_file(self):
        self.assertEqual(self.r._parse_id('stash:0:src/x.py'),
                         ('stash', '0', 'src/x.py'))

    def test_stash_file_path_with_colon(self):
        self.assertEqual(self.r._parse_id('stash:1:a:b.py'),
                         ('stash', '1', 'a:b.py'))

    def test_unknown_prefix(self):
        self.assertEqual(self.r._parse_id('weird:thing'),
                         ('other', 'thing'))


class TestColorizeDiff(unittest.TestCase):
    """``_colorize_diff`` returns ANSI on both delta and fallback paths."""

    def setUp(self):
        self.r = _load_recipe()
        # A minimal git-colored diff (caller's contract: already colored).
        self.colored = (
            '\x1b[1mdiff --git a/x b/x\x1b[m\n'
            '\x1b[31m--- a/x\x1b[m\n'
            '\x1b[32m+++ b/x\x1b[m\n'
            '@@ -1 +1 @@\n'
            '\x1b[31m-old\x1b[m\n'
            '\x1b[32m+new\x1b[m\n'
        )

    def test_fallback_path_returns_colored_text(self):
        # Force the no-delta branch by swapping the recipe module's
        # ``shutil`` so which() reports delta absent (a fake namespace,
        # not the shared module, so the patch can't leak).
        self.r.shutil = types.SimpleNamespace(which=lambda name: None)
        out = self.r._colorize_diff(self.colored)
        self.assertIn('\x1b[', out)
        # Fallback returns the caller's already-colored text unchanged.
        self.assertEqual(out, self.colored)

    def test_delta_path_returns_ansi(self):
        if shutil.which('delta') is None:
            self.skipTest('delta not on PATH')
        # Real which() finds delta; the helper pipes through it. The fresh
        # recipe module references the genuine ``subprocess`` module, so
        # this exercises the real delta binary end to end.
        out = self.r._colorize_diff(self.colored)
        self.assertIn('\x1b[', out)

    def test_delta_path_monkeypatched(self):
        # Prove the delta branch produces ANSI-bearing text without
        # depending on a delta install: swap which() + a fake subprocess
        # namespace on the recipe module only (never the shared
        # ``subprocess`` module, which would leak into other tests).
        self.r.shutil = types.SimpleNamespace(which=lambda name: '/usr/bin/delta')

        def fake_run(cmd, **kw):
            self.assertEqual(cmd[0], 'delta')
            return subprocess.CompletedProcess(
                cmd, 0, stdout='\x1b[34mrendered\x1b[0m\n', stderr='')

        fake_subprocess = types.SimpleNamespace(
            run=fake_run, CompletedProcess=subprocess.CompletedProcess)
        self.r.subprocess = fake_subprocess
        out = self.r._colorize_diff(self.colored)
        self.assertIn('\x1b[', out)
        self.assertIn('rendered', out)


class TestClassifyPositionals(unittest.TestCase):
    """``_classify_positionals`` sorts args into revs / paths / exit."""

    def setUp(self):
        self.r = _load_recipe()
        self._orig_argv = sys.argv

    def tearDown(self):
        sys.argv = self._orig_argv

    def _run(self, *args):
        sys.argv = ['browse-git', *args]
        self.r._revs = []
        self.r._paths = []
        self.r._classify_positionals()
        return self.r._revs, self.r._paths

    def test_existing_path_is_pathspec(self):
        # The recipe file itself certainly exists.
        revs, paths = self._run(str(_RECIPE))
        self.assertEqual(revs, [])
        self.assertEqual(paths, [str(_RECIPE)])

    def test_after_double_dash_is_pathspec(self):
        revs, paths = self._run('--', 'does/not/exist.py', 'also/missing')
        self.assertEqual(revs, [])
        self.assertEqual(paths, ['does/not/exist.py', 'also/missing'])

    def test_rev_is_classified_as_rev(self):
        # Stub git rev-parse so 'HEAD' classifies as a rev without a repo.
        def fake_run_git(*git_args):
            if 'rev-parse' in git_args:
                return subprocess.CompletedProcess(git_args, 0, '', '')
            return subprocess.CompletedProcess(git_args, 1, '', '')

        self.r._run_git = fake_run_git
        revs, paths = self._run('HEAD')
        self.assertEqual(revs, ['HEAD'])
        self.assertEqual(paths, [])

    def test_unknown_exits_2(self):
        # Neither an existing path nor a valid rev -> SystemExit(2).
        def fake_run_git(*git_args):
            return subprocess.CompletedProcess(git_args, 1, '', '')

        self.r._run_git = fake_run_git
        with self.assertRaises(SystemExit) as cm:
            self._run('definitely-not-a-real-ref-or-path-xyz')
        self.assertEqual(cm.exception.code, 2)

    def test_flag_tokens_are_skipped(self):
        # -h / --help before -- are left for the framework, not exited on.
        revs, paths = self._run('-h')
        self.assertEqual(revs, [])
        self.assertEqual(paths, [])


class TestParseDecorations(unittest.TestCase):
    """``_parse_decorations`` turns a ``%D`` string into colored chips."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_mixed_refs(self):
        # HEAD -> branch, remote, tag, and a slash-bearing local branch.
        # Remotes={'origin'} so origin/main is blue while feature/x stays
        # a cyan local branch.
        chips = self.r._parse_decorations(
            'HEAD -> main, origin/main, tag: v1.0, feature/x',
            remotes={'origin'})
        self.assertEqual(chips, [
            ('HEAD', 'green'),
            ('main', 'cyan'),
            ('origin/main', 'blue'),
            ('v1.0', 'yellow'),
            ('feature/x', 'cyan'),
        ])

    def test_empty_decoration(self):
        self.assertEqual(self.r._parse_decorations('', remotes=set()), [])
        self.assertEqual(self.r._parse_decorations(None, remotes=set()), [])

    def test_detached_head(self):
        self.assertEqual(
            self.r._parse_decorations('HEAD', remotes=set()),
            [('HEAD', 'green')])

    def test_tag_only(self):
        self.assertEqual(
            self.r._parse_decorations('tag: v2.3', remotes=set()),
            [('v2.3', 'yellow')])

    def test_remote_needs_known_remote(self):
        # Without 'origin' in remotes, a slash ref is treated as a local
        # branch (cyan), not blue.
        self.assertEqual(
            self.r._parse_decorations('origin/main', remotes=set()),
            [('origin/main', 'cyan')])
        self.assertEqual(
            self.r._parse_decorations('origin/main', remotes={'origin'}),
            [('origin/main', 'blue')])


class TestParseNameStatus(unittest.TestCase):
    """``_parse_name_status`` maps status lines to (letter, display path)."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_add_modify_delete(self):
        out = self.r._parse_name_status('A\tnew.py\nM\ta.py\nD\tgone.py\n')
        self.assertEqual(out, [
            ('A', 'new.py'),
            ('M', 'a.py'),
            ('D', 'gone.py'),
        ])

    def test_rename_shows_new_path(self):
        # 'R100\told\tnew' -> status 'R', new path is what we display + id.
        out = self.r._parse_name_status('R100\told.txt\tnew.txt\n')
        self.assertEqual(out, [('R', 'new.txt')])

    def test_copy_shows_new_path(self):
        out = self.r._parse_name_status('C75\tsrc.txt\tcopy.txt\n')
        self.assertEqual(out, [('C', 'copy.txt')])

    def test_blank_lines_ignored(self):
        self.assertEqual(self.r._parse_name_status('\n\n'), [])

    def test_status_letter_styles(self):
        # The recipe maps each letter to the spec'd palette color.
        self.assertEqual(self.r._STATUS_LETTER_STYLE['A'], 'green')
        self.assertEqual(self.r._STATUS_LETTER_STYLE['M'], 'yellow')
        self.assertEqual(self.r._STATUS_LETTER_STYLE['D'], 'red')
        self.assertEqual(self.r._STATUS_LETTER_STYLE['R'], 'cyan')


class TestReflogRow(unittest.TestCase):
    """``_reflog_row`` turns a NUL reflog record into a decorated Item."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _record(self, sha, selector, reldate, deco, subject):
        return '\x00'.join([sha, selector, reldate, deco, subject])

    def test_full_record(self):
        line = self._record(
            'deadbeef0000000000000000000000000000abcd',
            'HEAD@{0}', '2 days ago', 'HEAD -> main', 'commit: two')
        item = self.r._reflog_row(0, line)
        # id encodes the enumeration index n=0 + the sha.
        self.assertEqual(
            item.id, 'reflog:0:deadbeef0000000000000000000000000000abcd')
        self.assertEqual(item.tag, 'deadbee')
        self.assertEqual(item.tag_style, 'yellow')
        self.assertEqual(item.title, 'commit: two')
        self.assertTrue(item.has_children)
        # Selector + reldate are dim chips, then the %D decoration chips.
        self.assertEqual(item.chips, [
            ('HEAD@{0}', 'dim'),
            ('2 days ago', 'dim'),
            ('HEAD', 'green'),
            ('main', 'cyan'),
        ])

    def test_index_is_carried(self):
        # Same sha at two reflog positions -> distinct ids (no collapse).
        sha = 'cafe00000000000000000000000000000000babe'
        line = self._record(sha, 'HEAD@{3}', '1 hour ago', '', 'reset: moving')
        item = self.r._reflog_row(3, line)
        self.assertEqual(item.id, f'reflog:3:{sha}')
        self.assertEqual(item.chips, [('HEAD@{3}', 'dim'), ('1 hour ago', 'dim')])

    def test_malformed_returns_none(self):
        self.assertIsNone(self.r._reflog_row(0, 'only\x00three\x00fields'))

    def test_empty_returns_none(self):
        self.assertIsNone(self.r._reflog_row(0, ''))


class TestPorcelainParse(unittest.TestCase):
    """``_parse_porcelain_z`` turns NUL porcelain into ``[(XY, path)]``."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_one_sided_and_two_sided_codes(self):
        # Each NUL-terminated entry is 'XY<space><path>'. XY may carry a
        # space for one-sided changes; '??' is untracked.
        data = ('MM both.txt\x00'
                ' D tracked_del.txt\x00'
                ' M tracked_mod.txt\x00'
                'M  tracked_staged.txt\x00'
                'A  added.txt\x00'
                '?? untracked.txt\x00')
        self.assertEqual(self.r._parse_porcelain_z(data), [
            ('MM', 'both.txt'),
            (' D', 'tracked_del.txt'),
            (' M', 'tracked_mod.txt'),
            ('M ', 'tracked_staged.txt'),
            ('A ', 'added.txt'),
            ('??', 'untracked.txt'),
        ])

    def test_rename_uses_new_path_and_skips_old(self):
        # For a rename, '-z' emits the new path then a SECOND NUL field
        # carrying the old path; we keep the new path and drop the old.
        data = 'R  renamed_new.txt\x00renamed_old.txt\x00 M after.txt\x00'
        self.assertEqual(self.r._parse_porcelain_z(data), [
            ('R ', 'renamed_new.txt'),
            (' M', 'after.txt'),
        ])

    def test_copy_skips_old_path_too(self):
        data = 'C  copy_new.txt\x00copy_src.txt\x00'
        self.assertEqual(self.r._parse_porcelain_z(data),
                         [('C ', 'copy_new.txt')])

    def test_path_with_spaces_survives(self):
        # '-z' never quotes — a path with spaces is intact.
        data = ' M a file with spaces.txt\x00'
        self.assertEqual(self.r._parse_porcelain_z(data),
                         [(' M', 'a file with spaces.txt')])

    def test_empty_is_clean(self):
        self.assertEqual(self.r._parse_porcelain_z(''), [])


class TestStatusTag(unittest.TestCase):
    """``_status_tag`` chooses the one-letter status tag from ``XY``."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_staged_letter_wins(self):
        self.assertEqual(self.r._status_tag('M '), 'M')
        self.assertEqual(self.r._status_tag('A '), 'A')

    def test_worktree_letter_when_unstaged(self):
        self.assertEqual(self.r._status_tag(' M'), 'M')
        self.assertEqual(self.r._status_tag(' D'), 'D')

    def test_two_sided_prefers_staged(self):
        self.assertEqual(self.r._status_tag('MM'), 'M')
        self.assertEqual(self.r._status_tag('MD'), 'M')

    def test_untracked(self):
        self.assertEqual(self.r._status_tag('??'), '?')

    def test_question_mark_has_a_style(self):
        self.assertEqual(self.r._STATUS_LETTER_STYLE['?'], 'dim')


class TestStatusDiffPlan(unittest.TestCase):
    """``_status_diff_plan`` maps ``XY`` to the diff command(s) to run."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_staged_only(self):
        self.assertEqual(self.r._status_diff_plan('M ', 'f.txt'),
                         [('staged', ['diff', '--cached', '--', 'f.txt'])])

    def test_worktree_only(self):
        self.assertEqual(self.r._status_diff_plan(' M', 'f.txt'),
                         [('unstaged', ['diff', '--', 'f.txt'])])

    def test_both_sides(self):
        self.assertEqual(self.r._status_diff_plan('MM', 'f.txt'), [
            ('staged', ['diff', '--cached', '--', 'f.txt']),
            ('unstaged', ['diff', '--', 'f.txt']),
        ])

    def test_added_staged(self):
        self.assertEqual(self.r._status_diff_plan('A ', 'f.txt'),
                         [('staged', ['diff', '--cached', '--', 'f.txt'])])

    def test_deleted_worktree(self):
        self.assertEqual(self.r._status_diff_plan(' D', 'f.txt'),
                         [('unstaged', ['diff', '--', 'f.txt'])])

    def test_untracked_uses_no_index(self):
        self.assertEqual(
            self.r._status_diff_plan('??', 'f.txt'),
            [('untracked',
              ['diff', '--no-index', '--', '/dev/null', 'f.txt'])])


class TestStashIndex(unittest.TestCase):
    """``_stash_index`` extracts the 0-based index from a ``%gd`` selector."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_zero(self):
        self.assertEqual(self.r._stash_index('stash@{0}'), '0')

    def test_double_digit(self):
        self.assertEqual(self.r._stash_index('stash@{12}'), '12')

    def test_non_index_selector(self):
        self.assertIsNone(self.r._stash_index('garbage'))
        self.assertIsNone(self.r._stash_index(''))


class TestStashRow(unittest.TestCase):
    """``_stash_row`` turns a ``%gd %cr %gs`` NUL record into a stash Item."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _record(self, selector, reldate, subject):
        return '\x00'.join([selector, reldate, subject])

    def test_full_record(self):
        item = self.r._stash_row(
            self._record('stash@{0}', '2 hours ago', 'WIP on main: abc init'))
        self.assertEqual(item.id, 'stash:0')
        self.assertEqual(item.tag, 'stash@{0}')
        self.assertEqual(item.tag_style, 'yellow')
        self.assertEqual(item.title, 'WIP on main: abc init')
        self.assertTrue(item.has_children)
        self.assertEqual(item.chips, [('2 hours ago', 'dim')])

    def test_index_from_selector(self):
        item = self.r._stash_row(
            self._record('stash@{3}', '1 day ago', 'On main: hotfix'))
        # id keys on the index extracted from the selector, not enumeration.
        self.assertEqual(item.id, 'stash:3')
        self.assertEqual(item.tag, 'stash@{3}')

    def test_malformed_returns_none(self):
        self.assertIsNone(self.r._stash_row('stash@{0}\x00only-two'))

    def test_bad_selector_returns_none(self):
        # A record whose selector has no extractable index is skipped.
        self.assertIsNone(
            self.r._stash_row('garbage\x002 hours ago\x00WIP'))

    def test_empty_returns_none(self):
        self.assertIsNone(self.r._stash_row(''))


class TestForEachRefParse(unittest.TestCase):
    """``_parse_for_each_ref_line`` classifies a ref by its full prefix."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _line(self, full, short):
        return f'{full}\x00{short}'

    def test_local_branch(self):
        self.assertEqual(
            self.r._parse_for_each_ref_line(
                self._line('refs/heads/main', 'main')),
            ('branch', 'main'))

    def test_local_branch_with_slash(self):
        # A slash-bearing local branch is a branch (kind from the prefix,
        # not the short name shape).
        self.assertEqual(
            self.r._parse_for_each_ref_line(
                self._line('refs/heads/feature/x', 'feature/x')),
            ('branch', 'feature/x'))

    def test_remote(self):
        self.assertEqual(
            self.r._parse_for_each_ref_line(
                self._line('refs/remotes/origin/main', 'origin/main')),
            ('remote', 'origin/main'))

    def test_tag(self):
        self.assertEqual(
            self.r._parse_for_each_ref_line(
                self._line('refs/tags/v1.0', 'v1.0')),
            ('tag', 'v1.0'))

    def test_kind_style_palette(self):
        # The recipe colors each kind word via _REF_KIND_STYLE.
        self.assertEqual(self.r._REF_KIND_STYLE['branch'], 'cyan')
        self.assertEqual(self.r._REF_KIND_STYLE['remote'], 'blue')
        self.assertEqual(self.r._REF_KIND_STYLE['tag'], 'yellow')

    def test_unknown_namespace_is_skipped(self):
        # e.g. refs/stash and the like aren't part of the three views.
        self.assertIsNone(
            self.r._parse_for_each_ref_line(self._line('refs/stash', 'stash')))

    def test_blank_and_malformed(self):
        self.assertIsNone(self.r._parse_for_each_ref_line(''))
        self.assertIsNone(
            self.r._parse_for_each_ref_line('refs/heads/main'))  # no NUL


class TestCommitLogItems(unittest.TestCase):
    """``_commit_log_items`` stores sha/author/date columns, no sha tag."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._log_limit = 1000

    def _stub_git_log(self, records):
        """Stub ``_run_git`` so ``log`` returns ``records`` (NUL fields).

        ``remote`` returns empty (no remotes) so ``_parse_decorations``
        classifies slash refs as local branches without shelling out.
        """
        out = '\n'.join('\x00'.join(rec) for rec in records)

        def fake_run_git(*args):
            if args and args[0] == 'log':
                return subprocess.CompletedProcess(args, 0, out, '')
            if args and args[0] == 'remote':
                return subprocess.CompletedProcess(args, 0, '', '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git

    def test_columns_stored_and_no_sha_tag(self):
        # A commit with a HEAD -> main decoration: the row stores the
        # column display strings, sets no tag, and chips are the %D
        # decorations only (no trailing author·date chip).
        self._stub_git_log([
            ('deadbeefcafe1234567890abcdef000000000000',
             'HEAD -> main', 'Alice', '2 days ago', 'first subject'),
        ])
        items = self.r._commit_log_items([], [])
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it.id,
                         'commit:deadbeefcafe1234567890abcdef000000000000')
        self.assertEqual(it.title, 'first subject')
        self.assertTrue(it.has_children)

        # Sha / author / date are columns now.
        self.assertEqual(it.col_sha, 'deadbee')  # short sha (7)
        self.assertEqual(it.col_author, 'Alice')
        self.assertEqual(it.col_date, '2 days ago')

        # The sha no longer lives in the tag chip; no tag is set at all.
        self.assertEqual(getattr(it, 'tag', ''), '')
        self.assertEqual(getattr(it, 'tag_style', ''), '')

        # chips are ONLY the %D decorations — the dim author·date chip is
        # gone (author/date are columns now).
        self.assertEqual(it.chips, [('HEAD', 'green'), ('main', 'cyan')])

    def test_no_decoration_yields_empty_chips(self):
        # A bare commit (empty %D) carries no chips at all.
        self._stub_git_log([
            ('0123456789abcdef0123456789abcdef01234567',
             '', 'Bob', '5 minutes ago', 'plain subject'),
        ])
        it = self.r._commit_log_items([], [])[0]
        self.assertEqual(it.col_sha, '0123456')
        self.assertEqual(it.col_author, 'Bob')
        self.assertEqual(it.col_date, '5 minutes ago')
        self.assertEqual(it.chips, [])
        self.assertEqual(getattr(it, 'tag', ''), '')

    def test_log_failure_returns_error_row(self):
        # A non-zero git log still yields a single error Item (unchanged).
        def fake_run_git(*args):
            return subprocess.CompletedProcess(args, 1, '', 'boom')

        self.r._run_git = fake_run_git
        items = self.r._commit_log_items([], [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, '__error__')
        # The error row has no col_sha → git_row_content falls back for it.
        self.assertIsNone(getattr(items[0], 'col_sha', None))


class TestGitRowContent(unittest.TestCase):
    """``git_row_content`` builds padded columns with the subject last."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _widths(self, sha=7, author=5, date=12):
        return {'col_sha': sha, 'col_author': author, 'col_date': date}

    def _commit_item(self, **kw):
        defaults = dict(id='commit:deadbee', title='subj',
                        col_sha='deadbee', col_author='Alice',
                        col_date='2 days ago', chips=[])
        defaults.update(kw)
        return self.r.Item(**defaults)

    def test_columns_padded_then_subject_last(self):
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self._commit_item(
            col_sha='deadbee', col_author='Al', col_date='2 days ago',
            title='the subject', chips=[])
        segs = self.r.git_row_content(item, ctx)

        # Three column segments + the subject (no chips here).
        self.assertEqual(len(segs), 4)
        sha_seg, author_seg, date_seg, subject_seg = segs

        # sha column: yellow, left-justified to width 7 + 2-space gap.
        self.assertEqual(sha_seg, ('deadbee' + '  ', _YELLOW[0], _YELLOW[1]))
        # author column: dim, left-justified to width 5 ('Al' -> 'Al   ').
        self.assertEqual(author_seg, ('Al   ' + '  ', _DIM[0], _DIM[1]))
        # date column: dim, left-justified to width 12.
        self.assertEqual(date_seg,
                         ('2 days ago  ' + '  ', _DIM[0], _DIM[1]))

        # Subject comes LAST, plain (no fg, not bold) so a narrow pane
        # truncates the subject rather than the metadata columns.
        self.assertEqual(subject_seg, ('the subject', None, False))

        # Widths sourced from max_col_width per column field, in order.
        self.assertEqual(ctx.calls, ['col_sha', 'col_author', 'col_date'])

    def test_decoration_chips_between_date_and_subject(self):
        # The %D decorations render as ``[text] `` segments after the date
        # column and before the subject, styled by name.
        ctx = _FakeCtx(self._widths())
        item = self._commit_item(
            title='decorated',
            chips=[('HEAD', 'green'), ('main', 'cyan')])
        segs = self.r.git_row_content(item, ctx)
        # 3 columns + 2 chips + subject.
        self.assertEqual(len(segs), 6)
        head_seg, branch_seg = segs[3], segs[4]
        self.assertEqual(head_seg, ('[HEAD] ', *self.r.style('green')))
        self.assertEqual(branch_seg, ('[main] ', *self.r.style('cyan')))
        # Subject is still last.
        self.assertEqual(segs[-1], ('decorated', None, False))

    def test_rows_align_across_differing_lengths(self):
        # Two commits whose raw sha/author/date differ in length must, once
        # padded to the per-column max, yield equal segment widths.
        widths = self._widths(sha=7, author=7, date=12)
        a = self._commit_item(
            col_sha='abc1234', col_author='Al', col_date='2 days ago',
            title='a', chips=[])
        b = self._commit_item(
            col_sha='def5678', col_author='Bernard', col_date='3 weeks ago',
            title='bbbb', chips=[])
        segs_a = self.r.git_row_content(a, _FakeCtx(widths))
        segs_b = self.r.git_row_content(b, _FakeCtx(widths))
        # Per metadata column (sha/author/date → indices 0/1/2) the text
        # length is identical across the two rows.
        for col in range(3):
            self.assertEqual(len(segs_a[col][0]), len(segs_b[col][0]),
                             f'column {col} widths differ between rows')
        # Concrete widths: column field width + 2-space gap.
        self.assertEqual(len(segs_a[0][0]), 7 + 2)
        self.assertEqual(len(segs_a[1][0]), 7 + 2)
        self.assertEqual(len(segs_a[2][0]), 12 + 2)

    def test_worktree_group_row_aligns_label_under_subject(self):
        # A synthetic worktree-group row carries EMPTY column strings, so it
        # stays on the column path (not the fallback) and pads the three
        # leading columns to the commit widths — the label then begins at
        # the same offset as a commit subject (no decoration chips).
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self.r.Item(id='wc:untracked', title='Untracked changes',
                           col_sha='', col_author='', col_date='',
                           has_children=True)
        segs = self.r.git_row_content(item, ctx)
        # Three padded (empty) columns + the label, no chips.
        self.assertEqual(len(segs), 4)
        # Each column is just its gap-padded width of spaces.
        self.assertEqual(segs[0], (' ' * 7 + '  ', _YELLOW[0], _YELLOW[1]))
        self.assertEqual(segs[1], (' ' * 5 + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(segs[2], (' ' * 12 + '  ', _DIM[0], _DIM[1]))
        # The label is the last (subject) segment, plain — same slot a
        # commit subject occupies, so they line up vertically.
        self.assertEqual(segs[-1], ('Untracked changes', None, False))
        # Leading text width matches a commit's three columns exactly.
        commit = self._commit_item(
            col_sha='deadbee', col_author='Al', col_date='2 days ago',
            title='subj', chips=[])
        csegs = self.r.git_row_content(commit, _FakeCtx(
            self._widths(sha=7, author=5, date=12)))
        lead = lambda s: sum(len(seg[0]) for seg in s[:3])
        self.assertEqual(lead(segs), lead(csegs))

    def test_non_commit_row_falls_back(self):
        # A status/stash/ref/file row (no col_sha) must return EXACTLY
        # default_row_content(item, ctx) and never measure a column.
        ctx = _FakeCtx(self._widths())
        item = self.r.Item(id='status:M :beta.txt', title='beta.txt',
                           tag='M', tag_style='yellow', has_children=False)
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(segs,
                         [('DEFAULT', 'status:M :beta.txt', 'beta.txt')])
        # The fallback path must not measure columns.
        self.assertEqual(ctx.calls, [])

    def test_explicit_none_col_sha_also_falls_back(self):
        # Defensive: col_sha present but None still takes the fallback.
        ctx = _FakeCtx(self._widths())
        item = self.r.Item(id='__error__', title='boom', col_sha=None)
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(ctx.calls, [])


class TestParseIdWorktree(unittest.TestCase):
    """``_parse_id`` understands the ``wc:<bucket>`` worktree-group kind."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_untracked_bucket(self):
        self.assertEqual(self.r._parse_id('wc:untracked'),
                         ('wc', 'untracked'))

    def test_tracked_bucket(self):
        self.assertEqual(self.r._parse_id('wc:tracked'), ('wc', 'tracked'))

    def test_staged_bucket(self):
        self.assertEqual(self.r._parse_id('wc:staged'), ('wc', 'staged'))

    def test_conflicts_bucket(self):
        self.assertEqual(self.r._parse_id('wc:conflicts'),
                         ('wc', 'conflicts'))


class TestIsConflict(unittest.TestCase):
    """``_is_conflict`` flags the seven porcelain unmerged ``XY`` codes."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_all_unmerged_codes_are_conflicts(self):
        for xy in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'):
            self.assertTrue(self.r._is_conflict(xy), xy)

    def test_non_conflict_codes(self):
        for xy in ('MM', 'M ', ' M', '??', 'A ', ' D', 'R '):
            self.assertFalse(self.r._is_conflict(xy), xy)


class TestClassifyWorktree(unittest.TestCase):
    """``_classify_worktree`` buckets ``(XY, path)`` rows by group."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_conflict_codes_are_exclusive(self):
        # A conflict row lands ONLY in conflicts — never staged/tracked,
        # even though e.g. 'AA'/'DD' have both columns set.
        for xy in ('UU', 'AA', 'DD'):
            buckets = self.r._classify_worktree([(xy, 'c.txt')])
            self.assertEqual(buckets['conflicts'], [(xy, 'c.txt')], xy)
            self.assertEqual(buckets['staged'], [], xy)
            self.assertEqual(buckets['tracked'], [], xy)
            self.assertEqual(buckets['untracked'], [], xy)

    def test_two_sided_code_is_both_staged_and_tracked(self):
        buckets = self.r._classify_worktree([('MM', 'both.txt')])
        self.assertEqual(buckets['staged'], [('MM', 'both.txt')])
        self.assertEqual(buckets['tracked'], [('MM', 'both.txt')])
        self.assertEqual(buckets['untracked'], [])
        self.assertEqual(buckets['conflicts'], [])

    def test_untracked_only(self):
        buckets = self.r._classify_worktree([('??', 'new.txt')])
        self.assertEqual(buckets['untracked'], [('??', 'new.txt')])
        self.assertEqual(buckets['staged'], [])
        self.assertEqual(buckets['tracked'], [])
        self.assertEqual(buckets['conflicts'], [])

    def test_staged_only(self):
        buckets = self.r._classify_worktree([('M ', 's.txt')])
        self.assertEqual(buckets['staged'], [('M ', 's.txt')])
        self.assertEqual(buckets['tracked'], [])
        self.assertEqual(buckets['untracked'], [])
        self.assertEqual(buckets['conflicts'], [])

    def test_tracked_only(self):
        buckets = self.r._classify_worktree([(' M', 'w.txt')])
        self.assertEqual(buckets['tracked'], [(' M', 'w.txt')])
        self.assertEqual(buckets['staged'], [])
        self.assertEqual(buckets['untracked'], [])
        self.assertEqual(buckets['conflicts'], [])

    def test_empty_input_all_buckets_empty(self):
        buckets = self.r._classify_worktree([])
        self.assertEqual(buckets, {
            'untracked': [],
            'tracked': [],
            'staged': [],
            'conflicts': [],
        })


class TestStatusDiffPlanConflict(unittest.TestCase):
    """``_status_diff_plan`` shows one combined diff for unmerged codes."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_each_unmerged_code_yields_single_conflict_diff(self):
        for xy in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'):
            self.assertEqual(
                self.r._status_diff_plan(xy, 'f.txt'),
                [('conflict', ['diff', '--', 'f.txt'])], xy)


class TestUnmergedTagStyle(unittest.TestCase):
    """Unmerged status letters resolve to a styled tag."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_u_letter_has_a_style(self):
        self.assertIn('U', self.r._STATUS_LETTER_STYLE)
        self.assertTrue(self.r._STATUS_LETTER_STYLE['U'])

    def test_existing_conflict_letters_still_styled(self):
        # 'AA'->'A', 'DD'->'D', 'UU'->'U' all resolve to a non-empty style.
        for xy, letter in (('AA', 'A'), ('DD', 'D'), ('UU', 'U')):
            self.assertEqual(self.r._status_tag(xy), letter, xy)
            self.assertTrue(self.r._STATUS_LETTER_STYLE[letter], letter)


class TestWorktreeGroups(unittest.TestCase):
    """``_worktree_groups`` emits one expandable row per non-empty bucket."""

    def setUp(self):
        self.r = _load_recipe()

    def test_ordering_and_labels(self):
        # All four buckets non-empty: rows follow _WC_GROUPS order, ids are
        # wc:<bucket>, titles are the group labels, all expandable.
        self.r._worktree_status = lambda paths: [
            ('??', 'new.txt'),
            (' M', 'w.txt'),
            ('M ', 's.txt'),
            ('UU', 'c.txt'),
        ]
        items = self.r._worktree_groups([])
        self.assertEqual(
            [(it.id, it.title) for it in items],
            [('wc:untracked', 'Untracked changes'),
             ('wc:tracked', 'Tracked changes'),
             ('wc:staged', 'Staged changes'),
             ('wc:conflicts', 'Conflicts')])
        self.assertTrue(all(it.has_children for it in items))

    def test_rows_carry_empty_alignment_columns(self):
        # Each row leaves col_sha/col_author/col_date empty so
        # git_row_content aligns the label under the commit subjects.
        self.r._worktree_status = lambda paths: [('??', 'new.txt')]
        item, = self.r._worktree_groups([])
        self.assertEqual(
            (item.col_sha, item.col_author, item.col_date), ('', '', ''))

    def test_only_non_empty_buckets_appear(self):
        # Only untracked + staged have files → only those two rows, in order.
        self.r._worktree_status = lambda paths: [
            ('??', 'new.txt'),
            ('M ', 's.txt'),
        ]
        items = self.r._worktree_groups([])
        self.assertEqual([it.id for it in items],
                         ['wc:untracked', 'wc:staged'])

    def test_clean_tree_yields_no_rows(self):
        # A clean tree (status → []) produces no synthetic rows at all.
        self.r._worktree_status = lambda paths: []
        self.assertEqual(self.r._worktree_groups([]), [])

    def test_groups_constant_shape(self):
        # _WC_GROUPS defines BOTH order and labels for the four buckets.
        self.assertEqual(self.r._WC_GROUPS, [
            ('untracked', 'Untracked changes'),
            ('tracked', 'Tracked changes'),
            ('staged', 'Staged changes'),
            ('conflicts', 'Conflicts'),
        ])


class TestCommitsRootWorktreeScope(unittest.TestCase):
    """``_commits_root`` prepends worktree rows ONLY for a clean (no-rev) log."""

    def setUp(self):
        self.r = _load_recipe()

    def test_revs_suppress_worktree_rows(self):
        # A positional rev makes the log historical — no live wc: rows.
        self.r._revs = ['HEAD~1']
        self.r._paths = []
        sentinel = self.r.Item(id='commit:sentinel', title='s',
                               has_children=True)
        self.r._commit_log_items = lambda revs, paths: [sentinel]
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        items = self.r._commits_root()
        ids = [getattr(it, 'id', None) for it in items]
        self.assertIn('commit:sentinel', ids)
        self.assertFalse(any(str(i).startswith('wc:') for i in ids))

    def test_no_revs_prepends_worktree_rows(self):
        # With no rev, the wc: rows appear BEFORE the commit rows.
        self.r._revs = []
        self.r._paths = []
        sentinel = self.r.Item(id='commit:sentinel', title='s',
                               has_children=True)
        self.r._commit_log_items = lambda revs, paths: [sentinel]
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        items = self.r._commits_root()
        ids = [getattr(it, 'id', None) for it in items]
        self.assertEqual(ids, ['wc:staged', 'commit:sentinel'])


class TestWorktreeFiles(unittest.TestCase):
    """``_worktree_files`` returns one bucket's files as ``status:`` leaves."""

    def setUp(self):
        self.r = _load_recipe()

    def test_returns_only_that_buckets_files(self):
        self.r._worktree_status = lambda paths: [
            ('M ', 's.txt'),
            (' M', 'w.txt'),
            ('??', 'new.txt'),
        ]
        items = self.r._worktree_files('staged', [])
        self.assertEqual([it.id for it in items], ['status:M :s.txt'])
        it = items[0]
        self.assertEqual(it.title, 's.txt')
        self.assertEqual(it.tag, 'M')
        self.assertEqual(it.tag_style, 'yellow')
        self.assertFalse(it.has_children)

    def test_unknown_bucket_is_empty(self):
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        self.assertEqual(self.r._worktree_files('nope', []), [])

    def test_empty_bucket_is_empty(self):
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        self.assertEqual(self.r._worktree_files('conflicts', []), [])


class TestStatusLeafDedup(unittest.TestCase):
    """``_status_root`` and ``_worktree_files`` share ``_status_leaf``."""

    def setUp(self):
        self.r = _load_recipe()

    def test_status_root_builds_status_leaf_items(self):
        # Stub _run_git so status --porcelain -z returns canned -z text;
        # the rows _status_root builds must equal _status_leaf for the same
        # (xy, path) — proving both paths share the one constructor.
        data = 'M  s.txt\x00 M w.txt\x00?? new.txt\x00'

        def fake_run_git(*args):
            if args and args[0] == 'status':
                return subprocess.CompletedProcess(args, 0, data, '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git
        items = self.r._status_root()
        expected = [self.r._status_leaf(xy, path) for xy, path in (
            ('M ', 's.txt'), (' M', 'w.txt'), ('??', 'new.txt'))]
        self.assertEqual(
            [(it.id, it.tag, it.tag_style, it.title, it.has_children)
             for it in items],
            [(e.id, e.tag, e.tag_style, e.title, e.has_children)
             for e in expected])

    def test_status_leaf_shape(self):
        leaf = self.r._status_leaf('??', 'new.txt')
        self.assertEqual(leaf.id, 'status:??:new.txt')
        self.assertEqual(leaf.title, 'new.txt')
        self.assertEqual(leaf.tag, '?')
        self.assertEqual(leaf.tag_style, 'dim')
        self.assertFalse(leaf.has_children)


class TestGraphTranslate(unittest.TestCase):
    """``_graph_translate`` glyph-substitutes git's ASCII graph art."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_mapped_glyphs_substituted(self):
        # The node / lane / horizontal art chars are substituted 1:1.
        self.assertEqual(self.r._graph_translate('*'), '•')  # • node
        self.assertEqual(self.r._graph_translate('|'), '│')  # │ vertical
        self.assertEqual(self.r._graph_translate('_'), '▁')  # ▁ horizontal

    def test_diagonals_pass_through(self):
        # The merge diagonals are left as git's own ASCII art.
        self.assertEqual(self.r._graph_translate('/'), '/')    # asc diag
        self.assertEqual(self.r._graph_translate('\\'), '\\')  # desc diag

    def test_internal_spacing_preserved(self):
        # A multi-lane row keeps its inter-lane spaces (git's alignment);
        # only the trailing pad is stripped.
        self.assertEqual(
            self.r._graph_translate('| * | '),
            '│ • │')

    def test_merge_fanout_mixes_box_and_ascii(self):
        # A typical merge fan-out: the vertical lane is substituted while
        # the diagonal passes through — '|\' -> '│\', '|/' -> '│/'.
        self.assertEqual(self.r._graph_translate('|\\  '), '│\\')
        self.assertEqual(self.r._graph_translate('|/  '), '│/')

    def test_trailing_spaces_only_rstripped(self):
        # Leading/internal spaces stay; trailing run goes.
        self.assertEqual(self.r._graph_translate('  *   '), '  •')

    def test_unmapped_chars_pass_through(self):
        # Chars outside the map are untouched.
        self.assertEqual(self.r._graph_translate('* x'), '• x')


class TestCommitGraphItems(unittest.TestCase):
    """``_commit_graph_items`` builds commit + inert filler rows from --graph."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._log_limit = 1000

    def _stub_graph_log(self, lines):
        """Stub ``_run_git`` so ``log --graph`` returns ``lines`` verbatim.

        Each element of ``lines`` is one already-formed output line (art +
        optional ``\\x1f``-joined fields); they're joined with newlines.
        ``remote`` returns empty so decoration parsing needs no shell-out.
        """
        out = '\n'.join(lines)

        def fake_run_git(*args):
            if args and args[0] == 'log':
                # The --graph flag must be present in tree mode.
                self.assertIn('--graph', args)
                return subprocess.CompletedProcess(args, 0, out, '')
            if args and args[0] == 'remote':
                return subprocess.CompletedProcess(args, 0, '', '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git

    def _commit_line(self, art, sha, an, ar, s, d):
        # Mirror the recipe format: <art>\x1f%H\x1f%an\x1f%ar\x1f%s\x1f%D.
        return art + '\x1f'.join(['', sha, an, ar, s, d])

    def test_commit_line_builds_columnar_item_with_graph(self):
        sha = 'deadbeefcafe1234567890abcdef000000000000'
        self._stub_graph_log([
            self._commit_line('* ', sha, 'Alice', '2 days ago',
                              'first subject', 'HEAD -> main'),
        ])
        items = self.r._commit_graph_items([], [], 'root')
        self.assertEqual(len(items), 1)
        it = items[0]
        # Same columnar commit Item as the plain builder…
        self.assertEqual(it.id, f'commit:{sha}')
        self.assertEqual(it.title, 'first subject')
        self.assertTrue(it.has_children)
        self.assertEqual(it.col_sha, 'deadbee')
        self.assertEqual(it.col_author, 'Alice')
        self.assertEqual(it.col_date, '2 days ago')
        self.assertEqual(it.chips, [('HEAD', 'green'), ('main', 'cyan')])
        # …plus the translated graph art ('* ' -> '•').
        self.assertEqual(it.col_graph, '•')

    def test_filler_line_builds_inert_item(self):
        # A pure-art line (no \x1f) is a filler: inert, no col_sha, art only.
        sha = '0123456789abcdef0123456789abcdef01234567'
        self._stub_graph_log([
            self._commit_line('* ', sha, 'Bob', '1 hour ago', 'subj', ''),
            '|\\  ',
        ])
        items = self.r._commit_graph_items([], [], 'root')
        self.assertEqual(len(items), 2)
        filler = items[1]
        # Filler ids are namespaced by the build (ns='root' here) then a
        # per-build running counter.
        self.assertEqual(filler.id, 'filler:root:0')
        self.assertEqual(filler.title, '')
        self.assertFalse(filler.has_children)
        # No col_sha on a filler (so git_row_content takes the filler path).
        self.assertIsNone(getattr(filler, 'col_sha', None))
        # The whole line is the (translated) art: '|\' -> '│\' (the lane
        # becomes box-vertical, the diagonal passes through).
        self.assertEqual(filler.col_graph, '│\\')
        # _parse_id partitions on the first ':' so a namespaced filler id is
        # still kind 'other' (inert everywhere); rest keeps the ns:n tail.
        self.assertEqual(self.r._parse_id(filler.id), ('other', 'root:0'))

    def test_order_preserved_and_filler_indices_run(self):
        # Commits + fillers interleave in git's emitted order; filler ids
        # carry the build's ns then a unique running index.
        s1 = 'a' * 40
        s2 = 'b' * 40
        self._stub_graph_log([
            self._commit_line('* ', s1, 'A', 'now', 's1', ''),
            '|\\  ',
            self._commit_line('| * ', s2, 'B', 'now', 's2', ''),
            '|/  ',
        ])
        items = self.r._commit_graph_items([], [], 'root')
        self.assertEqual([it.id for it in items], [
            f'commit:{s1}', 'filler:root:0', f'commit:{s2}', 'filler:root:1',
        ])
        # The second commit's art keeps its leading lane: '| * ' -> '│ •'.
        self.assertEqual(items[2].col_graph, '│ •')

    def test_log_failure_returns_error_row(self):
        def fake_run_git(*args):
            return subprocess.CompletedProcess(args, 1, '', 'boom')

        self.r._run_git = fake_run_git
        items = self.r._commit_graph_items([], [], 'root')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, '__error__')

    def test_revs_and_paths_threaded_into_args(self):
        # The rev/path args + the -n limit reach git log (alongside --graph).
        captured = {}

        def fake_run_git(*args):
            if args and args[0] == 'log':
                captured['args'] = args
                return subprocess.CompletedProcess(args, 0, '', '')
            if args and args[0] == 'remote':
                return subprocess.CompletedProcess(args, 0, '', '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git
        self.r._log_limit = 42
        self.r._commit_graph_items(['HEAD~5'], ['src/'], 'root')
        args = captured['args']
        self.assertIn('--graph', args)
        self.assertIn('--no-color', args)
        self.assertIn('-n', args)
        self.assertIn('42', args)
        self.assertIn('HEAD~5', args)
        # Pathspec passed after a '--' sentinel.
        self.assertIn('--', args)
        self.assertEqual(args[args.index('--') + 1], 'src/')


class TestLogItemsRouting(unittest.TestCase):
    """``_log_items`` picks the graph vs plain builder on ``_tree_mode``."""

    def setUp(self):
        self.r = _load_recipe()

    def test_routes_to_plain_when_tree_off(self):
        # Tree off: the plain builder (no namespace) is used; ns is ignored.
        self.r._tree_mode = False
        self.r._commit_log_items = lambda revs, paths: ['PLAIN', revs, paths]
        self.r._commit_graph_items = (
            lambda revs, paths, ns: ['GRAPH', revs, paths, ns])
        self.assertEqual(self.r._log_items(['r'], ['p'], ns='root'),
                         ['PLAIN', ['r'], ['p']])

    def test_routes_to_graph_when_tree_on_threading_ns(self):
        # Tree on: the graph builder is used and the ns is threaded through.
        self.r._tree_mode = True
        self.r._commit_log_items = lambda revs, paths: ['PLAIN', revs, paths]
        self.r._commit_graph_items = (
            lambda revs, paths, ns: ['GRAPH', revs, paths, ns])
        self.assertEqual(self.r._log_items(['r'], ['p'], ns='ref:feat'),
                         ['GRAPH', ['r'], ['p'], 'ref:feat'])


class TestGitRowContentGraph(unittest.TestCase):
    """``git_row_content`` renders the graph column + filler blank-pad."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _widths(self, sha=7, author=5, date=12):
        return {'col_sha': sha, 'col_author': author, 'col_date': date}

    def test_commit_graph_inserted_after_date_before_chips(self):
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self.r.Item(
            id='commit:deadbee', title='subj', col_sha='deadbee',
            col_author='Al', col_date='2 days ago',
            chips=[('HEAD', 'green')], col_graph='•')
        segs = self.r.git_row_content(item, ctx)
        # sha, author, date, GRAPH, [HEAD], subject.
        self.assertEqual(len(segs), 6)
        graph_seg = segs[3]
        # Graph art + a single trailing space; GFG is terminal default.
        self.assertEqual(graph_seg, ('• ', None, False))
        # The chip follows the graph, the subject is still last.
        self.assertEqual(segs[4], ('[HEAD] ', *self.r.style('green')))
        self.assertEqual(segs[-1], ('subj', None, False))

    def test_tree_off_commit_row_unchanged(self):
        # A commit row WITHOUT col_graph (tree off) is byte-identical to the
        # pre-feature output: exactly sha/author/date + chips + subject, no
        # graph segment anywhere.
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self.r.Item(
            id='commit:deadbee', title='subj', col_sha='deadbee',
            col_author='Al', col_date='2 days ago', chips=[])
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(segs, [
            ('deadbee' + '  ', *_YELLOW),
            ('Al   ' + '  ', *_DIM),
            ('2 days ago  ' + '  ', *_DIM),
            ('subj', None, False),
        ])

    def test_filler_row_blank_pad_then_art(self):
        # A filler (col_graph set, no col_sha) blank-pads the sha+author+date
        # span then renders its art; both segments use the graph fg.
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self.r.Item(id='filler:root:0', title='', has_children=False,
                           col_graph='│\\')
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(len(segs), 2)
        pad_seg, art_seg = segs
        # pad width = 7+2 + 5+2 + 12+2 = 30 spaces.
        expected_pad = ' ' * (7 + 2 + 5 + 2 + 12 + 2)
        self.assertEqual(pad_seg, (expected_pad, None, False))
        self.assertEqual(art_seg, ('│\\', None, False))
        # The filler measured exactly the three metadata columns.
        self.assertEqual(ctx.calls, ['col_sha', 'col_author', 'col_date'])

    def test_filler_pad_aligns_with_commit_graph_column(self):
        # The filler's blank pad must equal the commit row's sha+author+date
        # prefix width so the two graph columns line up vertically.
        widths = self._widths(sha=7, author=7, date=12)
        commit = self.r.Item(
            id='commit:abc1234', title='c', col_sha='abc1234',
            col_author='Bernard', col_date='3 weeks ago', chips=[],
            col_graph='•')
        filler = self.r.Item(id='filler:root:0', title='', has_children=False,
                             col_graph='│')
        c_segs = self.r.git_row_content(commit, _FakeCtx(widths))
        f_segs = self.r.git_row_content(filler, _FakeCtx(widths))
        # Sum of the commit's three metadata column widths == filler pad len.
        prefix = sum(len(c_segs[i][0]) for i in range(3))
        self.assertEqual(len(f_segs[0][0]), prefix)

    def test_non_commit_non_filler_still_falls_back(self):
        # A row with neither col_sha nor col_graph (status/ref/etc.) still
        # falls back to default_row_content and measures no column.
        ctx = _FakeCtx(self._widths())
        item = self.r.Item(id='status:M :beta.txt', title='beta.txt',
                           tag='M', tag_style='yellow', has_children=False)
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(ctx.calls, [])


class TestPopTreeArg(unittest.TestCase):
    """``_pop_tree_arg`` pops --tree / --no-tree (last wins)."""

    def setUp(self):
        self.r = _load_recipe()
        self._orig_argv = sys.argv

    def tearDown(self):
        sys.argv = self._orig_argv

    def test_absent_returns_default(self):
        sys.argv = ['browse-git', 'HEAD']
        self.assertFalse(self.r._pop_tree_arg(False))
        self.assertTrue(self.r._pop_tree_arg(True))
        # argv untouched when the flag is absent.
        self.assertEqual(sys.argv, ['browse-git', 'HEAD'])

    def test_tree_sets_true_and_pops(self):
        sys.argv = ['browse-git', '--tree', 'HEAD']
        self.assertTrue(self.r._pop_tree_arg(False))
        self.assertEqual(sys.argv, ['browse-git', 'HEAD'])

    def test_no_tree_sets_false_and_pops(self):
        sys.argv = ['browse-git', '--no-tree']
        self.assertFalse(self.r._pop_tree_arg(True))
        self.assertEqual(sys.argv, ['browse-git'])

    def test_last_flag_wins_and_all_popped(self):
        sys.argv = ['browse-git', '--tree', '--no-tree']
        self.assertFalse(self.r._pop_tree_arg(False))
        self.assertEqual(sys.argv, ['browse-git'])
        sys.argv = ['browse-git', '--no-tree', '--tree']
        self.assertTrue(self.r._pop_tree_arg(False))
        self.assertEqual(sys.argv, ['browse-git'])


class TestToggleTree(unittest.TestCase):
    """``toggle_tree`` flips ``_tree_mode`` and refreshes the root."""

    def setUp(self):
        self.r = _load_recipe()

    def test_flip_and_refresh(self):
        calls = {'refresh': 0, 'messages': []}

        class Ctx:
            def message(self, text):
                calls['messages'].append(text)

            def refresh(self, id=None, on_complete=None):
                calls['refresh'] += 1

        ctx = Ctx()
        self.r._tree_mode = False
        self.r.toggle_tree(ctx)
        self.assertTrue(self.r._tree_mode)
        self.assertEqual(calls['refresh'], 1)
        self.assertEqual(calls['messages'], ['commit graph: on'])
        # A second toggle flips it back and refreshes again.
        self.r.toggle_tree(ctx)
        self.assertFalse(self.r._tree_mode)
        self.assertEqual(calls['refresh'], 2)
        self.assertEqual(calls['messages'][-1], 'commit graph: off')


class _FakeCursorItem:
    """An Item stand-in for the cursor: only ``.id`` is read by the hook."""

    def __init__(self, item_id):
        self.id = item_id


class _FakeCursorCtx:
    """A ``RowContext`` stand-in for the ``on_cursor_change`` hook.

    Exposes the three surfaces ``_skip_fillers`` reads — ``cursor`` (an
    item with ``.id``), ``cursor_index`` (its position), and ``cursor_to``
    (records every requested target id in ``moves``). The cursor itself is
    set from an ``(id, index)`` pair so a test can model the row the cursor
    rests on independently of where it would move next.
    """

    def __init__(self, cur_id, cursor_index):
        self.cursor = _FakeCursorItem(cur_id)
        self.cursor_index = cursor_index
        self.moves = []

    def cursor_to(self, id, on_complete=None):
        self.moves.append(id)


class TestSkipFillers(unittest.TestCase):
    """``_skip_fillers`` bounces up/down off filler rows to the next commit.

    The synthetic ordered list mirrors what ``_commit_graph_items`` records:
    commits and fillers interleaved in git order, stored in the module dict
    ``_graph_rows_by_ns`` keyed by build namespace, each value a list of
    ``(id, is_filler)`` pairs. Filler ids are namespaced (``filler:<ns>:<n>``)
    so they're globally unique across builds. ``cursor_index`` is only ever
    used to infer travel direction (sign of the delta from the previous
    index); the actual neighbour search walks the owning ns's list by id
    position, never by cursor index.
    """

    # Ordered build list under ns 'root': commit, filler, filler, commit,
    # filler, commit. Indices: 0 1 2 3 4 5.
    _ROWS = [
        ('commit:aaaa', False),
        ('filler:root:0', True),
        ('filler:root:1', True),
        ('commit:bbbb', False),
        ('filler:root:2', True),
        ('commit:cccc', False),
    ]

    def setUp(self):
        self.r = _load_recipe()
        self.r._tree_mode = True
        self.r._graph_rows_by_ns = {'root': list(self._ROWS)}
        # Seed prev index to a sentinel that doesn't bias direction; each
        # test sets it explicitly to model the prior cursor position.
        self.r._prev_cursor_index = 0

    def test_down_into_single_filler_skips_to_next_commit_below(self):
        # Was on commit:aaaa (idx 0); pressed down onto filler:root:0 (idx 1).
        self.r._prev_cursor_index = 0
        ctx = _FakeCursorCtx('filler:root:0', 1)
        self.r._skip_fillers(ctx, 'filler:root:0')
        # Down direction -> next non-filler below filler:root:0 is commit:bbbb.
        self.assertEqual(ctx.moves, ['commit:bbbb'])

    def test_up_into_single_filler_skips_to_next_commit_above(self):
        # Lone filler:root:2 (idx 4) reached by pressing up FROM commit:cccc
        # (5) -> nearest non-filler above is commit:bbbb.
        self.r._prev_cursor_index = 5
        ctx = _FakeCursorCtx('filler:root:2', 4)
        self.r._skip_fillers(ctx, 'filler:root:2')
        self.assertEqual(ctx.moves, ['commit:bbbb'])

    def test_down_through_run_of_fillers_skips_all(self):
        # A run of two consecutive fillers. Coming down from commit:aaaa onto
        # the FIRST filler must skip the whole run to commit:bbbb.
        self.r._prev_cursor_index = 0
        ctx = _FakeCursorCtx('filler:root:0', 1)
        self.r._skip_fillers(ctx, 'filler:root:0')
        self.assertEqual(ctx.moves, ['commit:bbbb'])

    def test_up_through_run_of_fillers_skips_all(self):
        # Coming UP from commit:bbbb (idx 3) onto filler:root:1 (idx 2) must
        # skip back past both fillers to commit:aaaa.
        self.r._prev_cursor_index = 3
        ctx = _FakeCursorCtx('filler:root:1', 2)
        self.r._skip_fillers(ctx, 'filler:root:1')
        self.assertEqual(ctx.moves, ['commit:aaaa'])

    def test_bottom_edge_filler_reverses_up(self):
        # A filler that is the LAST row with no commit below it: travelling
        # down must reverse and land on the nearest commit above.
        rows = [
            ('commit:aaaa', False),
            ('filler:root:0', True),
            ('filler:root:1', True),
        ]
        self.r._graph_rows_by_ns = {'root': rows}
        # Pressed down (prev 0 -> now 2) onto the trailing filler.
        self.r._prev_cursor_index = 0
        ctx = _FakeCursorCtx('filler:root:1', 2)
        self.r._skip_fillers(ctx, 'filler:root:1')
        # No commit below -> reverse: nearest commit above is commit:aaaa.
        self.assertEqual(ctx.moves, ['commit:aaaa'])

    def test_top_edge_filler_reverses_down(self):
        # A filler that is the FIRST row with no commit above it: travelling
        # up must reverse and land on the nearest commit below.
        rows = [
            ('filler:root:0', True),
            ('filler:root:1', True),
            ('commit:bbbb', False),
        ]
        self.r._graph_rows_by_ns = {'root': rows}
        # Pressed up (prev 2 -> now 0) onto the leading filler.
        self.r._prev_cursor_index = 2
        ctx = _FakeCursorCtx('filler:root:0', 0)
        self.r._skip_fillers(ctx, 'filler:root:0')
        # No commit above -> reverse: nearest commit below is commit:bbbb.
        self.assertEqual(ctx.moves, ['commit:bbbb'])

    def test_equal_index_defaults_to_down(self):
        # Defensive: if cursor_index == prev_index (no movement delta), the
        # direction defaults to +1 (down) per the spec.
        self.r._prev_cursor_index = 1
        ctx = _FakeCursorCtx('filler:root:0', 1)
        self.r._skip_fillers(ctx, 'filler:root:0')
        self.assertEqual(ctx.moves, ['commit:bbbb'])

    def test_landing_on_commit_records_index_and_no_move(self):
        # cur_id is a commit (not a filler): the hook must just record the
        # position (for the NEXT move's direction inference) and never move.
        self.r._prev_cursor_index = 99
        ctx = _FakeCursorCtx('commit:bbbb', 3)
        self.r._skip_fillers(ctx, 'commit:bbbb')
        self.assertEqual(ctx.moves, [])
        self.assertEqual(self.r._prev_cursor_index, 3)

    def test_bounce_does_not_update_prev_index(self):
        # On the bounce fire (cur_id IS a filler), prev_index must NOT be set
        # to the filler's index — it stays so the non-filler re-fire sets it.
        self.r._prev_cursor_index = 0
        ctx = _FakeCursorCtx('filler:root:0', 1)
        self.r._skip_fillers(ctx, 'filler:root:0')
        # prev_index unchanged by the bounce (still 0, not the filler's 1).
        self.assertEqual(self.r._prev_cursor_index, 0)

    def test_no_infinite_loop_reentry_on_nonfiller_refire(self):
        # Model the framework's async re-fire: cursor_to(commit) settles, the
        # hook fires AGAIN with the non-filler id at its new index. That
        # second fire must NOT issue another move (only records position).
        self.r._prev_cursor_index = 0
        ctx = _FakeCursorCtx('filler:root:0', 1)
        self.r._skip_fillers(ctx, 'filler:root:0')
        self.assertEqual(ctx.moves, ['commit:bbbb'])
        # Re-fire with the landed-on commit (index 3 in _ROWS).
        ctx2 = _FakeCursorCtx('commit:bbbb', 3)
        self.r._skip_fillers(ctx2, 'commit:bbbb')
        self.assertEqual(ctx2.moves, [])
        self.assertEqual(self.r._prev_cursor_index, 3)

    def test_tree_off_is_noop_records_index(self):
        # With tree mode off there are no fillers; even a filler-looking id
        # is ignored — record the position and return (no move).
        self.r._tree_mode = False
        self.r._prev_cursor_index = 99
        ctx = _FakeCursorCtx('filler:root:0', 1)
        self.r._skip_fillers(ctx, 'filler:root:0')
        self.assertEqual(ctx.moves, [])
        self.assertEqual(self.r._prev_cursor_index, 1)

    def test_none_cur_id_is_noop(self):
        # A placeholder / scope-root row (cur_id None) just records and
        # returns — never indexes the ordered list.
        self.r._prev_cursor_index = 99
        ctx = _FakeCursorCtx(None, 2)
        self.r._skip_fillers(ctx, None)
        self.assertEqual(ctx.moves, [])
        self.assertEqual(self.r._prev_cursor_index, 2)

    def test_filler_absent_from_any_list_is_noop(self):
        # Defensive: a filler id present in no ns list (e.g. a stale cursor
        # after a refresh) can't be located, so no move is issued.
        self.r._prev_cursor_index = 0
        ctx = _FakeCursorCtx('filler:gone:9', 1)
        self.r._skip_fillers(ctx, 'filler:gone:9')
        self.assertEqual(ctx.moves, [])

    def test_cross_ref_namespaces_do_not_collide(self):
        # The reviewer's branches-mode scenario: two refs drilled in, each its
        # own ns. Both builds happen to assign filler:<ns>:0, but the ns makes
        # the ids distinct, and each list resolves to ITS OWN neighbour commit
        # — never the other ref's. Lists are kept side by side (a fresh build
        # of one ref does not clobber the other).
        self.r._graph_rows_by_ns = {
            'ref:A': [
                ('commit:a1', False),
                ('filler:ref:A:0', True),
                ('commit:a2', False),
            ],
            'ref:B': [
                ('commit:b1', False),
                ('filler:ref:B:0', True),
                ('commit:b2', False),
            ],
        }
        # Bounce DOWN on A's filler -> A's neighbour below (commit:a2), not B's.
        self.r._prev_cursor_index = 0
        ctx_a = _FakeCursorCtx('filler:ref:A:0', 1)
        self.r._skip_fillers(ctx_a, 'filler:ref:A:0')
        self.assertEqual(ctx_a.moves, ['commit:a2'])
        # Bounce UP on B's filler -> B's neighbour above (commit:b1), not A's.
        self.r._prev_cursor_index = 5
        ctx_b = _FakeCursorCtx('filler:ref:B:0', 1)
        self.r._skip_fillers(ctx_b, 'filler:ref:B:0')
        self.assertEqual(ctx_b.moves, ['commit:b1'])

    def test_commit_graph_items_records_list_under_ns(self):
        # The builder must record its ordered (id, is_filler) list under the
        # build's ns so the hook can scan it later; filler ids carry the ns.
        self.r._log_limit = 1000
        s1 = 'a' * 40
        s2 = 'b' * 40
        out = '\n'.join([
            '\x1f'.join(['* ', s1, 'A', 'now', 's1', '']),
            '|\\  ',
            '\x1f'.join(['| * ', s2, 'B', 'now', 's2', '']),
        ])

        def fake_run_git(*args):
            if args and args[0] == 'log':
                return subprocess.CompletedProcess(args, 0, out, '')
            if args and args[0] == 'remote':
                return subprocess.CompletedProcess(args, 0, '', '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git
        self.r._graph_rows_by_ns = {}
        self.r._commit_graph_items([], [], 'ref:feat')
        self.assertEqual(self.r._graph_rows_by_ns['ref:feat'], [
            (f'commit:{s1}', False),
            ('filler:ref:feat:0', True),
            (f'commit:{s2}', False),
        ])

    def test_concurrent_builds_keep_separate_entries(self):
        # Two builds (root + a ref) each write their own ns entry; neither
        # clobbers the other, so both lists remain available to the hook.
        self.r._log_limit = 1000
        s1 = 'a' * 40

        def make_out(sha):
            return '\x1f'.join(['* ', sha, 'A', 'now', 's', ''])

        def fake_run_git(*args):
            if args and args[0] == 'log':
                return subprocess.CompletedProcess(args, 0, make_out(s1), '')
            if args and args[0] == 'remote':
                return subprocess.CompletedProcess(args, 0, '', '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git
        self.r._graph_rows_by_ns = {}
        self.r._commit_graph_items([], [], 'root')
        self.r._commit_graph_items([], [], 'ref:x')
        self.assertEqual(set(self.r._graph_rows_by_ns), {'root', 'ref:x'})


class _FakeSelItem:
    """An Item stand-in for a selected row: only ``.id`` is read."""

    def __init__(self, item_id):
        self.id = item_id


class _FakeSelCtx:
    """Stand-in for the ``on_selection_change`` ctx.

    Exposes ``selected`` (the Items currently selected), ``select(ids,
    replace)`` (records calls and updates the set), and the cursor surfaces
    the shared bounce reads (``cursor`` / ``cursor_index`` / ``cursor_to``).
    """

    def __init__(self, selected_ids, cur_id, cursor_index):
        self._sel = list(selected_ids)
        self.cursor = _FakeCursorItem(cur_id) if cur_id is not None else None
        self.cursor_index = cursor_index
        self.moves = []
        self.select_calls = []

    @property
    def selected(self):
        return [_FakeSelItem(i) for i in self._sel]

    def select(self, ids, replace=False):
        self.select_calls.append((list(ids), replace))
        self._sel = (list(ids) if replace
                     else self._sel + [i for i in ids if i not in self._sel])

    def cursor_to(self, id, on_complete=None):
        self.moves.append(id)


class TestSelectionChange(unittest.TestCase):
    """``_on_selection_change`` keeps fillers unselectable and bounces the
    cursor off a filler after a space / alt-space select-and-move (which
    mutates ``state.cursor`` directly and so bypasses ``on_cursor_change``).
    """

    _ROWS = [
        ('commit:aaaa', False),
        ('filler:root:0', True),
        ('commit:bbbb', False),
    ]

    def setUp(self):
        self.r = _load_recipe()
        self.r._tree_mode = True
        self.r._graph_rows_by_ns = {'root': list(self._ROWS)}
        self.r._prev_cursor_index = 0

    def test_filler_stripped_from_selection(self):
        # A filler that slipped into the selection is removed (unselectable);
        # the cursor is on a commit, so no bounce.
        ctx = _FakeSelCtx(['commit:aaaa', 'filler:root:0'], 'commit:aaaa', 0)
        self.r._on_selection_change(ctx, list(ctx._sel))
        self.assertEqual(ctx._sel, ['commit:aaaa'])
        self.assertEqual(ctx.select_calls, [(['commit:aaaa'], True)])
        self.assertEqual(ctx.moves, [])

    def test_no_strip_when_selection_has_no_fillers(self):
        ctx = _FakeSelCtx(['commit:aaaa', 'commit:bbbb'], 'commit:bbbb', 2)
        self.r._on_selection_change(ctx, list(ctx._sel))
        self.assertEqual(ctx.select_calls, [])
        self.assertEqual(ctx.moves, [])

    def test_select_all_strips_every_filler(self):
        ctx = _FakeSelCtx(['commit:aaaa', 'filler:root:0', 'commit:bbbb'],
                          'commit:aaaa', 0)
        self.r._on_selection_change(ctx, list(ctx._sel))
        self.assertEqual(ctx._sel, ['commit:aaaa', 'commit:bbbb'])

    def test_bounce_off_filler_after_space_move(self):
        # Space toggled commit:aaaa (idx 0) then stepped onto a filler (idx 1):
        # no filler in the selection, but the cursor must skip past it.
        self.r._prev_cursor_index = 0
        ctx = _FakeSelCtx(['commit:aaaa'], 'filler:root:0', 1)
        self.r._on_selection_change(ctx, list(ctx._sel))
        self.assertEqual(ctx.select_calls, [])
        self.assertEqual(ctx.moves, ['commit:bbbb'])

    def test_reentry_after_strip_terminates(self):
        # The real ctx.select re-fires the hook; a second pass on the now-clean
        # selection must not strip again (no loop).
        ctx = _FakeSelCtx(['filler:root:0', 'commit:aaaa'], 'commit:aaaa', 0)
        self.r._on_selection_change(ctx, list(ctx._sel))
        n = len(ctx.select_calls)
        self.r._on_selection_change(ctx, list(ctx._sel))
        self.assertEqual(len(ctx.select_calls), n)


if __name__ == '__main__':
    unittest.main()
