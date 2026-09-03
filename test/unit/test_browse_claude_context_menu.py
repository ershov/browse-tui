"""Unit tests for the ``recipes/browse-claude`` context menu (ticket #1029).

browse-claude is the most complex recipe — an actions-first DIRECTORY
hierarchy (over the cursor's project / cwd / worktree dirs) plus per-kind
message / session / agent menus. Following the committed convention
(browse-ps the pilot, browse-git the rich multi-kind case) the option list
is a PURE builder, ``context_menu_options(ctx)``, that inspects ``ctx.cursor``
and returns ``(label, token)`` rows WITHOUT opening a modal; a flat
``{token: handler}`` table (``_MENU_ACTIONS``) dispatches the chosen token.

We exercise the builders against a REAL headless ``Browser`` / ``Context``
(from ``test.async_._helpers``) with a known item under the cursor — not a fake
ctx. browse-tui swallows ``on_context_menu`` exceptions and a fake ctx hides
bugs, so the real ``Context.cursor`` read path is what we assert against;
``ctx.menu`` itself short-circuits to ``None`` in headless mode, which is
exactly why the builder is split out and tested directly.

The directory derivation (project / cwd / worktree) reads a session's recorded
``cwd`` and walks for a ``.git`` root, so the directory-hierarchy tests run
against a REAL throwaway ``~/.claude/projects`` fixture with a real git repo —
the gates (``_is_git_repo``, ``_has_plan_md``) can't drift from the filesystem.

The recipe is a ``--run-py`` script that imports ``browse_tui`` (only a real
module when the binary loads it) plus the sibling ``md_doc`` / ``md2ansi_lib``
plugins; we stub ``browse_tui`` in ``sys.modules`` and load the extension-less
recipe via ``SourceFileLoader`` with ``recipes/`` on ``sys.path`` so md_doc is
LIVE (``_session_cwd_and_root`` / the worktree walk lean on
``md_doc.find_git_root``). Same loader pattern as
``test/unit/test_browse_git_context_menu.py``.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from test.async_._helpers import (Context, Item, make_browser,
                                  mod as _real_mod, upsert as _real_upsert)


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-claude'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    The recipe pulls ``Action`` / ``Browser`` / ``BrowserConfig`` / ``Item``
    plus a handful of push-API helpers from ``browse_tui``; none are exercised
    by the pure builders under test (the cursor item comes from the REAL
    Browser below), so inert stubs are enough to let the module load. A fresh
    module each call keeps a stub left by another recipe's test from bleeding
    in.
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
    mod.upsert = lambda *a, **k: None
    mod.mod = lambda *a, **k: None
    mod.set_preview_op = lambda *a, **k: None
    mod.visible_items = lambda state: []
    mod.recipe_argv = lambda argv=None: []
    mod.KEEP_PARENT = object()
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-claude recipe with md_doc LIVE.

    ``recipes/`` is put on ``sys.path`` so the recipe's ``import md_doc`` /
    ``md2ansi_lib`` succeed — the directory cluster's git-root walk and the
    message toggle-markdown row both depend on those being importable.
    """
    _stub_browse_tui()
    recipes_dir = str(_RECIPE.parent)
    added = recipes_dir not in sys.path
    if added:
        sys.path.insert(0, recipes_dir)
    try:
        name = '_browse_claude_cm_under_test'
        loader = SourceFileLoader(name, str(_RECIPE))
        spec = importlib.util.spec_from_loader(name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        if added and recipes_dir in sys.path:
            sys.path.remove(recipes_dir)


def _browser_with_item(item, *, extra=()):
    """A real headless Browser whose cursor sits on ``item``.

    ``extra`` are sibling rows listed alongside ``item``. The cursor is parked
    on ``item`` via ``cursor_to`` after the root children settle, mirroring the
    procs / git context-menu tests.
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


def _git(repo, *args, env):
    subprocess.run(['git', '-C', repo, *args], check=True,
                   capture_output=True, text=True, env=env)


import contextlib


@contextlib.contextmanager
def _env(**overrides):
    """Temporarily set os.environ vars (e.g. ``SHELL``) for the block."""
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


# ----- per-kind builders against a real cursor (no real dirs) --------------


class TestPerKindMenus(unittest.TestCase):
    """``context_menu_options`` returns the right per-kind rows + dir cluster.

    Each case parks a real cursor on one kind of row. The per-kind rows come
    first, then the shared directory cluster. Here the cursor ids point at
    paths that DON'T resolve to a real ``~/.claude/projects`` session, so the
    directory cluster degrades to its always-on rows (``Open in browse-fs`` /
    ``Show full path``) — the git-mode / browse-plan gates and the dedup are
    exercised in the real-fixture test below.
    """

    def setUp(self):
        self.r = _load_recipe()

    def _ctx(self, item, extra=()):
        self.b = _browser_with_item(item, extra=extra)
        return Context(self.b)

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def test_message_menu_rows_and_hints(self):
        # A message row reuses the E / V / M / y / m actions, each carrying its
        # literal hotkey hint. md2ansi_lib is live (recipes/ on path) so the
        # ``m`` toggle row is present.
        item = Item(id=('msg', '/no/such/sess.jsonl', 3), title='hello',
                    has_children=False)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows)[:5], [
            'msg.edit', 'msg.view', 'msg.mdcat', 'msg.id', 'msg.toggle_md',
        ])
        self.assertEqual(dict((t, l) for l, t in rows)['msg.edit'],
                         'Edit source in $EDITOR (E)')
        self.assertEqual(dict((t, l) for l, t in rows)['msg.view'],
                         'View source in $PAGER (V)')
        self.assertEqual(dict((t, l) for l, t in rows)['msg.mdcat'],
                         'Render markdown via mdcat (M)')
        self.assertEqual(dict((t, l) for l, t in rows)['msg.id'],
                         'Show full id (y)')
        self.assertEqual(dict((t, l) for l, t in rows)['msg.toggle_md'],
                         'Toggle markdown coloring (m)')

    def test_message_toggle_md_row_absent_without_md2ansi(self):
        # When md2ansi_lib didn't load the ``m`` action is unbound, so the menu
        # drops the toggle row (matching the keybinding's availability).
        self.r._md2ansi_fn = None
        item = Item(id=('msg', '/no/such/sess.jsonl', 0), title='x')
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertNotIn('msg.toggle_md', _tokens(rows))
        self.assertIn('msg.id', _tokens(rows))

    def test_session_menu_rows(self):
        item = Item(id=('session', '/no/such/sess.jsonl'), title='sess',
                    has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows)[:2], ['session.view', 'session.path'])
        self.assertEqual(dict((t, l) for l, t in rows)['session.view'],
                         'Open transcript in $PAGER (V)')

    def test_agent_menu_rows(self):
        item = Item(id=('agent', '/no/such/sess.jsonl', 'abc123'),
                    title='subagent', has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows)[:2], ['agent.view', 'agent.id'])
        self.assertEqual(dict((t, l) for l, t in rows)['agent.view'],
                         'Open agent transcript in $PAGER (V)')

    def test_project_menu_is_dir_cluster_only(self):
        # A project row has no per-row actions of its own — only the directory
        # cluster. (Which git/plan rows appear depends on the resolved dir,
        # exercised in the real-fixture class; here we assert the cluster leads
        # with browse-fs and ends with show-path, with no per-kind rows before
        # it.)
        item = Item(id=('project', '/no/such/projdir'), title='proj',
                    has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        toks = _tokens(rows)
        self.assertEqual(toks[0], 'dir.fs')
        self.assertEqual(toks[-1], 'dir.path')
        # Every row is a directory-cluster row (no message/session/agent rows).
        self.assertTrue(all(t.startswith('dir.') for t in toks))

    def test_umbrella_menus_carry_source_actions_and_dir_cluster(self):
        # A prompt / tool / span umbrella row now returns a non-empty menu:
        # the source actions on the umbrella itself (edit / view / id, reusing
        # the msg.* tokens), then the shared directory cluster.
        for kind in ('prompt', 'tool', 'span'):
            item = Item(id=(kind, '/no/such/sess.jsonl', 7),
                        title=f'<{kind}>', has_children=True)
            rows = self.r.context_menu_options(self._ctx(item))
            toks = _tokens(rows)
            self.assertEqual(toks[:3], ['msg.edit', 'msg.view', 'msg.id'],
                             f'{kind} umbrella source rows')
            by_tok = dict((t, l) for l, t in rows)
            self.assertEqual(by_tok['msg.edit'], 'Edit source in $EDITOR (E)')
            self.assertEqual(by_tok['msg.view'], 'View source in $PAGER (V)')
            self.assertEqual(by_tok['msg.id'], 'Show full id (y)')
            # The directory cluster is appended (degrades to its always-on rows
            # here — the jsonl path doesn't resolve to a real session).
            dir_toks = [t for t in toks if t.startswith('dir.')]
            self.assertEqual(dir_toks[0], 'dir.fs')
            self.assertEqual(dir_toks[-1], 'dir.path')
            self.b.stop_workers()

    def test_dir_cluster_appended_to_every_kind(self):
        # The directory cluster (browse-fs first, show-path last) is appended
        # after the per-kind rows for message / session / agent / project rows.
        for item in (
            Item(id=('msg', '/no/such/s.jsonl', 0), title='m'),
            Item(id=('session', '/no/such/s.jsonl'), title='s'),
            Item(id=('agent', '/no/such/s.jsonl', 'a'), title='a'),
            Item(id=('project', '/no/such/p'), title='p'),
            Item(id=('prompt', '/no/such/s.jsonl', 0), title='<prompt>'),
            Item(id=('tool', '/no/such/s.jsonl', 0), title='<tool>'),
            Item(id=('span', '/no/such/s.jsonl', 0), title='<span>'),
        ):
            toks = _tokens(self.r.context_menu_options(self._ctx(item)))
            dir_toks = [t for t in toks if t.startswith('dir.')]
            self.assertEqual(dir_toks[0], 'dir.fs')
            self.assertEqual(dir_toks[-1], 'dir.path')
            self.b.stop_workers()

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

    def test_unmenued_kind_yields_empty(self):
        # A markdown subtree node ('md', …) / synthetic row has no menu.
        item = Item(id=('md', ('msg', '/s.jsonl', 0), (), None), title='# H')
        self.assertEqual(self.r.context_menu_options(self._ctx(item)), [])

    def test_no_clipboard_or_copy_entries(self):
        # Convention (#1028): recipe menus carry no clipboard / Copy rows.
        for item in (
            Item(id=('msg', '/s.jsonl', 0), title='m'),
            Item(id=('session', '/s.jsonl'), title='s'),
            Item(id=('agent', '/s.jsonl', 'a'), title='a'),
            Item(id=('project', '/p'), title='p'),
        ):
            for label in _labels(self.r.context_menu_options(self._ctx(item))):
                self.assertNotIn('copy', label.lower())
                self.assertNotIn('clipboard', label.lower())
            self.b.stop_workers()

    def test_every_emitted_token_has_a_handler(self):
        # Every token any builder can emit dispatches through _MENU_ACTIONS.
        emitted = set()
        for item in (
            Item(id=('msg', '/s.jsonl', 0), title='m'),
            Item(id=('session', '/s.jsonl'), title='s'),
            Item(id=('agent', '/s.jsonl', 'a'), title='a'),
            Item(id=('project', '/p'), title='p'),
            Item(id=('prompt', '/s.jsonl', 0), title='<prompt>'),
            Item(id=('tool', '/s.jsonl', 0), title='<tool>'),
            Item(id=('span', '/s.jsonl', 0), title='<span>'),
        ):
            for _l, tok in self.r.context_menu_options(self._ctx(item)):
                emitted.add(tok)
            self.b.stop_workers()
        for tok in emitted:
            self.assertIn(tok, self.r._MENU_ACTIONS,
                          f'token {tok!r} has no dispatch handler')


# ----- directory cluster pure helpers (no fixture) -------------------------


class TestDirHelpers(unittest.TestCase):
    """The pure dedup / chooser / action-row helpers in isolation."""

    def setUp(self):
        self.r = _load_recipe()

    def test_dedup_collapses_coincident_roles_by_realpath(self):
        # project / cwd / worktree all the same path → one entry, roles merged.
        dd = self.r._dedup_dirs([('project', '/p'), ('cwd', '/p'),
                                 ('worktree', '/p')])
        self.assertEqual(dd, [(['project', 'cwd', 'worktree'], '/p')])

    def test_dedup_keeps_distinct_dirs_in_first_seen_order(self):
        dd = self.r._dedup_dirs([('project', '/p'), ('cwd', '/p'),
                                 ('worktree', '/w')])
        self.assertEqual(dd, [(['project', 'cwd'], '/p'),
                              (['worktree'], '/w')])

    def test_dedup_resolves_symlinks(self):
        # Two paths that realpath to the same dir collapse to one.
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, tmp, ignore_errors=True)
        real = os.path.join(tmp, 'real')
        os.makedirs(real)
        link = os.path.join(tmp, 'link')
        os.symlink(real, link)
        dd = self.r._dedup_dirs([('project', real), ('cwd', link)])
        self.assertEqual(len(dd), 1)
        self.assertEqual(dd[0][0], ['project', 'cwd'])

    def test_dir_chooser_rows_label_role_and_path(self):
        rows = self.r._dir_chooser_rows([(['project', 'cwd'], '/p'),
                                         (['worktree'], '/w')])
        self.assertEqual(rows, [('project / cwd: /p', '/p'),
                                ('worktree: /w', '/w')])

    def test_choose_dir_skips_chooser_when_one_qualifies(self):
        # One dir → return its path directly, no ctx.menu call.
        class NoMenuCtx:
            def menu(self, items, **kw):
                raise AssertionError('chooser must not open for a single dir')
        path = self.r._choose_dir(NoMenuCtx(), [(['project', 'cwd'], '/p')])
        self.assertEqual(path, '/p')

    def test_choose_dir_opens_chooser_when_multiple_qualify(self):
        # Two dirs → ctx.menu is opened over the role-labelled chooser rows.
        captured = {}

        class RecCtx:
            def menu(self, items, **kw):
                captured['items'] = list(items)
                return '/w'  # user picks the worktree

        path = self.r._choose_dir(
            RecCtx(), [(['project'], '/p'), (['worktree'], '/w')])
        self.assertEqual(path, '/w')
        self.assertEqual([lbl for lbl, _v in captured['items']],
                         ['project: /p', 'worktree: /w'])

    def test_dir_action_rows_empty_when_no_dirs(self):
        self.assertEqual(self.r._dir_action_rows([]), [])

    def test_dir_action_rows_always_on_for_plain_dir(self):
        # A non-repo dir with no .PLAN.md offers only browse-fs, run-shell and
        # show-path (no git / plan rows).
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, tmp, ignore_errors=True)
        rows = self.r._dir_action_rows([(['project', 'cwd'], tmp)])
        self.assertEqual(_tokens(rows), ['dir.fs', 'dir.shell', 'dir.path'])


# ----- directory hierarchy against a REAL claude-projects + git fixture ----


class TestDirHierarchyRealFixture(unittest.TestCase):
    """The Level-1 actions + dedup against a real session / git repo.

    A throwaway ``~/.claude/projects`` tree holds one session whose recorded
    ``cwd`` is a real git repo carrying ``.PLAN.md`` — so ``_is_git_repo`` (the
    git-mode rows) and ``_has_plan_md`` (the browse-plan row) run through the
    actual filesystem, and the project / cwd / worktree dirs all dedup to that
    one repo (the common case → no Level-2 chooser).
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls._orig_home = os.environ.get('HOME')
        cls.addClassCleanup(cls._restore_home)

        cls.home = os.path.join(cls.tmp, 'home')
        os.makedirs(os.path.join(cls.home, '.claude', 'projects'))

        env = {**os.environ, 'LC_ALL': 'C',
               'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        # The real repo that a session ran in (its recorded cwd).
        cls.repo = os.path.join(cls.tmp, 'proj')
        os.makedirs(cls.repo)
        with open(os.path.join(cls.repo, 'a.txt'), 'w') as f:
            f.write('a\n')
        _git(cls.repo, 'init', '-q', '-b', 'main', env=env)
        _git(cls.repo, 'add', '.', env=env)
        _git(cls.repo, 'commit', '-q', '-m', 'init', env=env)
        with open(os.path.join(cls.repo, '.PLAN.md'), 'w') as f:
            f.write('# Plan\n\n- step\n')
        _git(cls.repo, 'add', '.PLAN.md', env=env)
        _git(cls.repo, 'commit', '-q', '-m', 'plan', env=env)

        # A plain (non-git, no plan) dir for the always-on baseline.
        cls.plain = os.path.join(cls.tmp, 'plain')
        os.makedirs(cls.plain)

    @classmethod
    def _restore_home(cls):
        if cls._orig_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = cls._orig_home

    def setUp(self):
        self.r = _load_recipe()
        os.environ['HOME'] = self.home
        self.r.CLAUDE_ROOT = os.path.join(self.home, '.claude', 'projects')

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def _make_session(self, cwd):
        """Create a ~/.claude/projects/<enc>/sess.jsonl recording ``cwd``."""
        enc = self.r._encode_project_path(cwd)
        projdir = os.path.join(self.home, '.claude', 'projects', enc)
        os.makedirs(projdir, exist_ok=True)
        sess = os.path.join(projdir, 'sess-1.jsonl')
        with open(sess, 'w') as f:
            f.write('{"type":"user","cwd":"%s",'
                    '"message":{"role":"user","content":"hi"}}\n' % cwd)
        return projdir, sess

    def test_session_dirs_dedup_to_one_repo(self):
        _projdir, sess = self._make_session(self.repo)
        dirs = self.r._dedup_dirs(
            self.r._cursor_context_dirs(('session', sess)))
        # project / cwd / worktree all resolve to the same repo → one entry.
        self.assertEqual(len(dirs), 1)
        roles, path = dirs[0]
        self.assertEqual(os.path.realpath(path), os.path.realpath(self.repo))
        self.assertIn('cwd', roles)
        self.assertIn('worktree', roles)

    def test_session_level1_actions_full_set_for_repo_with_plan(self):
        _projdir, sess = self._make_session(self.repo)
        self.b = _browser_with_item(
            Item(id=('session', sess), title='sess-1', has_children=True))
        ctx = Context(self.b)
        rows = self.r.context_menu_options(ctx)
        # Per-kind session rows, then the full directory cluster (run-shell
        # always; the single git row + browse-plan present because the dir is a
        # repo with .PLAN.md).
        self.assertEqual(_tokens(rows), [
            'session.view', 'session.path',
            'dir.fs', 'dir.shell',
            'dir.git',
            'dir.plan',
            'dir.path',
        ])

    def test_git_row_label_and_submenu_modes(self):
        _projdir, sess = self._make_session(self.repo)
        self.b = _browser_with_item(
            Item(id=('session', sess), title='sess-1', has_children=True))
        rows = self.r.context_menu_options(Context(self.b))
        by_tok = dict((t, l) for l, t in rows)
        # A single Level-1 'git ▸' row; the modes live in the Level-2 submenu.
        self.assertEqual(by_tok['dir.git'], 'git ▸')
        self.assertEqual(by_tok['dir.plan'], 'Browse plan in browse-plan')
        self.assertEqual(
            self.r._DIR_GIT_MODES,
            [('commits', 'commits'), ('branches', 'branches'),
             ('status', 'status'), ('stashes', 'stash'), ('reflog', 'reflog')])

    def test_project_row_resolves_same_dir_cluster(self):
        projdir, _sess = self._make_session(self.repo)
        self.b = _browser_with_item(
            Item(id=('project', projdir), title='proj', has_children=True))
        rows = self.r.context_menu_options(Context(self.b))
        # No per-kind project rows; the directory cluster carries run-shell, the
        # single git row + plan.
        self.assertEqual(_tokens(rows), [
            'dir.fs', 'dir.shell',
            'dir.git',
            'dir.plan',
            'dir.path',
        ])

    def test_message_row_resolves_same_dir_cluster(self):
        _projdir, sess = self._make_session(self.repo)
        self.b = _browser_with_item(
            Item(id=('msg', sess, 0), title='hi', has_children=False))
        rows = self.r.context_menu_options(Context(self.b))
        # Message rows + the same full directory cluster.
        self.assertEqual(_tokens(rows)[:4],
                         ['msg.edit', 'msg.view', 'msg.mdcat', 'msg.id'])
        self.assertIn('dir.git', _tokens(rows))
        self.assertIn('dir.plan', _tokens(rows))

    def test_plain_dir_session_has_no_git_or_plan_rows(self):
        _projdir, sess = self._make_session(self.plain)
        self.b = _browser_with_item(
            Item(id=('session', sess), title='sess', has_children=True))
        rows = self.r.context_menu_options(Context(self.b))
        toks = _tokens(rows)
        self.assertNotIn('dir.git', toks)
        self.assertNotIn('dir.plan', toks)
        # Always-on directory rows still present.
        self.assertIn('dir.fs', toks)
        self.assertIn('dir.shell', toks)
        self.assertIn('dir.path', toks)

    def test_is_git_repo_and_has_plan_gates(self):
        # Direct gate checks against the real filesystem.
        self.assertTrue(self.r._is_git_repo(self.repo))
        self.assertFalse(self.r._is_git_repo(self.plain))
        self.assertTrue(self.r._has_plan_md(self.repo))
        self.assertFalse(self.r._has_plan_md(self.plain))

    def test_worktree_omitted_and_no_crash_when_md_doc_absent(self):
        # The worktree role (and the git-mode rows) need md_doc's git-root
        # walk. With md_doc==None the cluster still resolves project / cwd from
        # the recorded cwd WITHOUT crashing — worktree is dropped and no
        # git-mode rows appear, even though the cwd is a real repo.
        self.r._md_doc = None
        _projdir, sess = self._make_session(self.repo)
        dirs = self.r._cursor_context_dirs(('session', sess))
        roles = {role for role, _p in dirs}
        self.assertIn('cwd', roles)
        self.assertNotIn('worktree', roles)
        self.b = _browser_with_item(
            Item(id=('session', sess), title='sess', has_children=True))
        toks = _tokens(self.r.context_menu_options(Context(self.b)))
        self.assertNotIn('dir.git', toks)
        self.assertIn('dir.fs', toks)


# ----- Level-2 chooser dispatch (filter + skip + launch) -------------------


# ----- a session spanning TWO cwds (main repo + a worktree under it) -------


class TestMultiCwdSession(unittest.TestCase):
    """A session whose records span TWO distinct cwds surfaces BOTH dirs.

    Mirrors a real browse-claude transcript: most records carry one ``cwd``
    (a main git repo), a minority carry a second (a git worktree nested under
    it). The directory cluster must surface BOTH distinct working directories
    so the Level-2 chooser appears (>1 distinct dir qualifies) — the bug was
    that the derivation collapsed the session to a single cwd, so the chooser
    never appeared even though the session genuinely worked in two directories.

    The fixture builds a real ``~/.claude/projects/<enc>/`` tree (HOME pointed
    at a throwaway dir) plus a real git repo and a real worktree under it, so
    the git-root walk (``md_doc.find_git_root``) resolves both — a worktree's
    ``.git`` is a *file*, which the walk follows.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls._orig_home = os.environ.get('HOME')
        cls.addClassCleanup(cls._restore_home)

        cls.home = os.path.join(cls.tmp, 'home')
        os.makedirs(os.path.join(cls.home, '.claude', 'projects'))

        env = {**os.environ, 'LC_ALL': 'C',
               'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        # The main repo the session started in.
        cls.main = os.path.join(cls.tmp, 'proj')
        os.makedirs(cls.main)
        with open(os.path.join(cls.main, 'a.txt'), 'w') as f:
            f.write('a\n')
        _git(cls.main, 'init', '-q', '-b', 'main', env=env)
        _git(cls.main, 'add', '.', env=env)
        _git(cls.main, 'commit', '-q', '-m', 'init', env=env)
        # A real git worktree nested UNDER the main repo (the second cwd) —
        # mirrors .claude/worktrees/<name>. Its ``.git`` is a file pointing at
        # the main repo's gitdir, so find_git_root resolves it to itself.
        cls.wt = os.path.join(cls.main, '.claude', 'worktrees', 'wt')
        os.makedirs(os.path.dirname(cls.wt))
        _git(cls.main, 'worktree', 'add', '-q', cls.wt, env=env)

    @classmethod
    def _restore_home(cls):
        if cls._orig_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = cls._orig_home

    def setUp(self):
        self.r = _load_recipe()
        os.environ['HOME'] = self.home
        self.r.CLAUDE_ROOT = os.path.join(self.home, '.claude', 'projects')

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def _make_two_cwd_session(self):
        """Create a session .jsonl whose records carry TWO distinct cwds.

        The project dir is encoded from the MAIN cwd (as Claude Code names it).
        Most records carry the main cwd; a minority carry the worktree cwd —
        the real transcript shape (majority/minority split, main seen first).
        """
        enc = self.r._encode_project_path(self.main)
        projdir = os.path.join(self.home, '.claude', 'projects', enc)
        os.makedirs(projdir, exist_ok=True)
        sess = os.path.join(projdir, 'sess-multi.jsonl')
        line = ('{"type":"user","cwd":"%s",'
                '"message":{"role":"user","content":"hi"}}\n')
        with open(sess, 'w') as f:
            for _ in range(5):           # majority: the main repo
                f.write(line % self.main)
            for _ in range(2):           # minority: the worktree
                f.write(line % self.wt)
        return projdir, sess

    def test_session_surfaces_both_distinct_dirs(self):
        # The crux: a two-cwd session must yield TWO distinct deduped dirs, so
        # the chooser would appear. (Pre-fix the derivation collapsed to one.)
        _projdir, sess = self._make_two_cwd_session()
        dirs = self.r._dedup_dirs(
            self.r._cursor_context_dirs(('session', sess)))
        reals = {os.path.realpath(p) for _roles, p in dirs}
        self.assertEqual(
            reals,
            {os.path.realpath(self.main), os.path.realpath(self.wt)},
            'both the main repo and the worktree must surface as context dirs')
        self.assertEqual(len(dirs), 2,
                         'two distinct working dirs → two chooser entries')

    def test_message_cursor_surfaces_both_dirs(self):
        # Same crux from a message row (resolves to the same session anchor):
        # both dirs surface, driven through the real headless Browser cursor.
        _projdir, sess = self._make_two_cwd_session()
        self.b = _browser_with_item(
            Item(id=('msg', sess, 0), title='hi', has_children=False))
        item_id = Context(self.b).cursor.id
        dirs = self.r._dedup_dirs(self.r._cursor_context_dirs(item_id))
        reals = {os.path.realpath(p) for _roles, p in dirs}
        self.assertIn(os.path.realpath(self.wt), reals)
        self.assertIn(os.path.realpath(self.main), reals)

    def test_two_cwd_session_opens_chooser_for_git_action(self):
        # End-to-end: the git launch over a two-(repo-)cwd session opens the
        # dir chooser listing BOTH dirs (both are git repos here). Drives
        # ``_run_git_mode`` — the shared launch the ``dir.git`` submenu feeds.
        _projdir, sess = self._make_two_cwd_session()
        dirs = self.r._dedup_dirs(
            self.r._cursor_context_dirs(('session', sess)))

        class RecCtx:
            def __init__(self):
                self.menu_items = None
                self.external = None

            def menu(self, items, **kw):
                self.menu_items = list(items)
                return None  # cancel — we only assert the chooser content

            def run_external(self, cmd, **kw):
                self.external = cmd

        ctx = RecCtx()
        self.r._run_git_mode(ctx, dirs, 'status')
        self.assertIsNotNone(ctx.menu_items,
                             'two repo dirs → chooser must open')
        chooser_paths = {v for _lbl, v in ctx.menu_items}
        self.assertEqual(
            {os.path.realpath(p) for p in chooser_paths},
            {os.path.realpath(self.main), os.path.realpath(self.wt)})

    def test_single_cwd_session_still_one_dir_no_chooser(self):
        # Regression guard: a single-cwd session still dedups to ONE dir (no
        # chooser), unchanged by the multi-cwd broadening.
        enc = self.r._encode_project_path(self.main)
        projdir = os.path.join(self.home, '.claude', 'projects', enc)
        os.makedirs(projdir, exist_ok=True)
        sess = os.path.join(projdir, 'sess-single.jsonl')
        with open(sess, 'w') as f:
            f.write('{"type":"user","cwd":"%s",'
                    '"message":{"role":"user","content":"hi"}}\n' % self.main)
        dirs = self.r._dedup_dirs(
            self.r._cursor_context_dirs(('session', sess)))
        self.assertEqual(len(dirs), 1)
        self.assertEqual(os.path.realpath(dirs[0][1]),
                         os.path.realpath(self.main))


class TestRunDirAction(unittest.TestCase):
    """``_run_dir_action`` filters dirs, skips the chooser when one qualifies.

    Driven with a recording ctx (no real subprocess) so we can assert the
    filtered dir set, the chooser skip, and the exact ``run_external`` argv.
    """

    def setUp(self):
        self.r = _load_recipe()

    class _RecCtx:
        def __init__(self, menu_choice=None):
            self.menu_choice = menu_choice
            self.menu_items = None
            self.external = None
            self.alerted = None

        def menu(self, items, **kw):
            self.menu_items = list(items)
            return self.menu_choice

        def run_external(self, cmd, **kw):
            self.external = (cmd, kw)
            return 0

        def alert(self, text, **kw):
            self.alerted = text

    def test_show_path_lists_every_dir_no_chooser(self):
        ctx = self._RecCtx()
        self.r._run_dir_action(ctx, 'dir.path',
                               [(['project'], '/p'), (['worktree'], '/w')])
        # Lists both distinct paths, one per line; never opens a chooser.
        self.assertEqual(ctx.alerted, '/p\n/w')
        self.assertIsNone(ctx.menu_items)

    def test_browse_fs_single_dir_skips_chooser(self):
        ctx = self._RecCtx()
        self.r._run_dir_action(ctx, 'dir.fs', [(['project', 'cwd'], '/p')])
        self.assertIsNone(ctx.menu_items)
        cmd, kw = ctx.external
        self.assertEqual(cmd, ['browse-fs', '/p'])
        self.assertTrue(kw.get('keep_screen'))

    def test_git_mode_filters_to_repo_dirs(self):
        # Two dirs, only one a repo → no chooser, runs on the repo dir.
        # ``_run_git_mode`` is the shared git launch the ``dir.git`` submenu
        # threads the chosen mode into (here ``commits``).
        repo = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, repo, ignore_errors=True)
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        _git(repo, 'init', '-q', '-b', 'main', env=env)
        plain = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, plain, ignore_errors=True)
        ctx = self._RecCtx()
        self.r._run_git_mode(ctx, [(['project'], plain), (['worktree'], repo)],
                             'commits')
        self.assertIsNone(ctx.menu_items)  # only one repo qualifies → no chooser
        cmd, _kw = ctx.external
        self.assertEqual(cmd, ['browse-git', repo, '--commits'])

    def test_git_mode_opens_chooser_for_two_repos(self):
        # Two repo dirs → the dir chooser lists BOTH (filtered + labelled).
        r1 = tempfile.mkdtemp()
        r2 = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, r1, ignore_errors=True)
        self.addCleanup(__import__('shutil').rmtree, r2, ignore_errors=True)
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        _git(r1, 'init', '-q', '-b', 'main', env=env)
        _git(r2, 'init', '-q', '-b', 'main', env=env)
        ctx = self._RecCtx(menu_choice=r2)
        self.r._run_git_mode(ctx, [(['project'], r1), (['worktree'], r2)],
                             'status')
        # Chooser opened, listing both repos role-labelled.
        self.assertEqual([lbl for lbl, _v in ctx.menu_items],
                         [f'project: {r1}', f'worktree: {r2}'])
        cmd, _kw = ctx.external
        self.assertEqual(cmd, ['browse-git', r2, '--status'])

    def test_git_submenu_routes_mode_to_launch(self):
        # ``dir.git`` opens the Level-2 mode submenu, then launches browse-git
        # with the chosen mode on the (single) repo dir — no dir chooser.
        repo = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, repo, ignore_errors=True)
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        _git(repo, 'init', '-q', '-b', 'main', env=env)
        ctx = self._RecCtx(menu_choice='stash')  # picks the 'stashes' mode
        self.r._run_dir_action(ctx, 'dir.git', [(['project', 'cwd'], repo)])
        # The submenu offered the five modes (label, mode).
        self.assertEqual(ctx.menu_items,
                         [('commits', 'commits'), ('branches', 'branches'),
                          ('status', 'status'), ('stashes', 'stash'),
                          ('reflog', 'reflog')])
        cmd, _kw = ctx.external
        self.assertEqual(cmd, ['browse-git', repo, '--stash'])

    def test_git_submenu_cancel_is_a_noop(self):
        # Cancelling the mode submenu (menu → None) launches nothing.
        repo = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, repo, ignore_errors=True)
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        _git(repo, 'init', '-q', '-b', 'main', env=env)
        ctx = self._RecCtx(menu_choice=None)
        self.r._run_dir_action(ctx, 'dir.git', [(['project', 'cwd'], repo)])
        self.assertIsNotNone(ctx.menu_items)  # submenu was shown
        self.assertIsNone(ctx.external)       # but nothing launched

    def test_chooser_cancel_is_a_noop(self):
        # Cancelling the dir chooser (menu → None) launches nothing.
        r1 = tempfile.mkdtemp()
        r2 = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, r1, ignore_errors=True)
        self.addCleanup(__import__('shutil').rmtree, r2, ignore_errors=True)
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        _git(r1, 'init', '-q', '-b', 'main', env=env)
        _git(r2, 'init', '-q', '-b', 'main', env=env)
        ctx = self._RecCtx(menu_choice=None)
        self.r._run_git_mode(ctx, [(['project'], r1), (['worktree'], r2)],
                             'commits')
        self.assertIsNotNone(ctx.menu_items)  # chooser was shown
        self.assertIsNone(ctx.external)       # but nothing launched

    def test_browse_plan_launches_plan_file(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, repo, ignore_errors=True)
        with open(os.path.join(repo, '.PLAN.md'), 'w') as f:
            f.write('# Plan\n')
        ctx = self._RecCtx()
        self.r._run_dir_action(ctx, 'dir.plan', [(['project', 'cwd'], repo)])
        cmd, kw = ctx.external
        self.assertEqual(cmd, ['browse-plan', '-f',
                               os.path.join(repo, '.PLAN.md')])
        self.assertTrue(kw.get('keep_screen'))

    def test_no_applicable_dir_is_a_noop(self):
        # A git launch with no repo dir filters to nothing → no chooser, no run.
        plain = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, plain, ignore_errors=True)
        ctx = self._RecCtx()
        self.r._run_git_mode(ctx, [(['cwd'], plain)], 'commits')
        self.assertIsNone(ctx.menu_items)
        self.assertIsNone(ctx.external)

    def test_run_shell_single_dir_skips_chooser(self):
        # 'Run shell here' applies to every dir; a single dir skips the chooser
        # and execs the user's $SHELL after cd-ing into it.
        ctx = self._RecCtx()
        with _env(SHELL='/bin/zsh'):
            self.r._run_dir_action(ctx, 'dir.shell',
                                   [(['project', 'cwd'], '/p')])
        self.assertIsNone(ctx.menu_items)
        cmd, _kw = ctx.external
        self.assertEqual(cmd, "cd /p && exec /bin/zsh -l -i")

    def test_run_shell_opens_chooser_for_two_dirs(self):
        # >1 distinct dir → the dir chooser opens (run-shell applies to all).
        ctx = self._RecCtx(menu_choice='/w')
        with _env(SHELL='/bin/bash'):
            self.r._run_dir_action(ctx, 'dir.shell',
                                   [(['project'], '/p'), (['worktree'], '/w')])
        self.assertEqual([lbl for lbl, _v in ctx.menu_items],
                         ['project: /p', 'worktree: /w'])
        cmd, _kw = ctx.external
        self.assertEqual(cmd, "cd /w && exec /bin/bash -l -i")


# ----- dispatch table reuses the existing action handlers ------------------


class TestDispatchReusesActions(unittest.TestCase):
    """The message / session / agent tokens reuse the existing action handlers.

    A fake-but-recording ctx confirms each token routes to the right reused
    handler (E / V / M / y / m) and that the path / id pop-up tokens call
    ``ctx.alert`` with the id field.
    """

    def setUp(self):
        self.r = _load_recipe()

    def test_message_tokens_call_reused_handlers(self):
        calls = []
        self.r._action_edit_source = lambda c: calls.append('edit')
        self.r._action_view_source = lambda c: calls.append('view')
        self.r._action_md_preview = lambda c: calls.append('mdcat')
        self.r._action_show_id = lambda c: calls.append('id')
        self.r._action_toggle_md = lambda c: calls.append('toggle')
        ctx = object()
        mid = ('msg', '/s.jsonl', 0)
        self.r._MENU_ACTIONS['msg.edit'](ctx, mid)
        self.r._MENU_ACTIONS['msg.view'](ctx, mid)
        self.r._MENU_ACTIONS['msg.mdcat'](ctx, mid)
        self.r._MENU_ACTIONS['msg.id'](ctx, mid)
        self.r._MENU_ACTIONS['msg.toggle_md'](ctx, mid)
        self.assertEqual(calls, ['edit', 'view', 'mdcat', 'id', 'toggle'])

    def test_session_path_alerts_jsonl(self):
        alerted = {}

        class Ctx:
            def alert(self, text, **kw):
                alerted['text'] = text
                alerted['title'] = kw.get('title')
        self.r._MENU_ACTIONS['session.path'](Ctx(), ('session', '/x/s.jsonl'))
        self.assertEqual(alerted['text'], '/x/s.jsonl')
        self.assertEqual(alerted['title'], 'session')

    def test_agent_id_alerts_agent_id(self):
        alerted = {}

        class Ctx:
            def alert(self, text, **kw):
                alerted['text'] = text
        self.r._MENU_ACTIONS['agent.id'](Ctx(), ('agent', '/s.jsonl', 'aid-9'))
        self.assertEqual(alerted['text'], 'aid-9')


class TestDetailLevelBindings(unittest.TestCase):
    """The detail filter is driven by the absolute ``1``-``6`` key
    bindings; the old ``.`` toggle is gone (#1153)."""

    def test_six_detail_level_actions_present_and_dot_gone(self):
        # The bindings live in the global Action list, not the context
        # menu — inspect the recipe source for them.
        with open(_RECIPE) as f:
            source = f.read()
        for key in ('1', '2', '3', '4', '5', '6'):
            self.assertIn(f"Action('{key}',", source,
                          f"missing detail-level binding for '{key}'")
        self.assertIn('_set_detail_level', source)
        self.assertNotIn("Action('.',", source,
                         "the '.' toggle binding should be gone")
        self.assertNotIn('_action_toggle_filter', source,
                         "the old toggle handler should be gone")


class TestCrossLinkMenu(unittest.TestCase):
    """#1247: cross-link jump rows in the menu + the Enter jump path.

    The link map needs REAL .jsonl files (master + one teammate
    transcript), so the fixture writes a co-located pair. The jump test
    drives the recipe's real ``get_children`` through a headless
    Browser, so ``_chain_expand_then_cursor`` exercises the same
    expand/cursor_to path the binary runs.
    """

    def setUp(self):
        self.r = _load_recipe()
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.sess, self.ap = self._build(self._tmp.name)

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()
        self._tmp.cleanup()

    def _build(self, tmp):
        """Master with teammate w1: spawn @1, reply @3, SendMessage @4."""
        import json as _json
        sess = os.path.join(tmp, 'parent.jsonl')
        tm = ('<teammate-message teammate_id="w1" summary="done 1">\n'
              'first result\n</teammate-message>')
        recs = [
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'kick off'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'toolu_A', 'name': 'Agent',
                  'input': {'name': 'w1',
                            'subagent_type': 'general-purpose',
                            'prompt': 'do the thing'}},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 'toolu_A',
                  'content': 'spawned'},
             ]},
             'toolUseResult': {'agentId': 'W1',
                               'agentType': 'general-purpose',
                               'status': 'completed'}},
            {'type': 'user', 'message': {'role': 'user', 'content': tm}},
            {'type': 'assistant', 'uuid': 'a2',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'toolu_S1',
                  'name': 'SendMessage',
                  'input': {'to': 'w1', 'summary': 'go again',
                            'message': 'more'}},
             ]}},
        ]
        with open(sess, 'w') as f:
            for rec in recs:
                f.write(_json.dumps(rec) + '\n')
        sub_dir = os.path.join(tmp, 'parent', 'subagents')
        os.makedirs(sub_dir)
        ap = os.path.join(sub_dir, 'agent-W1.jsonl')
        wrecs = [
            {'type': 'user', 'message': {'role': 'user', 'content': (
                '<teammate-message teammate_id="team-lead">\n'
                'do the thing\n</teammate-message>')}},
            {'type': 'assistant', 'uuid': 'wa1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'toolu_W1',
                  'name': 'SendMessage',
                  'input': {'to': 'team-lead', 'summary': 'done 1',
                            'message': 'first result'}},
             ]}},
        ]
        with open(ap, 'w') as f:
            for rec in wrecs:
                f.write(_json.dumps(rec) + '\n')
        return sess, ap

    def _ctx(self, item, extra=()):
        self.b = _browser_with_item(item, extra=extra)
        return Context(self.b)

    def test_link_rows_prepended_for_linked_msg_row(self):
        ctx = self._ctx(Item(id=('msg', self.sess, 3), title='reply'))
        rows = self.r.context_menu_options(ctx)
        label, token = rows[0]
        self.assertEqual(token, ('link.jump', ('msg', self.ap, 1)))
        self.assertTrue(label.startswith('↗ W1:2'), label)
        # The regular per-kind rows still follow.
        self.assertIn('msg.edit', _tokens(rows))

    def test_backlink_row_on_agent_group(self):
        ctx = self._ctx(Item(id=('agent', self.sess, 'W1'), title='w1'))
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(rows[0][1], ('link.jump', ('msg', self.sess, 1)))
        self.assertIn('agent.view', _tokens(rows))

    def test_backlink_row_on_worker_side_msg(self):
        # A row INSIDE the teammate transcript resolves through the
        # owning master's reverse index: the worker's SendMessage row
        # backlinks to the master's inbound-reply line.
        ctx = self._ctx(Item(id=('msg', self.ap, 1), title='send'))
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(rows[0][1], ('link.jump', ('msg', self.sess, 3)))
        self.assertIn('msg.edit', _tokens(rows))

    def test_no_link_rows_on_plain_row(self):
        ctx = self._ctx(Item(id=('msg', self.sess, 0), title='m'))
        rows = self.r.context_menu_options(ctx)
        self.assertFalse([t for t in _tokens(rows) if isinstance(t, tuple)])

    def _live_browser(self):
        """Headless Browser driving the recipe's real ``get_children``."""
        sess_item = Item(id=('session', self.sess), title='s',
                         has_children=True)

        def kids(item_id, *, reload=False):
            if item_id is None:
                return [sess_item]
            # The recipe under test builds STUB Items (browse_tui is
            # stubbed at load) — re-wrap them as real framework Items so
            # the Browser can index/expand them.
            return [Item(id=c.id, title=getattr(c, 'title', ''),
                         has_children=bool(getattr(c, 'has_children',
                                                   False)),
                         hidden=bool(getattr(c, 'hidden', False)),
                         meta=bool(getattr(c, 'meta', False)),
                         boundary=bool(getattr(c, 'boundary', False)))
                    for c in self.r.get_children(item_id)]

        self.b = make_browser(get_children=kids)
        b = self.b
        b.refresh()
        b.run_until_idle()
        b.expand(('session', self.sess))
        b.run_until_idle()
        b.cursor_to(('agent', self.sess, 'W1'))
        b.run_until_idle()
        return b

    def test_enter_jump_moves_cursor_across_files(self):
        # Enter on the subagent group row follows the backlink: the
        # spawn site's <prompt>/<tool> umbrellas expand, the cursor
        # lands on the spawn leaf. Real recipe get_children throughout.
        # Detail 'all' so the tool_use leaf itself is visible.
        self.r._DETAIL_LEVEL = 6
        b = self._live_browser()
        ctx = Context(b)
        self.assertEqual(ctx.cursor.id, ('agent', self.sess, 'W1'))
        self.r._on_enter_jump(ctx)
        b.run_until_idle()
        self.assertEqual(Context(b).cursor.id, ('msg', self.sess, 1))

    def test_enter_jump_falls_back_to_visible_ancestor(self):
        # At detail level 1 the spawn tool_use leaf is hidden — the jump
        # lands on the nearest filter-visible ancestor (the <prompt>).
        self.r._DETAIL_LEVEL = 1
        b = self._live_browser()
        self.r._on_enter_jump(Context(b))
        b.run_until_idle()
        self.assertEqual(Context(b).cursor.id, ('prompt', self.sess, 0))

    def test_enter_noop_without_links(self):
        ctx = self._ctx(Item(id=('msg', self.sess, 0), title='m'))
        before = ctx.cursor.id
        self.r._on_enter_jump(ctx)     # must not raise nor move
        self.assertEqual(Context(self.b).cursor.id, before)


