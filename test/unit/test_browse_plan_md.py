"""Unit tests for ``recipes/browse-plan``'s markdown launcher rows (#1018).

When the optional ``md_doc`` plugin is importable, a ticket whose BODY (the
``plan N get`` text) references one or more EXISTING ``.md`` files gains a
``[md] References`` umbrella child alongside its subtasks; expanding the
umbrella reveals one ``[md ↗]`` launcher row per referenced file, and Enter on
a launcher row opens that real file in browse-md by PATH. Detection is deferred
to expansion (the body is not in hand at listing time), so a leaf ticket
carries an OPTIMISTIC arrow that self-heals away if its expand yields neither
subtasks nor refs.

Enter is overridden so browse-plan — a BROWSER, not a picker — NEVER prints the
cursor id and quits: a launcher row launches, every other row toggles
expand/collapse.

These tests resolve refs on the FILESYSTEM (plan refs are repo-relative), so a
``tmp`` directory with real ``.md`` files stands in for the git root; ``plan``
is never shelled out (``_ticket_body`` / ``_run_plan`` are stubbed). The recipe
is loaded under a ``browse_tui`` stub with a launcher-capable ``md_doc`` forced
into ``sys.modules`` (see TESTING.md's import-order note) so the guarded
launcher block is present.
"""

import importlib.util
import os
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

    ``Item`` keeps its kwargs as attributes so the children-builder tests can
    read ``.id`` / ``.tag`` / ``.title`` / ``.has_children``. ``mod`` / ``upsert``
    return a recognisable op tuple so the self-heal test can assert the pushed
    op without a real framework. Always reinstalled fresh so a stub left by
    another recipe's unit test doesn't bleed in.
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
    mod.mod = lambda *a, **kw: ('mod', a, kw)

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
    launcher block) is evicted first so the recipe's import under our stub
    redefines the launcher block — see TESTING.md's import-order note.
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


class _LaunchCtx:
    """A ``ctx`` stand-in for the ``on_enter`` / ``_md_launch`` tests.

    Records ``run_external`` calls (argv-or-shell-string, env, ``keep_screen``),
    plus the expand/collapse a toggle would issue. ``state.expanded`` is the
    open-id set the toggle consults, mirroring the framework.
    """

    def __init__(self, cursor, expanded=None):
        self.cursor = cursor
        self.calls = []          # (cmd, env, keep_screen)
        self.expanded = expanded if expanded is not None else set()
        self.collapsed = []
        self.state = types.SimpleNamespace(expanded=self.expanded)

    def run_external(self, cmd, env=None, *, keep_screen=False):
        self.calls.append((cmd, env, keep_screen))
        return 0

    def expand(self, id, autoscroll=False):
        self.expanded.add(id)

    def collapse(self, id):
        self.collapsed.append(id)
        self.expanded.discard(id)


class _HealCtx:
    """A ``ctx`` stand-in for the self-heal hook.

    ``cached`` maps a parent id to its (already-fetched) children list — the
    ``cached_children`` return. ``update_data`` records the ops pushed so a
    test can assert the arrow-retracting ``mod`` op.
    """

    def __init__(self, cached):
        self._cached = cached
        self.ops = []

    def cached_children(self, parent_id):
        return self._cached.get(parent_id)

    def update_data(self, ops):
        self.ops.extend(ops)


class _MdLauncherBase(unittest.TestCase):
    """Common setUp: load a launcher-capable recipe + a tmp git-root anchor."""

    def setUp(self):
        self.r = _load_recipe('_browse_plan_md_under_test')
        self.assertTrue(hasattr(self.r._md_doc, 'launcher_row'),
                        'md_doc launcher block must be importable for these tests')
        self.tmp = tempfile.mkdtemp()
        # Anchor filesystem ref resolution at the tmp dir (plan refs are
        # repo-relative; ``_git_root`` is what resolve_refs is anchored on).
        self.r._git_root = lambda: self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, relpath, text='# doc\n'):
        """Create ``relpath`` under the tmp root and return it, abspath pair."""
        ab = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(ab), exist_ok=True)
        with open(ab, 'w') as f:
            f.write(text)
        return relpath, os.path.realpath(ab)

    def _body(self, tid, text):
        """Stub the ticket body returned by ``_ticket_body`` for ``tid``."""
        self.r._ticket_body = lambda t, _t=text, _id=tid: _t if t == _id else None


