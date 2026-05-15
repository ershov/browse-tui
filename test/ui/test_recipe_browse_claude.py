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
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--no-tree', '--pid', str(pid))
                # The session row's preview should include the
                # session-card "session:" line.
                t.wait_for('session:', timeout=3.0)
                # And the user message we put in the fake jsonl should
                # land in the timeline (proving we did a full session
                # scan, not just rendered the breadcrumb).
                t.wait_for('hello world', timeout=3.0)
                t.send('q')

    def test_J_K_jump_between_voice_rows(self):
        """``J``/``K`` skip over tool_use / tool_result / metadata rows.

        Fixture mixes (newest-first as the list renders): asst-text,
        asst-tool_use, user-tool_result, user-text. From the top
        (asst-text, the newest voice row), one ``J`` should land on
        user-text — skipping the two machinery rows between them.
        """
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, '.claude', 'projects')
            proj = os.path.join(root, '-home-test-jk')
            os.makedirs(proj)
            sess = os.path.join(proj, 'jk1234.jsonl')
            # Chronological order on disk; rendered list is reversed.
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
                # All four rows visible.
                t.wait_for('PROBE_ASST_VOICE')
                t.wait_for('PROBE_USER_VOICE')
                # Move cursor onto the assistant voice row (newest, first).
                t.send('Down')
                # Now press J — should skip the two machinery rows below
                # and land on the user voice row. We assert by reading the
                # preview pane (which the recipe updates to match cursor)
                # for the user prompt body.
                t.send('J')
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

    def _make_tree_fixture(self, tmp):
        """Two turns + one Agent dispatch with a real subagent jsonl."""
        import json as _json
        root = os.path.join(tmp, '.claude', 'projects')
        proj = os.path.join(root, '-home-test-tree')
        os.makedirs(proj)
        sess = os.path.join(proj, 'tree-sess.jsonl')
        with open(sess, 'w') as f:
            for rec in [
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
            ]:
                f.write(_json.dumps(rec) + '\n')
        sub_dir = os.path.join(proj, 'tree-sess', 'subagents')
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
                # Capture cursor row's preview-pane body BEFORE toggle.
                cap_tree = t.capture()
                self.assertIn('PROBE_TURN2_REPLY', cap_tree)
                # Flip to flat — preview should still target the same
                # message (PROBE_TURN2_REPLY). The structure changes
                # (no ▼ tree markers under the scope row in flat).
                t.send('t')
                # Wait long enough for refresh + cursor_to to land.
                import time
                time.sleep(0.15)
                cap_flat = t.capture()
                self.assertIn('PROBE_TURN2_REPLY', cap_flat,
                              f'cursor lost on tree→flat: {cap_flat[:400]!r}')
                # Flip back to tree.
                t.send('t')
                time.sleep(0.15)
                cap_back = t.capture()
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

    def test_user_assistant_rows_have_row_bg(self):
        """Conversational rows should render with ``\\e[48;5;...m`` bg stripes.

        Verifies the recipe's ``_ROW_BG_FOR_KIND`` + the framework's
        ``_write_segments(row_bg=…)`` plumbing end-to-end. Captures
        the tmux pane with ``-e`` (colors) and asserts the 256-color
        background SGR for user (235) appears in the output.
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
                # Session card preview should land on first paint.
                t.wait_for('session:', timeout=3.0)
                # And the user prompt from the fake fixture too.
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
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--no-tree', '--file', sess)
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
                # Subagent rows sort first per the recipe — cursor should
                # be on it after the inward drill, so Right expands it.
                t.send('Right')
                t.wait_for('subagent task', timeout=3.0)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
