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


def _make_repo_with_changes(tmpdir):
    """Init a repo with one commit, then leave the tree dirty in three ways.

    Mirrors ``_make_repo``'s git-env + local user setup. After the initial
    commit (adding ``alpha.txt`` / ``beta.txt`` / ``gamma.txt``) the tree is
    left with:
      * an UNTRACKED file (``untracked.txt``, written but never added),
      * an unstaged TRACKED modification (``beta.txt`` changed, not staged),
      * a STAGED change (``gamma.txt`` changed then ``git add``ed).
    These map to the recipe's untracked / tracked / staged worktree groups.
    Returns the repo directory.
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
    for name in ('alpha.txt', 'beta.txt', 'gamma.txt'):
        with open(os.path.join(tmpdir, name), 'w') as f:
            f.write(f'{name} v1\n')
    git('add', 'alpha.txt', 'beta.txt', 'gamma.txt')
    git('commit', '-q', '-m', 'first commit add files')
    # Untracked file — written, never added.
    with open(os.path.join(tmpdir, 'untracked.txt'), 'w') as f:
        f.write('brand new\n')
    # Unstaged tracked modification.
    with open(os.path.join(tmpdir, 'beta.txt'), 'w') as f:
        f.write('beta.txt changed unstaged\n')
    # Staged change.
    with open(os.path.join(tmpdir, 'gamma.txt'), 'w') as f:
        f.write('gamma.txt changed staged\n')
    git('add', 'gamma.txt')
    return tmpdir


def _make_repo_with_conflict(tmpdir):
    """Init a repo and produce a real, unresolved merge conflict.

    Commits ``conflict.txt`` on ``main``, branches off, edits the same line
    on each of two branches, then ``git merge``s the side branch back into
    ``main`` (with ``check=False`` since a conflicting merge exits non-zero).
    Leaves ``conflict.txt`` unmerged so the recipe's ``Conflicts`` group is
    non-empty. Returns ``(tmpdir, conflicted)`` whether or not the merge
    actually conflicted, plus a bool ``conflicted``.
    """
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
        'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
    }
    def git(*args, check=True):
        return subprocess.run(['git', '-C', tmpdir, *args], check=check,
                              capture_output=True, env=env)
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    path = os.path.join(tmpdir, 'conflict.txt')
    with open(path, 'w') as f:
        f.write('shared line\n')
    git('add', 'conflict.txt')
    git('commit', '-q', '-m', 'base commit')
    # Side branch edits the shared line.
    git('checkout', '-q', '-b', 'side')
    with open(path, 'w') as f:
        f.write('side edit\n')
    git('add', 'conflict.txt')
    git('commit', '-q', '-m', 'side edit')
    # Back on main, edit the same line differently.
    git('checkout', '-q', 'main')
    with open(path, 'w') as f:
        f.write('main edit\n')
    git('add', 'conflict.txt')
    git('commit', '-q', '-m', 'main edit')
    # Merge the side branch — expected to conflict (exit != 0).
    merge = git('merge', 'side', check=False)
    # The status check below confirms whether the conflict actually landed.
    status = git('status', '--porcelain', check=False)
    conflicted = b'UU conflict.txt' in status.stdout or merge.returncode != 0
    return tmpdir, conflicted


def _make_merge_repo(tmpdir):
    """Init a repo with a non-fast-forward merge so ``--graph`` emits fillers.

    Builds ``base`` on ``main``, a divergent ``feat`` branch, more work on
    ``main``, then a ``--no-ff`` merge. ``git log --graph`` then prints
    connector-only lines (``|\\`` after the merge node, ``|/`` joining the
    lanes back) that carry no commit — the recipe turns those into ``meta``
    filler rows. Visible top-down order in tree mode is::

        merge feature   (commit)
        |\\              (filler, meta)
        feat            (commit)
        mainwork        (commit)
        |/              (filler, meta)
        base            (commit)
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
    for name, content in (('a.txt', 'base\n'),):
        with open(os.path.join(tmpdir, name), 'w') as f:
            f.write(content)
        git('add', name)
    git('commit', '-q', '-m', 'base commit')
    git('checkout', '-q', '-b', 'feature')
    with open(os.path.join(tmpdir, 'b.txt'), 'w') as f:
        f.write('feat\n')
    git('add', 'b.txt')
    git('commit', '-q', '-m', 'feat commit')
    git('checkout', '-q', 'main')
    with open(os.path.join(tmpdir, 'c.txt'), 'w') as f:
        f.write('mainwork\n')
    git('add', 'c.txt')
    git('commit', '-q', '-m', 'mainwork commit')
    git('merge', '-q', '--no-ff', 'feature', '-m', 'merge feature')
    return tmpdir


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def _cursor_row(screen):
    """Return the ANSI-stripped text of the reverse-video cursor row.

    The cursor row carries the ``ESC[7m`` reverse-video sequence; find it,
    strip every escape, and return the plain text (trailing pad stripped).
    Returns ``None`` when no cursor marker is on screen.
    """
    for line in screen.splitlines():
        if '\x1b[7m' in line:
            return _ANSI_RE.sub('', line).rstrip()
    return None


