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
    mod.BrowserConfig = _Stub
    mod.Item = _Stub
    # ``upsert`` is a push-API op constructor used by the live-tail
    # worker. Tests don't exercise the framework's actual op-apply
    # behavior, so stub it to a plain tuple. The ``where=`` keyword
    # (added in the positioning-descriptor work) becomes a 5-tuple.
    mod.upsert = lambda id_, parent_id, *, where=None, **fields: (
        ('upsert', id_, parent_id, fields)
        if where is None
        else ('upsert', id_, parent_id, fields, where)
    )

    # ``mod`` op: patches an existing row's fields, never inserts. The
    # ``parent_id`` defaults to ``KEEP_PARENT`` (don't reparent).
    class _KeepParent:
        __slots__ = ()
        def __repr__(self):
            return 'KEEP_PARENT'
    KEEP_PARENT = _KeepParent()
    mod.KEEP_PARENT = KEEP_PARENT

    def _mod(id_, parent_id=KEEP_PARENT, *, where=None, **fields):
        if where is None:
            return ('mod', id_, parent_id, fields)
        return ('mod', id_, parent_id, fields, where)
    mod.mod = _mod

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
        # Usage now rides in ``_fmt_chrome``, not the body. The body
        # carries head / content; chrome appends the rule + metadata
        # rows + the one-line ``── usage:`` footer.
        obj = {
            'type': 'assistant',
            'uuid': 'abc-123',
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
        }
        body = self.r._render_assistant(obj)
        chrome = self.r._fmt_chrome(obj)
        self.assertIn('⏺ assistant', body)
        self.assertIn('claude-sonnet-4-6', body)
        self.assertIn('[end_turn]', body)
        self.assertIn('Hi there.', body)
        # Body no longer carries usage — it's chrome's concern.
        self.assertNotIn('cache read', body)
        self.assertNotIn('usage:', body)
        # Chrome carries a single ``usage`` line — a plain
        # three-space separated string, aligned with the other rows.
        import re as _re
        sgr = _re.compile(r'\x1b\[[0-9;]*m')
        plain_chrome = sgr.sub('', chrome)
        usage_lines = [
            ln for ln in plain_chrome.split('\n')
            if 'usage' in ln and ':' in ln
        ]
        self.assertEqual(len(usage_lines), 1)
        self.assertIn('input: 1,234', usage_lines[0])
        self.assertIn('output: 56', usage_lines[0])
        self.assertIn('cache read: 9,000', usage_lines[0])
        self.assertIn('cache new: 100', usage_lines[0])
        # Alignment: each label row's colon sits at the same column.
        # Only check the *first* colon — the inline usage sub-labels
        # also carry colons, but those land later in the line.
        label_lines = [
            ln for ln in plain_chrome.split('\n')
            if ':' in ln and not ln.startswith('─')
        ]
        colon_cols = {ln.index(':') for ln in label_lines}
        self.assertEqual(len(colon_cols), 1,
                         f'rows not aligned: {label_lines}')

    def test_chrome_usage_only(self):
        # No uuid/timestamp/cwd, only usage: chrome still emits the
        # leading rule and the ``usage:`` line.
        obj = {
            'type': 'assistant',
            'message': {
                'role': 'assistant',
                'content': [{'type': 'text', 'text': 'hi'}],
                'usage': {
                    'input_tokens': 5, 'output_tokens': 1,
                    'cache_read_input_tokens': 0,
                    'cache_creation_input_tokens': 0,
                },
            },
        }
        chrome = self.r._fmt_chrome(obj)
        self.assertTrue(chrome, 'chrome must surface usage even with no other rows')
        import re as _re
        sgr = _re.compile(r'\x1b\[[0-9;]*m')
        lines = sgr.sub('', chrome).split('\n')
        self.assertIn('usage:', lines[-1])
        self.assertIn('input: 5', lines[-1])

    def test_chrome_no_usage_no_rows_returns_empty(self):
        # No chrome-relevant fields and no usage → empty chrome (no
        # bare rule emitted).
        self.assertEqual(self.r._fmt_chrome({'type': 'assistant'}), '')

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

    def test_file_path_only(self):
        # File attachments render path-only — the body lives on disk
        # and can be megabytes; we surface the path and let the user
        # open it in $EDITOR if they want the content.
        out = self.r._render_attachment(self._attach(
            'file', displayPath='./foo.py', filename='/tmp/foo.py',
            content='print("hi")\n',
        ))
        self.assertIn('📎 attachment', out)
        self.assertIn('./foo.py', out)
        self.assertIn('/tmp/foo.py', out)
        self.assertNotIn('print("hi")', out)

    def test_compact_file_reference(self):
        out = self.r._render_attachment(self._attach(
            'compact_file_reference',
            displayPath='src-tui/040-state.py',
            filename='/abs/src-tui/040-state.py',
        ))
        self.assertIn('compact', out.lower())  # head says "attachment compact_file_reference"
        self.assertIn('src-tui/040-state.py', out)
        self.assertIn('/abs/src-tui/040-state.py', out)

    def test_edited_text_file(self):
        out = self.r._render_attachment(self._attach(
            'edited_text_file',
            filename='/tmp/foo.log',
            snippet='1\tline-one\n2\tline-two\n',
        ))
        self.assertIn('edited', out)
        self.assertIn('/tmp/foo.log', out)
        self.assertIn('line-one', out)

    def test_plan_mode_exit_kept(self):
        out = self.r._render_attachment(self._attach(
            'plan_mode_exit',
            planExists=True,
            planFilePath='/p/plan.md',
        ))
        self.assertIn('mode', out)
        self.assertIn('exit plan', out)
        self.assertIn('kept', out)
        self.assertIn('/p/plan.md', out)

    def test_plan_mode_exit_discarded(self):
        out = self.r._render_attachment(self._attach(
            'plan_mode_exit',
            planExists=False,
            planFilePath='/p/plan.md',
        ))
        self.assertIn('discarded', out)

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

    def test_invalid_json_line_returns_empty_preview(self):
        # Invalid lines yield ``None`` from ``_read_jsonl_line`` and
        # an empty preview. The previous ``[invalid json] <raw>``
        # stub was dropped because re-parsing a line that failed once
        # never succeeds — surfacing the raw bytes belongs to the
        # ``V`` action (which uses ``line_offsets`` for that purpose).
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl',
                                         delete=False) as f:
            f.write('not json\n')
            path = f.name
        try:
            out = self.r._preview_message(path, 0)
            self.assertEqual(out, '')
        finally:
            os.unlink(path)


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
    """``_preview_umbrella`` concatenates direct-children bodies."""

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

    def test_prompt_umbrella_preview_includes_user_and_reply(self):
        # ``<prompt>`` umbrella preview = concat of [user voice leaf,
        # assistant reply leaf].
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
                out = self.r._preview_umbrella(f'{path}#prompt:0')
                self.assertIn('PROBE_USER_PROMPT', out)
                self.assertIn('PROBE_ASST_REPLY', out)
                # Leaf preview: just the user prompt body (no children).
                leaf = self.r._preview_message(path, 0)
                self.assertIn('PROBE_USER_PROMPT', leaf)
                self.assertNotIn('PROBE_ASST_REPLY', leaf)
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)

    def test_prompt_umbrella_preview_recurses_same_file(self):
        # Turn root → assistant tool_use → user tool_result.
        # The ``<prompt>`` umbrella's preview now recursively dumps
        # every leaf body within the same file, so the deeper
        # tool_result body is included alongside the user prompt and
        # the assistant tool_use line.
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
                out = self.r._preview_umbrella(f'{path}#prompt:0')
                self.assertIn('PROBE_PROMPT', out)
                self.assertIn('PROBE_BASH_CMD', out)
                # Grandchild (inside the nested <tool> umbrella) is
                # now included — same file, recurse through.
                self.assertIn('PROBE_BASH_OUTPUT', out)
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)

    def test_umbrella_preview_survives_per_child_renderer_error(self):
        # A renderer raising should NOT blank the umbrella's preview —
        # subsequent children must still render, and the failure
        # surfaces as a per-child error stub.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'PROBE_GOOD_USER'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'PROBE_GOOD_ASST'},
             ]}},
        ])
        try:
            saved = self.r._TREE_MODE
            saved_renderers = dict(self.r._RENDERERS)
            try:
                self.r._TREE_MODE = True
                def _boom(_obj):
                    raise RuntimeError('PROBE_BOOM_MSG')
                self.r._RENDERERS['user'] = _boom
                out = self.r._preview_umbrella(f'{path}#prompt:0')
                # The failing child's error appears…
                self.assertIn('RuntimeError', out)
                self.assertIn('PROBE_BOOM_MSG', out)
                # …and the next child still renders.
                self.assertIn('PROBE_GOOD_ASST', out)
            finally:
                self.r._RENDERERS.clear()
                self.r._RENDERERS.update(saved_renderers)
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)

    def test_tool_umbrella_preview_includes_assistant_and_result(self):
        # ``<tool>`` umbrella preview = concat of [assistant tool_use
        # leaf, tool_result leaf].
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
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
                out = self.r._preview_umbrella(f'{path}#tool:1')
                self.assertIn('PROBE_BASH_CMD', out)
                self.assertIn('PROBE_BASH_OUTPUT', out)
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
            # Direct parent is now the ``<prompt>`` umbrella wrapping
            # the subagent's turn root, NOT the turn root leaf itself.
            self.assertEqual(ancestors[-1], f'{agent_path}#prompt:0')
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
            # Direct parent is the ``<prompt>`` umbrella; no outer
            # subagent group.
            self.assertEqual(ancestors, [f'{sess_path}#prompt:0'])

    def test_full_chain_crosses_file_boundary(self):
        # Parent session: u1 (turn root) → a1 (Task tool_use) → u2 (tool_result).
        # Subagent: su1 (turn root) → sa1 (assistant text).
        # Ancestors of sa1 (deepest, line 1 in subagent) should walk:
        #   parent: <prompt> umbrella @ line 0 (wraps u1)
        #          → <tool:Task> umbrella @ line 1 (wraps a1)
        #          → <subagent> group (#agent:AGENT01)
        #          → <prompt> umbrella @ line 0 of subagent (wraps su1)
        #   → sa1 itself (target, not included)
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp(prefix='subagent-chain-')
        try:
            sess_path = os.path.join(tmp, 'parent.jsonl')
            with open(sess_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user', 'content': 'dispatch'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'id': 'toolu_x',
                         'name': 'Task',
                         'input': {'prompt': 'go', 'subagent_type': 'Explore'}},
                    ]},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u2',
                    'message': {'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'toolu_x',
                         'content': 'done'},
                    ]},
                    'toolUseResult': {'agentId': 'AGENT01',
                                      'agentType': 'Explore',
                                      'status': 'completed'},
                }) + '\n')
            sub_dir = os.path.join(tmp, 'parent', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-AGENT01.jsonl')
            with open(agent_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'su1',
                    'message': {'role': 'user', 'content': 'inside go'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'sa1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'text', 'text': 'inside reply'},
                    ]},
                }) + '\n')

            chain = self.r._ancestor_ids_for(f'{agent_path}#1')
            # Expect (root → leaf), all umbrella ids:
            #   parent's <prompt> umbrella (line 0, wraps u1)
            #   parent's <tool:Task> umbrella (line 1, wraps a1)
            #   subagent group #agent:AGENT01
            #   subagent's <prompt> umbrella (line 0, wraps su1)
            self.assertEqual(chain, [
                f'{sess_path}#prompt:0',
                f'{sess_path}#tool:1',
                f'{sess_path}#agent:AGENT01',
                f'{agent_path}#prompt:0',
            ])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

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
    """``_resolve_md_pager`` walks $MD2ANSI / md2ansi+less in order."""

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

    def _scratch_bin(self, tmp, names):
        """Create executable stubs in ``tmp`` for each name in ``names``."""
        import stat
        for name in names:
            path = os.path.join(tmp, name)
            with open(path, 'w') as f:
                f.write('#!/bin/sh\ncat "$1"\n')
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

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

    def test_md2ansi_plus_less_pipes_to_less_rs(self):
        # Default fallback when both md2ansi and less exist: pipe
        # md2ansi output through ``less -RS`` via bash.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._scratch_bin(tmp, ['md2ansi', 'less'])
            saved_path = os.environ['PATH']
            restore = self._with_env(MD2ANSI=None)
            try:
                os.environ['PATH'] = tmp
                cmd = self.r._resolve_md_pager()
                self.assertEqual(cmd[0], 'bash')
                self.assertEqual(cmd[1], '-c')
                self.assertIn('md2ansi', cmd[2])
                self.assertIn('less -RS', cmd[2])
            finally:
                os.environ['PATH'] = saved_path
                restore()

    def test_md2ansi_alone_no_pipe(self):
        # Without ``less`` on PATH, fall back to bare ``md2ansi``.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._scratch_bin(tmp, ['md2ansi'])
            saved_path = os.environ['PATH']
            restore = self._with_env(MD2ANSI=None)
            try:
                os.environ['PATH'] = tmp
                self.assertEqual(self.r._resolve_md_pager(), ['md2ansi'])
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

    def test_cache_survives_file_growth_until_explicit_reset(self):
        # New semantics (post-mtime): _scan_tree's cache doesn't
        # auto-invalidate when the file changes. The live-tail worker
        # keeps the cached td in sync; explicit Ctrl-R / refresh
        # (via ``get_children(None)``) is what drops the cache.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'promptId': 'P1',
             'message': {'role': 'user', 'content': 'one'}},
        ])
        try:
            import json as _json
            td1 = self.r._scan_tree(path)
            self.assertEqual(len([r for r in td1.records if r]), 1)
            with open(path, 'a') as f:
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'text', 'text': 'x'},
                    ]},
                }) + '\n')
            # Calling _scan_tree again WITHOUT a reset returns the
            # same cached td (stale by design — tail would have
            # folded the new record under normal operation).
            td2 = self.r._scan_tree(path)
            self.assertIs(td1, td2)
            self.assertEqual(len([r for r in td2.records if r]), 1)

            # Explicit reset via get_children(None, reload=True) drops
            # the cache; next call rebuilds from disk with all records.
            self.r.get_children(None, reload=True)
            td3 = self.r._scan_tree(path)
            self.assertIsNot(td1, td3)
            self.assertEqual(len([r for r in td3.records if r]), 2)
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
    """Tree-mode listings: _list_tree_roots / _list_prompt_children /
    _list_tool_children / _list_span_records / get_children dispatch."""

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
        # three roots: span umbrella, prompt umbrella, prompt umbrella.
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
            # u1 prompt umbrella (line 1).
            self.assertEqual(roots[1].kind, 'prompt')
            self.assertEqual(roots[1].line_no, 1)
            self.assertEqual(roots[1].id, f'{path}#prompt:1')
            self.assertTrue(roots[1].title.startswith('<prompt>'))
            # u2 prompt umbrella (line 3).
            self.assertEqual(roots[2].kind, 'prompt')
            self.assertEqual(roots[2].line_no, 3)
            self.assertEqual(roots[2].id, f'{path}#prompt:3')
        finally:
            os.unlink(path)

    def test_drill_into_turn_root_pairs_tool_result(self):
        # u1 → a1 (tool_use) → u2 (tool_result) → a2 (text).
        # <prompt:0> children = [u1 leaf, <tool:1> umbrella, a2 leaf].
        # <tool:1> children = [a1 leaf, u2 leaf].
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
            prompt_kids = self.r._list_prompt_children(path, 0)
            # [u1 leaf @ line 0, <tool:1> umbrella, a2 leaf @ line 3].
            self.assertEqual(len(prompt_kids), 3)
            self.assertEqual(prompt_kids[0].id, f'{path}#0')
            self.assertEqual(prompt_kids[0].kind, 'message')
            self.assertEqual(prompt_kids[1].id, f'{path}#tool:1')
            self.assertEqual(prompt_kids[1].kind, 'tool')
            self.assertEqual(prompt_kids[2].id, f'{path}#3')
            self.assertEqual(prompt_kids[2].kind, 'message')
            # <tool:1>'s children = [a1 leaf, u2 leaf].
            tool_kids = self.r._list_tool_children(path, 1)
            self.assertEqual([it.id for it in tool_kids],
                             [f'{path}#1', f'{path}#2'])
            # Leaves have no children in tree-mode get_children.
            saved = self.r._TREE_MODE
            try:
                self.r._TREE_MODE = True
                self.assertEqual(self.r.get_children(f'{path}#0'), [])
                self.assertEqual(self.r.get_children(f'{path}#2'), [])
                self.assertEqual(self.r.get_children(f'{path}#3'), [])
            finally:
                self.r._TREE_MODE = saved
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
            # Two ``<prompt>`` umbrellas; both have kind='prompt'.
            self.assertEqual([(it.kind, it.line_no) for it in roots],
                             [('prompt', 0), ('prompt', 2)])
            # u1's prompt umbrella exposes [u1 leaf, a1 leaf].
            kids = self.r._list_prompt_children(path, 0)
            self.assertEqual([it.id for it in kids],
                             [f'{path}#0', f'{path}#1'])
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

            # ``<tool:1>`` umbrella children = [a1 leaf, u2 tool_result
            # leaf, <subagent> umbrella].
            kids = self.r._list_tool_children(sess_path, 1)
            self.assertEqual(len(kids), 3)
            self.assertEqual(kids[0].id, f'{sess_path}#1')
            self.assertEqual(kids[0].kind, 'message')
            self.assertEqual(kids[1].id, f'{sess_path}#2')
            self.assertEqual(kids[1].kind, 'message')   # tool_result
            self.assertEqual(kids[2].kind, 'subagent')
            self.assertEqual(kids[2].agent_id, 'AGENT01')
            self.assertEqual(kids[2].id,
                             f'{sess_path}#agent:AGENT01')
            # The assistant record itself is a leaf in tree mode now.
            td = self.r._scan_tree(sess_path)
            asst_item = self.r._tree_item(sess_path,
                                          td.records[1], td)
            self.assertFalse(asst_item.has_children)
            # But the <tool> umbrella wrapping it does have children.
            tool_item = self.r._tool_umbrella_item(
                sess_path, 1, td.records[1], td,
            )
            self.assertTrue(tool_item.has_children)
            self.assertEqual(tool_item.id, f'{sess_path}#tool:1')
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
                # Tree mode: session jsonl → root-level rows (one
                # ``<prompt>`` umbrella).
                self.r._TREE_MODE = True
                roots = self.r.get_children(path)
                self.assertEqual(len(roots), 1)
                self.assertEqual(roots[0].kind, 'prompt')
                self.assertEqual(roots[0].line_no, 0)   # u1
                self.assertEqual(roots[0].id, f'{path}#prompt:0')
                # Prompt umbrella's children → [u1 leaf, a1 leaf].
                kids = self.r.get_children(f'{path}#prompt:0')
                self.assertEqual([it.id for it in kids],
                                 [f'{path}#0', f'{path}#1'])
                # Regular message ids are leaves in tree mode.
                self.assertEqual(self.r.get_children(f'{path}#1'), [])
                self.assertEqual(self.r.get_children(f'{path}#0'), [])

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


