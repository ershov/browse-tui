"""Unit tests for the ``recipes/browse-jira-mcp`` argv parsing.

The recipe is a single-file ``--run-py`` script that imports ``browse_tui``
(only available when the binary loads it). We stub ``browse_tui`` in
``sys.modules`` and load the extension-less recipe via ``SourceFileLoader`` —
the same pattern as the other recipe unit tests.

Coverage: ``_resolve_jql`` — the JQL positional resolution, including that the
framework-owned ``--tty`` flag (auto-detected by ``Browser.run()``, left in
``sys.argv``) is dropped via ``recipe_argv()`` rather than misread as JQL.
"""

import importlib.util
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-jira-mcp'


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
    name = '_browse_jira_mcp_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestResolveJql(unittest.TestCase):
    """``_resolve_jql`` reads the first positional via ``recipe_argv()``."""

    def setUp(self):
        self.r = _load_recipe()
        self._saved_argv = list(sys.argv)

    def tearDown(self):
        sys.argv[:] = self._saved_argv

    def _resolve(self, *args):
        sys.argv[:] = ['browse-jira-mcp', *args]
        return self.r._resolve_jql()

    def test_no_args_returns_default(self):
        self.assertEqual(self._resolve(), self.r._JQL_DEFAULT)

    def test_positional_jql_overrides_default(self):
        self.assertEqual(self._resolve('project = FOO'), 'project = FOO')

    def test_help_flag_is_not_jql(self):
        # -h / --help left for Browser.run()'s help auto-detect, not JQL.
        self.assertEqual(self._resolve('-h'), self.r._JQL_DEFAULT)
        self.assertEqual(self._resolve('--help'), self.r._JQL_DEFAULT)

    def test_tty_flag_is_not_jql(self):
        # ``--tty -`` / ``--tty=-`` / ``--tty /dev/pts/N`` is the framework
        # UI-device flag, not JQL: it's dropped and the default is used.
        # Without the fix, ``-`` / the device path became the JQL.
        for args in (['--tty', '-'], ['--tty=-'], ['--tty', '/dev/pts/9']):
            self.assertEqual(self._resolve(*args), self.r._JQL_DEFAULT, args)

    def test_tty_before_jql_does_not_shadow_it(self):
        # ``--tty - "project = FOO"`` strips the flag/value and still uses
        # the real JQL (the flag preceding it no longer shadows it).
        self.assertEqual(
            self._resolve('--tty', '-', 'project = FOO'), 'project = FOO')


if __name__ == '__main__':
    unittest.main()
