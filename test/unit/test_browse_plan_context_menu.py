"""Unit tests for the ``recipes/browse-plan`` context menu (ticket #1032).

A browse-plan row is a ticket (a bare ``int`` id); the synthetic Project sits
at id=0 and gets no ticket menu. Following
the committed convention (browse-ps pilot, browse-git / browse-claude) the
option list is a PURE builder, ``context_menu_options(ctx)``, that inspects
``ctx.cursor`` / ``ctx.selected`` and returns ``(label, token)`` rows WITHOUT
opening a modal; a flat ``{token: handler}`` table (``_MENU_ACTIONS``)
dispatches the chosen token, and the per-row actions REUSE the recipe's
existing action handlers.

We exercise the builder against a REAL headless ``Browser`` / ``Context``
(from ``test.async_._helpers``) with a known ticket under the cursor — not a
fake ctx. browse-tui swallows ``on_context_menu`` exceptions and a fake ctx
hides bugs, so the real ``Context.cursor`` / ``Context.selected`` read paths
are what we assert against; ``ctx.menu`` itself short-circuits to ``None`` in
headless mode, which is exactly why the builder is split out and tested
directly. A two-ticket selection exercises the "Move under selected" gate.

SAFETY: every handler test that would shell out to ``plan`` stubs the recipe's
``_run_plan`` so NO real ``plan`` runs and the repo/worktree ``.PLAN.md`` is
NEVER touched (we assert the argv the handler WOULD pass). The recipe is a
``--run-py`` script importing ``browse_tui``, so we stub ``browse_tui`` in
``sys.modules`` and load the extension-less recipe via ``SourceFileLoader`` —
the same pattern as ``test/unit/test_browse_git_context_menu.py``.
"""

import importlib.util
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from test.async_._helpers import Context, Item, make_browser


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-plan'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    The recipe pulls ``Action`` / ``Browser`` / ``BrowserConfig`` / ``Item`` /
    ``recipe_argv`` / ``upsert`` from ``browse_tui``. The pure builders under
    test don't exercise the Browser stub (the cursor item comes from the REAL
    headless Browser below); ``upsert`` is only used by the mutation handlers,
    which the tests drive with a stubbed ``_run_plan``, so a simple sentinel is
    enough. A fresh module each call keeps a stub left by another recipe's test
    from bleeding in.
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
    mod.recipe_argv = lambda: []
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-plan recipe; returns a fresh module."""
    _stub_browse_tui()
    name = '_browse_plan_cm_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _browser_with_item(item, *, extra=()):
    """A real headless Browser whose cursor sits on ``item``.

    ``extra`` are sibling rows listed alongside ``item`` (so a multi-row
    selection can be built). The cursor is parked on ``item`` via ``cursor_to``
    after the root children settle, mirroring the procs / git tests.
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


# The full token list a ticket row emits with NO selection (no move.under). The
# order is load-bearing — it mirrors the keybinding layout the menu surfaces.
_BASE_TOKENS = [
    'status', 'assignee', 'comment', 'close', 'reopen', 'wontfix',
    'edit', 'edit.sub', 'view', 'view.sub', 'child', 'children', 'move',
]


