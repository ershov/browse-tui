"""Unit tests for the ``recipes/browse-fs`` context menu (ticket #1034).

browse-fs items are absolute paths: a ``str`` id is a real file/directory
(distinguished by ``os.path.isdir``), while a tuple id is a synthetic row —
``('missing', label, path)`` / ``('err', path)`` / ``('launch', …)``. The
context menu branches on that kind. Following the committed convention
(browse-procs / git / claude / md / plan) the option list is a PURE builder,
``context_menu_options(ctx)``, that inspects ``ctx.cursor`` / ``ctx.selected``
and returns ``(label, token)`` rows WITHOUT opening a modal; a flat
``{token: handler}`` table (``_MENU_ACTIONS``) dispatches the chosen token.

We exercise the builder against a REAL headless ``Browser`` / ``Context``
(from ``test.async_._helpers``) with a known path item under the cursor — not a
fake ctx. browse-tui swallows ``on_context_menu`` exceptions and a fake ctx
hides bugs, so the real ``Context.cursor`` / ``Context.selected`` /
``Context.targets`` read paths are what we assert against; ``ctx.menu`` itself
short-circuits to ``None`` in headless mode, which is exactly why the builder is
split out and tested directly. The git-mode gate (``_is_git_repo``) is exercised
against a REAL throwaway temp dir with a ``.git`` marker.

SAFETY: browse-fs has DESTRUCTIVE actions (Delete = rm, New file/dir = create).
These tests NEVER exercise a mutating handler against real files — they assert
the PURE options-builder / dispatch wiring. Any real path lives under a
``tempfile.TemporaryDirectory``; nothing is created or deleted outside one.

The recipe is a ``--run-py`` script that imports ``browse_tui`` (only a real
module when the binary loads it), so we stub ``browse_tui`` in ``sys.modules``
and load the extension-less recipe via ``SourceFileLoader`` — the same pattern
as ``test/unit/test_browse_fs.py`` / ``test_browse_git_context_menu.py``.
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
import unittest.mock
from importlib.machinery import SourceFileLoader
from pathlib import Path

from test.async_._helpers import Context, Item, make_browser


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-fs'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    The recipe pulls ``Action`` / ``Browser`` / ``BrowserConfig`` / ``Item``
    and the column helpers from ``browse_tui``; none are exercised by the pure
    builders under test (the cursor item comes from the REAL Browser below), so
    inert / minimal stubs are enough to let the module load. A fresh module
    each call keeps a stub left by another recipe's test from bleeding in.
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
    mod.cell_rjust = lambda s, width, fill=' ': s.rjust(width, fill)
    mod.style = lambda name: (None, False)
    mod.default_row_content = lambda item, ctx: []
    mod.recipe_argv = lambda: []
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-fs recipe; returns a fresh module."""
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
    _stub_browse_tui()
    name = '_browse_fs_cm_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _browser_with_item(item, *, extra=()):
    """A real headless Browser whose cursor sits on ``item``.

    ``extra`` are sibling rows listed alongside ``item`` (so a multi-row
    selection can be built). The cursor is parked on ``item`` via ``cursor_to``
    after the root children settle, mirroring the git/procs context-menu tests.
    """
    rows = [item, *extra]
    b = make_browser(get_children=lambda _id, *, reload=False: list(rows))
    b.refresh()
    b.run_until_idle()
    b.cursor_to(item.id)
    b.run_until_idle()
    return b


def _labels(rows):
    return [label for label, _tok in rows]


def _tokens(rows):
    return [tok for _label, tok in rows]


