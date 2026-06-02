"""UI tests for the ``recipes/browse-git`` commit / file / diff recipe.

The recipe runs ``git log`` / ``git show`` in the cwd. To keep tests
hermetic we initialise a small temp repo with two commits, ``cd`` into
it via the tmux fixture's launch line, and assert that the commit
list and per-commit file lists render.

The shebang ``#!/usr/bin/env -S browse-tui --run-py`` requires the
binary to be on PATH, which is fragile in tests; instead we invoke
``./browse-tui --run-py recipes/browse-git`` directly so the tests
are independent of the user's PATH.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
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


def _make_repo_with_n_commits(tmpdir, n):
    """Initialise a temp git repo with ``n`` single-file commits.

    Each commit touches a unique file ``f{i}.txt`` so the per-commit
    file list is non-empty. Used by tests that need cursor room to
    scroll through many entries.
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
    for i in range(n):
        path = os.path.join(tmpdir, f'f{i:03d}.txt')
        with open(path, 'w') as f:
            f.write(f'line {i}\n')
        git('add', f'f{i:03d}.txt')
        git('commit', '-q', '-m', f'commit {i:03d}')
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
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('first commit add alpha', timeout=5.0)
                t.wait_for('second commit add beta', timeout=5.0)
                t.send('q')

    def test_drills_into_commit_files(self):
        """Right-arrow on a commit reveals its changed files."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('second commit add beta', timeout=5.0)
                # Cursor starts on the newest commit; expand it.
                t.send('Right')
                t.wait_for('beta.txt', timeout=5.0)
                t.send('q')

    def test_branch_head_shows_decoration_chip(self):
        """The newest commit's row carries its ``HEAD -> main`` chip text.

        ``_make_repo`` leaves HEAD on branch ``main``, so ``git log``'s
        ``%D`` for the newest commit is ``HEAD -> main`` → the recipe
        renders a green ``[main]`` decoration chip after the subject.
        tmux strips SGR but keeps the chip text, so we assert the row
        carrying the subject also shows ``main``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('second commit add beta', timeout=5.0)
                # The decoration chip + the subject share the newest
                # commit's row; assert the row containing the subject
                # also contains the branch name.
                cap = t.wait_for('second commit add beta', timeout=5.0)
                row = next(ln for ln in cap.splitlines()
                           if 'second commit add beta' in ln)
                self.assertIn('main', row)
                t.send('q')

    def test_enter_toggles_file_list(self):
        """Enter opens a commit's file list, and a second Enter closes it.

        The standalone Children pane always shows the cursor's children,
        so we assert on the *tree* instead: an expanded commit shows its
        file as an indented ``[A] beta.txt`` row in the list pane, and the
        collapse removes that indented row. The commit's expand marker
        flips ``▼`` (open) ↔ ``▶`` (closed) in lockstep, which we also
        check on the subject's row.
        """
        indented_file = re.compile(r'^\s+\[A\] beta\.txt', re.MULTILINE)
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('second commit add beta', timeout=5.0)
                # Cursor starts on the newest commit. Enter expands it:
                # the indented file row appears and the marker is ▼.
                t.send('Enter')
                cap = t.wait_for(indented_file, timeout=5.0)
                subject_row = next(ln for ln in cap.splitlines()
                                   if 'second commit add beta' in ln)
                self.assertIn('▼', subject_row)
                # A second Enter collapses it — the indented file row goes
                # away and the marker flips back to ▶.
                t.send('Enter')
                deadline = time.time() + 3.0
                gone = False
                while time.time() < deadline:
                    if not indented_file.search(t.capture()):
                        gone = True
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    gone,
                    'indented [A] beta.txt still in the tree after a '
                    'second Enter — the expand/collapse toggle did not '
                    'fold the file list.')
                cap = t.capture()
                subject_row = next(ln for ln in cap.splitlines()
                                   if 'second commit add beta' in ln)
                self.assertIn('▶', subject_row)
                t.send('q')

    def test_mode_reflog_lists_entries(self):
        """``--mode reflog`` lists reflog entries with selector + action.

        The temp repo's two commits each produce a reflog entry, so the
        list shows ``HEAD@{n}`` selectors and ``commit`` action subjects.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--mode', 'reflog')
                # A reflog selector chip and a commit action subject.
                t.wait_for(re.compile(r'HEAD@\{'), timeout=5.0)
                t.wait_for('commit', timeout=5.0)
                t.send('q')

    def test_rapid_scroll_children_pane_lands(self):
        """Rapid 25-key burst lands the children pane within ~2s.

        Pre-#481 the cursor-driven children prefetch was FIFO, so a
        25-keystroke burst accumulated 25 ``get_children`` calls and
        the cursor's children pane appeared only after every visited
        commit had been fetched. With the prefetch slot the worker
        coalesces to ``max in-flight = 1`` and the final cursor's
        files-changed list appears within a fixed budget regardless
        of burst depth.

        Asserts the children pane shows at least one ``f###.txt`` row
        (the committed file path) inside 2s of the burst settling.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_with_n_commits(tmp, n=30)
            with TmuxFixture(cols=160, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                # Wait for the recipe to render the list (any commit
                # message is enough; commits are named 'commit 000'..).
                t.wait_for('commit 029', timeout=5.0)
                # Burst 25 j keys.
                for _ in range(25):
                    t.send('j')
                # After cursor stops, the children pane should populate
                # within 2s. Look for any ``f###.txt`` path — those are
                # the file ids displayed in the children grid pane.
                deadline = time.time() + 2.0
                file_re = re.compile(r'f\d{3}\.txt')
                found = False
                while time.time() < deadline:
                    pane = t.capture()
                    if file_re.search(pane):
                        found = True
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    found,
                    'children pane did not show any f###.txt entry '
                    'within 2s of cursor settling — prefetch slot may '
                    'not be coalescing.',
                )
                t.send('q')


if __name__ == '__main__':
    unittest.main()