def _selection_marked_rows(screen):
    """Plain text of every list row carrying the ``*`` selection marker.

    A selected row renders ``*`` in its chrome's marker column; the cursor
    row also carries the reverse-video escape. Strip ANSI, keep only rows
    whose first non-space glyph is the selection ``*``.
    """
    rows = []
    for line in screen.splitlines():
        plain = _ANSI_RE.sub('', line)
        if plain.lstrip().startswith('*'):
            rows.append(plain.rstrip())
    return rows


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

    def test_repo_dir_arg_browses_that_repo(self):
        """A leading repo-dir arg browses THAT repo, even from a non-repo cwd."""
        with tempfile.TemporaryDirectory() as repo, \
                tempfile.TemporaryDirectory() as elsewhere:
            _make_repo(repo)
            with TmuxFixture(cols=120, rows=30) as t:
                # Launch from an unrelated, non-repo cwd and point at the repo.
                t.send_line(f'cd {elsewhere}')
                t.launch(_BIN, '--run-py', _RECIPE, repo)
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

    def test_mode_status_shows_modified_file(self):
        """``--mode status`` lists a modified tracked file with an ``M`` tag.

        After ``_make_repo`` we dirty a tracked file (unstaged), so
        ``git status --porcelain`` reports `` M beta.txt`` → the recipe
        renders a row with an ``M`` status tag next to ``beta.txt``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            # Modify a tracked file without staging → worktree-modified.
            with open(os.path.join(tmp, 'beta.txt'), 'w') as f:
                f.write('beta changed\n')
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--mode', 'status')
                cap = t.wait_for('beta.txt', timeout=5.0)
                row = next(ln for ln in cap.splitlines() if 'beta.txt' in ln)
                # The status tag is rendered as ``[M]`` before the path.
                self.assertIn('[M]', row)
                t.send('q')

    def test_mode_stash_lists_stash(self):
        """``--mode stash`` lists a stash with its ``stash@{0}`` selector.

        After ``_make_repo`` we modify a tracked file and ``git stash`` it,
        so ``git stash list`` reports one entry → the recipe renders a row
        with a ``stash@{0}`` tag and a ``WIP on`` subject.
        """
        env = {
            **os.environ,
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
        }
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            # Dirty a tracked file, then stash it.
            with open(os.path.join(tmp, 'beta.txt'), 'w') as f:
                f.write('beta changed\n')
            subprocess.run(['git', '-C', tmp, 'stash'], check=True,
                           capture_output=True, env=env)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--mode', 'stash')
                cap = t.wait_for('stash@{', timeout=5.0)
                self.assertIn('WIP on', cap)
                t.send('q')

    def test_mode_branches_lists_and_drills(self):
        """``--mode branches`` lists ``main`` (branch tag); drilling shows commits.

        The temp repo is on branch ``main``, so branches mode renders a
        ``main`` row tagged ``branch``. Right-arrow on it lists that ref's
        commits — the newest commit subject appears beneath it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--mode', 'branches')
                cap = t.wait_for('main', timeout=5.0)
                row = next(ln for ln in cap.splitlines()
                           if re.search(r'\bmain\b', ln))
                # The kind word is rendered as a ``[branch]`` tag.
                self.assertIn('[branch]', row)
                # Drill into the ref's commits.
                t.send('Right')
                t.wait_for('second commit add beta', timeout=5.0)
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

    def test_worktree_groups_render_in_order(self):
        """The synthetic worktree rows render, top-down, in group order.

        ``_make_repo_with_changes`` leaves an untracked file, an unstaged
        tracked modification and a staged change, so the commits-mode root
        prepends three group rows — ``Untracked changes`` / ``Tracked
        changes`` / ``Staged changes`` — above the commit log, in that
        order. tmux strips SGR but keeps the label text, so we capture the
        pane and assert the rows appear and are vertically ordered.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_with_changes(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('Untracked changes', timeout=5.0)
                t.wait_for('Tracked changes', timeout=5.0)
                cap = t.wait_for('Staged changes', timeout=5.0)
                lines = cap.splitlines()
                def row_of(label):
                    return next(i for i, ln in enumerate(lines) if label in ln)
                untracked = row_of('Untracked changes')
                tracked = row_of('Tracked changes')
                staged = row_of('Staged changes')
                self.assertLess(untracked, tracked)
                self.assertLess(tracked, staged)
                t.send('q')

    def test_clean_repo_has_no_worktree_groups(self):
        """A clean tree shows none of the synthetic worktree rows.

        ``_make_repo`` commits everything, leaving the work tree clean, so
        ``_worktree_groups`` yields nothing and the commits-mode root is the
        bare log. We wait for a commit subject, then assert the pane carries
        no worktree-group label.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                cap = t.wait_for('second commit add beta', timeout=5.0)
                self.assertNotIn('Untracked changes', cap)
                self.assertNotIn('Staged changes', cap)
                self.assertNotIn('Tracked changes', cap)
                t.send('q')

    def test_worktree_group_drills_into_file(self):
        """Right-arrow on the first worktree row reveals its file leaf.

        The cursor starts on the topmost row — the ``Untracked changes``
        group — so a single Right expands it into its ``untracked.txt``
        file leaf.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_with_changes(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('Untracked changes', timeout=5.0)
                t.send('Right')
                t.wait_for('untracked.txt', timeout=5.0)
                t.send('q')

    def test_conflict_group_drills_into_file(self):
        """The ``Conflicts`` row appears mid-merge and drills to its file.

        ``_make_repo_with_conflict`` leaves ``conflict.txt`` unmerged, so the
        commits-mode root carries a ``Conflicts`` group. The cursor starts on
        the topmost row (``Conflicts`` is the only worktree group here), so a
        Right expands it into the ``conflict.txt`` leaf. If the harness can't
        produce a real conflict the test skips rather than asserting falsely.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _, conflicted = _make_repo_with_conflict(tmp)
            if not conflicted:
                self.skipTest('git merge did not leave a conflict in this '
                              'environment')
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('Conflicts', timeout=5.0)
                t.send('Right')
                t.wait_for('conflict.txt', timeout=5.0)
                t.send('q')