class TestTicketBodyRefs(_MdLauncherBase):
    """The body-References umbrella is built on expand from resolving refs."""

    def test_resolving_refs_kept_missing_dropped(self):
        # docs/real.md exists under the root → kept; gone.md does not → dropped.
        _, real_ab = self._write('docs/real.md')
        self._body(42, 'See docs/real.md and gone.md for details\n')
        refs = self.r._ticket_md_refs(42)
        self.assertEqual([ab for ab, _ in refs], [real_ab])
        self.assertEqual(refs[0][1], 'docs/real.md')      # label = repo-rel path

    def test_prefix_is_the_references_umbrella(self):
        self._write('r.md')
        self._body(7, 'see r.md\n')
        prefix = self.r._ticket_md_prefix(7)
        self.assertEqual(len(prefix), 1)
        umb = prefix[0]
        self.assertEqual(umb.id, ('md-refs', 7))
        self.assertEqual(umb.tag, 'md')
        self.assertTrue(umb.has_children)

    def test_no_resolving_refs_yields_no_umbrella(self):
        self._body(7, 'plain body, no markdown links\n')
        self.assertEqual(self.r._ticket_md_refs(7), [])
        self.assertEqual(self.r._ticket_md_prefix(7), [])

    def test_umbrella_children_are_file_launcher_rows(self):
        # Expanding ('md-refs', tid) yields one [md ↗] row per resolving ref,
        # each carrying a ('file', abspath) spec; sorted by display label.
        _, a_ab = self._write('a.md')
        _, b_ab = self._write('docs/b.md')
        self._body(9, 'see a.md and docs/b.md\n')
        rows = self.r.get_children(('md-refs', 9))
        self.assertEqual([r.id for r in rows], [
            ('launch', 9, 'file', a_ab),
            ('launch', 9, 'file', b_ab),
        ])
        for r in rows:
            self.assertFalse(r.has_children)
            self.assertEqual(r.tag, 'md ↗')

    def test_get_children_ticket_prepends_umbrella_before_subtasks(self):
        # A ticket row's children = umbrella FIRST, then its subtasks.
        self._write('a.md')
        self._body(5, 'refs a.md\n')
        self.r._list_subtasks = lambda tid: [
            self.r.Item(id=11, title='subtask')]
        rows = self.r.get_children(5)
        self.assertEqual([r.id for r in rows], [('md-refs', 5), 11])

    def test_get_children_ticket_no_refs_is_subtasks_only(self):
        self._body(5, 'no links here\n')
        self.r._list_subtasks = lambda tid: ['SUBTASKS']
        self.assertEqual(self.r.get_children(5), ['SUBTASKS'])

    def test_ticket_body_failure_yields_no_refs(self):
        # A failed ``plan N get`` (body None) produces no umbrella, no crash.
        self.r._ticket_body = lambda tid: None
        self.assertEqual(self.r._ticket_md_refs(5), [])
        self.assertEqual(self.r._ticket_md_prefix(5), [])