class TestUmbrellaShapes(unittest.TestCase):
    """``<prompt>``, ``<tool>``, ``<subagent>``, ``<system>`` umbrellas."""

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

    def test_prompt_umbrella_title_prefix_and_id(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hello world'}},
        ])
        try:
            roots = self.r._list_tree_roots(path)
            self.assertEqual(len(roots), 1)
            it = roots[0]
            self.assertEqual(it.kind, 'prompt')
            self.assertEqual(it.id, f'{path}#prompt:0')
            self.assertTrue(it.title.startswith('<prompt>'))
            self.assertIn('hello world', it.title)
        finally:
            os.unlink(path)

    def test_tool_umbrella_title_includes_tool_name(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
        ])
        try:
            kids = self.r._list_prompt_children(path, 0)
            # Children: [u1 leaf, <tool:Bash> umbrella].
            self.assertEqual(len(kids), 2)
            tool = kids[1]
            self.assertEqual(tool.kind, 'tool')
            self.assertEqual(tool.id, f'{path}#tool:1')
            self.assertEqual(tool.tool_name, 'Bash')
            self.assertTrue(tool.title.startswith('<tool:Bash>'))
        finally:
            os.unlink(path)

    def test_subagent_umbrella_title_prefix(self):
        # Subagent pseudo-item carries the ``<subagent>`` prefix.
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp(prefix='sa-umb-')
        try:
            sess = os.path.join(tmp, 'parent.jsonl')
            with open(sess, 'w') as f:
                f.write('{}\n')
            sub_dir = os.path.join(tmp, 'parent', 'subagents')
            os.makedirs(sub_dir)
            agent = os.path.join(sub_dir, 'agent-A1.jsonl')
            with open(agent, 'w') as f:
                f.write('{}\n')
            with open(os.path.join(sub_dir,
                                   'agent-A1.meta.json'), 'w') as f:
                _json.dump({'agentType': 'Explore',
                            'description': 'do stuff'}, f)
            item = self.r._subagent_pseudo_item(sess, 'A1', agent)
            self.assertTrue(item.title.startswith('<subagent>'))
            self.assertIn('do stuff', item.title)
            self.assertEqual(item.id, f'{sess}#agent:A1')
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_singleton_turn_still_gets_prompt_umbrella(self):
        # A user-only turn with no assistant reply still wraps in
        # ``<prompt>``.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'lonely'}},
        ])
        try:
            roots = self.r._list_tree_roots(path)
            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].kind, 'prompt')
            kids = self.r._list_prompt_children(path, 0)
            self.assertEqual(len(kids), 1)
            self.assertEqual(kids[0].id, f'{path}#0')
        finally:
            os.unlink(path)

    def test_singleton_tool_without_result_still_wraps(self):
        # Assistant with tool_use but no paired tool_result yet still
        # gets a ``<tool>`` umbrella; its only child is the assistant
        # itself.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Read',
                  'input': {'file_path': '/x'}},
             ]}},
        ])
        try:
            kids = self.r._list_prompt_children(path, 0)
            tool = kids[1]
            self.assertEqual(tool.kind, 'tool')
            tool_kids = self.r._list_tool_children(path, 1)
            self.assertEqual(len(tool_kids), 1)
            self.assertEqual(tool_kids[0].id, f'{path}#1')
        finally:
            os.unlink(path)

    def test_wrapped_leaf_keeps_row_bg_voice_marker(self):
        # User voice and assistant voice leaves keep their row_bg stripe
        # even when folded under a ``<prompt>`` / ``<tool>`` umbrella —
        # the visual marker belongs to the voice itself, not to the
        # outermost row.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hello'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'mixed'},
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
        ])
        try:
            roots = self.r._list_tree_roots(path)
            prompt = roots[0]
            self.assertEqual(prompt.row_bg, 235)
            kids = self.r._list_prompt_children(path, 0)
            user_leaf = kids[0]
            # The wrapped user leaf keeps the user-voice stripe.
            self.assertEqual(user_leaf.row_bg, 235)
            tool = kids[1]
            # Assistant has both text and tool_use → tool umbrella is
            # voice.
            self.assertEqual(tool.row_bg, 17)
            tool_kids = self.r._list_tool_children(path, 1)
            asst_leaf = tool_kids[0]
            self.assertEqual(asst_leaf.row_bg, 17)
        finally:
            os.unlink(path)

    def test_ancestor_chain_walks_through_umbrellas(self):
        # Tool_result u2 @ line 2 → <tool:1> → <prompt:0>.
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
        ])
        try:
            chain = self.r._ancestor_ids_for(f'{path}#2')
            self.assertEqual(chain, [
                f'{path}#prompt:0',
                f'{path}#tool:1',
            ])
        finally:
            os.unlink(path)