class TestMenuRows(unittest.TestCase):
    """``context_menu_options`` returns the ticket rows + the hotkey hints."""

    def setUp(self):
        self.r = _load_recipe()
        # Default keybinding layout (md2ansi absent): Move is on ``m``.
        self.r._md2ansi_fn = None

    def _ctx(self, item, extra=()):
        self.b = _browser_with_item(item, extra=extra)
        return Context(self.b)

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def test_ticket_menu_tokens_in_order(self):
        ctx = self._ctx(Item(id=5, title='A ticket', tag='open'))
        rows = self.r.context_menu_options(ctx)
        # No selection → the base set, no "Move under selected".
        self.assertEqual(_tokens(rows), _BASE_TOKENS)

    def test_hotkey_hints_present(self):
        # Every row that duplicates a keybinding carries the LITERAL hint in
        # parens (menus don't parse ``&``). Move is ``m`` here (md2ansi absent).
        ctx = self._ctx(Item(id=5, title='A ticket', tag='open'))
        by_tok = {t: l for l, t in self.r.context_menu_options(ctx)}
        self.assertEqual(by_tok['status'], 'Change status… (s)')
        self.assertEqual(by_tok['close'], 'Close (x)')
        self.assertEqual(by_tok['reopen'], 'Reopen (o)')
        self.assertEqual(by_tok['edit'], 'Edit body (e)')
        self.assertEqual(by_tok['edit.sub'], 'Edit subtree (E)')
        self.assertEqual(by_tok['view'], 'View (v)')
        self.assertEqual(by_tok['view.sub'], 'View subtree (V)')
        self.assertEqual(by_tok['child'], 'Add child… (c)')
        self.assertEqual(by_tok['children'], 'Add children… (C)')
        self.assertEqual(by_tok['move'], 'Move… (m)')

    def test_set_assignee_and_comment_have_no_hotkey_hint(self):
        # Set assignee / Add comment / Close as wontfix are menu-only — no
        # keybinding, so no parenthesised hint.
        ctx = self._ctx(Item(id=5, title='A ticket', tag='open'))
        by_tok = {t: l for l, t in self.r.context_menu_options(ctx)}
        self.assertEqual(by_tok['assignee'], 'Set assignee…')
        self.assertEqual(by_tok['comment'], 'Add comment…')
        self.assertEqual(by_tok['wontfix'], 'Close as wontfix')

    def test_move_hint_is_M_when_md2ansi_available(self):
        # When md2ansi_lib is importable the recipe rebinds ``m`` to the
        # markdown-coloring toggle and Move shifts to ``M`` — the menu hint
        # must track that gate (``_move_hotkey``).
        self.r._md2ansi_fn = lambda *a, **kw: ''
        ctx = self._ctx(Item(id=5, title='A ticket', tag='open'))
        by_tok = {t: l for l, t in self.r.context_menu_options(ctx)}
        self.assertEqual(by_tok['move'], 'Move… (M)')

    def test_no_menu_for_project_row(self):
        # The synthetic Project (id=0) is not a ticket → no menu.
        ctx = self._ctx(Item(id=0, title='Project', tag=''))
        self.assertEqual(self.r.context_menu_options(ctx), [])

    def test_no_menu_for_non_ticket_id(self):
        # Defensive: only real int ticket ids get the ticket menu; a non-int
        # id (anything that isn't a ticket row) yields no menu.
        ctx = self._ctx(Item(id=('not', 'a', 'ticket'), title='x'))
        self.assertEqual(self.r.context_menu_options(ctx), [])

    def test_no_menu_without_cursor(self):
        empty = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            empty.refresh()
            empty.run_until_idle()
            ctx = Context(empty)
            self.assertIsNone(ctx.cursor)
            self.assertEqual(self.r.context_menu_options(ctx), [])
        finally:
            empty.stop_workers()

    def test_no_clipboard_entries(self):
        # Convention (#1028): recipe menus carry no clipboard / Copy rows.
        ctx = self._ctx(Item(id=5, title='A ticket', tag='open'))
        for label in _labels(self.r.context_menu_options(ctx)):
            self.assertNotIn('copy', label.lower())
            self.assertNotIn('clipboard', label.lower())


