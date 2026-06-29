"""Unit tests for ``recipes/browse-plan``'s markdown references in the context menu.

When the optional ``md_doc`` plugin is importable, a ticket whose BODY (the
``plan N get`` text) references one or more EXISTING ``.md`` files gains one
context-menu entry per file (``_md_menu_rows``); selecting it opens that real
file in browse-md by PATH (``_md_launch`` → ``md_doc.launch``). The tree itself
carries no markdown rows: a leaf ticket shows NO expander, and Enter only
toggles expand/collapse (browse-plan is a BROWSER, not a picker, so Enter never
prints the cursor id and quits).

These tests resolve refs on the FILESYSTEM (plan refs are repo-relative), so a
``tmp`` directory with real ``.md`` files stands in for the git root; ``plan``
is never shelled out (``_ticket_body`` / ``_run_plan`` are stubbed). The recipe
is loaded under a ``browse_tui`` stub with ``md_doc`` forced into ``sys.modules``
(see TESTING.md's import-order note) so its browse_tui-gated block — which
defines ``md_doc.launch`` — is present.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-plan'


def _stub_browse_tui():
    """Insert a minimal ``browse_tui`` stub so the recipe imports.

    ``Item`` keeps its kwargs as attributes so the tests can read ``.id`` /
    ``.has_children``. ``upsert`` is provided only because the recipe's
    ``from browse_tui import …`` names it (the status actions use it). Always
    reinstalled fresh so a stub left by another recipe's unit test doesn't
    bleed in.
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
    mod.upsert = lambda *a, **kw: ('upsert', a, kw)

    def _recipe_argv(argv=None):
        return sys.argv[1:] if argv is None else argv
    mod.recipe_argv = _recipe_argv
    sys.modules['browse_tui'] = mod


def _load_recipe(name):
    """Load a fresh copy of the browse-plan recipe under module ``name``.

    ``recipes/`` is put on ``sys.path`` so the recipe's optional
    ``from md2ansi_lib import ...`` / ``import md_doc`` resolve just as
    ``--run-py`` arranges at runtime. A stale ``md_doc`` cached by another
    module (possibly imported WITHOUT a browse_tui stub, so missing the
    browse_tui-gated block) is evicted first so the recipe's import under our
    stub redefines it — see TESTING.md's import-order note.
    """
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
    _stub_browse_tui()
    sys.modules.pop('md_doc', None)
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _load_recipe_real(name):
    """Load the recipe against whatever ``browse_tui`` is already in sys.modules.

    Unlike ``_load_recipe`` this does NOT install the stub — the caller has put
    the real framework (the generated binary) there — so the recipe builds
    against genuine ``Browser`` / ``BrowserConfig`` and ``main`` can construct a
    real Browser to inspect. ``md_doc`` is evicted first (import-order note).
    """
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
    sys.modules.pop('md_doc', None)
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _Ctx:
    """A ``ctx`` stand-in for the on_enter / _md_launch / on_context_menu tests.

    Records ``run_external`` calls (argv-or-shell-string, env, ``keep_screen``)
    and the expand/collapse a toggle issues; ``menu`` records the items it was
    handed and returns the preset ``menu_result``. ``state.expanded`` is the
    open-id set the toggle consults, mirroring the framework.
    """

    def __init__(self, cursor=None, *, expanded=None, selected=None,
                 menu_result=None):
        self.cursor = cursor
        self.selected = selected if selected is not None else []
        self.calls = []          # (cmd, env, keep_screen)
        self.expanded = expanded if expanded is not None else set()
        self.collapsed = []
        self.state = types.SimpleNamespace(expanded=self.expanded)
        self.menu_result = menu_result
        self.menu_items = None

    def run_external(self, cmd, env=None, *, keep_screen=False):
        self.calls.append((cmd, env, keep_screen))
        return 0

    def expand(self, id, autoscroll=False):
        self.expanded.add(id)

    def collapse(self, id):
        self.collapsed.append(id)
        self.expanded.discard(id)

    def menu(self, items, **kw):
        self.menu_items = list(items)
        return self.menu_result