class TestLiveTail(unittest.TestCase):
    """``_read_new_records`` is the live-tail counterpart of ``_scan_tree``.

    Shared between tree- and flat-mode: reads new bytes, parses
    records, optionally folds them into the tree-mode ``_TreeData``.
    Returns ``(records, dirty_parents)``.
    """

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

    def _append(self, path, records):
        import json as _json
        with open(path, 'a') as f:
            for r in records:
                f.write(_json.dumps(r) + '\n')

    def setUp(self):
        # Each test starts with empty caches so prior tests don't leak
        # state across the module-level dicts.
        self.r._TREE_CACHE.clear()
        self.r._TAIL_STATE.clear()

    def test_line_offsets_sentinel_pattern(self):
        # ``td.line_offsets`` has N+1 entries (sentinel past the end)
        # so length(line k) = line_offsets[k+1] - line_offsets[k] for
        # all k in [0, N).
        import json as _json
        recs = [
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'one'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'ok'},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': 'two'}},
        ]
        path = self._write_jsonl(recs)
        try:
            td = self.r._scan_tree(path)
            self.assertEqual(len(td.records), 3)
            self.assertEqual(len(td.line_offsets), 4)
            self.assertEqual(td.line_offsets[0], 0)
            # Each entry strictly increases (every line has content).
            for i in range(len(td.line_offsets) - 1):
                self.assertLess(td.line_offsets[i], td.line_offsets[i + 1])
            # The recorded line bytes round-trip back to the source.
            with open(path, 'rb') as f:
                for k, want in enumerate(recs):
                    f.seek(td.line_offsets[k])
                    chunk = f.read(td.line_offsets[k + 1] - td.line_offsets[k])
                    self.assertEqual(_json.loads(chunk), want)
        finally:
            os.unlink(path)

    def test_line_offsets_grow_with_tail_appends(self):
        # Live tail extends ``records`` AND ``line_offsets`` in
        # lockstep. After appending K new lines the sentinel still
        # sits one past the last record.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            td = self.r._scan_tree(path)
            self.assertEqual(len(td.records), 1)
            self.assertEqual(len(td.line_offsets), 2)
            self._append(path, [
                {'type': 'assistant', 'uuid': 'a1',
                 'message': {'role': 'assistant', 'content': [
                     {'type': 'text', 'text': 'x'},
                 ]}},
                {'type': 'user', 'uuid': 'u2',
                 'message': {'role': 'user', 'content': 'two'}},
            ])
            self.r._read_new_records(path)
            self.assertEqual(len(td.records), 3)
            self.assertEqual(len(td.line_offsets), 4)
            # Sentinel matches the file size.
            self.assertEqual(td.line_offsets[-1], os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_read_jsonl_line_serves_from_cache(self):
        # After ``_scan_tree`` runs, ``_read_jsonl_line`` reads the
        # cached parsed dict — no file I/O.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._scan_tree(path)
            # Tamper with the cached record; if the function used
            # the cache, it must return the tampered value.
            self.r._TREE_CACHE[path].records[0] = {'__probe__': 'YES'}
            self.assertEqual(self.r._read_jsonl_line(path, 0),
                             {'__probe__': 'YES'})
            # Out-of-range / invalid index → None.
            self.assertIsNone(self.r._read_jsonl_line(path, 5))
        finally:
            os.unlink(path)

    def test_read_jsonl_line_returns_none_for_invalid_lines(self):
        # Invalid JSON line → ``td.records[n]`` is None, and the
        # reader returns None (no ``__raw__`` re-parse).
        import tempfile
        path = tempfile.NamedTemporaryFile(
            'w', suffix='.jsonl', delete=False,
        ).name
        with open(path, 'w') as f:
            f.write('not json\n')
        try:
            self.r._scan_tree(path)
            self.assertIsNone(self.r._read_jsonl_line(path, 0))
        finally:
            os.unlink(path)

    def test_bulk_scan_populates_tail_state(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._scan_tree(path)
            tail = self.r._TAIL_STATE.get(path)
            self.assertIsNotNone(tail)
            self.assertGreater(tail.byte_offset, 0)
            self.assertEqual(tail.last_size, tail.byte_offset)
            self.assertFalse(tail.error)
            self.assertIsNotNone(tail.cursor)
        finally:
            os.unlink(path)

    def test_read_extends_existing_turn(self):
        # Bulk-scan a file with one open turn, append an assistant reply,
        # read; the turn's direct children should grow and dirty parents
        # should mark the prompt umbrella.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
        ])
        try:
            td = self.r._scan_tree(path)
            self.assertEqual(len(td.turn_direct.get('u1', [])), 0)
            self._append(path, [
                {'type': 'assistant', 'uuid': 'a1',
                 'message': {'role': 'assistant', 'content': [
                     {'type': 'text', 'text': 'PROBE_REPLY'},
                 ]}},
            ])
            records, dirty = self.r._read_new_records(path)
            # One new record returned in file order.
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][1].get('uuid'), 'a1')
            # Tree fold: the new leaf belongs to <prompt:0>.
            self.assertIn(f'{path}#prompt:0', dirty)
            self.assertEqual(len(td.turn_direct['u1']), 1)
            tail = self.r._TAIL_STATE[path]
            self.assertGreater(tail.byte_offset, 0)
            self.assertFalse(tail.error)
        finally:
            os.unlink(path)

    def test_read_opens_new_turn(self):
        # Append a fresh user voice → new <prompt> umbrella appears
        # at the file's root level (dirty set contains the path).
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'first'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'r'},
             ]}},
        ])
        try:
            td = self.r._scan_tree(path)
            self.assertEqual(
                sum(1 for e in td.roots_in_order if e['kind'] == 'turn'),
                1,
            )
            self._append(path, [
                {'type': 'user', 'uuid': 'u2',
                 'message': {'role': 'user', 'content': 'second'}},
            ])
            records, dirty = self.r._read_new_records(path)
            self.assertEqual(len(records), 1)
            self.assertIn(path, dirty)
            self.assertEqual(
                sum(1 for e in td.roots_in_order if e['kind'] == 'turn'),
                2,
            )
        finally:
            os.unlink(path)

    def test_read_incomplete_trailing_line_holds_offset(self):
        # Append a partial line (no trailing newline). The tail must NOT
        # parse it; offset stays at the line's start so the next call
        # picks it up once the rest arrives.
        import json as _json
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._scan_tree(path)
            offset_before = self.r._TAIL_STATE[path].byte_offset
            full_line = _json.dumps({
                'type': 'assistant', 'uuid': 'a1',
                'message': {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'ok'},
                ]},
            })
            split = len(full_line) // 2
            with open(path, 'a') as f:
                f.write(full_line[:split])
            records, dirty = self.r._read_new_records(path)
            self.assertFalse(records)
            self.assertFalse(dirty)
            tail = self.r._TAIL_STATE[path]
            self.assertEqual(tail.byte_offset, offset_before)
            self.assertFalse(tail.error)
            with open(path, 'a') as f:
                f.write(full_line[split:] + '\n')
            records, dirty = self.r._read_new_records(path)
            self.assertEqual(len(records), 1)
            self.assertIn(f'{path}#prompt:0', dirty)
            tail = self.r._TAIL_STATE[path]
            self.assertGreater(tail.byte_offset, offset_before)
            self.assertFalse(tail.error)
        finally:
            os.unlink(path)

    def test_read_malformed_json_latches_error(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._scan_tree(path)
            with open(path, 'a') as f:
                f.write('not valid json\n')
            self.r._read_new_records(path)
            tail = self.r._TAIL_STATE[path]
            self.assertTrue(tail.error)
            # Further calls bail without resuming.
            with open(path, 'a') as f:
                f.write('{"type": "user", "uuid": "u2", '
                        '"message": {"role": "user", '
                        '"content": "second"}}\n')
            records, dirty = self.r._read_new_records(path)
            self.assertFalse(records)
            self.assertFalse(dirty)
            self.assertTrue(tail.error)
        finally:
            os.unlink(path)

    def test_read_truncation_latches_error(self):
        # File shrinks (e.g. external truncation / rotation) → offset
        # is stale → error flag latches; no patches issued.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'one'}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': 'two'}},
        ])
        try:
            self.r._scan_tree(path)
            with open(path, 'w') as f:
                f.write('')
            records, dirty = self.r._read_new_records(path)
            self.assertFalse(records)
            self.assertFalse(dirty)
            self.assertTrue(self.r._TAIL_STATE[path].error)
        finally:
            os.unlink(path)

    def test_read_no_change_returns_empty(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._scan_tree(path)
            self.assertEqual(self.r._read_new_records(path), ([], set()))
        finally:
            os.unlink(path)

    def test_read_no_state_returns_empty(self):
        # Tail without a prior bulk scan: nothing to do.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.assertNotIn(path, self.r._TAIL_STATE)
            self.assertEqual(self.r._read_new_records(path), ([], set()))
        finally:
            os.unlink(path)

    def test_read_works_without_tree_cache_flat_mode_fallback(self):
        # No ``_TREE_CACHE`` entry (flat-mode lazy bootstrap): the read
        # still returns records but produces no dirty parents.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._TAIL_STATE[path] = self.r._TailState(
                byte_offset=0, last_size=0, start_line=0,
                cursor=self.r._ScanCursor(), error=False,
            )
            self.assertNotIn(path, self.r._TREE_CACHE)
            records, dirty = self.r._read_new_records(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][0], 0)
            self.assertEqual(records[0][1].get('uuid'), 'u1')
            self.assertEqual(dirty, set())
            # ``start_line`` advances even without td.records.
            self.assertEqual(self.r._TAIL_STATE[path].start_line, 1)
        finally:
            os.unlink(path)

    def test_get_children_none_reload_clears_caches(self):
        # Ctrl-R signal: ``get_children(None, reload=True)`` wipes the
        # recipe-internal caches so _scan_tree rebuilds on the next
        # per-file get_children. ``reload=False`` (the default and the
        # initial-load path) preserves them — see ticket #407.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._scan_tree(path)
            self.assertIn(path, self.r._TREE_CACHE)
            self.assertIn(path, self.r._TAIL_STATE)
            # Initial-load semantics: don't wipe.
            self.r.get_children(None)
            self.assertIn(path, self.r._TREE_CACHE)
            self.assertIn(path, self.r._TAIL_STATE)
            # Explicit refresh: wipe.
            self.r.get_children(None, reload=True)
            self.assertNotIn(path, self.r._TREE_CACHE)
            self.assertNotIn(path, self.r._TAIL_STATE)
        finally:
            os.unlink(path)

    def test_get_children_subtree_reload_busts_just_that_file(self):
        # ``get_children(<jsonl>, reload=True)`` drops only that file's
        # _TreeData and rebuilds it; siblings' caches stay intact (same
        # _TreeData identity).
        path_a = self._write_jsonl([
            {'type': 'user', 'uuid': 'a1',
             'message': {'role': 'user', 'content': 'a'}},
        ])
        path_b = self._write_jsonl([
            {'type': 'user', 'uuid': 'b1',
             'message': {'role': 'user', 'content': 'b'}},
        ])
        try:
            td_a_before = self.r._scan_tree(path_a)
            td_b_before = self.r._scan_tree(path_b)
            # Reload path_a — its _TreeData should be rebuilt.
            self.r.get_children(path_a, reload=True)
            self.assertIsNot(self.r._TREE_CACHE[path_a], td_a_before)
            # path_b's cache untouched.
            self.assertIs(self.r._TREE_CACHE[path_b], td_b_before)
            # Non-reload call leaves both alone.
            td_a_after = self.r._TREE_CACHE[path_a]
            self.r.get_children(path_a)
            self.assertIs(self.r._TREE_CACHE[path_a], td_a_after)
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def _fake_browser_with_children(self, path, children):
        seen_ops = []

        class FakeState:
            def __init__(s):
                s._children = {path: list(children)}
                s._items_by_id = {it.id: it for it in children}
        class FakeBrowser:
            def __init__(s):
                s._state = FakeState()
            def update_data(s, ops):
                seen_ops.append(list(ops))
            def post(s, fn):
                pass
            def get_item(s, id_):
                return s._state._items_by_id.get(id_)
            def cached_children(s, parent_id):
                kids = s._state._children.get(parent_id)
                return None if kids is None else list(kids)
            def all_items(s):
                return iter(list(s._state._items_by_id.values()))
        return FakeBrowser(), seen_ops

    def test_push_flat_inserts_after_last_subagent(self):
        # New flat-mode rows must land AFTER the trailing subagent row
        # so the subagent group keeps its sticky-top position. The
        # push step builds Items from records returned by
        # ``_read_new_records`` and emits ``upsert(...,
        # where=('after', None, last_sub_idx))``.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'one'}},
        ])
        try:
            fake_subs = [
                self.r.Item(id=f'{path}#agent:SUB_A',
                            title='<subagent>  A'),
                self.r.Item(id=f'{path}#agent:SUB_B',
                            title='<subagent>  B'),
            ]
            fake_subs[0].kind = 'subagent'
            fake_subs[1].kind = 'subagent'
            # Just two existing items in the framework's cache; the
            # message row from line 0 isn't important here.
            fake_b, seen_ops = self._fake_browser_with_children(
                path, list(fake_subs),
            )

            records = [(
                1,
                {'type': 'user', 'uuid': 'u2',
                 'message': {'role': 'user', 'content': 'two'}},
            )]
            self.r._push_flat_inserts(fake_b, path, records)

            self.assertEqual(len(seen_ops), 1)
            ops = seen_ops[0]
            upserts = [op for op in ops if op[0] == 'upsert']
            self.assertEqual(len(upserts), 1)
            # ``after`` index 1 = last subagent's position.
            self.assertEqual(upserts[0][4], ('after', None, 1))
        finally:
            os.unlink(path)

    def test_push_flat_inserts_no_subagents_uses_minus_one(self):
        # No subagents in the existing list → ref=-1, which the
        # framework collapses to position 0 (top of list).
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'one'}},
        ])
        try:
            self.r._scan_tree(path)
            fake_msgs = self.r._list_messages(path)
            fake_b, seen_ops = self._fake_browser_with_children(
                path, fake_msgs,
            )

            records = [
                (1, {'type': 'user', 'uuid': 'u2',
                     'message': {'role': 'user', 'content': 'two'}}),
                (2, {'type': 'assistant', 'uuid': 'a1',
                     'message': {'role': 'assistant', 'content': [
                         {'type': 'text', 'text': 'three'},
                     ]}}),
            ]
            self.r._push_flat_inserts(fake_b, path, records)

            ops = seen_ops[0]
            upserts = [op for op in ops if op[0] == 'upsert']
            self.assertEqual(len(upserts), 2)
            for op in upserts:
                self.assertEqual(op[4], ('after', None, -1))
            self.assertFalse(
                [op for op in ops if op[0] == 'remove'],
            )
        finally:
            os.unlink(path)

    def test_cursor_tail_path_extraction(self):
        # Plain file path: returned as-is when it exists.
        path = self._write_jsonl([{'type': 'user'}])
        try:
            self.assertEqual(self.r._cursor_tail_path(path), path)
            # Message id (`<path>#N`).
            self.assertEqual(self.r._cursor_tail_path(f'{path}#0'), path)
            # Umbrella ids strip to the path.
            self.assertEqual(self.r._cursor_tail_path(f'{path}#prompt:0'),
                             path)
            self.assertEqual(self.r._cursor_tail_path(f'{path}#tool:1'),
                             path)
            self.assertEqual(self.r._cursor_tail_path(f'{path}#span:2'),
                             path)
            # Non-jsonl path: None.
            self.assertIsNone(self.r._cursor_tail_path('/etc/passwd'))
            self.assertIsNone(self.r._cursor_tail_path(None))
        finally:
            os.unlink(path)

    def test_cursor_tail_path_subagent_row_tree_vs_flat(self):
        # Subagent group row: tree mode → parent jsonl; flat mode →
        # subagent jsonl.
        import tempfile
        tmp = tempfile.mkdtemp(prefix='tail-sa-')
        try:
            sess = os.path.join(tmp, 'parent.jsonl')
            with open(sess, 'w') as f:
                f.write('{}\n')
            sub_dir = os.path.join(tmp, 'parent', 'subagents')
            os.makedirs(sub_dir)
            agent = os.path.join(sub_dir, 'agent-AGENT01.jsonl')
            with open(agent, 'w') as f:
                f.write('{}\n')
            row_id = f'{sess}#agent:AGENT01'
            saved = self.r._TREE_MODE
            try:
                self.r._TREE_MODE = True
                self.assertEqual(self.r._cursor_tail_path(row_id), sess)
                self.r._TREE_MODE = False
                self.assertEqual(self.r._cursor_tail_path(row_id), agent)
            finally:
                self.r._TREE_MODE = saved
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestViewEditSource(unittest.TestCase):
    """``V`` / ``E`` extract per-line bytes from ``line_offsets``."""

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

    def setUp(self):
        self.r._TREE_CACHE.clear()
        self.r._TAIL_STATE.clear()

    def test_gather_line_source_classifies_ids(self):
        path = '/tmp/fake.jsonl'
        items = [
            self.r.Item(id=f'{path}#3'),                  # message leaf
            self.r.Item(id=f'{path}#5'),                  # message leaf
            self.r.Item(id=f'{path}#prompt:10'),          # umbrella
            self.r.Item(id=f'{path}#tool:11'),            # umbrella
            self.r.Item(id=f'{path}#span:0'),             # umbrella
            self.r.Item(id=f'{path}#agent:AAA'),          # subagent group
            self.r.Item(id=path),                         # bare file
            self.r.Item(id='/some/proj'),                 # directory
        ]
        per_line, whole_paths = self.r._gather_line_source(items)
        # Two message leaves on the same path → grouped.
        self.assertEqual(per_line, {path: [3, 5]})
        # Whole-path entries: umbrellas + subagent + bare file + dir.
        whole_paths_only = [p for p, _ in whole_paths]
        self.assertEqual(len(whole_paths_only), 6)
        # Subagent group resolves to the agent's jsonl, not the parent
        # session.
        self.assertIn(
            os.path.join(self.r._subagents_dir(path), 'agent-AAA.jsonl'),
            whole_paths_only,
        )
        # Plain umbrella ids resolve to the file path.
        for u in (f'{path}', f'{path}', f'{path}', '/some/proj', path):
            pass
        self.assertEqual(whole_paths_only.count(path), 4)
        # __truncated__ falls back to whole-file.
        per_line2, whole2 = self.r._gather_line_source([
            self.r.Item(id=f'{path}#__truncated__'),
        ])
        self.assertEqual(per_line2, {})
        self.assertEqual([p for p, _ in whole2], [path])

    def test_write_line_excerpts_uses_offsets(self):
        import json as _json
        recs = [
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'one'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'two'},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': 'three'}},
        ]
        path = self._write_jsonl(recs)
        try:
            self.r._scan_tree(path)
            tmp = self.r._write_line_excerpts({path: [0, 2]})
            self.assertIsNotNone(tmp)
            try:
                with open(tmp, 'r') as f:
                    lines = [_json.loads(line) for line in f
                             if line.strip()]
                self.assertEqual(lines, [recs[0], recs[2]])
            finally:
                os.unlink(tmp)
        finally:
            os.unlink(path)

    def test_write_line_excerpts_no_cache_returns_none(self):
        # Without ``_TREE_CACHE`` we can't resolve offsets — bail
        # cleanly.
        path = '/tmp/nonexistent.jsonl'
        tmp = self.r._write_line_excerpts({path: [0]})
        self.assertIsNone(tmp)

    def test_gather_umbrella_lines_recurses_same_file(self):
        # ``<prompt:0>`` contains ``<tool:1>`` which contains a
        # tool_result on line 2. Gathering lines for the prompt
        # umbrella should pull all three same-file leaves.
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
        ])
        try:
            saved = self.r._TREE_MODE
            self.r._TREE_MODE = True
            try:
                self.r._scan_tree(path)
                # Prompt umbrella → user msg (0), assistant tool_use
                # (1), tool_result (2) in document order.
                self.assertEqual(
                    self.r._gather_umbrella_lines(f'{path}#prompt:0'),
                    [0, 1, 2],
                )
                # Tool umbrella alone → assistant + tool_result.
                self.assertEqual(
                    self.r._gather_umbrella_lines(f'{path}#tool:1'),
                    [1, 2],
                )
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)

    def test_gather_umbrella_lines_stops_at_subagent_boundary(self):
        # Parent session: u1 (turn root) → a1 (Task tool_use) → u2
        # (tool_result with agentId). Gathering lines for the
        # prompt umbrella includes the same-file leaves but NOT
        # anything from the subagent's jsonl.
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp(prefix='gum-sa-')
        try:
            sess_path = os.path.join(tmp, 'parent.jsonl')
            with open(sess_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user', 'content': 'go'},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'assistant', 'uuid': 'a1',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'id': 'tu1', 'name': 'Task',
                         'input': {'prompt': 'do', 'subagent_type': 'Explore'}},
                    ]},
                }) + '\n')
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u2',
                    'message': {'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'tu1',
                         'content': 'ok'},
                    ]},
                    'toolUseResult': {'agentId': 'AGENT01',
                                      'agentType': 'Explore',
                                      'status': 'completed'},
                }) + '\n')
            sub_dir = os.path.join(tmp, 'parent', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-AGENT01.jsonl')
            with open(agent_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'sub1',
                    'message': {'role': 'user', 'content': 'inside'},
                }) + '\n')
            saved = self.r._TREE_MODE
            self.r._TREE_MODE = True
            try:
                # All three parent-file leaves; nothing from subagent.
                lines = self.r._gather_umbrella_lines(f'{sess_path}#prompt:0')
                self.assertEqual(lines, [0, 1, 2])
            finally:
                self.r._TREE_MODE = saved
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gather_line_source_recurses_umbrella(self):
        # Pass an umbrella id to ``_gather_line_source``; per_line
        # picks up all leaves under that umbrella in document order.
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
        ])
        try:
            saved = self.r._TREE_MODE
            self.r._TREE_MODE = True
            try:
                self.r._scan_tree(path)
                items = [self.r.Item(id=f'{path}#prompt:0')]
                per_line, whole = self.r._gather_line_source(items)
                self.assertEqual(per_line, {path: [0, 1, 2]})
                self.assertFalse(whole)
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)

    def test_gather_umbrella_lines_multi_tool_full_coverage(self):
        # Stress: turn with two tool calls + a text reply between
        # them + a closing text reply. The umbrella gather must
        # pull every line in document order with no duplicates.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},                # 0
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},                                                          # 1
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1',
                  'content': 'r1'},
             ]}},                                                          # 2
            {'type': 'assistant', 'uuid': 'a2',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'between'},
             ]}},                                                          # 3
            {'type': 'assistant', 'uuid': 'a3',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't2', 'name': 'Read',
                  'input': {'file_path': '/x'}},
             ]}},                                                          # 4
            {'type': 'user', 'uuid': 'u3',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't2',
                  'content': 'r2'},
             ]}},                                                          # 5
            {'type': 'assistant', 'uuid': 'a4',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'after'},
             ]}},                                                          # 6
        ])
        try:
            saved = self.r._TREE_MODE
            self.r._TREE_MODE = True
            try:
                self.r._scan_tree(path)
                # Listing-order recursion may interleave; the test
                # checks the canonicalised output from the V/E
                # gather (dedupe + sort) is the full file range.
                items = [self.r.Item(id=f'{path}#prompt:0')]
                per_line, _ = self.r._gather_line_source(items)
                self.assertEqual(per_line, {path: [0, 1, 2, 3, 4, 5, 6]})
                # Each tool umbrella covers only its own pair.
                self.assertEqual(
                    sorted(self.r._gather_umbrella_lines(f'{path}#tool:1')),
                    [1, 2],
                )
                self.assertEqual(
                    sorted(self.r._gather_umbrella_lines(f'{path}#tool:4')),
                    [4, 5],
                )
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)

    def test_gather_dedupes_and_sorts_overlapping_targets(self):
        # User selects an umbrella AND one of its child leaves (or
        # two overlapping umbrellas). Pre-select pass collapses to
        # one entry per leaf in file order — no duplicates, no
        # out-of-order entries.
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
        ])
        try:
            saved = self.r._TREE_MODE
            self.r._TREE_MODE = True
            try:
                self.r._scan_tree(path)
                # Targets: leaf line 2 first, THEN the umbrella that
                # contains lines 0..2. Pre-select must collapse the
                # overlap and emit lines in file order.
                items = [
                    self.r.Item(id=f'{path}#2'),
                    self.r.Item(id=f'{path}#prompt:0'),
                ]
                per_line, _ = self.r._gather_line_source(items)
                self.assertEqual(per_line, {path: [0, 1, 2]})
            finally:
                self.r._TREE_MODE = saved
        finally:
            os.unlink(path)

    def test_write_line_excerpts_skips_out_of_range_lines(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._scan_tree(path)
            # Line 99 doesn't exist; no entry should be written.
            tmp = self.r._write_line_excerpts({path: [99]})
            self.assertIsNone(tmp)
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


class TestAttributionAgentTag(unittest.TestCase):
    """Subagent assistant rows surface attributionAgent in the tag."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_row_tag_assistant_with_attribution(self):
        rec = {'type': 'assistant', 'attributionAgent': 'general-purpose',
               'message': {'role': 'assistant',
                           'content': [{'type': 'text', 'text': 'hi'}]}}
        tag = self.r._row_tag('assistant', rec)
        self.assertEqual(tag, 'assistant · general-purpose')

    def test_row_tag_user_record_unaffected(self):
        # attributionAgent on a user row is unusual but shouldn't
        # cause the tag to mutate.
        rec = {'type': 'user', 'attributionAgent': 'general-purpose',
               'message': {'role': 'user', 'content': 'hi'}}
        tag = self.r._row_tag('user', rec)
        self.assertEqual(tag, 'user')

    def test_row_tag_missing_attribution(self):
        rec = {'type': 'assistant',
               'message': {'role': 'assistant',
                           'content': [{'type': 'text', 'text': 'hi'}]}}
        tag = self.r._row_tag('assistant', rec)
        self.assertEqual(tag, 'assistant')

    def test_list_messages_row_carries_attribution_tag(self):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        f.write(_json.dumps({
            'type': 'assistant', 'attributionAgent': 'general-purpose',
            'message': {'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'hi'}]},
        }) + '\n')
        f.close()
        try:
            self.r._TREE_CACHE.clear()
            items = self.r._list_messages(f.name)
            self.assertIn('general-purpose', items[0].tag)
        finally:
            os.unlink(f.name)


class TestSubagentUmbrellaVoice(unittest.TestCase):
    """Subagent umbrellas are marked as voice rows (assistant stripe)
    and their content is NOT inlined into the parent's preview."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _build(self, tmp, with_dispatch=True):
        import json as _json
        proj = os.path.join(tmp, '-x')
        os.makedirs(proj)
        sess = os.path.join(proj, 'parent-sid.jsonl')
        sub_dir = os.path.join(proj, 'parent-sid', 'subagents')
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, 'agent-A1.jsonl'), 'w') as f:
            f.write(_json.dumps({
                'type': 'assistant',
                'message': {'role': 'assistant',
                            'content': [{'type': 'text',
                                         'text': 'inside-subagent'}]},
            }) + '\n')
        with open(os.path.join(sub_dir, 'agent-A1.meta.json'), 'w') as fm:
            _json.dump({'agentType': 'general-purpose',
                        'description': 'do work'}, fm)
        recs = []
        if with_dispatch:
            recs += [
                {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
                 'message': {'role': 'user', 'content': 'kick off'}},
                {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                 'message': {'role': 'assistant', 'content': [{
                     'type': 'tool_use', 'id': 'toolu_X', 'name': 'Agent',
                     'input': {'subagent_type': 'general-purpose',
                               'description': 'do work', 'prompt': 'p'},
                 }]}},
                {'type': 'user', 'uuid': 't1', 'parentUuid': 'a1',
                 'message': {'role': 'user', 'content': [{
                     'type': 'tool_result', 'tool_use_id': 'toolu_X',
                     'content': 'done',
                 }]},
                 'toolUseResult': {'status': 'completed', 'agentId': 'A1',
                                   'agentType': 'general-purpose',
                                   'content': []}},
            ]
        with open(sess, 'w') as f:
            for r in recs:
                f.write(_json.dumps(r) + '\n')
        return sess

    def test_subagent_row_has_voice_bg(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build(tmp)
            rows = self.r._list_subagents_for_session(sess)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].row_bg, 17)  # assistant stripe

    def test_inline_subagent_pseudo_item_has_voice_bg(self):
        # Tree-mode placement uses _subagent_pseudo_item, not the
        # session-level lister. It must carry the same stripe.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build(tmp)
            sub_dir = self.r._subagents_dir(sess)
            agent_path = os.path.join(sub_dir, 'agent-A1.jsonl')
            item = self.r._subagent_pseudo_item(sess, 'A1', agent_path)
            self.assertEqual(item.row_bg, 17)

    def test_orphan_subagent_row_keeps_voice_bg(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build(tmp, with_dispatch=False)
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(sess)
            orphans = self.r._orphan_subagents_for_session(sess, td)
            self.assertEqual(len(orphans), 1)
            self.assertEqual(orphans[0].row_bg, 17)
            self.assertIn('orphan', orphans[0].tag)

    def test_parent_preview_does_not_inline_subagent_content(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build(tmp)
            self.r._TREE_CACHE.clear()
            # Compose the parent's <tool:Agent> umbrella preview — the
            # subagent's body must NOT bleed in via the umbrella cascade.
            preview = self.r._preview_umbrella(f'{sess}#tool:1')
            self.assertNotIn('inside-subagent', preview)


class TestScopeCard(unittest.TestCase):
    """Synthetic top row preview is prefixed by a scope card."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write(self, records, suffix='.jsonl'):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix=suffix, delete=False)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def test_card_session_file_fields(self):
        path = self._write([
            {'type': 'user', 'sessionId': 's-uuid', 'entrypoint': 'cli',
             'cwd': '/work', 'gitBranch': 'main', 'slug': 'happy-slug',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(path)
            card = self.r._fmt_scope_card(path, td)
            self.assertIn('browse-claude', card)
            self.assertIn(path, card)
            self.assertIn('s-uuid', card)
            self.assertIn('cli', card)
            self.assertIn('/work', card)
            self.assertIn('main', card)
            self.assertIn('happy-slug', card)
            self.assertIn('lines', card)
            self.assertIn('voice', card)
        finally:
            os.unlink(path)

    def test_card_omits_slug_when_absent(self):
        path = self._write([
            {'type': 'user', 'sessionId': 's-uuid',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(path)
            card = self.r._fmt_scope_card(path, td)
            self.assertNotIn('slug', card)
        finally:
            os.unlink(path)

    def test_card_subagent_variant(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-x')
            os.makedirs(proj)
            sub_dir = os.path.join(proj, 'parent-sid', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-AID42.jsonl')
            with open(agent_path, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'sessionId': 'parent-sid',
                    'message': {'role': 'user', 'content': 'do it'},
                }) + '\n')
            with open(os.path.join(sub_dir, 'agent-AID42.meta.json'),
                      'w') as fm:
                _json.dump({'agentType': 'general-purpose',
                            'description': 'compose previews'}, fm)
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(agent_path)
            card = self.r._fmt_scope_card(agent_path, td)
            self.assertIn('subagent', card)
            self.assertIn('AID42', card)
            self.assertIn('general-purpose', card)
            self.assertIn('compose previews', card)
            self.assertIn('parent-sid', card)

    def test_voice_count_cached_on_td(self):
        path = self._write([
            {'type': 'user',
             'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'attachment',
             'attachment': {'type': 'date_change', 'newDate': '2026-05-22'}},
        ])
        try:
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(path)
            self.assertIsNone(td.voice_count)
            self.r._fmt_scope_card(path, td)
            self.assertEqual(td.voice_count, 1)
        finally:
            os.unlink(path)

    def test_preview_umbrella_prepends_card_for_top_row(self):
        path = self._write([
            {'type': 'user', 'sessionId': 's-uuid',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._TREE_CACHE.clear()
            out = self.r.get_preview(path)
            # Card sits at the head of the preview.
            self.assertIn('browse-claude', out)
            self.assertIn('s-uuid', out)
        finally:
            os.unlink(path)


class TestToolResultFallback(unittest.TestCase):
    """Subagent records carry no top-level toolUseResult — the body
    renderer must fall back to the inline tool_result.content block,
    and the pairing resolver must honour sourceToolAssistantUUID."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_resolve_picks_up_source_tool_assistant_uuid(self):
        rec = {
            'type': 'user',
            'sourceToolAssistantUUID': 'uuid-asst-42',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'toolu_xyz',
                'content': 'ok',
            }]},
        }
        # Empty tool_owner map — must still resolve via the explicit
        # back-pointer.
        owner = self.r._resolve_tool_owner(rec, {})
        self.assertEqual(owner, 'uuid-asst-42')

    def test_resolve_prefers_source_tool_assistant_uuid_over_tool_owner(self):
        # When both signals point at different assistants, the explicit
        # uuid back-pointer wins (it's stamped by the runtime).
        rec = {
            'type': 'user',
            'sourceToolAssistantUUID': 'uuid-asst-explicit',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'toolu_xyz',
                'content': 'ok',
            }]},
        }
        owner = self.r._resolve_tool_owner(
            rec, {'toolu_xyz': 'uuid-asst-via-map'},
        )
        self.assertEqual(owner, 'uuid-asst-explicit')

    def test_body_falls_back_to_raw_content_when_tur_missing(self):
        body = self.r._fmt_tool_use_result(None, 'plain-string-content')
        self.assertEqual(body, 'plain-string-content')

    def test_body_falls_back_to_text_blocks_when_tur_missing(self):
        body = self.r._fmt_tool_use_result(None, [
            {'type': 'text', 'text': 'first'},
            {'type': 'text', 'text': 'second'},
        ])
        self.assertIn('first', body)
        self.assertIn('second', body)

    def test_render_tool_result_subagent_shape(self):
        # Subagent-shape record: no top-level toolUseResult; the body
        # has to come from message.content[].content.
        rec = {
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'toolu_abc',
                'is_error': False,
                'content': 'bash output line',
            }]},
        }
        part = rec['message']['content'][0]
        out = self.r._render_tool_result(rec, part)
        self.assertIn('tool_result', out)
        self.assertIn('bash output line', out)


