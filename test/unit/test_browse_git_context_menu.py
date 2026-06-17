"""Unit tests for the ``recipes/browse-git`` context menu (ticket #1030).

browse-git is the richest recipe — every row id is a tagged tuple whose
``id[0]`` names the KIND (``commit`` / ``file`` / ``ref`` / ``status`` /
``stash`` / ``reflog``), and the context menu branches on it. Following the
pilot convention (browse-procs, ticket #1033) the option list is a PURE
builder, ``context_menu_options(ctx)``, that inspects ``ctx.cursor`` and
returns ``(label, token)`` rows WITHOUT opening a modal; a flat
``{token: handler}`` table (``_MENU_ACTIONS``) dispatches the chosen token.

We exercise the builder against a REAL headless ``Browser`` / ``Context``
(from ``test.async_._helpers``) with a known git item under the cursor — not a
fake ctx. browse-tui swallows ``on_context_menu`` exceptions and a fake ctx
hides bugs, so the real ``Context.cursor`` / ``Context.selected`` read paths
are what we assert against; ``ctx.menu`` itself short-circuits to ``None`` in
headless mode, which is exactly why the builder is split out and tested
directly. The two conditional gates that touch git (``_one_other_commit`` →
the selection-gated "Diff against selected", ``_has_plan_md`` → the
``.PLAN.md`` entry) are exercised against a REAL throwaway temp repo so they
can't drift from git's behaviour.

The recipe is a ``--run-py`` script that imports ``browse_tui`` (only a real
module when the binary loads it), so we stub ``browse_tui`` in ``sys.modules``
and load the extension-less recipe via ``SourceFileLoader`` — the same pattern
as ``test/unit/test_browse_git.py`` / ``test_browse_procs.py``.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from test.async_._helpers import Context, Item, make_browser


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-git'


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
    mod.cell_width = len
    mod.style = lambda name: (None, False)
    mod.default_row_content = lambda item, ctx: []
    mod.recipe_argv = lambda: []
    mod.sanitize_ansi = lambda s: s
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-git recipe; returns a fresh module."""
    _stub_browse_tui()
    name = '_browse_git_cm_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _browser_with_item(item, *, extra=()):
    """A real headless Browser whose cursor sits on ``item``.

    ``extra`` are sibling rows listed alongside ``item`` (so a multi-row
    selection can be built). The cursor is parked on ``item`` via ``cursor_to``
    after the root children settle, exactly mirroring the procs test.
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
    """``context_menu_options`` returns the right rows for each git kind.

    Each case parks a real cursor on one kind of git row and asserts the
    kind-specific labels/tokens plus the shared trailing "Switch mode" row.
    ``_STDIN_KIND`` is forced to ``None`` (repo mode) so the Switch-mode row is
    present (it is suppressed in stdin mode, where `` ` `` itself flashes).
    """

    def setUp(self):
        self.r = _load_recipe()
        self.r._STDIN_KIND = None

    def _ctx(self, item, extra=()):
        self.b = _browser_with_item(item, extra=extra)
        return Context(self.b)

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def test_commit_menu_rows(self):
        item = Item(id=('commit', 'abc1234def'), title='subject',
                    has_children=True)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        # Kind-specific rows then the shared Switch-mode row, in order. The
        # two conditional commit rows (Diff against selected / browse-plan)
        # are absent here — no other commit selected, no real repo/.PLAN.md.
        self.assertEqual(_tokens(rows), [
            'commit.sha', 'commit.checkout', 'commit.branch', 'commit.tag',
            'commit.cherry', 'commit.revert', 'commit.reset', 'mode.switch',
        ])
        self.assertIn('Show full SHA', _labels(rows))
        self.assertIn('Reset ▸', _labels(rows))

    def test_file_menu_rows_and_E_hint(self):
        # A file row built the NORMAL way — title is the full path, no
        # synthetic ellipsis. "Show full path" is always present (the recipe
        # can't reliably detect render-time truncation, so it always offers
        # the pop-up). The Edit row duplicates the ``E`` action → ``(E)`` hint.
        item = Item(id=('file', 'abc1234def', 'src/app.py'), title='src/app.py',
                    tag='M', has_children=False)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows), [
            'file.view', 'file.edit', 'file.diff', 'file.history',
            'file.blame', 'file.path', 'mode.switch',
        ])
        edit_label = next(l for l, t in rows if t == 'file.edit')
        self.assertEqual(edit_label, 'Edit working-tree file (E)')

    def test_ref_menu_rows(self):
        item = Item(id=('ref', 'feature/x'), title='feature/x', tag='branch',
                    has_children=True)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows), [
            'ref.checkout', 'ref.merge', 'ref.rebase', 'ref.branch',
            'ref.delete', 'mode.switch',
        ])

    def test_status_menu_rows_and_E_hint(self):
        item = Item(id=('status', ' M', 'src/app.py'), title='src/app.py',
                    tag='M', has_children=False)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows), [
            'status.stage', 'status.unstage', 'status.discard',
            'status.edit', 'status.diff', 'status.opendir', 'mode.switch',
        ])
        edit_label = next(l for l, t in rows if t == 'status.edit')
        self.assertEqual(edit_label, 'Edit in $EDITOR (E)')

    def test_stash_node_menu_rows(self):
        item = Item(id=('stash', 0), title='WIP', tag='stash@{0}',
                    has_children=True)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows), [
            'stash.apply', 'stash.pop', 'stash.drop', 'stash.show',
            'mode.switch',
        ])

    def test_stash_file_uses_file_menu(self):
        # A file INSIDE a stash (('stash', n, path)) gets the file menu.
        item = Item(id=('stash', 2, 'doc.md'), title='doc.md', tag='M',
                    has_children=False)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows), [
            'file.view', 'file.edit', 'file.diff', 'file.history',
            'file.blame', 'file.path', 'mode.switch',
        ])

    def test_reflog_menu_rows(self):
        item = Item(id=('reflog', 0, 'abc1234def'), title='commit: x',
                    tag='abc1234', has_children=True)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows), [
            'reflog.checkout', 'reflog.reset', 'reflog.show', 'reflog.sha',
            'mode.switch',
        ])

    def test_switch_mode_row_shows_backtick_hint(self):
        # The shared Switch-mode row reuses the ` action, so its label carries
        # the backtick hotkey hint (literal text, the convention for a menu
        # entry that duplicates a keybinding).
        item = Item(id=('commit', 'abc'), title='x', has_children=True)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        mode_label = next(l for l, t in rows if t == 'mode.switch')
        self.assertIn('`', mode_label)

    def test_switch_mode_suppressed_in_stdin_mode(self):
        # In stdin mode ` itself flashes, so the menu drops the Switch-mode
        # row; a commit row then carries only its own (non-mode) entries.
        self.r._STDIN_KIND = 'log'
        item = Item(id=('commit', 'abc'), title='x', has_children=True)
        ctx = self._ctx(item)
        rows = self.r.context_menu_options(ctx)
        self.assertNotIn('mode.switch', _tokens(rows))
        self.assertEqual(_tokens(rows)[0], 'commit.sha')

    def test_no_cursor_yields_only_switch_mode(self):
        # An empty tree → no cursor item → no per-kind rows; the shared
        # Switch-mode row is still offered (repo mode).
        empty = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            empty.refresh()
            empty.run_until_idle()
            ctx = Context(empty)
            self.assertIsNone(ctx.cursor)
            self.assertEqual(_tokens(self.r.context_menu_options(ctx)),
                             ['mode.switch'])
        finally:
            empty.stop_workers()

    def test_unmenued_kind_yields_only_switch_mode(self):
        # A worktree-group ('wc', bucket) / sentinel / filler row has no
        # per-kind menu — only the shared Switch-mode row.
        item = Item(id=('wc', 'staged'), title='Staged changes',
                    has_children=True)
        ctx = self._ctx(item)
        self.assertEqual(_tokens(self.r.context_menu_options(ctx)),
                         ['mode.switch'])

    def test_no_clipboard_or_copy_entries(self):
        # Convention (#1028): recipe menus carry no clipboard / Copy rows.
        for item in (
            Item(id=('commit', 'abc'), title='x', has_children=True),
            Item(id=('file', 'abc', 'p.py'), title='p.py'),
            Item(id=('ref', 'main'), title='main', has_children=True),
            Item(id=('status', ' M', 'p.py'), title='p.py'),
            Item(id=('stash', 0), title='WIP', has_children=True),
            Item(id=('reflog', 0, 'abc'), title='x', has_children=True),
        ):
            ctx = self._ctx(item)
            for label in _labels(self.r.context_menu_options(ctx)):
                self.assertNotIn('copy', label.lower())
            self.b.stop_workers()


class TestResetSubmenu(unittest.TestCase):
    """The Reset ▸ submenu lists soft / mixed / hard, hard last (strong)."""

    def setUp(self):
        self.r = _load_recipe()

    def test_reset_submenu_rows(self):
        # ``_cm_commit_reset`` opens ``ctx.menu`` over these three; in headless
        # mode that returns None (no-op), so we assert the static row list it
        # would present by reading the literal options off the call. The rows
        # are inlined in the handler, so we drive it with a recording ctx.
        captured = {}

        class RecCtx:
            def menu(self, items, **kw):
                captured['items'] = list(items)
                return None  # cancel — no git runs

        self.r._cm_commit_reset(RecCtx(), 'abc1234')
        self.assertEqual([tok for _label, tok in captured['items']],
                         ['soft', 'mixed', 'hard'])
        # hard is listed last and flagged DISCARD in its label.
        hard_label = dict((t, l) for l, t in captured['items'])['hard']
        self.assertIn('DISCARD', hard_label)


class TestDispatchTable(unittest.TestCase):
    """Every token a builder can emit dispatches; destructive ops confirm."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._STDIN_KIND = None
        # Force both git-touching gates on so the builders emit their FULL row
        # sets (the conditional commit rows included) without a real repo.
        self.r._has_plan_md = lambda sha: True
        self.r._one_other_commit = lambda ctx, sha: 'deadbeef'

    def _all_emitted_tokens(self):
        class Cur:
            def __init__(self, id):
                self.id, self.title = id, 'p.py'

        class Ctx:
            def __init__(self, id):
                self._c = Cur(id)

            @property
            def cursor(self):
                return self._c

            @property
            def selected(self):
                return []

        emitted = set()
        cases = [
            ('commit', 'abc1234def'),
            ('file', 'abc1234def', 'p.py'),
            ('ref', 'main'),
            ('status', ' M', 'p.py'),
            ('stash', 2),
            ('stash', 2, 'p.md'),
            ('reflog', 0, 'abc1234def'),
        ]
        for cid in cases:
            for _label, tok in self.r.context_menu_options(Ctx(cid)):
                emitted.add(tok)
        return emitted

    def test_every_emitted_token_has_a_handler(self):
        emitted = self._all_emitted_tokens()
        for tok in emitted:
            self.assertIn(tok, self.r._MENU_ACTIONS,
                          f'token {tok!r} has no dispatch handler')

    def test_no_orphan_handlers(self):
        # Every handler in the table is reachable from some builder row.
        emitted = self._all_emitted_tokens()
        orphans = set(self.r._MENU_ACTIONS) - emitted
        self.assertEqual(orphans, set(),
                         f'handlers never emitted by a builder: {orphans}')

    def test_file_rev_maps_stash_selector(self):
        # A committed file's rev is its sha; a stashed file's rev is the
        # stash@{n} selector (so view/diff/blame target the right object).
        self.assertEqual(self.r._file_rev(('file', 'abc123', 'p.py')), 'abc123')
        self.assertEqual(self.r._file_rev(('stash', 3, 'p.py')), 'stash@{3}')


class TestConfirmGating(unittest.TestCase):
    """Destructive actions gate on a value-mapped Yes/No confirm.

    The single mutation path ``_git_action`` runs the command only when the
    confirm returns truthy; a cancel / No leaves git untouched. The hard cases
    (``strong=True``) put ``No`` first (the default button). We drive the
    handlers with a recording ctx so no git runs.
    """

    def setUp(self):
        self.r = _load_recipe()

    class _RecCtx:
        def __init__(self, answer):
            self.answer = answer
            self.confirmed = None
            self.ran = False
            self.buttons = None

        def confirm(self, message, buttons, **kw):
            self.confirmed = message
            self.buttons = list(buttons)
            return self.answer

        def error(self, *a, **k):
            pass

        def flash(self, *a, **k):
            pass

        def refresh(self, *a, **k):
            pass

    def test_git_action_skips_command_on_cancel(self):
        # answer None (cancel) → returns False, never reaches _run_git.
        ctx = self._RecCtx(None)
        ran = []
        self.r._run_git = lambda *a: ran.append(a)
        out = self.r._git_action(ctx, ['checkout', 'x'], ok_msg='ok',
                                 confirm_msg='go?')
        self.assertFalse(out)
        self.assertEqual(ran, [])
        self.assertEqual(ctx.confirmed, 'go?')

    def test_git_action_runs_command_on_yes(self):
        ctx = self._RecCtx(True)
        ran = []

        class _CP:
            returncode = 0
            stdout = ''
            stderr = ''

        self.r._run_git = lambda *a: ran.append(a) or _CP()
        out = self.r._git_action(ctx, ['checkout', 'x'], ok_msg='ok',
                                 confirm_msg='go?')
        self.assertTrue(out)
        self.assertEqual(ran, [('checkout', 'x')])

    def test_strong_confirm_defaults_to_no(self):
        # A strong confirm lists No FIRST (the default button) so an accidental
        # Enter doesn't fire the irreversible action. reset --hard via the
        # reflog handler is a strong case.
        ctx = self._RecCtx(False)
        self.r._run_git = lambda *a: None
        self.r._cm_reflog_reset(ctx, 'abc1234def')
        self.assertEqual([b for _l, b in ctx.buttons], [False, True])
        self.assertIn('DISCARD', ctx.confirmed)

    def test_discard_untracked_is_strong(self):
        # Discarding an untracked entry removes the file outright (clean -f)
        # behind a strong confirm.
        ctx = self._RecCtx(False)
        self.r._run_git = lambda *a: None
        self.r._cm_status_discard(ctx, '??', 'new.txt')
        self.assertEqual([b for _l, b in ctx.buttons], [False, True])

    def test_nondestructive_branch_create_has_no_confirm(self):
        # Creating a branch/tag is non-destructive — no confirm, runs after a
        # valid name. We stub input to return a name and assert _run_git fired
        # without a confirm dialog.
        ran = []

        class _CP:
            returncode = 0
            stdout = ''
            stderr = ''

        class Ctx(self._RecCtx):
            def input(self, prompt, default=''):
                return 'newbranch'

        ctx = Ctx(None)  # confirm would return None, but none should be asked
        confirms = []
        ctx.confirm = lambda *a, **k: confirms.append(a) or None
        self.r._run_git = lambda *a: ran.append(a) or _CP()
        self.r._cm_commit_branch(ctx, 'abc1234def')
        self.assertEqual(confirms, [])
        self.assertEqual(ran, [('branch', 'newbranch', 'abc1234def')])


class TestConditionalEntriesRealRepo(unittest.TestCase):
    """The selection-gated and ``.PLAN.md``-present rows against REAL git.

    A throwaway temp repo with two commits — one carrying ``.PLAN.md``, one
    not — exercises ``_has_plan_md`` (the browse-plan row) and
    ``_one_other_commit`` (the "Diff against selected" row) through the actual
    git CLI, so neither gate can drift from git's behaviour.
    """

    @classmethod
    def setUpClass(cls):
        cls._orig_cwd = os.getcwd()
        cls.repo = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.repo, ignore_errors=True)
        cls.addClassCleanup(lambda: os.chdir(cls._orig_cwd))
        env = {**os.environ, 'LC_ALL': 'C',
               'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}

        def git(*args):
            return subprocess.run(['git', '-C', cls.repo, *args], check=True,
                                  capture_output=True, text=True, env=env).stdout

        with open(os.path.join(cls.repo, 'a.txt'), 'w') as f:
            f.write('a\n')
        git('init', '-q', '-b', 'main')
        git('add', '.')
        git('commit', '-q', '-m', 'no plan commit')
        cls.sha_no_plan = git('rev-parse', 'HEAD').strip()
        with open(os.path.join(cls.repo, '.PLAN.md'), 'w') as f:
            f.write('# Plan\n\n- step one\n')
        git('add', '.PLAN.md')
        git('commit', '-q', '-m', 'add plan')
        cls.sha_plan = git('rev-parse', 'HEAD').strip()

    def setUp(self):
        self.r = _load_recipe()
        self.r._STDIN_KIND = None
        os.chdir(self.repo)

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def _commit_ctx(self, sha, extra=()):
        item = Item(id=('commit', sha), title='subj', has_children=True)
        self.b = _browser_with_item(item, extra=extra)
        return Context(self.b)

    def test_has_plan_md_gate(self):
        # The commit carrying .PLAN.md probes True; the one without, False.
        self.assertTrue(self.r._has_plan_md(self.sha_plan))
        self.assertFalse(self.r._has_plan_md(self.sha_no_plan))

    def test_browse_plan_row_only_when_plan_present(self):
        ctx = self._commit_ctx(self.sha_plan)
        self.assertIn('commit.plan',
                      _tokens(self.r.context_menu_options(ctx)))
        self.b.stop_workers()
        ctx2 = self._commit_ctx(self.sha_no_plan)
        self.assertNotIn('commit.plan',
                         _tokens(self.r.context_menu_options(ctx2)))

    def test_diff_against_selected_gated_on_one_other_commit(self):
        # No selection → no Diff-against-selected row.
        ctx = self._commit_ctx(self.sha_plan)
        self.assertNotIn('commit.diffsel',
                         _tokens(self.r.context_menu_options(ctx)))
        self.b.stop_workers()

        # Exactly one OTHER commit selected → the row appears.
        cursor_item = Item(id=('commit', self.sha_plan), title='subj',
                           has_children=True)
        other = Item(id=('commit', self.sha_no_plan), title='other',
                     has_children=True)
        self.b = _browser_with_item(cursor_item, extra=(other,))
        self.b.select([('commit', self.sha_no_plan)])
        self.b.run_until_idle()
        ctx2 = Context(self.b)
        self.assertEqual([it.id for it in ctx2.selected],
                         [('commit', self.sha_no_plan)])
        self.assertIn('commit.diffsel',
                      _tokens(self.r.context_menu_options(ctx2)))

    def test_one_other_commit_none_when_two_others_selected(self):
        # Two OTHER commits selected is ambiguous → gate returns None → no row.
        cursor_item = Item(id=('commit', self.sha_plan), title='subj',
                           has_children=True)
        o1 = Item(id=('commit', self.sha_no_plan), title='o1',
                  has_children=True)
        o2 = Item(id=('commit', 'feedface' * 5), title='o2',
                  has_children=True)
        self.b = _browser_with_item(cursor_item, extra=(o1, o2))
        self.b.select([('commit', self.sha_no_plan), ('commit', 'feedface' * 5)])
        self.b.run_until_idle()
        ctx = Context(self.b)
        self.assertIsNone(self.r._one_other_commit(ctx, self.sha_plan))
        self.assertNotIn('commit.diffsel',
                         _tokens(self.r.context_menu_options(ctx)))


if __name__ == '__main__':
    unittest.main()