class _MdBase(unittest.TestCase):
    """Common setUp: load an md_doc-capable recipe + a tmp git-root anchor."""

    def setUp(self):
        self.r = _load_recipe('_browse_plan_md_under_test')
        self.assertTrue(hasattr(self.r._md_doc, 'launch'),
                        'md_doc browse_tui-gated block must be importable')
        self.tmp = tempfile.mkdtemp()
        # Anchor filesystem ref resolution at the tmp dir (plan refs are
        # repo-relative; ``_git_root`` is what resolve_refs is anchored on).
        self.r._git_root = lambda: self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, relpath, text='# doc\n'):
        """Create ``relpath`` under the tmp root; return (relpath, abspath)."""
        ab = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(ab), exist_ok=True)
        with open(ab, 'w') as f:
            f.write(text)
        return relpath, os.path.realpath(ab)

    def _body(self, tid, text):
        """Stub the ticket body returned by ``_ticket_body`` for ``tid``."""
        self.r._ticket_body = lambda t, _t=text, _id=tid: _t if t == _id else None


class TestTicketMdRefs(_MdBase):
    """``_ticket_md_refs`` resolves a ticket body's ``.md`` references."""

    def test_resolving_refs_kept_missing_dropped(self):
        # docs/real.md exists under the root → kept; gone.md does not → dropped.
        _, real_ab = self._write('docs/real.md')
        self._body(42, 'See docs/real.md and gone.md for details\n')
        refs = self.r._ticket_md_refs(42)
        self.assertEqual([ab for ab, _ in refs], [real_ab])
        self.assertEqual(refs[0][1], 'docs/real.md')      # label = repo-rel path

    def test_no_resolving_refs_yields_empty(self):
        self._body(7, 'plain body, no markdown links\n')
        self.assertEqual(self.r._ticket_md_refs(7), [])

    def test_ticket_body_failure_yields_no_refs(self):
        # A failed ``plan N get`` (body None) produces no refs, no crash.
        self.r._ticket_body = lambda tid: None
        self.assertEqual(self.r._ticket_md_refs(5), [])


class TestLeafArrow(_MdBase):
    """A leaf ticket carries NO expander — the optimistic md arrow is gone."""

    def _line(self, tid, *, subtasks):
        """A ``_LIST_FORMAT`` line for ``tid`` with/without subtasks."""
        kids = '1' if subtasks else '0'
        return f'{tid}\tNONE\topen\t{kids}\t0\ttitle'

    def test_leaf_has_no_arrow_even_with_md_doc(self):
        # The fix: a leaf no longer gets an optimistic markdown expander.
        item = self.r._parse_ticket_line(self._line(5, subtasks=False))
        self.assertFalse(item.has_children)

    def test_ticket_with_subtasks_has_arrow(self):
        item = self.r._parse_ticket_line(self._line(5, subtasks=True))
        self.assertTrue(item.has_children)


class TestMdMenuRows(_MdBase):
    """``_md_menu_rows`` offers one menu row per referenced ``.md`` file."""

    def test_one_row_per_ref_sorted_and_labelled(self):
        _, a_ab = self._write('a.md')
        _, b_ab = self._write('docs/b.md')
        self._body(9, 'see a.md and docs/b.md\n')
        rows = self.r._md_menu_rows(_Ctx(cursor=self.r.Item(id=9)))
        self.assertEqual(rows, [
            ('Open a.md in browse-md', ('md', a_ab)),
            ('Open docs/b.md in browse-md', ('md', b_ab)),
        ])

    def test_empty_when_no_refs(self):
        self._body(9, 'no links here\n')
        self.assertEqual(self.r._md_menu_rows(_Ctx(cursor=self.r.Item(id=9))), [])

    def test_empty_for_project_row(self):
        # The synthetic Project (id=0) has no body to scan.
        self.assertEqual(self.r._md_menu_rows(_Ctx(cursor=self.r.Item(id=0))), [])

    def test_empty_for_no_cursor(self):
        self.assertEqual(self.r._md_menu_rows(_Ctx(cursor=None)), [])