class TestPerKindMenus(unittest.TestCase):
    """``context_menu_options`` returns the right rows for each fs kind.

    Each case parks a real cursor on one kind of row and asserts the
    kind-specific labels / tokens. Real paths live under a per-test temp dir.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.d = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self.d, ignore_errors=True))

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def _ctx(self, item, extra=()):
        self.b = _browser_with_item(item, extra=extra)
        return Context(self.b)

    def _file(self, name, body=''):
        p = os.path.join(self.d, name)
        with open(p, 'w') as f:
            f.write(body)
        return p

    def _subdir(self, name):
        p = os.path.join(self.d, name)
        os.mkdir(p)
        return p

    # -- directory --------------------------------------------------------

    def test_dir_menu_rows_non_repo(self):
        # A plain (non-git) directory: run shell, new file/dir, show path,
        # delete — and NO 'git' submenu row.
        d = self._subdir('plain')
        item = Item(id=d, title='plain/', has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), [
            'dir.shell', 'dir.newfile', 'dir.newdir', 'dir.path', 'delete',
        ])
        # No git submenu row on a non-repo dir.
        self.assertNotIn('dir.git', _tokens(rows))

    def test_dir_menu_rows_git_repo(self):
        # A directory carrying a .git marker gets a single 'git' submenu row,
        # between Run-shell-here and New file/dir (the five modes nest under it).
        d = self._subdir('repo')
        os.mkdir(os.path.join(d, '.git'))   # find_git_root only checks .git
        item = Item(id=d, title='repo/', has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), [
            'dir.shell', 'dir.git',
            'dir.newfile', 'dir.newdir', 'dir.path', 'delete',
        ])
        # The single git row carries the 'git ▸' submenu label.
        labels = dict((t, l) for l, t in rows)
        self.assertEqual(labels['dir.git'], 'git ▸')

    def test_dir_show_full_path_always_and_delete_hint(self):
        d = self._subdir('plain')
        item = Item(id=d, title='plain/', has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertIn('dir.path', _tokens(rows))   # always offered
        self.assertIn('Show full path', _labels(rows))
        delete_label = next(l for l, t in rows if t == 'delete')
        self.assertEqual(delete_label, 'Delete (d)')   # literal d hint

    # -- file -------------------------------------------------------------

    def test_plain_file_menu_rows_and_hotkey_hints(self):
        # A plain (non-md) file whose parent is NOT a repo: edit / view / open,
        # then show path / delete, then the shared directory actions on the
        # parent (Run-shell-here + New file/dir — no git rows). No browse-md /
        # browse-plan / mdcat / diff rows; the standalone file.shell row is gone
        # (Run-shell-here now rides in via dir.shell, exactly once).
        p = self._file('app.py', 'print(1)\n')
        item = Item(id=p, title='app.py', has_children=False)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), [
            'file.edit', 'file.view', 'file.open',
            'file.path', 'delete',
            'dir.shell', 'dir.newfile', 'dir.newdir',
        ])
        # The retired standalone file shell token is gone.
        self.assertNotIn('file.shell', _tokens(rows))
        labels = dict((t, l) for l, t in rows)
        # e / o / d hotkey hints are literal text (menus don't parse '&').
        self.assertEqual(labels['file.edit'], 'Edit in $EDITOR (e)')
        self.assertEqual(labels['file.open'], 'Open with default app (o)')
        self.assertEqual(labels['delete'], 'Delete (d)')
        self.assertEqual(labels['dir.shell'], 'Run shell here')
        self.assertIn('Show full path', _labels(rows))   # always offered

    def test_md_file_gets_browse_md_and_mdcat_rows(self):
        # A *.md file gains browse-md + Render-with-mdcat rows; NOT browse-plan
        # (its basename isn't .PLAN.md).
        p = self._file('notes.md', '# Notes\n')
        item = Item(id=p, title='notes.md', has_children=True)
        toks = _tokens(self.r.context_menu_options(self._ctx(item)))
        self.assertIn('file.md', toks)
        self.assertIn('file.mdcat', toks)
        self.assertNotIn('file.plan', toks)

    def test_capital_md_extension_also_gets_md_rows(self):
        p = self._file('READ.MD', '# x\n')
        item = Item(id=p, title='READ.MD', has_children=True)
        toks = _tokens(self.r.context_menu_options(self._ctx(item)))
        self.assertIn('file.md', toks)
        self.assertIn('file.mdcat', toks)

    def test_plan_md_gets_browse_plan_row(self):
        # A file named exactly .PLAN.md gets the browse-plan row. It also ends
        # in .md, so it additionally gets the browse-md / mdcat rows.
        p = self._file('.PLAN.md', '# Plan\n')
        item = Item(id=p, title='.PLAN.md', has_children=True)
        toks = _tokens(self.r.context_menu_options(self._ctx(item)))
        self.assertIn('file.plan', toks)
        self.assertIn('file.md', toks)
        plan_label = next(l for l, t in
                          self.r.context_menu_options(self._ctx(item))
                          if t == 'file.plan')
        self.assertEqual(plan_label, 'Browse in browse-plan')

    def test_non_plan_non_md_file_has_no_launch_rows(self):
        p = self._file('data.txt', 'x\n')
        item = Item(id=p, title='data.txt', has_children=False)
        toks = _tokens(self.r.context_menu_options(self._ctx(item)))
        for t in ('file.md', 'file.mdcat', 'file.plan', 'file.diff'):
            self.assertNotIn(t, toks)

    # -- file cursor folds in its PARENT directory's actions (#1069) -------

    def _file_in(self, dir_path, name, body=''):
        """Create ``name`` under ``dir_path`` (a real subdir) and return it."""
        p = os.path.join(dir_path, name)
        with open(p, 'w') as f:
            f.write(body)
        return p

    def test_file_with_repo_parent_includes_dir_actions(self):
        # Cursor on a FILE whose parent IS a git repo: the menu carries the
        # file actions AND the shared directory actions on the parent — the
        # 'git' submenu row + New file/dir (targeting the parent). The ONLY
        # Delete is the file delete; the parent's Delete / Show-full-path are
        # NOT here.
        repo = self._subdir('repo')
        os.mkdir(os.path.join(repo, '.git'))   # find_git_root only checks .git
        p = self._file_in(repo, 'app.py', 'print(1)\n')
        item = Item(id=p, title='app.py', has_children=False)
        rows = self.r.context_menu_options(self._ctx(item))
        toks = _tokens(rows)
        # File actions present.
        for t in ('file.edit', 'file.view', 'file.open', 'file.path'):
            self.assertIn(t, toks)
        # The parent is a repo → the 'git' submenu row appears.
        self.assertIn('dir.git', toks)
        # New file/dir on the parent.
        self.assertIn('dir.newfile', toks)
        self.assertIn('dir.newdir', toks)
        # The ONLY Delete is the file delete: 'delete' appears once, and there
        # is NO 'dir.path' (the dir's Show-full-path) — only the file's.
        self.assertEqual(toks.count('delete'), 1)
        self.assertNotIn('dir.path', toks)
        self.assertIn('file.path', toks)

    def test_file_with_non_repo_parent_has_no_git_rows(self):
        # Cursor on a FILE whose parent is NOT a repo: no 'git' submenu row, but
        # the New file/dir rows are still present (created in the parent).
        plain = self._subdir('plain')
        p = self._file_in(plain, 'data.txt', 'x\n')
        item = Item(id=p, title='data.txt', has_children=False)
        toks = _tokens(self.r.context_menu_options(self._ctx(item)))
        self.assertNotIn('dir.git', toks)
        self.assertIn('dir.newfile', toks)
        self.assertIn('dir.newdir', toks)

    def test_run_shell_here_appears_exactly_once_on_file_cursor(self):
        # Run-shell-here used to be a standalone file row AND a dir row; it now
        # comes only from _dir_actions, so it appears EXACTLY once on a file
        # cursor (token dir.shell; the retired file.shell is gone). Asserted for
        # both a repo-parent and a non-repo-parent file.
        for sub, mkrepo in (('repo', True), ('plain', False)):
            d = self._subdir(sub)
            if mkrepo:
                os.mkdir(os.path.join(d, '.git'))
            p = self._file_in(d, 'app.py', 'x\n')
            item = Item(id=p, title='app.py', has_children=False)
            rows = self.r.context_menu_options(self._ctx(item))
            toks = _tokens(rows)
            self.assertNotIn('file.shell', toks)
            self.assertEqual(toks.count('dir.shell'), 1)
            shell_labels = [l for l, t in rows if t == 'dir.shell']
            self.assertEqual(shell_labels, ['Run shell here'])
            self.b.stop_workers()

    def test_file_cursor_delete_is_only_the_file(self):
        # On a file cursor, Delete deletes the FILE — the parent dir's Delete is
        # never offered. The single 'delete' row carries the file's (d) hint and
        # routes the shared delete handler (which operates on the cursor row).
        repo = self._subdir('repo')
        os.mkdir(os.path.join(repo, '.git'))
        p = self._file_in(repo, 'app.py', 'x\n')
        item = Item(id=p, title='app.py', has_children=False)
        rows = self.r.context_menu_options(self._ctx(item))
        delete_rows = [(l, t) for l, t in rows if t == 'delete']
        self.assertEqual(len(delete_rows), 1)
        self.assertEqual(delete_rows[0][0], 'Delete (d)')

    # -- synthetic rows ---------------------------------------------------

    def test_missing_row_offers_only_show_full_path(self):
        # A ('missing', label, path) row: only Show full path (no file/dir
        # actions on a non-existent path).
        item = Item(id=('missing', 'ghost.txt', '/abs/ghost.txt'),
                    title='ghost.txt', tag='missing', tag_style='dim')
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), ['missing.path'])
        self.assertEqual(_labels(rows), ['Show full path'])

    def test_err_row_offers_nothing(self):
        # A ('err', path) row: no menu at all (a non-scannable path).
        item = Item(id=('err', '/abs/nope'), title='[error] boom',
                    tag='err', tag_style='red')
        self.assertEqual(self.r.context_menu_options(self._ctx(item)), [])

    def test_launch_row_offers_nothing(self):
        # A ('launch', parent, 'md-file', target) launcher row: no fs menu.
        item = Item(id=('launch', '/a.md', 'md-file', '/a.md'),
                    title='a.md', tag='md ↗')
        self.assertEqual(self.r.context_menu_options(self._ctx(item)), [])

    def test_no_cursor_yields_empty(self):
        empty = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            empty.refresh()
            empty.run_until_idle()
            ctx = Context(empty)
            self.assertIsNone(ctx.cursor)
            self.assertEqual(self.r.context_menu_options(ctx), [])
        finally:
            empty.stop_workers()

    # -- no clipboard -----------------------------------------------------

    def test_no_clipboard_or_copy_entries(self):
        # Convention (#1028): recipe menus carry no clipboard / Copy rows.
        d = self._subdir('plain')
        p = self._file('app.py')
        for item in (
            Item(id=d, title='plain/', has_children=True),
            Item(id=p, title='app.py'),
            Item(id=('missing', 'g', '/g'), title='g', tag='missing'),
        ):
            for label in _labels(self.r.context_menu_options(self._ctx(item))):
                self.assertNotIn('copy', label.lower())
            self.b.stop_workers()


class TestSelectionGatedDiff(unittest.TestCase):
    """The "Diff against selected" entry is gated on exactly one OTHER item.

    Built against a real Browser + selection so the ``ctx.selected`` read path
    is exercised. All paths live under a per-test temp dir.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.d = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self.d, ignore_errors=True))

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def _file(self, name):
        p = os.path.join(self.d, name)
        with open(p, 'w') as f:
            f.write(name)
        return p

    def test_no_diff_row_without_selection(self):
        a = self._file('a.txt')
        item = Item(id=a, title='a.txt')
        self.b = _browser_with_item(item)
        toks = _tokens(self.r.context_menu_options(Context(self.b)))
        self.assertNotIn('file.diff', toks)

    def test_diff_row_with_exactly_one_other_selected(self):
        a, b = self._file('a.txt'), self._file('b.txt')
        cursor = Item(id=a, title='a.txt')
        other = Item(id=b, title='b.txt')
        self.b = _browser_with_item(cursor, extra=(other,))
        self.b.select([b])
        self.b.run_until_idle()
        ctx = Context(self.b)
        # The selection is the single OTHER path → the row appears.
        self.assertEqual(self.r._selected_other_path(ctx, a), b)
        toks = _tokens(self.r.context_menu_options(ctx))
        self.assertIn('file.diff', toks)

    def test_no_diff_row_when_two_others_selected(self):
        # Two OTHER items selected is ambiguous → gate returns None → no row.
        # (But >1 selected real target flips the cursor menu to the multi
        # menu; here we select two OTHERS plus the cursor isn't selected, so
        # _str_targets sees the two selected rows and the multi menu wins —
        # which itself has no diff row. Either way: no file.diff.)
        a, b, c = self._file('a.txt'), self._file('b.txt'), self._file('c.txt')
        cursor = Item(id=a, title='a.txt')
        self.b = _browser_with_item(
            cursor, extra=(Item(id=b, title='b.txt'), Item(id=c, title='c.txt')))
        self.b.select([b, c])
        self.b.run_until_idle()
        ctx = Context(self.b)
        self.assertIsNone(self.r._selected_other_path(ctx, a))
        self.assertNotIn('file.diff', _tokens(self.r.context_menu_options(ctx)))


