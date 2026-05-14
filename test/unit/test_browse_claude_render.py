"""Unit tests for the per-message renderers in ``recipes/browse-claude``.

The recipe is a single-file ``--run-py`` script that imports
``browse_tui`` (only available when the binary loads it). To exercise
the renderer functions directly we stub ``browse_tui`` in
``sys.modules`` and load the recipe via ``importlib`` — only the
top-level ``Action``/``Browser``/``Item`` references resolve to the
stub, none of the renderers touch them.

Coverage focuses on the dispatcher (``_classify`` + ``_RENDERERS``),
each per-kind renderer's salient fields, and the chrome footer. ANSI
output is asserted by checking for the palette constants — and the
NO_COLOR pathway is asserted by re-loading the module with the
constants zeroed.
"""

import datetime
import importlib.util
import os
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-claude'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import."""
    if 'browse_tui' in sys.modules:
        return
    mod = types.ModuleType('browse_tui')

    class _Stub:
        def __init__(self, *a, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    mod.Action = _Stub
    mod.Browser = _Stub
    mod.Item = _Stub
    sys.modules['browse_tui'] = mod


def _load_recipe(force_color=True):
    """Load (or reload) the recipe; returns the module.

    ``force_color`` controls whether ANSI constants are kept (True) or
    zeroed via ``NO_COLOR`` (False) — exercises both code paths.
    """
    _stub_browse_tui()
    saved_no_color = os.environ.get('NO_COLOR')
    saved_force_color = os.environ.get('FORCE_COLOR')
    try:
        if force_color:
            os.environ['FORCE_COLOR'] = '1'
            os.environ.pop('NO_COLOR', None)
        else:
            os.environ['NO_COLOR'] = '1'
            os.environ.pop('FORCE_COLOR', None)
        # ``recipes/browse-claude`` has no ``.py`` extension; importlib's
        # default loader-from-extension lookup returns None. Use the
        # source loader explicitly.
        name = f'_browse_claude_{int(force_color)}'
        loader = SourceFileLoader(name, str(_RECIPE))
        spec = importlib.util.spec_from_loader(name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        if not force_color:
            mod._init_color()
        return mod
    finally:
        if saved_no_color is None:
            os.environ.pop('NO_COLOR', None)
        else:
            os.environ['NO_COLOR'] = saved_no_color
        if saved_force_color is None:
            os.environ.pop('FORCE_COLOR', None)
        else:
            os.environ['FORCE_COLOR'] = saved_force_color


class TestClassify(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_user(self):
        self.assertEqual(self.r._classify({'type': 'user'}), 'user')

    def test_assistant(self):
        self.assertEqual(self.r._classify({'type': 'assistant'}), 'assistant')

    def test_attachment(self):
        self.assertEqual(self.r._classify({'type': 'attachment'}), 'attachment')

    def test_system(self):
        self.assertEqual(self.r._classify({'type': 'system'}), 'system')

    def test_metadata_kinds(self):
        for t in ('summary', 'ai-title', 'custom-title', 'last-prompt',
                  'task-summary', 'tag', 'agent-name', 'agent-color',
                  'agent-setting', 'pr-link', 'mode', 'permission-mode',
                  'worktree-state', 'content-replacement',
                  'file-history-snapshot', 'attribution-snapshot',
                  'speculation-accept', 'queue-operation', 'progress',
                  'marble-origami-commit', 'marble-origami-snapshot'):
            self.assertEqual(self.r._classify({'type': t}), 'metadata',
                             f'type {t!r} should classify as metadata')

    def test_unknown(self):
        self.assertEqual(self.r._classify({'type': 'banana'}), 'unknown')
        self.assertEqual(self.r._classify({'__raw__': 'broken json'}),
                         'unknown')
        self.assertEqual(self.r._classify('not a dict'), 'unknown')


class TestRenderers(unittest.TestCase):
    """Each renderer should produce the expected salient text."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_user_text(self):
        out = self.r._render_user({
            'type': 'user',
            'message': {'role': 'user', 'content': 'hello world'},
            'permissionMode': 'plan',
        })
        self.assertIn('▶ user', out)
        self.assertIn('hello world', out)
        self.assertIn('[plan]', out)

    def test_user_meta_chip(self):
        out = self.r._render_user({
            'type': 'user', 'isMeta': True,
            'message': {'role': 'user', 'content': 'sys-injected'},
        })
        self.assertIn('[meta]', out)
        self.assertIn('sys-injected', out)

    def test_user_tool_result_routed(self):
        out = self.r._render_user({
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result',
                'tool_use_id': 'tool-abc12345',
                'content': 'output line',
            }]},
            'toolUseResult': 'output line',
        })
        self.assertIn('↳ tool_result', out)
        self.assertIn('output line', out)
        self.assertIn('id=tool-abc', out)

    def test_user_tool_result_error_flag(self):
        out = self.r._render_user({
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result',
                'tool_use_id': 'x',
                'content': 'oops',
                'is_error': True,
            }]},
            'toolUseResult': 'oops',
        })
        self.assertIn('[error]', out)

    def test_assistant_text_with_usage(self):
        out = self.r._render_assistant({
            'type': 'assistant',
            'message': {
                'role': 'assistant',
                'model': 'claude-sonnet-4-6',
                'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': 'Hi there.'}],
                'usage': {
                    'input_tokens': 1234,
                    'output_tokens': 56,
                    'cache_read_input_tokens': 9000,
                    'cache_creation_input_tokens': 100,
                },
            },
        })
        self.assertIn('⏺ assistant', out)
        self.assertIn('claude-sonnet-4-6', out)
        self.assertIn('[end_turn]', out)
        self.assertIn('Hi there.', out)
        self.assertIn('1,234', out)            # comma-formatted
        self.assertIn('cache read', out)

    def test_assistant_thinking_separator(self):
        out = self.r._render_assistant({
            'type': 'assistant',
            'message': {
                'role': 'assistant',
                'content': [
                    {'type': 'thinking',
                     'thinking': 'pondering...',
                     'signature': 'sig'},
                    {'type': 'text', 'text': 'done'},
                ],
            },
        })
        self.assertIn('thinking', out)
        self.assertIn('pondering...', out)

    def test_assistant_tool_use_bash(self):
        out = self.r._render_assistant({
            'type': 'assistant',
            'message': {
                'role': 'assistant',
                'content': [{
                    'type': 'tool_use', 'id': 'tu_1', 'name': 'Bash',
                    'input': {'command': 'ls -la',
                              'description': 'list files'},
                }],
            },
        })
        self.assertIn('🔧 Bash', out)
        self.assertIn('$ ls -la', out)
        self.assertIn('# list files', out)

    def test_tool_use_edit_formats_diff(self):
        # Drive _fmt_tool_use_part directly via the assistant pathway.
        out = self.r._render_assistant({
            'type': 'assistant',
            'message': {
                'role': 'assistant',
                'content': [{
                    'type': 'tool_use', 'name': 'Edit',
                    'input': {'file_path': '/tmp/x',
                              'old_string': 'foo', 'new_string': 'bar'},
                }],
            },
        })
        self.assertIn('/tmp/x', out)
        self.assertIn('- foo', out)
        self.assertIn('+ bar', out)


class TestToolUseResultDispatch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _user_with_tur(self, tur):
        return {
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'x', 'content': '',
            }]},
            'toolUseResult': tur,
        }

    def test_bash_shape(self):
        out = self.r._render_user(self._user_with_tur({
            'stdout': 'hello\n', 'stderr': '',
            'interrupted': False, 'isImage': False, 'noOutputExpected': False,
        }))
        self.assertIn('hello', out)

    def test_bash_interrupted(self):
        out = self.r._render_user(self._user_with_tur({
            'stdout': '', 'stderr': '',
            'interrupted': True, 'isImage': False, 'noOutputExpected': False,
        }))
        self.assertIn('[interrupted]', out)

    def test_bash_stderr_section(self):
        out = self.r._render_user(self._user_with_tur({
            'stdout': 'ok', 'stderr': 'oops',
            'interrupted': False, 'isImage': False, 'noOutputExpected': False,
        }))
        self.assertIn('stderr', out)
        self.assertIn('oops', out)

    def test_edit_structured_patch(self):
        out = self.r._render_user(self._user_with_tur({
            'filePath': '/tmp/x', 'oldString': 'a', 'newString': 'b',
            'originalFile': 'a', 'replaceAll': False, 'userModified': False,
            'structuredPatch': [{
                'oldStart': 1, 'newStart': 1,
                'lines': [' ctx', '-a', '+b'],
            }],
        }))
        self.assertIn('@@', out)
        self.assertIn('-a', out)
        self.assertIn('+b', out)

    def test_subagent(self):
        out = self.r._render_user(self._user_with_tur({
            'agentId': 'abcd1234efgh',
            'agentType': 'general-purpose',
            'status': 'completed',
            'totalDurationMs': 12340,
            'totalTokens': 1234,
            'totalToolUseCount': 5,
            'content': [{'type': 'text', 'text': 'I did the thing.'}],
            'prompt': '...', 'usage': {},
        }))
        self.assertIn('🤖', out)
        self.assertIn('general-purpose', out)
        self.assertIn('[completed]', out)
        self.assertIn('I did the thing.', out)
        self.assertIn('12.3s', out)

    def test_task_update(self):
        out = self.r._render_user(self._user_with_tur({
            'taskId': 42, 'success': True,
            'statusChange': {'from': 'open', 'to': 'in-progress'},
            'updatedFields': {'assignee': 'me'},
            'verificationNudgeNeeded': False,
        }))
        self.assertIn('#42', out)
        self.assertIn('open', out)
        self.assertIn('in-progress', out)
        self.assertIn('assignee', out)

    def test_grep_content(self):
        out = self.r._render_user(self._user_with_tur({
            'filenames': ['a.py'], 'mode': 'content',
            'numFiles': 1, 'numLines': 3,
            'content': 'a.py:1:hit\na.py:2:hit\n',
        }))
        self.assertIn('1 files', out)
        self.assertIn('3 lines', out)
        self.assertIn('a.py:1:hit', out)

    def test_glob(self):
        out = self.r._render_user(self._user_with_tur({
            'filenames': ['x.py', 'y.py'], 'mode': 'glob', 'numFiles': 2,
        }))
        self.assertIn('2 files', out)
        self.assertIn('x.py', out)

    def test_string_tur(self):
        out = self.r._render_user(self._user_with_tur('plain string output'))
        self.assertIn('plain string output', out)

    def test_unknown_dict_falls_back_to_json(self):
        out = self.r._render_user(self._user_with_tur({
            'someUnseenShape': True, 'foo': 'bar',
        }))
        # Just need *some* hint of the data to come through.
        self.assertIn('foo', out)
        self.assertIn('bar', out)


class TestAttachmentRenderers(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _attach(self, sub, **fields):
        return {'type': 'attachment',
                'attachment': dict(type=sub, **fields)}

    def test_file(self):
        out = self.r._render_attachment(self._attach(
            'file', displayPath='./foo.py', filename='/tmp/foo.py',
            content='print("hi")\n',
        ))
        self.assertIn('📎 attachment', out)
        self.assertIn('./foo.py', out)
        self.assertIn('print("hi")', out)

    def test_diagnostics(self):
        out = self.r._render_attachment(self._attach(
            'diagnostics', isNew=True,
            files=[{'file': 'a.py', 'diagnostics': [
                {'severity': 'error', 'message': 'bad',
                 'range': {'start': {'line': 12}}},
            ]}],
        ))
        self.assertIn('[new]', out)
        self.assertIn('a.py', out)
        self.assertIn('error', out)
        self.assertIn('L12', out)
        self.assertIn('bad', out)

    def test_hook_success(self):
        out = self.r._render_attachment(self._attach(
            'hook_success', hookName='lint', hookEvent='PreToolUse',
            exitCode=0, durationMs=42,
            stdout='ok\n', stderr='',
        ))
        self.assertIn('lint', out)
        self.assertIn('exit=0', out)
        self.assertIn('42ms', out)

    def test_hook_failure_color_branch(self):
        out = self.r._render_attachment(self._attach(
            'hook_success', hookName='lint', hookEvent='PreToolUse',
            exitCode=1, durationMs=10,
            stdout='', stderr='broken',
        ))
        self.assertIn('exit=1', out)
        self.assertIn('broken', out)

    def test_skill_listing(self):
        out = self.r._render_attachment(self._attach(
            'skill_listing', skillCount=12, isInitial=True, content='- a\n- b',
        ))
        self.assertIn('12 skills', out)
        self.assertIn('initial', out)

    def test_delta(self):
        out = self.r._render_attachment(self._attach(
            'mcp_instructions_delta',
            addedNames=['a', 'b'], removedNames=['c'],
            addedBlocks=[],
        ))
        self.assertIn('+ a', out)
        self.assertIn('+ b', out)
        self.assertIn('- c', out)

    def test_date_change(self):
        out = self.r._render_attachment(self._attach(
            'date_change', newDate='2026-05-07',
        ))
        self.assertIn('2026-05-07', out)

    def test_queued_command(self):
        out = self.r._render_attachment(self._attach(
            'queued_command', commandMode='bash', prompt='do the thing',
        ))
        self.assertIn('queued', out)
        self.assertIn('do the thing', out)


class TestSystemRenderers(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_turn_duration(self):
        out = self.r._render_system({
            'type': 'system', 'subtype': 'turn_duration',
            'durationMs': 12345, 'messageCount': 8,
        })
        self.assertIn('⏱', out)
        self.assertIn('12.35s', out)
        self.assertIn('8 msgs', out)

    def test_api_error(self):
        out = self.r._render_system({
            'type': 'system', 'subtype': 'api_error',
            'message': 'rate limited', 'status': 429,
        })
        self.assertIn('api_error', out)
        self.assertIn('rate limited', out)
        self.assertIn('429', out)


class TestMetadataRenderers(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_ai_title(self):
        out = self.r._render_metadata({
            'type': 'ai-title', 'aiTitle': 'Fix the bug',
        })
        self.assertIn('Fix the bug', out)

    def test_last_prompt(self):
        out = self.r._render_metadata({
            'type': 'last-prompt', 'lastPrompt': 'do X',
        })
        self.assertIn('do X', out)

    def test_pr_link(self):
        out = self.r._render_metadata({
            'type': 'pr-link', 'prRepository': 'foo/bar',
            'prNumber': 12, 'prUrl': 'https://github.com/foo/bar/pull/12',
            'timestamp': '2026-05-07T00:00:00Z',
        })
        self.assertIn('foo/bar#12', out)
        self.assertIn('https://github.com/foo/bar/pull/12', out)

    def test_permission_mode(self):
        out = self.r._render_metadata({
            'type': 'permission-mode', 'permissionMode': 'acceptEdits',
        })
        self.assertIn('[acceptEdits]', out)

    def test_worktree_state_active(self):
        out = self.r._render_metadata({
            'type': 'worktree-state',
            'worktreeSession': {
                'worktreePath': '/tmp/wt', 'worktreeBranch': 'feat',
                'originalCwd': '/home/u', 'originalBranch': 'main',
            },
        })
        self.assertIn('/tmp/wt', out)
        self.assertIn('feat', out)
        self.assertIn('main', out)

    def test_worktree_state_exited(self):
        out = self.r._render_metadata({
            'type': 'worktree-state', 'worktreeSession': None,
        })
        self.assertIn('exited', out)

    def test_speculation_accept(self):
        out = self.r._render_metadata({
            'type': 'speculation-accept', 'timeSavedMs': 3400,
        })
        self.assertIn('+3.4s saved', out)

    def test_attribution_snapshot_summarised(self):
        out = self.r._render_metadata({
            'type': 'attribution-snapshot',
            'fileStates': {
                '/a': {'claudeContribution': 100, 'contentHash': 'h', 'mtime': 0},
                '/b': {'claudeContribution': 50,  'contentHash': 'h', 'mtime': 0},
            },
        })
        self.assertIn('2 files', out)
        self.assertIn('150', out)


class TestChrome(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_full(self):
        out = self.r._fmt_chrome({
            'uuid': 'abc',
            'parentUuid': 'def',
            'sessionId': 'sess',
            'cwd': '/home/u',
            'gitBranch': 'main',
            'timestamp': '2026-05-07T00:00:00Z',
            'version': '2.1.39',
            'isSidechain': True,
        })
        for needle in ('uuid', 'abc', 'def', 'sess', '/home/u', 'main',
                       '2026-05-07', '2.1.39', 'sidechain'):
            self.assertIn(needle, out, f'missing {needle!r}')

    def test_empty(self):
        self.assertEqual(self.r._fmt_chrome({}), '')


class TestColorToggle(unittest.TestCase):
    """The same renderer should emit ANSI when on, plain text when off."""

    def test_color_on_emits_csi(self):
        r = _load_recipe(force_color=True)
        out = r._render_user({
            'type': 'user',
            'message': {'role': 'user', 'content': 'hi'},
        })
        self.assertIn('\x1b[', out)

    def test_color_off_strips_csi(self):
        r = _load_recipe(force_color=False)
        out = r._render_user({
            'type': 'user',
            'message': {'role': 'user', 'content': 'hi'},
        })
        self.assertNotIn('\x1b', out)
        self.assertIn('▶ user', out)
        self.assertIn('hi', out)


class TestPreviewMessageDispatcher(unittest.TestCase):
    """``_preview_message`` reads a line, classifies, dispatches, appends chrome."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_dispatch(self):
        import json as _json
        import tempfile

        records = [
            {'type': 'user',
             'message': {'role': 'user', 'content': 'first prompt'},
             'uuid': 'u1', 'cwd': '/x', 'timestamp': 't'},
            {'type': 'assistant',
             'message': {'role': 'assistant', 'model': 'm',
                         'stop_reason': 'end_turn',
                         'content': [{'type': 'text', 'text': 'reply'}]},
             'uuid': 'a1', 'cwd': '/x', 'timestamp': 't'},
            {'type': 'last-prompt', 'lastPrompt': 'bookmark'},
        ]
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl',
                                         delete=False) as f:
            for r in records:
                f.write(_json.dumps(r) + '\n')
            path = f.name
        try:
            self.assertIn('first prompt', self.r._preview_message(path, 0))
            self.assertIn('reply',        self.r._preview_message(path, 1))
            self.assertIn('bookmark',     self.r._preview_message(path, 2))
            # Chrome footer should be present on records that carry uuid.
            self.assertIn('uuid', self.r._preview_message(path, 0))
        finally:
            os.unlink(path)

    def test_invalid_json_line(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl',
                                         delete=False) as f:
            f.write('not json\n')
            path = f.name
        try:
            out = self.r._preview_message(path, 0)
            self.assertIn('[invalid json]', out)
            self.assertIn('not json', out)
        finally:
            os.unlink(path)


class TestSessionPreview(unittest.TestCase):
    """``_preview_session`` folds metadata + renders a recent timeline."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write_session(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False,
                                        prefix='abc1234-')
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def test_card_folds_metadata(self):
        path = self._write_session([
            {'type': 'custom-title', 'customTitle': 'My Title',
             'sessionId': 'abc1234'},
            {'type': 'permission-mode', 'permissionMode': 'acceptEdits',
             'sessionId': 'abc1234'},
            {'type': 'pr-link', 'prRepository': 'foo/bar', 'prNumber': 5,
             'prUrl': 'https://github.com/foo/bar/pull/5',
             'timestamp': '2026-05-07T00:00:00Z', 'sessionId': 'abc1234'},
            {'type': 'tag', 'tag': 'perf', 'sessionId': 'abc1234'},
            {'type': 'last-prompt', 'lastPrompt': 'do the X',
             'sessionId': 'abc1234'},
            {'type': 'user',
             'message': {'role': 'user', 'content': 'hello'},
             'timestamp': '2026-05-07T00:00:01Z'},
            {'type': 'assistant',
             'message': {'role': 'assistant',
                         'content': [{'type': 'text', 'text': 'world'}]},
             'timestamp': '2026-05-07T00:00:02Z'},
        ])
        try:
            out = self.r._preview_session(path)
            self.assertIn('My Title', out)
            self.assertIn('[acceptEdits]', out)
            self.assertIn('foo/bar#5', out)
            self.assertIn('perf', out)
            self.assertIn('do the X', out)
            self.assertIn('1 user', out)
            self.assertIn('1 asst', out)
            self.assertIn('timeline', out)
        finally:
            os.unlink(path)

    def test_empty_session_renders_zero_counts(self):
        path = self._write_session([])
        try:
            out = self.r._preview_session(path)
            # No events to surface, but the card still names the session
            # and shows zeroed counts. Easier on the eye than a bare
            # "empty session" string when the file briefly has no rows.
            self.assertIn('0 msg', out)
            self.assertIn('(no events)', out)
        finally:
            os.unlink(path)

    def test_timeline_lists_recent_events(self):
        # >30 events, only the trailing 30 should render.
        records = [
            {'type': 'user',
             'message': {'role': 'user', 'content': f'prompt {i}'},
             'timestamp': f'2026-05-07T00:00:{i:02d}Z'}
            for i in range(40)
        ]
        path = self._write_session(records)
        try:
            out = self.r._preview_session(path)
            # Last prompt MUST appear.
            self.assertIn('prompt 39', out)
            # First prompt should NOT appear (it was trimmed).
            self.assertNotIn('prompt 0 ', out)
        finally:
            os.unlink(path)

    def test_worktree_active(self):
        path = self._write_session([
            {'type': 'worktree-state',
             'worktreeSession': {'worktreePath': '/tmp/wt',
                                 'worktreeBranch': 'feat'},
             'sessionId': 'abc1234'},
        ])
        try:
            out = self.r._preview_session(path)
            self.assertIn('/tmp/wt', out)
            self.assertIn('feat', out)
        finally:
            os.unlink(path)


class TestSubagentPreview(unittest.TestCase):
    """Subagent preview reuses the session pipeline + sidecar fields."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _make_subagent(self, agent_id, records, sidecar=None):
        """Lay out a parent session + subagent .jsonl + .meta.json."""
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp(prefix='claude-')
        sess_path = os.path.join(tmp, 'parent-session.jsonl')
        with open(sess_path, 'w') as f:
            f.write(_json.dumps({'type': 'user',
                                 'message': {'role': 'user',
                                             'content': 'parent prompt'}}) + '\n')
        sub_dir = os.path.join(tmp, 'parent-session', 'subagents')
        os.makedirs(sub_dir)
        agent_path = os.path.join(sub_dir, f'agent-{agent_id}.jsonl')
        with open(agent_path, 'w') as f:
            for r in records:
                f.write(_json.dumps(r) + '\n')
        if sidecar is not None:
            with open(os.path.join(sub_dir,
                                   f'agent-{agent_id}.meta.json'), 'w') as f:
                _json.dump(sidecar, f)
        return tmp, sess_path, agent_id

    def test_renders_as_session(self):
        tmp, sess_path, agent_id = self._make_subagent(
            'AG01',
            records=[
                {'type': 'user',
                 'message': {'role': 'user', 'content': 'go do the thing'},
                 'timestamp': '2026-05-07T00:00:01Z'},
                {'type': 'assistant',
                 'message': {'role': 'assistant', 'model': 'm',
                             'stop_reason': 'end_turn',
                             'content': [{'type': 'text', 'text': 'done'}]},
                 'timestamp': '2026-05-07T00:00:02Z'},
            ],
            sidecar={'agentType': 'general-purpose',
                     'description': 'Test the thing'},
        )
        out = self.r._preview_subagent(f'{sess_path}#agent:{agent_id}')
        # Card content
        self.assertIn(f'agent {agent_id}', out)
        self.assertIn('general-purpose', out)
        self.assertIn('Test the thing', out)
        # Counts (1 user + 1 assistant)
        self.assertIn('2 msg', out)
        self.assertIn('1 user', out)
        self.assertIn('1 asst', out)
        # Timeline
        self.assertIn('timeline', out)
        self.assertIn('go do the thing', out)
        self.assertIn('done', out)

    def test_no_sidecar(self):
        tmp, sess_path, agent_id = self._make_subagent(
            'NO_META',
            records=[{'type': 'user',
                      'message': {'role': 'user', 'content': 'hi'}}],
        )
        out = self.r._preview_subagent(f'{sess_path}#agent:{agent_id}')
        self.assertIn(f'agent {agent_id}', out)
        # Without sidecar, agent_type/desc rows shouldn't surface.
        self.assertNotIn('type   :', out)
        self.assertNotIn('desc   :', out)
        # But timeline + counts still render.
        self.assertIn('timeline', out)
        self.assertIn('hi', out)


class TestRunningSessions(unittest.TestCase):
    """``_pid_alive`` + the running-session helpers, and --running filtering."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_pid_alive_self(self):
        self.assertTrue(self.r._pid_alive(os.getpid()))

    def test_pid_alive_garbage_pid(self):
        self.assertFalse(self.r._pid_alive(0))
        self.assertFalse(self.r._pid_alive(-1))
        self.assertFalse(self.r._pid_alive('not a pid'))

    def test_pid_alive_dead(self):
        # PID 2^22 - 1 is well above the default Linux max_pid; if it
        # happened to be live this test would flake, but that's a 1-in-4M
        # corner case and the function correctly handles either outcome.
        self.assertFalse(self.r._pid_alive(2**22 - 1))

    def test_scan_populates_index(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sdir = os.path.join(tmp, '.claude', 'sessions')
            os.makedirs(sdir)
            with open(os.path.join(sdir, f'{os.getpid()}.json'), 'w') as f:
                _json.dump({'sessionId': 'sess-live', 'pid': os.getpid(),
                            'cwd': '/x', 'status': 'idle'}, f)
            with open(os.path.join(sdir, '9999999.json'), 'w') as f:
                _json.dump({'sessionId': 'sess-dead', 'pid': 9999999,
                            'cwd': '/x'}, f)
            saved = os.environ.get('HOME')
            try:
                os.environ['HOME'] = tmp
                self.r._scan_running_sessions()
                self.assertIn('sess-live', self.r._RUNNING_INDEX)
                self.assertNotIn('sess-dead', self.r._RUNNING_INDEX)
            finally:
                if saved is None: os.environ.pop('HOME', None)
                else: os.environ['HOME'] = saved
                self.r._RUNNING_INDEX.clear()

    def test_find_session_by_id(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-home-x')
            os.makedirs(proj)
            target = os.path.join(proj, 'sess-foo.jsonl')
            open(target, 'w').close()
            saved = self.r.CLAUDE_ROOT
            try:
                self.r.CLAUDE_ROOT = tmp
                self.assertEqual(self.r._find_session_by_id('sess-foo'), target)
                self.assertIsNone(self.r._find_session_by_id('nope'))
            finally:
                self.r.CLAUDE_ROOT = saved

    def test_find_session_by_pid(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # Lay out the session-meta + a matching jsonl
            sdir = os.path.join(tmp, '.claude', 'sessions')
            os.makedirs(sdir)
            with open(os.path.join(sdir, '1234.json'), 'w') as f:
                _json.dump({'sessionId': 'sid-1', 'pid': 1234}, f)
            projects = os.path.join(tmp, '.claude', 'projects')
            os.makedirs(os.path.join(projects, '-x'))
            target = os.path.join(projects, '-x', 'sid-1.jsonl')
            open(target, 'w').close()
            saved_home = os.environ.get('HOME')
            saved_root = self.r.CLAUDE_ROOT
            try:
                os.environ['HOME'] = tmp
                self.r.CLAUDE_ROOT = projects
                self.assertEqual(self.r._find_session_by_pid(1234), target)
                self.assertIsNone(self.r._find_session_by_pid(99999))
            finally:
                if saved_home is None: os.environ.pop('HOME', None)
                else: os.environ['HOME'] = saved_home
                self.r.CLAUDE_ROOT = saved_root

    def test_running_only_filters_sessions(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'live-proj'))
            live_path = os.path.join(tmp, 'live-proj', 'live-sid.jsonl')
            dead_path = os.path.join(tmp, 'live-proj', 'dead-sid.jsonl')
            with open(live_path, 'w') as f: f.write('{}\n')
            with open(dead_path, 'w') as f: f.write('{}\n')
            self.r._RUNNING_INDEX.clear()
            self.r._RUNNING_INDEX['live-sid'] = {
                'sessionId': 'live-sid', 'pid': os.getpid(),
                'cwd': '/x', 'status': 'idle',
            }
            saved_only = self.r._RUNNING_ONLY
            try:
                self.r._RUNNING_ONLY = True
                rows = self.r._list_sessions(os.path.join(tmp, 'live-proj'))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].title, 'live-sid')
                self.assertIn('pid', rows[0].tag)
                # Without --running, both surface.
                self.r._RUNNING_ONLY = False
                rows = self.r._list_sessions(os.path.join(tmp, 'live-proj'))
                self.assertEqual(len(rows), 2)
            finally:
                self.r._RUNNING_ONLY = saved_only
                self.r._RUNNING_INDEX.clear()


class TestArgPoppers(unittest.TestCase):
    """``_pop_value`` / ``_pop_flag`` parse the recipe's hand-rolled CLI."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _with_argv(self, argv):
        import sys as _sys
        saved = _sys.argv
        _sys.argv = ['browse-claude'] + argv
        return saved

    def _restore_argv(self, saved):
        import sys as _sys
        _sys.argv = saved

    def test_pop_value_space_form(self):
        saved = self._with_argv(['--session', 'abc'])
        try:
            self.assertEqual(self.r._pop_value('--session'), 'abc')
            import sys as _sys
            self.assertEqual(_sys.argv, ['browse-claude'])
        finally:
            self._restore_argv(saved)

    def test_pop_value_equals_form(self):
        saved = self._with_argv(['--session=abc'])
        try:
            self.assertEqual(self.r._pop_value('--session'), 'abc')
        finally:
            self._restore_argv(saved)

    def test_pop_value_int_conv(self):
        saved = self._with_argv(['--pid', '4242'])
        try:
            self.assertEqual(self.r._pop_value('--pid', int), 4242)
        finally:
            self._restore_argv(saved)

    def test_pop_value_bad_int(self):
        saved = self._with_argv(['--pid', 'banana'])
        try:
            self.assertIs(self.r._pop_value('--pid', int), False)
        finally:
            self._restore_argv(saved)

    def test_pop_value_absent(self):
        saved = self._with_argv(['--other', 'x'])
        try:
            self.assertIsNone(self.r._pop_value('--session'))
        finally:
            self._restore_argv(saved)

    def test_pop_flag(self):
        saved = self._with_argv(['--running', 'extra'])
        try:
            self.assertTrue(self.r._pop_flag('--running'))
            self.assertFalse(self.r._pop_flag('--running'))   # already popped
            import sys as _sys
            self.assertEqual(_sys.argv, ['browse-claude', 'extra'])
        finally:
            self._restore_argv(saved)


class TestDecodeProjectPath(unittest.TestCase):
    """``~`` substitution and edge cases for the project-name decoder."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_home_collapsed(self):
        saved = os.environ.get('HOME')
        try:
            os.environ['HOME'] = '/home/ubuntu'
            self.assertEqual(
                self.r._decode_project_path('-home-ubuntu-sandvault-src'),
                '~/sandvault/src',
            )
        finally:
            if saved is None: os.environ.pop('HOME', None)
            else: os.environ['HOME'] = saved

    def test_home_exact(self):
        saved = os.environ.get('HOME')
        try:
            os.environ['HOME'] = '/home/ubuntu'
            self.assertEqual(
                self.r._decode_project_path('-home-ubuntu'), '~',
            )
        finally:
            if saved is None: os.environ.pop('HOME', None)
            else: os.environ['HOME'] = saved

    def test_outside_home_unchanged(self):
        saved = os.environ.get('HOME')
        try:
            os.environ['HOME'] = '/home/ubuntu'
            self.assertEqual(
                self.r._decode_project_path('-tmp-foo'), '/tmp/foo',
            )
        finally:
            if saved is None: os.environ.pop('HOME', None)
            else: os.environ['HOME'] = saved

    def test_non_path_returned_verbatim(self):
        self.assertEqual(self.r._decode_project_path('weird'), 'weird')


class TestMessageOrderReverse(unittest.TestCase):
    """``_list_messages`` returns newest-first with a trailing truncation marker."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def test_newest_first(self):
        path = self._write([
            {'type': 'user', 'message': {'role': 'user', 'content': 'one'}},
            {'type': 'user', 'message': {'role': 'user', 'content': 'two'}},
            {'type': 'user', 'message': {'role': 'user', 'content': 'three'}},
        ])
        try:
            items = self.r._list_messages(path)
            titles = [it.title for it in items]
            self.assertIn('three', titles[0])
            self.assertIn('two',   titles[1])
            self.assertIn('one',   titles[2])
        finally:
            os.unlink(path)

    def test_truncation_marker_at_end(self):
        path = self._write([
            {'type': 'user', 'message': {'role': 'user', 'content': f'msg{i}'}}
            for i in range(10)
        ])
        try:
            items = self.r._list_messages(path, limit=3)
            # Three real items + one truncation marker at the END.
            self.assertEqual(len(items), 4)
            self.assertIn('older entries hidden', items[-1].title)
            # First three should be the *latest* three (msg9, msg8, msg7).
            self.assertIn('msg9', items[0].title)
            self.assertIn('msg8', items[1].title)
            self.assertIn('msg7', items[2].title)
        finally:
            os.unlink(path)

    def test_no_marker_when_under_cap(self):
        path = self._write([
            {'type': 'user', 'message': {'role': 'user', 'content': 'only'}},
        ])
        try:
            items = self.r._list_messages(path, limit=10)
            self.assertEqual(len(items), 1)
            self.assertNotIn('older entries hidden', items[0].title)
        finally:
            os.unlink(path)


class TestTreeChildrenPreview(unittest.TestCase):
    """``_preview_message`` in tree mode appends direct-children bodies."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write_jsonl(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def test_turn_root_preview_includes_children(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user',
                         'content': 'PROBE_USER_PROMPT'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'PROBE_ASST_REPLY'},
             ]}},
        ])
        try:
            saved = self.r._TREE_MODE
            try:
                self.r._TREE_MODE = True
                out = self.r._preview_message(path, 0)   # turn root
                # Own body: the user prompt.
                self.assertIn('PROBE_USER_PROMPT', out)
                # Direct child's body: the assistant reply.
                self.assertIn('PROBE_ASST_REPLY', out)
                # Without tree mode: only the user prompt.
                self.r._TREE_MODE = False
                flat = self.r._preview_message(path, 0)
                self.assertIn('PROBE_USER_PROMPT', flat)
                self.assertNotIn('PROBE_ASST_REPLY', flat)
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)

    def test_children_preview_is_single_level_no_recursion(self):
        # Turn root → assistant tool_use → user tool_result.
        # In tree mode, the turn root preview should show the assistant
        # but NOT the deeper tool_result (that's the grandchild).
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'PROBE_PROMPT'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'PROBE_BASH_CMD'}},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1',
                  'content': 'PROBE_BASH_OUTPUT'},
             ]}},
        ])
        try:
            saved = self.r._TREE_MODE
            try:
                self.r._TREE_MODE = True
                out = self.r._preview_message(path, 0)
                self.assertIn('PROBE_PROMPT', out)
                self.assertIn('PROBE_BASH_CMD', out)  # direct child
                self.assertNotIn('PROBE_BASH_OUTPUT', out)  # grandchild
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)


class TestAncestorIdsForSubagent(unittest.TestCase):
    """``_ancestor_ids_for`` prepends the outer subagent-group row."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_subagent_record_includes_outer_group_id(self):
        # Lay out a parent session + a real subagent jsonl.
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp(prefix='subagent-anc-')
        try:
            sess_path = os.path.join(tmp, 'parent.jsonl')
            with open(sess_path, 'w') as f:
                # Outer session needs to exist on disk so the
                # outer-subagent detection accepts it.
                f.write('{}\n')
            sub_dir = os.path.join(tmp, 'parent', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-AGENT01.jsonl')
            with open(agent_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user',
                                'content': 'hello inside subagent'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'text', 'text': 'reply'},
                    ]},
                }) + '\n')
            # u1 at line 0 is a turn root; a1 at line 1 is a child.
            ancestors = self.r._ancestor_ids_for(f'{agent_path}#1')
            # First ancestor (root) should be the outer subagent group
            # in the parent session.
            self.assertTrue(ancestors,
                            'expected at least one ancestor')
            self.assertEqual(ancestors[0],
                             f'{sess_path}#agent:AGENT01')
            # Direct parent should be the turn root inside the subagent.
            self.assertEqual(ancestors[-1], f'{agent_path}#0')
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_main_session_record_has_no_outer_subagent_group(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess_path = os.path.join(tmp, 'main.jsonl')
            with open(sess_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user', 'content': 'go'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'text', 'text': 'r'},
                    ]},
                }) + '\n')
            ancestors = self.r._ancestor_ids_for(f'{sess_path}#1')
            # Direct parent is the turn root; no outer subagent group.
            self.assertEqual(ancestors, [f'{sess_path}#0'])

    def test_outer_subagent_group_id_helper(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess_path = os.path.join(tmp, 'parent.jsonl')
            with open(sess_path, 'w') as f:
                f.write('{}\n')
            sub_dir = os.path.join(tmp, 'parent', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-ABC.jsonl')
            with open(agent_path, 'w') as f:
                f.write('{}\n')
            self.assertEqual(
                self.r._outer_subagent_group_id(agent_path),
                f'{sess_path}#agent:ABC',
            )
            # Non-subagent paths return None.
            self.assertIsNone(self.r._outer_subagent_group_id(sess_path))
            self.assertIsNone(self.r._outer_subagent_group_id('/nope'))


class TestMdPagerResolution(unittest.TestCase):
    """``_resolve_md_pager`` walks $MD2ANSI / md / md2ansi in order."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _with_env(self, **kw):
        """Snapshot, override, return restore-fn."""
        saved = {k: os.environ.get(k) for k in kw}
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return restore

    def test_env_var_wins(self):
        restore = self._with_env(MD2ANSI='my-md-cmd --flag')
        try:
            self.assertEqual(self.r._resolve_md_pager(),
                             ['my-md-cmd', '--flag'])
        finally:
            restore()

    def test_env_pipeline_uses_shell(self):
        restore = self._with_env(MD2ANSI='md2ansi | less -R')
        try:
            cmd = self.r._resolve_md_pager()
            self.assertEqual(cmd[0], 'bash')
            self.assertEqual(cmd[1], '-c')
            self.assertIn('md2ansi | less -R', cmd[2])
        finally:
            restore()

    def test_falls_through_to_path(self):
        import shutil
        # Build a temp dir with a sentinel ``md`` script, prepend to PATH.
        import tempfile, stat
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = os.path.join(tmp, 'md')
            with open(sentinel, 'w') as f:
                f.write('#!/bin/sh\ncat "$1"\n')
            os.chmod(sentinel, os.stat(sentinel).st_mode | stat.S_IXUSR)
            saved_path = os.environ['PATH']
            restore = self._with_env(MD2ANSI=None)
            try:
                os.environ['PATH'] = tmp
                # Force which() to re-resolve by passing the modified PATH.
                self.assertEqual(self.r._resolve_md_pager(), ['md'])
            finally:
                os.environ['PATH'] = saved_path
                restore()

    def test_none_when_nothing_resolves(self):
        restore = self._with_env(MD2ANSI=None)
        saved_path = os.environ.get('PATH', '')
        try:
            os.environ['PATH'] = '/nonexistent-' + str(os.getpid())
            self.assertIsNone(self.r._resolve_md_pager())
        finally:
            os.environ['PATH'] = saved_path
            restore()


class TestScanTree(unittest.TestCase):
    """``_scan_tree`` builds the parentUuid/promptId tree + caching."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write_jsonl(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False,
                                        prefix='abcd1234-')
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def test_simple_chain(self):
        # One turn: u1 (voice) → a1 (tool_use) → u2 (tool_result) → a2 (text).
        # In the new structure: u1 is the turn root; a1 and a2 are direct
        # members; u2 is re-parented under a1 via tool_use_id pairing.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1',
                  'content': 'ok'},
             ]}},
            {'type': 'assistant', 'uuid': 'a2',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'done'},
             ]}},
        ])
        try:
            td = self.r._scan_tree(path)
            # One turn root (u1) at top level.
            self.assertEqual(len(td.roots_in_order), 1)
            self.assertEqual(td.roots_in_order[0]['kind'], 'turn')
            self.assertEqual(td.roots_in_order[0]['rec']['uuid'], 'u1')
            # u1's direct members: a1 (tool_use), a2 (text). u2 is NOT
            # here — it's paired under a1.
            direct = [r['uuid'] for r in td.turn_direct['u1']]
            self.assertEqual(direct, ['a1', 'a2'])
            # a1 owns u2 as a tool_child.
            self.assertEqual(
                [r['uuid'] for r in td.tool_children['a1']], ['u2'],
            )
        finally:
            os.unlink(path)

    def test_turn_boundary_makes_new_root(self):
        # Two consecutive user voices → two turn roots; the first
        # auto-closes when the second opens (no turn_duration between).
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'first'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'reply'},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': 'second'}},
        ])
        try:
            td = self.r._scan_tree(path)
            self.assertEqual(len(td.roots_in_order), 2)
            self.assertEqual(td.roots_in_order[0]['rec']['uuid'], 'u1')
            self.assertEqual(td.roots_in_order[1]['rec']['uuid'], 'u2')
            # u1 has a1 as a direct member. u2 has no members yet.
            self.assertEqual(
                [r['uuid'] for r in td.turn_direct['u1']], ['a1'],
            )
            self.assertEqual(td.turn_direct['u2'], [])
        finally:
            os.unlink(path)

    def test_lonely_user_record_is_still_a_turn_root(self):
        # Defensive: a user voice with no parent and no sibling records.
        # New algorithm doesn't care about parentUuid — text content
        # alone makes it a turn root.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'parentUuid': 'does-not-exist',
             'message': {'role': 'user', 'content': 'broken'}},
        ])
        try:
            td = self.r._scan_tree(path)
            self.assertEqual(len(td.roots_in_order), 1)
            self.assertEqual(td.roots_in_order[0]['kind'], 'turn')
            self.assertEqual(td.roots_in_order[0]['rec']['uuid'], 'u1')
        finally:
            os.unlink(path)

    def test_metadata_outside_turn_wraps_in_span(self):
        # permission-mode (before any turn) → preamble span.
        # u1 turn root.
        # last-prompt (after u1, no turn_duration yet) → turn member.
        # turn_duration closes the turn.
        # ai-title after that → tail span.
        path = self._write_jsonl([
            {'type': 'permission-mode', 'permissionMode': 'plan'},
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'last-prompt', 'lastPrompt': 'go'},
            {'type': 'system', 'subtype': 'turn_duration',
             'durationMs': 1, 'messageCount': 1},
            {'type': 'ai-title', 'aiTitle': 'something'},
        ])
        try:
            td = self.r._scan_tree(path)
            # 3 top-level entries: preamble span, u1 turn, tail span.
            self.assertEqual(len(td.roots_in_order), 3)
            self.assertEqual(td.roots_in_order[0]['kind'], 'span')
            self.assertEqual(td.roots_in_order[0]['start'], 0)
            self.assertEqual(
                [r['type'] for r in td.roots_in_order[0]['records']],
                ['permission-mode'],
            )
            self.assertEqual(td.roots_in_order[1]['kind'], 'turn')
            self.assertEqual(td.roots_in_order[1]['rec']['uuid'], 'u1')
            self.assertEqual(td.roots_in_order[2]['kind'], 'span')
            self.assertEqual(td.roots_in_order[2]['start'], 4)
            # u1's direct members: last-prompt + turn_duration.
            self.assertEqual(
                [r['type'] for r in td.turn_direct['u1']],
                ['last-prompt', 'system'],
            )
        finally:
            os.unlink(path)

    def test_queue_operation_inside_turn_is_voice_member(self):
        # Queued prompts sent while a turn is open should land inside
        # the turn (not a span) AND get the user-voice row_bg.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'sleep 10'}},
             ]}},
            {'type': 'queue-operation', 'operation': 'enqueue',
             'content': 'also do this next', 'timestamp': '2026-05-14'},
            {'type': 'system', 'subtype': 'turn_duration',
             'durationMs': 1, 'messageCount': 1},
        ])
        try:
            td = self.r._scan_tree(path)
            # One turn root (u1); queue-op + turn_duration are turn
            # members. No span at the top level.
            self.assertEqual(len(td.roots_in_order), 1)
            self.assertEqual(td.roots_in_order[0]['kind'], 'turn')
            self.assertEqual(td.roots_in_order[0]['rec']['uuid'], 'u1')
            direct_types = [r['type'] for r in td.turn_direct['u1']]
            self.assertIn('queue-operation', direct_types)
            # The Item built for it picks up row_bg.
            queue_rec = next(r for r in td.records or []
                             if isinstance(r, dict)
                             and r.get('type') == 'queue-operation')
            item = self.r._tree_item(path, queue_rec, td)
            self.assertEqual(getattr(item, 'row_bg', None), 235)
        finally:
            os.unlink(path)

    def test_hook_attachment_pairs_with_tool_use(self):
        # Attachment with toolUseID matching a tool_use part nests
        # under that tool_use's assistant row.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
            {'type': 'attachment',
             'attachment': {'type': 'hook_success', 'hookName': 'lint',
                            'hookEvent': 'PreToolUse', 'exitCode': 0,
                            'durationMs': 5, 'toolUseID': 't1',
                            'stdout': '', 'stderr': ''}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1',
                  'content': 'ok'},
             ]}},
        ])
        try:
            td = self.r._scan_tree(path)
            # u1 turn root, a1 direct member. u2 + hook attachment under a1.
            self.assertEqual(td.turn_direct['u1'][0]['uuid'], 'a1')
            tool_kids = td.tool_children['a1']
            kinds = [r['type'] for r in tool_kids]
            self.assertIn('user', kinds)        # tool_result
            self.assertIn('attachment', kinds)  # hook
        finally:
            os.unlink(path)

    def test_forked_from_flagged(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'promptId': 'P1', 'forkedFrom': 'old-uuid-aaaa',
             'message': {'role': 'user', 'content': 'resumed'}},
        ])
        try:
            td = self.r._scan_tree(path)
            self.assertEqual(td.by_uuid['u1'].get('_tree_forked_from'),
                             'old-uuid-aaaa')
        finally:
            os.unlink(path)

    def test_cache_invalidates_on_mtime_change(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'promptId': 'P1',
             'message': {'role': 'user', 'content': 'one'}},
        ])
        try:
            import json as _json
            td1 = self.r._scan_tree(path)
            self.assertEqual(len([r for r in td1.records if r]), 1)
            # Append a new record + bump mtime.
            with open(path, 'a') as f:
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'text', 'text': 'x'},
                    ]},
                }) + '\n')
            import time
            # File mtime is at second granularity on many fs's — force
            # a different mtime via os.utime.
            now = os.stat(path).st_mtime
            os.utime(path, (now + 1, now + 1))
            td2 = self.r._scan_tree(path)
            self.assertEqual(len([r for r in td2.records if r]), 2)
            self.assertIsNot(td1, td2)   # different cached instance
        finally:
            os.unlink(path)

    def test_agent_link_resolves_subagent(self):
        # Parent assistant with Task tool_use; child user tool_result
        # whose toolUseResult.agentId resolves to a subagent jsonl.
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp(prefix='sa-')
        try:
            sess_path = os.path.join(tmp, 'parent.jsonl')
            with open(sess_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1', 'parentUuid': None,
                    'promptId': 'P1',
                    'message': {'role': 'user', 'content': 'go'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'id': 'toolu_x',
                         'name': 'Task',
                         'input': {'prompt': 'do thing',
                                   'subagent_type': 'Explore'}},
                    ]},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1',
                    'promptId': 'P1',
                    'message': {'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'toolu_x',
                         'content': 'output'},
                    ]},
                    'toolUseResult': {
                        'agentId': 'AGENT01', 'agentType': 'Explore',
                        'status': 'completed',
                    },
                }) + '\n')
            # Lay out the matching subagent file.
            sub_dir = os.path.join(tmp, 'parent', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-AGENT01.jsonl')
            with open(agent_path, 'w') as f:
                f.write('{}\n')

            td = self.r._scan_tree(sess_path)
            self.assertIn('toolu_x', td.agent_link)
            self.assertEqual(td.agent_link['toolu_x']['agent_id'], 'AGENT01')
            self.assertEqual(td.agent_link['toolu_x']['agent_path'],
                             agent_path)
            self.assertEqual(td.agent_link['toolu_x']['assistant_uuid'],
                             'a1')
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_last_voice_id_picks_latest(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'promptId': 'P1',
             'message': {'role': 'user', 'content': 'first voice'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
            {'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1',
             'promptId': 'P1',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't',
                  'content': 'ok'},
             ]}},
            {'type': 'assistant', 'uuid': 'a2', 'parentUuid': 'u2',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'latest voice'},
             ]}},
        ])
        try:
            self.assertEqual(self.r._last_voice_id(path), f'{path}#3')
        finally:
            os.unlink(path)


