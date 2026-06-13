"""UI tests for the ``recipes/browse-fs`` filesystem-browser recipe.

Drives the recipe under tmux against a temp directory, verifying that
entries render and that the background mtime watcher picks up an
external file creation.

The shebang ``#!/usr/bin/env -S browse-tui --run-py`` requires the
binary to be on PATH, which is fragile in tests; instead we invoke
``./browse-tui --run-py recipes/browse-fs -- <tmp>`` directly so the
test is independent of the user's PATH.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-fs')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestBrowseFs(unittest.TestCase):

    def test_lists_directory_entries(self):
        """The recipe enumerates dirs and files with expected styling."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'sub'))
            with open(os.path.join(tmp, 'a.txt'), 'w') as f:
                f.write('hello')
            with open(os.path.join(tmp, 'b.txt'), 'w') as f:
                f.write('world')
            with TmuxFixture(cols=120, rows=30) as t:
                t.launch(_BIN, '--run-py', _RECIPE, tmp)
                t.wait_for('a.txt')
                t.wait_for('b.txt')
                t.wait_for('sub/')
                t.send('q')

    def test_watcher_picks_up_new_file(self):
        """An externally-created file appears within the watcher's poll cadence."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'one.txt'), 'w') as f:
                f.write('1')
            with TmuxFixture(cols=120, rows=30) as t:
                t.launch(_BIN, '--run-py', _RECIPE, tmp)
                t.wait_for('one.txt')
                # The watcher captures its mtime baseline on its first
                # tick (~1s after start); any dir mutation before then is
                # absorbed silently. Wait past the first tick before
                # mutating so the next tick observes a real change.
                time.sleep(1.5)
                with open(os.path.join(tmp, 'two.txt'), 'w') as f:
                    f.write('2')
                # Watcher polls every ~1s; the main loop sometimes only
                # repaints on the next keystroke after async work lands,
                # so fall back to a forced redraw if we don't see the
                # update on the first try.
                try:
                    t.wait_for('two.txt', timeout=3.0)
                except AssertionError:
                    t.redraw()
                    t.wait_for('two.txt', timeout=2.0)
                t.send('q')


class TestBrowseFsStdin(unittest.TestCase):
    """``browse-fs -`` displays the stdin path list as the root level.

    End-to-end against the shipped binary: we pipe a newline-separated
    path list into the recipe (``printf '…' | browse-tui … browse-fs
    -``) with the shell ``cd``'d into the temp dir so the relative paths
    resolve there. Because the recipe slurps ``sys.stdin`` BEFORE the UI
    starts, the pipe's EOF is reached during ingest and the parsed list
    drives the tree — the UI itself stays on the tmux pane's terminal.
    """

    def _launch_piped(self, t, tmp, lines):
        """``cd tmp && printf '<lines>' | browse-tui --run-py browse-fs -``."""
        payload = ''.join(f'{line}\n' for line in lines)
        cmd = (
            f'cd {shlex.quote(tmp)} && '
            f'printf %s {shlex.quote(payload)} | '
            f'{shlex.quote(_BIN)} --run-py {shlex.quote(_RECIPE)} -'
        )
        t.send_line(cmd)

    def test_piped_mixed_list_roots_expand_and_preview(self):
        # A mixed list: a file, a directory (with a child), a missing
        # path, and a path with spaces — all relative to the temp dir.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'adir'))
            with open(os.path.join(tmp, 'adir', 'child.txt'), 'w') as f:
                f.write('child body')
            with open(os.path.join(tmp, 'plain.txt'), 'w') as f:
                f.write('plain file body')
            with open(os.path.join(tmp, 'a file.txt'), 'w') as f:
                f.write('spaced body')
            with TmuxFixture(cols=120, rows=30) as t:
                self._launch_piped(
                    t, tmp,
                    ['plain.txt', 'adir', 'missing-xyz', 'a file.txt'])
                # The roots are exactly the piped entries (verbatim labels;
                # the missing one carries the dim marker).
                t.wait_for('plain.txt', timeout=6.0)
                t.wait_for('adir', timeout=4.0)
                t.wait_for('a file.txt', timeout=4.0)
                # The missing row renders with a dim ``[missing]`` chip
                # before the verbatim label.
                t.wait_for(re.compile(r'\[missing\]\s+missing-xyz'),
                           timeout=4.0)
                # The cursor starts on the first root (plain.txt) → its
                # preview is the file head.
                t.wait_for('plain file body', timeout=4.0)
                # Expanding the directory reveals its real child.
                t.wait_for('adir', timeout=4.0)
                t.send('Down')                 # move onto adir
                t.send('Right')                # expand it
                t.wait_for('child.txt', timeout=4.0)
                t.send('q')

    def test_piped_without_dash_auto_engages_stdin(self):
        # ``cmd | browse-fs`` with NO ``-`` on the command line: the recipe
        # auto-detects the piped (non-tty) stdin and shows the path list
        # exactly as the explicit ``-`` form would.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'plain.txt'), 'w') as f:
                f.write('plain file body')
            payload = 'plain.txt\n'
            cmd = (
                f'cd {shlex.quote(tmp)} && '
                f'printf %s {shlex.quote(payload)} | '
                f'{shlex.quote(_BIN)} --run-py {shlex.quote(_RECIPE)}'
            )
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(cmd)
                # The piped path is the root (stdin mode) without typing ``-``.
                t.wait_for('plain.txt', timeout=6.0)
                t.wait_for('plain file body', timeout=4.0)
                t.send('q')

    def test_missing_path_preview_shows_error_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TmuxFixture(cols=120, rows=30) as t:
                self._launch_piped(t, tmp, ['ghost.txt'])
                # The lone missing row renders with the dim chip.
                t.wait_for(re.compile(r'\[missing\]\s+ghost.txt'),
                           timeout=6.0)
                # The cursor lands on it; the preview shows the underlying
                # stat error (its text, distinct from the row's chip)
                # rather than crashing.
                t.wait_for(re.compile(r'No such file'), timeout=4.0)
                t.send('q')

    def test_empty_stdin_clean_ui(self):
        # Empty input ⇒ an empty root list. The recipe must not crash;
        # the framework shows its "no items" state and quits cleanly.
        with tempfile.TemporaryDirectory() as tmp:
            with TmuxFixture(cols=120, rows=30) as t:
                cmd = (
                    f'cd {shlex.quote(tmp)} && '
                    f'printf %s "" | '
                    f'{shlex.quote(_BIN)} --run-py {shlex.quote(_RECIPE)} -'
                )
                t.send_line(cmd)
                # The title bar renders (the UI came up) and stays stable.
                t.wait_for('browse-fs', timeout=6.0)
                t.wait_stable(timeout=3.0)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
