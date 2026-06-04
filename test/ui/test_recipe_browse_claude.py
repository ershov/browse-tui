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


def _make_fake_claude(tmpdir, with_subagents=False, relocated=False):
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

    When ``relocated`` is true (implies ``with_subagents``), the session
    .jsonl stays under ``-home-test-project/`` but its subagents live
    under a **cwd-derived** project dir, mirroring what Claude Code does
    after a session enters a git worktree. The session records carry
    ``cwd=/home/test/project/.worktrees/wt`` which encodes to
    ``-home-test-project--worktrees-wt`` (``.`` and ``/`` both → ``-``),
    so the subagents land at
    ``-home-test-project--worktrees-wt/abcd1234-deadbeef/subagents/``.
    The session also carries an ``Agent`` tool_use + a tool_result whose
    ``toolUseResult.agentId`` links to the relocated subagent.
    """
    if relocated:
        with_subagents = True
    root = os.path.join(tmpdir, '.claude', 'projects')
    proj = os.path.join(root, '-home-test-project')
    os.makedirs(proj)
    sess = os.path.join(proj, 'abcd1234-deadbeef.jsonl')
    cwd = '/home/test/project'
    agent_id = 'FAKEAGENT01'
    records = [
        {
            'type': 'last-prompt',
            'leafUuid': '12345678-aaaa-bbbb-cccc-dddddddddddd',
            'sessionId': 'abcd1234',
        },
        {
            'type': 'permission-mode',
            'permissionMode': 'plan',
            'sessionId': 'abcd1234',
        },
        {
            'type': 'user',
            'message': {'role': 'user', 'content': 'hello world'},
        },
    ]
    if relocated:
        # The session moved into a worktree; every record now records the
        # worktree cwd, which is where Claude Code stores the subagents.
        cwd = '/home/test/project/.worktrees/wt'
        for rec in records:
            rec['cwd'] = cwd
        # The dispatching assistant call + its tool_result link the main
        # session to the subagent via ``toolUseResult.agentId``.
        records.append({
            'type': 'assistant',
            'uuid': 'a-dispatch',
            'cwd': cwd,
            'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'toolu_DISPATCH01',
                 'name': 'Agent',
                 'input': {'description': 'PROBE-DESC test the thing'}},
            ]},
        })
        records.append({
            'type': 'user',
            'uuid': 'u-result',
            'parentUuid': 'a-dispatch',
            'cwd': cwd,
            'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_DISPATCH01',
                 'content': 'done'},
            ]},
            'toolUseResult': {'agentId': agent_id},
        })
    with open(sess, 'w') as f:
        for rec in records:
            f.write(json.dumps(rec) + '\n')
    if with_subagents:
        if relocated:
            enc = cwd.replace('/', '-').replace('.', '-')
            sub_dir = os.path.join(
                root, enc, 'abcd1234-deadbeef', 'subagents')
        else:
            sub_dir = os.path.join(proj, 'abcd1234-deadbeef', 'subagents')
        os.makedirs(sub_dir)
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
                t.launch(_BIN, '--run-py', _RECIPE, '--no-tree')
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
        preview (now lightweight metadata — sessionId, mtime, etc.)
        renders. Scoping into the session upgrades it to the full
        cascade.
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
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--no-tree', '--pid', str(pid))
                # Session row's lightweight preview surfaces the
                # sessionId and mtime — proves the preview pane is
                # populated even though we haven't scoped in.
                t.wait_for('abcd1234', timeout=3.0)
                # Scoping in upgrades the preview to the full cascade,
                # which now includes the message body.
                t.send('Right')
                t.wait_for('hello world', timeout=3.0)
                t.send('q')

    def test_J_K_jump_between_voice_rows(self):
        """``J``/``K`` skip over tool_use / tool_result / metadata rows.

        Fixture (chronological list order since #475): user-text,
        user-tool_result, asst-tool_use, asst-text. After Right-
        expanding the session, the recipe lands the cursor on the
        latest voice (asst-text). One ``K`` (prev voice) should skip
        the two machinery rows and land on user-text.
        """
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, '.claude', 'projects')
            proj = os.path.join(root, '-home-test-jk')
            os.makedirs(proj)
            sess = os.path.join(proj, 'jk1234.jsonl')
            records = [
                {'type': 'user',
                 'message': {'role': 'user',
                             'content': 'PROBE_USER_VOICE'}},
                {'type': 'user',
                 'message': {'role': 'user', 'content': [
                     {'type': 'tool_result', 'tool_use_id': 'x',
                      'content': 'PROBE_TOOL_RESULT'},
                 ]},
                 'toolUseResult': 'PROBE_TOOL_RESULT'},
                {'type': 'assistant',
                 'message': {'role': 'assistant', 'content': [
                     {'type': 'tool_use', 'name': 'Bash',
                      'input': {'command': 'PROBE_TOOL_USE'}},
                 ]}},
                {'type': 'assistant',
                 'message': {'role': 'assistant', 'content': [
                     {'type': 'text', 'text': 'PROBE_ASST_VOICE'},
                 ]}},
            ]
            with open(sess, 'w') as f:
                for r in records:
                    f.write(_json.dumps(r) + '\n')
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--no-tree')
                t.wait_for('/home/test/jk')
                t.send('Right')
                t.wait_for('jk1234')
                t.send('Down')
                t.send('Right')
                # All four rows visible; cursor lands on the latest
                # voice (PROBE_ASST_VOICE) automatically.
                t.wait_for('PROBE_ASST_VOICE')
                t.wait_for('PROBE_USER_VOICE')
                # Press K (prev voice) — should skip the two
                # machinery rows above and land on PROBE_USER_VOICE.
                t.send('K')
                # The preview pane should now show the user's text body
                # — "▶ user" header is in the renderer, the body is the
                # PROBE marker.
                t.wait_for('▶ user', timeout=3.0)
                t.send('q')

    def _make_two_sessions(self, tmp, *, expand_b=True):
        """Project with two sessions, each holding 2 voice rows + machinery.

        Layout chronologically (on disk) for each session:
            voice_FIRST  →  tool_use  →  tool_result  →  voice_LAST

        After reverse-time rendering, each session's children read:
            voice_LAST, tool_result, tool_use, voice_FIRST

        ``A`` is the older session, ``B`` is the newer. The list sorts
        newest-first so B appears above A; ``expand_b`` controls whether
        the B subtree is opened too (the cross-subtree tests need it,
        the collapsed-tail test does not).
        """
        import json as _json
        root = os.path.join(tmp, '.claude', 'projects')
        proj = os.path.join(root, '-home-test-jk-multi')
        os.makedirs(proj)

        def _write_session(path, label, mtime):
            with open(path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user',
                    'message': {'role': 'user',
                                'content': f'{label}_VOICE_FIRST'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'name': 'Bash',
                         'input': {'command': f'{label}_TOOLCALL'}},
                    ]},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'user',
                    'message': {'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'x',
                         'content': f'{label}_TOOLRESULT'},
                    ]},
                    'toolUseResult': f'{label}_TOOLRESULT',
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'user',
                    'message': {'role': 'user',
                                'content': f'{label}_VOICE_LAST'},
                }) + '\n')
            os.utime(path, (mtime, mtime))

        sess_a = os.path.join(proj, 'A-sess.jsonl')
        sess_b = os.path.join(proj, 'B-sess.jsonl')
        _write_session(sess_a, 'A', mtime=1000.0)
        _write_session(sess_b, 'B', mtime=2000.0)   # newer → top
        return proj, sess_a, sess_b

    def test_J_forward_from_top_lands_on_first_voice(self):
        """(1) Two expanded sessions; J from the top of the tree finds first voice.

        Expand project → expand B (newer, top) → expand A. Then ``g``
        sends cursor to the very first row (the project row); ``J``
        walks forward across both session-row breadcrumbs and B's
        machinery rows and lands on B's newest voice — the first voice
        in display order.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj, sess_a, sess_b = self._make_two_sessions(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--no-tree')
                t.wait_for('/home/test/jk/multi')
                t.send('Right')                  # expand project
                t.wait_for('B-sess')
                t.send('Down')                   # cursor → B-sess
                t.send('Right')                  # expand B
                t.wait_for('B_VOICE_LAST')
                # Walk down past B's 4 expanded rows to A-sess, expand A.
                for _ in range(5):
                    t.send('Down')
                t.send('Right')                  # expand A
                t.wait_for('A_VOICE_LAST')
                # Cursor back to the top of the visible list.
                t.send('g')
                # First voice forward from the project row is B's newest
                # message (B_VOICE_LAST — B's last on disk, first in
                # the reverse-time render). cursor_to lands on it; the
                # preview pane refreshes to the user voice renderer.
                t.send('J')
                cap = t.wait_for('▶ user', timeout=3.0)
                self.assertIn('B_VOICE_LAST', cap)
                t.send('q')

    def test_K_backward_from_collapsed_tail_lands_on_last_voice(self):
        """(2) Last visible row is a collapsed subtree; K skips back to last voice.

        Cursor sits on A-sess (collapsed, bottom row). K walks upward
        through B's expanded subtree and stops at the most recent voice
        row above the cursor — which is B's *oldest* voice row
        (B_VOICE_FIRST), since the list within B is newest-first and
        we're walking up.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj, sess_a, sess_b = self._make_two_sessions(tmp)
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/jk/multi')
                t.send('Right')                  # expand project
                t.wait_for('A-sess')
                t.send('Down')                   # cursor → B-sess (newer, top)
                t.send('Right')                  # expand B
                t.wait_for('B_VOICE_LAST')
                # Now cursor is on B-sess (still). End jumps to last
                # visible row, which is A-sess (collapsed at the bottom).
                t.send('End')
                t.wait_for('A-sess')             # already visible — just settle
                # K should walk up past A-sess (kind=session, no row_bg),
                # past B's tool_use/tool_result/older-text-rows, and
                # land on the *closest voice above the cursor*, which is
                # B_VOICE_FIRST (B's oldest voice row, appearing near
                # the bottom of B's expansion).
                t.send('K')
                t.wait_for('▶ user', timeout=3.0)
                cap = t.capture()
                self.assertIn('B_VOICE_FIRST', cap)
                t.send('q')

    def test_JK_across_expanded_subtrees(self):
        """(3) Both A and B expanded; J/K cross the subtree boundary."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj, sess_a, sess_b = self._make_two_sessions(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/jk/multi')
                t.send('Right')                  # expand project
                t.wait_for('B-sess')
                t.send('Down')                   # cursor → B-sess
                t.send('Right')                  # expand B
                t.wait_for('B_VOICE_LAST')
                # Walk down past B's 4 rows to A-sess, then expand.
                # B's expansion: 4 visible message rows. Down 5 times
                # lands on A-sess. Down 1 more would go past it.
                for _ in range(5):
                    t.send('Down')
                t.send('Right')                  # expand A
                t.wait_for('A_VOICE_LAST')

                # Now cursor is on A-sess. Move into B's oldest voice
                # (B_VOICE_FIRST) — closest to the boundary between
                # B's subtree and A-sess.
                t.send('K')                      # K from A-sess
                cap = t.wait_for('▶ user', timeout=3.0)
                self.assertIn('B_VOICE_FIRST', cap)

                # Forward across the boundary: J from B's last voice
                # should cross A-sess (kind=session, skipped) and land
                # on A's first voice in display order = A_VOICE_LAST
                # (A's newest message, top of its expansion).
                t.send('J')
                cap = t.wait_for('A_VOICE_LAST', timeout=3.0)
                # Preview pane should also have refreshed.
                self.assertIn('▶ user', cap)

                # And K from A_VOICE_LAST should return us to B_VOICE_FIRST.
                t.send('K')
                cap = t.wait_for('B_VOICE_FIRST', timeout=3.0)
                self.assertIn('▶ user', cap)
                t.send('q')

    def test_K_scrolls_viewport_to_keep_cursor_on_screen(self):
        """``K`` on a long transcript should scroll-follow the cursor.

        Builds a session with many machinery rows between two voice
        rows so the second voice is well below the bottom of the pane
        from a normal cursor scroll, then verifies ``K`` from the
        topmost row (newest in the reverse-time list) lands the
        viewport on the older voice row.
        """
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, '.claude', 'projects')
            proj = os.path.join(root, '-home-test-jk3')
            os.makedirs(proj)
            sess = os.path.join(proj, 's.jsonl')
            with open(sess, 'w') as f:
                # Chronological order on disk; rendered list is reversed
                # so 'OLDER_VOICE' lands near the bottom of the rendered
                # list and 'NEWER_VOICE' near the top.
                f.write(_json.dumps({
                    'type': 'user',
                    'message': {'role': 'user',
                                'content': 'OLDER_VOICE'},
                }) + '\n')
                # 50 machinery rows in between.
                for i in range(50):
                    f.write(_json.dumps({
                        'type': 'assistant',
                        'message': {'role': 'assistant', 'content': [
                            {'type': 'tool_use', 'name': 'Bash',
                             'input': {'command': f'cmd{i}'}},
                        ]},
                    }) + '\n')
                f.write(_json.dumps({
                    'type': 'user',
                    'message': {'role': 'user',
                                'content': 'NEWER_VOICE'},
                }) + '\n')
            with TmuxFixture(cols=140, rows=20, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/jk3')
                t.send('Right')                  # expand project
                t.wait_for('s', timeout=3.0)
                t.send('Down')                   # cursor → session row
                t.send('Right')                  # expand session (J only walks visible)
                t.wait_for('NEWER_VOICE')
                # From the session row, J walks down through the
                # visible expansion and lands on the first voice row
                # (NEWER_VOICE, newest in reverse-time order).
                t.send('J')
                t.wait_for('▶ user', timeout=3.0)
                # Now J again should jump past 50 machinery rows to the
                # older voice. With only 20 rows in the pane, scroll
                # MUST follow or the cursor leaves the screen — and
                # without the preview update the user couldn't tell.
                t.send('J')
                # OLDER_VOICE must appear *in the list pane*, which only
                # happens if the viewport scrolled. Capture the pane and
                # look for the marker in the visible content.
                cap = t.wait_for('OLDER_VOICE', timeout=3.0)
                # And the preview pane should refresh to its body.
                t.wait_for('▶ user', timeout=3.0)
                t.send('q')

    def _make_tree_fixture(self, tmp, relocated=False):
        """Two turns + one Agent dispatch with a real subagent jsonl.

        When ``relocated`` is true the session ``.jsonl`` stays under
        ``-home-test-tree/`` but its subagent transcript lives under a
        **cwd-derived** project dir, mirroring what Claude Code does
        once a session enters a git worktree. Every record carries
        ``cwd=/home/test/tree/.worktrees/wt`` which encodes to
        ``-home-test-tree--worktrees-wt`` (``.`` and ``/`` both → ``-``),
        so ``agent-AGENT01.jsonl`` lands at
        ``-home-test-tree--worktrees-wt/tree-sess/subagents/`` rather
        than co-located with the session.
        """
        import json as _json
        root = os.path.join(tmp, '.claude', 'projects')
        proj = os.path.join(root, '-home-test-tree')
        os.makedirs(proj)
        sess = os.path.join(proj, 'tree-sess.jsonl')
        records = [
            {'type': 'permission-mode', 'permissionMode': 'plan'},
            # Turn 1.
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'promptId': 'P1',
             'message': {'role': 'user',
                         'content': 'PROBE_TURN1_USER'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'toolu_x', 'name': 'Task',
                  'input': {'prompt': 'PROBE_AGENT_PROMPT',
                            'subagent_type': 'Explore'}},
             ]}},
            {'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1',
             'promptId': 'P1',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 'toolu_x',
                  'content': 'PROBE_AGENT_RESULT'},
             ]},
             'toolUseResult': {'agentId': 'AGENT01',
                               'agentType': 'Explore',
                               'status': 'completed'}},
            {'type': 'assistant', 'uuid': 'a2', 'parentUuid': 'u2',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'PROBE_TURN1_REPLY'},
             ]}},
            # Turn 2 (new promptId → new turn root).
            {'type': 'user', 'uuid': 'u3', 'parentUuid': 'a2',
             'promptId': 'P2',
             'message': {'role': 'user',
                         'content': 'PROBE_TURN2_USER'}},
            {'type': 'assistant', 'uuid': 'a3', 'parentUuid': 'u3',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'PROBE_TURN2_REPLY'},
             ]}},
        ]
        if relocated:
            # The session moved into a worktree; every record records the
            # worktree cwd, which is where Claude Code stores subagents.
            cwd = '/home/test/tree/.worktrees/wt'
            for rec in records:
                rec['cwd'] = cwd
            enc = cwd.replace('/', '-').replace('.', '-')
            sub_dir = os.path.join(root, enc, 'tree-sess', 'subagents')
        else:
            sub_dir = os.path.join(proj, 'tree-sess', 'subagents')
        with open(sess, 'w') as f:
            for rec in records:
                f.write(_json.dumps(rec) + '\n')
        os.makedirs(sub_dir)
        agent_path = os.path.join(sub_dir, 'agent-AGENT01.jsonl')
        with open(agent_path, 'w') as f:
            f.write(json.dumps({
                'type': 'user', 'uuid': 'agU1', 'parentUuid': None,
                'message': {'role': 'user',
                            'content': 'PROBE_SUBAGENT_PROMPT'},
            }) + '\n')
        with open(os.path.join(sub_dir,
                               'agent-AGENT01.meta.json'), 'w') as f:
            json.dump({'agentType': 'Explore',
                       'description': 'PROBE_SUBAGENT_DESC'}, f)
        return sess

    def test_tree_flag_lands_on_latest_voice(self):
        """``--tree`` opens the session, expands ancestors, lands on latest voice."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_tree_fixture(tmp)
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                # Latest voice = PROBE_TURN2_REPLY (the final assistant
                # text). Preview pane should show its body.
                t.wait_for('PROBE_TURN2_REPLY', timeout=3.0)
                # Tree structure must be visible: both turn-roots
                # expanded as ancestors of the cursor row.
                cap = t.capture()
                self.assertIn('PROBE_TURN1_USER', cap)
                self.assertIn('PROBE_TURN2_USER', cap)
                # ▼ markers indicate the expanded subtrees of the
                # turn-roots between the scope root and the cursor.
                self.assertIn('▼', cap)
                t.send('q')

    def test_t_toggle_preserves_cursor(self):
        """``t`` flips tree↔flat without losing the cursor's message."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_tree_fixture(tmp)
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                t.wait_for('PROBE_TURN2_REPLY', timeout=3.0)
                # We synchronise on observable state transitions rather
                # than a fixed sleep, so the assertion never races a
                # mid-toggle repaint (or the async child-load that backs
                # the tree rebuild) under load. Each toggle:
                #   1. wait for a MODE-SPECIFIC marker that is absent in
                #      the other mode — proves the toggle was processed
                #      (not a stale pre-toggle frame). Tree mode shows
                #      ``<prompt>`` turn-root markers; flat mode replaces
                #      them with collapsed subagent rows (``[Explore``).
                #   2. wait for PROBE_TURN2_REPLY — proves the cursor's
                #      message re-rendered (the tree expand briefly shows
                #      a ``⧗ loading…`` placeholder before children land).
                self.assertIn('<prompt>', t.capture())
                # Flip to flat — preview should still target the same
                # message (PROBE_TURN2_REPLY).
                t.send('t')
                t.wait_for('[Explore', timeout=3.0)
                cap_flat = t.wait_for('PROBE_TURN2_REPLY', timeout=3.0)
                self.assertIn('PROBE_TURN2_REPLY', cap_flat,
                              f'cursor lost on tree→flat: {cap_flat[:400]!r}')
                # Flip back to tree — the ``<prompt>`` turn-root marker
                # returns once the tree view is rebuilt.
                t.send('t')
                t.wait_for('<prompt>', timeout=3.0)
                cap_back = t.wait_for('PROBE_TURN2_REPLY', timeout=3.0)
                self.assertIn('PROBE_TURN2_REPLY', cap_back,
                              f'cursor lost on flat→tree: {cap_back[:400]!r}')
                t.send('q')

    def test_tree_right_on_session_jumps_to_latest_turn_root(self):
        """Drilling into a session row in tree mode lands on the latest turn root.

        Single-level: cursor lands on the latest user voice (turn root)
        among the session's direct children. The user can press Right
        again to drill into that turn and land on the assistant reply.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_tree_fixture(tmp)
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--tree')
                t.wait_for('/home/test/tree', timeout=3.0)
                t.send('Right')                  # expand project
                t.wait_for('tree-sess', timeout=3.0)
                t.send('Down')                   # cursor → session row
                t.send('Right')                  # expand session — one level
                # Latest turn root (PROBE_TURN2_USER) is where the
                # cursor should land.
                t.wait_for('PROBE_TURN2_USER', timeout=3.0)
                cap = t.capture()
                self.assertIn('▶ user', cap)
                self.assertIn('PROBE_TURN2_USER', cap)
                # Pressing Right again should drill into turn 2 and
                # land on its latest voice (PROBE_TURN2_REPLY).
                t.send('Right')
                t.wait_for('PROBE_TURN2_REPLY', timeout=3.0)
                t.send('q')

    def test_tree_expand_assistant_shows_subagent(self):
        """Expanding the Agent-calling assistant row reveals the subagent.

        The fixture has two turns: turn 1 dispatched the subagent;
        turn 2 is just text. Auto-cursor-on-open lands inside turn 2,
        so we use ``K`` to walk back to turn 1's user voice, expand
        turn 1, then drill into the Task-calling assistant row.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_tree_fixture(tmp)
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                t.wait_for('PROBE_TURN2_REPLY', timeout=3.0)
                # K walks UP through visible voices. From PROBE_TURN2_REPLY,
                # the voice rows above (in order) are:
                #   • u3 leaf      (PROBE_TURN2_USER)
                #   • <prompt:5>   (umbrella for turn 2; same content)
                #   • <prompt:1>   (turn 1 root, collapsed at top level)
                # Three K's land cursor on the <prompt:1> umbrella row,
                # ready to be expanded with Right.
                t.send('K')
                t.send('K')
                t.send('K')
                t.wait_for('PROBE_TURN1_USER', timeout=3.0)
                # Right in tree mode expands the row AND auto-jumps the
                # cursor to the latest voice inside the subtree. After
                # expanding turn 1, cursor lands on PROBE_TURN1_REPLY
                # (the final assistant text leaf). Up two rows lands
                # on the Task-calling <tool:Task> umbrella (one row up
                # for the tool umbrella, since the assistant leaf
                # sits a level inside).
                t.send('Right')
                t.wait_for('PROBE_TURN1_REPLY', timeout=3.0)
                t.send('Up')
                t.send('Right')                  # expand Task umbrella
                t.wait_for('PROBE_SUBAGENT_DESC', timeout=3.0)
                t.send('q')

    def test_tree_expand_assistant_shows_relocated_subagent(self):
        """Tree mode reveals a worktree-relocated subagent inline.

        Same flow as ``test_tree_expand_assistant_shows_subagent`` but
        the subagent transcript lives under the cwd-derived worktree
        project dir, not co-located with the session ``.jsonl``. The
        Task-calling assistant row must still surface the subagent
        (``PROBE_SUBAGENT_DESC``), and drilling into it must reveal its
        transcript line — proving the tree-mode resolution sites route
        through ``_resolve_agent_jsonl``.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_tree_fixture(tmp, relocated=True)
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                t.wait_for('PROBE_TURN2_REPLY', timeout=3.0)
                # Walk back to turn 1's root umbrella (see the co-located
                # variant for the row-by-row reasoning behind the 3 K's).
                t.send('K')
                t.send('K')
                t.send('K')
                t.wait_for('PROBE_TURN1_USER', timeout=3.0)
                t.send('Right')                  # expand turn 1
                t.wait_for('PROBE_TURN1_REPLY', timeout=3.0)
                t.send('Up')
                t.send('Right')                  # expand Task umbrella
                # The relocated subagent must surface inline under the
                # dispatching assistant — fails before routing through
                # the resolver (the agent_link is never built because
                # the co-located subagents dir is empty).
                t.wait_for('PROBE_SUBAGENT_DESC', timeout=3.0)
                # Drill into the subagent row: its transcript line must
                # render, proving get_children resolves the relocated
                # agent jsonl too.
                t.send('Right')
                t.wait_for('PROBE_SUBAGENT_PROMPT', timeout=3.0)
                t.send('q')

    def test_user_assistant_rows_have_row_bg(self):
        """Conversational rows should render with ``\\e[48;5;...m`` bg stripes.

        Verifies the recipe's ``_ROW_BG_FOR_KIND`` + the framework's
        ``_write_segments(row_bg=…)`` plumbing end-to-end. Captures
        the tmux pane with ``-e`` (colors) and asserts the 256-color
        background SGR for user (235) appears in the output.

        After the session-row expansion the cursor lands on the
        latest voice (the user row) and the reverse-video selection
        hides its bg stripe. ``Up`` moves cursor off so the stripe is
        observable in the capture.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Fixture has a user message; drill in to see it as a row.
            _make_fake_claude(tmp)
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--no-tree')
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef')
                t.send('Down')
                t.send('Right')
                t.wait_for('hello world', timeout=3.0)
                # Move cursor off the user row so its bg stripe is
                # visible (reverse-video selection masks the stripe).
                t.send('Up')
                t.wait_stable()
                colored = t.capture(colors=True)
                # The user-voice bg colour code we configured for browse-claude.
                self.assertIn('48;5;235', colored,
                              'expected user-row bg escape in colored capture')
                t.send('q')

    def test_positional_jsonl_shows_preview_for_top_row(self):
        """Passing a .jsonl positional puts its session preview on top.

        The top row is the scope_root row; previously the framework
        skipped preview fetches for scope_root, leaving the pane blank
        when the user launched into a single file. Regression for that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp)
            sess = os.path.join(
                tmp, '.claude', 'projects', '-home-test-project',
                'abcd1234-deadbeef.jsonl',
            )
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, sess)
                # The user prompt from the fake fixture should land on
                # first paint (preview cascades over the session's
                # children, same as any umbrella).
                t.wait_for('hello world', timeout=3.0)
                t.send('q')

    def test_live_tail_picks_up_appended_record(self):
        """Append to the cursor's file mid-run; the new row appears.

        Verifies the background tailer's path: bulk-scan on launch
        populates _TAIL_STATE, then a write from outside is detected
        on the next 5s tick and folded into the cached _TreeData
        with update_data patches. We append a fresh turn root so the
        new ``<prompt>`` umbrella shows up at the file's root level.
        """
        import tempfile, json as _json, time
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '.claude', 'projects', '-home-test-tail')
            os.makedirs(proj)
            sess = os.path.join(proj, 'tail-sess.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user',
                                'content': 'PROBE_INITIAL'},
                }) + '\n')
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                t.wait_for('PROBE_INITIAL', timeout=3.0)
                # Append a new turn while the recipe is running. The
                # tailer polls every 5s, so we wait up to ~7s for it
                # to fold the new record + the framework to redraw.
                with open(sess, 'a') as f:
                    f.write(_json.dumps({
                        'type': 'user', 'uuid': 'u2',
                        'message': {'role': 'user',
                                    'content': 'PROBE_APPENDED'},
                    }) + '\n')
                t.wait_for('PROBE_APPENDED', timeout=7.5)
                t.send('q')

    def test_live_tail_flat_mode_picks_up_appended_record(self):
        """Same as the tree-mode test, but in flat mode (newest-first list).

        Flat mode never calls ``_scan_tree`` so tail state bootstraps
        lazily inside the worker; the listing rebuilds via
        ``b.refresh(path)`` rather than incremental ``update_data``
        upserts (which would put new rows in the wrong place since
        the list is newest-first).
        """
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '.claude', 'projects', '-home-test-tailf')
            os.makedirs(proj)
            sess = os.path.join(proj, 'tail-sess.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user',
                                'content': 'PROBE_INITIAL_FLAT'},
                }) + '\n')
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                # ``--item 0`` makes the recipe scope INTO the session
                # (rather than landing on a project-list row) — so the
                # message rows render in the list directly.
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--no-tree', '--file', sess, '--item', '0')
                t.wait_for('PROBE_INITIAL_FLAT', timeout=3.0)
                with open(sess, 'a') as f:
                    f.write(_json.dumps({
                        'type': 'user', 'uuid': 'u2',
                        'message': {'role': 'user',
                                    'content': 'PROBE_APPENDED_FLAT'},
                    }) + '\n')
                t.wait_for('PROBE_APPENDED_FLAT', timeout=7.5)
                t.send('q')

    def test_drills_into_subagent(self):
        """Expanding a subagent reveals its own transcript lines.

        Drill project -> session -> subagent group, then verify the
        subagent's first user message ('subagent task: do the thing')
        renders. Distinct from the parent session's 'hello world' so
        we know we routed into the right .jsonl.

        After #475 expanding a session jumps the cursor to the latest
        voice (a record row, not a subagent). Navigate Home to the
        top of the session's children (where subagents sit) before
        expanding the subagent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp, with_subagents=True)
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--no-tree')
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef')
                t.send('Down')
                t.send('Right')
                t.wait_for('PROBE-DESC', timeout=3.0)
                t.wait_stable()
                # Navigate to the subagent row. Cursor landed on the
                # latest voice inside the session (user "hello world")
                # after expanding; the subagent group sits at the top
                # of the session's children (above the records). 3
                # Up presses reach it past the 3 record rows.
                # (Using Home would pin the cursor to row 0 and the
                # framework's anchor-replay would fight subsequent
                # Down presses on async children deliveries.)
                t.send('Up')
                t.send('Up')
                t.send('Up')
                t.send('Right')   # expand subagent
                t.wait_for('subagent task', timeout=5.0)
                t.send('q')

    def test_relocated_subagent_count_tag(self):
        """Session count tag picks up worktree-relocated subagents.

        The session .jsonl lives under ``-home-test-project/`` but its
        subagents were stored under the cwd-derived worktree project
        dir. The ``· N sub`` tag on the session row must still reflect
        them — proving ``_count_subagents`` resolves via session cwds,
        not just the co-located dir.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp, relocated=True)
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--no-tree')
                t.wait_for('/home/test/project')
                t.send('Right')
                # The session row's tag shows ``· 1 sub`` only if the
                # relocated subagent dir was discovered.
                t.wait_for('1 sub', timeout=3.0)
                t.send('q')

    def test_relocated_subagent_listed_under_session(self):
        """A worktree-relocated subagent shows as a sibling of the messages.

        Same as ``test_lists_subagents_under_session`` but the subagent
        transcript lives under the cwd-derived project dir rather than
        co-located with the session .jsonl.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp, relocated=True)
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--no-tree')
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef')
                t.send('Down')
                t.send('Right')
                t.wait_for('PROBE-DESC', timeout=3.0)
                t.send('q')

    def test_drills_into_relocated_subagent(self):
        """Expanding a worktree-relocated subagent reveals its transcript.

        Mirrors ``test_drills_into_subagent`` but the subagent .jsonl is
        stored under the cwd-derived worktree project dir, so reaching
        its lines exercises ``_resolve_agent_jsonl`` rather than the
        co-located fast path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp, relocated=True)
            with TmuxFixture(cols=140, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--no-tree')
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef')
                t.send('Down')
                t.send('Right')
                t.wait_for('PROBE-DESC', timeout=3.0)
                t.wait_stable()
                # After expanding the session the cursor lands on the
                # latest *voice* — the ``hello world`` user turn (the
                # appended Agent tool_use / tool_result rows aren't
                # voice). The subagent group sits at the top of the
                # session's children, above the 2 metadata rows; 3 Up
                # presses reach it (matches test_drills_into_subagent).
                t.send('Up')
                t.send('Up')
                t.send('Up')
                t.send('Right')   # expand subagent
                t.wait_for('subagent task', timeout=5.0)
                t.send('q')


    def test_h_toggle_hides_non_voice_rows(self):
        """Pressing 'h' hides non-voice umbrellas and leaves in tree mode.

        Fixture has a user voice (``PROBE_VOICE``) and one assistant
        tool_use (``PROBE_TOOL_CALL``). Under ``--tree``, the tool wraps
        in a ``<tool:Bash>`` umbrella that is **not** voice-bearing
        (pure tool_use, no text), so after ``h`` the umbrella row and
        its leaf both disappear and the preview composes only from the
        voice content.
        """
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '.claude', 'projects', '-home-test-htog')
            os.makedirs(proj)
            sess = os.path.join(proj, 'htog.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user', 'content': 'PROBE_VOICE'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1',
                    'parentUuid': 'u1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'id': 't1',
                         'name': 'Bash',
                         'input': {'command': 'PROBE_TOOL_CALL'}},
                    ]}},
                ) + '\n')
            import time
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                t.wait_for('PROBE_VOICE', timeout=3.0)
                t.wait_for('PROBE_TOOL_CALL', timeout=3.0)
                t.send('h')
                # Allow the mod batch + preview re-fetch to settle.
                time.sleep(0.4)
                # PROBE_VOICE survives in the (still-visible) prompt
                # umbrella and the user-voice leaf.
                cap = t.capture()
                self.assertIn('PROBE_VOICE', cap)
                # PROBE_TOOL_CALL should be gone from list AND preview
                # (preview respects ``hidden`` on children).
                self.assertNotIn('PROBE_TOOL_CALL', cap,
                                 'tool_use should not appear on screen '
                                 'after filter is on; got: '
                                 + cap[-1000:])
                t.send('q')

    def test_h_toggle_round_trip_restores_view(self):
        """``h h`` round-trip restores the original visible list."""
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '.claude', 'projects', '-home-test-hrt')
            os.makedirs(proj)
            sess = os.path.join(proj, 'hrt.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user', 'content': 'PROBE_RT_VOICE'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1',
                    'parentUuid': 'u1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'id': 't1',
                         'name': 'Bash',
                         'input': {'command': 'PROBE_RT_TOOL'}},
                    ]}},
                ) + '\n')
            import time
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                t.wait_for('PROBE_RT_VOICE', timeout=3.0)
                t.wait_for('PROBE_RT_TOOL', timeout=3.0)
                t.send('h')
                time.sleep(0.4)
                cap_on = t.capture()
                self.assertNotIn('PROBE_RT_TOOL', cap_on,
                                 'filter on: tool should be hidden')
                t.send('h')
                time.sleep(0.4)
                cap_off = t.capture()
                self.assertIn('PROBE_RT_VOICE', cap_off)
                self.assertIn('PROBE_RT_TOOL', cap_off,
                              'filter off: tool should be visible again')
                t.send('q')

    def test_cursor_lands_on_last_voice_in_large_file(self):
        """On a big file whose root-level fetch takes longer than the
        startup ``run_until_idle`` window, the recipe's ``cursor_to``
        on the last voice must still win.

        Regression for: the framework's startup ``_reanchor_cursor``
        was clobbering an already-set recipe anchor when the target
        row hadn't loaded yet, leaving the cursor stranded on the
        scope_root row forever.
        """
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '.claude', 'projects', '-home-test-big')
            os.makedirs(proj)
            sess = os.path.join(proj, 'big.jsonl')
            # ~12000 records: large enough that the root fetch + its
            # apply pass exceeds the 0.5s ``run_until_idle`` budget.
            with open(sess, 'w') as f:
                prev = None
                for turn in range(1500):
                    u = f'u{turn:05d}'
                    f.write(_json.dumps({
                        'type': 'user', 'uuid': u, 'parentUuid': prev,
                        'message': {'role': 'user',
                                    'content': f'turn {turn}'},
                    }) + '\n'); prev = u
                    for k in range(3):
                        a = f'a{turn:05d}_{k}'
                        f.write(_json.dumps({
                            'type': 'assistant', 'uuid': a,
                            'parentUuid': prev,
                            'message': {'role': 'assistant', 'content': [
                                {'type': 'tool_use', 'id': f't{turn}_{k}',
                                 'name': 'Bash', 'input': {'cmd': 'echo'}}]},
                        }) + '\n'); prev = a
                        r = f'r{turn:05d}_{k}'
                        f.write(_json.dumps({
                            'type': 'user', 'uuid': r, 'parentUuid': prev,
                            'message': {'role': 'user', 'content': [
                                {'type': 'tool_result',
                                 'tool_use_id': f't{turn}_{k}',
                                 'content': 'out'}]},
                        }) + '\n'); prev = r
                    a = f'a{turn:05d}_done'
                    f.write(_json.dumps({
                        'type': 'assistant', 'uuid': a, 'parentUuid': prev,
                        'message': {'role': 'assistant', 'content': [
                            {'type': 'text',
                             'text': f'PROBE_LAST_VOICE_{turn}'}]},
                    }) + '\n'); prev = a
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                # The last voice is in turn 1499 — give it long enough
                # for the scan + initial fetches + cursor snap. Without
                # the framework fix, this times out: the cursor stays
                # parked on the scope_root row indefinitely.
                t.wait_for('PROBE_LAST_VOICE_1499', timeout=10.0)
                t.send('q')

    def test_no_show_all_starts_in_filtered_mode(self):
        """``--no-show-all`` boots straight into the voice-only view."""
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '.claude', 'projects', '-home-test-cli')
            os.makedirs(proj)
            sess = os.path.join(proj, 'cli.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user', 'content': 'PROBE_BOOT_VOICE'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1',
                    'parentUuid': 'u1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'id': 't1',
                         'name': 'Bash',
                         'input': {'command': 'PROBE_BOOT_TOOL'}},
                    ]}},
                ) + '\n')
            with TmuxFixture(cols=160, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--no-show-all', '--file', sess)
                t.wait_for('PROBE_BOOT_VOICE', timeout=3.0)
                cap = t.capture()
                self.assertNotIn('PROBE_BOOT_TOOL', cap,
                                 'tool row should be hidden on boot under '
                                 '--no-show-all; got: ' + cap[-800:])
                t.send('q')

    # -- SendMessage inter-agent round-trip (#643/#649) --------------------

    def _make_sendmessage_fixture(self, tmp):
        """A session holding one full leader↔worker SendMessage round-trip.

        Five records, chronological on disk:

          1. a turn-opening human ``user`` prompt (so the session has a
             genuine human turn root above the exchange);
          2. an ``assistant`` record carrying the outbound ``SendMessage``
             tool_use (recipient / summary / message markdown);
          3. the ``{success, message}`` delivery ack tool_result;
          4. a ``user`` ``<task-notification>`` record — the worker's
             inbound reply (task-id / tool-use-id / status / summary /
             result markdown).

        The PROBE_ markers are unique so the tmux assertions can pin each
        rendered fragment. Returns the session ``.jsonl`` path.
        """
        import json as _json
        root = os.path.join(tmp, '.claude', 'projects')
        proj = os.path.join(root, '-home-test-sendmsg')
        os.makedirs(proj)
        sess = os.path.join(proj, 'sendmsg-sess.jsonl')
        recipient = 'PROBE_WORKER_7'
        tool_use_id = 'toolu_SENDMSG01'
        records = [
            # Human turn root.
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'promptId': 'P1',
             'message': {'role': 'user',
                         'content': 'PROBE_HUMAN_PROMPT'}},
            # Outbound: leader → worker SendMessage.
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': tool_use_id,
                  'name': 'SendMessage',
                  'input': {'recipient': recipient,
                            'summary': 'PROBE_SEND_SUMMARY',
                            'message': '## PROBE_SEND_BODY heading'}},
             ]}},
            # Delivery ack — shape is exactly {success, message}.
            {'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': tool_use_id,
                  'content': 'PROBE_ACK_RAW'},
             ]},
             'toolUseResult': {
                 'success': True,
                 'message': ('Message delivered to ' + recipient
                             + '. Output: /tmp/x/tasks/'
                             + recipient + '.output')}},
            # Inbound: worker → leader task-notification reply.
            {'type': 'user', 'uuid': 'u3', 'parentUuid': 'u2',
             'promptId': 'P-notify',
             'message': {'role': 'user', 'content': (
                 '<task-notification>'
                 '<task-id>' + recipient + '</task-id>'
                 '<tool-use-id>' + tool_use_id + '</tool-use-id>'
                 '<output-file>/tmp/x/tasks/' + recipient + '.output'
                 '</output-file>'
                 '<status>completed</status>'
                 '<summary>PROBE_REPLY_SUMMARY</summary>'
                 '<result>## PROBE_REPLY_BODY heading</result>'
                 '</task-notification>')}},
        ]
        with open(sess, 'w') as f:
            for rec in records:
                f.write(_json.dumps(rec) + '\n')
        return sess

    def test_flat_sendmessage_round_trip_renders(self):
        """Flat mode: outbound, ack and inbound each render with their kind.

        Drives ``--no-tree`` and scopes straight into the session
        (``--item 0``) so the message rows list directly. Asserts the
        outbound ``→ <recipient>`` header + message markdown and the
        ``agent-send`` tag; the inbound ``← …`` one-liner + result
        markdown and the ``agent-reply`` tag (NOT ``user``); and the
        compact ``✓ delivered`` ack form.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_sendmessage_fixture(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--no-tree', '--file', sess, '--item', '0')
                # Scoped via ``--item 0`` the rows list chronologically;
                # the inbound reply one-liner (``← <task_id> · <status> ·
                # <summary>``) is the last (latest voice) row.
                t.wait_for('PROBE_REPLY_SUMMARY', timeout=3.0)
                cap = t.capture()
                # Outbound one-liner + its distinct kind tag.
                self.assertIn('PROBE_WORKER_7', cap)
                self.assertIn('agent-send', cap,
                              'outbound row should tag as agent-send; '
                              'got: ' + cap[-1200:])
                # Inbound tagged agent-reply, NOT the human ``user`` kind.
                self.assertIn('agent-reply', cap,
                              'inbound reply should tag as agent-reply; '
                              'got: ' + cap[-1200:])
                # The ``←`` direction glyph distinguishes the reply.
                self.assertIn('←', cap)
                # Drill into the outbound row's preview: header + markdown.
                # Cursor lands on the latest voice (the reply, bottom row);
                # K walks up to the outbound SendMessage voice row.
                # Synchronise on PROBE_SEND_BODY: it lives only in the
                # SendMessage ``message`` field, so it appears solely in
                # the preview once the cursor reaches the outbound row —
                # unlike ``→ PROBE_WORKER_7`` which is also in the list
                # row and would match before the preview rendered.
                t.send('K')
                cap2 = t.wait_for('PROBE_SEND_BODY', timeout=3.0)
                self.assertIn('→ PROBE_WORKER_7', cap2,
                              'outbound preview should render the '
                              'recipient header; got: ' + cap2[-1200:])
                t.send('q')

    def test_flat_sendmessage_inbound_preview_and_ack(self):
        """Flat mode: inbound preview renders its result; ack is compact.

        On open the cursor lands on the latest voice — the inbound reply
        — so its preview (``← task-notification`` header + ``<result>``
        markdown) renders immediately. The ack row, sitting between the
        two voice rows, renders its compact ``✓ delivered`` status when
        the cursor reaches it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_sendmessage_fixture(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--no-tree', '--file', sess, '--item', '0')
                # Inbound reply preview on first paint.
                cap = t.wait_for('← task-notification', timeout=3.0)
                self.assertIn('PROBE_REPLY_BODY', cap,
                              'inbound preview should render result '
                              'markdown; got: ' + cap[-1200:])
                # The cursor lands on the latest voice (the reply, the
                # bottom row). The ack tool_result sits directly above it
                # in the chronological flat list; move the cursor onto it
                # so its compact ``✓ delivered`` preview renders. The long
                # ``Output: /tmp`` path must be trimmed from that view.
                t.send('Up')
                cap2 = t.wait_for('✓ delivered', timeout=3.0)
                self.assertNotIn('.output', cap2,
                                 'ack should trim the long Output: path; '
                                 'got: ' + cap2[-1200:])
                t.send('q')

    def test_flat_sendmessage_voice_filter_keeps_voice_hides_ack(self):
        """Flat voice-only: outbound + inbound survive, the ack is hidden.

        Boots ``--no-show-all`` (voice-only). The outbound SendMessage and
        the inbound task-notification both classify as voice, so their
        rows stay; the ``{success, message}`` ack is a plain tool_result
        (machinery) and must be filtered out.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_sendmessage_fixture(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--no-tree', '--no-show-all',
                         '--file', sess, '--item', '0')
                t.wait_for('PROBE_REPLY_SUMMARY', timeout=3.0)
                cap = t.capture()
                # Both voice halves of the channel remain listed.
                self.assertIn('agent-send', cap,
                              'outbound should survive voice-only filter; '
                              'got: ' + cap[-1200:])
                self.assertIn('agent-reply', cap,
                              'inbound should survive voice-only filter; '
                              'got: ' + cap[-1200:])
                # The delivery ack (tool_result machinery) is filtered out
                # — its one-liner prefix ``↳ tool_result`` must be gone.
                self.assertNotIn('↳ tool_result', cap,
                                 'ack should be hidden under voice-only; '
                                 'got: ' + cap[-1200:])
                t.send('q')

    def test_tree_sendmessage_reply_umbrella_not_prompt(self):
        """Tree mode: the task-notification turn umbrella reads ``<reply>``.

        The inbound task-notification opens its own turn (so the leader's
        follow-up nests under it), but its umbrella must NOT masquerade as
        a human ``<prompt>`` — it carries the ``<reply>`` prefix and the
        ``agent-reply`` kind. The genuine human turn keeps ``<prompt>``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_sendmessage_fixture(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--tree')
                t.wait_for('/home/test/sendmsg', timeout=3.0)
                t.send('Right')                  # expand project
                t.wait_for('sendmsg-sess', timeout=3.0)
                t.send('Down')                   # cursor → session row
                t.send('Right')                  # expand session — one level
                # The session's direct children are the two turn roots:
                # the human ``<prompt>`` and the task-notification turn.
                cap = t.wait_for('<reply>', timeout=3.0)
                # The agent-reply turn umbrella reads ``<reply>``, not
                # ``<prompt>``, and surfaces its ``←`` one-liner.
                self.assertIn('<reply>', cap,
                              'task-notification turn should use the '
                              '<reply> prefix; got: ' + cap[-1400:])
                self.assertIn('PROBE_REPLY_SUMMARY', cap)
                # The genuine human turn keeps the ``<prompt>`` prefix.
                self.assertIn('<prompt>', cap,
                              'human turn should keep <prompt>; '
                              'got: ' + cap[-1400:])
                t.send('q')

    def test_tree_sendmessage_outbound_umbrella_and_stripes(self):
        """Tree mode: the SendMessage tool umbrella carries the agent-send kind.

        Drilling into the human turn reveals the ``<tool:SendMessage>``
        umbrella; its kind is ``agent-send`` and the outbound header
        renders in its preview. The inbound reply, an ``agent-reply`` row,
        is NOT re-parented under that umbrella — it stays a sibling turn.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_sendmessage_fixture(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--file', sess)
                # Latest voice = the inbound reply turn; its preview shows
                # the task-notification render.
                t.wait_for('← task-notification', timeout=3.0)
                cap0 = t.capture()
                self.assertIn('PROBE_REPLY_BODY', cap0)
                # Walk up to the human turn root and expand it to reveal
                # the SendMessage tool umbrella.
                t.send('K')
                t.send('K')
                t.send('K')
                t.wait_for('PROBE_HUMAN_PROMPT', timeout=3.0)
                t.send('Right')                  # expand the human turn
                # The <tool:SendMessage> umbrella surfaces under the turn.
                cap = t.wait_for('SendMessage', timeout=3.0)
                self.assertIn('→ PROBE_WORKER_7', cap,
                              'outbound one-liner should show under the '
                              'turn; got: ' + cap[-1400:])
                t.send('q')

    def test_tree_sendmessage_voice_filter_keeps_voice_hides_ack(self):
        """Tree voice-only: outbound + inbound survive, the ack is hidden."""
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_sendmessage_fixture(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--no-show-all', '--file', sess)
                # Inbound reply still lands as the latest voice.
                t.wait_for('← task-notification', timeout=3.0)
                # Walk up to the human turn and expand it; the
                # SendMessage outbound voice row survives the filter
                # while its ack tool_result is hidden.
                t.send('K')
                t.send('K')
                t.send('K')
                t.wait_for('PROBE_HUMAN_PROMPT', timeout=3.0)
                t.send('Right')                  # expand human turn
                cap = t.wait_for('SendMessage', timeout=3.0)
                self.assertIn('→ PROBE_WORKER_7', cap,
                              'outbound should survive voice-only filter '
                              'in tree mode; got: ' + cap[-1400:])
                self.assertNotIn('↳ tool_result', cap,
                                 'ack should be hidden under voice-only '
                                 'in tree mode; got: ' + cap[-1400:])
                t.send('q')


if __name__ == '__main__':
    unittest.main()