class TestOrphanSubagents(unittest.TestCase):
    """Tree-mode surfaces subagent files whose dispatch isn't wired."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _build_fixture(self, tmp, agents):
        """Create a session.jsonl + ``subagents/agent-<id>.jsonl`` files.

        ``agents`` is a list of ``(agent_id, agent_type, description,
        with_dispatch)`` — when ``with_dispatch`` is True, a paired
        assistant tool_use + user tool_result are emitted in the main
        thread so ``_maybe_link_subagent`` wires the file.
        """
        import json as _json
        proj = os.path.join(tmp, '-x')
        os.makedirs(proj)
        sess_path = os.path.join(proj, 'parent-sid.jsonl')
        sub_dir = os.path.join(proj, 'parent-sid', 'subagents')
        os.makedirs(sub_dir)
        records = []
        for agent_id, agent_type, desc, with_dispatch in agents:
            ap = os.path.join(sub_dir, f'agent-{agent_id}.jsonl')
            with open(ap, 'w') as f:
                f.write('{}\n')
            with open(os.path.join(sub_dir, f'agent-{agent_id}.meta.json'),
                      'w') as fm:
                _json.dump({'agentType': agent_type,
                            'description': desc}, fm)
            if not with_dispatch:
                continue
            tu_id = f'toolu_{agent_id}'
            asst_uuid = f'uuid-asst-{agent_id}'
            records.append({
                'type': 'user', 'uuid': f'uuid-user-{agent_id}',
                'parentUuid': None,
                'message': {'role': 'user', 'content': 'go'},
            })
            records.append({
                'type': 'assistant', 'uuid': asst_uuid,
                'parentUuid': f'uuid-user-{agent_id}',
                'message': {'role': 'assistant', 'content': [{
                    'type': 'tool_use', 'id': tu_id, 'name': 'Agent',
                    'input': {'subagent_type': agent_type,
                              'description': desc, 'prompt': 'p'},
                }]},
            })
            records.append({
                'type': 'user', 'uuid': f'uuid-tr-{agent_id}',
                'parentUuid': asst_uuid,
                'message': {'role': 'user', 'content': [{
                    'type': 'tool_result', 'tool_use_id': tu_id,
                    'content': 'ok',
                }]},
                'toolUseResult': {
                    'status': 'completed', 'agentId': agent_id,
                    'agentType': agent_type, 'content': [],
                },
            })
        with open(sess_path, 'w') as f:
            for r in records:
                f.write(_json.dumps(r) + '\n')
        return sess_path

    def test_orphans_returned_separately(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build_fixture(tmp, [
                ('A1', 'general-purpose', 'wired one',   True),
                ('A2', 'general-purpose', 'wired two',   True),
                ('A3', 'general-purpose', 'orphan one',  False),
            ])
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(sess)
            orphans = self.r._orphan_subagents_for_session(sess, td)
            self.assertEqual(len(orphans), 1)
            self.assertEqual(orphans[0].agent_id, 'A3')
            self.assertIn('orphan', orphans[0].tag)
            self.assertEqual(orphans[0].tag_style, 'dim')
            self.assertTrue(getattr(orphans[0], 'is_orphan', False))

    def test_flat_mode_still_lists_all_subs(self):
        # _list_session_children (flat-mode path) returns the full
        # subagent set unchanged — orphan surfacing is tree-mode only.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build_fixture(tmp, [
                ('A1', 'general-purpose', 'wired one',   True),
                ('A2', 'general-purpose', 'wired two',   True),
                ('A3', 'general-purpose', 'orphan one',  False),
            ])
            self.r._TREE_CACHE.clear()
            children = self.r._list_session_children(sess)
            sub_rows = [c for c in children
                        if getattr(c, 'kind', None) == 'subagent']
            self.assertEqual(len(sub_rows), 3)
            self.assertEqual(
                {r.agent_id for r in sub_rows},
                {'A1', 'A2', 'A3'},
            )

    def test_tree_roots_includes_orphans_at_top(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build_fixture(tmp, [
                ('A1', 'general-purpose', 'wired one',   True),
                ('A2', 'general-purpose', 'orphan one',  False),
            ])
            self.r._TREE_CACHE.clear()
            roots = self.r._list_tree_roots(sess)
            # First row is the orphan subagent; A1 (wired) is not at
            # the top — it renders inline under its dispatching turn.
            self.assertEqual(getattr(roots[0], 'kind', None), 'subagent')
            self.assertEqual(roots[0].agent_id, 'A2')
            kinds = [getattr(r, 'kind', None) for r in roots]
            self.assertNotIn('subagent', kinds[1:])


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


class TestVoiceOnlyFilter(unittest.TestCase):
    """``h`` hotkey + ``--no-show-all`` filter: hide everything that
    isn't voice or a subagent umbrella.

    Predicate lives in ``_passes_filter`` and is consulted by every
    Item builder. Toggle emits a single ``mod`` batch — the framework's
    hide-displacement hook handles cursor migration, so the recipe
    doesn't touch the cursor itself.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self.r._TREE_CACHE.clear()
        self.r._TAIL_STATE.clear()
        # Default state for every test; individual tests flip it
        # explicitly. Restored in tearDown so other suites don't see
        # the filter on.
        self._saved_filter = self.r._FILTER_VOICE_ONLY
        self.r._FILTER_VOICE_ONLY = False

    def tearDown(self):
        self.r._FILTER_VOICE_ONLY = self._saved_filter

    def _write_jsonl(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    # ---- _passes_filter ---------------------------------------------------

    def test_passes_filter_off_returns_true_always(self):
        # With the filter off, the predicate is always True — callers
        # rely on this so the same code path serves both states.
        self.r._FILTER_VOICE_ONLY = False
        self.assertTrue(self.r._passes_filter('whatever#0'))
        self.assertTrue(self.r._passes_filter('whatever#prompt:0'))
        self.assertTrue(self.r._passes_filter('whatever#tool:0'))
        self.assertTrue(self.r._passes_filter('whatever#span:0'))
        self.assertTrue(self.r._passes_filter('whatever#agent:ABC'))
        self.assertTrue(self.r._passes_filter('__truncated__'))

    def test_passes_filter_voice_leaf(self):
        # voice leaf passes; non-voice leaf doesn't.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._scan_tree(path)
            self.r._FILTER_VOICE_ONLY = True
            self.assertTrue(self.r._passes_filter(f'{path}#0'))
            self.assertFalse(self.r._passes_filter(f'{path}#1'))
        finally:
            os.unlink(path)

    def test_passes_filter_prompt_umbrella_always_true(self):
        # ``#prompt:`` is always voice-bearing by construction.
        self.r._FILTER_VOICE_ONLY = True
        # No td needed; predicate short-circuits on the prompt prefix.
        self.assertTrue(self.r._passes_filter('any/path#prompt:42'))

    def test_passes_filter_tool_umbrella_pure_tool_use(self):
        # Pure tool_use (no assistant text) doesn't pass.
        path = self._write_jsonl([
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._scan_tree(path)
            self.r._FILTER_VOICE_ONLY = True
            self.assertFalse(self.r._passes_filter(f'{path}#tool:0'))
        finally:
            os.unlink(path)

    def test_passes_filter_tool_umbrella_mixed_text(self):
        # Assistant turn with text + tool_use: voice-bearing.
        path = self._write_jsonl([
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'Doing X now'},
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._scan_tree(path)
            self.r._FILTER_VOICE_ONLY = True
            self.assertTrue(self.r._passes_filter(f'{path}#tool:0'))
        finally:
            os.unlink(path)

    def test_passes_filter_span_umbrella_membership(self):
        # ``#span:`` passes iff any record in the span is voice.
        # Fabricate a td directly to avoid relying on _scan_tree
        # producing a specific span layout.
        path = '/tmp/fake-span.jsonl'
        td = self.r._TreeData()
        td.span_records[0] = [
            {'type': 'system', 'subtype': 'hook'},
            {'type': 'attachment', 'attachment': {'type': 'file'}},
        ]
        td.span_records[5] = [
            {'type': 'system', 'subtype': 'hook'},
            {'type': 'user', 'uuid': 'u',
             'message': {'role': 'user', 'content': 'hi'}},
        ]
        self.r._TREE_CACHE[path] = td
        try:
            self.r._FILTER_VOICE_ONLY = True
            self.assertFalse(self.r._passes_filter(f'{path}#span:0'))
            self.assertTrue(self.r._passes_filter(f'{path}#span:5'))
        finally:
            self.r._TREE_CACHE.pop(path, None)

    def test_passes_filter_subagent_always_true(self):
        # Subagent umbrellas are unconditionally visible — the recipe
        # doesn't peek into another file to check.
        self.r._FILTER_VOICE_ONLY = True
        self.assertTrue(self.r._passes_filter('whatever#agent:ABC-DEF'))

    def test_passes_filter_unparseable_id_is_permissive(self):
        # Synthetic ids (err rows, ``__truncated__``) must stay visible.
        self.r._FILTER_VOICE_ONLY = True
        self.assertTrue(self.r._passes_filter('__truncated__'))
        self.assertTrue(self.r._passes_filter('whatever#__truncated__'))
        self.assertTrue(self.r._passes_filter('whatever#not_a_kind:5'))

    # ---- Item builders set ``hidden`` ------------------------------------

    def test_flat_message_builder_sets_hidden_under_filter(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._FILTER_VOICE_ONLY = True
            items = self.r._list_messages(path)
            # Filter out the synthetic truncation row if any.
            by_id = {it.id: it for it in items
                     if not it.id.endswith('__truncated__')}
            self.assertFalse(by_id[f'{path}#0'].hidden,
                             'voice leaf must be visible')
            self.assertTrue(by_id[f'{path}#1'].hidden,
                            'pure tool_use leaf must be hidden')
        finally:
            os.unlink(path)

    def test_flat_message_builder_default_hidden_false(self):
        # Filter off: every row built with hidden=False.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._FILTER_VOICE_ONLY = False
            items = self.r._list_messages(path)
            for it in items:
                self.assertFalse(it.hidden,
                                 f'{it.id} should be visible')
        finally:
            os.unlink(path)

    def test_tree_item_sets_hidden_under_filter(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'uuid': 'a1',
             'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            td = self.r._scan_tree(path)
            self.r._FILTER_VOICE_ONLY = True
            voice_item = self.r._tree_item(path, td.records[0], td)
            tool_item = self.r._tree_item(path, td.records[1], td)
            self.assertFalse(voice_item.hidden)
            self.assertTrue(tool_item.hidden)
        finally:
            os.unlink(path)

    def test_subagent_pseudo_item_always_visible(self):
        # Even with the filter on, subagent umbrellas keep hidden=False.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._FILTER_VOICE_ONLY = True
            # Use a fake agent path the helper will stat (file doesn't
            # need to exist — _subagent_pseudo_item catches OSError).
            item = self.r._subagent_pseudo_item(
                path, 'AID-1', '/nonexistent/agent.jsonl',
            )
            self.assertFalse(item.hidden)
        finally:
            os.unlink(path)

    # ---- toggle action ---------------------------------------------------

    def _fake_browser_with_items(self, items_by_id):
        seen_ops = []

        class FakeState:
            def __init__(s):
                s._items_by_id = dict(items_by_id)
                s._children = {}
                s._preview = {}

        invalidate_calls = []

        class FakeBrowser:
            def __init__(s):
                s._state = FakeState()
                s._preview_cursor_id = 'sentinel'
                s._needs_redraw = set()
            def update_data(s, ops):
                seen_ops.append(list(ops))
            def invalidate_preview(s, id_):
                invalidate_calls.append(id_)
            def all_items(s):
                return iter(list(s._state._items_by_id.values()))
            def drop_preview_cache(s, id_=None):
                pass

        class FakeCtx:
            def __init__(s):
                s._browser = FakeBrowser()
                s.browser = s._browser
                s.messages = []
            def message(s, m):
                s.messages.append(m)
            def all_items(s):
                return s._browser.all_items()
            def update_data(s, ops):
                return s._browser.update_data(ops)
            def drop_preview_cache(s, id_=None):
                return s._browser.drop_preview_cache(id_)

        return FakeCtx(), seen_ops

    def test_toggle_action_emits_mod_batch_for_loaded_items(self):
        # Toggling ON should mod every non-voice loaded item to
        # hidden=True; voice items and subagents stay hidden=False.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'uuid': 'a1',
             'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._scan_tree(path)
            voice = self.r.Item(id=f'{path}#0')
            voice.hidden = False
            tool = self.r.Item(id=f'{path}#1')
            tool.hidden = False
            sub = self.r.Item(id=f'{path}#agent:ABC')
            sub.hidden = False
            ctx, seen = self._fake_browser_with_items({
                voice.id: voice, tool.id: tool, sub.id: sub,
            })
            self.r._action_toggle_filter(ctx)
            self.assertTrue(self.r._FILTER_VOICE_ONLY)
            self.assertEqual(len(seen), 1)
            ops = seen[0]
            # Every op is a mod op (no removes / upserts).
            self.assertTrue(all(op[0] == 'mod' for op in ops),
                            f'non-mod ops in batch: {ops}')
            by_id = {op[1]: op for op in ops}
            # Only the tool row changes (voice and subagent stay visible).
            self.assertIn(tool.id, by_id)
            self.assertNotIn(voice.id, by_id)
            self.assertNotIn(sub.id, by_id)
            self.assertEqual(by_id[tool.id][3].get('hidden'), True)
        finally:
            os.unlink(path)

    def test_toggle_action_round_trip_restores_visibility(self):
        # Toggle ON then OFF: every item ends up with hidden=False.
        path = self._write_jsonl([
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._scan_tree(path)
            tool = self.r.Item(id=f'{path}#0')
            tool.hidden = False
            ctx, seen = self._fake_browser_with_items({tool.id: tool})
            self.r._action_toggle_filter(ctx)
            self.assertTrue(self.r._FILTER_VOICE_ONLY)
            # Simulate the framework applying the first batch.
            for op in seen[0]:
                if op[0] == 'mod' and op[1] == tool.id:
                    tool.hidden = op[3].get('hidden', False)
            self.r._action_toggle_filter(ctx)
            self.assertFalse(self.r._FILTER_VOICE_ONLY)
            self.assertEqual(len(seen), 2)
            # Second batch flips the tool back to hidden=False.
            mods = {op[1]: op[3] for op in seen[1] if op[0] == 'mod'}
            self.assertIn(tool.id, mods)
            self.assertEqual(mods[tool.id].get('hidden'), False)
        finally:
            os.unlink(path)

    def test_toggle_action_drops_preview_cache(self):
        # Umbrella previews compose from non-hidden children. After a
        # filter flip, every cached preview is potentially stale —
        # the recipe drops the whole preview cache via the public
        # ``drop_preview_cache()`` API, which the framework guarantees
        # will also re-kick the cursor preview and signal a redraw.
        ctx, _ = self._fake_browser_with_items({})
        # Replace the fake's drop_preview_cache with a spy so we can
        # confirm the recipe called it.
        drops = []
        ctx._browser.drop_preview_cache = lambda id_=None: drops.append(id_)
        self.r._action_toggle_filter(ctx)
        self.assertEqual(drops, [None],
                         'recipe should drop the whole preview cache '
                         'so the framework re-fetches the cursor view')

    def test_toggle_action_no_remove_ops(self):
        # Toggle never emits remove ops — visibility is non-destructive.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'uuid': 'a1',
             'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._scan_tree(path)
            it0 = self.r.Item(id=f'{path}#0'); it0.hidden = False
            it1 = self.r.Item(id=f'{path}#1'); it1.hidden = False
            ctx, seen = self._fake_browser_with_items({
                it0.id: it0, it1.id: it1,
            })
            self.r._action_toggle_filter(ctx)
            ops = seen[0]
            self.assertFalse(
                any(op[0] in ('remove', 'clear_children') for op in ops),
                f'destructive op leaked into toggle batch: {ops}',
            )
        finally:
            os.unlink(path)

    # ---- live tail under filter -----------------------------------------

    def test_push_flat_inserts_under_filter_marks_hidden(self):
        # New flat-mode rows under the filter pick up the right
        # ``hidden`` flag at construction.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'one'}},
        ])
        try:
            td = self.r._scan_tree(path)
            # In real usage the tail step appends to td.records before
            # _push_flat_inserts runs; emulate that so _passes_filter
            # has data for the new lines.
            new_records = [
                (1, {'type': 'user', 'uuid': 'u2',
                     'message': {'role': 'user', 'content': 'two'}}),
                (2, {'type': 'assistant', 'uuid': 'a1',
                     'message': {'role': 'assistant', 'content': [
                         {'type': 'tool_use', 'id': 't1',
                          'name': 'X', 'input': {}},
                     ]}}),
            ]
            for _n, rec in new_records:
                td.records.append(rec)
            seen_ops = []

            class FakeState:
                def __init__(s):
                    s._children = {path: []}
                    s._items_by_id = {}
            class FakeBrowser:
                def __init__(s):
                    s._state = FakeState()
                def update_data(s, ops):
                    seen_ops.append(list(ops))
                def post(s, fn):
                    pass
                def get_item(s, id_):
                    return s._state._items_by_id.get(id_)
                def cached_children(s, parent_id):
                    kids = s._state._children.get(parent_id)
                    return None if kids is None else list(kids)

            self.r._FILTER_VOICE_ONLY = True
            self.r._push_flat_inserts(FakeBrowser(), path, new_records)
            self.assertEqual(len(seen_ops), 1)
            ops = seen_ops[0]
            by_id = {op[1]: op[3] for op in ops if op[0] == 'upsert'}
            self.assertEqual(by_id[f'{path}#1'].get('hidden'), False,
                             'voice row should arrive visible')
            self.assertEqual(by_id[f'{path}#2'].get('hidden'), True,
                             'tool_use row should arrive hidden')
        finally:
            os.unlink(path)

    def test_push_tail_diffs_reveals_voice_bearing_parent(self):
        # Live tail under filter: a previously-hidden umbrella whose
        # predicate now passes gets a ``mod(parent_id, hidden=False)``
        # before its new child upserts. We fabricate the transition by
        # setting up a hidden span umbrella in the framework's index,
        # then call _push_tail_diffs with that span id marked dirty
        # after pre-loading the td with a voice record in the span.
        path = '/tmp/fake-tail.jsonl'
        td = self.r._TreeData()
        # Span at line 0 now contains a voice record (transition).
        td.span_records[0] = [
            {'type': 'user', 'uuid': 'u',
             'message': {'role': 'user', 'content': 'hi'}},
        ]
        # Stub for get_children: returns the span's records as items.
        td.records = [td.span_records[0][0]]
        self.r._TREE_CACHE[path] = td
        try:
            span_id = f'{path}#span:0'
            span_item = self.r.Item(id=span_id)
            span_item.hidden = True   # was hidden before the voice arrival

            seen_ops = []

            class FakeState:
                def __init__(s):
                    s._items_by_id = {span_id: span_item}
                    s._children = {}     # span not expanded → no children
            class FakeBrowser:
                def __init__(s):
                    s._state = FakeState()
                def update_data(s, ops):
                    seen_ops.append(list(ops))
                def get_item(s, id_):
                    return s._state._items_by_id.get(id_)
                def cached_children(s, parent_id):
                    kids = s._state._children.get(parent_id)
                    return None if kids is None else list(kids)

            self.r._FILTER_VOICE_ONLY = True
            self.r._push_tail_diffs(FakeBrowser(), [span_id])
            self.assertEqual(len(seen_ops), 1)
            mods = [op for op in seen_ops[0] if op[0] == 'mod']
            self.assertEqual(len(mods), 1)
            self.assertEqual(mods[0][1], span_id)
            self.assertEqual(mods[0][3].get('hidden'), False)
        finally:
            self.r._TREE_CACHE.pop(path, None)

    # ---- session row preview goes through umbrella cascade -------------

    def test_session_preview_uses_umbrella_cascade(self):
        # The session row's preview is built the same way as any other
        # umbrella: get_children + concatenated child bodies. Under the
        # voice-only filter, non-voice children are skipped because
        # they're hidden=True at construction.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'PROBE_VOICE_TEXT'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'PROBE_TOOL_NAME',
                  'input': {'cmd': 'ls'}},
             ]}},
        ])
        try:
            self.r._FILTER_VOICE_ONLY = False
            full = self.r.get_preview(path)
            self.assertIn('PROBE_VOICE_TEXT', full)
            self.assertIn('PROBE_TOOL_NAME', full,
                          'without filter the tool body should appear')
            # Clear the tree cache so item builders re-read the filter.
            self.r._TREE_CACHE.clear()
            self.r._FILTER_VOICE_ONLY = True
            filtered = self.r.get_preview(path)
            self.assertIn('PROBE_VOICE_TEXT', filtered)
            self.assertNotIn('PROBE_TOOL_NAME', filtered,
                             'with filter the tool body must be hidden')
        finally:
            os.unlink(path)

    # ---- preview respects hidden ----------------------------------------

    def test_umbrella_preview_skips_hidden_children(self):
        # A ``#prompt:`` preview composes from its non-hidden children
        # only. Under voice-only, a tool umbrella with no voice content
        # is hidden and should not contribute to the prompt's preview.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'ask something'}},
            {'type': 'assistant', 'uuid': 'a1',
             'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1',
                  'name': 'Bash', 'input': {'command': 'ls /secret'}},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'parentUuid': 'a1',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1',
                  'content': 'machinery noise'},
             ]}},
        ])
        try:
            self.r._scan_tree(path)
            self.r._FILTER_VOICE_ONLY = True
            preview_filtered = self.r._preview_umbrella(f'{path}#prompt:0')
            # Without the filter the tool's machinery would appear.
            self.r._FILTER_VOICE_ONLY = False
            preview_full = self.r._preview_umbrella(f'{path}#prompt:0')
            # Sanity: full preview contains the tool's input.
            self.assertIn('ls /secret', preview_full)
            # Filtered preview drops the hidden tool umbrella entirely.
            self.assertNotIn('ls /secret', preview_filtered)
            self.assertNotIn('machinery noise', preview_filtered)
            # And it still contains the user prompt body.
            self.assertIn('ask something', preview_filtered)
        finally:
            os.unlink(path)

    # ---- CLI + help -----------------------------------------------------

    def test_help_text_mentions_h_hotkey(self):
        self.assertIn(' h ', self.r._HELP_INTRO_TMPL,
                      'help intro should list the h hotkey')
        self.assertIn('--show-all', self.r._HELP_INTRO_TMPL)
        self.assertIn('--no-show-all', self.r._HELP_INTRO_TMPL)

    def test_h_action_registered(self):
        # Sanity: the recipe registers an 'h' action with the expected
        # handler. We can't run the full main() under unit test (it
        # touches argv / stdin) — inspect the source for the binding.
        with open(_RECIPE) as f:
            source = f.read()
        self.assertIn("Action('h',", source)
        self.assertIn('_action_toggle_filter', source)