class TestMoveUnderSelectedGate(unittest.TestCase):
    """"Move under selected" appears iff exactly one OTHER ticket is selected."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._md2ansi_fn = None

    def tearDown(self):
        b = getattr(self, 'b', None)
        if b is not None:
            b.stop_workers()

    def test_absent_with_no_selection(self):
        self.b = _browser_with_item(Item(id=5, title='A', tag='open'))
        ctx = Context(self.b)
        self.assertIsNone(self.r._one_other_ticket(ctx))
        self.assertNotIn('move.under',
                         _tokens(self.r.context_menu_options(ctx)))

    def test_present_with_one_other_selected(self):
        # Cursor on 5, ticket 7 ALSO selected → the gate resolves to 7 and the
        # row appears (last, after the base set).
        self.b = _browser_with_item(
            Item(id=5, title='A', tag='open'),
            extra=(Item(id=7, title='B', tag='open'),))
        self.b.select([7])
        self.b.run_until_idle()
        ctx = Context(self.b)
        self.assertEqual([it.id for it in ctx.selected], [7])
        self.assertEqual(self.r._one_other_ticket(ctx), 7)
        rows = self.r.context_menu_options(ctx)
        self.assertEqual(_tokens(rows), _BASE_TOKENS + ['move.under'])
        by_tok = {t: l for l, t in rows}
        self.assertEqual(by_tok['move.under'], 'Move under selected')

    def test_absent_when_cursor_itself_is_the_only_selection(self):
        # Selecting only the cursor ticket is NOT "one other" → no row.
        self.b = _browser_with_item(Item(id=5, title='A', tag='open'))
        self.b.select([5])
        self.b.run_until_idle()
        ctx = Context(self.b)
        self.assertIsNone(self.r._one_other_ticket(ctx))
        self.assertNotIn('move.under',
                         _tokens(self.r.context_menu_options(ctx)))

    def test_absent_with_two_others_selected(self):
        # Two OTHER tickets selected is ambiguous → gate None → no row.
        self.b = _browser_with_item(
            Item(id=5, title='A', tag='open'),
            extra=(Item(id=7, title='B', tag='open'),
                   Item(id=9, title='C', tag='open')))
        self.b.select([7, 9])
        self.b.run_until_idle()
        ctx = Context(self.b)
        self.assertIsNone(self.r._one_other_ticket(ctx))
        self.assertNotIn('move.under',
                         _tokens(self.r.context_menu_options(ctx)))

    def test_project_excluded_from_other_count(self):
        # The synthetic Project (id=0) selected alongside one ticket does NOT
        # count as an "other" — so 0 + 7 still resolves to 7.
        self.b = _browser_with_item(
            Item(id=5, title='A', tag='open'),
            extra=(Item(id=0, title='Project', tag=''),
                   Item(id=7, title='B', tag='open')))
        self.b.select([0, 7])
        self.b.run_until_idle()
        ctx = Context(self.b)
        self.assertEqual(self.r._one_other_ticket(ctx), 7)


class TestDispatchTable(unittest.TestCase):
    """Every token a builder can emit dispatches; no orphan handlers."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._md2ansi_fn = None

    def _all_emitted_tokens(self):
        # Drive the builder with a recording ctx forcing the gate ON so the
        # full row set (including move.under) is emitted without a real Browser.
        class Cur:
            def __init__(self, id):
                self.id = id

        class Ctx:
            def __init__(self, cur, selected):
                self._cur, self._sel = cur, selected

            @property
            def cursor(self):
                return self._cur

            @property
            def selected(self):
                return self._sel

        cur = Cur(5)
        ctx = Ctx(cur, [Cur(7)])  # one other selected → move.under present
        return {tok for _l, tok in self.r.context_menu_options(ctx)}

    def test_every_emitted_token_has_a_handler(self):
        for tok in self._all_emitted_tokens():
            self.assertIn(tok, self.r._MENU_ACTIONS,
                          f'token {tok!r} has no dispatch handler')

    def test_no_orphan_handlers(self):
        emitted = self._all_emitted_tokens()
        orphans = set(self.r._MENU_ACTIONS) - emitted
        self.assertEqual(orphans, set(),
                         f'handlers never emitted by a builder: {orphans}')

    def test_on_context_menu_noop_on_cancel(self):
        # ctx.menu returns None (headless / cancel) → no handler fires.
        ran = []

        class Ctx:
            cursor = None

            def menu(self, items, **kw):
                return None

        # Replace the table with sentinels to prove none is called.
        self.r._MENU_ACTIONS = {t: (lambda c: ran.append(t))
                                for t in _BASE_TOKENS}
        self.r.on_context_menu(Ctx())
        self.assertEqual(ran, [])


class _RecCtx:
    """A recording ctx: captures confirm/input/flash/error and never opens UI."""

    def __init__(self, *, cursor_id=5, targets_ids=(5,),
                 confirm_answer=None, input_answer=None):
        self._cursor_id = cursor_id
        self._targets_ids = list(targets_ids)
        self.confirm_answer = confirm_answer
        self.input_answer = input_answer
        self.confirmed = None
        self.confirm_buttons = None
        self.input_prompt = None
        self.flashed = []
        self.errored = []
        self.refreshed = False

    class _Item:
        def __init__(self, id):
            self.id = id

    @property
    def cursor(self):
        return self._Item(self._cursor_id)

    @property
    def targets(self):
        return [self._Item(i) for i in self._targets_ids]

    @property
    def selected(self):
        return [self._Item(i) for i in self._targets_ids]

    def confirm(self, message, buttons=None, **kw):
        self.confirmed = message
        self.confirm_buttons = list(buttons) if buttons else None
        return self.confirm_answer

    def input(self, prompt, default='', **kw):
        self.input_prompt = prompt
        return self.input_answer

    def flash(self, *a, **kw):
        self.flashed.append(a[0] if a else '')

    def error(self, *a, **kw):
        self.errored.append(a[0] if a else '')

    def refresh(self, *a, **kw):
        self.refreshed = True

    def update_data(self, *a, **kw):
        pass


