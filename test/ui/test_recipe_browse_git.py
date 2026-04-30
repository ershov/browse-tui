"""UI tests for the ``recipes/browse-git`` commit / file / diff recipe.

The recipe runs ``git log`` / ``git show`` in the cwd. To keep tests
hermetic we initialise a small temp repo with two commits, ``cd`` into
it via the tmux fixture's launch line, and assert that the commit
list and per-commit file lists render.

The shebang ``#!/usr/bin/env -S browse-tui --python`` requires the
binary to be on PATH, which is fragile in tests; instead we invoke
``./browse-tui --python recipes/browse-git`` directly so the tests
are independent of the user's PATH.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-git')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not shutil.which('git'):
        raise unittest.SkipTest('git not available; browse-git tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _make_repo(tmpdir):
    """Initialise a temp git repo with two commits.

    Sets ``user.name`` / ``user.email`` locally so the commits succeed
    without depending on the test runner's global git config. Returns
    the repo directory.
    """
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
        'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
    }
    def git(*args):
        subprocess.run(['git', '-C', tmpdir, *args], check=True,
                       capture_output=True, env=env)
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    with open(os.path.join(tmpdir, 'alpha.txt'), 'w') as f:
        f.write('alpha\n')
    git('add', 'alpha.txt')
    git('commit', '-q', '-m', 'first commit add alpha')
    with open(os.path.join(tmpdir, 'beta.txt'), 'w') as f:
        f.write('beta\n')
    git('add', 'beta.txt')
    git('commit', '-q', '-m', 'second commit add beta')
    return tmpdir


class TestBrowseGit(unittest.TestCase):

    def test_lists_commits(self):
        """The recipe lists commits from the cwd's git repo."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                # cd into the repo before launching so git operations
                # use it as the working tree.
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('first commit add alpha', timeout=5.0)
                t.wait_for('second commit add beta', timeout=5.0)
                t.send('q')

    def test_drills_into_commit_files(self):
        """Right-arrow on a commit reveals its changed files."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('second commit add beta', timeout=5.0)
                # Cursor starts on the newest commit; expand it.
                t.send('Right')
                t.wait_for('beta.txt', timeout=5.0)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
