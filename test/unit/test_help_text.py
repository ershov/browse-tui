"""Tests for the recipe-pluggable help-text composer (#79, #91).

Covers four surfaces:

  * ``Action.section`` — the new field on the dataclass and the values
    that ``default_actions()`` tags onto each built-in binding.
  * ``compose_help_text(browser, …)`` — the dynamic help-screen builder
    that replaces the static ``_HELP_TEXT`` block. Section headers,
    intro/outro placement, custom-actions visibility.
  * ``_resolve_help_text(value)`` — the ``--help-intro`` /
    ``--help-outro`` flag-value resolver: literal strings, ``@PATH``
    file loads, ``@@`` escapes, and missing-file fatal handling.
  * ``Browser.run()`` auto-detect of ``-h`` / ``--help`` in
    ``sys.argv`` — recipes that don't argparse their own argv get
    recipe-aware help for free without entering the TUI loop (#91).
"""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')
_actions = load('_browse_tui_actions', '070-actions.py')
_cli = load('_browse_tui_cli', '080-cli.py')

# Wire up the cross-module names the concatenated production build
# resolves automatically. Mirrors the test_actions.py setup.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
# Browser.run() looks up compose_help_text as a bare global for the
# -h/--help auto-detect short-circuit (#91); inject it for tests.
_state.compose_help_text = _render.compose_help_text
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
_render.VisibleEntry = _state.VisibleEntry
_render.default_actions = _actions.default_actions
_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.current_scope = _state.current_scope
_actions._search_find = _state._search_find
_actions._search_jump_nearest = _state._search_jump_nearest
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions._resolve_landing = _state._resolve_landing
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode
_cli.Browser = _state.Browser
_cli.Action = _actions.Action


Action = _actions.Action
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
default_actions = _actions.default_actions
compose_help_text = _render.compose_help_text
_resolve_help_text = _cli._resolve_help_text


def _make_browser(**kw):
    """Build a headless Browser; tests don't run workers."""
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


class _StopRun(Exception):
    """Sentinel raised by patched ``start_workers`` to abort ``run()``.

    Lets the test verify the auto-detect predicate without dragging in
    the rest of the TUI loop (Context, dispatch_key, render_full, …)
    which aren't all wired in this test loader.
    """


# ---- Action.section --------------------------------------------------------


class TestActionSectionField(unittest.TestCase):
    """Action gains a ``section`` field used by the help composer."""

    def test_default_actions_have_known_sections(self):
        sections = {a.section for a in default_actions() if a.section}
        # Every built-in default action should land in one of the five
        # named sections so the composer renders them.
        self.assertIn('NAVIGATION', sections)
        self.assertIn('PREVIEW', sections)
        self.assertIn('SEARCH', sections)
        self.assertIn('SELECTION', sections)
        self.assertIn('OTHER', sections)

    def test_every_default_action_has_a_section(self):
        # Belt-and-braces: catch a future default-action addition that
        # forgets to set ``section`` (so it would silently disappear
        # from the help screen).
        for a in default_actions():
            self.assertTrue(
                a.section,
                f'default action {a.key!r} missing section= tag',
            )

    def test_user_action_default_section_is_empty(self):
        a = Action('e', 'Edit', lambda c: None, 'cursor')
        self.assertEqual(a.section, '')

    def test_user_action_section_kwarg_supported(self):
        # Section= is a real positional/keyword arg on Action.
        a = Action('e', 'Edit', lambda c: None, 'cursor', 'CUSTOM')
        self.assertEqual(a.section, 'CUSTOM')


# ---- compose_help_text -----------------------------------------------------


