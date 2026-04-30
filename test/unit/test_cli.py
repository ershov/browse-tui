"""Unit tests for ``080-cli.py``: argparser, install/uninstall, --python loader.

These exercise the CLI surface added in ticket #12. The argparse layer is
trivial; the real load-bearing parts are the env-var assembly, the
install dry-run, and the --python self-injection contract.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from test.unit._loader import load


_data = load('_browse_tui_data', '030-data.py')
_actions = load('_browse_tui_actions', '070-actions.py')
_cli = load('_browse_tui_cli', '080-cli.py')

# ``make_cli_action`` references ``Action`` by bare name — in the
# concatenated build that's resolved across modules; for tests we inject.
_cli.Action = _actions.Action
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
        self.assertFalse(args.no_preview)
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
        self.assertTrue(args.no_preview)

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

    def test_python_passthrough(self):
        # ``--`` separates browse-tui args from the recipe's argv.
        args, extras = _cli.parse_args([
            '--python', 'recipe.py', '--', '-x', 'arg',
        ])
        self.assertEqual(args.python, 'recipe.py')
        self.assertEqual(extras, ['-x', 'arg'])


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

    def test_help_includes_keybindings(self):
        # Run the concatenated build directly so _HELP_TEXT (defined in
        # 050-render.py) is in scope alongside the CLI dispatcher.
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
        self.assertIn('Default keybindings', proc.stdout)
        # Spot-check a couple of well-known bindings show up.
        self.assertIn('NAVIGATION', proc.stdout)
        self.assertIn('Quit', proc.stdout)


class TestPythonLoader(unittest.TestCase):
    """``cmd_python`` self-injects the module as ``browse_tui`` and runs the script."""

    def test_self_injection(self):
        # The recipe script imports ``browse_tui`` and pokes a marker.
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False,
        ) as f:
            f.write('import browse_tui\n')
            f.write('browse_tui._test_marker = "OK"\n')
            script = f.name
        try:
            # ``cmd_python`` does ``sys.modules['browse_tui'] = sys.modules[__name__]``;
            # _loader.py doesn't register the module in ``sys.modules`` so we
            # do it here to mirror the runtime invariant (in the concatenated
            # build, ``__name__`` is ``'__main__'`` which is always present).
            saved_argv = list(sys.argv)
            saved_browse = sys.modules.get('browse_tui')
            saved_self = sys.modules.get(_cli.__name__)
            sys.modules[_cli.__name__] = _cli
            try:
                rc = _cli.cmd_python(script, [], version='0.1.0')
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


if __name__ == '__main__':
    unittest.main()