class TestCrossLinkJumpVoiceRace(unittest.TestCase):
    """#1243: a cross-link jump must not lose the race to the expand-time
    jump-to-latest-voice.

    ``_jump_to_link`` reveals its target by expanding the ancestor chain
    and then ``cursor_to``-ing the target. Those programmatic expands fire
    the ``on_expand`` lifecycle hook like any other; with the ancestors'
    children UNCACHED the hook parks each id in ``_AWAITING_VOICE_JUMP``
    and ``_on_children_loaded`` posts the deferred jump-to-latest-voice
    AFTER the chain's cursor_to — dragging the cursor off the target onto
    whatever voice is newest under the last ancestor. The fix:
    ``_chain_expand_then_cursor`` claims each collapsed ancestor in
    ``_VOICE_JUMP_SUPPRESSED`` before expanding it, and ``_on_expand``
    skips (and consumes) the claim.

    Unlike ``TestCrossLinkMenu``'s jump tests, the headless Browser here
    registers the recipe's REAL ``on_expand`` / ``on_children_loaded``
    hooks — without them the race under test doesn't exist. And because
    ``run_until_idle`` never fires the post-drain lifecycle hooks (only
    the real main loop does), the pump replicates the loop's tick order:
    drain → apply children results → fire expand/collapse → fire
    children-loaded.

    Fixture: the ``TestCrossLinkMenu`` master/teammate pair, plus one
    assistant text voice appended to the AGENT transcript. The forward
    jump (teammate-reply row → the agent's SendMessage record) then has
    ancestor chain ``[('agent', …), ('span', ap, 1)]`` where the span's
    latest voice is the appended record — NEWER than the jump target and
    exactly what the unsuppressed deferred jump used to land on.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._tmp = tempfile.TemporaryDirectory()
        self.sess, self.ap = TestCrossLinkMenu._build(self, self._tmp.name)
        # A voice NEWER than the jump target, in the same span of the
        # agent transcript: the last ancestor's latest voice ≠ target.
        import json as _json
        with open(self.ap, 'a') as f:
            f.write(_json.dumps(
                {'type': 'assistant', 'uuid': 'wa2',
                 'message': {'role': 'assistant', 'content': [
                     {'type': 'text', 'text': 'AGENT_NEWEST'}]}}) + '\n')
        self.r._DETAIL_LEVEL = 6
        self.target = ('msg', self.ap, 1)
        self.last_ancestor = ('span', self.ap, 1)

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()
        self._tmp.cleanup()

    def _hooked_browser(self):
        """Headless Browser with the recipe's lifecycle hooks REGISTERED."""
        sess_item = Item(id=('session', self.sess), title='s',
                         has_children=True)

        def kids(item_id, *, reload=False):
            if item_id is None:
                return [sess_item]
            return [Item(id=c.id, title=getattr(c, 'title', ''),
                         has_children=bool(getattr(c, 'has_children',
                                                   False)),
                         hidden=bool(getattr(c, 'hidden', False)),
                         meta=bool(getattr(c, 'meta', False)),
                         boundary=bool(getattr(c, 'boundary', False)))
                    for c in self.r.get_children(item_id)]

        self.b = make_browser(get_children=kids,
                              on_expand=self.r._on_expand,
                              on_children_loaded=self.r._on_children_loaded)
        return self.b

    def _pump(self, b, timeout=5.0):
        """One real-main-loop tick at a time, until fully settled."""
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            b.drain_main_queue()
            b.apply_children_results()
            b._fire_expand_collapse_if_pending()
            b._fire_children_loaded_if_pending()
            if (b._main_queue.empty() and not b._children_queue
                    and not b._children_results
                    and not b._children_in_flight
                    and not b._state._children_pending
                    and not self.r._AWAITING_VOICE_JUMP):
                return
            _time.sleep(0.005)
        self.fail('pump: browser did not settle')

    def _cursor_on_reply_row(self, b):
        """Expand down to and land on the teammate-reply master row."""
        b.refresh()
        self._pump(b)
        b.expand(('session', self.sess))
        self._pump(b)
        b.expand(('prompt', self.sess, 0))
        self._pump(b)
        b.cursor_to(('msg', self.sess, 3))
        self._pump(b)
        self.assertEqual(Context(b).cursor.id, ('msg', self.sess, 3))

    def test_jump_wins_over_newer_voice_in_uncached_ancestors(self):
        # Preconditions: single forward link into the agent transcript,
        # and the last ancestor's latest voice is the NEWER appended
        # record — the row the unsuppressed race used to land on.
        self.assertEqual(self.r._links_for_id(('msg', self.sess, 3)),
                         [self.target])
        self.assertEqual(
            self.r._latest_voice_among_children(self.last_ancestor),
            ('msg', self.ap, 2))

        b = self._hooked_browser()
        self._cursor_on_reply_row(b)
        self.r._on_enter_jump(Context(b))
        self._pump(b)
        self.assertEqual(
            Context(b).cursor.id, self.target,
            'the jump target must win — not the newer voice the '
            'expand-time jump-to-latest-voice would land on')

    def test_manual_reexpand_after_jump_still_drills_to_latest_voice(self):
        b = self._hooked_browser()
        self._cursor_on_reply_row(b)
        self.r._on_enter_jump(Context(b))
        self._pump(b)
        self.assertEqual(Context(b).cursor.id, self.target)
        # Every claim was consumed by the suppressed expands.
        self.assertEqual(self.r._VOICE_JUMP_SUPPRESSED, set())
        # A MANUAL collapse + re-expand of the same ancestor must drill
        # in to the latest voice normally (one-shot suppression).
        b.collapse(self.last_ancestor)
        self._pump(b)
        b.expand(self.last_ancestor)
        self._pump(b)
        self.assertEqual(Context(b).cursor.id, ('msg', self.ap, 2),
                         'a manual re-expand must jump to the latest '
                         'voice — the claim is consume-on-fire')


