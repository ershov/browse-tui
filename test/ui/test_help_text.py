"""UI tests for the recipe-pluggable help text + hotkey descriptions (#79).

Exercises the end-to-end wiring: ``--help`` runs through the composer,
``?`` in the TUI shows the same composed text plus any custom actions
or intro/outro prose set by the recipe / CLI flags.
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
                     f"printf 'a\\n' | {_BIN} --root-cmd cat "
                     f"--no-children-pane")
            t.wait_for('#a a')
            t.send('?')
            t.wait_for('NAVIGATION')
            cap = t.capture()
            self.assertIn('NAVIGATION', cap)
            t.send('?')   # close help
            t.send('q')

    def test_help_screen_shows_custom_actions(self):
        with TmuxFixture(cols=80, rows=80) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\n' | {_BIN} --root-cmd cat "
                     f"--no-children-pane "
                     f"-a 'e:Edit cursor in editor:true' "
                     f"-a 'd:Delete with confirm:true'")
            t.wait_for('#a a')
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
                     f"printf 'a\\n' | {_BIN} --root-cmd cat "
                     f"--no-children-pane "
                     f"--help-intro 'PROJECT-INTRO-MARKER' "
                     f"--help-outro 'project-outro-marker'")
            t.wait_for('#a a')
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
                         f"printf 'a\\n' | {_BIN} --root-cmd cat "
                         f"--no-children-pane "
                         f"--help-intro '@{intro}'")
                t.wait_for('#a a')
                t.send('?')
                t.wait_for('FROM-FILE-INTRO')
                t.send('?')
                t.send('q')


if __name__ == '__main__':
    unittest.main()