class TestMultiSelectMenu(unittest.TestCase):
    """A multi-row selection yields the count-labelled Delete / Open rows."""

    def setUp(self):
        self.r = _load_recipe()
        self.d = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self.d, ignore_errors=True))

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def _file(self, name):
        p = os.path.join(self.d, name)
        with open(p, 'w') as f:
            f.write(name)
        return p

    def test_multi_select_delete_and_edit_rows(self):
        a, b = self._file('a.txt'), self._file('b.txt')
        cursor = Item(id=a, title='a.txt')
        self.b = _browser_with_item(cursor, extra=(Item(id=b, title='b.txt'),))
        self.b.select([a, b])
        self.b.run_until_idle()
        ctx = Context(self.b)
        self.assertEqual(len(ctx.selected), 2)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows), ['delete', 'multi.edit'])
        # Count appears in the labels; the d hint is on the Delete row.
        labels = dict((t, l) for l, t in rows)
        self.assertEqual(labels['delete'], 'Delete 2 items (d)')
        self.assertEqual(labels['multi.edit'], 'Open 2 in $EDITOR')

    def test_multi_select_count_excludes_synthetic_rows(self):
        # A selection mixing a real file with a missing/err row counts only
        # the real (str-path) targets — but a single real target falls back to
        # the file menu, so we use two real + one synthetic to stay in multi.
        a, b = self._file('a.txt'), self._file('b.txt')
        cursor = Item(id=a, title='a.txt')
        miss = Item(id=('missing', 'g', '/g'), title='g', tag='missing')
        self.b = _browser_with_item(
            cursor, extra=(Item(id=b, title='b.txt'), miss))
        self.b.select([a, b, ('missing', 'g', '/g')])
        self.b.run_until_idle()
        rows = self.r.context_menu_options(Context(self.b))
        labels = dict((t, l) for l, t in rows)
        # Three selected, but only two are real paths.
        self.assertEqual(labels['delete'], 'Delete 2 items (d)')


