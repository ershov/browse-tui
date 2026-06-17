"""Unit tests for the ``recipes/browse-md`` context menu (ticket #1031).

browse-md surfaces a markdown tree whose row ids are tagged tuples — a per-file
root ``('file', path)``, an in-file heading / list item ``('content', path,
line)``, and the cross-file reference nodes ``('md', anchor, chain, line)`` /
the ``('refs', anchor, chain)`` References umbrella. The context menu branches
on that KIND. Following the committed convention (browse-procs the pilot,
browse-git / browse-claude the rich multi-kind cases) the option list is a PURE
builder, ``context_menu_options(ctx)``, that inspects ``ctx.cursor`` and returns
``(label, token)`` rows WITHOUT opening a modal; a flat ``{token: handler}``
table (``_MENU_ACTIONS``) dispatches the chosen token.

We exercise the builder against a REAL headless ``Browser`` / ``Context`` (from
``test.async_._helpers``) with a known item under the cursor — not a fake ctx.
browse-tui swallows ``on_context_menu`` exceptions and a fake ctx hides bugs, so
the real ``Context.cursor`` read path is what we assert against; ``ctx.menu``
itself short-circuits to ``None`` in headless mode, which is exactly why the
builder is split out and tested directly. The cross-file reference rows are
exercised against a REAL temp-file fixture parsed through ``_reparse`` (so the
``('md', …)`` node and its ``chain`` come from the genuine ref-resolution path,
not a hand-built id).

The recipe is a ``--run-py`` script that imports ``browse_tui`` (only a real
module when the binary loads it) plus the sibling ``md_doc`` / ``md2ansi_lib``
plugins; we stub ``browse_tui`` in ``sys.modules`` and load the extension-less
recipe via ``SourceFileLoader`` with ``recipes/`` on ``sys.path`` so md_doc is
LIVE. Same loader pattern as ``test/unit/test_browse_git_context_menu.py`` /
``test_browse_claude_context_menu.py``.
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from test.async_._helpers import Context, Item, make_browser


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-md'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    The recipe pulls ``Action`` / ``Browser`` / ``BrowserConfig`` / ``Item`` and
    ``recipe_argv`` from ``browse_tui``; none are exercised by the pure builders
    under test (the cursor item comes from the REAL Browser below), so inert
    stubs are enough to let the module load. A fresh module each call keeps a
    stub left by another recipe's test from bleeding in.
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
    mod.recipe_argv = lambda argv=None: []
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-md recipe with md_doc / md2ansi_lib LIVE.

    ``recipes/`` is put on ``sys.path`` so the recipe's hard
    ``from md2ansi_lib import ...`` / ``import md_doc`` resolve to the real
    plugins (the parser, ref resolution, and the ``m``-toggle gate all depend on
    them). A fresh module instance per call keeps module-level state
    (``_BY_ID`` / ``_FILES`` / ``_INPUT_FILES``) from bleeding between tests.
    """
    recipes_dir = str(_RECIPE.parent)
    added = recipes_dir not in sys.path
    if added:
        sys.path.insert(0, recipes_dir)
    try:
        _stub_browse_tui()
        name = '_browse_md_cm_under_test'
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
    procs / git / claude context-menu tests.
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
    """``context_menu_options`` returns the right rows for each browse-md kind.

    Each case parks a real cursor on one kind of row and asserts the
    kind-specific tokens plus the shared trailing "Toggle markdown coloring"
    row (always present here — md2ansi_lib is LIVE under the loader, so the
    ``m`` gate is satisfied).
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

    def test_file_root_menu_rows_and_hints(self):
        # A file root built the NORMAL way — title is the (relative) file label,
        # no synthetic ellipsis. "Show full path" is always present (the recipe
        # can't reliably detect render-time truncation, so it always offers the
        # pop-up). E / V / M and Ctrl-R rows carry their literal hotkey hints.
        item = Item(id=('file', '/proj/doc.md'), title='doc.md',
                    has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), [
            'file.edit', 'file.view', 'file.mdcat', 'file.opendir',
            'file.path', 'file.rescan', 'toggle_md',
        ])
        labels = dict((t, l) for l, t in rows)
        self.assertEqual(labels['file.edit'], 'Edit file in $EDITOR (E)')
        self.assertEqual(labels['file.view'], 'View file in $PAGER (V)')
        self.assertEqual(labels['file.mdcat'], 'Render with mdcat (M)')
        self.assertEqual(labels['file.rescan'], 'Re-scan all files (Ctrl-R)')
        self.assertIn('Show full path', _labels(rows))

    def test_show_full_path_always_present_on_normal_file_row(self):
        # A normally-built file row (no synthetic ellipsis in the title) still
        # offers Show-full-path — the always-on behaviour, not gated on a
        # title heuristic.
        item = Item(id=('file', '/a/b/c/deeply/nested/readme.md'),
                    title='deeply/nested/readme.md', has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertIn('file.path', _tokens(rows))
        self.assertNotIn('…', item.title)  # no synthetic ellipsis

    def test_heading_menu_rows_and_hints(self):
        # A heading row (kind == 'heading') gets the section actions + the
        # heading-anchor row + expand/collapse. E / V / M carry hints.
        item = Item(id=('content', '/proj/doc.md', 0), title='Intro',
                    tag='h1', has_children=True)
        item.kind = 'heading'
        self.r._BY_ID = {item.id: item}
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), [
            'content.edit', 'content.view', 'content.mdcat',
            'content.anchor', 'content.expand', 'content.collapse',
            'toggle_md',
        ])
        labels = dict((t, l) for l, t in rows)
        self.assertEqual(labels['content.edit'], 'Edit at this line (E)')
        self.assertEqual(labels['content.view'], 'View section in $PAGER (V)')
        self.assertEqual(labels['content.mdcat'], 'Render section via mdcat (M)')

    def test_list_item_menu_omits_heading_anchor(self):
        # A list-item row (kind == 'list-item') has no heading anchor, so the
        # Show-heading-anchor row is omitted; expand/collapse stay.
        item = Item(id=('content', '/proj/doc.md', 5), title='a bullet',
                    tag='ul', has_children=False)
        item.kind = 'list-item'
        self.r._BY_ID = {item.id: item}
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), [
            'content.edit', 'content.view', 'content.mdcat',
            'content.expand', 'content.collapse', 'toggle_md',
        ])
        self.assertNotIn('content.anchor', _tokens(rows))

    def test_cross_file_ref_menu_rows(self):
        # A cross-file reference node (a referenced-doc root) gets open / edit /
        # show-target. No E hint on Edit (E edits the PRIMARY file, not this one).
        item = Item(
            id=('md', ('file', '/proj/doc.md'), ('/proj/other.md',), None),
            title='other.md', tag='md', has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), [
            'ref.open', 'ref.edit', 'ref.target', 'toggle_md',
        ])
        labels = dict((t, l) for l, t in rows)
        self.assertEqual(labels['ref.open'], 'Open referenced doc in browse-md')
        self.assertEqual(labels['ref.edit'], 'Edit referenced file')
        self.assertEqual(labels['ref.target'], 'Show link target')

    def test_cross_file_ref_heading_node_also_gets_ref_menu(self):
        # A heading INSIDE a referenced file (line set) is still a cross-file
        # reference node — same ref menu, targeting the same referenced file.
        item = Item(
            id=('md', ('file', '/proj/doc.md'), ('/proj/other.md',), 3),
            title='Section', tag='h2', has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), [
            'ref.open', 'ref.edit', 'ref.target', 'toggle_md',
        ])

    def test_refs_umbrella_yields_only_toggle(self):
        # The References umbrella groups refs but is not itself a single ref —
        # it has no per-kind menu, only the shared toggle row.
        item = Item(id=('refs', ('file', '/proj/doc.md'), ()),
                    title='References', tag='links', has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertEqual(_tokens(rows), ['toggle_md'])

    def test_toggle_md_row_shows_m_hint(self):
        # The shared toggle row reuses the ``m`` action, so its label carries
        # the ``m`` hotkey hint (literal text, the menu convention).
        item = Item(id=('file', '/proj/doc.md'), title='doc.md',
                    has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        toggle_label = next(l for l, t in rows if t == 'toggle_md')
        self.assertEqual(toggle_label, 'Toggle markdown coloring (m)')

    def test_toggle_md_suppressed_when_md2ansi_absent(self):
        # The ``m`` keybinding is only bound when md2ansi_lib loaded; the menu's
        # toggle row mirrors that gate. With ``_md2ansi_fn`` None a file row
        # carries only its own (non-toggle) entries.
        self.r._md2ansi_fn = None
        item = Item(id=('file', '/proj/doc.md'), title='doc.md',
                    has_children=True)
        rows = self.r.context_menu_options(self._ctx(item))
        self.assertNotIn('toggle_md', _tokens(rows))
        self.assertEqual(_tokens(rows)[0], 'file.edit')

    def test_no_cursor_yields_only_toggle(self):
        # An empty tree → no cursor item → no per-kind rows; the shared toggle
        # row is still offered (md2ansi_lib live).
        empty = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            empty.refresh()
            empty.run_until_idle()
            ctx = Context(empty)
            self.assertIsNone(ctx.cursor)
            self.assertEqual(_tokens(self.r.context_menu_options(ctx)),
                             ['toggle_md'])
        finally:
            empty.stop_workers()

    def test_stale_non_tuple_id_yields_only_toggle(self):
        # A stale non-tuple / empty-tuple cursor id must not traceback on the
        # id[0] dispatch — it falls through to just the shared toggle row.
        for bad in ('stale-string', (), 42):
            item = Item(id=bad, title='x', has_children=False)
            ctx = self._ctx(item)
            self.assertEqual(_tokens(self.r.context_menu_options(ctx)),
                             ['toggle_md'])
            self.b.stop_workers()

    def test_no_clipboard_or_copy_entries(self):
        # Convention (#1028): recipe menus carry no clipboard / Copy rows.
        h = Item(id=('content', '/d.md', 0), title='H', tag='h1',
                 has_children=True)
        h.kind = 'heading'
        self.r._BY_ID = {h.id: h}
        cases = [
            Item(id=('file', '/d.md'), title='d.md', has_children=True),
            h,
            Item(id=('md', ('file', '/d.md'), ('/o.md',), None), title='o.md',
                 has_children=True),
        ]
        for item in cases:
            ctx = self._ctx(item)
            for label in _labels(self.r.context_menu_options(ctx)):
                self.assertNotIn('copy', label.lower())
                self.assertNotIn('clipboard', label.lower())
            self.b.stop_workers()


class TestSlug(unittest.TestCase):
    """``_slug`` derives a GitHub-style heading anchor from a title."""

    def setUp(self):
        self.r = _load_recipe()

    def test_basic_lowercase_and_hyphens(self):
        self.assertEqual(self.r._slug('My Heading'), 'my-heading')

    def test_strips_punctuation_and_emphasis_markers(self):
        # Inline emphasis markers + punctuation drop; words join with hyphens.
        self.assertEqual(self.r._slug('My **bold** Title!'), 'my-bold-title')
        self.assertEqual(self.r._slug('Setup & Config'), 'setup-config')

    def test_collapses_whitespace_runs(self):
        self.assertEqual(self.r._slug('  a   b  '), 'a-b')

    def test_preserves_existing_hyphens(self):
        self.assertEqual(self.r._slug('well-known thing'), 'well-known-thing')


class TestRefTarget(unittest.TestCase):
    """``_md_ref_target`` recovers the referenced file from a ``('md', …)`` id."""

    def setUp(self):
        self.r = _load_recipe()

    def test_doc_root_node(self):
        self.assertEqual(
            self.r._md_ref_target(
                ('md', ('file', '/a.md'), ('/x/b.md',), None)),
            '/x/b.md')

    def test_heading_node_in_referenced_file(self):
        self.assertEqual(
            self.r._md_ref_target(
                ('md', ('file', '/a.md'), ('/x/b.md',), 7)),
            '/x/b.md')

    def test_deep_chain_returns_last(self):
        # A ref-of-a-ref chain targets the LAST (deepest) file in the chain.
        self.assertEqual(
            self.r._md_ref_target(
                ('md', ('file', '/a.md'), ('/x/b.md', '/x/c.md'), None)),
            '/x/c.md')

    def test_empty_chain_or_bad_id_returns_none(self):
        # The primary file is never browsed as an ('md', …) node — an empty
        # chain (or any malformed id) yields None.
        self.assertIsNone(
            self.r._md_ref_target(('md', ('file', '/a.md'), (), None)))
        self.assertIsNone(self.r._md_ref_target(('file', '/a.md')))
        self.assertIsNone(self.r._md_ref_target('stale'))


class TestDispatchTable(unittest.TestCase):
    """Every token a builder can emit dispatches; no orphan handlers."""

    def setUp(self):
        self.r = _load_recipe()

    def _all_emitted_tokens(self):
        class Cur:
            def __init__(self, id):
                self.id = id
                self.title = 'row'

        class Ctx:
            def __init__(self, id):
                self._c = Cur(id)

            @property
            def cursor(self):
                return self._c

        # A heading id must be a known heading in _BY_ID so the anchor row is
        # emitted (the only conditional row).
        heading_id = ('content', '/d.md', 0)
        h = Item(id=heading_id, title='H', tag='h1', has_children=True)
        h.kind = 'heading'
        self.r._BY_ID = {heading_id: h}

        emitted = set()
        cases = [
            ('file', '/d.md'),
            heading_id,
            ('content', '/d.md', 5),                       # list item
            ('md', ('file', '/d.md'), ('/o.md',), None),   # ref doc
            ('md', ('file', '/d.md'), ('/o.md',), 3),      # ref heading
            ('refs', ('file', '/d.md'), ()),               # umbrella
        ]
        for cid in cases:
            for _label, tok in self.r.context_menu_options(Ctx(cid)):
                emitted.add(tok)
        return emitted

    def test_every_emitted_token_has_a_handler(self):
        for tok in self._all_emitted_tokens():
            self.assertIn(tok, self.r._MENU_ACTIONS,
                          f'token {tok!r} has no dispatch handler')

    def test_no_orphan_handlers(self):
        # Every handler in the table is reachable from some builder row.
        emitted = self._all_emitted_tokens()
        orphans = set(self.r._MENU_ACTIONS) - emitted
        self.assertEqual(orphans, set(),
                         f'handlers never emitted by a builder: {orphans}')


class TestDispatchReusesActions(unittest.TestCase):
    """The source tokens reuse the existing action handlers; pop-ups alert.

    A fake-but-recording ctx confirms each token routes to the right reused
    handler (E / V / M / m), that "Re-scan" reuses ``ctx.refresh``, that the
    expand / collapse tokens call the framework ops, and that the path / anchor /
    target pop-up tokens call ``ctx.alert``.
    """

    def setUp(self):
        self.r = _load_recipe()

    def test_file_source_tokens_reuse_handlers(self):
        calls = []
        self.r._action_edit_source = lambda c: calls.append('edit')
        self.r._action_view_source = lambda c: calls.append('view')
        self.r._action_md_preview = lambda c: calls.append('mdcat')
        ctx = object()
        fid = ('file', '/d.md')
        self.r._MENU_ACTIONS['file.edit'](ctx, fid)
        self.r._MENU_ACTIONS['file.view'](ctx, fid)
        self.r._MENU_ACTIONS['file.mdcat'](ctx, fid)
        self.assertEqual(calls, ['edit', 'view', 'mdcat'])

    def test_content_source_tokens_reuse_handlers(self):
        calls = []
        self.r._action_edit_source = lambda c: calls.append('edit')
        self.r._action_view_source = lambda c: calls.append('view')
        self.r._action_md_preview = lambda c: calls.append('mdcat')
        ctx = object()
        cid = ('content', '/d.md', 0)
        self.r._MENU_ACTIONS['content.edit'](ctx, cid)
        self.r._MENU_ACTIONS['content.view'](ctx, cid)
        self.r._MENU_ACTIONS['content.mdcat'](ctx, cid)
        self.assertEqual(calls, ['edit', 'view', 'mdcat'])

    def test_toggle_md_reuses_handler(self):
        calls = []
        self.r._action_toggle_md = lambda c: calls.append('toggle')
        self.r._MENU_ACTIONS['toggle_md'](object(), ('file', '/d.md'))
        self.assertEqual(calls, ['toggle'])

    def test_rescan_reuses_ctx_refresh(self):
        # "Re-scan all files" is exactly the Ctrl-R reload: ctx.refresh() (a
        # root refresh enqueues reload=True downstream).
        class Ctx:
            def __init__(self):
                self.refreshed = False

            def refresh(self):
                self.refreshed = True

        ctx = Ctx()
        self.r._MENU_ACTIONS['file.rescan'](ctx, ('file', '/d.md'))
        self.assertTrue(ctx.refreshed)

    def test_file_path_alerts_full_path(self):
        alerted = {}

        class Ctx:
            def alert(self, text, **kw):
                alerted['text'] = text
                alerted['title'] = kw.get('title')

        self.r._MENU_ACTIONS['file.path'](Ctx(), ('file', '/a/b/c/doc.md'))
        self.assertEqual(alerted['text'], '/a/b/c/doc.md')
        self.assertEqual(alerted['title'], 'path')

    def test_open_dir_launches_browse_fs_on_dirname(self):
        launched = {}

        class Ctx:
            def run_external(self, cmd, **kw):
                launched['cmd'] = cmd
                launched['kw'] = kw

        self.r._MENU_ACTIONS['file.opendir'](Ctx(), ('file', '/a/b/doc.md'))
        self.assertEqual(launched['cmd'], ['browse-fs', '/a/b'])
        self.assertTrue(launched['kw'].get('keep_screen'))

    def test_content_anchor_alerts_basename_slug(self):
        # The anchor pop-up is basename(path)#slug(title), derived from _BY_ID.
        cid = ('content', '/proj/sub/doc.md', 0)
        item = Item(id=cid, title='My Heading', tag='h1', has_children=True)
        item.kind = 'heading'
        self.r._BY_ID = {cid: item}
        alerted = {}

        class Ctx:
            def alert(self, text, **kw):
                alerted['text'] = text
                alerted['title'] = kw.get('title')

        self.r._MENU_ACTIONS['content.anchor'](Ctx(), cid)
        self.assertEqual(alerted['text'], 'doc.md#my-heading')
        self.assertEqual(alerted['title'], 'anchor')

    def test_content_expand_collapse_reuse_framework_ops(self):
        cid = ('content', '/d.md', 0)
        ops = []

        class Ctx:
            def expand_subtree(self, id, **kw):
                ops.append(('expand', id))

            def collapse(self, id):
                ops.append(('collapse', id))

        self.r._MENU_ACTIONS['content.expand'](Ctx(), cid)
        self.r._MENU_ACTIONS['content.collapse'](Ctx(), cid)
        self.assertEqual(ops, [('expand', cid), ('collapse', cid)])

    def test_ref_target_alerts_resolved_path(self):
        alerted = {}

        class Ctx:
            def alert(self, text, **kw):
                alerted['text'] = text
                alerted['title'] = kw.get('title')

        self.r._MENU_ACTIONS['ref.target'](
            Ctx(), ('md', ('file', '/d.md'), ('/x/other.md',), None))
        self.assertEqual(alerted['text'], '/x/other.md')
        self.assertEqual(alerted['title'], 'link target')

    def test_ref_open_launches_browse_md_on_target(self):
        launched = {}

        class Ctx:
            def run_external(self, cmd, **kw):
                launched['cmd'] = cmd
                launched['kw'] = kw

        self.r._MENU_ACTIONS['ref.open'](
            Ctx(), ('md', ('file', '/d.md'), ('/x/other.md',), None))
        self.assertEqual(launched['cmd'], ['browse-md', '/x/other.md'])
        self.assertTrue(launched['kw'].get('keep_screen'))

    def test_ref_edit_launches_editor_on_target(self):
        launched = {}

        class Ctx:
            def run_external(self, cmd, **kw):
                launched['cmd'] = cmd

        old = os.environ.get('EDITOR')
        os.environ['EDITOR'] = 'nano'
        try:
            self.r._MENU_ACTIONS['ref.edit'](
                Ctx(), ('md', ('file', '/d.md'), ('/x/other.md',), None))
        finally:
            if old is None:
                os.environ.pop('EDITOR', None)
            else:
                os.environ['EDITOR'] = old
        self.assertEqual(launched['cmd'], ['nano', '/x/other.md'])


class TestCrossFileRefRealFixture(unittest.TestCase):
    """The ref menu against a REAL parsed cross-file reference.

    A temp dir with ``main.md`` (which links to ``other.md``) is parsed through
    the recipe's genuine ``_reparse`` pipeline; expanding the file root's
    References umbrella yields a real ``('md', …)`` node whose ``chain``
    resolves to ``other.md`` — so the menu + ``_md_ref_target`` are exercised on
    an id produced by the actual ref-resolution path, not a hand-built tuple.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, self.tmp,
                        ignore_errors=True)
        self.other = os.path.join(self.tmp, 'other.md')
        with open(self.other, 'w') as f:
            f.write('# Other\n\nbody\n')
        self.main = os.path.join(self.tmp, 'main.md')
        with open(self.main, 'w') as f:
            f.write('# Main\n\nSee [other](other.md) for more.\n')
        # Drive the recipe's real parse pipeline over the two files.
        self.r._INPUT_FILES = [(self.main, '')]
        self.r._reparse()

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def test_umbrella_child_is_a_ref_node_resolving_to_other(self):
        # The file root references other.md → a References umbrella; its child
        # is the referenced-doc node whose target is other.md's abspath.
        kids = self.r.get_children(('file', self.main))
        umbrella = next(k for k in kids if k.id[0] == 'refs')
        ref_nodes = self.r.get_children(umbrella.id)
        self.assertEqual(len(ref_nodes), 1)
        ref = ref_nodes[0]
        self.assertEqual(ref.id[0], 'md')
        self.assertEqual(self.r._md_ref_target(ref.id), self.other)

    def test_ref_menu_on_real_node(self):
        # The real ref node gets the cross-file reference menu, and Show-link-
        # target would pop up other.md's resolved abspath. The recipe builds
        # its rows via the STUB ``browse_tui.Item`` (the recipe-side type),
        # which the real headless Browser can't store; so we park the cursor on
        # a real helper Item carrying the genuine ref node's id (the id is what
        # the menu + ``_md_ref_target`` read).
        kids = self.r.get_children(('file', self.main))
        umbrella = next(k for k in kids if k.id[0] == 'refs')
        ref = self.r.get_children(umbrella.id)[0]
        self.assertEqual(ref.id[0], 'md')  # the genuine ref-resolution id
        cursor = Item(id=ref.id, title='other.md', has_children=True)
        self.b = _browser_with_item(cursor)
        ctx = Context(self.b)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows),
                         ['ref.open', 'ref.edit', 'ref.target', 'toggle_md'])

        alerted = {}

        class RecCtx:
            def alert(self, text, **kw):
                alerted['text'] = text

        self.r._MENU_ACTIONS['ref.target'](RecCtx(), ref.id)
        self.assertEqual(alerted['text'], self.other)


if __name__ == '__main__':
    unittest.main()