class TestTreeListings(unittest.TestCase):
    """Tree-mode get_children / _list_tree_roots / _list_tree_children."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write_jsonl(self, records, prefix='sess-'):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False,
                                        prefix=prefix)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def test_session_roots_wraps_metadata_in_span(self):
        # permission-mode (preamble) + u1 (turn) + u2 (next turn) →
        # three roots: span, turn, turn.
        path = self._write_jsonl([
            {'type': 'permission-mode', 'permissionMode': 'plan'},
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'first'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'reply'},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': 'second'}},
        ])
        try:
            roots = self.r._list_tree_roots(path)
            self.assertEqual(len(roots), 3)
            # Preamble span umbrella.
            self.assertEqual(roots[0].kind, 'span')
            self.assertEqual(roots[0].id, f'{path}#span:0')
            # u1 turn root.
            self.assertEqual(roots[1].kind, 'message')
            self.assertEqual(roots[1].line_no, 1)
            # u2 turn root.
            self.assertEqual(roots[2].kind, 'message')
            self.assertEqual(roots[2].line_no, 3)
        finally:
            os.unlink(path)

    def test_drill_into_turn_root_pairs_tool_result(self):
        # u1 → a1 (tool_use) → u2 (tool_result) → a2 (text).
        # u1's direct children: a1, a2. u2 is paired under a1.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1',
                  'content': 'ok'},
             ]}},
            {'type': 'assistant', 'uuid': 'a2',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'done'},
             ]}},
        ])
        try:
            # u1's children: [a1, a2] (a1 is line 1, a2 is line 3).
            kids = self.r._list_tree_children(path, 0)
            self.assertEqual([it.line_no for it in kids], [1, 3])
            # a1's tool-execution children: [u2].
            tool_kids = self.r._list_tree_children(path, 1)
            self.assertEqual([it.line_no for it in tool_kids], [2])
            # u2 and a2 are leaves.
            self.assertEqual(self.r._list_tree_children(path, 2), [])
            self.assertEqual(self.r._list_tree_children(path, 3), [])
        finally:
            os.unlink(path)

    def test_span_umbrella_children_are_records(self):
        path = self._write_jsonl([
            {'type': 'permission-mode', 'permissionMode': 'plan'},
            {'type': 'ai-title', 'aiTitle': 'something'},
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
        ])
        try:
            roots = self.r._list_tree_roots(path)
            self.assertEqual(roots[0].kind, 'span')
            self.assertEqual(roots[0].span_count, 2)
            # Drill into the span: two metadata records.
            span_kids = self.r._list_span_records(path, 0)
            self.assertEqual([it.line_no for it in span_kids], [0, 1])
        finally:
            os.unlink(path)

    def test_two_consecutive_user_voices_yield_two_turn_roots(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'first'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'r'},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': 'second'}},
        ])
        try:
            roots = self.r._list_tree_roots(path)
            self.assertEqual([it.line_no for it in roots], [0, 2])
            # u1's direct child is a1 (no other voices in this turn).
            kids = self.r._list_tree_children(path, 0)
            self.assertEqual([it.line_no for it in kids], [1])
        finally:
            os.unlink(path)

    def test_subagent_attaches_to_assistant_row(self):
        # Assistant with Task tool_use; tool_result resolves to a real
        # subagent file. Children of the assistant row include both
        # the tool_result AND the subagent pseudo-row.
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp(prefix='sa-tree-')
        try:
            sess_path = os.path.join(tmp, 'parent.jsonl')
            with open(sess_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1', 'parentUuid': None,
                    'promptId': 'P1',
                    'message': {'role': 'user', 'content': 'go'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'id': 'toolu_x',
                         'name': 'Task',
                         'input': {'prompt': 'do thing',
                                   'subagent_type': 'Explore'}},
                    ]},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1',
                    'promptId': 'P1',
                    'message': {'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'toolu_x',
                         'content': 'output'},
                    ]},
                    'toolUseResult': {'agentId': 'AGENT01',
                                      'agentType': 'Explore',
                                      'status': 'completed'},
                }) + '\n')
            sub_dir = os.path.join(tmp, 'parent', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-AGENT01.jsonl')
            with open(agent_path, 'w') as f:
                f.write('{}\n')
            with open(os.path.join(sub_dir,
                                   'agent-AGENT01.meta.json'), 'w') as f:
                _json.dump({'agentType': 'Explore',
                            'description': 'probe stuff'}, f)

            kids = self.r._list_tree_children(sess_path, 1)  # assistant @ line 1
            # Two children: the tool_result (paired via tool_use_id) and
            # the subagent pseudo-row (via agent_link).
            self.assertEqual(len(kids), 2)
            kinds = [k.kind for k in kids]
            self.assertIn('message', kinds)    # tool_result
            self.assertIn('subagent', kinds)
            sub = next(k for k in kids if k.kind == 'subagent')
            self.assertEqual(sub.agent_id, 'AGENT01')
            self.assertEqual(sub.id, f'{sess_path}#agent:AGENT01')
            # And the assistant row itself reports has_children=True.
            td = self.r._scan_tree(sess_path)
            asst_item = self.r._tree_item(sess_path,
                                          td.records[1], td)
            self.assertTrue(asst_item.has_children)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_get_children_dispatch_tree_mode(self):
        # The get_children dispatcher should route by id shape AND mode.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'promptId': 'P1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'done'},
             ]}},
        ])
        try:
            saved = self.r._TREE_MODE
            try:
                # Tree mode: session jsonl → root-level rows (one turn root).
                self.r._TREE_MODE = True
                roots = self.r.get_children(path)
                self.assertEqual(len(roots), 1)
                self.assertEqual(roots[0].kind, 'message')
                self.assertEqual(roots[0].line_no, 0)   # u1
                # Turn root's children → [a1].
                kids = self.r.get_children(f'{path}#0')
                self.assertEqual([it.line_no for it in kids], [1])
                # a1 is a leaf (no tool_use parts in this fixture).
                self.assertEqual(self.r.get_children(f'{path}#1'), [])

                # Flat mode: session jsonl → messages newest-first list.
                self.r._TREE_MODE = False
                flat = self.r.get_children(path)
                titles = [it.title for it in flat if it.kind == 'message']
                self.assertEqual(len(titles), 2)
                # Message id has no children in flat mode.
                self.assertEqual(self.r.get_children(f'{path}#0'), [])
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)


class TestSubagentRowTag(unittest.TestCase):
    """Subagent rows surface type · msg count · time-ago."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_tag_includes_relative_time(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-x')
            os.makedirs(proj)
            sess_path = os.path.join(proj, 'parent-sid.jsonl')
            open(sess_path, 'w').close()
            sub_dir = os.path.join(proj, 'parent-sid', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-A1.jsonl')
            with open(agent_path, 'w') as f:
                f.write('{}\n')
            with open(os.path.join(sub_dir, 'agent-A1.meta.json'), 'w') as f:
                _json.dump({'agentType': 'general-purpose',
                            'description': 'do the thing'}, f)
            # Pin mtime so the relative-time formatter is deterministic.
            ts = datetime.datetime.now().timestamp() - 3 * 3600  # 3h ago
            os.utime(agent_path, (ts, ts))

            rows = self.r._list_subagents_for_session(sess_path)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            # Tag should still carry type and msg count, plus the time.
            self.assertIn('general-purpose', row.tag)
            self.assertIn('1 msg', row.tag)
            self.assertIn('3h ago', row.tag)


class TestRowBgForKind(unittest.TestCase):
    """User/assistant message rows get a row-bg highlight."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def test_user_row_has_bg(self):
        path = self._write([
            {'type': 'user', 'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            items = self.r._list_messages(path)
            self.assertEqual(getattr(items[0], 'row_bg', None), 235)
        finally:
            os.unlink(path)

    def test_assistant_row_has_bg(self):
        path = self._write([
            {'type': 'assistant',
             'message': {'role': 'assistant',
                         'content': [{'type': 'text', 'text': 'hi'}]}},
        ])
        try:
            items = self.r._list_messages(path)
            self.assertEqual(getattr(items[0], 'row_bg', None), 17)
        finally:
            os.unlink(path)

    def test_other_kinds_have_no_bg(self):
        path = self._write([
            {'type': 'attachment',
             'attachment': {'type': 'file', 'displayPath': '/x',
                            'filename': '/x', 'content': ''}},
            {'type': 'system', 'subtype': 'turn_duration',
             'durationMs': 1, 'messageCount': 1},
            {'type': 'permission-mode', 'permissionMode': 'plan'},
        ])
        try:
            items = self.r._list_messages(path)
            for it in items:
                self.assertIsNone(getattr(it, 'row_bg', None),
                                  f'{it.title!r} should have no row_bg')
        finally:
            os.unlink(path)

    def test_assistant_tool_use_only_has_no_bg(self):
        # Tool calls are machinery, not voice — even though kind=='assistant'.
        path = self._write([
            {'type': 'assistant',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
        ])
        try:
            items = self.r._list_messages(path)
            self.assertIsNone(getattr(items[0], 'row_bg', None))
        finally:
            os.unlink(path)

    def test_user_tool_result_only_has_no_bg(self):
        # Tool results are machinery too — kind=='user' but no speech.
        path = self._write([
            {'type': 'user',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 'x',
                  'content': 'output'},
             ]},
             'toolUseResult': 'output'},
        ])
        try:
            items = self.r._list_messages(path)
            self.assertIsNone(getattr(items[0], 'row_bg', None))
        finally:
            os.unlink(path)

    def test_assistant_with_text_and_tool_use_gets_bg(self):
        # Mixed content (text + tool_use) still counts as voice.
        path = self._write([
            {'type': 'assistant',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': "I'll run this."},
                 {'type': 'tool_use', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
        ])
        try:
            items = self.r._list_messages(path)
            self.assertEqual(getattr(items[0], 'row_bg', None), 17)
        finally:
            os.unlink(path)

    def test_assistant_thinking_only_has_no_bg(self):
        # A thinking-only assistant step (no text yielded) isn't speech.
        path = self._write([
            {'type': 'assistant',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'thinking', 'thinking': 'pondering...',
                  'signature': 'sig'},
             ]}},
        ])
        try:
            items = self.r._list_messages(path)
            self.assertIsNone(getattr(items[0], 'row_bg', None))
        finally:
            os.unlink(path)


class TestProjectOrdering(unittest.TestCase):
    """Projects sort by latest .jsonl mtime, not directory mtime."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_active_project_bubbles_up(self):
        # Two projects: ``stale`` was created later but its .jsonl is
        # older; ``active`` was created first but its .jsonl is fresh.
        # Sorting by dir mtime would put ``stale`` first — sorting by
        # latest .jsonl mtime must put ``active`` first.
        import tempfile, time
        with tempfile.TemporaryDirectory() as tmp:
            saved = self.r.CLAUDE_ROOT
            try:
                self.r.CLAUDE_ROOT = tmp
                # active: dir created first, .jsonl recent.
                active_dir = os.path.join(tmp, '-home-active')
                os.makedirs(active_dir)
                active_jsonl = os.path.join(active_dir, 'a.jsonl')
                with open(active_jsonl, 'w') as f:
                    f.write('{}\n')
                os.utime(active_jsonl, (1000000.0, 1000000.0))
                # stale: dir created later, .jsonl is older.
                stale_dir = os.path.join(tmp, '-home-stale')
                os.makedirs(stale_dir)
                stale_jsonl = os.path.join(stale_dir, 'b.jsonl')
                with open(stale_jsonl, 'w') as f:
                    f.write('{}\n')
                os.utime(stale_jsonl, (500000.0, 500000.0))
                # Force the dir mtimes to invert: stale dir is "newer".
                os.utime(active_dir, (500000.0, 500000.0))
                os.utime(stale_dir, (2000000.0, 2000000.0))
                # Now nudge the active .jsonl to be the freshest signal.
                os.utime(active_jsonl, (3000000.0, 3000000.0))

                projects = self.r._list_projects()
                titles = [p.title for p in projects]
                self.assertEqual(titles[0], '/home/active',
                                 f'active project should sort first, got {titles}')
            finally:
                self.r.CLAUDE_ROOT = saved


class TestMultilinePreservation(unittest.TestCase):
    """Session preview should preserve newlines, not collapse them."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_summarise_keeps_newlines(self):
        out = self.r._summarise_message({
            'type': 'user',
            'message': {'role': 'user',
                        'content': 'line one\nline two\nline three'},
        })
        # _summarise_message no longer collapses — newlines stay.
        self.assertIn('\n', out)
        for needle in ('line one', 'line two', 'line three'):
            self.assertIn(needle, out)

    def test_oneline_collapses_for_row_titles(self):
        s = 'foo\nbar\n  baz '
        self.assertEqual(self.r._oneline(s), 'foo bar baz')

    def test_indent_continuations_aligns(self):
        out = self.r._indent_continuations('a\nb\nc', '   ')
        self.assertEqual(out, 'a\n   b\n   c')

    def test_indent_continuations_one_line_unchanged(self):
        self.assertEqual(self.r._indent_continuations('hi', '   '), 'hi')

    def test_card_renders_multiline_value(self):
        # task-summary with embedded newlines should appear multi-line
        # in the card, indented under the value column.
        path = self._write_session([
            {'type': 'task-summary',
             'summary': 'planning:\n  - step 1\n  - step 2',
             'sessionId': 'abc'},
        ])
        try:
            out = self.r._preview_session(path)
            self.assertIn('planning:', out)
            self.assertIn('step 1', out)
            self.assertIn('step 2', out)
            # Continuation indent: the `now:` row's label is followed by
            # ': ' and the indent matches that width — verify by checking
            # the full alignment exists.
            lines = out.split('\n')
            now_idx = next((i for i, l in enumerate(lines)
                            if 'now' in l and ':' in l), None)
            self.assertIsNotNone(now_idx)
            # Subsequent lines from the multi-line value should start
            # with whitespace (the indent).
            self.assertTrue(lines[now_idx + 1].startswith(' '))
        finally:
            os.unlink(path)

    def test_timeline_multiline_title_indents(self):
        path = self._write_session([
            {'type': 'user',
             'message': {'role': 'user',
                         'content': 'first line\nsecond line\nthird line'},
             'timestamp': '2026-05-08T00:00:01Z'},
        ])
        try:
            out = self.r._preview_session(path)
            self.assertIn('first line', out)
            self.assertIn('second line', out)
            self.assertIn('third line', out)
            # Continuation lines indented to align under the title column.
            lines = out.split('\n')
            first_idx = next((i for i, l in enumerate(lines)
                              if 'first line' in l), None)
            self.assertIsNotNone(first_idx)
            cont = lines[first_idx + 1]
            # Continuations indented by _TIMELINE_PREFIX_WIDTH (21) spaces.
            self.assertTrue(cont.startswith(' ' * 21),
                            f'continuation should be indented: {cont!r}')
            self.assertIn('second line', cont)
        finally:
            os.unlink(path)

    def test_row_list_title_is_single_line(self):
        # The list-row title must be single-line, even when the
        # underlying content is multi-line.
        # _list_messages applies _oneline; just verify _oneline-of-summary
        # has no newlines for a multi-line user prompt.
        obj = {'type': 'user',
               'message': {'role': 'user', 'content': 'hi\nbye'}}
        title = self.r._oneline(self.r._summarise_message(obj))
        self.assertNotIn('\n', title)
        self.assertIn('hi', title)
        self.assertIn('bye', title)

    def _write_session(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False,
                                        prefix='abc1234-')
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name


class TestSummariseTitles(unittest.TestCase):
    """Per-type one-line titles that drive the message list rows."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_assistant_tool_use_only(self):
        out = self.r._summarise_message({
            'type': 'assistant',
            'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'name': 'Bash',
                 'input': {'command': 'ls /tmp'}},
            ]},
        })
        self.assertEqual(out, '🔧 Bash(ls /tmp)')

    def test_assistant_text_plus_tool_use(self):
        # If there's text content, fall back to text-prefix shape.
        out = self.r._summarise_message({
            'type': 'assistant',
            'message': {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'thinking aloud'},
                {'type': 'tool_use', 'name': 'Bash',
                 'input': {'command': 'ls'}},
            ]},
        })
        self.assertIn('thinking aloud', out)

    def test_user_tool_result(self):
        out = self.r._summarise_message({
            'type': 'user',
            'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'x',
                 'content': 'hello\nworld'},
            ]},
            'toolUseResult': {
                'stdout': 'hello\n', 'stderr': '',
                'interrupted': False, 'isImage': False, 'noOutputExpected': False,
            },
        })
        self.assertTrue(out.startswith('↳ tool_result:'))
        self.assertIn('hello', out)

    def test_attachment_file(self):
        out = self.r._summarise_message({
            'type': 'attachment',
            'attachment': {'type': 'file', 'displayPath': './foo.py',
                           'filename': '/tmp/foo.py', 'content': 'hi'},
        })
        self.assertEqual(out, '📎 file: ./foo.py')

    def test_attachment_hook_success(self):
        out = self.r._summarise_message({
            'type': 'attachment',
            'attachment': {'type': 'hook_success', 'hookName': 'lint',
                           'hookEvent': 'PreToolUse', 'exitCode': 0,
                           'durationMs': 5},
        })
        self.assertEqual(out, '✓ hook lint (PreToolUse)')

    def test_attachment_hook_failure(self):
        out = self.r._summarise_message({
            'type': 'attachment',
            'attachment': {'type': 'hook_success', 'hookName': 'lint',
                           'hookEvent': 'PreToolUse', 'exitCode': 1,
                           'durationMs': 5},
        })
        self.assertTrue(out.startswith('✗ hook'))

    def test_system_turn_duration(self):
        out = self.r._summarise_message({
            'type': 'system', 'subtype': 'turn_duration',
            'durationMs': 1234, 'messageCount': 3,
        })
        self.assertIn('1.23s', out)
        self.assertIn('3 msgs', out)

    def test_system_api_error(self):
        out = self.r._summarise_message({
            'type': 'system', 'subtype': 'api_error',
        })
        self.assertIn('api_error', out)

    def test_metadata_titles(self):
        cases = [
            ({'type': 'ai-title', 'aiTitle': 'X'},                   'ai-title: X'),
            ({'type': 'custom-title', 'customTitle': 'Y'},           'custom-title: Y'),
            ({'type': 'last-prompt', 'lastPrompt': 'Z'},             'last-prompt: Z'),
            ({'type': 'tag', 'tag': 'perf'},                         'tag: perf'),
            ({'type': 'pr-link',
              'prRepository': 'foo/bar', 'prNumber': 42},            'PR foo/bar#42'),
            ({'type': 'mode', 'mode': 'coordinator'},                'mode: coordinator'),
            ({'type': 'speculation-accept', 'timeSavedMs': 2500},
              'speculation-accept: +2.5s saved'),
        ]
        for obj, want in cases:
            self.assertEqual(self.r._summarise_message(obj), want,
                             f'failed for {obj["type"]}')

    def test_user_meta_marker(self):
        out = self.r._summarise_message({
            'type': 'user', 'isMeta': True,
            'message': {'role': 'user', 'content': 'sys'},
        })
        self.assertIn('user/meta', out)

    def test_tag_style_includes_new_kinds(self):
        for k in ('attachment', 'pr-link', 'mode', 'speculation-accept',
                  'queue-operation', 'ai-title', 'custom-title',
                  'file-history-snapshot', 'attribution-snapshot',
                  'content-replacement', 'progress'):
            self.assertIn(k, self.r._TAG_STYLE_FOR_KIND,
                          f'missing tag style for {k!r}')


if __name__ == '__main__':
    unittest.main()