class TestDispatchTable(unittest.TestCase):
    """Every token a builder can emit dispatches; no orphan handlers."""

    def setUp(self):
        self.r = _load_recipe()

    class _Cur:
        def __init__(self, id, title='x'):
            self.id, self.title = id, title

    class _Ctx:
        """A tiny ctx exposing cursor / selected for the PURE builders only."""

        def __init__(self, cur, selected=()):
            self._cur = cur
            self._sel = list(selected)

        @property
        def cursor(self):
            return self._cur

        @property
        def selected(self):
            return self._sel

        @property
        def targets(self):
            return self._sel or ([self._cur] if self._cur else [])

    def _all_emitted_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, 'repo')
            os.mkdir(repo)
            os.mkdir(os.path.join(repo, '.git'))   # makes _is_git_repo True
            plan = os.path.join(d, '.PLAN.md')
            with open(plan, 'w') as f:
                f.write('# p\n')
            md = os.path.join(d, 'doc.md')
            with open(md, 'w') as f:
                f.write('# d\n')
            other = os.path.join(d, 'other.md')
            with open(other, 'w') as f:
                f.write('# o\n')
            txt = os.path.join(d, 'a.txt')
            with open(txt, 'w') as f:
                f.write('x\n')

            emitted = set()
            # Directory (git repo → emits the 'git' submenu row too).
            emitted |= set(_tokens(self.r.context_menu_options(
                self._Ctx(self._Cur(repo)))))
            # File that is BOTH .PLAN.md (→ file.plan) AND *.md (→ file.md /
            # file.mdcat). No selection → the plain file menu.
            cur = self._Cur(plan)
            other_item = self._Cur(other)
            emitted |= set(_tokens(self.r.context_menu_options(
                self._Ctx(cur))))
            # Selection of exactly one OTHER → file.diff (single real target →
            # still the file menu, not multi).
            emitted |= set(_tokens(self.r.context_menu_options(
                self._Ctx(cur, selected=[other_item]))))
            # Plain .md file (browse-md / mdcat without plan).
            emitted |= set(_tokens(self.r.context_menu_options(
                self._Ctx(self._Cur(md)))))
            # Missing special.
            emitted |= set(_tokens(self.r.context_menu_options(
                self._Ctx(self._Cur(('missing', 'g', '/g'))))))
            # Multi-select (two real targets → delete / multi.edit).
            emitted |= set(_tokens(self.r.context_menu_options(
                self._Ctx(self._Cur(txt),
                          selected=[self._Cur(txt), self._Cur(md)]))))
            return emitted

    def test_every_emitted_token_has_a_handler(self):
        for tok in self._all_emitted_tokens():
            self.assertIn(tok, self.r._MENU_ACTIONS,
                          f'token {tok!r} has no dispatch handler')

    def test_no_orphan_handlers(self):
        emitted = self._all_emitted_tokens()
        orphans = set(self.r._MENU_ACTIONS) - emitted
        self.assertEqual(orphans, set(),
                         f'handlers never emitted by a builder: {orphans}')