class TestOptimisticArrow(_MdLauncherBase):
    """Leaf tickets get an optimistic arrow that self-heals when empty."""

    def _line(self, tid, *, subtasks):
        """A ``_LIST_FORMAT`` line for ``tid`` with/without subtasks."""
        kids = '1' if subtasks else '0'
        return f'{tid}\tNONE\topen\t{kids}\t0\ttitle'

    def test_leaf_ticket_gets_optimistic_arrow_when_md_doc_present(self):
        # No subtasks, but md_doc is present → optimistic arrow (body not yet read).
        item = self.r._parse_ticket_line(self._line(5, subtasks=False))
        self.assertTrue(item.has_children)

    def test_ticket_with_subtasks_always_has_arrow(self):
        item = self.r._parse_ticket_line(self._line(5, subtasks=True))
        self.assertTrue(item.has_children)

    def test_self_heal_retracts_arrow_on_empty_expand(self):
        # A leaf ticket that expanded to NO children drops its arrow.
        ctx = _HealCtx({5: []})
        self.r._on_children_loaded(ctx, [5])
        self.assertEqual(ctx.ops, [('mod', (5,), {'has_children': False})])

    def test_self_heal_keeps_arrow_when_children_present(self):
        ctx = _HealCtx({5: ['something']})
        self.r._on_children_loaded(ctx, [5])
        self.assertEqual(ctx.ops, [])

    def test_self_heal_skips_uncached(self):
        # Not yet fetched (None, not []) → no premature retraction.
        ctx = _HealCtx({})
        self.r._on_children_loaded(ctx, [5])
        self.assertEqual(ctx.ops, [])

    def test_self_heal_ignores_umbrella_tuple_ids(self):
        # The umbrella (a tuple id) is never empty; the hook leaves it untouched
        # even if its cache momentarily reads [].
        ctx = _HealCtx({('md-refs', 5): []})
        self.r._on_children_loaded(ctx, [('md-refs', 5)])
        self.assertEqual(ctx.ops, [])


class TestEnterOverride(_MdLauncherBase):
    """Enter launches a ``[md ↗]`` row, else toggles; NEVER prints/quits."""

    def test_enter_on_launcher_opens_file_by_path(self):
        # A file launcher row: Enter opens the on-disk file by argv (plain
        # argv, no stdin), the git root as --root, parent keeps the alt screen.
        row = self.r.Item(id=('launch', 5, 'file', '/repo/a.md'),
                          has_children=False)
        ctx = _LaunchCtx(row)
        self.r.on_enter(ctx)
        self.assertEqual(len(ctx.calls), 1)
        cmd, env, keep = ctx.calls[0]
        self.assertIsInstance(cmd, list)            # path form is plain argv
        self.assertEqual(cmd[0], 'browse-md')
        self.assertIn('/repo/a.md', cmd)
        self.assertIsNone(env)
        self.assertEqual(cmd[cmd.index('--root') + 1], self.tmp)
        self.assertTrue(keep)
        self.assertIn('--no-alt-screen', cmd)
        self.assertIn('--quit-on-scope-up', cmd)
        # NOT expanded/collapsed — a launcher row launches, never toggles.
        self.assertEqual(ctx.expanded, set())
        self.assertEqual(ctx.collapsed, [])

    def test_enter_on_expandable_ticket_toggles(self):
        # A normal ticket row keeps the expand/collapse toggle — no launch,
        # and crucially no print-and-quit.
        item = self.r.Item(id=42, has_children=True)
        ctx = _LaunchCtx(item)
        self.r.on_enter(ctx)                        # closed → expand
        self.assertIn(42, ctx.expanded)
        self.assertEqual(ctx.calls, [])
        self.r.on_enter(ctx)                        # open → collapse
        self.assertEqual(ctx.collapsed, [42])

    def test_enter_on_umbrella_toggles(self):
        # The umbrella is expandable (a tuple id, but id[0] != 'launch') → toggle.
        item = self.r.Item(id=('md-refs', 5), has_children=True)
        ctx = _LaunchCtx(item)
        self.r.on_enter(ctx)
        self.assertIn(('md-refs', 5), ctx.expanded)
        self.assertEqual(ctx.calls, [])

    def test_enter_on_leaf_is_noop(self):
        item = self.r.Item(id=11, has_children=False)
        ctx = _LaunchCtx(item)
        self.r.on_enter(ctx)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(ctx.collapsed, [])
        self.assertEqual(ctx.expanded, set())

    def test_enter_with_no_cursor_is_noop(self):
        ctx = _LaunchCtx(None)
        self.r.on_enter(ctx)                        # must not raise
        self.assertEqual(ctx.calls, [])


