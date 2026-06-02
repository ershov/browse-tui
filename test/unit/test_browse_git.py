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


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    Always installs a fresh module so a stub left behind by another
    recipe's unit test doesn't bleed in. ``Item`` keeps its kwargs as
    attributes so the children-builder tests can read ``.id`` / ``.tag``
    if needed; ``Browser`` / ``BrowserConfig`` / ``Action`` are inert.
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


if __name__ == '__main__':
    unittest.main()