# ---- #424: composer ↔ framework cache integration ----------------------
#
# These tests exercise the three new behaviors added to
# ``_collect_umbrella_preview``:
#   * children flow through ``_BROWSER.cached_children`` (no duplicate
#     ``get_children`` calls after the first composition),
#   * absent children are eager-pushed via one batched
#     ``_BROWSER.update_data(ops)`` call per top-level invocation,
#   * each leaf's rendered body is cached on ``Item.preview`` via a
#     single main-thread ``post`` callback.
#
# The recipe is unit-testable against a fake Browser that captures
# ``cached_children`` reads, ``update_data`` op batches, and
# main-thread posts. The fake mirrors the framework's threading
# contract: every method is callable from any thread; ``post``-ed
# callables run inline (we drain them at the test's discretion via
# ``flush()``), and ``update_data`` mirrors the real apply behaviour
# enough that subsequent reads see the upserts.


class _FakeBrowser:
    """In-process stand-in for ``Browser`` used by #424 tests.

    Tracks every interesting call:
      * ``cached_children_calls`` — ids the composer asked about.
      * ``update_data_calls`` — list of op-list batches submitted.
      * ``posted`` — pending main-thread callables (drained by
        ``flush()``).
      * ``items_by_id`` / ``_children_cache`` — minimal storage so
        the second pass can observe the eager-push effect.

    Threading model: synchronous. ``update_data`` and ``post`` both
    queue work onto ``posted``; the test drives it explicitly via
    ``flush()``.
    """

    def __init__(self):
        self.cached_children_calls = []
        self.update_data_calls = []
        self.posted = []
        self.items_by_id = {}
        self._children_cache = {}

    def cached_children(self, parent_id):
        self.cached_children_calls.append(parent_id)
        entry = self._children_cache.get(parent_id)
        if entry is None:
            return None
        return list(entry)

    def get_cached_preview(self, id_):
        item = self.items_by_id.get(id_)
        return getattr(item, 'preview', None) if item is not None else None

    def update_data(self, ops):
        ops_list = list(ops)
        self.update_data_calls.append(ops_list)

        def _apply(ops_list=ops_list):
            for op in ops_list:
                kind = op[0]
                if kind != 'upsert':
                    continue
                id_ = op[1]
                parent_id = op[2]
                fields = op[3]
                if id_ in self.items_by_id:
                    item = self.items_by_id[id_]
                    for k, v in fields.items():
                        setattr(item, k, v)
                else:
                    item = _RecordingItem(id_, **fields)
                    self.items_by_id[id_] = item
                kids = self._children_cache.setdefault(parent_id, [])
                if item not in kids:
                    kids.append(item)
        self.posted.append(_apply)

    def post(self, fn):
        self.posted.append(fn)

    def set_preview(self, id_, text):
        """Mirror the framework's #431 behavior: queue a main-thread
        write that lands ``item.preview = text`` on the next flush.
        Like the real ``Browser.set_preview``, this no-ops silently
        when ``id_`` is not in ``items_by_id`` at apply time.
        """
        if text is None:
            text = ''

        def _apply(id_=id_, text=text):
            item = self.items_by_id.get(id_)
            if item is None:
                return
            item.preview = text
            item.preview_render = None
        self.posted.append(_apply)

    def flush(self):
        """Drain queued main-thread work, FIFO."""
        while self.posted:
            fn = self.posted.pop(0)
            fn()