class TestGitRepoGate(unittest.TestCase):
    """``_is_git_repo`` reflects a real ``.git`` marker (or md_doc absence)."""

    def setUp(self):
        self.r = _load_recipe()

    def test_repo_and_non_repo(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, 'repo')
            os.mkdir(repo)
            os.mkdir(os.path.join(repo, '.git'))
            plain = os.path.join(d, 'plain')
            os.mkdir(plain)
            self.assertTrue(self.r._is_git_repo(repo))
            self.assertFalse(self.r._is_git_repo(plain))

    def test_false_without_md_doc(self):
        # The git rows are gated on md_doc; absent it, never a repo.
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, 'repo')
            os.mkdir(repo)
            os.mkdir(os.path.join(repo, '.git'))
            self.r._md_doc = None
            self.assertFalse(self.r._is_git_repo(repo))


class TestGitSubmenu(unittest.TestCase):
    """The single ``git`` row opens a mode submenu that routes to browse-git.

    ``_open_git_menu`` re-invokes ``ctx.menu`` over the five modes (the
    second-level menu); the chosen mode launches ``browse-git <dir> --mode
    NAME``. In headless mode the real ``ctx.menu`` returns None, so — as
    browse-git's Reset ▸ test does — we drive the handler with a recording ctx
    that scripts the submenu choice and captures the ``run_external`` argv.
    """

    def setUp(self):
        self.r = _load_recipe()

    class _Ctx:
        def __init__(self, choice):
            self._choice = choice
            self.menu_items = None
            self.cmd = None
            self.keep_screen = None

        def menu(self, items, **kw):
            self.menu_items = list(items)
            return self._choice

        def run_external(self, cmd, env=None, *, keep_screen=False):
            self.cmd = cmd
            self.keep_screen = keep_screen

    def test_submenu_lists_the_five_modes(self):
        # Cancel (None) so nothing launches; assert the rows the submenu shows.
        ctx = self._Ctx(None)
        self.r._open_git_menu(ctx, '/some/repo')
        self.assertEqual([tok for _label, tok in ctx.menu_items],
                         ['commits', 'branches', 'status', 'stash', 'reflog'])
        # Level-2 labels drop the 'git ' prefix.
        labels = [label for label, _tok in ctx.menu_items]
        self.assertEqual(labels,
                         ['commits', 'branches', 'status', 'stashes', 'reflog'])
        # A cancel is a no-op — nothing launched.
        self.assertIsNone(ctx.cmd)

    def test_chosen_mode_launches_browse_git(self):
        # Picking 'stashes' (token 'stash') runs browse-git <dir> --mode stash.
        ctx = self._Ctx('stash')
        self.r._open_git_menu(ctx, '/some/repo')
        self.assertEqual(ctx.cmd,
                         ['browse-git', '/some/repo', '--mode', 'stash'])
        self.assertTrue(ctx.keep_screen)

    def test_dir_git_dispatch_threads_target_dir_for_file_cursor(self):
        # The dir.git handler routes through _target_dir, so on a FILE path the
        # submenu launch targets the file's PARENT directory.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'app.py')
            with open(p, 'w') as f:
                f.write('x\n')
            ctx = self._Ctx('commits')
            self.r._MENU_ACTIONS['dir.git'](ctx, p)
            self.assertEqual(ctx.cmd,
                             ['browse-git', d, '--mode', 'commits'])


