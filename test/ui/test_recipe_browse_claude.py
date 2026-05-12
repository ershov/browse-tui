"""UI tests for the ``recipes/browse-claude`` Claude-Code-session browser.

The recipe walks ``$HOME/.claude/projects/<encoded-path>/<session>.jsonl``
files in three levels (project → session → message). To keep the tests
hermetic we point ``HOME`` at a temp directory pre-populated with a
fake project / session / messages tree, then drive the recipe under
tmux and assert that each level renders.

The shebang ``#!/usr/bin/env -S browse-tui --run-py`` requires the
binary to be on PATH, which is fragile in tests; instead we invoke
``./browse-tui --run-py recipes/browse-claude`` directly so the tests
are independent of the user's PATH.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-claude')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _make_fake_claude(tmpdir, with_subagents=False):
    """Create a fake ``$HOME/.claude/projects`` layout for tests.

    Returns the path to the projects root. Layout::

        tmp/.claude/projects/-home-test-project/abcd1234-deadbeef.jsonl

    The .jsonl contains three records mirroring real Claude-Code shapes:
    a ``last-prompt`` bookmark, a ``permission-mode`` event, and a user
    turn. The names embed predictable substrings (``home/test/project``,
    ``abcd1234-deadbeef``, ``last-prompt``) so the tests can wait on
    them with cheap string matching.

    When ``with_subagents`` is true, also create a sibling
    ``abcd1234-deadbeef/subagents/`` dir containing one
    ``agent-FAKEAGENT01.jsonl`` (with two messages) and the matching
    ``.meta.json`` ({agentType, description}). This mirrors the real
    on-disk layout — the recipe enumerates these as drillable rows
    alongside the session's top-level messages.
    """
    root = os.path.join(tmpdir, '.claude', 'projects')
    proj = os.path.join(root, '-home-test-project')
    os.makedirs(proj)
    sess = os.path.join(proj, 'abcd1234-deadbeef.jsonl')
    with open(sess, 'w') as f:
        f.write(json.dumps({
            'type': 'last-prompt',
            'leafUuid': '12345678-aaaa-bbbb-cccc-dddddddddddd',
            'sessionId': 'abcd1234',
        }) + '\n')
        f.write(json.dumps({
            'type': 'permission-mode',
            'permissionMode': 'plan',
            'sessionId': 'abcd1234',
        }) + '\n')
        f.write(json.dumps({
            'type': 'user',
            'message': {'role': 'user', 'content': 'hello world'},
        }) + '\n')
    if with_subagents:
        sub_dir = os.path.join(proj, 'abcd1234-deadbeef', 'subagents')
        os.makedirs(sub_dir)
        agent_id = 'FAKEAGENT01'
        agent_jsonl = os.path.join(sub_dir, f'agent-{agent_id}.jsonl')
        with open(agent_jsonl, 'w') as f:
            f.write(json.dumps({
                'parentUuid': None,
                'isSidechain': True,
                'agentId': agent_id,
                'type': 'user',
                'message': {
                    'role': 'user',
                    'content': 'subagent task: do the thing',
                },
            }) + '\n')
            f.write(json.dumps({
                'parentUuid': 'aaaa',
                'isSidechain': True,
                'agentId': agent_id,
                'type': 'assistant',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': 'roger'}],
                },
            }) + '\n')
        meta_path = os.path.join(sub_dir, f'agent-{agent_id}.meta.json')
        with open(meta_path, 'w') as f:
            json.dump({
                'agentType': 'general-purpose',
                'description': 'PROBE-DESC test the thing',
            }, f)
    return root


class TestBrowseClaude(unittest.TestCase):

    def _launch_env(self, tmp):
        """tmux env dict pointing HOME at the temp tree.

        ``TmuxFixture`` accepts an env dict at construction time and
        merges it into every shell invocation, so the recipe sees our
        synthetic ``$HOME/.claude/projects`` rather than the real one.
        """
        return {'HOME': tmp}

    def test_lists_projects(self):
        """Top level renders the (decoded) project path."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp)
            with TmuxFixture(cols=120, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/project')
                t.send('q')

    def test_drills_into_session(self):
        """Right-arrow expands the project and reveals the session id."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp)
            with TmuxFixture(cols=120, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef', timeout=3.0)
                t.send('q')

    def test_drills_into_messages(self):
        """Drilling into the session lists the per-line records."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp)
            with TmuxFixture(cols=120, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef')
                t.send('Down')
                t.send('Right')
                # Wait for any of the three message kinds to render —
                # ``last-prompt`` is the first record so it's the most
                # reliable wait target.
                t.wait_for('last-prompt', timeout=3.0)
                t.send('q')

    def test_lists_subagents_under_session(self):
        """A session with subagents shows them as siblings of its messages.

        Drill project -> session and look for the meta.json description
        we baked into the fixture (``PROBE-DESC``). Confirms subagents
        appear at all and that we surface their description as the title.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp, with_subagents=True)
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef')
                t.send('Down')
                t.send('Right')
                t.wait_for('PROBE-DESC', timeout=3.0)
                t.send('q')

    def test_pid_lands_on_session_with_preview(self):
        """``--pid PID`` should show the session preview, not a blank pane.

        Regression test for the bug where scoping directly into the
        .jsonl made the session a ``scope_root`` entry, which the
        framework skips for preview updates. The fix scopes into the
        parent dir and moves the cursor onto the session row, so its
        preview (card + timeline) renders.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp)
            # Layer a sessions/<pid>.json sidecar pointing at the fake
            # session we created. Use this process's PID so the recipe's
            # liveness check passes.
            pid = os.getpid()
            sdir = os.path.join(tmp, '.claude', 'sessions')
            os.makedirs(sdir)
            with open(os.path.join(sdir, f'{pid}.json'), 'w') as f:
                json.dump({'sessionId': 'abcd1234-deadbeef', 'pid': pid,
                           'cwd': '/home/test/project', 'status': 'idle'}, f)
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--pid', str(pid))
                # The session row's preview should include the
                # session-card "session:" line.
                t.wait_for('session:', timeout=3.0)
                # And the user message we put in the fake jsonl should
                # land in the timeline (proving we did a full session
                # scan, not just rendered the breadcrumb).
                t.wait_for('hello world', timeout=3.0)
                t.send('q')

    def test_drills_into_subagent(self):
        """Expanding a subagent reveals its own transcript lines.

        Drill project -> session -> subagent group, then verify the
        subagent's first user message ('subagent task: do the thing')
        renders. Distinct from the parent session's 'hello world' so
        we know we routed into the right .jsonl.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp, with_subagents=True)
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef')
                t.send('Down')
                t.send('Right')
                t.wait_for('PROBE-DESC', timeout=3.0)
                # Subagent rows sort first per the recipe — cursor should
                # be on it after the inward drill, so Right expands it.
                t.send('Right')
                t.wait_for('subagent task', timeout=3.0)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