class TestBrowseGitTreeMeta(unittest.TestCase):
    """Tree-mode filler rows are framework ``meta`` rows (ticket #741).

    Drives the real binary against a ``--no-ff`` merge repo whose
    ``git log --graph`` emits connector-only filler lines. The recipe marks
    those filler Items ``meta=True``; the framework then skips the cursor
    over them (preventively, no bounce) and never selects them. Also checks
    the graph art stays aligned under the commit rows' graph column.
    """

    def test_cursor_skips_filler_rows_preventively(self):
        """Down from the merge commit lands on commits, never on a filler.

        Visible order is merge / |\\ (filler) / feat / mainwork / |/
        (filler) / base. Stepping Down must land on feat, then mainwork,
        then base — each step skipping any interleaved filler with no
        intermediate landing on the connector-only rows.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_merge_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--tree')
                t.wait_for('merge feature', timeout=5.0)
                t.wait_stable()
                # Cursor starts on the newest (merge) commit.
                self.assertIn('merge feature', _cursor_row(t.capture(colors=True)))
                # Each Down skips the connector filler(s) and lands on the
                # next commit subject — never on a connector-only row.
                for expected in ('feat commit', 'mainwork commit', 'base commit'):
                    t.send('Down')
                    t.wait_stable()
                    row = _cursor_row(t.capture(colors=True))
                    self.assertIsNotNone(row, 'cursor marker vanished')
                    self.assertIn(expected, row,
                                  f'Down did not land on {expected!r}; '
                                  f'cursor row was {row!r}')

    def test_graph_art_renders_aligned_under_commit_column(self):
        """Filler connector art lines up under the commit rows' bullet column.

        The merge node's bullet and the connector art on the surrounding
        filler rows share the same horizontal offset — meta chrome blanks the
        marker/expander glyphs but preserves their width, so the graph column
        stays aligned.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_merge_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--tree')
                t.wait_for('merge feature', timeout=5.0)
                t.wait_stable()
                plain = _ANSI_RE.sub('', t.capture())
                lines = plain.splitlines()
                # The merge commit's row carries a bullet '•' (its node).
                merge_row = next(ln for ln in lines if 'merge feature' in ln)
                bullet_col = merge_row.index('•')
                # A connector-only filler row (the '|\' below the merge node,
                # translated to '│\') carries no commit text; its first lane
                # glyph aligns at the same column as the commit bullet.
                filler_row = next(
                    ln for ln in lines
                    if '│' in ln and 'commit' not in ln and 'merge' not in ln)
                self.assertEqual(filler_row.index('│'), bullet_col,
                                 'filler connector art is not aligned under '
                                 f'the commit bullet column.\n{plain}')

    def test_filler_rows_are_unselectable(self):
        """Ctrl-A select-all marks every commit but no connector filler row.

        A meta row is never selectable, so select-all adds the 4 commits to
        the selection (status bar ``[4]``) and leaves both connector-only
        filler rows unmarked.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_merge_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--tree')
                t.wait_for('merge feature', timeout=5.0)
                t.wait_stable()
                t.send('C-a')           # select all (landable) rows
                t.wait_stable()
                cap = t.capture(colors=True)
                marked = _selection_marked_rows(cap)
                # All four commits are marked; the two connector fillers are
                # not (they carry no '*').
                self.assertEqual(len(marked), 4,
                                 f'expected 4 selected commit rows, got '
                                 f'{len(marked)}:\n{marked}')
                for row in marked:
                    self.assertNotIn(
                        '│\\', row,
                        f'a connector filler row was selected: {row!r}')
                    # Every marked row carries commit metadata (a subject),
                    # never a connector-only line.
                    self.assertTrue(
                        'commit' in row or 'feat' in row,
                        f'a non-commit row was selected: {row!r}')


if __name__ == '__main__':
    unittest.main()