class _RecordingItem:
    """Mimic ``Item``'s relevant fields without pulling in the framework.

    ``hidden`` defaults to False (matches Item default). ``preview`` is
    the per-Item preview cache the framework reads via
    ``get_cached_preview``. Extra kwargs land as attributes (mirrors
    ``Item.__init__`` + ``setattr`` extras).
    """

    def __init__(self, id_, **fields):
        self.id = id_
        self.preview = None
        self.preview_render = None
        self.hidden = False
        for k, v in fields.items():
            setattr(self, k, v)


class TestUmbrellaPreviewCacheIntegration(unittest.TestCase):
    """#424: cached_children-driven reads + eager push + leaf preview cache."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self._saved_BROWSER = self.r._BROWSER
        self._saved_TREE_MODE = self.r._TREE_MODE
        self.r._TREE_MODE = True

    def tearDown(self):
        self.r._BROWSER = self._saved_BROWSER
        self.r._TREE_MODE = self._saved_TREE_MODE
        # Drop the recipe-level tree cache so each test gets a fresh
        # scan of its synthetic .jsonl.
        self.r._TREE_CACHE.clear()

    def _write_jsonl(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def _instrument_get_children(self):
        """Wrap ``get_children`` to record every call. Returns the counter
        and a teardown callable to restore the original."""
        counter = {'calls': []}
        original = self.r.get_children

        def _wrapped(item_id, *, reload=False):
            counter['calls'].append(item_id)
            return original(item_id, reload=reload)

        self.r.get_children = _wrapped
        return counter, (lambda: setattr(self.r, 'get_children', original))

    def _make_three_record_session(self):
        # Turn root → assistant tool_use → user tool_result. Yields a
        # ``<prompt>`` umbrella with a nested ``<tool>`` umbrella.
        return self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'PROBE_USER'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'PROBE_BASH'}},
             ]}},
            {'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1',
                  'content': 'PROBE_OUTPUT'},
             ]}},
        ])

    def test_no_browser_falls_back_to_get_children(self):
        # Smoke: with ``_BROWSER=None`` (e.g. unit-test harness without
        # a real Browser) the composer still works — it just doesn't
        # eager-push or leaf-cache. Verifies graceful degradation.
        self.r._BROWSER = None
        path = self._make_three_record_session()
        try:
            out = self.r._preview_umbrella(f'{path}#prompt:0')
            self.assertIn('PROBE_USER', out)
            self.assertIn('PROBE_BASH', out)
            self.assertIn('PROBE_OUTPUT', out)
        finally:
            os.unlink(path)

    def test_composer_does_not_double_call_get_children(self):
        # First composition: ``get_children`` is called for the prompt
        # umbrella and the nested tool umbrella (one each).
        # Second composition: cache hits for both — no new calls.
        self.r._BROWSER = _FakeBrowser()
        path = self._make_three_record_session()
        counter, restore = self._instrument_get_children()
        try:
            self.r._preview_umbrella(f'{path}#prompt:0')
            self.r._BROWSER.flush()
            first_pass_calls = list(counter['calls'])
            # First pass must have fetched both umbrellas.
            self.assertIn(f'{path}#prompt:0', first_pass_calls)
            self.assertIn(f'{path}#tool:1', first_pass_calls)

            self.r._preview_umbrella(f'{path}#prompt:0')
            self.r._BROWSER.flush()
            # No additional ``get_children`` calls — cache served
            # every read.
            self.assertEqual(
                len(counter['calls']), len(first_pass_calls),
                f'second pass should hit cache; saw new calls: '
                f'{counter["calls"][len(first_pass_calls):]}',
            )
        finally:
            restore()
            os.unlink(path)

    def test_leaf_previews_are_populated(self):
        # After ``_preview_umbrella`` runs, every leaf child of the
        # umbrella should have ``Item.preview`` populated. We check by
        # consulting the FakeBrowser's items_by_id index after the
        # post queue drains.
        self.r._BROWSER = _FakeBrowser()
        path = self._make_three_record_session()
        try:
            self.r._preview_umbrella(f'{path}#prompt:0')
            self.r._BROWSER.flush()
            # The leaf record ids are ``<path>#0``, ``<path>#1``,
            # ``<path>#2``. All three should have a non-empty
            # ``preview`` slot. Umbrellas (#prompt:, #tool:) are not
            # leaves and won't be populated by this code path — the
            # framework's worker handles those.
            for n in (0, 1, 2):
                cid = f'{path}#{n}'
                item = self.r._BROWSER.items_by_id.get(cid)
                self.assertIsNotNone(item, f'leaf {cid} not in index')
                self.assertTrue(
                    item.preview,
                    f'leaf {cid}.preview should be populated; '
                    f'got {item.preview!r}',
                )
        finally:
            os.unlink(path)

    def test_eager_push_one_update_data_call_per_invocation(self):
        # The composer should accumulate upserts across the recursive
        # descent and flush them in exactly one ``update_data`` call.
        self.r._BROWSER = _FakeBrowser()
        path = self._make_three_record_session()
        try:
            self.r._preview_umbrella(f'{path}#prompt:0')
            self.assertEqual(
                len(self.r._BROWSER.update_data_calls), 1,
                'composer should flush exactly one update_data batch '
                'per top-level invocation; got '
                f'{len(self.r._BROWSER.update_data_calls)}',
            )
            # The batch should contain at least one upsert (the leaves
            # under the prompt umbrella).
            ops = self.r._BROWSER.update_data_calls[0]
            upserts = [op for op in ops if op[0] == 'upsert']
            self.assertGreater(
                len(upserts), 0,
                'expected at least one upsert op for the leaves',
            )
        finally:
            os.unlink(path)

    def test_tail_tick_only_re_renders_new_records(self):
        # Compose once. Append a new leaf via a synthetic upsert. Wrap
        # ``_render_record_with_rule`` to count calls. The second
        # composition should re-render only the new leaf (existing
        # ones are cache hits).
        self.r._BROWSER = _FakeBrowser()
        path = self._make_three_record_session()

        render_calls = []
        original_render = self.r._render_record_with_rule

        def _counting_render(p, n):
            render_calls.append((p, n))
            return original_render(p, n)

        self.r._render_record_with_rule = _counting_render
        try:
            self.r._preview_umbrella(f'{path}#prompt:0')
            self.r._BROWSER.flush()
            calls_after_first = list(render_calls)
            # First pass: the three leaves all rendered.
            self.assertEqual(
                len(calls_after_first), 3,
                f'first pass should render 3 leaves; got '
                f'{calls_after_first}',
            )

            # Simulate a tail tick: append a new leaf to the cached
            # children list. (Real tail worker would do this via
            # ``update_data`` upsert; we shortcut by mutating the
            # fake's storage. The point of the test is to verify the
            # composer's cache behavior, not the tail worker's.)
            new_leaf = _RecordingItem(
                f'{path}#3',
                title='NEW',
                tag='user',
                hidden=False,
            )
            self.r._BROWSER.items_by_id[new_leaf.id] = new_leaf
            self.r._BROWSER._children_cache.setdefault(
                f'{path}#prompt:0', []
            ).append(new_leaf)

            # Now write the new record to disk and re-scan so
            # ``_render_record_with_rule`` can read it.
            import json as _json
            with open(path, 'a') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u3', 'parentUuid': 'a1',
                    'message': {'role': 'user',
                                'content': 'PROBE_NEW_RECORD'},
                }) + '\n')
            self.r._TREE_CACHE.pop(path, None)

            render_calls.clear()
            self.r._preview_umbrella(f'{path}#prompt:0')
            self.r._BROWSER.flush()
            # Second pass: only the new leaf re-renders. The three
            # existing leaves were cache hits.
            self.assertEqual(
                len(render_calls), 1,
                f'second pass should render only the new leaf; got '
                f'{render_calls}',
            )
            self.assertEqual(render_calls[0], (path, 3))
        finally:
            self.r._render_record_with_rule = original_render
            os.unlink(path)

    def test_collapse_and_re_expand_uses_cached_children(self):
        # The "collapse + re-expand" UX is mediated by the framework's
        # expand goal — but the relevant invariant for the composer is
        # that a second ``cached_children`` read on the same id hits
        # the framework's cache (so the user's first expand sees the
        # preview-populated children instantly). After ``_preview_umbrella``
        # runs once, ``cached_children`` for the umbrella should be
        # non-None.
        self.r._BROWSER = _FakeBrowser()
        path = self._make_three_record_session()
        try:
            self.r._preview_umbrella(f'{path}#prompt:0')
            self.r._BROWSER.flush()
            # First expand: the framework consults its cache via
            # cached_children. The recipe-side composer has populated
            # it; the framework would now skip the children-queue
            # fetch entirely.
            cached = self.r._BROWSER.cached_children(f'{path}#prompt:0')
            self.assertIsNotNone(
                cached, 'cached_children should be populated after '
                'preview composition',
            )
            self.assertGreater(
                len(cached), 0,
                'cached children list should be non-empty',
            )
            # Second preview pass: the composer reads from the cache,
            # never re-asks ``get_children`` for this id.
            counter, restore = self._instrument_get_children()
            try:
                self.r._preview_umbrella(f'{path}#prompt:0')
                self.r._BROWSER.flush()
                self.assertNotIn(
                    f'{path}#prompt:0', counter['calls'],
                    'second composition must not re-fetch '
                    'cached umbrella children',
                )
            finally:
                restore()
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
