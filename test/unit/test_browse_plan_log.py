"""Unit tests for ``recipes/browse-plan``'s plan-CLI audit logging (#765).

browse-plan funnels every ``plan`` shell-out through ``_run_plan``; when a
module-level ``_log_sink`` is wired (``main`` points it at the thread-safe
``Browser.log``) that chokepoint records an audit trail for the framework
``~`` log viewer:

  * any non-zero return (incl. the timeout/exception fallback) — even a
    read — as ``[error] plan <argv>: <stderr>``;
  * otherwise only DB-mutating calls as ``plan <argv>`` (a call is a read,
    and logged nothing on success, iff its argv carries ``list`` / ``get``).

This ticket also drops browse-plan's own ``~`` binding (and its
``_action_command_log`` handler) so the recipe inherits the framework's
``~`` → ``_view_log`` pager.

The recipe is a single-file ``--run-py`` script. The logging tests stub
``browse_tui`` (the recipe only needs its ``Action`` / ``Item`` symbols at
import time) and drive ``_run_plan`` with a monkeypatched ``subprocess.run``
so no real ``plan`` binary is required. The keymap test instead loads the
*real* framework — the generated ``browse-tui`` binary, which is exactly the
module ``--run-py`` injects as ``browse_tui`` — so ``build_keymap`` resolves
``~`` against the genuine default actions.
"""

import importlib.util
import subprocess
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-plan'
_BIN = _REPO / 'browse-tui'