class TestTailSubagentBlockInsert(unittest.TestCase):
    """#1245: a subagent linked by the live tail must land INSIDE the
    session's top block (the divider sentinel ids double as insertion
    pivots), not append at the bottom of the root listing; a session's
    FIRST subagent brings the whole block in at the top. After every
    insert the visible sequence equals a fresh ``_list_tree_roots``
    refetch, so a later refetch is an ordering no-op.

    Same harness as ``TestCrossLinkJumpVoiceRace``: headless Browser
    with the recipe's real hooks and the drain→apply→fire pump. The
    recipe's ``upsert``/``mod`` globals (inert stubs from
    ``_stub_browse_tui``) are swapped for the REAL op constructors from
    the same state module the Browser uses, so ``_push_tail_diffs``
    ops interoperate with its ``update_data``.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.r.upsert = _real_upsert
        self.r.mod = _real_mod
        self.r._DETAIL_LEVEL = 6
        self._tmp = tempfile.TemporaryDirectory()
        self.sess = os.path.join(self._tmp.name, 'parent.jsonl')
        self.sub_dir = os.path.join(self._tmp.name, 'parent', 'subagents')
        os.makedirs(self.sub_dir)

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()
        self._tmp.cleanup()

    def _append(self, path, recs):
        import json as _json
        with open(path, 'a') as f:
            for rec in recs:
                f.write(_json.dumps(rec) + '\n')

    def _spawn_recs(self, aid, tool_id, auuid):
        """Assistant Agent tool_use + linking tool_result pair."""
        return [
            {'type': 'assistant', 'uuid': auuid,
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': tool_id, 'name': 'Agent',
                  'input': {'prompt': 'go'}},
             ]}},
            {'type': 'user',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': tool_id,
                  'content': 'spawned'},
             ]},
             'toolUseResult': {'agentId': aid, 'status': 'completed'}},
        ]

    def _mk_agent_file(self, aid, mtime=None):
        ap = os.path.join(self.sub_dir, f'agent-{aid}.jsonl')
        self._append(ap, [
            {'type': 'user',
             'message': {'role': 'user', 'content': 'sub prompt'}},
        ])
        if mtime is not None:
            os.utime(ap, (mtime, mtime))
        return ap

    def _mk_session(self, *, with_agent):
        recs = [
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'kick off'}},
        ]
        if with_agent:
            recs += self._spawn_recs('W1', 'toolu_A', 'a1')
            self._mk_agent_file('W1', mtime=time.time() - 100)
        self._append(self.sess, recs)

    def _hooked_browser(self):
        sess_item = Item(id=('session', self.sess), title='s',
                         has_children=True)

        def kids(item_id, *, reload=False):
            if item_id is None:
                return [sess_item]
            return [Item(id=c.id, title=getattr(c, 'title', ''),
                         has_children=bool(getattr(c, 'has_children',
                                                   False)),
                         hidden=bool(getattr(c, 'hidden', False)),
                         meta=bool(getattr(c, 'meta', False)),
                         boundary=bool(getattr(c, 'boundary', False)))
                    for c in self.r.get_children(item_id)]

        self.b = make_browser(get_children=kids,
                              on_expand=self.r._on_expand,
                              on_children_loaded=self.r._on_children_loaded)
        return self.b

    def _pump(self, b, timeout=5.0):
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            b.drain_main_queue()
            b.apply_children_results()
            b._fire_expand_collapse_if_pending()
            b._fire_children_loaded_if_pending()
            if (b._main_queue.empty() and not b._children_queue
                    and not b._children_results
                    and not b._children_in_flight
                    and not b._state._children_pending
                    and not self.r._AWAITING_VOICE_JUMP):
                return
            _time.sleep(0.005)
        self.fail('pump: browser did not settle')

    def _open_session(self):
        b = self._hooked_browser()
        b.refresh()
        self._pump(b)
        b.expand(('session', self.sess))
        self._pump(b)
        return b

    def _tail_arrive(self, b, aid, tool_id, auuid):
        """Simulate one tail tick that links a NEW subagent."""
        self._mk_agent_file(aid)
        self._append(self.sess, self._spawn_recs(aid, tool_id, auuid))
        records, dirty = self.r._read_new_records(self.sess)
        self.assertIn(('session', self.sess), dirty)
        self.r._push_tail_diffs(b, dirty)
        self._pump(b)

    def _root_ids(self, b):
        return [it.id for it in b.cached_children(('session', self.sess))]

    def test_insert_into_existing_block(self):
        self._mk_session(with_agent=True)
        b = self._open_session()
        sep_sub = ('sep', self.sess, 'subagents')
        sep_sess = ('sep', self.sess, 'session')
        self.assertEqual(self._root_ids(b), [
            sep_sub, ('agent', self.sess, 'W1'), sep_sess,
            ('prompt', self.sess, 0)])

        self._tail_arrive(b, 'W2', 'toolu_B', 'a2')
        got = self._root_ids(b)
        # Newest-first: W2 lands right after the Subagents divider.
        self.assertEqual(got, [
            sep_sub, ('agent', self.sess, 'W2'),
            ('agent', self.sess, 'W1'), sep_sess,
            ('prompt', self.sess, 0)])
        # Refetch-identical (and dividers not duplicated).
        self.assertEqual(got,
                         [c.id for c in
                          self.r.get_children(('session', self.sess))])

    def test_first_agent_creates_block_at_top(self):
        self._mk_session(with_agent=False)
        b = self._open_session()
        self.assertEqual(self._root_ids(b), [('prompt', self.sess, 0)])

        self._tail_arrive(b, 'W1', 'toolu_A', 'a1')
        got = self._root_ids(b)
        self.assertEqual(got, [
            ('sep', self.sess, 'subagents'), ('agent', self.sess, 'W1'),
            ('sep', self.sess, 'session'), ('prompt', self.sess, 0)])
        self.assertEqual(got,
                         [c.id for c in
                          self.r.get_children(('session', self.sess))])

    def test_subsequent_refetch_is_ordering_noop(self):
        self._mk_session(with_agent=True)
        b = self._open_session()
        self._tail_arrive(b, 'W2', 'toolu_B', 'a2')
        before = self._root_ids(b)
        # A refetch of the session's children reproduces the same
        # sequence: no divider duplicates, no reordering.
        b.refresh(('session', self.sess))
        self._pump(b)
        self.assertEqual(self._root_ids(b), before)
        # And an idle tail tick (no new bytes) pushes nothing.
        records, dirty = self.r._read_new_records(self.sess)
        self.assertEqual((records, dirty), ([], set()))


if __name__ == '__main__':
    unittest.main()
