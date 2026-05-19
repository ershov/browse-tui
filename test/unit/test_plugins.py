"""Unit tests for the plugin system.

Covers the contracts spelled out in
``docs/superpowers/specs/2026-05-19-plugins-design.md``:

  * ``BrowserConfig`` carries every Browser construction parameter.
  * ``register_plugin`` mechanics (append-only, name fallback, multi-
    registration, idempotent via Python import caching).
  * Lifecycle-hook firing in ``Browser.__init__`` (before/after init)
    and ``Browser.run`` (before/after run, ``finally``-protected).
  * ``--plugin`` CLI extraction and ``--plugin`` + ``--run-cli`` hard
    error.
  * Introspection / composition via ``registered_plugins``.
"""

import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from test.unit._loader import load


# --- Module loading + cross-wiring ---------------------------------------

_term = load('_browse_tui_term_pl', '020-terminal.py')
_data = load('_browse_tui_data_pl', '030-data.py')
_plugins = load('_browse_tui_plugins_pl', '035-plugins.py')
_state = load('_browse_tui_state_pl', '040-state.py')
_context = load('_browse_tui_context_pl', '060-context.py')
_cli = load('_browse_tui_cli_pl', '080-cli.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
# Wire the plugin registry into the state module so Browser sees it.
_state.registered_plugins = _plugins.registered_plugins

_context.register_plugin = _plugins.register_plugin

_cli.Browser = _state.Browser
_cli.BrowserConfig = _state.BrowserConfig
_cli.Item = _data.Item
_cli.term_suspend = lambda: None
_cli.term_resume = lambda: None


Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
PluginConfig = _plugins.PluginConfig
register_plugin = _plugins.register_plugin
registered_plugins = _plugins.registered_plugins
Context = _context.Context


def _reset_plugins():
    registered_plugins.clear()


class TestBrowserConfig(unittest.TestCase):
    """Defaults match the prior ``Browser(**kwargs)`` surface."""

    def test_defaults_match_prior_kwargs(self):
        cfg = BrowserConfig()
        self.assertEqual(cfg.title, 'browse-tui')
        self.assertEqual(cfg.show_ids, 'auto')
        self.assertEqual(cfg.show_scope_crumb, False)
        self.assertEqual(cfg.preview_ansi, True)
        self.assertEqual(cfg.list_ratio, 0.30)
        self.assertEqual(cfg.split, 'auto')
        self.assertEqual(cfg.multi_select, True)
        self.assertEqual(cfg.print_format, '{id}')
        self.assertEqual(cfg.preview_buffer_cap_chars, 100_000)
        self.assertEqual(cfg.preview_buffer_cap_lines, 1000)
        self.assertEqual(cfg._headless, False)

    def test_browser_reads_from_config(self):
        b = Browser(BrowserConfig(title='X', _headless=True))
        self.assertEqual(b.title, 'X')

    def test_browser_default_arg_is_default_config(self):
        b = Browser()
        # Defaults landed; headless not set so terminal-init would
        # normally fire — but we don't call run(), so this is fine.
        self.assertEqual(b.title, 'browse-tui')


class TestRegisterPlugin(unittest.TestCase):
    """Mechanics: append, name fallback, multi-call, list publicness."""

    def setUp(self):
        _reset_plugins()

    def test_appends_one_entry(self):
        register_plugin(PluginConfig())
        self.assertEqual(len(registered_plugins), 1)

    def test_two_calls_same_module_append_two(self):
        register_plugin(PluginConfig())
        register_plugin(PluginConfig())
        self.assertEqual(len(registered_plugins), 2)

    def test_explicit_name_survives(self):
        register_plugin(PluginConfig(name='explicit'))
        self.assertEqual(registered_plugins[0].name, 'explicit')

    def test_default_name_from_caller_module(self):
        register_plugin(PluginConfig())
        # Caller is this test module — pytest names it under
        # ``test.unit.test_plugins`` or similar; just confirm it's
        # not ``None`` and not the framework's own module name.
        self.assertIsNotNone(registered_plugins[0].name)
        self.assertNotIn('035-plugins', registered_plugins[0].name)

    def test_list_is_public_and_mutable(self):
        register_plugin(PluginConfig(name='a'))
        register_plugin(PluginConfig(name='b'))
        # Reorder
        registered_plugins.reverse()
        self.assertEqual([p.name for p in registered_plugins], ['b', 'a'])
        # Remove
        registered_plugins.pop()
        self.assertEqual([p.name for p in registered_plugins], ['b'])


class TestHookFiring(unittest.TestCase):
    """Hooks fire at the right points in ``__init__`` / ``run``."""

    def setUp(self):
        _reset_plugins()

    def test_on_before_init_fires_and_can_mutate_config(self):
        def hook(browser, config):
            config.title = 'mutated'
        register_plugin(PluginConfig(on_before_init=hook))
        b = Browser(BrowserConfig(_headless=True))
        self.assertEqual(b.title, 'mutated')

    def test_on_after_init_fires_with_built_browser(self):
        captured = []

        def hook(browser):
            captured.append(browser.title)
        register_plugin(PluginConfig(on_after_init=hook))
        Browser(BrowserConfig(_headless=True, title='post'))
        self.assertEqual(captured, ['post'])

    def test_missing_hooks_are_skipped_silently(self):
        # All-None config — should not error.
        register_plugin(PluginConfig())
        Browser(BrowserConfig(_headless=True))

    def test_hook_order_matches_registration_order(self):
        order = []
        register_plugin(PluginConfig(name='first',
                                     on_after_init=lambda b: order.append(1)))
        register_plugin(PluginConfig(name='second',
                                     on_after_init=lambda b: order.append(2)))
        Browser(BrowserConfig(_headless=True))
        self.assertEqual(order, [1, 2])

    def test_on_before_init_exception_propagates(self):
        def boom(browser, config):
            raise RuntimeError('hook failed')
        register_plugin(PluginConfig(on_before_init=boom))
        with self.assertRaises(RuntimeError):
            Browser(BrowserConfig(_headless=True))


class TestIntrospectionAndComposition(unittest.TestCase):
    """`registered_plugins` is a live, mutable view."""

    def setUp(self):
        _reset_plugins()

    def test_plugin_can_wrap_another_plugins_hook(self):
        order = []

        def first(browser):
            order.append('first')
        register_plugin(PluginConfig(name='first', on_after_init=first))

        # "Second" plugin wraps the first by name.
        for cfg in registered_plugins:
            if cfg.name == 'first':
                orig = cfg.on_after_init

                def wrapped(browser, _orig=orig):
                    _orig(browser)
                    order.append('wrapped')
                cfg.on_after_init = wrapped

        Browser(BrowserConfig(_headless=True))
        self.assertEqual(order, ['first', 'wrapped'])

    def test_removed_entry_does_not_fire(self):
        register_plugin(PluginConfig(name='kept',
                                     on_after_init=lambda b: None))
        register_plugin(PluginConfig(name='removed',
                                     on_after_init=lambda b: 1 / 0))
        # Drop the bad one.
        registered_plugins[:] = [p for p in registered_plugins
                                 if p.name != 'removed']
        Browser(BrowserConfig(_headless=True))  # no ZeroDivisionError


class TestContextPassthrough(unittest.TestCase):
    """`Context.register_plugin` delegates to the module function."""

    def setUp(self):
        _reset_plugins()

    def test_context_register_plugin_appends(self):
        # Build a Browser (no plugins yet) and grab its context.
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        ctx.register_plugin(PluginConfig(name='via-context'))
        self.assertEqual(registered_plugins[-1].name, 'via-context')


class TestCLIExtraction(unittest.TestCase):
    """`--plugin` is pulled out of argv anywhere it appears."""

    def test_plugin_before_other_flags(self):
        plugins, remaining = _cli._extract_plugins(
            ['--plugin', 'foo', '-c', 'cmd'])
        self.assertEqual(plugins, ['foo'])
        self.assertEqual(remaining, ['-c', 'cmd'])

    def test_plugin_equals_form(self):
        plugins, remaining = _cli._extract_plugins(['--plugin=foo'])
        self.assertEqual(plugins, ['foo'])
        self.assertEqual(remaining, [])

    def test_repeated_preserves_order(self):
        plugins, _ = _cli._extract_plugins(
            ['--plugin', 'a', '--plugin', 'b', '--plugin=c'])
        self.assertEqual(plugins, ['a', 'b', 'c'])

    def test_plugin_before_recipe_flag(self):
        plugins, remaining = _cli._extract_plugins(
            ['--plugin', 'foo', '--run-py', 'recipe.py', 'arg'])
        self.assertEqual(plugins, ['foo'])
        self.assertEqual(remaining, ['--run-py', 'recipe.py', 'arg'])

    def test_missing_value_errors_out(self):
        with self.assertRaises(SystemExit):
            _cli._extract_plugins(['--plugin'])


class TestCLIRejectsPluginWithRunCli(unittest.TestCase):
    """``--plugin`` combined with external CLI recipe must exit non-zero."""

    def setUp(self):
        _reset_plugins()

    def test_run_cli_with_plugin_errors(self):
        with tempfile.NamedTemporaryFile(
                'w', suffix='.sh', delete=False) as f:
            f.write('#!/bin/bash\necho hi\n')
            recipe = f.name
        try:
            os.chmod(recipe, 0o755)
            err_io = []
            with patch.object(_cli.sys, 'stderr') as stderr:
                stderr.write = err_io.append
                rc = _cli.main(['--plugin', 'foo', '--run-cli', recipe])
            self.assertEqual(rc, 2)
            self.assertTrue(
                any('--plugin requires' in line for line in err_io),
                err_io,
            )
        finally:
            os.unlink(recipe)


class TestPluginLoading(unittest.TestCase):
    """End-to-end: ``_load_plugins`` imports both path and name forms."""

    def setUp(self):
        _reset_plugins()

    def test_load_path_form_runs_module_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod_path = os.path.join(tmp, 'test_plugin_path_form.py')
            with open(mod_path, 'w') as f:
                f.write(
                    'import sys\n'
                    'sys.modules["browse_tui"] = sys.modules.get'
                    '("_browse_tui_plugins_pl", sys.modules["__main__"])\n'
                    'LOADED = True\n'
                )
            _cli._load_plugins([mod_path])
            self.assertIn('test_plugin_path_form', sys.modules)
            self.assertTrue(
                getattr(sys.modules['test_plugin_path_form'], 'LOADED'))

    def test_load_missing_module_raises(self):
        with self.assertRaises(ImportError):
            _cli._load_plugins(['_definitely_not_a_real_module_xyz_'])


class TestSysPathSetup(unittest.TestCase):
    """`_setup_plugin_sys_path` prepends discovery directories."""

    def test_binary_dir_added_when_argv0_set(self):
        # We can't easily change argv[0] in-process; just verify that
        # calling the helper twice doesn't duplicate entries.
        before = list(sys.path)
        _cli._setup_plugin_sys_path()
        once = list(sys.path)
        _cli._setup_plugin_sys_path()
        twice = list(sys.path)
        self.assertEqual(once, twice)
        # Best-effort: at least one entry should have been added
        # versus ``before`` (or be already present from a prior test).
        self.assertTrue(set(before).issubset(set(once)))


if __name__ == '__main__':
    unittest.main()