class TestComposeHelpText(unittest.TestCase):
    """``compose_help_text(browser)`` builds the dynamic help body."""

    def test_default_help_has_section_headers(self):
        b = _make_browser()
        text = compose_help_text(b)
        self.assertIn('NAVIGATION', text)
        self.assertIn('PREVIEW', text)
        self.assertIn('SEARCH', text)
        self.assertIn('SELECTION', text)
        self.assertIn('OTHER', text)

    def test_default_help_includes_well_known_keys(self):
        b = _make_browser()
        text = compose_help_text(b)
        # Spot-check a couple of bindings from each section.
        self.assertIn('Cursor down', text)
        self.assertIn('Toggle preview pane', text)
        self.assertIn('Enter search mode', text)
        self.assertIn('Quit', text)

    def test_default_help_includes_preview_ansi_toggle(self):
        # The capital-R binding registered in #244 should surface in the
        # PREVIEW section of the auto-generated help body.
        b = _make_browser()
        text = compose_help_text(b)
        self.assertIn('Toggle preview ANSI colours', text)

    def test_no_actions_no_custom_section(self):
        b = _make_browser()
        text = compose_help_text(b)
        self.assertNotIn('CUSTOM ACTIONS', text)

    def test_actions_render_in_custom_section(self):
        b = _make_browser(actions=[
            Action('e', 'Edit in editor', lambda c: None, 'cursor'),
            Action('d', 'Delete with confirm', lambda c: None, 'targets'),
        ])
        text = compose_help_text(b)
        self.assertIn('CUSTOM ACTIONS', text)
        self.assertIn('Edit in editor', text)
        self.assertIn('Delete with confirm', text)

    def test_actions_with_blank_label_skipped(self):
        b = _make_browser(actions=[
            Action('x', '', lambda c: None, 'cursor'),
        ])
        text = compose_help_text(b)
        # No label means nothing useful to surface — the section header
        # should not appear because its rows would be empty.
        self.assertNotIn('CUSTOM ACTIONS', text)

    def test_help_intro_appears_at_top(self):
        b = _make_browser(help_intro='Welcome to the browser')
        text = compose_help_text(b)
        idx_intro = text.find('Welcome to the browser')
        idx_nav = text.find('NAVIGATION')
        self.assertGreaterEqual(idx_intro, 0)
        self.assertLess(idx_intro, idx_nav)

    def test_help_outro_appears_at_bottom(self):
        b = _make_browser(help_outro='Goodbye for now')
        text = compose_help_text(b)
        idx_outro = text.find('Goodbye for now')
        idx_other = text.rfind('OTHER')
        self.assertGreaterEqual(idx_outro, 0)
        self.assertGreater(idx_outro, idx_other)

    def test_intro_and_outro_both_present(self):
        b = _make_browser(
            help_intro='INTRO_MARKER',
            help_outro='OUTRO_MARKER',
        )
        text = compose_help_text(b)
        i_intro = text.find('INTRO_MARKER')
        i_outro = text.find('OUTRO_MARKER')
        self.assertGreaterEqual(i_intro, 0)
        self.assertGreaterEqual(i_outro, 0)
        self.assertLess(i_intro, i_outro)

    def test_no_leading_or_trailing_blank_lines(self):
        b = _make_browser()
        text = compose_help_text(b)
        # The composer should produce exactly one trailing newline and
        # no leading blank line.
        self.assertFalse(text.startswith('\n'))
        self.assertTrue(text.endswith('\n'))
        # No double-trailing-newline either.
        self.assertFalse(text.endswith('\n\n'))

    def test_include_usage_flag_is_accepted(self):
        # The flag is currently a no-op in the composer (the argparse
        # usage block is prepended by main()), but the API surface
        # accepts it — make sure both shapes parse cleanly.
        b = _make_browser()
        a = compose_help_text(b, include_usage=True)
        c = compose_help_text(b, include_usage=False)
        # Same body either way in this phase.
        self.assertEqual(a, c)


# ---- _resolve_help_text ----------------------------------------------------


class TestResolveHelpText(unittest.TestCase):
    """``--help-intro`` / ``--help-outro`` value resolution."""

    def test_plain_text_returned_verbatim(self):
        self.assertEqual(
            _resolve_help_text('Welcome to browse-tui'),
            'Welcome to browse-tui',
        )

    def test_at_path_loads_file(self):
        with tempfile.NamedTemporaryFile(
            'w', delete=False, suffix='.txt',
        ) as f:
            f.write('From file\n')
            path = f.name
        try:
            self.assertEqual(
                _resolve_help_text(f'@{path}'),
                'From file\n',
            )
        finally:
            os.unlink(path)

    def test_double_at_escapes(self):
        # @@foo → literal @foo (no file lookup).
        self.assertEqual(_resolve_help_text('@@foo'), '@foo')
        self.assertEqual(_resolve_help_text('@@'), '@')

    def test_missing_file_raises_systemexit(self):
        with self.assertRaises(SystemExit):
            _resolve_help_text('@/nonexistent/path/xyz.txt')

    def test_empty_string_returned_verbatim(self):
        # Edge case: empty doesn't start with @, so it round-trips.
        self.assertEqual(_resolve_help_text(''), '')


# ---- Browser.run() -h/--help auto-detect (#91) ----------------------------