class TestMenuDispatch(_MdBase):
    """``on_context_menu`` appends the md rows and launches on an ``('md', …)``."""

    def test_menu_includes_md_rows(self):
        _, a_ab = self._write('a.md')
        self._body(5, 'refs a.md\n')
        ctx = _Ctx(cursor=self.r.Item(id=5), menu_result=None)
        self.r.on_context_menu(ctx)
        tokens = [tok for _label, tok in ctx.menu_items]
        self.assertIn(('md', a_ab), tokens)

    def test_md_token_launches_browse_md(self):
        ctx = _Ctx(cursor=self.r.Item(id=5), menu_result=('md', '/x/a.md'))
        self.r.on_context_menu(ctx)
        self.assertEqual(len(ctx.calls), 1)
        cmd, env, keep = ctx.calls[0]
        self.assertEqual(cmd[0], 'browse-md')
        self.assertIn('/x/a.md', cmd)
        self.assertEqual(cmd[cmd.index('--root') + 1], self.tmp)
        self.assertIsNone(env)
        self.assertTrue(keep)

    def test_non_md_token_routes_to_menu_actions(self):
        # A plain action token still dispatches through ``_MENU_ACTIONS`` and
        # does NOT launch browse-md.
        called = []
        self.r._MENU_ACTIONS = dict(self.r._MENU_ACTIONS)
        self.r._MENU_ACTIONS['status'] = lambda ctx: called.append(ctx)
        ctx = _Ctx(cursor=self.r.Item(id=5), menu_result='status')
        self.r.on_context_menu(ctx)
        self.assertEqual(called, [ctx])
        self.assertEqual(ctx.calls, [])

    def test_cancel_is_noop(self):
        ctx = _Ctx(cursor=self.r.Item(id=5), menu_result=None)
        self.r.on_context_menu(ctx)        # must not raise
        self.assertEqual(ctx.calls, [])


class TestEnterToggle(_MdBase):
    """Enter only toggles expand/collapse; it NEVER prints the id and quits."""

    def test_expandable_ticket_toggles(self):
        item = self.r.Item(id=42, has_children=True)
        ctx = _Ctx(cursor=item)
        self.r.on_enter(ctx)                        # closed → expand
        self.assertIn(42, ctx.expanded)
        self.assertEqual(ctx.calls, [])
        self.r.on_enter(ctx)                        # open → collapse
        self.assertEqual(ctx.collapsed, [42])

    def test_leaf_is_noop(self):
        ctx = _Ctx(cursor=self.r.Item(id=11, has_children=False))
        self.r.on_enter(ctx)
        self.assertEqual(ctx.expanded, set())
        self.assertEqual(ctx.collapsed, [])

    def test_no_cursor_is_noop(self):
        ctx = _Ctx(cursor=None)
        self.r.on_enter(ctx)                        # must not raise
        self.assertEqual(ctx.calls, [])


class TestLaunch(_MdBase):
    """``_md_launch`` builds the right browse-md invocation (run_external stub)."""

    def test_launch_passes_path_and_root(self):
        ctx = _Ctx()
        self.r._md_launch(ctx, '/x/y.md')
        self.assertEqual(len(ctx.calls), 1)
        cmd, env, keep = ctx.calls[0]
        self.assertEqual(cmd[0], 'browse-md')
        self.assertIn('/x/y.md', cmd)              # path form is plain argv
        self.assertEqual(cmd[cmd.index('--root') + 1], self.tmp)
        self.assertIsNone(env)
        self.assertTrue(keep)
        self.assertIn('--no-alt-screen', cmd)
        self.assertIn('--quit-on-scope-up', cmd)

    def test_launch_without_git_root_omits_root(self):
        self.r._git_root = lambda: None
        ctx = _Ctx()
        self.r._md_launch(ctx, '/x/y.md')
        cmd, _, _ = ctx.calls[0]
        self.assertNotIn('--root', cmd)