class _CP:
    """A successful CompletedProcess stand-in for the stubbed ``_run_plan``."""

    returncode = 0
    stdout = ''
    stderr = ''


class TestHandlersIsolated(unittest.TestCase):
    """The new mutation handlers — argv + gating — with ``_run_plan`` STUBBED.

    No real ``plan`` runs (the recipe's ``_run_plan`` is replaced with a
    recorder), so the repo / worktree ``.PLAN.md`` is never touched. We assert
    the exact argv each handler WOULD pass and that destructive ops gate on the
    confirm.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.ran = []
        self.r._run_plan = lambda *a, **kw: self.ran.append(a) or _CP()

    # -- close as wontfix (destructive → confirm) ---------------------------

    def test_wontfix_skips_plan_on_cancel(self):
        ctx = _RecCtx(targets_ids=(5,), confirm_answer=None)  # cancel
        self.r._action_close_wontfix(ctx)
        self.assertEqual(self.ran, [])
        self.assertIn('wontfix', ctx.confirmed.lower())

    def test_wontfix_skips_plan_on_no(self):
        ctx = _RecCtx(targets_ids=(5,), confirm_answer=False)  # No
        self.r._action_close_wontfix(ctx)
        self.assertEqual(self.ran, [])

    def test_wontfix_runs_close_wontfix_on_yes(self):
        ctx = _RecCtx(targets_ids=(5,), confirm_answer=True)
        self.r._action_close_wontfix(ctx)
        self.assertEqual(self.ran, [('5', 'close', 'wontfix')])
        # value-mapped Yes/No buttons (so the result is used directly).
        self.assertEqual([v for _l, v in ctx.confirm_buttons], [True, False])

    def test_wontfix_multi_target(self):
        ctx = _RecCtx(targets_ids=(5, 7), confirm_answer=True)
        self.r._action_close_wontfix(ctx)
        self.assertEqual(self.ran, [('5', '7', 'close', 'wontfix')])

    # -- set assignee (input → mod set) -------------------------------------

    def test_set_assignee_runs_mod_set(self):
        ctx = _RecCtx(targets_ids=(5,), input_answer='alice')
        self.r._action_set_assignee(ctx)
        self.assertEqual(self.ran, [('5', 'mod', 'set(assignee="alice")')])
        self.assertTrue(ctx.refreshed)

    def test_set_assignee_cancel_is_noop(self):
        ctx = _RecCtx(targets_ids=(5,), input_answer=None)  # esc
        self.r._action_set_assignee(ctx)
        self.assertEqual(self.ran, [])

    def test_set_assignee_blank_clears(self):
        # Blank name CLEARS the assignee (set(assignee="")), not a no-op.
        ctx = _RecCtx(targets_ids=(5,), input_answer='   ')
        self.r._action_set_assignee(ctx)
        self.assertEqual(self.ran, [('5', 'mod', 'set(assignee="")')])

    # -- add comment (input → comment add) ----------------------------------

    def test_add_comment_runs_comment_add(self):
        ctx = _RecCtx(targets_ids=(5,), input_answer='needs review')
        self.r._action_add_comment(ctx)
        self.assertEqual(self.ran, [('5', 'comment', 'add', 'needs review')])
        self.assertTrue(ctx.refreshed)

    def test_add_comment_blank_is_noop(self):
        for ans in (None, '', '   '):
            self.ran.clear()
            ctx = _RecCtx(targets_ids=(5,), input_answer=ans)
            self.r._action_add_comment(ctx)
            self.assertEqual(self.ran, [], f'blank/{ans!r} should not run plan')

    # -- move under selected (reparent as last child) -----------------------

    def test_move_under_selected_runs_move_last(self):
        # Cursor 5, single other selected 7 → plan 5 move last 7 (reparent).
        ctx = _RecCtx(cursor_id=5, targets_ids=(5, 7))
        self.r._action_move_under_selected(ctx)
        self.assertEqual(self.ran, [('5', 'move', 'last', '7')])
        self.assertTrue(ctx.refreshed)

    def test_move_under_selected_noop_when_gate_fails(self):
        # Selection no longer "one other" (cursor only) → flash, no plan run.
        ctx = _RecCtx(cursor_id=5, targets_ids=(5,))
        self.r._action_move_under_selected(ctx)
        self.assertEqual(self.ran, [])
        self.assertTrue(ctx.flashed)


if __name__ == '__main__':
    unittest.main()