class TestDiffHandler(unittest.TestCase):
    """``_diff_selected`` pages a real diff; identical files flash instead.

    Read-only — ``diff`` only reads the two files; nothing is mutated. Both
    files live under a temp dir.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.d = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self.d, ignore_errors=True))

    class _PageCtx:
        def __init__(self, selected, cursor):
            self._sel = selected
            self._cur = cursor
            self.paged = None
            self.flashed = None
            self.errored = None

        @property
        def selected(self):
            return self._sel

        @property
        def cursor(self):
            return self._cur

        def page(self, text, lang=''):
            self.paged = (text, lang)

        def flash(self, msg, log=False):
            self.flashed = msg

        def error(self, msg):
            self.errored = msg

    def _file(self, name, body):
        p = os.path.join(self.d, name)
        with open(p, 'w') as f:
            f.write(body)
        return p

    class _It:
        def __init__(self, id):
            self.id = id

    def test_diff_pages_when_files_differ(self):
        a = self._file('a.txt', 'one\ntwo\n')
        b = self._file('b.txt', 'one\nTWO\n')
        ctx = self._PageCtx([self._It(b)], self._It(a))
        self.r._diff_selected(ctx, a)
        self.assertIsNotNone(ctx.paged)
        text, lang = ctx.paged
        self.assertEqual(lang, 'diff')
        self.assertTrue(text.strip())          # a real diff
        self.assertIsNone(ctx.flashed)

    def test_identical_files_flash(self):
        a = self._file('a.txt', 'same\n')
        b = self._file('b.txt', 'same\n')
        ctx = self._PageCtx([self._It(b)], self._It(a))
        self.r._diff_selected(ctx, a)
        self.assertIsNone(ctx.paged)           # nothing to page
        self.assertIsNotNone(ctx.flashed)
        self.assertIn('no differences', ctx.flashed)

    def test_no_other_selected_flashes(self):
        a = self._file('a.txt', 'x\n')
        ctx = self._PageCtx([], self._It(a))   # nothing else selected
        self.r._diff_selected(ctx, a)
        self.assertIsNone(ctx.paged)
        self.assertIn('select exactly one other', ctx.flashed)


class TestShellHandler(unittest.TestCase):
    """``_run_shell`` builds a ``cd <dir> && exec <shell>`` string (no cwd kwarg)."""

    def setUp(self):
        self.r = _load_recipe()

    class _Ctx:
        def __init__(self):
            self.cmd = None

        def run_external(self, cmd, env=None, *, keep_screen=False):
            self.cmd = cmd

    def test_run_shell_changes_into_dir(self):
        ctx = self._Ctx()
        with unittest.mock.patch.dict(os.environ, {'SHELL': '/bin/zsh'}):
            self.r._run_shell(ctx, '/some/dir')
        # A shell STRING (run_external runs it via sh -c) that cds first.
        self.assertIsInstance(ctx.cmd, str)
        self.assertIn('cd ', ctx.cmd)
        self.assertIn('/some/dir', ctx.cmd)
        self.assertIn('/bin/zsh', ctx.cmd)

    def test_run_shell_quotes_spaced_dir(self):
        ctx = self._Ctx()
        with unittest.mock.patch.dict(os.environ, {'SHELL': '/bin/bash'}):
            self.r._run_shell(ctx, '/a dir/with spaces')
        # The dir is shell-quoted so spaces don't split the cd argument.
        self.assertIn("'/a dir/with spaces'", ctx.cmd)

    def test_run_shell_defaults_to_sh(self):
        ctx = self._Ctx()
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.r._run_shell(ctx, '/d')
        self.assertIn('exec sh', ctx.cmd)


class TestDirActionsTargetCursorDir(unittest.TestCase):
    """The shared dir.* handlers act on the cursor's directory.

    ``_target_dir(path)`` collapses the two cursor kinds: a directory cursor
    passes its own path (the dir verbatim), a file cursor passes the file's
    PARENT. The same dir.* token therefore threads the right directory to its
    handler either way — so Run-shell-here / New file/dir (offered on both a
    directory cursor and a file cursor via ``_dir_actions``) operate on the
    directory being listed. Paths live under a real temp dir so ``os.path.isdir``
    can distinguish the two.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.d = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self.d, ignore_errors=True))

    class _ShellCtx:
        def __init__(self):
            self.cmd = None

        def run_external(self, cmd, env=None, *, keep_screen=False):
            self.cmd = cmd

    def test_target_dir_of_real_file_is_parent(self):
        p = os.path.join(self.d, 'app.py')
        with open(p, 'w') as f:
            f.write('x\n')
        self.assertEqual(self.r._target_dir(p), self.d)

    def test_target_dir_of_real_dir_is_itself(self):
        sub = os.path.join(self.d, 'sub')
        os.mkdir(sub)
        self.assertEqual(self.r._target_dir(sub), sub)

    def test_dir_shell_on_file_cursor_targets_parent(self):
        # dir.shell dispatched with a FILE path cds into the file's parent dir
        # (the directory being listed), not the file itself.
        p = os.path.join(self.d, 'app.py')
        with open(p, 'w') as f:
            f.write('x\n')
        ctx = self._ShellCtx()
        self.r._MENU_ACTIONS['dir.shell'](ctx, p)
        self.assertIsInstance(ctx.cmd, str)
        self.assertIn(self.d, ctx.cmd)
        # The shell cds to the parent dir, not into a path ending in the file.
        self.assertNotIn('app.py', ctx.cmd)

    def test_dir_shell_on_dir_cursor_targets_that_dir(self):
        # dir.shell dispatched with a DIRECTORY path cds into that dir verbatim.
        sub = os.path.join(self.d, 'sub')
        os.mkdir(sub)
        ctx = self._ShellCtx()
        self.r._MENU_ACTIONS['dir.shell'](ctx, sub)
        self.assertIn(sub, ctx.cmd)

    def test_dir_newfile_on_file_cursor_creates_in_parent(self):
        # New file… on a FILE cursor creates IN the parent dir (the listed dir),
        # via the dir.newfile handler routed through _target_dir.
        existing = os.path.join(self.d, 'app.py')
        with open(existing, 'w') as f:
            f.write('x\n')

        created = []

        class Ctx:
            def input(self_inner, prompt):
                return 'fresh.txt'

            def error(self_inner, msg):
                created.append(('error', msg))

            def refresh(self_inner, *a, **kw):
                created.append('refresh')

        self.r._MENU_ACTIONS['dir.newfile'](Ctx(), existing)
        # Created under the parent dir (the file's containing dir), not nested
        # under the file's own path.
        self.assertTrue(os.path.exists(os.path.join(self.d, 'fresh.txt')))
        self.assertIn('refresh', created)


if __name__ == '__main__':
    unittest.main()