class TestRunDetectsHelpFlag(unittest.TestCase):
    """``Browser.run()`` short-circuits on ``-h`` / ``--help`` in sys.argv.

    Recipes that don't argparse their own argv (the common case) need
    the help flag to surface recipe-aware help instead of dropping the
    user into the TUI with the flag as a meaningless arg. Recipes that
    do argparse first are unaffected — argparse strips the flag from
    sys.argv before ``run()`` is called.
    """

    def _run_with_argv(self, browser, argv):
        """Invoke ``browser.run()`` with sys.argv set to ``argv``.

        Captures stdout and restores sys.argv on exit.
        """
        old_argv = sys.argv
        sys.argv = argv
        try:
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                rc = browser.run()
            return rc, out.getvalue()
        finally:
            sys.argv = old_argv

    def test_run_returns_0_when_h_in_argv(self):
        b = _make_browser()
        rc, out = self._run_with_argv(b, ['recipe', '-h'])
        self.assertEqual(rc, 0)
        # Composed help text always carries the default section headers.
        self.assertIn('NAVIGATION', out)
        self.assertIn('OTHER', out)

    def test_run_returns_0_when_long_help_in_argv(self):
        b = _make_browser()
        rc, out = self._run_with_argv(b, ['recipe', '--help'])
        self.assertEqual(rc, 0)
        self.assertIn('NAVIGATION', out)

    def test_run_with_custom_action_shows_in_help(self):
        # The whole point of #91: recipes' own actions surface in -h
        # output. Without the auto-detect, the recipe's run() would
        # enter the TUI and the custom action labels would be lost.
        b = _make_browser(actions=[
            Action('z', 'My custom thing', lambda c: None, 'cursor'),
        ])
        rc, out = self._run_with_argv(b, ['recipe', '-h'])
        self.assertEqual(rc, 0)
        self.assertIn('CUSTOM ACTIONS', out)
        self.assertIn('My custom thing', out)

    def test_run_with_help_intro_shows_intro(self):
        # Recipe-supplied help_intro must appear when -h is auto-detected.
        b = _make_browser(help_intro='RECIPE-INTRO-MARKER')
        rc, out = self._run_with_argv(b, ['recipe', '-h'])
        self.assertEqual(rc, 0)
        self.assertIn('RECIPE-INTRO-MARKER', out)

    def test_run_with_help_outro_shows_outro(self):
        b = _make_browser(help_outro='RECIPE-OUTRO-MARKER')
        rc, out = self._run_with_argv(b, ['recipe', '--help'])
        self.assertEqual(rc, 0)
        self.assertIn('RECIPE-OUTRO-MARKER', out)

    def test_run_help_in_middle_position(self):
        # ``recipe /tmp -h`` — the help flag isn't necessarily the
        # first argv entry; auto-detect must scan all positions.
        b = _make_browser()
        rc, out = self._run_with_argv(b, ['recipe', '/tmp', '-h'])
        self.assertEqual(rc, 0)
        self.assertIn('NAVIGATION', out)

    def test_run_no_help_flag_does_not_short_circuit(self):
        # Sanity: without -h/--help, the auto-detect must not fire.
        # Calling run() directly here would proceed to start_workers
        # and then to the TUI loop body whose cross-module symbols
        # (Context, dispatch_key, render_full, …) aren't all wired in
        # this test loader. So we test the detection at the boundary
        # by patching start_workers to raise — if auto-detect fired,
        # start_workers would never be called and the exception
        # wouldn't propagate.
        b = _make_browser()
        called = []

        def _raise():
            called.append(True)
            raise _StopRun()

        with patch.object(b, 'start_workers', side_effect=_raise):
            old_argv = sys.argv
            sys.argv = ['recipe']
            try:
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    try:
                        b.run()
                    except _StopRun:
                        pass
            finally:
                sys.argv = old_argv
        # start_workers was called → auto-detect did NOT fire.
        self.assertTrue(called)
        # And no help text was printed.
        self.assertNotIn('NAVIGATION', out.getvalue())

    def test_run_does_not_match_h_inside_other_args(self):
        # ``--help-intro`` and other strings containing 'h' must NOT
        # trigger the short-circuit: we match exact ``-h`` / ``--help``
        # tokens via membership, not substring.
        b = _make_browser()
        called = []

        def _raise():
            called.append(True)
            raise _StopRun()

        with patch.object(b, 'start_workers', side_effect=_raise):
            old_argv = sys.argv
            sys.argv = ['recipe', '--help-intro', 'text', '--mode', 'h']
            try:
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    try:
                        b.run()
                    except _StopRun:
                        pass
            finally:
                sys.argv = old_argv
        self.assertTrue(called)
        self.assertNotIn('NAVIGATION', out.getvalue())


if __name__ == '__main__':
    unittest.main()