class TestConfigWiring(unittest.TestCase):
    """``main`` wires ``on_enter`` / ``on_children_loaded`` into the Browser.

    Loads the REAL framework (the generated binary, which is exactly the module
    ``--run-py`` injects as ``browse_tui``) and drives ``main`` up to — not
    through — ``Browser.run``, then reads the resolved hooks off the Browser.
    This is the proof Enter is NOT the framework ``print-exit`` default: the
    recipe's ``on_enter`` callable owns it.
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

    def test_on_children_loaded_is_the_self_heal_hook(self):
        browser = self._build_browser()
        self.assertIs(browser._on_children_loaded, self.r._on_children_loaded)


class TestLaunchArgv(_MdLauncherBase):
    """``_md_launch`` builds the right browse-md invocation (run_external stub)."""

    def test_launch_passes_path_and_root(self):
        ctx = _LaunchCtx(None)
        self.r._md_launch(ctx, ('file', '/x/y.md'))
        self.assertEqual(len(ctx.calls), 1)
        cmd, env, keep = ctx.calls[0]
        self.assertEqual(cmd[0], 'browse-md')
        self.assertIn('/x/y.md', cmd)
        self.assertEqual(cmd[cmd.index('--root') + 1], self.tmp)
        self.assertIsNone(env)
        self.assertTrue(keep)

    def test_launch_without_git_root_omits_root(self):
        self.r._git_root = lambda: None
        ctx = _LaunchCtx(None)
        self.r._md_launch(ctx, ('file', '/x/y.md'))
        cmd, _, _ = ctx.calls[0]
        self.assertNotIn('--root', cmd)

    def test_launch_ignores_unknown_spec(self):
        ctx = _LaunchCtx(None)
        self.r._md_launch(ctx, ('weird', 'thing'))     # not ('file', …)
        self.assertEqual(ctx.calls, [])

    def test_launch_ignores_malformed_spec(self):
        ctx = _LaunchCtx(None)
        self.r._md_launch(ctx, ('file',))              # missing the path
        self.assertEqual(ctx.calls, [])


class TestPreview(_MdLauncherBase):
    """Tuple-id rows preview cleanly (never shell ``plan "(...)" get``)."""

    def test_umbrella_preview_is_the_ticket_body(self):
        self._body(5, '# Ticket body\nrefs a.md\n')
        # md2ansi may or may not be present; either way the body text survives.
        out = self.r.get_preview(('md-refs', 5))
        self.assertIn('Ticket body', out)

    def test_launcher_preview_is_referenced_file_contents(self):
        _, ab = self._write('a.md', '# Referenced doc\nhello\n')
        out = self.r.get_preview(('launch', 5, 'file', ab))
        self.assertIn('Referenced doc', out)

    def test_launcher_preview_unreadable_file(self):
        out = self.r.get_preview(('launch', 5, 'file', '/no/such/file.md'))
        self.assertIn('cannot read', out)


class TestInertWhenMdDocAbsent(unittest.TestCase):
    """With ``md_doc`` unimportable the whole launcher feature is inert."""

    def setUp(self):
        self.r = _load_recipe('_browse_plan_md_inert_under_test')
        self.r._md_doc = None       # simulate md_doc absent

    def test_leaf_ticket_has_no_optimistic_arrow(self):
        # No subtasks AND no md_doc → plain leaf, no arrow.
        line = '5\tNONE\topen\t0\t0\ttitle'
        self.assertFalse(self.r._parse_ticket_line(line).has_children)
        # A ticket WITH subtasks still arrows.
        line2 = '5\tNONE\topen\t1\t0\ttitle'
        self.assertTrue(self.r._parse_ticket_line(line2).has_children)

    def test_refs_and_prefix_empty(self):
        self.r._ticket_body = lambda tid: 'see a.md\n'
        self.assertEqual(self.r._ticket_md_refs(5), [])
        self.assertEqual(self.r._ticket_md_prefix(5), [])
        self.assertEqual(self.r._ticket_md_children(5), [])

    def test_self_heal_noop(self):
        ctx = _HealCtx({5: []})
        self.r._on_children_loaded(ctx, [5])
        self.assertEqual(ctx.ops, [])

    def test_launch_noop(self):
        ctx = _LaunchCtx(self.r.Item(id=('launch', 5, 'file', '/x.md')))
        self.r.on_enter(ctx)
        self.assertEqual(ctx.calls, [])


if __name__ == '__main__':
    unittest.main()
