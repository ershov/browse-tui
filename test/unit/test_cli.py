"""Unit tests for ``080-cli.py``: argparser, install/uninstall, recipe runners.

These exercise the CLI surface added in ticket #12 plus the recipe
runners (``--run`` / ``--run-py`` / ``--run-cli`` and the bare-positional
shorthand). The argparse layer is trivial; the real load-bearing parts
are the env-var assembly, the install dry-run, the recipe-mode dispatch
in ``parse_args``, and the ``cmd_run_py`` self-injection contract.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from test.unit._loader import load


_data = load('_browse_tui_data', '030-data.py')
_term = load('_browse_tui_term', '020-terminal.py')
_state = load('_browse_tui_state', '040-state.py')
_actions = load('_browse_tui_actions', '070-actions.py')
_cli = load('_browse_tui_cli', '080-cli.py')

# Cross-module name wiring (concatenated builds resolve these naturally).
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.expand_path_rows = _data.expand_path_rows
_state.notify_wake = _term.notify_wake
_cli.Action = _actions.Action
_cli.Browser = _state.Browser
_cli.BrowserConfig = _state.BrowserConfig
_cli.Item = _data.Item
# Headless tests don't have a real terminal; stub the suspend/resume hooks.
_cli.term_suspend = lambda: None
_cli.term_resume = lambda: None


class TestArgParser(unittest.TestCase):
    """Argparse defaults, repeated -a, install-vs-children orthogonality."""

    def test_defaults(self):
        args, extras = _cli.parse_args([])
        self.assertEqual(args.input, 'tsv')
        self.assertEqual(args.fields, 'id,title')
        self.assertEqual(args.record_sep, 'nl')
        self.assertEqual(args.on_enter, 'print-exit')
        self.assertEqual(args.print_format, '{id}')
        # Tri-state default: None means "auto" — the Browser resolves
        # the actual visibility based on get_preview presence.
        self.assertIsNone(args.preview)
        self.assertEqual(args.action, [])
        self.assertEqual(extras, [])

    def test_children_cmd(self):
        args, _ = _cli.parse_args(['--children-cmd', 'ls $TUI_ID'])
        self.assertEqual(args.children_cmd, 'ls $TUI_ID')

    def test_repeated_action(self):
        args, _ = _cli.parse_args([
            '-a', 'k:label:cmd',
            '-a', 'l:other:other',
        ])
        self.assertEqual(args.action, ['k:label:cmd', 'l:other:other'])

    def test_no_preview_flag(self):
        args, _ = _cli.parse_args(['--no-preview'])
        self.assertFalse(args.preview)

    def test_preview_flag(self):
        # Positive form: --preview overrides the auto rule and forces
        # the pane visible regardless of get_preview presence.
        args, _ = _cli.parse_args(['--preview'])
        self.assertTrue(args.preview)

    def test_preview_ansi_default_true(self):
        # #245: default is ANSI-on so existing behaviour is preserved.
        args, _ = _cli.parse_args([])
        self.assertTrue(args.preview_ansi)

    def test_no_preview_ansi_flag_sets_false(self):
        args, _ = _cli.parse_args(['--no-preview-ansi'])
        self.assertFalse(args.preview_ansi)

    def test_preview_ansi_flag_sets_true(self):
        args, _ = _cli.parse_args(['--preview-ansi'])
        self.assertTrue(args.preview_ansi)

    def test_preview_ansi_in_help(self):
        # The flag should be advertised in --help so users discover it.
        parser = _cli.build_argparser()
        help_text = parser.format_help()
        self.assertIn('--preview-ansi', help_text)
        self.assertIn('--no-preview-ansi', help_text)

    def test_children_pane_default_true(self):
        args, _ = _cli.parse_args([])
        self.assertTrue(args.children_pane)

    def test_no_children_pane_flag_sets_false(self):
        args, _ = _cli.parse_args(['--no-children-pane'])
        self.assertFalse(args.children_pane)

    def test_children_pane_flag_sets_true(self):
        # Positive form mainly useful for overriding a recipe-side
        # config that turned the pane off; CLI default is already True.
        args, _ = _cli.parse_args(['--children-pane'])
        self.assertTrue(args.children_pane)

    def test_multi_select_default_true(self):
        args, _ = _cli.parse_args([])
        self.assertTrue(args.multi_select)

    def test_no_multi_select_flag_sets_false(self):
        args, _ = _cli.parse_args(['--no-multi-select'])
        self.assertFalse(args.multi_select)

    def test_multi_select_flag_sets_true(self):
        args, _ = _cli.parse_args(['--multi-select'])
        self.assertTrue(args.multi_select)

    def test_initial_scope(self):
        args, _ = _cli.parse_args(['--initial-scope', 'x'])
        self.assertEqual(args.initial_scope, 'x')

    def test_install_orthogonal_to_children(self):
        # Both flags should land on the namespace; the dispatch in main()
        # picks one. argparse doesn't enforce mutual exclusion here, by design.
        args, _ = _cli.parse_args([
            '--install', 'user', '--children-cmd', 'ls',
        ])
        self.assertEqual(args.install, 'user')
        self.assertEqual(args.children_cmd, 'ls')

    def test_no_run_field_in_tui_mode(self):
        # In normal TUI mode the namespace still carries run/run_mode
        # set to None — main()'s recipe gate keys off ``args.run``.
        args, extras = _cli.parse_args([])
        self.assertIsNone(args.run)
        self.assertIsNone(args.run_mode)
        self.assertEqual(extras, [])


class TestRecipeDispatch(unittest.TestCase):
    """parse_args recipe-mode rules: bare positional + --run/--run-py/--run-cli."""

    def test_bare_positional_is_auto_run(self):
        args, extras = _cli.parse_args(['my-recipe', 'arg1', 'arg2'])
        self.assertEqual(args.run, 'my-recipe')
        self.assertEqual(args.run_mode, 'auto')
        self.assertEqual(extras, ['arg1', 'arg2'])

    def test_run_flag_explicit_auto(self):
        args, extras = _cli.parse_args(['--run', 'my-recipe', 'arg1'])
        self.assertEqual(args.run, 'my-recipe')
        self.assertEqual(args.run_mode, 'auto')
        self.assertEqual(extras, ['arg1'])

    def test_run_py_explicit_python(self):
        args, extras = _cli.parse_args(['--run-py', 'recipe.py', '-x'])
        self.assertEqual(args.run, 'recipe.py')
        self.assertEqual(args.run_mode, 'py')
        self.assertEqual(extras, ['-x'])

    def test_run_cli_explicit_exec(self):
        args, extras = _cli.parse_args(['--run-cli', 'recipe.sh', '-x'])
        self.assertEqual(args.run, 'recipe.sh')
        self.assertEqual(args.run_mode, 'cli')
        self.assertEqual(extras, ['-x'])

    def test_run_no_args_after_recipe(self):
        args, extras = _cli.parse_args(['--run-py', 'recipe.py'])
        self.assertEqual(args.run, 'recipe.py')
        self.assertEqual(extras, [])

    def test_recipe_args_are_passed_verbatim(self):
        # Args that look like browse-tui flags must land in the
        # recipe's argv, not be parsed by argparse.
        args, extras = _cli.parse_args([
            '--run', 'my-recipe',
            '--no-preview', '--children-cmd', 'ls',
        ])
        # No argparse fields populated — namespace doesn't carry them.
        self.assertFalse(hasattr(args, 'no_preview'))
        self.assertFalse(hasattr(args, 'children_cmd'))
        self.assertEqual(extras, ['--no-preview', '--children-cmd', 'ls'])

    def test_run_with_path_like_recipe(self):
        # The motivating case: ``browse-tui recipes/browse-fs ~/foo``
        # used to crash because ``~/foo`` was an unknown positional.
        args, extras = _cli.parse_args([
            'recipes/browse-fs', '/tmp/some/dir',
        ])
        self.assertEqual(args.run, 'recipes/browse-fs')
        self.assertEqual(extras, ['/tmp/some/dir'])

    def test_flag_before_recipe_path_falls_through_to_argparse(self):
        # ``browse-tui --children-cmd ls my-recipe`` → argparse sees
        # ``my-recipe`` as an unknown positional and rejects it. parse_args
        # itself just builds a normal TUI namespace; argparse's own error
        # path takes over.
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                _cli.parse_args(['--children-cmd', 'ls', 'my-recipe'])

    def test_run_flag_without_script_errors(self):
        # ``--run`` with nothing after errors with exit code 2.
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                _cli.parse_args(['--run'])
        self.assertEqual(cm.exception.code, 2)

    def test_run_flag_with_flag_argument_errors(self):
        # ``--run --foo`` is a misuse, not a recipe path — error.
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                _cli.parse_args(['--run', '--foo'])


class TestRecipeAutoDetect(unittest.TestCase):
    """``_detect_recipe_mode`` classifies recipes by shebang + +x bit."""

    def _write(self, content: str, executable: bool):
        fd, path = tempfile.mkstemp(prefix='recipe_')
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        if executable:
            os.chmod(path, 0o755)
        else:
            os.chmod(path, 0o644)
        self.addCleanup(os.unlink, path)
        return path

    def test_python_shebang_classified_as_py(self):
        path = self._write('#!/usr/bin/env python3\nprint("hi")\n',
                           executable=False)
        self.assertEqual(_cli._detect_recipe_mode(path), 'py')

    def test_python_shebang_with_browse_tui_run_py(self):
        # The recipe-style shebang our shipped recipes use.
        path = self._write(
            '#!/usr/bin/env -S browse-tui --run-py\nprint(1)\n',
            executable=False,
        )
        # No literal "python" in the shebang — must classify as cli (or
        # error if not +x). The shebang chain handles routing through
        # browse-tui --run-py at the kernel level.
        # Here we test +x → cli.
        os.chmod(path, 0o755)
        self.assertEqual(_cli._detect_recipe_mode(path), 'cli')

    def test_python_word_boundary_avoids_false_positive(self):
        # ``cpython`` in a path should NOT match — \bpython\b only.
        path = self._write(
            '#!/opt/cpython3/bin/foo\nprint(1)\n',
            executable=True,
        )
        self.assertEqual(_cli._detect_recipe_mode(path), 'cli')

    def test_executable_bash_shebang_is_cli(self):
        path = self._write('#!/bin/bash\necho hi\n', executable=True)
        self.assertEqual(_cli._detect_recipe_mode(path), 'cli')

    def test_no_shebang_not_executable_is_error(self):
        path = self._write('print("hi")\n', executable=False)
        self.assertEqual(_cli._detect_recipe_mode(path), 'error')

    def test_no_shebang_executable_is_cli(self):
        path = self._write('print("hi")\n', executable=True)
        self.assertEqual(_cli._detect_recipe_mode(path), 'cli')

    def test_missing_file_is_error(self):
        self.assertEqual(_cli._detect_recipe_mode('/nope/missing'), 'error')


class TestRecordSepDecoding(unittest.TestCase):
    """Translation of ``--record-sep`` flag values to raw bytes."""

    def test_nl(self):
        self.assertEqual(_cli.decode_record_sep('nl'), b'\n')

    def test_null(self):
        self.assertEqual(_cli.decode_record_sep('null'), b'\0')

    def test_literal(self):
        self.assertEqual(_cli.decode_record_sep('|||'), b'|||')


class TestParseActionSpec(unittest.TestCase):
    """``KEY:LABEL:CMD`` splitting — first two colons only."""

    def test_basic(self):
        k, l, c = _cli.parse_action_spec('e:Edit:$EDITOR "$TUI_ID"')
        self.assertEqual(k, 'e')
        self.assertEqual(l, 'Edit')
        self.assertEqual(c, '$EDITOR "$TUI_ID"')

    def test_missing_parts(self):
        with self.assertRaises(ValueError):
            _cli.parse_action_spec('e:Edit')

    def test_colons_in_cmd_preserved(self):
        # The CMD may contain colons freely (sed s///g, URLs, etc.).
        k, l, c = _cli.parse_action_spec('e::echo a:b:c')
        self.assertEqual(k, 'e')
        self.assertEqual(l, '')
        self.assertEqual(c, 'echo a:b:c')


class TestItemEnv(unittest.TestCase):
    """``item_env`` exports TUI_* vars; reserved names are dispatcher-owned."""

    def _item(self, **kwargs):
        return _data.to_item({'id': 'i1', 'title': 't', **kwargs})

    def test_standard_fields(self):
        env = _cli.item_env(self._item(tag='T', tag_style='red'))
        self.assertEqual(env['TUI_ID'], 'i1')
        self.assertEqual(env['TUI_TITLE'], 't')
        self.assertEqual(env['TUI_TAG'], 'T')
        self.assertEqual(env['TUI_TAG_STYLE'], 'red')
        self.assertEqual(env['TUI_HAS_CHILDREN'], '0')

    def test_custom_attribute(self):
        item = self._item(path='/foo/bar')
        env = _cli.item_env(item)
        self.assertEqual(env['TUI_PATH'], '/foo/bar')

    def test_bool_attribute_coerced(self):
        item = self._item(has_children=True)
        env = _cli.item_env(item)
        self.assertEqual(env['TUI_HAS_CHILDREN'], '1')
        # Custom bool attributes follow the same convention.
        item2 = self._item()
        item2.is_dir = False
        env2 = _cli.item_env(item2)
        self.assertEqual(env2['TUI_IS_DIR'], '0')

    def test_reserved_names_not_overwritten(self):
        item = self._item()
        # An item attribute named ``bin`` would normally export as
        # ``TUI_BIN`` — but that slot is reserved for the running binary.
        item.bin = '/should/not/leak'
        item.ids_file = '/also/no'
        item.ids_count = 999
        item.targets = 'lies'
        env = _cli.item_env(item, bin_path='/real/bin', targets='cursor')
        self.assertEqual(env['TUI_BIN'], '/real/bin')
        self.assertEqual(env['TUI_TARGETS'], 'cursor')
        self.assertEqual(env['TUI_IDS_COUNT'], '0')
        self.assertNotIn('lies', env['TUI_TARGETS'])

    def test_non_identifier_attrs_skipped(self):
        item = self._item()
        # Underscore-prefixed = private; not exported.
        item._private = 'shh'
        # dashes/spaces are not valid identifiers; skipped silently.
        # (setattr accepts anything, but our dir() filter skips them.)
        env = _cli.item_env(item)
        self.assertNotIn('TUI__PRIVATE', env)
        # Standard fields still present.
        self.assertIn('TUI_ID', env)


class TestRunActionCmd(unittest.TestCase):
    """``run_action_cmd`` runs bash, honours timeout, returns exit codes."""

    def _item(self):
        return _data.to_item({'id': 'i1', 'title': 't'})

    def test_success(self):
        rc = _cli.run_action_cmd('exit 0', self._item())
        self.assertEqual(rc, 0)

    def test_failure(self):
        rc = _cli.run_action_cmd('exit 7', self._item())
        self.assertEqual(rc, 7)

    def test_timeout(self):
        rc = _cli.run_action_cmd('sleep 5', self._item(), timeout=0.1)
        self.assertEqual(rc, 124)

    def test_env_visible_to_child(self):
        # Use a tmpfile to capture child stdout deterministically.
        with tempfile.NamedTemporaryFile(mode='w+', delete=False) as f:
            path = f.name
        try:
            rc = _cli.run_action_cmd(
                f'echo "$TUI_ID" > {path}', self._item(),
            )
            self.assertEqual(rc, 0)
            with open(path) as f:
                self.assertEqual(f.read().strip(), 'i1')
        finally:
            os.unlink(path)


class TestInstallDryRun(unittest.TestCase):
    """End-to-end install via tmpdir-scoped paths.

    Each test isolates filesystem state by chdir-ing into a tmpdir and
    using ``--install local`` (which writes ``./browse-tui`` relative to
    the CWD). ``sys.argv[0]`` is repointed at a small fake binary so we
    don't shuffle around the actual concatenated build.
    """

    def setUp(self):
        self._cwd = os.getcwd()
        self._argv0 = sys.argv[0]
        self._tmp = tempfile.mkdtemp(prefix='browse-tui-test-')
        os.chdir(self._tmp)
        # Fake the running binary: a tiny script with predictable bytes.
        self._fake = os.path.join(self._tmp, 'src-binary')
        with open(self._fake, 'wb') as f:
            f.write(b'#!/bin/sh\necho fake-binary\n')
        os.chmod(self._fake, 0o755)
        sys.argv[0] = self._fake

    def tearDown(self):
        sys.argv[0] = self._argv0
        os.chdir(self._cwd)
        # Clean up the tmpdir aggressively; tests should leave no trace.
        import shutil as _sh
        _sh.rmtree(self._tmp, ignore_errors=True)

    def test_local_install_copies(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _cli.cmd_install('local')
            self.assertEqual(rc, 0)
            dst = os.path.join(self._tmp, 'browse-tui')
            self.assertTrue(os.path.exists(dst))
            with open(self._fake, 'rb') as a, open(dst, 'rb') as b:
                self.assertEqual(a.read(), b.read())
            # Re-installing the same content is a no-op (rc 0).
            rc2 = _cli.cmd_install('local')
            self.assertEqual(rc2, 0)

    def test_idempotent(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(_cli.cmd_install('local'), 0)
            # Second run with identical content prints "already installed".
            rc = _cli.cmd_install('local')
            self.assertEqual(rc, 0)
        # Sanity: idempotent path was hit (no "exists and differs" message).
        self.assertIn('already installed', buf.getvalue())


class TestHelpOutput(unittest.TestCase):
    """``--help`` includes the in-app keybindings reference."""

    def test_bare_invocation_prints_help_with_zero_exit(self):
        # ``browse-tui`` (no args) — there's no useful default action,
        # so we print --help and exit 0 instead of erroring.
        import subprocess
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        binary = os.path.join(root, 'browse-tui')
        if not os.path.exists(binary):
            self.skipTest('browse-tui binary not built (run ./build-tui.sh)')
        proc = subprocess.run(
            [binary], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn('usage:', proc.stdout)
        self.assertIn('NAVIGATION', proc.stdout)
        # Negative: the old error path must not surface.
        self.assertNotIn('--children-cmd or --root-cmd is required',
                         proc.stderr + proc.stdout)

    def test_help_includes_keybindings(self):
        # Run the concatenated build directly so the help composer
        # (defined in 050-render.py) is in scope alongside the CLI
        # dispatcher.
        import subprocess
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        binary = os.path.join(root, 'browse-tui')
        if not os.path.exists(binary):
            self.skipTest('browse-tui binary not built (run ./build-tui.sh)')
        proc = subprocess.run(
            [binary, '--help'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        # Spot-check the section headers and a couple of well-known
        # bindings — emitted by ``compose_help_text``.
        self.assertIn('usage:', proc.stdout)
        self.assertIn('NAVIGATION', proc.stdout)
        self.assertIn('PREVIEW', proc.stdout)
        self.assertIn('SEARCH', proc.stdout)
        self.assertIn('OTHER', proc.stdout)
        self.assertIn('Quit', proc.stdout)


class TestRunPyLoader(unittest.TestCase):
    """``cmd_run_py`` self-injects the module as ``browse_tui`` and runs the script."""

    def test_self_injection(self):
        # The recipe script imports ``browse_tui`` and pokes a marker.
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False,
        ) as f:
            f.write('import browse_tui\n')
            f.write('browse_tui._test_marker = "OK"\n')
            script = f.name
        try:
            # ``cmd_run_py`` does ``sys.modules['browse_tui'] = sys.modules[__name__]``;
            # _loader.py doesn't register the module in ``sys.modules`` so we
            # do it here to mirror the runtime invariant (in the concatenated
            # build, ``__name__`` is ``'__main__'`` which is always present).
            saved_argv = list(sys.argv)
            saved_browse = sys.modules.get('browse_tui')
            saved_self = sys.modules.get(_cli.__name__)
            sys.modules[_cli.__name__] = _cli
            try:
                rc = _cli.cmd_run_py(script, [], version='0.1.0')
            finally:
                sys.argv[:] = saved_argv
                if saved_browse is None:
                    sys.modules.pop('browse_tui', None)
                else:
                    sys.modules['browse_tui'] = saved_browse
                if saved_self is None:
                    sys.modules.pop(_cli.__name__, None)
                else:
                    sys.modules[_cli.__name__] = saved_self
            self.assertEqual(rc, 0)
            # Marker landed on the module that backs ``browse_tui`` —
            # which, in our test setup, is ``_browse_tui_cli`` (i.e. _cli).
            self.assertEqual(getattr(_cli, '_test_marker', None), 'OK')
        finally:
            os.unlink(script)


class TestPreviewFetcherWiring(unittest.TestCase):
    """``--preview-cmd`` must wire ``browser.get_preview`` for both builders.

    Regression: ``_build_eager_browser`` (the ``--root-cmd`` path) used
    to drop ``args.preview_cmd`` on the floor — only the lazy
    ``--children-cmd`` path constructed the fetcher. As a result the v/e
    bindings reported "No preview available" when launched with a
    ``--root-cmd`` recipe, even when ``--preview-cmd`` was supplied.
    """

    def test_make_preview_fetcher_returns_none_when_unset(self):
        self.assertIsNone(_cli._make_preview_fetcher('', timeout=5))
        self.assertIsNone(_cli._make_preview_fetcher(None, timeout=5))

    def test_make_preview_fetcher_runs_command_with_tui_id(self):
        # Smoke: a real shell invocation. ``echo $TUI_ID`` produces the
        # id followed by a newline.
        get_preview = _cli._make_preview_fetcher('echo "$TUI_ID"', timeout=5)
        self.assertIsNotNone(get_preview)
        self.assertEqual(get_preview('item-42'), 'item-42\n')

    def test_make_preview_fetcher_returns_error_string_on_timeout(self):
        # subprocess.TimeoutExpired must surface as an inline error, not
        # a raise — a flaky preview shouldn't crash the UI.
        get_preview = _cli._make_preview_fetcher('sleep 5', timeout=0.1)
        result = get_preview('x')
        self.assertTrue(result.startswith('[error]'),
                        f'expected [error] prefix, got: {result!r}')

    def test_eager_builder_wires_preview_fetcher(self):
        # Build via parse_args → _build_eager_browser; verify get_preview
        # is set when --preview-cmd is supplied alongside --root-cmd.
        # Use a heredoc-ish argv that produces 2 rows.
        args, _ = _cli.parse_args([
            '--root-cmd', "printf 'a\\nb\\n'",
            '--preview-cmd', 'echo PREVIEW-OF-$TUI_ID',
        ])
        b = _cli._build_eager_browser(args, fields=['id', 'title'], record_sep=b'\n')
        try:
            self.assertIsNotNone(b)
            self.assertIsNotNone(
                b.get_preview,
                '--root-cmd + --preview-cmd should produce a get_preview',
            )
            self.assertEqual(b.get_preview('a'), 'PREVIEW-OF-a\n')
        finally:
            if b is not None:
                b.stop_workers()

    def test_eager_builder_no_preview_cmd_yields_none(self):
        # Without --preview-cmd, browser.get_preview stays None (as
        # before — this is the v/e "No preview available" path).
        args, _ = _cli.parse_args([
            '--root-cmd', "printf 'a\\n'",
        ])
        b = _cli._build_eager_browser(args, fields=['id', 'title'], record_sep=b'\n')
        try:
            self.assertIsNotNone(b)
            self.assertIsNone(b.get_preview)
        finally:
            if b is not None:
                b.stop_workers()

    def test_lazy_builder_still_wires_preview_fetcher(self):
        # Sanity: refactor of the lazy builder didn't break the existing
        # path.
        args, _ = _cli.parse_args([
            '--children-cmd', "printf 'a\\nb\\n'",
            '--preview-cmd', 'echo LAZY-$TUI_ID',
        ])
        b = _cli._build_lazy_browser(args, fields=['id', 'title'], record_sep=b'\n')
        try:
            self.assertIsNotNone(b.get_preview)
            self.assertEqual(b.get_preview('z'), 'LAZY-z\n')
        finally:
            b.stop_workers()


class TestPathSep(unittest.TestCase):
    """``--path-sep`` argparse capture + eager-builder / run_tui wiring."""

    def test_default_is_none(self):
        args, _ = _cli.parse_args([])
        self.assertIsNone(args.path_sep)

    def test_argparse_captures_value(self):
        args, _ = _cli.parse_args(['--path-sep', '/'])
        self.assertEqual(args.path_sep, '/')

    def test_in_help(self):
        parser = _cli.build_argparser()
        self.assertIn('--path-sep', parser.format_help())

    def test_eager_builder_splits_into_tree(self):
        # ``--path-sep /`` end to end: rows parsed then from_flat_tree
        # synthesizes the prefix tree (intermediate nodes included).
        args, _ = _cli.parse_args([
            '--root-cmd', "printf 'docs/api/auth.md\\ndocs/README.md\\n'",
            '--path-sep', '/',
        ])
        b = _cli._build_eager_browser(
            args, fields=['id', 'title'], record_sep=b'\n',
        )
        try:
            self.assertIsNotNone(b)
            s = b._state
            # Synthesised intermediate prefixes appear as real nodes.
            self.assertEqual(
                set(s._items_by_id),
                {'docs', 'docs/api', 'docs/api/auth.md', 'docs/README.md'},
            )
            # Top-level nodes parent to the CLI's default root id ('').
            self.assertEqual(s._parent_of_id['docs'], '')
            self.assertEqual(s._parent_of_id['docs/api'], 'docs')
            self.assertEqual(s._parent_of_id['docs/api/auth.md'], 'docs/api')
            self.assertEqual(s._parent_of_id['docs/README.md'], 'docs')
            # has_children derived from structure.
            self.assertTrue(s._items_by_id['docs'].has_children)
            self.assertTrue(s._items_by_id['docs/api'].has_children)
            self.assertFalse(s._items_by_id['docs/api/auth.md'].has_children)
        finally:
            if b is not None:
                b.stop_workers()

    def test_eager_builder_metadata_rides_onto_leaf(self):
        # A metadata column (size) rides along onto the leaf unchanged;
        # only the id is split.
        args, _ = _cli.parse_args([
            '--root-cmd', "printf 'docs/api/auth.md\\t10\\n'",
            '--fields', 'id,size',
            '--path-sep', '/',
        ])
        b = _cli._build_eager_browser(
            args, fields=['id', 'size'], record_sep=b'\n',
        )
        try:
            self.assertIsNotNone(b)
            s = b._state
            self.assertEqual(
                set(s._items_by_id),
                {'docs', 'docs/api', 'docs/api/auth.md'},
            )
            leaf = s._items_by_id['docs/api/auth.md']
            self.assertEqual(getattr(leaf, 'size', None), '10')
            # The synthesised prefixes carry no metadata.
            self.assertIsNone(
                getattr(s._items_by_id['docs'], 'size', None)
            )
        finally:
            if b is not None:
                b.stop_workers()

    def test_path_sep_with_children_cmd_errors(self):
        # Lazy mode is rejected: run_tui returns 2 with the exact message.
        args, _ = _cli.parse_args([
            '--children-cmd', 'ls $TUI_ID',
            '--path-sep', '/',
        ])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = _cli.run_tui(args)
        self.assertEqual(rc, 2)
        self.assertEqual(
            buf.getvalue(),
            'error: --path-sep requires --root-cmd (eager mode)\n',
        )

    def test_parent_column_warns_and_explicit_mode_wins(self):
        # A parent column beats path_sep: warning emitted, parent-pointer
        # grouping used (ids NOT split into prefix nodes).
        args, _ = _cli.parse_args([
            '--root-cmd', "printf '{\"id\": \"a/b\"}\\n"
                          "{\"id\": \"a/b/c\", \"parent\": \"a/b\"}\\n'",
            '--input', 'json',
            '--path-sep', '/',
        ])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            b = _cli._build_eager_browser(
                args, fields=['id', 'title'], record_sep=b'\n',
            )
        try:
            self.assertEqual(
                buf.getvalue(),
                'browse-tui: --path-sep ignored: rows carry explicit '
                'parent/depth\n',
            )
            self.assertIsNotNone(b)
            s = b._state
            # No synthesised 'a' prefix — explicit parent mode won.
            self.assertEqual(set(s._items_by_id), {'a/b', 'a/b/c'})
            self.assertEqual(s._parent_of_id['a/b'], '')
            self.assertEqual(s._parent_of_id['a/b/c'], 'a/b')
        finally:
            if b is not None:
                b.stop_workers()

    def test_depth_column_warns_and_explicit_mode_wins(self):
        # A depth column likewise beats path_sep: warning + depth-coded
        # grouping, ids untouched.
        args, _ = _cli.parse_args([
            '--root-cmd', "printf '{\"id\": \"a/b\", \"depth\": 0}\\n"
                          "{\"id\": \"a/b/c\", \"depth\": 1}\\n'",
            '--input', 'json',
            '--path-sep', '/',
        ])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            b = _cli._build_eager_browser(
                args, fields=['id', 'title'], record_sep=b'\n',
            )
        try:
            self.assertEqual(
                buf.getvalue(),
                'browse-tui: --path-sep ignored: rows carry explicit '
                'parent/depth\n',
            )
            self.assertIsNotNone(b)
            s = b._state
            self.assertEqual(set(s._items_by_id), {'a/b', 'a/b/c'})
            self.assertEqual(s._parent_of_id['a/b'], '')
            self.assertEqual(s._parent_of_id['a/b/c'], 'a/b')
        finally:
            if b is not None:
                b.stop_workers()

    def test_null_valued_parent_does_not_warn_and_path_split_runs(self):
        # A null-valued parent column does NOT override path-split (the
        # precedence keys off non-None values), so no warning is emitted
        # and the ids are still split into the synthesized prefix tree.
        args, _ = _cli.parse_args([
            '--root-cmd',
            "printf '{\"id\": \"docs/api/auth.md\", \"parent\": null}\\n'",
            '--input', 'json',
            '--path-sep', '/',
        ])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            b = _cli._build_eager_browser(
                args, fields=['id', 'title'], record_sep=b'\n',
            )
        try:
            self.assertEqual(buf.getvalue(), '')
            self.assertIsNotNone(b)
            s = b._state
            # Path-split ran: synthesized prefix nodes are present.
            self.assertEqual(
                set(s._items_by_id),
                {'docs', 'docs/api', 'docs/api/auth.md'},
            )
        finally:
            if b is not None:
                b.stop_workers()

    def test_null_valued_depth_does_not_warn_and_path_split_runs(self):
        # Same for a null-valued depth column.
        args, _ = _cli.parse_args([
            '--root-cmd',
            "printf '{\"id\": \"docs/api/auth.md\", \"depth\": null}\\n'",
            '--input', 'json',
            '--path-sep', '/',
        ])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            b = _cli._build_eager_browser(
                args, fields=['id', 'title'], record_sep=b'\n',
            )
        try:
            self.assertEqual(buf.getvalue(), '')
            self.assertIsNotNone(b)
            s = b._state
            self.assertEqual(
                set(s._items_by_id),
                {'docs', 'docs/api', 'docs/api/auth.md'},
            )
        finally:
            if b is not None:
                b.stop_workers()


class TestResolveListSize(unittest.TestCase):
    """``--list-size`` parses ``N`` (lines) or ``N%`` (percentage)."""

    def test_none_returns_default(self):
        self.assertAlmostEqual(_cli._resolve_list_size(None), 0.30)
        self.assertAlmostEqual(_cli._resolve_list_size(''), 0.30)

    def test_percent_form(self):
        self.assertAlmostEqual(_cli._resolve_list_size('30%'), 0.30)
        self.assertAlmostEqual(_cli._resolve_list_size('50%'), 0.50)
        self.assertAlmostEqual(_cli._resolve_list_size('100%'), 0.999, places=2)

    def test_percent_clamped_to_safe_range(self):
        # 0% / 100% would give degenerate panes; clamped just inside.
        self.assertGreater(_cli._resolve_list_size('0%'), 0.0)
        self.assertLess(_cli._resolve_list_size('100%'), 1.0)

    def test_absolute_lines_uses_terminal_height(self):
        # Stub get_terminal_size to a known value.
        import os as _os
        saved = _os.get_terminal_size
        _os.get_terminal_size = lambda: type('S', (), {'lines': 100})()
        try:
            self.assertAlmostEqual(_cli._resolve_list_size('40'), 0.40)
            self.assertAlmostEqual(_cli._resolve_list_size('25'), 0.25)
        finally:
            _os.get_terminal_size = saved

    def test_absolute_lines_falls_back_when_no_terminal(self):
        import os as _os

        def boom():
            raise OSError('no tty')

        saved = _os.get_terminal_size
        _os.get_terminal_size = boom
        try:
            self.assertAlmostEqual(_cli._resolve_list_size('40'), 0.30)
        finally:
            _os.get_terminal_size = saved

    def test_invalid_input_falls_back_with_warning(self):
        # Not numeric, not N% — fall back to default; stderr warning.
        import io as _io
        import contextlib as _ctx
        buf = _io.StringIO()
        with _ctx.redirect_stderr(buf):
            r = _cli._resolve_list_size('garbage')
        self.assertAlmostEqual(r, 0.30)
        self.assertIn('warning', buf.getvalue().lower())

    def test_zero_or_negative_lines_falls_back(self):
        import io as _io
        import contextlib as _ctx
        with _ctx.redirect_stderr(_io.StringIO()):
            self.assertAlmostEqual(_cli._resolve_list_size('0'), 0.30)
            self.assertAlmostEqual(_cli._resolve_list_size('-5'), 0.30)


class TestResolveSplitType(unittest.TestCase):
    """``--split-type`` long/short forms, case-insensitivity, auto threshold."""

    def test_long_forms_map_to_short(self):
        self.assertEqual(_cli._resolve_split_type('horizontal', 80), 'h')
        self.assertEqual(_cli._resolve_split_type('vertical', 300), 'v')
        self.assertEqual(_cli._resolve_split_type('mixed', 80), 'm')
        self.assertEqual(_cli._resolve_split_type('preview-children', 80), 'pc')

    def test_short_forms_pass_through(self):
        self.assertEqual(_cli._resolve_split_type('h', 80), 'h')
        self.assertEqual(_cli._resolve_split_type('v', 80), 'v')
        self.assertEqual(_cli._resolve_split_type('m', 80), 'm')
        self.assertEqual(_cli._resolve_split_type('pc', 80), 'pc')

    def test_mixed_case(self):
        self.assertEqual(_cli._resolve_split_type('Horizontal', 80), 'h')
        self.assertEqual(_cli._resolve_split_type('VERTICAL', 300), 'v')
        self.assertEqual(_cli._resolve_split_type('Auto', 300), 'v')
        self.assertEqual(_cli._resolve_split_type('AUTO', 80), 'h')
        self.assertEqual(_cli._resolve_split_type('PC', 80), 'pc')
        self.assertEqual(_cli._resolve_split_type('Preview-Children', 80), 'pc')

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            _cli._resolve_split_type('nope', 80)
        with self.assertRaises(ValueError):
            _cli._resolve_split_type('', 80)
        with self.assertRaises(ValueError):
            _cli._resolve_split_type(42, 80)

    def test_auto_threshold_below(self):
        # 229 cols → horizontal stack.
        self.assertEqual(_cli._resolve_split_type('auto', 229), 'h')
        self.assertEqual(_cli._resolve_split_type('a', 80), 'h')
        self.assertEqual(_cli._resolve_split_type('auto', 0), 'h')

    def test_auto_threshold_at_and_above(self):
        # 230 cols (the boundary) → vertical; wider also vertical.
        self.assertEqual(_cli._resolve_split_type('auto', 230), 'v')
        self.assertEqual(_cli._resolve_split_type('auto', 300), 'v')
        self.assertEqual(_cli._resolve_split_type('a', 500), 'v')

    def test_none_defaults_to_auto(self):
        # None → treated as 'auto'; threshold rules apply.
        self.assertEqual(_cli._resolve_split_type(None, 80), 'h')
        self.assertEqual(_cli._resolve_split_type(None, 230), 'v')


class TestSplitTypeArgparse(unittest.TestCase):
    """``--split-type`` is captured on the namespace; default is 'auto'."""

    def test_default_is_auto(self):
        args, _ = _cli.parse_args([])
        self.assertEqual(args.split_type, 'auto')

    def test_short_value(self):
        args, _ = _cli.parse_args(['--split-type', 'h'])
        self.assertEqual(args.split_type, 'h')

    def test_long_value(self):
        args, _ = _cli.parse_args(['--split-type', 'preview-children'])
        self.assertEqual(args.split_type, 'preview-children')

    def test_invalid_value_accepted_by_argparse_then_rejected_by_resolver(self):
        # We deliberately don't use argparse choices=, so parsing succeeds
        # and the resolver raises a clean ValueError instead.
        args, _ = _cli.parse_args(['--split-type', 'bogus'])
        self.assertEqual(args.split_type, 'bogus')
        with self.assertRaises(ValueError):
            _cli._resolve_split_type(args.split_type, 100)


class TestSplitTypeWiring(unittest.TestCase):
    """``--split-type`` flows through both builders to ``Browser.split``."""

    def test_lazy_builder_passes_split_h(self):
        args, _ = _cli.parse_args([
            '--children-cmd', "printf 'a\\n'",
            '--split-type', 'h',
        ])
        b = _cli._build_lazy_browser(
            args, fields=['id', 'title'], record_sep=b'\n', split='h',
        )
        try:
            self.assertEqual(b.split, 'h')
        finally:
            b.stop_workers()

    def test_lazy_builder_passes_split_v(self):
        args, _ = _cli.parse_args([
            '--children-cmd', "printf 'a\\n'",
            '--split-type', 'v',
        ])
        b = _cli._build_lazy_browser(
            args, fields=['id', 'title'], record_sep=b'\n', split='v',
        )
        try:
            self.assertEqual(b.split, 'v')
        finally:
            b.stop_workers()

    def test_eager_builder_passes_split_m(self):
        args, _ = _cli.parse_args([
            '--root-cmd', "printf 'a\\n'",
            '--split-type', 'mixed',
        ])
        b = _cli._build_eager_browser(
            args, fields=['id', 'title'], record_sep=b'\n', split='m',
        )
        try:
            self.assertIsNotNone(b)
            self.assertEqual(b.split, 'm')
        finally:
            if b is not None:
                b.stop_workers()

    def test_eager_builder_passes_split_pc(self):
        args, _ = _cli.parse_args([
            '--root-cmd', "printf 'a\\n'",
            '--split-type', 'pc',
        ])
        b = _cli._build_eager_browser(
            args, fields=['id', 'title'], record_sep=b'\n', split='pc',
        )
        try:
            self.assertIsNotNone(b)
            self.assertEqual(b.split, 'pc')
        finally:
            if b is not None:
                b.stop_workers()

    def test_auto_wide_terminal_resolves_to_v(self):
        # Drive the resolver at the boundary used in run_tui.
        self.assertEqual(_cli._resolve_split_type('auto', 300), 'v')
        # And confirm a Browser built with that value sticks.
        args, _ = _cli.parse_args([
            '--root-cmd', "printf 'a\\n'",
            '--split-type', 'auto',
        ])
        b = _cli._build_eager_browser(
            args, fields=['id', 'title'], record_sep=b'\n', split='v',
        )
        try:
            self.assertEqual(b.split, 'v')
        finally:
            if b is not None:
                b.stop_workers()

    def test_auto_narrow_terminal_resolves_to_h(self):
        self.assertEqual(_cli._resolve_split_type('auto', 80), 'h')
        args, _ = _cli.parse_args([
            '--root-cmd', "printf 'a\\n'",
            '--split-type', 'auto',
        ])
        b = _cli._build_eager_browser(
            args, fields=['id', 'title'], record_sep=b'\n', split='h',
        )
        try:
            self.assertEqual(b.split, 'h')
        finally:
            if b is not None:
                b.stop_workers()


class TestTerminalColsForAuto(unittest.TestCase):
    """``_terminal_cols_for_auto`` falls back gracefully across sources."""

    def test_returns_positive_int(self):
        # Whatever the runner is using, the result is a positive int —
        # we don't pin a value because tests run under varying TTY shapes
        # (unittest, pytest, CI VMs without a pty, etc.).
        cols = _cli._terminal_cols_for_auto()
        self.assertIsInstance(cols, int)
        self.assertGreater(cols, 0)

    def test_explicit_default_when_all_sources_fail(self):
        # Force every detection branch to raise/return a non-positive
        # value so the fallback is the only return path. Patches:
        #   * ``builtins.open`` so /dev/tty (used by both the ioctl
        #     probe and the stty fallback) raises OSError;
        #   * ``shutil.get_terminal_size`` so its 80-fallback doesn't
        #     swallow the chain;
        #   * ``subprocess.run`` so the stty probe never spawns a real
        #     ``stty`` (would otherwise return live tty dimensions).
        import builtins
        from unittest import mock
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == '/dev/tty':
                raise OSError('no tty in test')
            return real_open(path, *a, **kw)

        def fake_size(*_a, **_kw):
            raise OSError('no terminal')

        def fake_run(*_a, **_kw):
            raise OSError('no stty in test')

        with mock.patch('builtins.open', side_effect=fake_open), \
             mock.patch.object(_cli.shutil, 'get_terminal_size',
                               side_effect=fake_size), \
             mock.patch.object(_cli.subprocess, 'run',
                               side_effect=fake_run):
            self.assertEqual(_cli._terminal_cols_for_auto(default=42), 42)

    def test_default_mode_does_not_probe_std_fds(self):
        """In /dev/tty mode, a failed device ioctl never falls to the std fds.

        The terminal device is a deliberate choice (spec §1.4), not
        "whichever std fd is a tty" — so when the /dev/tty ioctl fails,
        resolution drops to shutil / stty / default, NOT to
        ``os.get_terminal_size`` on fd 0/1/2. (Pre-#830 this DID probe
        the std fds; that std-fd-first fallback is intentionally gone so
        the chosen width tracks the real UI device, not a piped
        stdout/stderr.) Here every fallback is forced to fail, so a std-fd
        probe — if it still existed and the runner's fds were a tty —
        would be the only way to beat the default; we assert it doesn't.
        """
        import builtins
        from unittest import mock
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == '/dev/tty':
                raise OSError('no tty in test')
            return real_open(path, *a, **kw)

        from collections import namedtuple
        Size = namedtuple('Size', ('columns', 'lines'))

        # If the function still probed the std fds, fd 2 would yield 242
        # and win. The new policy ignores the std fds in /dev/tty mode.
        def fake_size(fd=1):
            if fd == 2:
                return Size(242, 40)
            raise OSError(f'fd {fd} not a tty')

        def fake_run(*_a, **_kw):
            raise OSError('no stty in test')

        with mock.patch('builtins.open', side_effect=fake_open), \
             mock.patch.object(_cli.os, 'get_terminal_size',
                               side_effect=fake_size), \
             mock.patch.object(_cli.shutil, 'get_terminal_size',
                               side_effect=fake_size), \
             mock.patch.object(_cli.subprocess, 'run',
                               side_effect=fake_run):
            self.assertEqual(_cli._terminal_cols_for_auto(default=80), 80)

    def test_tty_dash_probes_std_fds(self):
        """``--tty -`` mode probes the std fds (the UI rides on them).

        With ``tty_path='-'`` the terminal device *is* the process std
        streams, so width comes from ``os.get_terminal_size`` on fd 0
        then fd 1 — never opening /dev/tty.
        """
        import builtins
        from unittest import mock
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == '/dev/tty':
                raise AssertionError('--tty - must not open /dev/tty')
            return real_open(path, *a, **kw)

        from collections import namedtuple
        Size = namedtuple('Size', ('columns', 'lines'))

        # fd 0 (stdin) is a pipe; fd 1 (stdout) is the pty → 242 cols.
        def fake_size(fd=1):
            if fd == 1:
                return Size(242, 40)
            raise OSError(f'fd {fd} not a tty')

        with mock.patch('builtins.open', side_effect=fake_open), \
             mock.patch.object(_cli.os, 'get_terminal_size',
                               side_effect=fake_size):
            self.assertEqual(
                _cli._terminal_cols_for_auto('-', default=80), 242)


class TestTerminalSeparationErrors(unittest.TestCase):
    """No-tty / piped ``--tty -`` clean-error contract (spec §5).

    Both paths must fail *cleanly*: a one-line diagnostic on stderr, a
    non-zero exit, ZERO bytes on stdout (so a command substitution never
    captures terminal-control noise or a half-built UI), and no Python
    traceback. These exercise the real binary as a subprocess because the
    contract is about process-level stdin/stdout/stderr wiring, not an
    in-process function call.
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        cls.binary = os.path.join(root, 'browse-tui')
        if not os.path.exists(cls.binary):
            raise unittest.SkipTest(
                'browse-tui binary not built (run ./build-tui.sh)')

    def test_no_controlling_terminal_clean_error(self):
        """No ``/dev/tty`` + no ``--tty -`` → clean error, no traceback.

        ``start_new_session=True`` runs the child via ``setsid(2)`` so it
        has *no* controlling terminal; stdin/stdout/stderr are pipes (not
        a tty). With neither a controlling terminal nor ``--tty -`` to
        ride the std streams, ``term_init``'s ``/dev/tty`` open fails and
        the binary must surface the exact ``no controlling terminal``
        guidance — pointing the user at ``--tty -`` — and exit non-zero.

        Strong assertions (each would flip if the behaviour regressed):
          * exit code is exactly 1 (SystemExit(str) → rc 1), not 0;
          * stderr is *exactly* the guidance line (a regression that
            dropped the ``pass --tty -`` hint, or leaked a usage banner,
            would fail the equality);
          * stdout is empty — a half-initialised UI painting escape bytes
            to stdout (the pre-separation bug) would make this non-zero;
          * no ``Traceback`` — an unhandled ``OSError``/``termios.error``
            instead of the clean ``SystemExit`` would print one.
        """
        import subprocess
        proc = subprocess.run(
            [self.binary, '--root-cmd', 'cat'],
            input=b'a\nb\n',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(
            proc.stderr,
            b'browse-tui: no controlling terminal; '
            b'pass --tty - to run over stdin/stdout\n',
        )
        self.assertEqual(len(proc.stdout), 0,
                         f'stdout must be empty, got {proc.stdout!r}')
        self.assertNotIn(b'Traceback', proc.stderr)

    def test_piped_tty_dash_writes_zero_stdout(self):
        """Piped ``--tty -`` → clean error, ZERO stdout bytes, no traceback.

        Regression guard for the ``--tty -`` teardown leak (#841):
        ``printf 'a\\nb\\n' | browse-tui --root-cmd cat --tty -`` resolves
        the UI device to the std streams (fd 0/1), but stdin here is a
        *pipe*, so the raw-mode ``tcgetattr`` fails. The teardown that
        follows must NOT emit any alt-screen / cursor-restore bytes to
        stdout (fd 1 is the captured stream) — the ``_in_raw`` guard means
        ``_leave_raw`` is a no-op when raw was never entered.

        The byte-exact ``len(stdout) == 0`` is the load-bearing
        assertion: the pre-#841 bug leaked teardown escape bytes here,
        contaminating what a command substitution would capture. stderr
        must be exactly ``not a terminal`` (distinct from the no-tty
        message: here the device resolved but isn't a tty), rc 1, and no
        traceback.
        """
        import subprocess
        proc = subprocess.run(
            [self.binary, '--root-cmd', 'cat', '--tty', '-'],
            input=b'a\nb\n',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(proc.stdout), 0,
                         f'stdout must be empty (teardown leak?), '
                         f'got {proc.stdout!r}')
        self.assertEqual(proc.stderr, b'browse-tui: not a terminal\n')
        self.assertNotIn(b'Traceback', proc.stderr)


if __name__ == '__main__':
    unittest.main()
