"""Unit tests for the ``recipes/browse-mcp`` argv parsing.

The recipe is a single-file ``--run-py`` script that imports ``browse_tui``
(only available when the binary loads it). We stub ``browse_tui`` in
``sys.modules`` and load the extension-less recipe via ``SourceFileLoader`` —
the same pattern as the other recipe unit tests.

Coverage: ``main()`` server-command resolution — the whole argv tail is the
MCP server command, read via ``recipe_argv()`` so the framework-owned
``--tty`` flag (auto-detected by ``Browser.run()``, left in ``sys.argv``) is
dropped rather than leaking into the spawned command.
"""

import importlib.util
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-mcp'


def _recipe_argv(argv=None):
    """Stub of the framework's ``recipe_argv`` (mirrors 040-state.py):
    ``sys.argv[1:]`` (or ``argv``) minus the framework's ``--tty VALUE`` /
    ``--tty=VALUE`` flag. Tests patch ``sys.argv``, so reading it here matches
    what the recipe sees."""
    if argv is None:
        argv = sys.argv[1:]
    out, skip_next = [], False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == '--tty':
            skip_next = True
            continue
        if arg.startswith('--tty='):
            continue
        out.append(arg)
    return out


def _stub_browse_tui():
    """Insert a minimal ``browse_tui`` stub so the recipe imports."""
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
    mod.recipe_argv = _recipe_argv
    sys.modules['browse_tui'] = mod


def _load_recipe():
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
    _stub_browse_tui()
    name = '_browse_mcp_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _StopMain(Exception):
    """Raised by the recording Browser stub to halt ``main()`` after the
    server command has been resolved and handed to ``_MCPClient``."""


class TestServerCommandArgv(unittest.TestCase):
    """``main()`` resolves the MCP server command via ``recipe_argv()``."""

    def setUp(self):
        self.r = _load_recipe()
        self._saved_argv = list(sys.argv)

    def tearDown(self):
        sys.argv[:] = self._saved_argv

    def _cmd_for(self, argv):
        """Drive ``main()`` with ``argv``; return the cmd handed to _MCPClient
        (or raise the recipe's ``SystemExit`` for the usage-error path)."""
        captured = {}

        def _rec_client(cmd):
            captured['cmd'] = cmd
            return types.SimpleNamespace()

        def _rec_browser(*a, **kw):
            raise _StopMain  # halt before the TUI / run()

        self.r._MCPClient = _rec_client
        self.r.Browser = _rec_browser
        sys.argv[:] = ['browse-mcp', *argv]
        try:
            self.r.main()
        except _StopMain:
            pass
        return captured.get('cmd')

    def test_plain_server_command_passed_through(self):
        self.assertEqual(
            self._cmd_for(['uvx', 'mcp-atlassian']), ['uvx', 'mcp-atlassian'])

    def test_tty_flag_is_stripped_from_the_command(self):
        # ``--tty -`` / ``--tty=-`` / ``--tty /dev/pts/N`` is the framework
        # UI-device flag, not part of the server command: dropped before the
        # command is built. Without the fix it leaked into the spawned cmd.
        self.assertEqual(
            self._cmd_for(['--tty', '-', 'uvx', 'srv']), ['uvx', 'srv'])
        self.assertEqual(
            self._cmd_for(['--tty=-', 'uvx', 'srv']), ['uvx', 'srv'])
        self.assertEqual(
            self._cmd_for(['--tty', '/dev/pts/9', 'npx', 'srv', '/tmp']),
            ['npx', 'srv', '/tmp'])

    def test_only_tty_flag_is_a_usage_error(self):
        # ``browse-mcp --tty -`` with no server command: the filtered cmd is
        # empty, so it's a usage error (exit 2), not an empty-command spawn.
        with self.assertRaises(SystemExit) as cm:
            self._cmd_for(['--tty', '-'])
        self.assertEqual(cm.exception.code, 2)

    def test_no_args_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            self._cmd_for([])
        self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
