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
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from test.async_._helpers import Context, Item, make_browser


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
    """The detail filter is driven by the absolute ``1``-``4`` key
    bindings; the old ``.`` toggle is gone (#1153)."""

    def test_four_detail_level_actions_present_and_dot_gone(self):
        # The bindings live in the global Action list, not the context
        # menu — inspect the recipe source for them.
        with open(_RECIPE) as f:
            source = f.read()
        for key in ('1', '2', '3', '4'):
            self.assertIn(f"Action('{key}',", source,
                          f"missing detail-level binding for '{key}'")
        self.assertIn('_set_detail_level', source)
        self.assertNotIn("Action('.',", source,
                         "the '.' toggle binding should be gone")
        self.assertNotIn('_action_toggle_filter', source,
                         "the old toggle handler should be gone")


if __name__ == '__main__':
    unittest.main()
