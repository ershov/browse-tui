"""UI tests for the recipe-pluggable help text + hotkey descriptions (#79, #91).

Exercises the end-to-end wiring: ``--help`` runs through the composer,
``?`` in the TUI shows the same composed text plus any custom actions
or intro/outro prose set by the recipe / CLI flags. ``-h`` / ``--help``
on a recipe (e.g. ``./recipes/browse-fs -h``) auto-detects in
``Browser.run()`` and short-circuits to print recipe-aware help (#91).
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestHelpFlag(unittest.TestCase):
    """``browse-tui --help`` includes USAGE + the dynamic key list."""

    def test_help_flag_shows_composed_text(self):
        out = subprocess.run(
            [_BIN, '--help'],
            capture_output=True, text=True, timeout=5,
        ).stdout
        self.assertIn('usage:', out.lower())
        self.assertIn('NAVIGATION', out)
        self.assertIn('PREVIEW', out)
        self.assertIn('SEARCH', out)
        self.assertIn('OTHER', out)

    def test_help_flag_includes_custom_actions(self):
        out = subprocess.run(
            [_BIN, '--help', '-a', 'e:Edit:true'],
            capture_output=True, text=True, timeout=5,
        ).stdout
        self.assertIn('CUSTOM ACTIONS', out)
        self.assertIn('Edit', out)

    def test_help_flag_includes_intro_and_outro(self):
        out = subprocess.run(
            [_BIN, '--help',
             '--help-intro', 'PROJECT-INTRO-MARKER',
             '--help-outro', 'project-outro-marker'],
            capture_output=True, text=True, timeout=5,
        ).stdout
        self.assertIn('PROJECT-INTRO-MARKER', out)
        self.assertIn('project-outro-marker', out)
        # Intro is above NAVIGATION; outro is below.
        i_intro = out.find('PROJECT-INTRO-MARKER')
        i_nav = out.find('NAVIGATION')
        i_outro = out.find('project-outro-marker')
        self.assertLess(i_intro, i_nav)
        self.assertGreater(i_outro, i_nav)

    def test_help_intro_at_path_loads_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            intro = os.path.join(tmp, 'intro.txt')
            with open(intro, 'w') as f:
                f.write('FROM-FILE-INTRO\n')
            out = subprocess.run(
                [_BIN, '--help', '--help-intro', f'@{intro}'],
                capture_output=True, text=True, timeout=5,
            ).stdout
            self.assertIn('FROM-FILE-INTRO', out)


class TestHelpScreenInTui(unittest.TestCase):
    """``?`` inside the running TUI shows the composed help body."""

    def test_help_screen_shows_section_headers(self):
        with TmuxFixture(cols=80, rows=80) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\n' | {_BIN} --show-ids always --root-cmd cat "
                     f"--no-children-pane")
            t.wait_for('a a')
            t.send('?')
            t.wait_for('NAVIGATION')
            cap = t.capture()
            self.assertIn('NAVIGATION', cap)
            t.send('?')   # close help
            t.send('q')

    def test_help_screen_shows_custom_actions(self):
        with TmuxFixture(cols=80, rows=80) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\n' | {_BIN} --show-ids always --root-cmd cat "
                     f"--no-children-pane "
                     f"-a 'e:Edit cursor in editor:true' "
                     f"-a 'd:Delete with confirm:true'")
            t.wait_for('a a')
            t.send('?')
            t.wait_for('CUSTOM ACTIONS')
            cap = t.capture()
            self.assertIn('Edit cursor in editor', cap)
            self.assertIn('Delete with confirm', cap)
            t.send('?')   # close help
            t.send('q')

    def test_help_intro_and_outro_in_help_screen(self):
        with TmuxFixture(cols=80, rows=80) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\n' | {_BIN} --show-ids always --root-cmd cat "
                     f"--no-children-pane "
                     f"--help-intro 'PROJECT-INTRO-MARKER' "
                     f"--help-outro 'project-outro-marker'")
            t.wait_for('a a')
            t.send('?')
            t.wait_for('PROJECT-INTRO-MARKER')
            t.wait_for('project-outro-marker')
            t.send('?')
            t.send('q')

    def test_help_intro_at_path_in_help_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            intro = os.path.join(tmp, 'intro.md')
            with open(intro, 'w') as f:
                f.write('FROM-FILE-INTRO\n')
            with TmuxFixture(cols=80, rows=80) as t:
                t.launch('bash', '-c',
                         f"printf 'a\\n' | {_BIN} --show-ids always --root-cmd cat "
                         f"--no-children-pane "
                         f"--help-intro '@{intro}'")
                t.wait_for('a a')
                t.send('?')
                t.wait_for('FROM-FILE-INTRO')
                t.send('?')
                t.send('q')

    def test_cursor_move_resets_preview_scroll(self):
        """Scrolling the preview, then moving cursor, lands at top of new preview.

        Without the reset, the new item's preview would scroll on with
        the previous item's offset, hiding the first lines of content.

        Markers ``HEAD_a``/``HEAD_b`` only appear on line 1 of each
        preview; using a distinctive token (rather than ``line 1``)
        avoids substring collisions with ``line 10``, ``line 11`` …
        when the scroll ticks past ten.
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(
                _BIN,
                '--children-cmd', 'printf "a\\nb\\n"',
                '--preview-cmd',
                'printf "HEAD_%s\\n" "$TUI_ID"; '
                'i=2; while [ $i -le 30 ]; do '
                '  printf "%s mid %d\\n" "$TUI_ID" "$i"; '
                '  i=$((i+1)); '
                'done',
                '--no-children-pane',
                '--show-ids', 'always',
            )
            t.wait_for('a a')
            t.wait_for('HEAD_a', timeout=5.0)
            # Push the preview pane down by several lines. The
            # browse-tui ``shift-down`` action is bound to the terminal's
            # S-Down sequence.
            for _ in range(5):
                t.send('S-Down')
            # Confirm we actually scrolled — line 1 leaves the visible
            # window once enough lines have shifted up.
            cap_scrolled = t.wait_stable()
            self.assertNotIn('HEAD_a', cap_scrolled)
            # Move cursor to b — preview pane should reset to the top.
            t.send('j')
            t.wait_for('HEAD_b', timeout=5.0)
            cap = t.wait_stable()
            self.assertIn('HEAD_b', cap)
            t.send('q')

    def test_cursor_move_dismisses_help_and_shows_preview(self):
        """Navigating with the cursor closes the help overlay.

        Open ``?``, move down, and confirm the help body is gone and
        the per-item preview for the new cursor row took its place.
        Without the fix, the help overlay stayed put even after the
        cursor moved — leaving the user staring at a stale screen.

        Uses ``--children-cmd`` (lazy mode) so the initial preview
        fetch fires on startup; eager mode (``--root-cmd``) leaves the
        first preview unrequested until a cursor move, which would
        muddy this test's "before" state.
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(
                _BIN,
                '--children-cmd', 'printf "a\\nb\\n"',
                '--preview-cmd', 'printf "PREVIEW-FOR-%s" "$TUI_ID"',
                '--no-children-pane',
                '--show-ids', 'always',
            )
            t.wait_for('a a')
            t.wait_for('PREVIEW-FOR-a', timeout=5.0)
            t.send('?')
            t.wait_for('NAVIGATION')
            t.send('j')   # cursor → b
            t.wait_for('PREVIEW-FOR-b', timeout=5.0)
            cap = t.wait_stable()
            self.assertNotIn('NAVIGATION', cap)
            self.assertIn('PREVIEW-FOR-b', cap)
            t.send('q')


class TestRecipeHelpFlag(unittest.TestCase):
    """Recipes that don't argparse their own argv get -h/--help for free.

    Browser.run() auto-detects the help flag in sys.argv and prints
    recipe-aware help (intro/outro + CUSTOM ACTIONS) without entering
    the TUI loop. Without the fix, ``-h`` would fall through to the
    TUI as a meaningless argv entry and the user would be dropped into
    the interactive mode.
    """

    def test_recipe_dash_h_short_form_shows_help(self):
        # browse-fs is a recipe with help_intro AND custom actions —
        # exercises both surfaces in a single run.
        out = subprocess.run(
            [_BIN, '--python',
             os.path.join(_REPO, 'recipes/browse-fs'),
             '--', '-h'],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(out.returncode, 0)
        # Recipe's _HELP_INTRO leads with this string.
        self.assertIn('browse-fs', out.stdout)
        # Default section headers from the composer.
        self.assertIn('NAVIGATION', out.stdout)
        # Recipe-defined actions surface in CUSTOM ACTIONS.
        self.assertIn('CUSTOM ACTIONS', out.stdout)
        self.assertIn('Edit cursor in $EDITOR', out.stdout)

    def test_recipe_dash_dash_help_long_form_shows_help(self):
        out = subprocess.run(
            [_BIN, '--python',
             os.path.join(_REPO, 'recipes/browse-fs'),
             '--', '--help'],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(out.returncode, 0)
        self.assertIn('CUSTOM ACTIONS', out.stdout)
        self.assertIn('Delete (with confirmation)', out.stdout)

    def test_recipe_help_via_top_level_h_flag(self):
        # When invoking ``browse-tui --python <recipe> -h`` (no ``--``
        # separator), argparse claims the ``-h`` and sets args.help.
        # The dispatcher must forward it to the recipe — without that
        # forwarding, the recipe's Browser.run() would never see the
        # flag and the user would land in the TUI.
        out = subprocess.run(
            [_BIN, '--python',
             os.path.join(_REPO, 'recipes/browse-fs'),
             '-h'],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(out.returncode, 0)
        self.assertIn('CUSTOM ACTIONS', out.stdout)
        self.assertIn('Edit cursor in $EDITOR', out.stdout)

    def test_recipe_help_via_top_level_long_help_flag(self):
        out = subprocess.run(
            [_BIN, '--python',
             os.path.join(_REPO, 'recipes/browse-fs'),
             '--help'],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(out.returncode, 0)
        self.assertIn('CUSTOM ACTIONS', out.stdout)

    def test_recipe_with_own_argparse_keeps_its_own_help(self):
        # Recipes that argparse their own argv consume -h before
        # Browser.run() is called; auto-detect must not interfere.
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, 'recipe.py')
            with open(script, 'w') as f:
                f.write(
                    "import argparse, sys\n"
                    "from browse_tui import Browser, Item\n"
                    "p = argparse.ArgumentParser(prog='custom_recipe',\n"
                    "    description='RECIPE-OWN-HELP-MARKER')\n"
                    "p.add_argument('--mode', default='default')\n"
                    "args = p.parse_args()\n"
                    "b = Browser(get_children=lambda _id: [Item('x')])\n"
                    "sys.exit(b.run())\n"
                )
            out = subprocess.run(
                [_BIN, '--python', script, '--', '-h'],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(out.returncode, 0)
            # The recipe's own argparse handled -h and emitted its own
            # description; Browser.run()'s auto-detect never fired,
            # so we should NOT see the composer's NAVIGATION block.
            self.assertIn('RECIPE-OWN-HELP-MARKER', out.stdout)
            self.assertNotIn('NAVIGATION', out.stdout)
            self.assertNotIn('CUSTOM ACTIONS', out.stdout)


if __name__ == '__main__':
    unittest.main()