def _stub_browse_tui():
    """Insert a minimal ``browse_tui`` stub so the recipe imports.

    The logging tests touch only ``_run_plan`` / ``_is_read``, which don't
    use any framework class — the stub just satisfies the module-level
    ``from browse_tui import ...``. Always reinstalled fresh so a stub left
    by another recipe's unit test doesn't bleed in.
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
    mod.upsert = lambda *a, **kw: (a, kw)
    sys.modules['browse_tui'] = mod


def _load_recipe(name):
    """Load a fresh copy of the browse-plan recipe under module ``name``.

    A fresh module per call keeps the module-level ``_log_sink`` (mutated
    by these tests) isolated. ``recipes/`` is put on ``sys.path`` so the
    recipe's optional ``from md2ansi_lib import ...`` resolves just as
    ``--run-py`` arranges at runtime.
    """
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _completed(returncode, *, stdout='', stderr=''):
    """A ``subprocess.run`` stand-in returning a fixed ``CompletedProcess``."""
    def _run(argv, *a, **kw):
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr,
        )
    return _run


class _FakeCtx:
    """A ``Context`` stand-in for driving ``_action_create`` / ``_action_edit``.

    ``run_external`` returns the canned ``rc`` (the editor exit code) and is a
    leaf-level stub — these handlers shell out to ``$EDITOR`` rather than
    going through ``_run_plan``, so no ``subprocess`` patching is needed. The
    captured ``errors`` / ``refreshes`` let a test confirm the failure branch
    routes through ``ctx.error`` (which logs ``[error] …`` in production, not
    a ``plan …`` mutation row) and that ``refresh`` still fires either way.
    ``insert`` records the ``on_confirm`` callback so a test can invoke it
    directly, mirroring how the real insert-mode dispatch calls it.
    """

    def __init__(self, cursor_id, rc):
        self.cursor = types.SimpleNamespace(id=cursor_id)
        self._rc = rc
        self.errors = []
        self.refreshes = 0
        self.on_confirm = None

    def run_external(self, cmd, env=None):
        return self._rc

    def error(self, text):
        self.errors.append(text)

    def refresh(self, *a, **kw):
        self.refreshes += 1

    def insert(self, label, on_confirm):
        self.on_confirm = on_confirm


class TestRunPlanLogging(unittest.TestCase):
    """``_run_plan`` records the right audit lines through ``_log_sink``."""

    def setUp(self):
        self._saved = sys.modules.get('browse_tui')
        _stub_browse_tui()
        self.bp = _load_recipe('_browse_plan_log_under_test')
        self.captured = []
        self.bp._log_sink = self.captured.append
        # Pin the binary so the logged ``<argv>`` never carries a ``-f``
        # prefix from the ambient environment.
        self.bp._PLAN_FILE = None

    def tearDown(self):
        if self._saved is not None:
            sys.modules['browse_tui'] = self._saved
        else:
            sys.modules.pop('browse_tui', None)

    def test_mutation_success_logs_argv(self):
        """A DB-mutating call that succeeds logs ``plan <argv>``."""
        with mock.patch.object(self.bp.subprocess, 'run', _completed(0)):
            result = self.bp._run_plan('5', 'status', 'done')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.captured, ['plan 5 status done'])

    def test_other_mutations_log(self):
        """create / close / move / comment / edit all count as mutations."""
        calls = [
            (('create', 'title="x"'), 'plan create title="x"'),
            (('5', 'close'), 'plan 5 close'),
            (('5', 'move', 'after', '6'), 'plan 5 move after 6'),
            (('5', 'comment', 'add', 'hi'), 'plan 5 comment add hi'),
            (('5', 'reopen'), 'plan 5 reopen'),
        ]
        for args, expected in calls:
            self.captured.clear()
            with self.subTest(args=args):
                with mock.patch.object(self.bp.subprocess, 'run', _completed(0)):
                    self.bp._run_plan(*args)
                self.assertEqual(self.captured, [expected])

    def test_failing_call_logs_error_even_for_read(self):
        """Non-zero return logs ``[error] plan <argv>: <stderr>`` — incl. reads."""
        with mock.patch.object(
            self.bp.subprocess, 'run', _completed(1, stderr='no such ticket\n')
        ):
            result = self.bp._run_plan('5', 'get')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.captured, ['[error] plan 5 get: no such ticket'])

    def test_failing_mutation_logs_error_not_plain(self):
        """A failed mutation logs only the ``[error]`` line (not a plain row)."""
        with mock.patch.object(
            self.bp.subprocess, 'run', _completed(2, stderr='bad status')
        ):
            self.bp._run_plan('5', 'status', 'frobnicate')
        self.assertEqual(self.captured, ['[error] plan 5 status frobnicate: bad status'])

    def test_successful_reads_log_nothing(self):
        """``list`` / ``-r list`` / ``get`` / ``project get`` (success) log nothing."""
        reads = [
            ('list', '--format', 'X'),
            ('5', '-r', 'list', '--format', 'X'),
            ('5', 'get'),
            ('project', 'get'),
        ]
        with mock.patch.object(self.bp.subprocess, 'run', _completed(0, stdout='ok')):
            for args in reads:
                with self.subTest(args=args):
                    self.bp._run_plan(*args)
        self.assertEqual(self.captured, [])

    def test_is_read_heuristic(self):
        """``_is_read`` recognises exactly the ``list`` / ``get`` verbs."""
        self.assertTrue(self.bp._is_read(('list', '--format', 'X')))
        self.assertTrue(self.bp._is_read(('5', '-r', 'list', '--format', 'X')))
        self.assertTrue(self.bp._is_read(('5', 'get')))
        self.assertTrue(self.bp._is_read(('project', 'get')))
        self.assertFalse(self.bp._is_read(('5', 'status', 'done')))
        self.assertFalse(self.bp._is_read(('create', 'title="x"')))
        self.assertFalse(self.bp._is_read(('5', 'close')))

    def test_timeout_fallback_logs_error(self):
        """The exception fallback (e.g. timeout) is a non-zero return → ``[error]``."""
        def _boom(argv, *a, **kw):
            raise subprocess.TimeoutExpired(cmd='plan', timeout=30)

        with mock.patch.object(self.bp.subprocess, 'run', _boom):
            result = self.bp._run_plan('list')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.captured), 1)
        line = self.captured[0]
        self.assertTrue(line.startswith('[error] plan list: '), line)
        self.assertIn('timed out', line)

    def test_no_sink_no_crash(self):
        """With ``_log_sink`` unset (headless helpers) nothing is logged."""
        self.bp._log_sink = None
        with mock.patch.object(self.bp.subprocess, 'run', _completed(0)):
            result = self.bp._run_plan('5', 'status', 'done')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.captured, [])


class TestCreateEditLogging(unittest.TestCase):
    """``_action_create`` / ``_action_edit`` audit their successful mutations.

    These handlers shell out to ``$EDITOR`` via ``ctx.run_external`` (not
    ``_run_plan``), so they log the mutation themselves on a zero exit — in a
    concise human-readable form (``create -r``, ``edit 5``, ``edit project``),
    never the noisy ``-e`` / ``title=…, move=…`` argv. A non-zero exit takes
    the ``ctx.error`` branch and emits no ``plan …`` row from this code.
    """

    def setUp(self):
        self._saved = sys.modules.get('browse_tui')
        _stub_browse_tui()
        self.bp = _load_recipe('_browse_plan_createedit_under_test')
        self.captured = []
        self.bp._log_sink = self.captured.append
        # Pin the binary so a logged line never carries a ``-f`` prefix.
        self.bp._PLAN_FILE = None

    def tearDown(self):
        if self._saved is not None:
            sys.modules['browse_tui'] = self._saved
        else:
            sys.modules.pop('browse_tui', None)

    def _run_create(self, *, recursive, rc):
        """Drive ``_action_create``, then fire the captured ``on_confirm``."""
        ctx = _FakeCtx(cursor_id=0, rc=rc)
        self.bp._action_create(ctx, recursive)
        self.assertIsNotNone(ctx.on_confirm, 'handler should call ctx.insert')
        ctx.on_confirm('after', 5)
        return ctx

    def test_create_success_logs_concise(self):
        """A successful ``create`` logs ``plan create`` (no ``-e`` / expr)."""
        self._run_create(recursive=False, rc=0)
        self.assertEqual(self.captured, ['plan create'])

    def test_create_recursive_success_logs_concise(self):
        """A successful ``create -r`` logs ``plan create -r``."""
        self._run_create(recursive=True, rc=0)
        self.assertEqual(self.captured, ['plan create -r'])

    def test_create_failure_logs_no_mutation_row(self):
        """A failed ``create`` routes to ``ctx.error`` and logs no ``plan`` row."""
        ctx = self._run_create(recursive=False, rc=1)
        self.assertEqual(self.captured, [])
        self.assertEqual(len(ctx.errors), 1)
        self.assertIn('create exited 1', ctx.errors[0])

    def test_edit_success_logs_id(self):
        """A successful ``edit`` on a real ticket logs ``plan edit <id>``."""
        ctx = _FakeCtx(cursor_id=5, rc=0)
        self.bp._action_edit(ctx, False)
        self.assertEqual(self.captured, ['plan edit 5'])

    def test_edit_recursive_success_logs_id(self):
        """A successful ``edit -r`` logs ``plan edit -r <id>``."""
        ctx = _FakeCtx(cursor_id=5, rc=0)
        self.bp._action_edit(ctx, True)
        self.assertEqual(self.captured, ['plan edit -r 5'])

    def test_edit_project_success_logs_project(self):
        """Editing the root (cursor id 0) logs ``plan edit project``."""
        ctx = _FakeCtx(cursor_id=0, rc=0)
        self.bp._action_edit(ctx, False)
        self.assertEqual(self.captured, ['plan edit project'])

    def test_edit_failure_logs_no_mutation_row(self):
        """A failed ``edit`` routes to ``ctx.error`` and logs no ``plan`` row."""
        ctx = _FakeCtx(cursor_id=5, rc=2)
        self.bp._action_edit(ctx, False)
        self.assertEqual(self.captured, [])
        self.assertEqual(len(ctx.errors), 1)
        self.assertIn('edit exited 2', ctx.errors[0])

    def test_no_sink_no_crash(self):
        """With ``_log_sink`` unset (headless helpers) success logs nothing."""
        self.bp._log_sink = None
        edit_ctx = _FakeCtx(cursor_id=5, rc=0)
        self.bp._action_edit(edit_ctx, False)
        create_ctx = _FakeCtx(cursor_id=0, rc=0)
        self.bp._action_create(create_ctx, False)
        create_ctx.on_confirm('after', 5)
        self.assertEqual(self.captured, [])
        # The handlers still ran to completion (refresh fired).
        self.assertEqual(edit_ctx.refreshes, 1)
        self.assertEqual(create_ctx.refreshes, 1)


class TestTildeBinding(unittest.TestCase):
    """browse-plan drops its own ``~`` so it inherits the framework viewer."""

    @classmethod
    def setUpClass(cls):
        if not _BIN.exists():
            raise unittest.SkipTest(
                'generated browse-tui binary missing; run ./build-tui.sh'
            )

    def setUp(self):
        # Load the real framework: the generated binary IS the module that
        # ``--run-py`` injects as ``browse_tui``. Loading it under the name
        # ``browse_tui`` means the recipe builds against genuine
        # ``Action`` / ``Browser`` / ``build_keymap`` / ``_view_log``.
        self._saved = sys.modules.get('browse_tui')
        loader = SourceFileLoader('browse_tui', str(_BIN))
        spec = importlib.util.spec_from_loader('browse_tui', loader)
        self.bt = importlib.util.module_from_spec(spec)
        loader.exec_module(self.bt)
        sys.modules['browse_tui'] = self.bt
        self.bp = _load_recipe('_browse_plan_keymap_under_test')

    def tearDown(self):
        if self._saved is not None:
            sys.modules['browse_tui'] = self._saved
        else:
            sys.modules.pop('browse_tui', None)

    def _build_browser(self):
        """Run the recipe's ``main`` up to (not through) ``Browser.run``.

        Patches ``Browser.run`` to a no-op so the TUI never starts; the
        recipe stashes the constructed Browser in its ``_BROWSER`` global
        right before calling ``run``, which is what we inspect.
        """
        orig_run = self.bt.Browser.run
        self.bt.Browser.run = lambda self: 0
        saved_argv = sys.argv
        sys.argv = ['browse-plan']
        try:
            try:
                self.bp.main()
            except SystemExit:
                pass
        finally:
            self.bt.Browser.run = orig_run
            sys.argv = saved_argv
        return self.bp._BROWSER

    def test_tilde_resolves_to_framework_view_log(self):
        """``~`` in the built keymap is the framework ``_view_log`` handler."""
        browser = self._build_browser()
        keymap = self.bt.build_keymap(browser)
        self.assertIn('~', keymap)
        self.assertIs(keymap['~'].handler, self.bt._view_log)

    def test_recipe_does_not_bind_tilde(self):
        """The recipe contributes no ``~`` action of its own."""
        browser = self._build_browser()
        recipe_keys = [a.key for a in browser.actions]
        self.assertNotIn('~', recipe_keys)

    def test_action_command_log_removed(self):
        """The old ``_action_command_log`` handler no longer exists."""
        self.assertFalse(hasattr(self.bp, '_action_command_log'))

    def test_log_sink_wired_to_browser_log(self):
        """``main`` points ``_log_sink`` at the Browser's thread-safe ``log``."""
        browser = self._build_browser()
        self.assertEqual(self.bp._log_sink, browser.log)


if __name__ == '__main__':
    unittest.main()
