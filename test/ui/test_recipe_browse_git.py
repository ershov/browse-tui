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
import shlex
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


def _make_repo_with_shared_head_worktrees(tmpdir):
    """Init a repo whose main tree + a linked worktree share one HEAD commit.

    Both branches (``main`` and the linked ``feat``) point at the SAME commit,
    and each work tree carries a tracked modification. In ``--all`` mode the
    log shows that commit once, and ``_inject_worktree_tips`` stacks both
    worktrees' synthetic ``Tracked changes`` rows above it — the case the
    branch-name chip disambiguates. The linked worktree lives OUTSIDE
    ``tmpdir`` (sibling ``<tmpdir>_wt``) so it isn't itself an untracked
    entry. Returns ``(main_dir, wt_dir)``.
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
    with open(os.path.join(tmpdir, 'shared.txt'), 'w') as f:
        f.write('shared v1\n')
    git('add', 'shared.txt')
    git('commit', '-q', '-m', 'shared base commit')
    # Linked worktree on a new branch 'feat', pointing at the same commit.
    wt_dir = tmpdir + '_wt'
    git('worktree', 'add', '-q', '-b', 'feat', wt_dir)
    # A tracked modification in EACH worktree → each gets a Tracked group.
    with open(os.path.join(tmpdir, 'shared.txt'), 'w') as f:
        f.write('main edit\n')
    with open(os.path.join(wt_dir, 'shared.txt'), 'w') as f:
        f.write('feat edit\n')
    return tmpdir, wt_dir


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

    def test_launched_from_subdir_renders_file_diff(self):
        """Launched from a SUBDIR, a commit's per-file diff still paints.

        git lists changed files repo-root-relative (``show --name-status``),
        so the per-file ``git show -- <path>`` must run from the root or it
        finds nothing and the preview comes back empty. The recipe chdir's to
        the work-tree root at startup; here we launch from ``sub/deep`` and
        assert the diff of a file committed there actually renders.
        """
        env = {
            **os.environ,
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
        }
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            deep = os.path.join(tmp, 'sub', 'deep')
            os.makedirs(deep)
            with open(os.path.join(deep, 'gamma.txt'), 'w') as f:
                f.write('gammamarker\n')
            subprocess.run(['git', '-C', tmp, 'add', '-A'], check=True,
                           capture_output=True, env=env)
            subprocess.run(['git', '-C', tmp, 'commit', '-q', '-m',
                            'third add gamma in subdir'], check=True,
                           capture_output=True, env=env)
            with TmuxFixture(cols=120, rows=30) as t:
                # Launch from the nested subdir, not the repo root.
                t.send_line(f'cd {shlex.quote(deep)}')
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('third add gamma in subdir', timeout=5.0)
                t.send('Right')                          # expand HEAD commit
                t.wait_for('[A] sub/deep/gamma.txt', timeout=5.0)
                t.send('Down')                           # cursor -> file row
                # The diff preview must paint the added line; before the fix
                # this stayed empty (root-relative path resolved against cwd).
                t.wait_for('gammamarker', timeout=5.0)
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

        The recipe runs with no children pane, so we assert on the
        *tree*: an expanded commit shows its file as an indented
        ``[A] beta.txt`` row in the list pane, and the collapse removes
        that indented row. The commit's expand marker
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

    def test_display_mode_keys_repaint_columns(self):
        """The 1/2/3 keys repaint the commit columns in place — no refetch.

        Default mode 3 renders the author column (``Test``); pressing
        ``1`` (subject only) must drop it from the already-loaded rows
        while the subjects stay, and ``3`` must bring it back. This
        exercises the lightweight ``ctx.redraw`` repaint path: the
        per-mode column strings already live on every commit Item, so a
        repaint — not a refetch of the log — is all the switch needs.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                # Default mode 3 shows the author column ("Test").
                t.wait_for('first commit add alpha', timeout=5.0)
                t.wait_for('Test', timeout=5.0)
                # Mode 1 (subject only): the author column drops away, but
                # the subjects (the loaded rows) remain — a pure repaint.
                t.send('1')
                deadline = time.time() + 5.0
                dropped = False
                while time.time() < deadline:
                    cap = t.capture()
                    if 'Test' not in cap and 'first commit add alpha' in cap:
                        dropped = True
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    dropped,
                    'author column "Test" still present after pressing 1 — '
                    'the display-mode switch did not repaint the list.')
                # Mode 3 again: the author column comes back.
                t.send('3')
                t.wait_for('Test', timeout=5.0)
                t.send('q')

    def test_mode_reflog_lists_entries(self):
        """``--reflog`` lists reflog entries with selector + action.

        The temp repo's two commits each produce a reflog entry, so the
        list shows ``HEAD@{n}`` selectors and ``commit`` action subjects.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--reflog')
                # A reflog selector chip and a commit action subject.
                t.wait_for(re.compile(r'HEAD@\{'), timeout=5.0)
                t.wait_for('commit', timeout=5.0)
                t.send('q')

    def test_mode_status_shows_modified_file(self):
        """``--status`` lists a modified tracked file with an ``M`` tag.

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
                t.launch(_BIN, '--run-py', _RECIPE, '--status')
                cap = t.wait_for('beta.txt', timeout=5.0)
                row = next(ln for ln in cap.splitlines() if 'beta.txt' in ln)
                # The status tag is rendered as ``[M]`` before the path.
                self.assertIn('[M]', row)
                t.send('q')

    def test_mode_stash_lists_stash(self):
        """``--stash`` lists a stash with its ``stash@{0}`` selector.

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
                t.launch(_BIN, '--run-py', _RECIPE, '--stash')
                cap = t.wait_for('stash@{', timeout=5.0)
                self.assertIn('WIP on', cap)
                t.send('q')

    def test_mode_branches_lists_and_drills(self):
        """``--branches`` lists ``main`` (branch tag); drilling shows commits.

        The temp repo is on branch ``main``, so branches mode renders a
        ``main`` row tagged ``branch``. Right-arrow on it lists that ref's
        commits — the newest commit subject appears beneath it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--branches')
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

        The recipe hides the children pane by default (the preview
        already lists the changed files), so we first toggle it on with
        Alt+P — its files-changed behaviour still has to be correct.

        Pre-#481 the cursor-driven children prefetch was FIFO, so a
        25-keystroke burst accumulated 25 ``get_children`` calls and
        the cursor's children pane appeared only after every visited
        commit had been fetched. With the prefetch slot the worker
        coalesces to ``max in-flight = 1`` and the final cursor's
        files-changed list appears within a fixed budget regardless
        of burst depth.

        Asserts the children pane shows at least one ``[A] f###.txt``
        row. The ``[A]`` status prefix is the children grid's row
        format — it pins the match to the pane, since the preview's
        ``--stat`` lists the same file as a bare ``f###.txt | +N``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_with_n_commits(tmp, n=30)
            with TmuxFixture(cols=160, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                # Wait for the recipe to render the list (any commit
                # message is enough; commits are named 'commit 000'..).
                t.wait_for('commit 029', timeout=5.0)
                # Toggle the children pane on (Alt+P) and wait for its
                # separator before bursting, so the burst exercises the
                # pane's prefetch path rather than racing its first paint.
                t.send('M-p')
                t.wait_for('Children', timeout=5.0)
                # Burst 25 j keys.
                for _ in range(25):
                    t.send('j')
                # After cursor stops, the children pane should populate
                # within 2s. Look for any ``[A] f###.txt`` row — the file
                # ids the children grid renders for the cursor's commit.
                deadline = time.time() + 2.0
                file_re = re.compile(r'\[A\] f\d{3}\.txt')
                found = False
                while time.time() < deadline:
                    pane = t.capture()
                    if file_re.search(pane):
                        found = True
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    found,
                    'children pane did not show any [A] f###.txt entry '
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

    def test_all_mode_tags_shared_head_groups_with_branch_chip(self):
        """``--all`` tags each worktree's synthetic rows with its branch chip.

        With two branches (``main`` + linked ``feat``) at one shared commit
        and a tracked edit in each work tree, the all-branches log stacks two
        ``Tracked changes`` rows above the single commit. Each carries its
        worktree's branch short-name chip, so the otherwise-identical rows
        stay attributable — the row text shows ``main`` on one and ``feat``
        on the other. tmux strips SGR but keeps the chip text.
        """
        with tempfile.TemporaryDirectory() as tmp:
            main_dir, wt_dir = _make_repo_with_shared_head_worktrees(tmp)
            try:
                with TmuxFixture(cols=120, rows=30) as t:
                    t.send_line(f'cd {main_dir}')
                    t.launch(_BIN, '--run-py', _RECIPE, '--all')
                    t.wait_for('Tracked changes', timeout=5.0)
                    cap = t.wait_for('feat', timeout=5.0)
                    tracked_rows = [ln for ln in cap.splitlines()
                                    if 'Tracked changes' in ln]
                    self.assertEqual(len(tracked_rows), 2)
                    joined = '\n'.join(tracked_rows)
                    self.assertIn('main', joined)
                    self.assertIn('feat', joined)
                    t.send('q')
            finally:
                shutil.rmtree(wt_dir, ignore_errors=True)

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

        Commits mode now lands the startup cursor on the HEAD commit
        (ticket #1132), so first ``Home`` to the topmost row — the
        ``Untracked changes`` group — then a single Right expands it into
        its ``untracked.txt`` file leaf.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_with_changes(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('Untracked changes', timeout=5.0)
                t.send('Home')         # off the HEAD-commit start row
                t.send('Right')
                t.wait_for('untracked.txt', timeout=5.0)
                t.send('q')

    def test_conflict_group_drills_into_file(self):
        """The ``Conflicts`` row appears mid-merge and drills to its file.

        ``_make_repo_with_conflict`` leaves ``conflict.txt`` unmerged, so the
        commits-mode root carries a ``Conflicts`` group. Commits mode lands
        the startup cursor on the HEAD commit (ticket #1132), so first
        ``Home`` to the topmost row (``Conflicts`` is the only worktree group
        here), then a Right expands it into the ``conflict.txt`` leaf. If the
        harness can't produce a real conflict the test skips rather than
        asserting falsely.
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
                t.send('Home')         # off the HEAD-commit start row
                t.send('Right')
                t.wait_for('conflict.txt', timeout=5.0)
                t.send('q')


def _make_repo_with_context_diff(tmpdir):
    """Init a repo whose newest commit's diff carries UNCHANGED context.

    A multi-line file (committed, then one line changed) so the file
    diff has context lines (``alpha`` / ``gamma`` / …) that stay put.
    delta renders those unchanged lines once per row in unified mode but
    TWICE (old + new column) in side-by-side — the marker the wide-pane
    tests key on. Mirrors ``_make_repo``'s git-env + local user setup;
    returns the repo directory.
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
    path = os.path.join(tmpdir, 'ctx.txt')
    with open(path, 'w') as f:
        f.write('alpha\nbeta\ngamma\ndelta\nepsilon\n')
    git('add', 'ctx.txt')
    git('commit', '-q', '-m', 'first add ctx')
    with open(path, 'w') as f:
        f.write('alpha\nBETA-changed\ngamma\ndelta\nepsilon\n')
    git('add', 'ctx.txt')
    git('commit', '-q', '-m', 'second change beta line')
    return tmpdir


@unittest.skipUnless(shutil.which('delta'), 'delta not on PATH')
class TestBrowseGitSideBySide(unittest.TestCase):
    """Wide-pane delta diffs render side-by-side, and a resize across the
    160-col threshold re-renders with no keypress (ticket #838).

    In the default 'h' split the preview pane spans the full terminal
    width, so ``preview_width == cols``: a 120-col terminal is unified,
    a 170-col terminal is side-by-side. The marker is delta's column
    duplication — an UNCHANGED context line (``alpha``) appears once per
    row in unified output but twice (old + new column) side-by-side.
    """

    @staticmethod
    def _sbs_rows(cap):
        """How many captured rows carry ``alpha`` twice (both columns)."""
        return sum(1 for ln in cap.splitlines() if ln.count('alpha') >= 2)

    def _wait_sbs(self, t, want, timeout=4.0):
        """Poll until the diff is painted with ``want`` side-by-side rows.

        The re-render is async (``on_resize`` drops the cache, the
        framework refetches the cursor preview a frame later), so we poll
        rather than snapshot. We require ``alpha`` to be present in every
        accepted capture so a transient empty/loading preview frame
        (where the count is 0 but no diff is shown) is never mistaken for
        the unified (``want == 0``) state.
        """
        deadline = time.time() + timeout
        last = ''
        while time.time() < deadline:
            last = t.capture()
            if 'alpha' in last and self._sbs_rows(last) == want:
                return last
            time.sleep(0.05)
        self.fail(f'expected {want} side-by-side row(s); last capture had '
                  f'{self._sbs_rows(last)}:\n{last}')

    def _open_file_diff(self, t):
        """Drive the cursor onto the newest commit's file row and wait for
        its diff to render.

        Waiting for the context line (``alpha``) to actually paint matters
        for the resize test: it guarantees the launch-width preview has
        rendered AND the baseline ``on_resize`` has fired (recording
        ``_prev_pw``) before the test resizes — so the resize is a genuine
        width *change*, not the first (baseline-only) fire.
        """
        t.wait_for('second change beta line', timeout=5.0)
        t.send('Right')                       # expand newest commit
        # Wait for the expanded file ROW, not the bare 'ctx.txt' — the
        # commit preview's --stat ('ctx.txt | 2 +-') already shows that
        # at startup, so a bare match returns before the async expansion
        # paints and 'Down' would race onto the next commit. The '[M] '
        # status prefix pins the wait to the row (cf. the '[A]'-prefix fix
        # in test_rapid_scroll_children_pane_lands).
        t.wait_for('[M] ctx.txt', timeout=5.0)
        t.send('Down')                        # cursor -> ctx.txt file row
        t.wait_for('alpha', timeout=5.0)      # diff preview has painted

    def test_wide_pane_renders_side_by_side(self):
        """A 170-col launch shows the file diff in two columns."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_with_context_diff(tmp)
            with TmuxFixture(cols=170, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                self._open_file_diff(t)
                # The unchanged context line is duplicated across columns.
                self._wait_sbs(t, 1)
                t.send('q')

    def test_resize_across_threshold_reflows_without_keypress(self):
        """Crossing 160 up then down re-renders the diff with no keypress."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_with_context_diff(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE)
                self._open_file_diff(t)
                # 120 < 160 → unified: the context line is NOT duplicated.
                self._wait_sbs(t, 0)
                # Resize wide — NO keypress. on_resize drops the cache and
                # the framework refetches, re-rendering side-by-side.
                t.resize(170, 30)
                self._wait_sbs(t, 1)
                # Resize back narrow — again no keypress; reflows to unified.
                t.resize(120, 30)
                self._wait_sbs(t, 0)
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

    def test_graph_art_is_colored_not_monochrome(self):
        """The graph connectors render in git's NATIVE colour (ticket #756).

        ``--color=always`` makes git colour the lane connectors; the recipe
        passes that ANSI through (fg=None) so it reaches the screen. On a
        ``--no-ff`` merge the lanes use *different* colours per lane, so the
        coloured capture must show the connector glyphs (``│`` / ``\\`` /
        ``/``) wrapped in an SGR *foreground* sequence, with at least two
        DISTINCT colours present — proving the art is no longer monochrome.

        Also asserts no colour bleeds into the subject: every connector
        colour run is closed (by git's own reset and/or the framework's
        rule-3 trailing ``\\e[m``) before the commit subject text, so a
        subject line never carries a lane colour straight up to its words.
        """
        # An SGR foreground colour: 30-37 / 90-97 (basic) or 38;5;N (256).
        fg_sgr = re.compile(r'\x1b\[(?:3[0-7]|9[0-7]|38;5;\d+)m')
        # A connector glyph immediately following an SGR fg run.
        colored_connector = re.compile(
            r'(\x1b\[(?:3[0-7]|9[0-7]|38;5;\d+)m)[│\\/•]')
        with tempfile.TemporaryDirectory() as tmp:
            _make_merge_repo(tmp)
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(f'cd {tmp}')
                t.launch(_BIN, '--run-py', _RECIPE, '--tree')
                t.wait_for('merge feature', timeout=5.0)
                t.wait_stable()
                colored = t.capture(colors=True)
                # The connector glyphs carry git's colour: find the SGR fg
                # codes that immediately precede a lane glyph.
                hits = colored_connector.findall(colored)
                self.assertTrue(
                    hits,
                    'no coloured graph connector found — the art rendered '
                    f'monochrome.\n{colored}')
                distinct = set(hits)
                self.assertGreaterEqual(
                    len(distinct), 2,
                    'expected at least two distinct lane colours in the '
                    f'merge graph, saw {distinct!r}.\n{colored}')
                # No colour bleed into the subject: on the line carrying a
                # commit subject, the subject text must not be tinted by a
                # still-open lane colour. Check the merge subject's row — its
                # graph segment ('•') is uncoloured here, so the text up to
                # and including the subject carries no lingering fg run.
                for line in colored.splitlines():
                    if 'feat commit' in line:
                        before_subject = line.split('feat commit')[0]
                        # The last SGR before the subject must be a reset
                        # (\e[m or \e[39m), not a colour-set, so the subject
                        # is not tinted by the lane colour.
                        sgrs = re.findall(r'\x1b\[[0-9;]*m', before_subject)
                        if any(fg_sgr.match(s) for s in sgrs):
                            # A fg colour appears before the subject — the
                            # last one must have been closed by a reset.
                            last_fg = max(
                                i for i, s in enumerate(sgrs)
                                if fg_sgr.match(s))
                            resets = [
                                i for i, s in enumerate(sgrs)
                                if s in ('\x1b[m', '\x1b[0m', '\x1b[39m')]
                            self.assertTrue(
                                any(r > last_fg for r in resets),
                                'a lane colour bled into the commit subject '
                                f'(unreset fg before text).\n{line!r}')
                        break

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


def _make_stdin_repo(tmpdir):
    """Repo + dirty worktree whose git output feeds the ``-`` payloads.

    Two commits over ``alpha.txt`` (``alpha payload v1`` → ``v2``, plus a
    committed ``beta.txt``), then a dirty tree: an unstaged ``beta.txt``
    edit (``beta worktree edit``) and an ``untracked.txt`` — distinctive
    strings so the preview-pane assertions can't match a file name.
    ``LC_ALL=C`` callers get stable human-status prose. Returns tmpdir.
    """
    env = {
        **os.environ, 'LC_ALL': 'C',
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
        f.write('alpha payload v1\n')
    with open(os.path.join(tmpdir, 'beta.txt'), 'w') as f:
        f.write('beta payload v1\n')
    git('add', 'alpha.txt', 'beta.txt')
    git('commit', '-q', '-m', 'first commit add alpha')
    with open(os.path.join(tmpdir, 'alpha.txt'), 'w') as f:
        f.write('alpha payload v2\n')
    git('add', 'alpha.txt')
    git('commit', '-q', '-m', 'second commit change alpha')
    # Dirty worktree: unstaged tracked edit + an untracked file.
    with open(os.path.join(tmpdir, 'beta.txt'), 'w') as f:
        f.write('beta worktree edit\n')
    with open(os.path.join(tmpdir, 'untracked.txt'), 'w') as f:
        f.write('brand new\n')
    return tmpdir


def _git_stdout(repo, *args):
    """Return real ``git -C repo args…`` output (the stdin payload)."""
    env = {**os.environ, 'LC_ALL': 'C'}
    return subprocess.run(['git', '-C', repo, *args], check=True,
                          capture_output=True, text=True, env=env).stdout


class TestBrowseGitStdin(unittest.TestCase):
    """``browse-git -`` browses git output slurped from stdin (#862).

    End-to-end against the shipped binary: real ``git diff`` / ``log`` /
    ``status`` output is captured from a temp fixture repo into a
    payload file and the recipe launched with stdin redirected from it
    (``… browse-git - < payload``) — a file redirect is a pipe that is
    already closed, the faithful piped-input shape. The launches happen
    from a separate NON-repo cwd, proving the slurped text (not git, not
    a work tree) is the data source. The pre-UI error paths (empty /
    unrecognized input) run the binary headlessly — they exit before any
    terminal is touched.
    """

    def _launch_stdin(self, t, cwd, payload):
        """``cd <cwd> && browse-tui --run-py browse-git - < payload``."""
        t.send_line(f'cd {shlex.quote(cwd)} && {shlex.quote(_BIN)} '
                    f'--run-py {shlex.quote(_RECIPE)} - '
                    f'< {shlex.quote(payload)}')

    @staticmethod
    def _payload(elsewhere, text):
        path = os.path.join(elsewhere, 'payload.txt')
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_piped_diff_file_rows_preview_and_gating(self):
        # `git diff` → a synthetic umbrella stats row, auto-expanded over
        # one [M] row per file. Stepping onto the file row shows its own
        # hunks from the text (delta-rendered when present — either way
        # the changed line's text appears). The backtick mode picker is
        # gated off with a flash; launched from a non-repo cwd.
        with tempfile.TemporaryDirectory() as repo, \
                tempfile.TemporaryDirectory() as elsewhere:
            _make_stdin_repo(repo)
            payload = self._payload(elsewhere, _git_stdout(repo, 'diff'))
            with TmuxFixture(cols=120, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                # The umbrella row is visible and auto-expanded, with the
                # file row beneath it (this payload is a single-file diff
                # → the singular 'file' form).
                cap = t.wait_for('beta.txt', timeout=8.0)
                self.assertRegex(cap, r'diff: 1 file \+\d+ -\d+')
                row = next(ln for ln in cap.splitlines() if 'beta.txt' in ln)
                self.assertIn('[M]', row)
                # Step onto the file row → its block is the preview.
                t.send('Down')
                t.wait_for('beta worktree edit', timeout=5.0)
                # Mode switching re-runs git → flashes in stdin mode.
                t.send('`')
                t.wait_for('mode switch not available for piped input',
                           timeout=5.0)
                t.send('q')

    def test_piped_without_dash_auto_engages_stdin(self):
        # ``git diff | browse-git`` with NO ``-`` on the command line: the
        # recipe auto-detects the piped (non-tty) stdin and ingests the diff
        # exactly as the explicit ``-`` form would. Same redirect shape, the
        # ``-`` token simply dropped from the launch line; from a non-repo
        # cwd so the slurped text (not git) is provably the source.
        with tempfile.TemporaryDirectory() as repo, \
                tempfile.TemporaryDirectory() as elsewhere:
            _make_stdin_repo(repo)
            payload = self._payload(elsewhere, _git_stdout(repo, 'diff'))
            with TmuxFixture(cols=120, rows=30) as t:
                t.send_line(
                    f'cd {shlex.quote(elsewhere)} && {shlex.quote(_BIN)} '
                    f'--run-py {shlex.quote(_RECIPE)} '
                    f'< {shlex.quote(payload)}')
                cap = t.wait_for('beta.txt', timeout=8.0)
                self.assertRegex(cap, r'diff: 1 file \+\d+ -\d+')
                row = next(ln for ln in cap.splitlines() if 'beta.txt' in ln)
                self.assertIn('[M]', row)
                t.send('q')

    def test_piped_log_lists_commit_rows(self):
        # `git log` → one columnar commit row per block: subject last,
        # short-sha + author columns leading. 200 cols so the long
        # absolute Date column doesn't push the subject off the pane.
        with tempfile.TemporaryDirectory() as repo, \
                tempfile.TemporaryDirectory() as elsewhere:
            _make_stdin_repo(repo)
            payload = self._payload(elsewhere, _git_stdout(repo, 'log'))
            with TmuxFixture(cols=200, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                cap = t.wait_for('second commit change alpha', timeout=8.0)
                t.wait_for('first commit add alpha', timeout=5.0)
                row = next(ln for ln in cap.splitlines()
                           if 'second commit change alpha' in ln)
                # The author column and a 7-hex short sha lead the row.
                self.assertIn('Test', row)
                self.assertRegex(row, r'\b[0-9a-f]{7}\b')
                t.send('q')

    def test_piped_log_p_block_previews_its_patch(self):
        # `git log -p` → the newest commit's preview carries its own
        # patch text from the slurped block (delta or verbatim).
        with tempfile.TemporaryDirectory() as repo, \
                tempfile.TemporaryDirectory() as elsewhere:
            _make_stdin_repo(repo)
            payload = self._payload(elsewhere, _git_stdout(repo, 'log', '-p'))
            with TmuxFixture(cols=200, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                t.wait_for('second commit change alpha', timeout=8.0)
                # Cursor starts on the newest commit; its block contains
                # the alpha v1→v2 patch.
                t.wait_for('alpha payload v2', timeout=5.0)
                t.send('q')

    def test_piped_porcelain_status_rows(self):
        with tempfile.TemporaryDirectory() as repo, \
                tempfile.TemporaryDirectory() as elsewhere:
            _make_stdin_repo(repo)
            payload = self._payload(
                elsewhere, _git_stdout(repo, 'status', '--porcelain'))
            with TmuxFixture(cols=120, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                cap = t.wait_for('untracked.txt', timeout=8.0)
                beta = next(ln for ln in cap.splitlines() if 'beta.txt' in ln)
                self.assertIn('[M]', beta)
                untracked = next(ln for ln in cap.splitlines()
                                 if 'untracked.txt' in ln)
                self.assertIn('[?]', untracked)
                t.send('q')

    def test_piped_human_status_rows(self):
        # The prose `git status` form builds the same status-view rows.
        with tempfile.TemporaryDirectory() as repo, \
                tempfile.TemporaryDirectory() as elsewhere:
            _make_stdin_repo(repo)
            payload = self._payload(elsewhere, _git_stdout(repo, 'status'))
            with TmuxFixture(cols=120, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                cap = t.wait_for('untracked.txt', timeout=8.0)
                beta = next(ln for ln in cap.splitlines() if 'beta.txt' in ln)
                self.assertIn('[M]', beta)
                untracked = next(ln for ln in cap.splitlines()
                                 if 'untracked.txt' in ln)
                self.assertIn('[?]', untracked)
                t.send('q')

    # ---- the diff-mode stat umbrella (#917 / Option B #938) --------------

    # A two-file diff. Written directly (a valid unified diff) so the row
    # counts are deterministic regardless of git's heuristics: big.txt
    # gets 8 added lines, small.txt one swap (1/1).
    _STAT_DIFF = (
        'diff --git a/big.txt b/big.txt\n'
        '--- a/big.txt\n'
        '+++ b/big.txt\n'
        '@@ -1,1 +1,9 @@\n'
        ' ctx\n'
        + ''.join(f'+added line {n}\n' for n in range(1, 9))
        + 'diff --git a/small.txt b/small.txt\n'
        '--- a/small.txt\n'
        '+++ b/small.txt\n'
        '@@ -1 +1 @@\n'
        '-old\n'
        '+new\n')

    def test_piped_diff_umbrella_auto_expands_with_stat_preview(self):
        # The umbrella row carries the short stats title and is
        # auto-expanded so both file rows are visible immediately; its own
        # preview is the synthesised stat table (per-file ``+N -M`` + the
        # git summary footer).
        with tempfile.TemporaryDirectory() as elsewhere:
            payload = self._payload(elsewhere, self._STAT_DIFF)
            with TmuxFixture(cols=120, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                # Title: 2 files, +9 (8+1) / -1.
                cap = t.wait_for('diff: 2 files +9 -1', timeout=8.0)
                # Auto-expanded: both file rows visible without a keypress.
                self.assertIn('big.txt', cap)
                self.assertIn('small.txt', cap)
                # Cursor starts on the umbrella → the stat table is the
                # preview: a ``<path> | +N -M`` row per file + the footer.
                cap = t.wait_for('2 files changed', timeout=5.0)
                count_rows = [re.sub(r'\x1b\[[0-9;]*m', '', ln)
                              for ln in cap.splitlines()
                              if ' | ' in ln
                              and ('big.txt' in ln or 'small.txt' in ln)]
                self.assertTrue(count_rows, f'no count rows in:\n{cap}')
                # The Option-B layout: ``+N -M`` (no histogram bars after).
                self.assertTrue(any(re.search(r'\| \+8 -0$', r)
                                    for r in count_rows), count_rows)
                self.assertTrue(any(re.search(r'\| \+1 -1$', r)
                                    for r in count_rows), count_rows)
                self.assertIn('insertions(+)', cap)
                self.assertIn('deletion(-)', cap)     # singular: 1 deletion
                t.send('q')

    def test_piped_diff_stat_counts_are_colored(self):
        # The preview's per-file ``+N`` carries green SGR, ``-M`` red —
        # the raw escapes survive to the rendered pane (capture -e). The
        # framework strips them when ANSI is off (R) — asserted too.
        with tempfile.TemporaryDirectory() as elsewhere:
            payload = self._payload(elsewhere, self._STAT_DIFF)
            with TmuxFixture(cols=120, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                t.wait_for('2 files changed', timeout=8.0)
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    cap = t.capture(colors=True)
                    if '\x1b[32m' in cap and '\x1b[31m' in cap:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f'count colors never painted:\n{cap!r}')
                # The colors ride the count cell of a file row.
                row = next(ln for ln in cap.splitlines()
                           if 'big.txt' in ln and ' | ' in ln)
                self.assertIn('\x1b[32m', row)     # green add
                self.assertIn('\x1b[31m', row)     # red remove
                # Toggle ANSI off (R): the framework neutralises the SGR.
                t.send('R')
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    cap = t.capture(colors=True)
                    row = next((ln for ln in cap.splitlines()
                                if 'big.txt' in ln and ' | ' in ln), '')
                    if row and '\x1b[32m' not in row:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f'ANSI-off did not strip count color:\n{row!r}')
                t.send('q')

    def test_piped_diff_stat_fits_narrow_and_refits_on_resize(self):
        # A single LONG-PATH file at a 35-col launch: the path is
        # front-elided (``...`` prefix) so the ``+N -M`` counts still fit
        # the pane. Then resize wider with NO keypress — the umbrella
        # preview re-renders via the existing on_resize cache-drop and
        # more of the path shows. The cursor is the umbrella throughout
        # (its preview is the stat table). The preview pane spans the full
        # terminal width in the default split, so preview_width tracks cols.
        path = 'some/long/nested/path/big-changes.txt'
        big = ''.join(f'+added line {n}\n' for n in range(1, 41))   # 40 adds
        diff = (f'diff --git a/{path} b/{path}\n'
                f'--- a/{path}\n+++ b/{path}\n@@ -1 +1,40 @@\n ctx\n' + big)
        with tempfile.TemporaryDirectory() as elsewhere:
            payload = self._payload(elsewhere, diff)

            def name_len(cap):
                # The visible (SGR-stripped) name segment before ' | '.
                for ln in cap.splitlines():
                    if 'big-changes.txt' in ln and ' | ' in ln:
                        plain = re.sub(r'\x1b\[[0-9;]*m', '', ln)
                        return len(plain.split(' | ', 1)[0])
                return None

            with TmuxFixture(cols=35, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                t.wait_for('1 file changed', timeout=8.0)
                # Poll until the narrow-width stat row paints: elided
                # name, counts present, row within the pane.
                deadline = time.time() + 5.0
                narrow_name = None
                while time.time() < deadline:
                    cap = t.capture()
                    row = next((ln for ln in cap.splitlines()
                                if 'big-changes.txt' in ln and ' | ' in ln),
                               None)
                    if row is not None and re.search(r'\+40 -0', row):
                        self.assertLessEqual(len(row), 35, repr(row))
                        self.assertIn('...', row)   # front-elided path
                        narrow_name = name_len(cap)
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(narrow_name,
                                     'narrow stat row never painted')
                # Resize wider — no keypress. on_resize drops the umbrella
                # preview; the framework refetches and the path re-fits.
                t.resize(100, 30)
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    wide_name = name_len(t.capture())
                    if wide_name is not None and wide_name > narrow_name:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f'name did not grow on resize (narrow='
                              f'{narrow_name}, last={name_len(t.capture())})')
                t.send('q')

    def test_piped_diff_umbrella_actions_do_not_crash(self):
        # The umbrella's ('sdiff',) id is an unknown kind to the file-only
        # actions: E is a no-op, Enter folds/unfolds it, and the gated
        # mode/tree pickers flash — none crash the UI. The tree-list row
        # carries the ▼ (expanded) / ▶ (collapsed) glyph; the Children /
        # Preview panes keep showing the umbrella's files either way, so
        # the fold is asserted on the umbrella's own list row.
        def umbrella_row(cap):
            return next(ln for ln in cap.splitlines() if 'diff: 2 files' in ln)

        with tempfile.TemporaryDirectory() as elsewhere:
            payload = self._payload(elsewhere, self._STAT_DIFF)
            with TmuxFixture(cols=120, rows=30) as t:
                self._launch_stdin(t, elsewhere, payload)
                cap = t.wait_for('diff: 2 files', timeout=8.0)
                self.assertIn('▼', umbrella_row(cap))   # ▼ expanded
                t.send('E')                 # no-op on the umbrella
                t.send('Enter')             # collapse the umbrella
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if '▶' in umbrella_row(t.capture()):  # ▶ collapsed
                        break
                    time.sleep(0.05)
                else:
                    self.fail('umbrella did not collapse on Enter')
                t.send('Enter')             # re-expand
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if '▼' in umbrella_row(t.capture()):
                        break
                    time.sleep(0.05)
                else:
                    self.fail('umbrella did not re-expand on Enter')
                t.send('q')

    # -- pre-UI error paths (headless: the recipe exits before any UI) --

    def test_unrecognized_stdin_errors_before_ui(self):
        proc = subprocess.run(
            [_BIN, '--run-py', _RECIPE, '-'],
            input='certainly not git output\n',
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('unrecognized stdin input', proc.stderr)
        self.assertEqual(proc.stdout, '')

    def test_empty_stdin_errors_before_ui(self):
        proc = subprocess.run(
            [_BIN, '--run-py', _RECIPE, '-'],
            input='', capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('empty stdin input', proc.stderr)
        self.assertEqual(proc.stdout, '')

    def test_dash_with_other_args_errors_before_ui(self):
        proc = subprocess.run(
            [_BIN, '--run-py', _RECIPE, '-', '--status'],
            input='?? x\n', capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('cannot be combined', proc.stderr)
        self.assertEqual(proc.stdout, '')


if __name__ == '__main__':
    unittest.main()