class TestConfigWiring(unittest.TestCase):
    """``main`` wires the recipe's ``on_enter`` and DOESN'T set on_children_loaded.

    Loads the REAL framework (the generated binary, which is exactly the module
    ``--run-py`` injects as ``browse_tui``) and drives ``main`` up to — not
    through — ``Browser.run``, then reads the resolved hooks off the Browser.
    This is the proof Enter is NOT the framework ``print-exit`` default: the
    recipe's ``on_enter`` callable owns it; and with the optimistic-arrow
    self-heal gone, no ``on_children_loaded`` hook is wired.
    """

    _BIN = _REPO / 'browse-tui'

    @classmethod
    def setUpClass(cls):
        if not cls._BIN.exists():
            raise unittest.SkipTest(
                'generated browse-tui binary missing; run ./build-tui.sh')

    def setUp(self):
        recipes_dir = str(_REPO / 'recipes')
        if recipes_dir not in sys.path:
            sys.path.insert(0, recipes_dir)
        self._saved = sys.modules.get('browse_tui')
        sys.modules.pop('md_doc', None)
        loader = SourceFileLoader('browse_tui', str(self._BIN))
        spec = importlib.util.spec_from_loader('browse_tui', loader)
        self.bt = importlib.util.module_from_spec(spec)
        loader.exec_module(self.bt)
        sys.modules['browse_tui'] = self.bt
        self.r = _load_recipe_real('_browse_plan_md_wiring_under_test')

    def tearDown(self):
        if self._saved is not None:
            sys.modules['browse_tui'] = self._saved
        else:
            sys.modules.pop('browse_tui', None)

    def _build_browser(self):
        orig_run = self.bt.Browser.run
        self.bt.Browser.run = lambda self: 0
        saved_argv = sys.argv
        sys.argv = ['browse-plan']
        try:
            try:
                self.r.main()
            except SystemExit:
                pass
        finally:
            self.bt.Browser.run = orig_run
            sys.argv = saved_argv
        return self.r._BROWSER

    def test_on_enter_is_the_recipe_handler(self):
        browser = self._build_browser()
        self.assertIs(browser.on_enter, self.r.on_enter)
        # Not the framework print-exit default (a picker would print + quit).
        self.assertNotEqual(browser.on_enter, 'print-exit')

    def test_no_children_loaded_hook(self):
        # The self-heal hook is gone (no optimistic arrow to retract).
        browser = self._build_browser()
        self.assertIsNone(browser._on_children_loaded)


class TestInertWhenMdDocAbsent(unittest.TestCase):
    """With ``md_doc`` unimportable the markdown feature is inert."""

    def setUp(self):
        self.r = _load_recipe('_browse_plan_md_inert_under_test')
        self.r._md_doc = None       # simulate md_doc absent

    def test_leaf_has_no_arrow(self):
        # No subtasks → plain leaf; a ticket WITH subtasks still arrows.
        self.assertFalse(
            self.r._parse_ticket_line('5\tNONE\topen\t0\t0\ttitle').has_children)
        self.assertTrue(
            self.r._parse_ticket_line('5\tNONE\topen\t1\t0\ttitle').has_children)

    def test_refs_empty(self):
        self.r._ticket_body = lambda tid: 'see a.md\n'
        self.assertEqual(self.r._ticket_md_refs(5), [])

    def test_menu_rows_empty(self):
        self.assertEqual(self.r._md_menu_rows(_Ctx(cursor=self.r.Item(id=5))), [])

    def test_launch_noop(self):
        ctx = _Ctx()
        self.r._md_launch(ctx, '/x.md')
        self.assertEqual(ctx.calls, [])


if __name__ == '__main__':
    unittest.main()
