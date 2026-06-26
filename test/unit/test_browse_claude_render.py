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


# ---- markdown id helpers (mirror the recipe's inline tuple construction) ----
#
# The recipe builds markdown ids inline as ``('md', anchor, chain, line)`` and
# References-umbrella ids as ``('refs', anchor, chain)`` (md_doc is codec-free).
# These mirror that construction so the tests build the same ids the recipe
# does without re-encoding any string codec.

def _md_id(anchor, chain=(), line_offset=None):
    """Build a markdown-node id ``('md', anchor, chain, line)``."""
    return ('md', anchor, tuple(chain), line_offset)


def _refs_id(doc):
    """Build a References-umbrella id ``('refs', anchor, chain)`` for a document.

    ``doc`` is either a message anchor ``('msg', jsonl, n)`` (message-level
    umbrella → empty chain) or an ``('md', anchor, chain, line)`` file-doc id
    (file-doc umbrella → that file's anchor + chain).
    """
    if isinstance(doc, tuple) and doc and doc[0] == 'md':
        return ('refs', doc[1], doc[2])
    return ('refs', doc, ())


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

    # ``set_preview_op`` is the preview-batch op constructor (#446) —
    # the umbrella composer folds leaf-preview writes into the same
    # ``update_data`` batch as the eager-push upserts. Stub it as a
    # tagged tuple so the recipe's ops list carries a recognisable
    # shape for the _FakeBrowser to interpret.
    mod.set_preview_op = lambda id_, text: ('set_preview', id_, text)

    # visible_items is used by the cursor-on-open ready-check
    # (_focus_latest_voice_when_ready). Tests don't exercise the
    # focus flow directly, so a no-op returning [] is fine.
    mod.visible_items = lambda state: []

    # ``recipe_argv`` drops the framework's ``--tty VALUE`` / ``--tty=VALUE``
    # flag from the recipe's positional scan (mirrors 040-state.py). Tests
    # patch ``sys.argv`` before driving ``main()``, so reading it here
    # matches what the recipe sees.
    def _recipe_argv(argv=None):
        if argv is None:
            argv = sys.argv[1:]
        out, skip_next = [], False
        for arg in argv:
            if skip_next:
                skip_next = False
                continue
            if arg == '--tty':
                skip_next = True
                continue
            if arg.startswith('--tty='):
                continue
            out.append(arg)
        return out
    mod.recipe_argv = _recipe_argv

    sys.modules['browse_tui'] = mod


def _load_recipe(force_color=True, *, with_md_doc=False):
    """Load (or reload) the recipe; returns the module.

    ``force_color`` controls whether ANSI constants are kept (True) or
    zeroed via ``NO_COLOR`` (False) — exercises both code paths.

    ``with_md_doc`` controls markdown availability. The recipe derives
    ``_MD_COLOR`` / ``_md_doc`` from whether ``md2ansi_lib`` / ``md_doc``
    are importable at load time. Other test modules (e.g. ``test_md_doc``)
    put ``recipes/`` on ``sys.path`` and import ``md2ansi_lib`` at
    collection, leaving them in ``sys.modules`` — so a plain load would
    pick up coloring non-deterministically by test order. The default
    (``False``) loads with ``recipes/`` off ``sys.path`` and those modules
    evicted, forcing the md-less baseline so the raw-markdown assertions
    are order-independent; ``_load_recipe_with_md_doc`` passes ``True`` to
    keep md2ansi / ``md_doc`` live for the colored-path tests.
    """
    _stub_browse_tui()
    saved_no_color = os.environ.get('NO_COLOR')
    saved_force_color = os.environ.get('FORCE_COLOR')
    saved_md_path = saved_md_mods = None
    if not with_md_doc:
        _recipes_dir = str(_RECIPE.parent)
        saved_md_path = list(sys.path)
        sys.path[:] = [p for p in sys.path if p != _recipes_dir]
        saved_md_mods = {m: sys.modules.pop(m, None)
                         for m in ('md2ansi_lib', 'md_doc')}
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
        if not with_md_doc:
            sys.path[:] = saved_md_path
            for _m, _v in saved_md_mods.items():
                if _v is not None:
                    sys.modules[_m] = _v


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

    def test_task_update_updated_fields_as_list(self):
        # Newer CC builds emit ``updatedFields`` as a list of names
        # rather than a {name: value} dict — render without crashing.
        out = self.r._render_user(self._user_with_tur({
            'taskId': '1', 'success': True,
            'statusChange': {'from': 'pending', 'to': 'completed'},
            'updatedFields': ['status'],
        }))
        self.assertIn('#1', out)
        self.assertIn('pending', out)
        self.assertIn('completed', out)
        self.assertIn('status', out)

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


class TestTurnOffset(unittest.TestCase):
    """``_ts_offset`` / ``_turn_root_ts`` + the chrome ``offset`` row.

    The offset is the elapsed time of a message since the user request that
    opens its turn. It rides in the chrome footer right after ``timestamp``
    and is suppressed for the turn root itself and for out-of-turn records.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_ts_offset_formats(self):
        f = self.r._ts_offset
        base = '2026-06-09T00:00:00.000Z'
        self.assertEqual(f(base, '2026-06-09T00:00:00.500Z'), '+0.5s')
        self.assertEqual(f(base, '2026-06-09T00:01:30.000Z'), '+1m30s')
        self.assertEqual(f(base, '2026-06-09T00:10:05.000Z'), '+10m05s')
        self.assertEqual(f(base, '2026-06-09T01:02:05.000Z'), '+1h02m')

    def test_ts_offset_missing_or_unparseable_is_none(self):
        f = self.r._ts_offset
        ok = '2026-06-09T00:00:00.000Z'
        self.assertIsNone(f(None, ok))
        self.assertIsNone(f(ok, ''))
        self.assertIsNone(f('garbage', 'garbage'))

    def _write(self, records):
        import json as _json
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl',
                                         delete=False) as f:
            for r in records:
                f.write(_json.dumps(r) + '\n')
            return f.name

    def _records(self):
        # line 0: out-of-turn metadata (folds into a <system> span)
        # line 1: user turn root          (ts T0)
        # line 2: plain assistant reply   (ts T0 + 90s, direct turn member)
        return [
            {'type': 'summary', 'summary': 'pre-turn'},
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'do the thing'},
             'timestamp': '2026-06-09T00:00:00.000Z'},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'model': 'm',
                         'stop_reason': 'end_turn',
                         'content': [{'type': 'text', 'text': 'done'}]},
             'timestamp': '2026-06-09T00:01:30.000Z'},
        ]

    def test_turn_root_ts_resolution(self):
        path = self._write(self._records())
        try:
            # Turn member resolves to the opening user request's timestamp.
            self.assertEqual(self.r._turn_root_ts(path, 2),
                             '2026-06-09T00:00:00.000Z')
            # The turn root itself has no offset (it IS the request).
            self.assertIsNone(self.r._turn_root_ts(path, 1))
            # Out-of-turn span member has no umbrella request.
            self.assertIsNone(self.r._turn_root_ts(path, 0))
        finally:
            os.unlink(path)

    def test_chrome_offset_row_after_timestamp(self):
        import re as _re
        path = self._write(self._records())
        sgr = _re.compile(r'\x1b\[[0-9;]*m')
        try:
            out = sgr.sub('', self.r._preview_message(path, 2))
            lines = out.split('\n')
            ts_i = next(i for i, ln in enumerate(lines)
                        if ln.lstrip().startswith('timestamp'))
            self.assertTrue(lines[ts_i + 1].lstrip().startswith('offset'),
                            f'offset must follow timestamp: {lines}')
            self.assertIn('+1m30s', lines[ts_i + 1])
            # Turn root preview carries no offset row.
            root_out = sgr.sub('', self.r._preview_message(path, 1))
            self.assertNotIn('offset', root_out)
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


class TestDetailLevelArg(unittest.TestCase):
    """``--detail`` parses numbers + word aliases; rejects bad values.

    ``main()`` touches argv / stdin / Browser, so we can't run it
    headless — exercise the two pieces it composes instead:
    ``_parse_detail_level`` (the conv) and its use through
    ``_pop_value`` (the same primitive ``-n`` / ``--pid`` use).
    """

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

    def test_parse_numbers(self):
        for raw, lvl in (('1', 1), ('2', 2), ('3', 3), ('4', 4)):
            with self.subTest(raw=raw):
                self.assertEqual(self.r._parse_detail_level(raw), lvl)

    def test_parse_word_aliases(self):
        for raw, lvl in (('voice', 1), ('tools', 2),
                         ('detailed', 3), ('all', 4)):
            with self.subTest(raw=raw):
                self.assertEqual(self.r._parse_detail_level(raw), lvl)

    def test_parse_aliases_case_insensitive_and_trimmed(self):
        self.assertEqual(self.r._parse_detail_level('VOICE'), 1)
        self.assertEqual(self.r._parse_detail_level('  All '), 4)

    def test_parse_rejects_bad_values(self):
        for bad in ('0', '5', '', 'foo', 'vo', '-1', '3.0'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.r._parse_detail_level(bad)

    def test_pop_value_space_form(self):
        saved = self._with_argv(['--detail', '3'])
        try:
            self.assertEqual(
                self.r._pop_value('--detail', self.r._parse_detail_level), 3)
            import sys as _sys
            self.assertEqual(_sys.argv, ['browse-claude'])
        finally:
            self._restore_argv(saved)

    def test_pop_value_equals_form_alias(self):
        saved = self._with_argv(['--detail=all'])
        try:
            self.assertEqual(
                self.r._pop_value('--detail', self.r._parse_detail_level), 4)
        finally:
            self._restore_argv(saved)

    def test_pop_value_invalid_is_false_sentinel(self):
        # A bad value pops as ``False`` so ``main`` can emit the usage
        # error (mirrors ``--pid banana``).
        saved = self._with_argv(['--detail', 'bogus'])
        try:
            self.assertIs(
                self.r._pop_value('--detail', self.r._parse_detail_level),
                False)
        finally:
            self._restore_argv(saved)

    def test_pop_value_absent_is_none(self):
        saved = self._with_argv(['--other', 'x'])
        try:
            self.assertIsNone(
                self.r._pop_value('--detail', self.r._parse_detail_level))
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


class TestRealProjectPath(unittest.TestCase):
    """``_real_project_path`` / ``_session_cwd_and_root`` (#659).

    The accurate replacements for the lossy name-decode: derive the path
    from a session's recorded ``cwd`` so a genuine hyphen survives, and
    expose the ``(cwd, project_root)`` anchors the reference resolver
    needs. State touched is HOME plus the byte-regex cwd cache; both are
    saved/cleared in setUp/tearDown so the tests pass identically in
    isolation and under full discover (no module-global pollution).

    Loaded with ``md_doc`` live (``_load_recipe_with_md_doc``): the git-root
    walk-up moved into ``md_doc`` (``find_git_root``), and
    ``_session_cwd_and_root`` — a markdown-reference-resolution anchor whose
    only callers are md-gated — now reaches it via ``_md_doc``, so these tests
    need the module present.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe_with_md_doc()

    def setUp(self):
        self._saved_home = os.environ.get('HOME')
        os.environ['HOME'] = '/home/u'
        # cwd scans are cached by (path, size, mtime); clearing keeps a
        # stale tmp-path entry from a prior test out of our way.
        self.r._SESSION_CWDS_CACHE.clear()

    def tearDown(self):
        self.r._SESSION_CWDS_CACHE.clear()
        if self._saved_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self._saved_home

    def _write_session(self, project_dir, name, rec):
        import json as _json
        path = os.path.join(project_dir, name)
        with open(path, 'w') as f:
            f.write(_json.dumps(rec) + '\n')
        return path

    def test_real_hyphen_preserved_and_home_collapsed(self):
        # A cwd with a *genuine* hyphen the lossy decode would mangle.
        import tempfile
        cwd = '/home/u/browse-tui'
        with tempfile.TemporaryDirectory() as tmp:
            enc = self.r._encode_project_path(cwd)   # -home-u-browse-tui
            proj = os.path.join(tmp, enc)
            os.makedirs(proj)
            self._write_session(proj, 's1.jsonl', {'type': 'user', 'cwd': cwd})
            # The lossy decoder mangles the hyphen; the real path keeps it.
            self.assertEqual(self.r._decode_project_path(enc), '~/browse/tui')
            self.assertEqual(self.r._real_project_path(proj), '~/browse-tui')

    def test_prefers_encoding_match_over_extra_cwds(self):
        # A worktree session records multiple cwds; we pin the one that
        # round-trips back to this project dir, regardless of scan order.
        import tempfile
        cwd = '/home/u/proj-x'
        other = '/home/u/proj-x/.worktrees/wt'
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, self.r._encode_project_path(cwd))
            os.makedirs(proj)
            # Two records, the non-canonical cwd first in the file.
            import json as _json
            path = os.path.join(proj, 's1.jsonl')
            with open(path, 'w') as f:
                f.write(_json.dumps({'cwd': other}) + '\n')
                f.write(_json.dumps({'cwd': cwd}) + '\n')
            self.assertEqual(self.r._real_project_cwd(proj), cwd)
            self.assertEqual(self.r._real_project_path(proj), '~/proj-x')

    def test_falls_back_to_lossy_decode_without_cwd(self):
        # No readable cwd (empty dir / records without cwd) → lossy decode.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            enc = '-home-u-no-cwd-here'
            proj = os.path.join(tmp, enc)
            os.makedirs(proj)
            # Empty project dir.
            self.assertIsNone(self.r._real_project_cwd(proj))
            self.assertEqual(
                self.r._real_project_path(proj),
                self.r._decode_project_path(enc),
            )
            # Same fallback when a session exists but carries no cwd field.
            self._write_session(proj, 's1.jsonl', {'type': 'summary'})
            self.assertIsNone(self.r._real_project_cwd(proj))
            self.assertEqual(
                self.r._real_project_path(proj),
                self.r._decode_project_path(enc),
            )

    def test_find_git_root_returns_ancestor(self):
        # A `.git` directory anywhere up the tree is the root. The walk-up
        # itself moved into md_doc (``find_git_root``); the recipe consumes it
        # via ``_md_doc`` (the resolver-anchor path).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, 'repo')
            deep = os.path.join(repo, 'a', 'b', 'c')
            os.makedirs(deep)
            os.makedirs(os.path.join(repo, '.git'))
            self.assertEqual(self.r._md_doc.find_git_root(deep), repo)
            # A `.git` *file* (worktree/submodule layout) counts too.
            wt = os.path.join(tmp, 'wt')
            os.makedirs(wt)
            open(os.path.join(wt, '.git'), 'w').close()
            self.assertEqual(self.r._md_doc.find_git_root(wt), wt)

    def test_find_git_root_none_when_absent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, 'plain', 'deep')
            os.makedirs(plain)
            self.assertIsNone(self.r._md_doc.find_git_root(plain))
            self.assertIsNone(self.r._md_doc.find_git_root(''))

    def test_session_cwd_and_root_uses_git_ancestor(self):
        # cwd from the records; project_root = the `.git` ancestor.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.path.join(tmp, 'repo', 'sub-dir')
            os.makedirs(cwd)
            os.makedirs(os.path.join(tmp, 'repo', '.git'))
            proj = os.path.join(tmp, self.r._encode_project_path(cwd))
            os.makedirs(proj)
            sess = self._write_session(proj, 's1.jsonl', {'cwd': cwd})
            got_cwd, got_root = self.r._session_cwd_and_root(sess)
            self.assertEqual(got_cwd, cwd)
            self.assertEqual(got_root, os.path.join(tmp, 'repo'))

    def test_session_cwd_and_root_falls_back_to_cwd(self):
        # No `.git` above cwd → project_root is the cwd itself.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.path.join(tmp, 'plain-proj')
            os.makedirs(cwd)
            proj = os.path.join(tmp, self.r._encode_project_path(cwd))
            os.makedirs(proj)
            sess = self._write_session(proj, 's1.jsonl', {'cwd': cwd})
            self.assertEqual(
                self.r._session_cwd_and_root(sess), (cwd, cwd),
            )

    def test_session_cwd_and_root_no_cwd_returns_real_path(self):
        # Unreadable cwd → (None, expanded lossy project dir), an absolute
        # path the resolver can join against (no leftover ``~``).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            enc = '-home-u-ghost'
            proj = os.path.join(tmp, enc)
            os.makedirs(proj)
            sess = os.path.join(proj, 's1.jsonl')
            open(sess, 'w').close()
            got_cwd, got_root = self.r._session_cwd_and_root(sess)
            self.assertIsNone(got_cwd)
            self.assertEqual(got_root, '/home/u/ghost')
            self.assertFalse(got_root.startswith('~'))


class TestMessageOrder(unittest.TestCase):
    """``_list_messages`` returns rows in **chronological** order (#475) —
    matches tree-mode ordering so the t-toggle doesn't flip the
    conversation. ``limit=0`` (the new default) disables truncation
    entirely; explicit ``limit=N`` keeps the latest N with a marker
    pinned at the **top** of the list (representing the older entries
    that came before the kept window).
    """

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

    def test_chronological_order(self):
        path = self._write([
            {'type': 'user', 'message': {'role': 'user', 'content': 'one'}},
            {'type': 'user', 'message': {'role': 'user', 'content': 'two'}},
            {'type': 'user', 'message': {'role': 'user', 'content': 'three'}},
        ])
        try:
            items = self.r._list_messages(path)
            titles = [it.title for it in items]
            self.assertIn('one',   titles[0])
            self.assertIn('two',   titles[1])
            self.assertIn('three', titles[2])
        finally:
            os.unlink(path)

    def test_truncation_marker_at_start(self):
        path = self._write([
            {'type': 'user', 'message': {'role': 'user', 'content': f'msg{i}'}}
            for i in range(10)
        ])
        try:
            items = self.r._list_messages(path, limit=3)
            # One truncation marker at the START + three real items.
            self.assertEqual(len(items), 4)
            self.assertIn('older entries hidden', items[0].title)
            # Real items are the *latest* three in chronological order.
            self.assertIn('msg7', items[1].title)
            self.assertIn('msg8', items[2].title)
            self.assertIn('msg9', items[3].title)
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

    def test_no_marker_when_limit_is_zero(self):
        # limit=0 means "no cap" — even for files with many records,
        # the marker should not appear.
        path = self._write([
            {'type': 'user', 'message': {'role': 'user', 'content': f'msg{i}'}}
            for i in range(50)
        ])
        try:
            items = self.r._list_messages(path, limit=0)
            self.assertEqual(len(items), 50)
            for it in items:
                self.assertNotIn('older entries hidden', it.title)
        finally:
            os.unlink(path)


class TestTreeChildrenPreview(unittest.TestCase):
    """``_preview_umbrella`` concatenates direct-children bodies."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        # These tests assert the full "show everything" baseline (incl.
        # non-voice tool rows), so pin the voice-only filter OFF — it's
        # on by default and would otherwise hide that content.
        self._saved_FILTER = self.r._DETAIL_LEVEL
        self.r._DETAIL_LEVEL = 4

    def tearDown(self):
        self.r._DETAIL_LEVEL = self._saved_FILTER

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
                out = ''.join(self.r._preview_umbrella(('prompt', path, 0)))
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
                out = ''.join(self.r._preview_umbrella(('prompt', path, 0)))
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
                out = ''.join(self.r._preview_umbrella(('prompt', path, 0)))
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
                out = ''.join(self.r._preview_umbrella(('tool', path, 1)))
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
            ancestors = self.r._ancestor_ids_for(('msg', agent_path, 1))
            # First ancestor (root) should be the outer subagent group
            # in the parent session.
            self.assertTrue(ancestors,
                            'expected at least one ancestor')
            self.assertEqual(ancestors[0],
                             ('agent', sess_path, 'AGENT01'))
            # Direct parent is now the ``<prompt>`` umbrella wrapping
            # the subagent's turn root, NOT the turn root leaf itself.
            self.assertEqual(ancestors[-1], ('prompt', agent_path, 0))
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
            ancestors = self.r._ancestor_ids_for(('msg', sess_path, 1))
            # Direct parent is the ``<prompt>`` umbrella; no outer
            # subagent group.
            self.assertEqual(ancestors, [('prompt', sess_path, 0)])

    def test_full_chain_crosses_file_boundary(self):
        # Parent session: u1 (turn root) → a1 (Task tool_use) → u2 (tool_result).
        # Subagent: su1 (turn root) → sa1 (assistant text).
        # Ancestors of sa1 (deepest, line 1 in subagent) should walk:
        #   parent: <prompt> umbrella @ line 0 (wraps u1)
        #          → <tool:Task> umbrella @ line 1 (wraps a1)
        #          → <subagent> group (('agent', sess, 'AGENT01'))
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

            chain = self.r._ancestor_ids_for(('msg', agent_path, 1))
            # Expect (root → leaf), all umbrella ids:
            #   parent's <prompt> umbrella (line 0, wraps u1)
            #   parent's <tool:Task> umbrella (line 1, wraps a1)
            #   subagent group ('agent', sess, 'AGENT01')
            #   subagent's <prompt> umbrella (line 0, wraps su1)
            self.assertEqual(chain, [
                ('prompt', sess_path, 0),
                ('tool', sess_path, 1),
                ('agent', sess_path, 'AGENT01'),
                ('prompt', agent_path, 0),
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
                ('agent', sess_path, 'ABC'),
            )
            # Non-subagent paths return None.
            self.assertIsNone(self.r._outer_subagent_group_id(sess_path))
            self.assertIsNone(self.r._outer_subagent_group_id('/nope'))

    def test_outer_subagent_group_id_relocated_crosses_project_dirs(self):
        """Worktree-relocated subagent → real session in another project dir.

        Once a session enters a git worktree, Claude Code stores its
        subagents under the cwd-derived project dir, NOT next to the
        session ``.jsonl`` (which stays in the project dir it was born
        in). So the co-located ``<sid>.jsonl`` sibling of the subagent's
        ``<sid>`` dir is absent, and the helper must locate the real
        session by searching ``<CLAUDE_ROOT>/*/<sid>.jsonl``. Before the
        fix it only checked the sibling and returned ``None`` — which
        broke the ``t``-toggle ancestry walk from inside the subagent.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, '.claude', 'projects')
            # Session lives under its original project dir.
            sess_proj = os.path.join(root, '-home-test-tree')
            os.makedirs(sess_proj)
            sess_path = os.path.join(sess_proj, 'tree-sess.jsonl')
            with open(sess_path, 'w') as f:
                f.write('{}\n')
            # Subagent lives under the DISTINCT cwd-derived project dir.
            sub_dir = os.path.join(
                root, '-home-test-tree--worktrees-wt',
                'tree-sess', 'subagents')
            os.makedirs(sub_dir)
            agent_path = os.path.join(sub_dir, 'agent-AGENT01.jsonl')
            with open(agent_path, 'w') as f:
                f.write('{}\n')
            orig_root = self.r.CLAUDE_ROOT
            # Caches are keyed by sid; clear so this fixture's lookup
            # isn't shadowed by a stale entry from another test.
            self.r._SESSION_PATH_BY_SID.clear()
            self.r.CLAUDE_ROOT = root
            try:
                self.assertEqual(
                    self.r._outer_subagent_group_id(agent_path),
                    ('agent', sess_path, 'AGENT01'),
                )
                # A subagent whose <sid> matches no session anywhere → None.
                orphan_sub = os.path.join(
                    root, '-home-test-tree--worktrees-wt',
                    'orphan-sess', 'subagents')
                os.makedirs(orphan_sub)
                orphan_agent = os.path.join(orphan_sub, 'agent-ZZZ.jsonl')
                with open(orphan_agent, 'w') as f:
                    f.write('{}\n')
                self.assertIsNone(
                    self.r._outer_subagent_group_id(orphan_agent))
            finally:
                self.r.CLAUDE_ROOT = orig_root
                self.r._SESSION_PATH_BY_SID.clear()


class TestMdPagerResolution(unittest.TestCase):
    """``_resolve_md_pager`` walks $MDCAT / mdcat+less in order."""

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
        restore = self._with_env(MDCAT='my-md-cmd --flag')
        try:
            self.assertEqual(self.r._resolve_md_pager(),
                             ['my-md-cmd', '--flag'])
        finally:
            restore()

    def test_env_pipeline_uses_shell(self):
        restore = self._with_env(MDCAT='mdcat | less -R')
        try:
            cmd = self.r._resolve_md_pager()
            self.assertEqual(cmd[0], 'bash')
            self.assertEqual(cmd[1], '-c')
            self.assertIn('mdcat | less -R', cmd[2])
        finally:
            restore()

    def test_mdcat_plus_less_pipes_to_less_rs(self):
        # Default fallback when both mdcat and less exist: pipe
        # mdcat output through ``less -RS`` via bash.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._scratch_bin(tmp, ['mdcat', 'less'])
            saved_path = os.environ['PATH']
            restore = self._with_env(MDCAT=None)
            try:
                os.environ['PATH'] = tmp
                cmd = self.r._resolve_md_pager()
                self.assertEqual(cmd[0], 'bash')
                self.assertEqual(cmd[1], '-c')
                self.assertIn('mdcat', cmd[2])
                self.assertIn('less -RS', cmd[2])
            finally:
                os.environ['PATH'] = saved_path
                restore()

    def test_mdcat_alone_no_pipe(self):
        # Without ``less`` on PATH, fall back to bare ``mdcat``.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._scratch_bin(tmp, ['mdcat'])
            saved_path = os.environ['PATH']
            restore = self._with_env(MDCAT=None)
            try:
                os.environ['PATH'] = tmp
                self.assertEqual(self.r._resolve_md_pager(), ['mdcat'])
            finally:
                os.environ['PATH'] = saved_path
                restore()

    def test_none_when_nothing_resolves(self):
        restore = self._with_env(MDCAT=None)
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
            self.assertEqual(self.r._last_voice_id(path), ('msg', path, 3))
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
            self.assertEqual(roots[0].id, ('span', path, 0))
            # u1 prompt umbrella (line 1).
            self.assertEqual(roots[1].kind, 'prompt')
            self.assertEqual(roots[1].line_no, 1)
            self.assertEqual(roots[1].id, ('prompt', path, 1))
            self.assertTrue(roots[1].title.startswith('<prompt>'))
            # u2 prompt umbrella (line 3).
            self.assertEqual(roots[2].kind, 'prompt')
            self.assertEqual(roots[2].line_no, 3)
            self.assertEqual(roots[2].id, ('prompt', path, 3))
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
            self.assertEqual(prompt_kids[0].id, ('msg', path, 0))
            self.assertEqual(prompt_kids[0].kind, 'message')
            self.assertEqual(prompt_kids[1].id, ('tool', path, 1))
            self.assertEqual(prompt_kids[1].kind, 'tool')
            self.assertEqual(prompt_kids[2].id, ('msg', path, 3))
            self.assertEqual(prompt_kids[2].kind, 'message')
            # <tool:1>'s children = [a1 leaf, u2 leaf].
            tool_kids = self.r._list_tool_children(path, 1)
            self.assertEqual([it.id for it in tool_kids],
                             [('msg', path, 1), ('msg', path, 2)])
            # Leaves have no children in tree-mode get_children.
            saved = self.r._TREE_MODE
            try:
                self.r._TREE_MODE = True
                self.assertEqual(self.r.get_children(('msg', path, 0)), [])
                self.assertEqual(self.r.get_children(('msg', path, 2)), [])
                self.assertEqual(self.r.get_children(('msg', path, 3)), [])
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
                             [('msg', path, 0), ('msg', path, 1)])
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
            self.assertEqual(kids[0].id, ('msg', sess_path, 1))
            self.assertEqual(kids[0].kind, 'message')
            self.assertEqual(kids[1].id, ('msg', sess_path, 2))
            self.assertEqual(kids[1].kind, 'message')   # tool_result
            self.assertEqual(kids[2].kind, 'subagent')
            self.assertEqual(kids[2].agent_id, 'AGENT01')
            self.assertEqual(kids[2].id,
                             ('agent', sess_path, 'AGENT01'))
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
            self.assertEqual(tool_item.id, ('tool', sess_path, 1))
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
                roots = self.r.get_children(('session', path))
                self.assertEqual(len(roots), 1)
                self.assertEqual(roots[0].kind, 'prompt')
                self.assertEqual(roots[0].line_no, 0)   # u1
                self.assertEqual(roots[0].id, ('prompt', path, 0))
                # Prompt umbrella's children → [u1 leaf, a1 leaf].
                kids = self.r.get_children(('prompt', path, 0))
                self.assertEqual([it.id for it in kids],
                                 [('msg', path, 0), ('msg', path, 1)])
                # Regular message ids are leaves in tree mode.
                self.assertEqual(self.r.get_children(('msg', path, 1)), [])
                self.assertEqual(self.r.get_children(('msg', path, 0)), [])

                # Flat mode: session jsonl → messages newest-first list.
                self.r._TREE_MODE = False
                flat = self.r.get_children(('session', path))
                titles = [it.title for it in flat if it.kind == 'message']
                self.assertEqual(len(titles), 2)
                # Message id has no children in flat mode.
                self.assertEqual(self.r.get_children(('msg', path, 0)), [])
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
            self.assertEqual(it.id, ('prompt', path, 0))
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
            self.assertEqual(tool.id, ('tool', path, 1))
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
            self.assertEqual(item.id, ('agent', sess, 'A1'))
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
            self.assertEqual(kids[0].id, ('msg', path, 0))
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
            self.assertEqual(tool_kids[0].id, ('msg', path, 1))
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
            chain = self.r._ancestor_ids_for(('msg', path, 2))
            self.assertEqual(chain, [
                ('prompt', path, 0),
                ('tool', path, 1),
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
            self.assertIn(('prompt', path, 0), dirty)
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
            self.assertIn(('session', path), dirty)
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
            self.assertIn(('prompt', path, 0), dirty)
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
            self.r.get_children(('session', path_a), reload=True)
            self.assertIsNot(self.r._TREE_CACHE[path_a], td_a_before)
            # path_b's cache untouched.
            self.assertIs(self.r._TREE_CACHE[path_b], td_b_before)
            # Non-reload call leaves both alone.
            td_a_after = self.r._TREE_CACHE[path_a]
            self.r.get_children(('session', path_a))
            self.assertIs(self.r._TREE_CACHE[path_a], td_a_after)
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def _fake_browser_with_children(self, path, children):
        seen_ops = []
        # Flat-mode session children are keyed under the ``('session', …)`` id.
        session_id = ('session', path)

        class FakeState:
            def __init__(s):
                s._children = {session_id: list(children)}
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

    def test_push_flat_inserts_appends_at_end(self):
        # Since #475 flat mode renders chronologically, new tail
        # records must append at the **end** of the parent's child
        # list — not be inserted after the last subagent. The push
        # step emits plain ``upsert`` ops (no ``where=`` kwarg) so the
        # framework's default "append at end" placement applies.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'one'}},
        ])
        try:
            fake_subs = [
                self.r.Item(id=('agent', path, 'SUB_A'),
                            title='<subagent>  A'),
                self.r.Item(id=('agent', path, 'SUB_B'),
                            title='<subagent>  B'),
            ]
            fake_subs[0].kind = 'subagent'
            fake_subs[1].kind = 'subagent'
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
            # No positioning descriptor — default-append at end.
            # Upsert tuple shape: ('upsert', id, parent_id, fields).
            self.assertEqual(len(upserts[0]), 4)
        finally:
            os.unlink(path)

    def test_push_flat_inserts_preserves_record_order(self):
        # New records arrive from _read_new_records in file order
        # (oldest of the batch first). The push must preserve that
        # order so the framework appends in chronological sequence.
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
            # Op order matches input order: line 1 first, then line 2.
            self.assertEqual(upserts[0][1], ('msg', path, 1))
            self.assertEqual(upserts[1][1], ('msg', path, 2))
            self.assertFalse(
                [op for op in ops if op[0] == 'remove'],
            )
        finally:
            os.unlink(path)

    def test_cursor_tail_path_extraction(self):
        # Session id: resolves to its .jsonl when it exists.
        path = self._write_jsonl([{'type': 'user'}])
        try:
            self.assertEqual(self.r._cursor_tail_path(('session', path)), path)
            # Message id ``('msg', jsonl, n)``.
            self.assertEqual(self.r._cursor_tail_path(('msg', path, 0)), path)
            # Umbrella ids resolve to the path.
            self.assertEqual(self.r._cursor_tail_path(('prompt', path, 0)),
                             path)
            self.assertEqual(self.r._cursor_tail_path(('tool', path, 1)),
                             path)
            self.assertEqual(self.r._cursor_tail_path(('span', path, 2)),
                             path)
            # Non-jsonl path: None.
            self.assertIsNone(self.r._cursor_tail_path(('session', '/etc/passwd')))
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
            row_id = ('agent', sess, 'AGENT01')
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


class TestSeedScopeRootItem(unittest.TestCase):
    """``_seed_scope_root_item`` seeds the by-id index ONLY.

    The scope-root session Item must land in ``_items_by_id`` so the scope
    row renders its rich session header on first paint — but it must NOT be
    pushed into the parent project's ``_children`` listing. Seeding that
    listing with this single session would make the framework treat it as
    'already cached', so a later scope-up (``_scope_up`` →
    ``_ensure_children_fetched``) would skip the real
    ``_list_sessions(parent)`` fetch and show only this one session instead
    of all of the project's.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _write_jsonl(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for rec in records:
            f.write(_json.dumps(rec) + '\n')
        f.close()
        return f.name

    def _fake_browser(self):
        class FakeState:
            def __init__(s):
                s._items_by_id = {}
                s._children = {}
        class FakeBrowser:
            def __init__(s):
                s._state = FakeState()
        return FakeBrowser()

    def test_seeds_items_by_id_only(self):
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            b = self._fake_browser()
            self.r._seed_scope_root_item(b, path)
            sess_id = ('session', path)
            # Rich session Item seeded for the scope row.
            self.assertIn(sess_id, b._state._items_by_id)
            self.assertEqual(b._state._items_by_id[sess_id].kind, 'session')
            # Parent project listing left untouched — scope-up still
            # triggers the real _list_sessions fetch (no pre-cached entry).
            self.assertEqual(b._state._children, {})
        finally:
            os.unlink(path)

    def test_noop_when_item_unbuildable(self):
        # _session_item returns None for a missing file → no seed at all.
        b = self._fake_browser()
        self.r._seed_scope_root_item(b, '/no/such/file-xyz.jsonl')
        self.assertEqual(b._state._items_by_id, {})
        self.assertEqual(b._state._children, {})


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
            self.r.Item(id=('msg', path, 3)),                  # message leaf
            self.r.Item(id=('msg', path, 5)),                  # message leaf
            self.r.Item(id=('prompt', path, 10)),          # umbrella
            self.r.Item(id=('tool', path, 11)),            # umbrella
            self.r.Item(id=('span', path, 0)),             # umbrella
            self.r.Item(id=('agent', path, 'AAA')),          # subagent group
            self.r.Item(id=('session', path)),            # session row
            self.r.Item(id=('project', '/some/proj')),    # project dir
        ]
        per_line, whole_paths = self.r._gather_line_source(items)
        # Two message leaves on the same path → grouped.
        self.assertEqual(per_line, {path: [3, 5]})
        # Whole-path entries: umbrellas + subagent + session + project dir.
        whole_paths_only = [p for p, _ in whole_paths]
        self.assertEqual(len(whole_paths_only), 6)
        # Subagent group resolves to the agent's jsonl, not the parent
        # session.
        self.assertIn(
            os.path.join(self.r._subagents_dir(path), 'agent-AAA.jsonl'),
            whole_paths_only,
        )
        # The project dir resolves to its directory path.
        self.assertIn('/some/proj', whole_paths_only)
        # Plain umbrella ids + the session row resolve to the file path.
        self.assertEqual(whole_paths_only.count(path), 4)
        # __truncated__ falls back to whole-file.
        per_line2, whole2 = self.r._gather_line_source([
            self.r.Item(id=('trunc', path)),
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
                    self.r._gather_umbrella_lines(('prompt', path, 0)),
                    [0, 1, 2],
                )
                # Tool umbrella alone → assistant + tool_result.
                self.assertEqual(
                    self.r._gather_umbrella_lines(('tool', path, 1)),
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
                lines = self.r._gather_umbrella_lines(('prompt', sess_path, 0))
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
                items = [self.r.Item(id=('prompt', path, 0))]
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
                items = [self.r.Item(id=('prompt', path, 0))]
                per_line, _ = self.r._gather_line_source(items)
                self.assertEqual(per_line, {path: [0, 1, 2, 3, 4, 5, 6]})
                # Each tool umbrella covers only its own pair.
                self.assertEqual(
                    sorted(self.r._gather_umbrella_lines(('tool', path, 1))),
                    [1, 2],
                )
                self.assertEqual(
                    sorted(self.r._gather_umbrella_lines(('tool', path, 4))),
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
                    self.r.Item(id=('msg', path, 2)),
                    self.r.Item(id=('prompt', path, 0)),
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
            preview = ''.join(self.r._preview_umbrella(('tool', sess, 1)))
            self.assertNotIn('inside-subagent', preview)

    def test_tool_umbrella_for_agent_dispatch_has_subagent_bg(self):
        # When a <tool:Agent> umbrella wraps a resolvable subagent
        # dispatch, the umbrella row itself must carry the subagent
        # voice stripe — the voice marker propagates the whole way up
        # from the subagent leaf through its <tool:Agent> umbrella.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build(tmp)
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(sess)
            # Line 1 is the assistant tool_use record dispatching Agent.
            assistant_rec = td.records[1]
            item = self.r._tool_umbrella_item(sess, 1, assistant_rec, td)
            self.assertEqual(item.row_bg, 17,
                             '<tool:Agent> umbrella should inherit '
                             'subagent stripe when dispatching a resolvable '
                             'subagent transcript')

    def test_tool_umbrella_for_agent_dispatch_passes_voice_filter(self):
        # The voice-only filter (`.` key) must keep <tool:Agent> rows
        # visible when they wrap a resolvable subagent — the umbrella
        # carries the subagent voice stripe, so filtering them out
        # would hide the visual cue.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build(tmp)
            self.r._TREE_CACHE.clear()
            self.r._scan_tree(sess)
            tool_id = ('tool', sess, 1)
            # With voice-only filter ON, the <tool:Agent> id passes.
            saved = self.r._DETAIL_LEVEL
            self.r._DETAIL_LEVEL = 1
            try:
                self.assertTrue(self.r._passes_filter(tool_id))
            finally:
                self.r._DETAIL_LEVEL = saved

    def test_tool_umbrella_for_bash_filtered_out_in_voice_only(self):
        # Non-Agent tool (e.g. Bash) — no voice content, no subagent
        # link: should be hidden by the voice-only filter.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-z')
            os.makedirs(proj)
            sess = os.path.join(proj, 's.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
                 'message': {'role': 'user', 'content': 'ls'}},
                {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                 'message': {'role': 'assistant', 'content': [{
                     'type': 'tool_use', 'id': 'tb', 'name': 'Bash',
                     'input': {'command': 'ls'},
                 }]}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()
            self.r._scan_tree(sess)
            tool_id = ('tool', sess, 1)
            saved = self.r._DETAIL_LEVEL
            self.r._DETAIL_LEVEL = 1
            try:
                self.assertFalse(self.r._passes_filter(tool_id))
            finally:
                self.r._DETAIL_LEVEL = saved

    def test_umbrella_preview_omits_chrome_even_after_direct_leaf_visit(self):
        # Regression: previously, when a leaf had been visited directly
        # first (populating its cache with body + chrome) and then the
        # umbrella was viewed, the umbrella's cached-leaf shortcut would
        # pick up the chrome-bearing cache and bleed chrome into the
        # umbrella cascade. The fix: umbrella always renders fresh and
        # never reads/writes the leaf cache.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-c')
            os.makedirs(proj)
            sess = os.path.join(proj, 's.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
                 'sessionId': 'SID-XYZ',  # chrome carries this
                 'message': {'role': 'user', 'content': 'hello'}},
                {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                 'sessionId': 'SID-XYZ',
                 'message': {'role': 'assistant', 'content': [{
                     'type': 'text', 'text': 'hi back',
                 }]}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()

            # Step 1: simulate a direct leaf visit by writing the
            # chrome-bearing preview into the framework cache. (In real
            # usage, the framework's get_preview path produces this.)
            leaf_id = ('msg', sess, 0)
            chrome_text = self.r._preview_message(sess, 0)
            self.assertIn('SID-XYZ', chrome_text,
                          'sanity: leaf preview should include sessionId chrome')

            # Step 2: build the umbrella for the prompt and consume it.
            # The umbrella cascade should NOT include the chrome line
            # even though the leaf has been visited directly.
            umbrella = ''.join(self.r._preview_umbrella(('prompt', sess, 0)))
            # The user's voice content is in the umbrella.
            self.assertIn('hello', umbrella)
            # Chrome (sessionId line) must NOT appear in the umbrella.
            self.assertNotIn('SID-XYZ', umbrella,
                             'umbrella preview should not include leaf chrome')

    def test_toggle_filter_drops_preview_cache_for_synthetic_row(self):
        # Regression #4 (user-reported): on a freshly-opened jsonl,
        # cursor on the synthetic top row, pressing `.` (toggle
        # voice-only filter) must drop the cached preview so the
        # umbrella is re-streamed against the new filter.
        #
        # Previously the umbrella generated content survived the
        # toggle on the first press — only the second-pair toggle
        # (off then on again) finally re-streamed. Root cause: stale
        # cache from before the filter flip stayed in Item.preview;
        # nothing invalidated it on the synthetic-row id specifically.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-h')
            os.makedirs(proj)
            sess = os.path.join(proj, 's.jsonl')
            # Mix of voice (user/assistant text) and non-voice
            # (tool_use without text) records — so the filter has
            # something to hide.
            recs = [
                {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
                 'message': {'role': 'user',
                             'content': 'PROBE_VOICE_USER'}},
                {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                 'message': {'role': 'assistant', 'content': [{
                     'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                     'input': {'command': 'PROBE_BASH_HIDDEN'},
                 }]}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()

            # With filter OFF, the umbrella body contains both records.
            saved = self.r._DETAIL_LEVEL
            self.r._DETAIL_LEVEL = 4
            try:
                with_all = ''.join(self.r._preview_umbrella(('session', sess)))
                self.assertIn('PROBE_VOICE_USER', with_all)
                self.assertIn('PROBE_BASH_HIDDEN', with_all)

                # With filter ON, the umbrella body should drop the
                # non-voice <tool:Bash> umbrella.
                self.r._DETAIL_LEVEL = 1
                # Clear the recipe-level tree cache so the next
                # _preview_umbrella reflects the new filter state when
                # it builds children via get_children (mod ops aren't
                # simulated here — we use the same predicate).
                self.r._TREE_CACHE.clear()
                with_voice = ''.join(self.r._preview_umbrella(('session', sess)))
                self.assertIn('PROBE_VOICE_USER', with_voice)
                self.assertNotIn(
                    'PROBE_BASH_HIDDEN', with_voice,
                    'voice-only filter should hide the non-voice '
                    'tool_use record from the umbrella preview',
                )
            finally:
                self.r._DETAIL_LEVEL = saved

    def test_focus_latest_voice_when_ready_jumps_after_load(self):
        # _focus_latest_voice_when_ready chains:
        #   1. b.expand(target_jsonl).then(_fire)
        #   2. _fire reads _last_voice_id, checks cursor still on
        #      scope_root, then runs _chain_expand_then_cursor.
        # We capture each step with a fake Browser.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            sess = os.path.join(tmp, 's.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1',
                 'message': {'role': 'user', 'content': 'a'}},
                {'type': 'user', 'uuid': 'u2',
                 'message': {'role': 'user', 'content': 'b'}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()

            # Fake browser captures expand/cursor_to + simulates the
            # scope row at the cursor (still on it = user hasn't moved).
            calls = {'cursor_to': [], 'expand_chain': []}

            class _ScopeRowItem:
                def __init__(self, id_):
                    self.id = id_

            class _ScopeRowEntry:
                kind = 'normal'

                def __init__(self, id_):
                    self.item = _ScopeRowItem(id_)

            class _State:
                cursor = 0

            class _Pending:
                def __init__(self):
                    self._cb = None
                def then(self, cb):
                    self._cb = cb
                    return self
                def fire(self):
                    if self._cb:
                        self._cb()

            class _FakeBrowser:
                def __init__(self):
                    self._state = _State()
                    self._pendings = []
                def expand(self, _id, autoscroll=False):
                    p = _Pending()
                    self._pendings.append(p)
                    calls['expand_chain'].append(_id)
                    return p
                def cursor_to(self, _id):
                    calls['cursor_to'].append(_id)

            # Override visible_items so the scope-row check passes
            # (cursor still on the row whose id == ('session', target_jsonl)).
            saved_vi = self.r.visible_items
            self.r.visible_items = (
                lambda state: [_ScopeRowEntry(('session', sess))])
            try:
                b = _FakeBrowser()
                self.r._focus_latest_voice_when_ready(b, sess)
                # First expand call: the ('session', …) scope id (to wait
                # for scope_root's children).
                self.assertEqual(calls['expand_chain'], [('session', sess)])
                # No cursor_to yet — Pending not resolved.
                self.assertEqual(calls['cursor_to'], [])
                # Resolve each pending in sequence — the chain calls
                # ``b.expand(...).then(...)`` once per ancestor before
                # finally firing cursor_to. Drain until cursor_to fires.
                fired = 0
                while fired < len(b._pendings) and not calls['cursor_to']:
                    b._pendings[fired].fire()
                    fired += 1
                # Now the chain should have fired cursor_to on the
                # latest voice.
                self.assertEqual(calls['cursor_to'], [('msg', sess, 1)])
            finally:
                self.r.visible_items = saved_vi

    def test_focus_latest_voice_when_ready_cancels_if_cursor_moved(self):
        # If the user has navigated off scope_root before the
        # deferred fire, the jump cancels — no cursor_to.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            sess = os.path.join(tmp, 's.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1',
                 'message': {'role': 'user', 'content': 'voice'}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()

            calls = {'cursor_to': [], 'expand_chain': []}

            class _NormalEntry:
                kind = 'normal'
                class item:
                    id = 'somewhere/else'

            class _State:
                cursor = 0

            class _Pending:
                def __init__(self):
                    self._cb = None
                def then(self, cb):
                    self._cb = cb
                    return self
                def fire(self):
                    if self._cb:
                        self._cb()

            class _FakeBrowser:
                def __init__(self):
                    self._state = _State()
                    self._pendings = []
                def expand(self, _id, autoscroll=False):
                    p = _Pending()
                    self._pendings.append(p)
                    calls['expand_chain'].append(_id)
                    return p
                def cursor_to(self, _id):
                    calls['cursor_to'].append(_id)

            # visible_items reports cursor on a row whose id is NOT
            # the scope target — user has navigated away.
            saved_vi = self.r.visible_items
            self.r.visible_items = lambda state: [_NormalEntry()]
            try:
                b = _FakeBrowser()
                self.r._focus_latest_voice_when_ready(b, sess)
                # Fire delivery — user has navigated, jump cancels.
                b._pendings[0].fire()
                # cursor_to should NOT have been called.
                self.assertEqual(
                    calls['cursor_to'], [],
                    'cursor_to should be suppressed when the user '
                    'has navigated off the scope row before fire',
                )
            finally:
                self.r.visible_items = saved_vi

    def _make_two_turn_session(self, tmp):
        """A session whose tree has two ``<prompt>`` umbrellas.

        ``_latest_voice_among_children(sess)`` resolves to the *second*
        umbrella (latest top-level voice), which is the row the generic
        ``on_expand`` jump would land on — distinct from the deep latest
        message the dedicated startup focus targets.
        """
        import json as _json
        proj = os.path.join(tmp, '-x')
        os.makedirs(proj)
        sess = os.path.join(proj, 'sess.jsonl')
        recs = [
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'promptId': 'P1',
             'message': {'role': 'user', 'content': 'TURN1_USER'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant',
                         'content': [{'type': 'text',
                                      'text': 'TURN1_REPLY'}]}},
            {'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1',
             'promptId': 'P2',
             'message': {'role': 'user', 'content': 'TURN2_USER'}},
            {'type': 'assistant', 'uuid': 'a2', 'parentUuid': 'u2',
             'message': {'role': 'assistant',
                         'content': [{'type': 'text',
                                      'text': 'TURN2_REPLY'}]}},
        ]
        with open(sess, 'w') as f:
            for r in recs:
                f.write(_json.dumps(r) + '\n')
        return sess

    def test_on_expand_does_not_jump_on_scope_root(self):
        """#720: the scope_root's startup expand must NOT fire a
        competing jump-to-latest-voice.

        Two cursor-on-open jumps fire for the session scope_root opening:
        ``_focus_latest_voice_when_ready`` (deep latest message, the
        intended landing) and the generic ``on_expand`` hook
        (``_jump_to_latest_voice`` → the latest *top-level* umbrella).
        They race through the post queue; when the umbrella jump wins
        the cursor strands on a ``<prompt>`` umbrella whose heavy
        streaming preview hasn't composed yet, leaving the preview pane
        blank. The fix: ``_focus_latest_voice_when_ready`` records the
        scope_root it owns in ``_DEFERRED_FOCUS_SCOPE_ROOT``, and
        ``_on_expand`` skips its jump for that id, letting the deep
        landing win. The cross-file preview upgrade still fires.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_two_turn_session(tmp)
            scope = ('session', sess)
            self.r._TREE_CACHE.clear()
            saved_mode = self.r._TREE_MODE
            self.r._TREE_MODE = True
            calls = {'cursor_to': [], 'invalidate': []}

            class _FakeBrowser:
                _state = type('S', (), {'scope_stack': [scope],
                                        'expanded': set()})()
                def get_item(self, _id):
                    return None
                def invalidate_preview(self, id_):
                    calls['invalidate'].append(id_)

            class _Ctx:
                state = type('S', (), {'expanded': set()})()
                def cached_children(self, _id):
                    # Children ARE cached (immediate-jump branch); the
                    # fix must suppress regardless of cache state.
                    return [object()]
                def cursor_to(self, id_):
                    calls['cursor_to'].append(id_)

            saved_browser = self.r._BROWSER
            self.r._BROWSER = _FakeBrowser()
            # Simulate _focus_latest_voice_when_ready claiming the
            # scope_root's landing (the no-``--item`` path).
            self.r._DEFERRED_FOCUS_SCOPE_ROOT.add(scope)
            self.addCleanup(self.r._DEFERRED_FOCUS_SCOPE_ROOT.discard, scope)
            try:
                # Sanity: the competing target exists and is a top-level
                # umbrella (NOT the scope_root itself).
                latest = self.r._latest_voice_among_children(scope)
                self.assertIsNotNone(latest)
                self.assertNotEqual(latest, scope)
                self.assertEqual(latest[0], 'prompt')

                self.r._on_expand(_Ctx(), [scope])

                # No competing jump for the scope_root.
                self.assertEqual(
                    calls['cursor_to'], [],
                    'on_expand must not jump-to-latest-voice for the '
                    'scope_root the deferred focus owns',
                )
                # And the scope_root must NOT be parked for a deferred
                # jump either.
                self.assertNotIn(scope, self.r._AWAITING_VOICE_JUMP)
                # The cross-file preview upgrade still fires (scope_root
                # must go heavy).
                self.assertIn(scope, calls['invalidate'])
            finally:
                self.r._BROWSER = saved_browser
                self.r._TREE_MODE = saved_mode
                self.r._AWAITING_VOICE_JUMP.discard(scope)
                self.r._DEFERRED_FOCUS_SCOPE_ROOT.discard(scope)

    def test_on_expand_scope_root_claim_is_consumed_once(self):
        """#720: the deferred-focus claim is one-shot.

        The startup ``on_expand`` of the scope_root is suppressed (the
        focus owns that landing), but the claim must be CONSUMED so a
        later *manual* collapse + re-expand of the scope_root row drills
        in to the latest voice normally. A persistent claim would
        suppress that legitimate jump for the process lifetime — the
        regression the consume-once discard fixes.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_two_turn_session(tmp)
            scope = ('session', sess)
            self.r._TREE_CACHE.clear()
            saved_mode = self.r._TREE_MODE
            self.r._TREE_MODE = True
            calls = {'cursor_to': []}

            class _FakeBrowser:
                _state = type('S', (), {'scope_stack': [scope],
                                        'expanded': set()})()
                def get_item(self, _id):
                    return None
                def invalidate_preview(self, id_):
                    pass

            class _Ctx:
                state = type('S', (), {'expanded': set()})()
                def cached_children(self, _id):
                    return [object()]
                def cursor_to(self, id_):
                    calls['cursor_to'].append(id_)

            saved_browser = self.r._BROWSER
            self.r._BROWSER = _FakeBrowser()
            # Focus claims the scope_root's startup landing.
            self.r._DEFERRED_FOCUS_SCOPE_ROOT.add(scope)
            self.addCleanup(self.r._DEFERRED_FOCUS_SCOPE_ROOT.discard, scope)
            ctx = _Ctx()
            try:
                # 1st expand (startup) — suppressed AND the claim is
                # consumed.
                self.r._on_expand(ctx, [scope])
                self.assertEqual(
                    calls['cursor_to'], [],
                    'startup scope_root expand must be suppressed',
                )
                self.assertNotIn(
                    scope, self.r._DEFERRED_FOCUS_SCOPE_ROOT,
                    'the claim must be consumed on the first expand',
                )
                # 2nd expand (manual re-expand) — drills in normally now.
                self.r._on_expand(ctx, [scope])
                expected = self.r._latest_voice_among_children(scope)
                self.assertIsNotNone(expected)
                self.assertEqual(
                    calls['cursor_to'], [expected],
                    'a manual re-expand of the scope_root must jump to '
                    'the latest voice (claim was consumed)',
                )
            finally:
                self.r._BROWSER = saved_browser
                self.r._TREE_MODE = saved_mode
                self.r._AWAITING_VOICE_JUMP.discard(scope)

    def test_on_expand_scope_root_jumps_when_focus_not_active(self):
        """#720 guard: with no deferred focus (``--item`` launch) the
        scope_root keeps its latest-voice landing.

        ``_DEFERRED_FOCUS_SCOPE_ROOT`` is empty when launched with
        ``--item`` (no ``_focus_latest_voice_when_ready``). The generic
        ``on_expand`` jump must then still land the scope_root on its
        latest top-level voice, preserving the long-standing flat-mode
        behavior (e.g. the SendMessage round-trip UI tests).
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_two_turn_session(tmp)
            scope = ('session', sess)
            self.r._TREE_CACHE.clear()
            saved_mode = self.r._TREE_MODE
            self.r._TREE_MODE = True
            calls = {'cursor_to': []}

            class _FakeBrowser:
                _state = type('S', (), {'scope_stack': [scope],
                                        'expanded': set()})()
                def get_item(self, _id):
                    return None
                def invalidate_preview(self, id_):
                    pass

            class _Ctx:
                state = type('S', (), {'expanded': set()})()
                def cached_children(self, _id):
                    return [object()]
                def cursor_to(self, id_):
                    calls['cursor_to'].append(id_)

            saved_browser = self.r._BROWSER
            self.r._BROWSER = _FakeBrowser()
            # No deferred-focus claim — the --item launch path.
            self.r._DEFERRED_FOCUS_SCOPE_ROOT.discard(scope)
            try:
                self.r._on_expand(_Ctx(), [scope])
                expected = self.r._latest_voice_among_children(scope)
                self.assertIsNotNone(expected)
                self.assertEqual(calls['cursor_to'], [expected])
            finally:
                self.r._BROWSER = saved_browser
                self.r._TREE_MODE = saved_mode

    def test_on_expand_still_jumps_on_non_scope_umbrella(self):
        """#720 guard: suppressing the scope_root jump must not break the
        jump for a normal umbrella expansion.

        Expanding a ``<prompt>`` umbrella (not claimed by the deferred
        focus) must still land the cursor on its latest voice — the
        signature drill-in gesture is unchanged.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_two_turn_session(tmp)
            self.r._TREE_CACHE.clear()
            saved_mode = self.r._TREE_MODE
            self.r._TREE_MODE = True
            umbrella = ('prompt', sess, 2)  # turn-2 umbrella (TURN2_USER)
            calls = {'cursor_to': []}

            class _FakeBrowser:
                _state = type('S', (), {'scope_stack': [sess],
                                        'expanded': set()})()
                def get_item(self, _id):
                    return None
                def invalidate_preview(self, id_):
                    pass

            class _Ctx:
                state = type('S', (), {'expanded': set()})()
                def cached_children(self, _id):
                    return [object()]
                def cursor_to(self, id_):
                    calls['cursor_to'].append(id_)

            saved_browser = self.r._BROWSER
            self.r._BROWSER = _FakeBrowser()
            # The scope_root claim must not suppress a different id.
            self.r._DEFERRED_FOCUS_SCOPE_ROOT.add(sess)
            self.addCleanup(self.r._DEFERRED_FOCUS_SCOPE_ROOT.discard, sess)
            try:
                self.r._on_expand(_Ctx(), [umbrella])
                # The umbrella's latest voice must be landed on.
                expected = self.r._latest_voice_among_children(umbrella)
                self.assertIsNotNone(expected)
                self.assertEqual(calls['cursor_to'], [expected])
            finally:
                self.r._BROWSER = saved_browser
                self.r._TREE_MODE = saved_mode

    def test_scan_tree_serializes_concurrent_callers(self):
        # Two threads calling _scan_tree on the same path concurrently
        # must NOT both fully parse the file — the second blocks on the
        # per-path mutex and picks up the populated _TREE_CACHE.
        import tempfile
        import json as _json
        import threading as _threading
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            sess = os.path.join(tmp, 's.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({
                    'type': 'user', 'uuid': 'u1',
                    'message': {'role': 'user', 'content': 'x'},
                }) + '\n')
            self.r._TREE_CACHE.clear()
            self.r._SCAN_LOCKS.clear()

            # Instrument the actual scan body to count invocations.
            calls = []
            original = self.r._scan_tree_locked

            def _counting_locked(p):
                calls.append(p)
                # Tiny sleep to ensure the second caller hits the lock
                # while we're inside.
                _time.sleep(0.01)
                return original(p)

            self.r._scan_tree_locked = _counting_locked
            try:
                results = [None, None]

                def _call(idx):
                    results[idx] = self.r._scan_tree(sess)

                t1 = _threading.Thread(target=_call, args=(0,))
                t2 = _threading.Thread(target=_call, args=(1,))
                t1.start()
                t2.start()
                t1.join(timeout=2.0)
                t2.join(timeout=2.0)
                # Exactly one scan ran; both callers got the same td.
                self.assertEqual(
                    len(calls), 1,
                    f'expected exactly one scan; got {len(calls)}',
                )
                self.assertIsNotNone(results[0])
                self.assertIs(results[0], results[1])
            finally:
                self.r._scan_tree_locked = original

    def test_scan_tree_tracks_latest_voice_line(self):
        # _scan_tree's forward pass eagerly populates
        # td.latest_voice_line so the cursor-on-open path doesn't have
        # to walk records[] in reverse.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            sess = os.path.join(tmp, 's.jsonl')
            recs = [
                # 0: voice (user text)
                {'type': 'user', 'uuid': 'u1',
                 'message': {'role': 'user', 'content': 'first'}},
                # 1: NOT voice (tool_use without text)
                {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                 'message': {'role': 'assistant', 'content': [{
                     'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                     'input': {'command': 'ls'},
                 }]}},
                # 2: voice (assistant text — the latest)
                {'type': 'assistant', 'uuid': 'a2', 'parentUuid': 'u1',
                 'message': {'role': 'assistant', 'content': [{
                     'type': 'text', 'text': 'reply',
                 }]}},
                # 3: NOT voice (tool_result)
                {'type': 'user', 'uuid': 'u2', 'parentUuid': 'a1',
                 'message': {'role': 'user', 'content': [{
                     'type': 'tool_result', 'tool_use_id': 't1',
                     'content': 'output',
                 }]}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(sess)
            # Line 2 was the most-recent voice. Lines 1 and 3 are
            # machinery (no text content) so they don't shift the
            # tracker.
            self.assertEqual(td.latest_voice_line, 2)

    def test_last_voice_id_is_o1_via_tracked_field(self):
        # _last_voice_id reads td.latest_voice_line; no reverse walk.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            sess = os.path.join(tmp, 's.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1',
                 'message': {'role': 'user', 'content': 'q'}},
                {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                 'message': {'role': 'assistant', 'content': [{
                     'type': 'text', 'text': 'r',
                 }]}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()
            self.assertEqual(self.r._last_voice_id(sess), ('msg', sess, 1))

    def test_last_voice_id_none_when_no_voice_records(self):
        # Pure-machinery transcript: scan completes with
        # latest_voice_line still None, _last_voice_id returns None.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            sess = os.path.join(tmp, 's.jsonl')
            recs = [
                # All non-voice (tool_use / tool_result / system)
                {'type': 'system', 'uuid': 's1',
                 'subtype': 'compaction', 'content': ''},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(sess)
            self.assertIsNone(td.latest_voice_line)
            self.assertIsNone(self.r._last_voice_id(sess))

    def test_flush_refreshes_hidden_against_live_filter(self):
        # #4 follow-up: the umbrella generator captures each child's
        # ``hidden`` flag at descent time. If the user toggles the
        # voice-only filter between descent and flush (the kick path
        # for `h`), the abandoned generator's finally flush must
        # re-evaluate hidden against the live filter — otherwise the
        # stale flag clobbers the toggle's mod ops.
        op = ('upsert', '/x/file.jsonl#tool:5',
              '/x/file.jsonl#prompt:0',
              {'hidden': False, 'title': 't'},
              None)
        # Mock _passes_filter so we can drive the live filter state.
        original_passes = self.r._passes_filter
        self.r._passes_filter = lambda *_a, **_k: False  # hidden=True
        try:
            refreshed = self.r._refresh_hidden_in_op(op)
            self.assertEqual(refreshed[3]['hidden'], True,
                             '_refresh_hidden_in_op should reflect '
                             'the live filter state')
            # Non-upsert ops pass through unchanged.
            mod_op = ('mod', '/x#0', None, {'title': 'new'}, None)
            self.assertIs(self.r._refresh_hidden_in_op(mod_op), mod_op)
            # Upsert without hidden in fields also passes through.
            no_hidden = ('upsert', '/x#0', None, {'title': 't'}, None)
            self.assertIs(self.r._refresh_hidden_in_op(no_hidden), no_hidden)
        finally:
            self.r._passes_filter = original_passes

    def test_umbrella_does_not_clobber_leaf_preview_cache(self):
        # Regression: previously, the umbrella's set_preview_op
        # side-effect wrote body-only into leaf Item.preview, so a
        # subsequent direct leaf visit painted body-only (missing
        # chrome). Now the umbrella never writes leaf cache; direct
        # visits manage their own cache via get_preview.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-d')
            os.makedirs(proj)
            sess = os.path.join(proj, 's.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
                 'sessionId': 'SID-ABC',
                 'message': {'role': 'user', 'content': 'q'}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()

            # Run the umbrella generator to completion — this used to
            # write leaf previews into the framework cache.
            list(self.r._preview_umbrella(('prompt', sess, 0)))

            # A subsequent leaf preview must still produce chrome.
            leaf_text = self.r._preview_message(sess, 0)
            self.assertIn('SID-ABC', leaf_text,
                          'leaf preview should still include chrome '
                          'after the umbrella ran')

    def test_tool_umbrella_for_non_agent_tool_has_no_bg(self):
        # Sanity: <tool:Bash> (or any non-Agent tool) without an
        # agent_link gets no row stripe — the propagation is specific
        # to Agent/Task dispatches.
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-y')
            os.makedirs(proj)
            sess = os.path.join(proj, 's.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
                 'message': {'role': 'user', 'content': 'ls'}},
                {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
                 'message': {'role': 'assistant', 'content': [{
                     'type': 'tool_use', 'id': 'toolu_B', 'name': 'Bash',
                     'input': {'command': 'ls'},
                 }]}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            self.r._TREE_CACHE.clear()
            td = self.r._scan_tree(sess)
            item = self.r._tool_umbrella_item(sess, 1, td.records[1], td)
            self.assertIsNone(getattr(item, 'row_bg', None))


class TestSessionRowVsScopeRootPreview(unittest.TestCase):
    """When a session row is just a list element (not scope_root, not
    expanded), the preview is metadata-only. When scope_root or
    expanded, it's the full umbrella cascade."""

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

    def test_metadata_preview_reads_one_line(self):
        path = self._write([
            {'type': 'user', 'sessionId': 's-uuid', 'cwd': '/w',
             'gitBranch': 'main', 'slug': 'happy',
             'message': {'role': 'user', 'content': 'first prompt body'}},
            {'type': 'assistant', 'sessionId': 's-uuid',
             'message': {'role': 'assistant',
                         'content': [{'type': 'text', 'text': 'reply body'}]}},
        ])
        try:
            out = self.r._preview_file_metadata(path)
            self.assertIn('browse-claude', out)
            self.assertIn(path, out)
            self.assertIn('s-uuid', out)
            self.assertIn('main', out)
            self.assertIn('happy', out)
            self.assertIn('mtime', out)
            # The body of the file must NOT appear — that's the cascade
            # preview's job, not the cheap metadata path.
            self.assertNotIn('reply body', out)
            self.assertNotIn('first prompt body', out)
        finally:
            os.unlink(path)

    def test_metadata_preview_no_scan_tree(self):
        # _preview_file_metadata must NOT populate _TREE_CACHE.
        path = self._write([
            {'type': 'user', 'sessionId': 's',
             'message': {'role': 'user', 'content': 'hi'}},
        ])
        try:
            self.r._TREE_CACHE.clear()
            self.r._preview_file_metadata(path)
            self.assertNotIn(path, self.r._TREE_CACHE,
                             '_scan_tree should not have been called')
        finally:
            os.unlink(path)

    def test_get_preview_session_row_goes_through_metadata(self):
        # No _BROWSER → _item_is_active returns True; force the
        # context by stashing a fake browser with empty scope/expand.
        path = self._write([
            {'type': 'user', 'sessionId': 'sid',
             'message': {'role': 'user', 'content': 'body'}},
        ])
        try:
            class _S:
                def __init__(self):
                    self.scope_stack = []
                    self.expanded = set()
            class _B:
                _state = _S()
            saved = self.r._BROWSER
            self.r._BROWSER = _B()
            self.r._TREE_CACHE.clear()
            try:
                out = self.r.get_preview(('session', path))
            finally:
                self.r._BROWSER = saved
            self.assertIn(path, out)
            self.assertNotIn('body', out)
        finally:
            os.unlink(path)

    def test_get_preview_scope_root_goes_through_full_cascade(self):
        path = self._write([
            {'type': 'user', 'sessionId': 'sid',
             'message': {'role': 'user', 'content': 'body-line'}},
        ])
        try:
            class _S:
                def __init__(self, scope):
                    self.scope_stack = [scope]
                    self.expanded = set()
            class _B:
                def __init__(self, scope):
                    self._state = _S(scope)
                def cached_children(self, _id): return None
                def update_data(self, _ops): pass
                def set_preview(self, _id, _text): pass
                items_by_id = {}
            saved = self.r._BROWSER
            self.r._BROWSER = _B(('session', path))
            self.r._TREE_CACHE.clear()
            try:
                # Umbrella branches return a generator (#460) — drain
                # it for the substring assertions below.
                result = self.r.get_preview(('session', path))
                out = (
                    ''.join(result) if not isinstance(result, str) else result
                )
            finally:
                self.r._BROWSER = saved
            # Scope card + cascaded body content.
            self.assertIn('browse-claude', out)
            self.assertIn('body-line', out)
        finally:
            os.unlink(path)

    def test_get_preview_expanded_goes_through_full_cascade(self):
        path = self._write([
            {'type': 'user', 'sessionId': 'sid',
             'message': {'role': 'user', 'content': 'body-line'}},
        ])
        try:
            class _S:
                def __init__(self, expanded):
                    self.scope_stack = []
                    self.expanded = expanded
            class _B:
                def __init__(self, expanded):
                    self._state = _S(expanded)
                def cached_children(self, _id): return None
                def update_data(self, _ops): pass
                def set_preview(self, _id, _text): pass
                items_by_id = {}
            saved = self.r._BROWSER
            self.r._BROWSER = _B({('session', path)})
            self.r._TREE_CACHE.clear()
            try:
                # Umbrella branches return a generator (#460).
                result = self.r.get_preview(('session', path))
                out = (
                    ''.join(result) if not isinstance(result, str) else result
                )
            finally:
                self.r._BROWSER = saved
            self.assertIn('body-line', out)
        finally:
            os.unlink(path)


class TestSubagentRowLightweightPreview(unittest.TestCase):
    """``('agent', …)`` rows follow the same cross-file rule as session rows:
    lightweight metadata until the row is scope_root or expanded.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _build(self, tmp):
        import json as _json
        proj = os.path.join(tmp, '-x')
        os.makedirs(proj)
        sess = os.path.join(proj, 'parent-sid.jsonl')
        with open(sess, 'w') as f:
            f.write(_json.dumps({
                'type': 'user', 'sessionId': 'parent-sid',
                'message': {'role': 'user', 'content': 'go'},
            }) + '\n')
        sub_dir = os.path.join(proj, 'parent-sid', 'subagents')
        os.makedirs(sub_dir)
        agent_path = os.path.join(sub_dir, 'agent-A1.jsonl')
        with open(agent_path, 'w') as f:
            f.write(_json.dumps({
                'type': 'user', 'sessionId': 'parent-sid',
                'message': {'role': 'user',
                            'content': 'SUBAGENT-INTERNAL-BODY'},
            }) + '\n')
        with open(os.path.join(sub_dir, 'agent-A1.meta.json'), 'w') as fm:
            _json.dump({'agentType': 'general-purpose',
                        'description': 'compose previews'}, fm)
        return sess, agent_path

    def test_metadata_preview_for_subagent_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, agent_path = self._build(tmp)
            out = self.r._preview_file_metadata(agent_path)
            self.assertIn('subagent', out)
            self.assertIn('A1', out)
            self.assertIn('general-purpose', out)
            self.assertIn('compose previews', out)
            self.assertIn(agent_path, out)
            # Body of the subagent jsonl must NOT appear.
            self.assertNotIn('SUBAGENT-INTERNAL-BODY', out)

    def test_get_preview_subagent_row_inactive_metadata(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, agent_path = self._build(tmp)
            item_id = ('agent', sess, 'A1')
            class _S:
                scope_stack = []
                expanded = set()
            class _B:
                _state = _S()
                def cached_children(self, _id): return None
                def update_data(self, _ops): pass
                def set_preview(self, _id, _text): pass
                items_by_id = {}
            saved = self.r._BROWSER
            self.r._BROWSER = _B()
            self.r._TREE_CACHE.clear()
            try:
                out = self.r.get_preview(item_id)
            finally:
                self.r._BROWSER = saved
            self.assertIn('subagent', out)
            self.assertIn('A1', out)
            self.assertNotIn('SUBAGENT-INTERNAL-BODY', out)
            self.assertNotIn(agent_path, self.r._TREE_CACHE,
                             'no _scan_tree on inactive subagent row')

    def test_get_preview_subagent_row_scope_root_active(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, agent_path = self._build(tmp)
            item_id = ('agent', sess, 'A1')
            class _S:
                def __init__(self, scope):
                    self.scope_stack = [scope]
                    self.expanded = set()
            class _B:
                def __init__(self, scope):
                    self._state = _S(scope)
                def cached_children(self, _id): return None
                def update_data(self, _ops): pass
                def set_preview(self, _id, _text): pass
                items_by_id = {}
            saved = self.r._BROWSER
            self.r._BROWSER = _B(item_id)
            self.r._TREE_CACHE.clear()
            try:
                # Umbrella branches return a generator (#460).
                result = self.r.get_preview(item_id)
                out = (
                    ''.join(result) if not isinstance(result, str) else result
                )
            finally:
                self.r._BROWSER = saved
            self.assertIn('SUBAGENT-INTERNAL-BODY', out)

    def test_get_preview_subagent_row_expanded_active(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, agent_path = self._build(tmp)
            item_id = ('agent', sess, 'A1')
            class _S:
                def __init__(self, expanded):
                    self.scope_stack = []
                    self.expanded = expanded
            class _B:
                def __init__(self, expanded):
                    self._state = _S(expanded)
                def cached_children(self, _id): return None
                def update_data(self, _ops): pass
                def set_preview(self, _id, _text): pass
                items_by_id = {}
            saved = self.r._BROWSER
            self.r._BROWSER = _B({item_id})
            self.r._TREE_CACHE.clear()
            try:
                # Umbrella branches return a generator (#460).
                result = self.r.get_preview(item_id)
                out = (
                    ''.join(result) if not isinstance(result, str) else result
                )
            finally:
                self.r._BROWSER = saved
            self.assertIn('SUBAGENT-INTERNAL-BODY', out)


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
            # ``get_preview`` for a session id returns the umbrella
            # generator (cross-file scope_root path) — drain it for
            # the assertions below.
            result = self.r.get_preview(('session', path))
            out = ''.join(result) if not isinstance(result, str) else result
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
            # ``--- Subagents:`` divider, then the orphan subagent; A1
            # (wired) is not at the top — it renders inline under its
            # dispatching turn.
            self.assertTrue(getattr(roots[0], 'meta', False))
            self.assertEqual(roots[0].id, ('sep', sess, 'subagents'))
            self.assertEqual(roots[0].title, '--- Subagents:')
            self.assertEqual(getattr(roots[1], 'kind', None), 'subagent')
            self.assertEqual(roots[1].agent_id, 'A2')
            kinds = [getattr(r, 'kind', None) for r in roots]
            self.assertNotIn('subagent', kinds[2:])

    def test_tree_roots_brackets_orphan_block_with_meta_dividers(self):
        # A session WITH orphaned subagents brackets the orphan block:
        # ``--- Subagents:`` (meta) → orphan rows → ``--- Session:``
        # (meta) → the turn/span umbrellas. Both dividers are meta rows
        # with session-namespaced, stable ids.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build_fixture(tmp, [
                ('A1', 'general-purpose', 'wired one',   True),
                ('A2', 'general-purpose', 'orphan one',  False),
                ('A3', 'general-purpose', 'orphan two',  False),
            ])
            self.r._TREE_CACHE.clear()
            roots = self.r._list_tree_roots(sess)
            # [subagents-sep, A2, A3, session-sep, <umbrellas...>].
            self.assertTrue(getattr(roots[0], 'meta', False))
            self.assertEqual(roots[0].id, ('sep', sess, 'subagents'))
            self.assertEqual(roots[0].title, '--- Subagents:')
            self.assertEqual(getattr(roots[1], 'kind', None), 'subagent')
            self.assertEqual(getattr(roots[2], 'kind', None), 'subagent')
            self.assertEqual({roots[1].agent_id, roots[2].agent_id},
                             {'A2', 'A3'})
            self.assertTrue(getattr(roots[3], 'meta', False))
            self.assertEqual(roots[3].id, ('sep', sess, 'session'))
            self.assertEqual(roots[3].title, '--- Session:')
            # Everything after the session divider is a turn/span
            # umbrella — never a subagent or a meta divider.
            tail_kinds = [getattr(r, 'kind', None) for r in roots[4:]]
            self.assertNotIn('subagent', tail_kinds)
            self.assertTrue(roots[4:])  # at least one umbrella present
            self.assertFalse(any(getattr(r, 'meta', False)
                                 for r in roots[4:]))

    def test_tree_roots_no_dividers_without_orphans(self):
        # A session whose subagents are all wired (or has none) renders
        # exactly as before — no meta dividers at all.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._build_fixture(tmp, [
                ('A1', 'general-purpose', 'wired one',  True),
                ('A2', 'general-purpose', 'wired two',  True),
            ])
            self.r._TREE_CACHE.clear()
            roots = self.r._list_tree_roots(sess)
            self.assertFalse(any(getattr(r, 'meta', False) for r in roots))
            self.assertFalse(any(getattr(r, 'kind', None) == 'subagent'
                                 for r in roots))


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
    """``1``-``4`` detail levels + ``--detail`` flag: hide everything
    below the chosen level. Level 1 (voice-only) is the default; raising
    the level (up to 4 = all) reveals more record kinds.

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
        self._saved_filter = self.r._DETAIL_LEVEL
        self.r._DETAIL_LEVEL = 4

    def tearDown(self):
        self.r._DETAIL_LEVEL = self._saved_filter

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
        self.r._DETAIL_LEVEL = 4
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
            self.r._DETAIL_LEVEL = 1
            self.assertTrue(self.r._passes_filter(('msg', path, 0)))
            self.assertFalse(self.r._passes_filter(('msg', path, 1)))
        finally:
            os.unlink(path)

    def test_passes_filter_prompt_umbrella_always_true(self):
        # A ``('prompt', …)`` umbrella is always voice-bearing by
        # construction. Seed its record as a pure tool_use (non-voice as a
        # plain message) and a tree-data entry, so a True result can ONLY
        # come from the ``tag == 'prompt'`` rule — not the no-td guard nor
        # the message fall-through (both of which we route past here).
        path = '/tmp/fake-prompt.jsonl'
        td = self.r._TreeData()
        td.records = [{'type': 'assistant', 'message': {'role': 'assistant',
                       'content': [{'type': 'tool_use', 'id': 't',
                                    'name': 'X', 'input': {}}]}}]
        self.r._TREE_CACHE[path] = td
        try:
            self.r._DETAIL_LEVEL = 1
            self.assertTrue(self.r._passes_filter(('prompt', path, 0)))
        finally:
            self.r._TREE_CACHE.pop(path, None)

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
            self.r._DETAIL_LEVEL = 1
            self.assertFalse(self.r._passes_filter(('tool', path, 0)))
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
            self.r._DETAIL_LEVEL = 1
            self.assertTrue(self.r._passes_filter(('tool', path, 0)))
        finally:
            os.unlink(path)

    def test_passes_filter_span_umbrella_membership(self):
        # A ``('span', …)`` umbrella passes iff any record in the span is voice.
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
            self.r._DETAIL_LEVEL = 1
            self.assertFalse(self.r._passes_filter(('span', path, 0)))
            self.assertTrue(self.r._passes_filter(('span', path, 5)))
        finally:
            self.r._TREE_CACHE.pop(path, None)

    def test_passes_filter_subagent_always_true(self):
        # Subagent umbrellas (``('agent', …)``) are unconditionally
        # visible — the recipe doesn't peek into another file to check.
        self.r._DETAIL_LEVEL = 1
        self.assertTrue(self.r._passes_filter(('agent', 'whatever', 'ABC-DEF')))

    def test_passes_filter_unparseable_id_is_permissive(self):
        # Synthetic ids (err rows, ``__truncated__``) must stay visible.
        self.r._DETAIL_LEVEL = 1
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
            self.r._DETAIL_LEVEL = 1
            items = self.r._list_messages(path)
            # Filter out the synthetic truncation row if any.
            by_id = {it.id: it for it in items if it.id[0] != 'trunc'}
            self.assertFalse(by_id[('msg', path, 0)].hidden,
                             'voice leaf must be visible')
            self.assertTrue(by_id[('msg', path, 1)].hidden,
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
            self.r._DETAIL_LEVEL = 4
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
            self.r._DETAIL_LEVEL = 1
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
            self.r._DETAIL_LEVEL = 1
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
                s.flashes = []
            def flash(s, text, log=False):
                s.flashes.append(text)
            def all_items(s):
                return s._browser.all_items()
            def update_data(s, ops):
                return s._browser.update_data(ops)
            def drop_preview_cache(s, id_=None):
                return s._browser.drop_preview_cache(id_)

        return FakeCtx(), seen_ops

    def test_set_level_emits_mod_batch_for_loaded_items(self):
        # Setting level 1 (voice) should mod every non-voice loaded item
        # to hidden=True; voice items and subagents stay hidden=False.
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
            voice = self.r.Item(id=('msg', path, 0))
            voice.hidden = False
            tool = self.r.Item(id=('msg', path, 1))
            tool.hidden = False
            sub = self.r.Item(id=('agent', path, 'ABC'))
            sub.hidden = False
            ctx, seen = self._fake_browser_with_items({
                voice.id: voice, tool.id: tool, sub.id: sub,
            })
            # From show-all (level 4, setUp) down to voice (level 1).
            self.r._set_detail_level(ctx, 1)
            self.assertEqual(self.r._DETAIL_LEVEL, 1)
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

    def test_set_level_round_trip_restores_visibility(self):
        # Level 1 then level 4: a tool row hides then re-shows.
        path = self._write_jsonl([
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
        ])
        try:
            self.r._scan_tree(path)
            tool = self.r.Item(id=('msg', path, 0))
            tool.hidden = False
            ctx, seen = self._fake_browser_with_items({tool.id: tool})
            self.r._set_detail_level(ctx, 1)
            self.assertEqual(self.r._DETAIL_LEVEL, 1)
            # Simulate the framework applying the first batch.
            for op in seen[0]:
                if op[0] == 'mod' and op[1] == tool.id:
                    tool.hidden = op[3].get('hidden', False)
            self.r._set_detail_level(ctx, 4)
            self.assertEqual(self.r._DETAIL_LEVEL, 4)
            self.assertEqual(len(seen), 2)
            # Second batch flips the tool back to hidden=False.
            mods = {op[1]: op[3] for op in seen[1] if op[0] == 'mod'}
            self.assertIn(tool.id, mods)
            self.assertEqual(mods[tool.id].get('hidden'), False)
        finally:
            os.unlink(path)

    def test_set_level_drops_preview_cache(self):
        # Umbrella previews compose from non-hidden children. After a
        # level change, every cached preview is potentially stale —
        # the recipe drops the whole preview cache via the public
        # ``drop_preview_cache()`` API, which the framework guarantees
        # will also re-kick the cursor preview and signal a redraw.
        ctx, _ = self._fake_browser_with_items({})
        # Replace the fake's drop_preview_cache with a spy so we can
        # confirm the recipe called it.
        drops = []
        ctx._browser.drop_preview_cache = lambda id_=None: drops.append(id_)
        self.r._set_detail_level(ctx, 1)
        self.assertEqual(drops, [None],
                         'recipe should drop the whole preview cache '
                         'so the framework re-fetches the cursor view')

    def test_set_level_no_remove_ops(self):
        # A level change never emits remove ops — visibility is
        # non-destructive.
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
            it0 = self.r.Item(id=('msg', path, 0)); it0.hidden = False
            it1 = self.r.Item(id=('msg', path, 1)); it1.hidden = False
            ctx, seen = self._fake_browser_with_items({
                it0.id: it0, it1.id: it1,
            })
            self.r._set_detail_level(ctx, 1)
            ops = seen[0]
            self.assertFalse(
                any(op[0] in ('remove', 'clear_children') for op in ops),
                f'destructive op leaked into level-change batch: {ops}',
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
                    s._children = {('session', path): []}
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

            self.r._DETAIL_LEVEL = 1
            self.r._push_flat_inserts(FakeBrowser(), path, new_records)
            self.assertEqual(len(seen_ops), 1)
            ops = seen_ops[0]
            by_id = {op[1]: op[3] for op in ops if op[0] == 'upsert'}
            self.assertEqual(by_id[('msg', path, 1)].get('hidden'), False,
                             'voice row should arrive visible')
            self.assertEqual(by_id[('msg', path, 2)].get('hidden'), True,
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
            span_id = ('span', path, 0)
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

            self.r._DETAIL_LEVEL = 1
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
            self.r._DETAIL_LEVEL = 4
            # Session-path preview routes through the umbrella
            # generator (#460); drain it for the assertions.
            full = ''.join(self.r.get_preview(('session', path)))
            self.assertIn('PROBE_VOICE_TEXT', full)
            self.assertIn('PROBE_TOOL_NAME', full,
                          'without filter the tool body should appear')
            # Clear the tree cache so item builders re-read the filter.
            self.r._TREE_CACHE.clear()
            self.r._DETAIL_LEVEL = 1
            filtered = ''.join(self.r.get_preview(('session', path)))
            self.assertIn('PROBE_VOICE_TEXT', filtered)
            self.assertNotIn('PROBE_TOOL_NAME', filtered,
                             'with filter the tool body must be hidden')
        finally:
            os.unlink(path)

    # ---- preview respects hidden ----------------------------------------

    def test_umbrella_preview_skips_hidden_children(self):
        # A ``('prompt', …)`` preview composes from its non-hidden children
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
            self.r._DETAIL_LEVEL = 1
            preview_filtered = ''.join(
                self.r._preview_umbrella(('prompt', path, 0)),
            )
            # Without the filter the tool's machinery would appear.
            self.r._DETAIL_LEVEL = 4
            preview_full = ''.join(
                self.r._preview_umbrella(('prompt', path, 0)),
            )
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

    def test_help_text_mentions_detail_level_keys(self):
        # The in-app ``1``-``4`` detail-level keys live in the intro
        # (shown by ``?``); the old ``.`` hotkey is gone.
        self.assertIn('1-4', self.r._HELP_INTRO,
                      'help intro should list the 1-4 detail-level keys')
        self.assertNotIn(' .  ', self.r._HELP_INTRO,
                         'the . hotkey should no longer appear')
        # The CLI flag lives in the usage block (shown only by ``--help``);
        # the removed show-all flags are gone. (Build the old flag name at
        # runtime so it doesn't linger as a literal in the source.)
        self.assertIn('--detail', self.r._HELP_USAGE_TMPL)
        self.assertNotIn('--' + 'show-all', self.r._HELP_USAGE_TMPL)
        # The flags block must NOT leak into the in-app intro.
        self.assertNotIn('Usage:', self.r._HELP_INTRO)
        # The old 'h' hotkey is gone from the help intro.
        self.assertNotIn(' h ', self.r._HELP_INTRO,
                         "the 'h' hotkey should no longer appear")

    def test_detail_level_actions_registered(self):
        # Sanity: the recipe registers the four '1'-'4' detail-level
        # actions and no longer a '.' (or 'h') one. We can't run the
        # full main() under unit test (it touches argv / stdin) —
        # inspect the source for the bindings.
        with open(_RECIPE) as f:
            source = f.read()
        for key in ('1', '2', '3', '4'):
            self.assertIn(f"Action('{key}',", source,
                          f"missing detail-level binding for '{key}'")
        self.assertIn('_set_detail_level', source)
        self.assertNotIn("Action('.',", source,
                         "the '.' toggle binding should be gone")
        self.assertNotIn('_action_toggle_filter', source,
                         "the old toggle handler should be gone")
        self.assertNotIn(
            "Action('h',", source,
            "the 'h' binding for the voice-only filter should be gone")

    def test_filter_voice_only_default_is_on(self):
        # Voice-only (detail level 1) is the default at module load;
        # --detail raises it (up to 4 = all).
        with open(_RECIPE) as f:
            source = f.read()
        self.assertIn('_DETAIL_LEVEL = 1', source)
        self.assertNotIn('_DETAIL_LEVEL = 4  #', source)

    def test_on_resize_drops_preview_cache(self):
        # #829: the recipe registers an on_resize handler that drops the
        # whole preview cache, so a pane-layout change (terminal resize OR
        # split/ratio — the broadened on_resize, #828) triggers a refetch
        # and ``get_preview`` re-lays width-dependent previews (md2ansi
        # tables / wrapped voice prose) at the new ctx.preview_width. We
        # can't run the full main() under unit test (it touches argv /
        # stdin — see test_dot_action_registered), so confirm (a) the
        # exact registration is present in source, and (b) that handler
        # shape actually calls ``drop_preview_cache()`` when invoked.
        with open(_RECIPE) as f:
            source = f.read()
        self.assertIn(
            'on_resize=lambda ctx, cols, rows: ctx.drop_preview_cache(),',
            source,
            'browse-claude must register on_resize -> drop_preview_cache '
            'so width-dependent previews refetch on a layout change')
        # Behavioural check on the registered handler shape: a spy ctx
        # records the drop call. (The end-to-end re-render is covered by
        # test/ui/test_recipe_browse_claude.py.)
        drops = []

        class _SpyCtx:
            def drop_preview_cache(self, id_=None):
                drops.append(id_)

        on_resize = lambda ctx, cols, rows: ctx.drop_preview_cache()
        on_resize(_SpyCtx(), 120, 40)
        self.assertEqual(
            drops, [None],
            'on_resize must drop the entire preview cache (id=None) so the '
            'framework re-fetches the cursor preview at the new width')


class TestRecordMinLevel(unittest.TestCase):
    """``_record_min_level`` classifies one record into a detail tier (1-4).

    Whitelist semantics: voice→1, lived machinery→2, curated metadata→3,
    everything unrecognised (and ``isMeta``) →4.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_voice_user_is_level_1(self):
        rec = {'type': 'user', 'message': {'role': 'user', 'content': 'hi'}}
        self.assertEqual(self.r._record_min_level(rec), 1)

    def test_voice_assistant_text_is_level_1(self):
        rec = {'type': 'assistant', 'message': {'role': 'assistant',
               'content': [{'type': 'text', 'text': 'done'}]}}
        self.assertEqual(self.r._record_min_level(rec), 1)

    def test_assistant_tool_use_is_level_2(self):
        rec = {'type': 'assistant', 'message': {'role': 'assistant',
               'content': [{'type': 'tool_use', 'id': 't', 'name': 'Bash',
                            'input': {'command': 'ls'}}]}}
        self.assertEqual(self.r._record_min_level(rec), 2)

    def test_user_tool_result_is_level_2(self):
        rec = {'type': 'user', 'message': {'role': 'user',
               'content': [{'type': 'tool_result', 'tool_use_id': 't',
                            'content': 'ok'}]}}
        self.assertEqual(self.r._record_min_level(rec), 2)

    def test_assistant_thinking_only_is_level_2(self):
        rec = {'type': 'assistant', 'message': {'role': 'assistant',
               'content': [{'type': 'thinking', 'thinking': 'hmm'}]}}
        self.assertEqual(self.r._record_min_level(rec), 2)

    def test_system_turn_duration_is_level_2(self):
        rec = {'type': 'system', 'subtype': 'turn_duration',
               'durationMs': 1000, 'messageCount': 3}
        self.assertEqual(self.r._record_min_level(rec), 2)

    def test_system_api_error_is_level_2(self):
        rec = {'type': 'system', 'subtype': 'api_error'}
        self.assertEqual(self.r._record_min_level(rec), 2)

    def test_l3_useful_plain_types_are_level_3(self):
        for t in ('summary', 'task-summary', 'last-prompt', 'pr-link',
                  'worktree-state', 'custom-title', 'tag', 'queue-operation',
                  'marble-origami-commit'):
            with self.subTest(type=t):
                # ``queue-operation`` with content is voice → seed it empty
                # so it classifies as plain metadata here.
                rec = {'type': t}
                self.assertEqual(self.r._record_min_level(rec), 3)

    def test_l3_useful_system_local_command_is_level_3(self):
        rec = {'type': 'system', 'subtype': 'local_command', 'content': 'ls'}
        self.assertEqual(self.r._record_min_level(rec), 3)

    def test_l3_useful_attachments_are_level_3(self):
        for sub in ('file', 'queued_command', 'hook_success', 'diagnostics'):
            with self.subTest(attachment=sub):
                rec = {'type': 'attachment', 'attachment': {'type': sub}}
                self.assertEqual(self.r._record_min_level(rec), 3)

    def test_unlisted_system_subtype_is_level_4(self):
        rec = {'type': 'system', 'subtype': 'hook'}
        self.assertEqual(self.r._record_min_level(rec), 4)

    def test_unlisted_attachment_subtype_is_level_4(self):
        rec = {'type': 'attachment', 'attachment': {'type': 'skill_listing'}}
        self.assertEqual(self.r._record_min_level(rec), 4)

    def test_unknown_type_is_level_4(self):
        self.assertEqual(self.r._record_min_level({'type': 'progress'}), 4)
        self.assertEqual(self.r._record_min_level({'type': 'totally-new'}), 4)

    def test_ismeta_record_is_level_4(self):
        # ``isMeta`` outranks even its own (otherwise-voice) type: an
        # injected-context user record is never part of the lived turn.
        rec = {'type': 'user', 'isMeta': True,
               'message': {'role': 'user', 'content': 'injected'}}
        self.assertEqual(self.r._record_min_level(rec), 4)

    def test_non_dict_record_is_level_4(self):
        self.assertEqual(self.r._record_min_level(None), 4)


class TestPassesFilterByLevel(unittest.TestCase):
    """``_passes_filter`` gates each row tag against ``_DETAIL_LEVEL``.

    A row shows iff its min level is ``<= _DETAIL_LEVEL``. Verified at all
    four levels for msg / tool / span / prompt / agent ids.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self.r._TREE_CACHE.clear()
        self._saved_level = self.r._DETAIL_LEVEL

    def tearDown(self):
        self.r._DETAIL_LEVEL = self._saved_level
        self.r._TREE_CACHE.clear()

    def _write_jsonl(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def _visible_at(self, item_id, td=None):
        """Tuple of bools — does ``item_id`` pass at levels 1,2,3,4?"""
        out = []
        for lvl in (1, 2, 3, 4):
            self.r._DETAIL_LEVEL = lvl
            out.append(self.r._passes_filter(item_id, td))
        return tuple(out)

    def test_msg_leaf_gating_across_levels(self):
        # line 0 voice user (1), line 1 assistant tool_use (2), line 2
        # user tool_result (2). Plus a level-3 metadata leaf and a
        # level-4 unknown leaf fabricated on a td.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'X', 'input': {}},
             ]}},
            {'type': 'user', 'uuid': 'u2',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'},
             ]}},
            {'type': 'tag', 'tag': 'release'},        # level 3
            {'type': 'progress', 'data': {}},          # level 4 (unknown)
        ])
        try:
            td = self.r._scan_tree(path)
            self.assertEqual(self._visible_at(('msg', path, 0), td),
                             (True, True, True, True))   # voice
            self.assertEqual(self._visible_at(('msg', path, 1), td),
                             (False, True, True, True))  # tool_use
            self.assertEqual(self._visible_at(('msg', path, 2), td),
                             (False, True, True, True))  # tool_result
            self.assertEqual(self._visible_at(('msg', path, 3), td),
                             (False, False, True, True)) # tag (L3)
            self.assertEqual(self._visible_at(('msg', path, 4), td),
                             (False, False, False, True))# unknown (L4)
        finally:
            os.unlink(path)

    def test_tool_umbrella_gating_bare_vs_subagent(self):
        # A bare Bash tool_use umbrella tracks its assistant min level
        # (2). An <tool:Agent> umbrella wrapping a resolvable subagent
        # is voice-promoted to level 1.
        path = self._write_jsonl([
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'tb', 'name': 'Bash',
                  'input': {'command': 'ls'}},
             ]}},
        ])
        try:
            td = self.r._scan_tree(path)
            # Bare Bash umbrella: hidden until level 2.
            self.assertEqual(self._visible_at(('tool', path, 1), td),
                             (False, True, True, True))
            # Subagent-wrap promotion: stub the predicate so the same
            # umbrella id reads as a resolvable-subagent wrap → level 1.
            saved = self.r._tool_umbrella_wraps_subagent
            try:
                self.r._tool_umbrella_wraps_subagent = lambda rec, td_: True
                self.assertEqual(self._visible_at(('tool', path, 1), td),
                                 (True, True, True, True))
            finally:
                self.r._tool_umbrella_wraps_subagent = saved
        finally:
            os.unlink(path)

    def test_span_umbrella_gating_by_min_member(self):
        # span 0: only an unlisted system + a tag (min level 3).
        # span 1: includes a voice user (min level 1).
        path = '/tmp/fake-span-levels.jsonl'
        td = self.r._TreeData()
        td.span_records[0] = [
            {'type': 'system', 'subtype': 'hook'},     # level 4
            {'type': 'tag', 'tag': 'x'},               # level 3
        ]
        td.span_records[1] = [
            {'type': 'system', 'subtype': 'hook'},     # level 4
            {'type': 'user', 'message': {'role': 'user', 'content': 'hi'}},
        ]
        self.r._TREE_CACHE[path] = td
        try:
            self.assertEqual(self._visible_at(('span', path, 0), td),
                             (False, False, True, True))  # min member = L3
            self.assertEqual(self._visible_at(('span', path, 1), td),
                             (True, True, True, True))     # has voice
        finally:
            self.r._TREE_CACHE.pop(path, None)

    def test_prompt_umbrella_always_visible(self):
        # Turn roots open at a user voice → visible at every level, even
        # when the underlying record is a pure tool_use.
        path = '/tmp/fake-prompt-levels.jsonl'
        td = self.r._TreeData()
        td.records = [{'type': 'assistant', 'message': {'role': 'assistant',
                       'content': [{'type': 'tool_use', 'id': 't',
                                    'name': 'X', 'input': {}}]}}]
        self.r._TREE_CACHE[path] = td
        try:
            self.assertEqual(self._visible_at(('prompt', path, 0), td),
                             (True, True, True, True))
        finally:
            self.r._TREE_CACHE.pop(path, None)

    def test_agent_umbrella_always_visible(self):
        # Subagent umbrellas are unconditionally shown at every level.
        self.assertEqual(self._visible_at(('agent', 'whatever', 'ABC')),
                         (True, True, True, True))


class TestShowId(unittest.TestCase):
    """``y`` (`_action_show_id`) pages the cursor's full id.

    A 1s flash can't be read or copied, so the action hands the full id
    to the pager (full-screen, selectable). Headless ``page`` would
    spawn a real pager, so we drive the handler directly with a fake
    ``ctx`` that records the ``page`` call.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_pages_cursor_id(self):
        paged = []

        cursor_id = ('prompt', '/proj/sess.jsonl', 5)

        class _Cursor:
            id = cursor_id

        class _Ctx:
            cursor = _Cursor()
            def page(self, text, lang=''):
                paged.append(text)

        self.r._action_show_id(_Ctx())
        # The id is a tuple, so ``str()`` is a real stringification (not
        # a no-op) — the pager receives ``str(id)``.
        self.assertEqual(paged, [str(cursor_id)])

    def test_no_cursor_is_noop(self):
        # The 'cursor' gate normally prevents this, but the internal
        # guard must also keep it a no-op (no page, no crash).
        paged = []

        class _Ctx:
            cursor = None
            def page(self, text, lang=''):
                paged.append(text)

        self.r._action_show_id(_Ctx())
        self.assertEqual(paged, [])

    def test_y_binding_wires_show_id_with_cursor_gate(self):
        # The 'y' action must keep its 'cursor' gate and paste-friendly
        # label while routing to the pager-backed handler.
        with open(_RECIPE) as f:
            source = f.read()
        self.assertIn(
            "Action('y',     'Show full id (paste-friendly)',"
            "   _action_show_id,     'cursor')",
            source,
        )
        # The handler pages the id (stringified for the pager); it must
        # not fall back to a flash.
        self.assertIn('ctx.page(str(ctx.cursor.id))', source)


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
                if kind == 'upsert':
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
                elif kind == 'set_preview':
                    # Mirror Browser.set_preview semantics: writes into
                    # ``Item.preview`` if the id is registered, otherwise
                    # silently no-op. Coerces None to ''.
                    _, id_, text = op
                    item = self.items_by_id.get(id_)
                    if item is None:
                        continue
                    item.preview = text if text is not None else ''
                    item.preview_render = None
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
        self._saved_FILTER = self.r._DETAIL_LEVEL
        self.r._TREE_MODE = True
        # These tests assert the full "show everything" baseline (incl.
        # non-voice tool rows), so pin the voice-only filter OFF — it's
        # on by default and would otherwise hide that content.
        self.r._DETAIL_LEVEL = 4

    def tearDown(self):
        self.r._BROWSER = self._saved_BROWSER
        self.r._TREE_MODE = self._saved_TREE_MODE
        self.r._DETAIL_LEVEL = self._saved_FILTER
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
            out = ''.join(self.r._preview_umbrella(('prompt', path, 0)))
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
            ''.join(self.r._preview_umbrella(('prompt', path, 0)))
            self.r._BROWSER.flush()
            first_pass_calls = list(counter['calls'])
            # First pass must have fetched both umbrellas.
            self.assertIn(('prompt', path, 0), first_pass_calls)
            self.assertIn(('tool', path, 1), first_pass_calls)

            ''.join(self.r._preview_umbrella(('prompt', path, 0)))
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

    def test_umbrella_does_not_populate_leaf_preview_cache(self):
        # Regression: previously, ``_preview_umbrella`` populated each
        # visited leaf's ``Item.preview`` as a side effect (via
        # set_preview_op). That cache was body-only, but a direct
        # leaf visit's preview is body + chrome — sharing the cache
        # slot between the two consumers caused chrome to bleed into
        # the umbrella OR strip from the leaf depending on visit
        # order. The umbrella no longer writes to the leaf cache; each
        # consumer manages its own.
        self.r._BROWSER = _FakeBrowser()
        path = self._make_three_record_session()
        try:
            ''.join(self.r._preview_umbrella(('prompt', path, 0)))
            self.r._BROWSER.flush()
            # Leaves get *registered* in the index via eager-push
            # upserts (so the framework's tree expansion is cheap),
            # but their ``preview`` slot stays None — populated only
            # by the framework's own worker when a direct cursor
            # visit calls ``get_preview(leaf_id)``.
            for n in (0, 1, 2):
                cid = ('msg', path, n)
                item = self.r._BROWSER.items_by_id.get(cid)
                self.assertIsNotNone(item, f'leaf {cid} not in index')
                self.assertFalse(
                    item.preview,
                    f'leaf {cid}.preview should be None — umbrella '
                    f'must not pollute the leaf cache. Got '
                    f'{item.preview!r}',
                )
        finally:
            os.unlink(path)

    def test_eager_push_one_update_data_call_per_invocation(self):
        # For a small descent (well under STREAM_BATCH=25 records),
        # the composer should accumulate upserts and flush them in a
        # single ``update_data`` call on the generator's ``finally:``
        # path.
        self.r._BROWSER = _FakeBrowser()
        path = self._make_three_record_session()
        try:
            ''.join(self.r._preview_umbrella(('prompt', path, 0)))
            self.assertEqual(
                len(self.r._BROWSER.update_data_calls), 1,
                'composer should flush exactly one update_data batch '
                'per top-level invocation when records < STREAM_BATCH; '
                f'got {len(self.r._BROWSER.update_data_calls)}',
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

    def test_tail_tick_re_renders_all_leaves_each_pass(self):
        # The umbrella deliberately renders each leaf fresh on every
        # compose — no leaf preview cache. The minor cost (one JSONL
        # read + body render per leaf per compose) is the price of
        # keeping the leaf cache uncluttered for direct visits.
        self.r._BROWSER = _FakeBrowser()
        path = self._make_three_record_session()

        render_calls = []
        original_render = self.r._render_record_with_rule

        def _counting_render(p, n):
            render_calls.append((p, n))
            return original_render(p, n)

        self.r._render_record_with_rule = _counting_render
        try:
            ''.join(self.r._preview_umbrella(('prompt', path, 0)))
            self.r._BROWSER.flush()
            calls_after_first = list(render_calls)
            self.assertEqual(
                len(calls_after_first), 3,
                f'first pass should render 3 leaves; got '
                f'{calls_after_first}',
            )

            render_calls.clear()
            ''.join(self.r._preview_umbrella(('prompt', path, 0)))
            self.r._BROWSER.flush()
            # Second pass: every leaf re-renders. There's no leaf
            # cache to hit.
            self.assertEqual(
                len(render_calls), 3,
                f'second pass should re-render all 3 leaves; got '
                f'{render_calls}',
            )
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
            ''.join(self.r._preview_umbrella(('prompt', path, 0)))
            self.r._BROWSER.flush()
            # First expand: the framework consults its cache via
            # cached_children. The recipe-side composer has populated
            # it; the framework would now skip the children-queue
            # fetch entirely.
            cached = self.r._BROWSER.cached_children(('prompt', path, 0))
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
                ''.join(self.r._preview_umbrella(('prompt', path, 0)))
                self.r._BROWSER.flush()
                self.assertNotIn(
                    ('prompt', path, 0), counter['calls'],
                    'second composition must not re-fetch '
                    'cached umbrella children',
                )
            finally:
                restore()
        finally:
            os.unlink(path)


class TestUmbrellaGenerator(unittest.TestCase):
    """#459: ``_preview_umbrella`` is a generator yielding chunks.

    Verifies:
      * First yield is the scope card (when ``_scope_card_path`` resolves).
      * Yields land in document order, one chunk per leaf.
      * Full drain reproduces the substrings the old non-generator
        implementation produced (sanity for the join-equivalence claim).
      * Partial drain past STREAM_BATCH followed by ``gen.close()``
        flushes the buffered side-effect ops via ``finally:``.
      * A short partial drain (< STREAM_BATCH) closed early still
        flushes via ``finally:``.
    """

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
        self.r._TREE_CACHE.clear()

    def _write_jsonl(self, records):
        import json as _json
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for r in records:
            f.write(_json.dumps(r) + '\n')
        f.close()
        return f.name

    def _make_session(self, n_turns):
        """Build a session with ``n_turns`` independent user-prompt records.

        Each record is a turn-root prompt — no nested tool calls — so
        every turn produces exactly one yielded leaf chunk under the
        session's umbrella cascade.
        """
        records = []
        for i in range(n_turns):
            records.append({
                'type': 'user', 'uuid': f'u{i}',
                'message': {'role': 'user',
                            'content': f'PROBE_TURN_{i:04d}'},
            })
        return self._write_jsonl(records)

    def test_yields_scope_card_first(self):
        # The session path resolves a scope-card path; the first yield
        # is the rendered scope card. Subsequent yields are leaves.
        self.r._BROWSER = None
        path = self._make_session(2)
        try:
            gen = self.r._preview_umbrella(('session', path))
            first = next(gen)
            # The scope card prints the recipe banner string;
            # leaf chunks never carry it.
            self.assertIn('browse-claude', first)
            # Trailing separator on every chunk.
            self.assertTrue(first.endswith('\n\n'))
            # Drain to release file handles / generator state.
            for _ in gen:
                pass
        finally:
            os.unlink(path)

    def test_yields_in_document_order(self):
        # Pull each chunk one at a time and confirm the leaf order
        # tracks the on-disk record order.
        self.r._BROWSER = None
        path = self._make_session(4)
        try:
            chunks = list(self.r._preview_umbrella(('session', path)))
            # First chunk is the scope card; the remaining ``n``
            # chunks should each carry exactly one PROBE_TURN_NNNN
            # marker in ascending order.
            leaf_chunks = [c for c in chunks if 'PROBE_TURN_' in c]
            self.assertEqual(
                len(leaf_chunks), 4,
                f'expected one leaf chunk per turn; got {len(leaf_chunks)}',
            )
            for i, chunk in enumerate(leaf_chunks):
                self.assertIn(f'PROBE_TURN_{i:04d}', chunk)
        finally:
            os.unlink(path)

    def test_full_drain_matches_substrings(self):
        # ``''.join(generator)`` over a small fixture should still
        # contain every body the old non-generator umbrella produced.
        self.r._BROWSER = None
        path = self._make_session(3)
        try:
            full = ''.join(self.r._preview_umbrella(('session', path)))
            for i in range(3):
                self.assertIn(f'PROBE_TURN_{i:04d}', full)
            # Scope card sits at the head.
            head, _, _ = full.partition('PROBE_TURN_0000')
            self.assertIn('browse-claude', head)
        finally:
            os.unlink(path)

    def test_partial_drain_then_close_flushes_eager_push_ops(self):
        # STREAM_BATCH=25 — build a session large enough that pulling
        # ~30 chunks crosses the batch boundary at least once. After
        # ``gen.close()`` the ``finally:`` flush should have posted the
        # eager-push upserts. (Leaf preview cache writes are NOT a
        # side-effect any more; only the upserts that register leaves
        # in the framework's tree remain.)
        self.r._BROWSER = _FakeBrowser()
        path = self._make_session(60)
        try:
            gen = self.r._preview_umbrella(('session', path))
            next(gen)   # scope card
            pulled = 0
            for _ in range(30):
                try:
                    next(gen)
                    pulled += 1
                except StopIteration:
                    break
            gen.close()
            self.r._BROWSER.flush()
            # More than one ``update_data`` batch fired — the
            # mid-stream STREAM_BATCH flush and the ``finally:`` flush
            # are distinct posts.
            self.assertGreaterEqual(
                len(self.r._BROWSER.update_data_calls), 2,
                'expected at least two update_data batches '
                '(mid-stream + finally flush)',
            )
            # The batches contain upserts only — no set_preview ops.
            for ops in self.r._BROWSER.update_data_calls:
                for op in ops:
                    self.assertNotEqual(
                        op[0], 'set_preview',
                        'umbrella must not emit set_preview ops for '
                        'leaves — they would clobber direct-visit '
                        'previews. Got: %r' % (op,),
                    )

        finally:
            os.unlink(path)

    def test_finally_flushes_remainder(self):
        # A short partial drain (well under STREAM_BATCH=25) should
        # still see its eager-push upserts flushed via the
        # ``finally:`` path on ``gen.close()``.
        self.r._BROWSER = _FakeBrowser()
        path = self._make_session(10)
        try:
            gen = self.r._preview_umbrella(('session', path))
            next(gen)   # scope card
            for _ in range(5):
                next(gen)
            gen.close()
            self.r._BROWSER.flush()
            # At least one update_data batch fired (the finally flush).
            self.assertGreaterEqual(
                len(self.r._BROWSER.update_data_calls), 1,
                'expected at least one update_data batch (finally flush)',
            )
            # No set_preview ops should appear.
            for ops in self.r._BROWSER.update_data_calls:
                for op in ops:
                    self.assertNotEqual(op[0], 'set_preview')
        finally:
            os.unlink(path)


class TestAskUserQuestion(unittest.TestCase):
    """``AskUserQuestion`` renders the request *and* the reply through a
    shared markdown formatter, and both halves count as voice."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _input(self, multi=False):
        return {'questions': [{
            'question': 'How should the batch API be shaped?',
            'header': 'API shape',
            'multiSelect': multi,
            'options': [
                {'label': 'Add preview ops to update_data',
                 'description': 'Op tuples set_preview/append_preview.',
                 'preview': 'ops = [...]\nb.update_data(ops)'},
                {'label': 'Add plural convenience methods',
                 'description': 'set_previews({id: text}) etc.'},
                {'label': 'Both: ops + plural wrappers',
                 'description': 'Recipes pick what reads best.'},
            ],
        }]}

    def _tur(self, answer, multi=False, annotation=None):
        inp = self._input(multi=multi)
        q = inp['questions'][0]['question']
        return {
            'questions': inp['questions'],
            'answers': {q: answer},
            'annotations': ({q: {'preview': annotation}} if annotation else {}),
        }

    def test_tool_use_renders_question_and_options(self):
        out = self.r._render_assistant({
            'type': 'assistant',
            'message': {'role': 'assistant', 'content': [{
                'type': 'tool_use', 'id': 'tu', 'name': 'AskUserQuestion',
                'input': self._input(),
            }]},
        })
        self.assertIn('🔧 AskUserQuestion', out)
        # Header chip prefix + the full question text are present.
        self.assertIn('API shape:', out)
        self.assertIn('How should the batch API be shaped?', out)
        # Every option label is present, none is marked as chosen.
        self.assertIn('Add preview ops to update_data', out)
        self.assertIn('Add plural convenience methods', out)
        self.assertIn('Both: ops + plural wrappers', out)
        self.assertIn('◯', out)
        self.assertNotIn('●', out)
        # Numbered list + preview code fence both appear.
        self.assertIn('1.', out)
        self.assertIn('```', out)
        self.assertIn('ops = [...]', out)

    def test_tool_use_multiselect_uses_checkboxes(self):
        out = self.r._render_assistant({
            'type': 'assistant',
            'message': {'role': 'assistant', 'content': [{
                'type': 'tool_use', 'name': 'AskUserQuestion',
                'input': self._input(multi=True),
            }]},
        })
        self.assertIn('☐', out)
        self.assertNotIn('☑', out)

    def test_tool_result_marks_chosen_option(self):
        out = self.r._render_user({
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'tu', 'content': '',
            }]},
            'toolUseResult': self._tur('Add preview ops to update_data'),
        })
        # Chosen option carries the filled glyph; the others keep ◯.
        self.assertIn('● **Add preview ops to update_data**', out)
        self.assertIn('◯ **Add plural convenience methods**', out)
        self.assertIn('◯ **Both: ops + plural wrappers**', out)

    def test_tool_result_multiselect_marks_each_chosen(self):
        out = self.r._render_user({
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'tu', 'content': '',
            }]},
            'toolUseResult': self._tur(
                ['Add preview ops to update_data', 'Both: ops + plural wrappers'],
                multi=True),
        })
        self.assertIn('☑ **Add preview ops to update_data**', out)
        self.assertIn('☐ **Add plural convenience methods**', out)
        self.assertIn('☑ **Both: ops + plural wrappers**', out)

    def test_tool_result_other_answer_surfaced(self):
        out = self.r._render_user({
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'tu', 'content': '',
            }]},
            'toolUseResult': self._tur('Update the recipe to batch previews.'),
        })
        # No option was selected — all stay ◯ — and the custom answer is
        # surfaced separately.
        self.assertNotIn('●', out)
        self.assertIn('**Other:** "Update the recipe to batch previews."', out)

    def test_is_voice_assistant_question(self):
        rec = {
            'type': 'assistant',
            'message': {'role': 'assistant', 'content': [{
                'type': 'tool_use', 'name': 'AskUserQuestion',
                'input': self._input(),
            }]},
        }
        self.assertTrue(self.r._is_voice(rec))

    def test_is_voice_user_reply(self):
        rec = {
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'tu', 'content': '',
            }]},
            'toolUseResult': self._tur('Add preview ops to update_data'),
        }
        self.assertTrue(self.r._is_voice(rec))

    def test_is_voice_unrelated_tool_use_still_machinery(self):
        # Sanity check: a non-AskUserQuestion tool_use is NOT voice.
        rec = {
            'type': 'assistant',
            'message': {'role': 'assistant', 'content': [{
                'type': 'tool_use', 'name': 'Bash',
                'input': {'command': 'ls'},
            }]},
        }
        self.assertFalse(self.r._is_voice(rec))

    def test_is_voice_unrelated_tool_result_still_machinery(self):
        rec = {
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'tu', 'content': 'ok',
            }]},
            'toolUseResult': {'stdout': 'ok', 'stderr': '',
                              'interrupted': False, 'isImage': False,
                              'noOutputExpected': False},
        }
        self.assertFalse(self.r._is_voice(rec))


class TestOnExpandJumpComposition(unittest.TestCase):
    """``_on_expand`` + ``_on_children_loaded`` jump-to-latest-voice.

    The right-arrow expand half of the old ``_action_tree_right`` moved
    onto the ``on_expand`` / ``on_children_loaded`` lifecycle hooks so
    the post-expand jump fires for EVERY expansion source, not just the
    keyboard. The composition splits on whether the expanded node's
    children are cached at fire time:

      * cached  → ``_on_expand`` jumps to the latest voice immediately;
      * uncached→ ``_on_expand`` parks the id and ``_on_children_loaded``
        performs the jump once the async fetch settles.

    These tests drive the recipe's hook callbacks directly against a
    fake ctx (recording ``cursor_to`` + serving a controllable
    ``cached_children``) and a real on-disk session so the module-level
    ``get_children`` / ``_latest_voice_among_children`` resolve. The
    end-to-end keyboard path is covered by the tmux UI suite
    (``test/ui/test_recipe_browse_claude.py``).
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        # Isolate the module-level pending set across tests.
        self.r._AWAITING_VOICE_JUMP.clear()
        self.addCleanup(self.r._AWAITING_VOICE_JUMP.clear)
        self.r._TREE_CACHE.clear()
        self.addCleanup(self.r._TREE_CACHE.clear)
        # Tree mode is the default; pin it so the fixture's turn-root
        # children resolve the same way regardless of test order.
        self._saved_tree_mode = self.r._TREE_MODE
        self.r._TREE_MODE = True
        self.addCleanup(setattr, self.r, '_TREE_MODE', self._saved_tree_mode)
        # ``_invalidate_if_cross_file`` reads the module ``_BROWSER``;
        # install a recorder so we can assert the cross-file upgrade.
        self._saved_browser = self.r._BROWSER
        self.addCleanup(setattr, self.r, '_BROWSER', self._saved_browser)

    class _AllExpanded:
        """Stand-in for ``state.expanded`` where every id is expanded.

        Default for tests that don't exercise the collapse-before-settle
        guard, so ``_on_children_loaded``'s still-expanded check passes.
        """

        def __contains__(self, _id):
            return True

    def _make_ctx(self, *, cached, expanded=None):
        """Fake ctx recording ``cursor_to`` + serving ``cached_children``.

        ``cached`` maps an id to the value ``cached_children`` should
        return (``None`` = fetch in flight, a list = available).
        ``expanded`` is the set exposed as ``ctx.state.expanded`` (the
        recipe's still-expanded guard reads it); ``None`` means "treat
        everything as expanded". Also installs a recording ``_BROWSER``
        so cross-file invalidation is observable via ``invalidated``.
        """
        cursor_to_calls = []
        invalidated = []
        expanded_set = self._AllExpanded() if expanded is None else expanded

        class _FakeBrowser:
            def invalidate_preview(self, id_):
                invalidated.append(id_)

        self.r._BROWSER = _FakeBrowser()

        class _State:
            expanded = expanded_set

        class _Ctx:
            state = _State()

            def cached_children(self, id_):
                val = cached.get(id_, None)
                return None if val is None else list(val)

            def cursor_to(self, id_):
                cursor_to_calls.append(id_)

        return _Ctx(), cursor_to_calls, invalidated

    def _write_session(self, tmp):
        """A 2-voice turn: user FIRST_VOICE, a tool_use, asst LAST_VOICE.

        Tree-mode listing for ``('prompt', sess, 0)`` is::

            #0 user FIRST_VOICE | #1 tool_use | #2 asst LAST_VOICE

        so ``_latest_voice_among_children(('prompt', sess, 0))`` is the
        final assistant text leaf, ``('msg', sess, 2)`` — the deterministic
        jump target used by these tests.
        """
        import json as _json
        sess = os.path.join(tmp, 's.jsonl')
        recs = [
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'FIRST_VOICE'}},
            {'type': 'assistant', 'uuid': 'a1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'name': 'Bash',
                  'input': {'command': 'x'}}]}},
            {'type': 'assistant', 'uuid': 'a2',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'text', 'text': 'LAST_VOICE'}]}},
        ]
        with open(sess, 'w') as f:
            for r in recs:
                f.write(_json.dumps(r) + '\n')
        return sess

    def test_uncached_expand_defers_then_children_loaded_jumps(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._write_session(tmp)
            turn = ('prompt', sess, 0)
            # Children NOT cached at expand time → defer, no jump yet.
            ctx, cursor_to_calls, _ = self._make_ctx(cached={turn: None})
            self.r._on_expand(ctx, [turn])
            self.assertEqual(cursor_to_calls, [],
                             'uncached expand must not jump immediately')
            self.assertIn(turn, self.r._AWAITING_VOICE_JUMP,
                          'uncached expand must park the id as pending')
            # Fetch settles → the children-loaded hook performs the jump
            # and clears the pending entry.
            self.r._on_children_loaded(ctx, [turn])
            self.assertEqual(cursor_to_calls, [('msg', sess, 2)],
                             'children-loaded must jump to the latest voice')
            self.assertNotIn(turn, self.r._AWAITING_VOICE_JUMP,
                             'pending id must be discarded after the jump')

    def test_cached_expand_jumps_immediately(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._write_session(tmp)
            turn = ('prompt', sess, 0)
            # Children already cached (non-None) → jump now, no defer.
            ctx, cursor_to_calls, _ = self._make_ctx(
                cached={turn: ['sentinel']})
            self.r._on_expand(ctx, [turn])
            self.assertEqual(cursor_to_calls, [('msg', sess, 2)],
                             'cached expand must jump to the latest voice now')
            self.assertNotIn(turn, self.r._AWAITING_VOICE_JUMP,
                             'cached expand must not park a pending id')

    def test_children_loaded_for_non_pending_parent_does_not_jump(self):
        # A settlement that the recipe never deferred (a background
        # refresh / tail refetch of an already-expanded parent) must
        # NOT yank the cursor.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._write_session(tmp)
            turn = ('prompt', sess, 0)
            ctx, cursor_to_calls, _ = self._make_ctx(cached={turn: ['x']})
            # _AWAITING_VOICE_JUMP is empty (cleared in setUp).
            self.r._on_children_loaded(ctx, [turn])
            self.assertEqual(cursor_to_calls, [],
                             'children-loaded for a non-deferred parent '
                             'must not move the cursor')

    def test_collapse_before_settle_discards_without_jumping(self):
        # Expand an UNCACHED umbrella (id parked), then collapse it
        # before the async fetch settles. The framework still fires
        # on_children_loaded when the fetch lands (settlement is
        # unconditional). The hook must NOT jump onto a now-hidden
        # child — it must drop the stale entry and skip the cursor_to.
        # Without the still-expanded guard this jumps onto a hidden row
        # (sticky anchor); without unconditional discard the stale id
        # would also leak.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._write_session(tmp)
            turn = ('prompt', sess, 0)
            # Expanded set starts with the turn (it was just expanded).
            expanded = {turn}
            ctx, cursor_to_calls, _ = self._make_ctx(
                cached={turn: None}, expanded=expanded)
            # Uncached expand → parked, no jump yet.
            self.r._on_expand(ctx, [turn])
            self.assertEqual(cursor_to_calls, [])
            self.assertIn(turn, self.r._AWAITING_VOICE_JUMP)
            # User collapses the turn before its fetch delivers.
            expanded.discard(turn)
            # Fetch settles → framework fires on_children_loaded anyway.
            self.r._on_children_loaded(ctx, [turn])
            # No jump (parent no longer expanded), and the entry is gone.
            self.assertEqual(cursor_to_calls, [],
                             'must not jump onto a child of a collapsed row')
            self.assertNotIn(turn, self.r._AWAITING_VOICE_JUMP,
                             'stale pending id must be discarded even when '
                             'the jump is skipped')

    def test_cross_file_id_invalidated_on_expand(self):
        # Expanding a cross-file row (session / agent) drops its cached
        # preview so it upgrades from the metadata card to the heavy
        # umbrella cascade; non-cross-file ids are left alone.
        session_id = ('session', '/x/s.jsonl')
        agent_id = ('agent', '/x/s.jsonl', 'AB')
        prompt_id = ('prompt', '/x/s.jsonl', 0)
        ctx, _, invalidated = self._make_ctx(
            cached={session_id: ['k'], agent_id: ['k'], prompt_id: ['k']})
        self.r._on_expand(ctx, [session_id, agent_id, prompt_id])
        self.assertEqual(invalidated, [session_id, agent_id],
                         'only cross-file ids should be invalidated')

    def test_jump_no_voice_among_children_is_noop(self):
        # _jump_to_latest_voice no-ops when there is no voice at this
        # level: a bare leaf id has no children, so the latest-voice
        # lookup returns None and no cursor_to is issued.
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as tmp:
            sess = os.path.join(tmp, 'm.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1',
                 'message': {'role': 'user', 'content': 'V'}},
            ]
            with open(sess, 'w') as f:
                for r in recs:
                    f.write(_json.dumps(r) + '\n')
            ctx, cursor_to_calls, _ = self._make_ctx(cached={})
            # A bare message-leaf id resolves to no children at all.
            self.r._jump_to_latest_voice(ctx, ('msg', sess, 0))
            self.assertEqual(cursor_to_calls, [])

    def test_jump_skips_when_latest_voice_is_self(self):
        # If the only voice among children is the row itself (latest ==
        # item_id), the guard skips the redundant cursor_to.
        ctx, cursor_to_calls, _ = self._make_ctx(cached={})
        saved = self.r._latest_voice_among_children
        self.r._latest_voice_among_children = lambda _id: 'SAME'
        self.addCleanup(setattr, self.r,
                        '_latest_voice_among_children', saved)
        self.r._jump_to_latest_voice(ctx, 'SAME')
        self.assertEqual(cursor_to_calls, [])


class TestOnScopeChangeCrossFileUpgrade(unittest.TestCase):
    """``_on_scope_change`` upgrades a cross-file scope_root's preview.

    The cross-file preview invalidation that the deleted ``alt-down``
    override (``_action_scope_down``) appended after its inline
    ``scope_into`` now lives on the ``on_scope_change`` lifecycle hook,
    gated on ``direction == 'in'``. So it fires for EVERY scope-in source
    — alt-down, programmatic ``ctx.scope_into``, startup ``initial_scope``
    — and only drops the cache for cross-file ids (bare ``.jsonl`` /
    ``('agent', …)``), leaving in-file scope rows on their already-heavy
    preview. Scope-OUT must NOT invalidate.

    These drive the hook directly against a recorder ``_BROWSER``; the
    end-to-end programmatic scope-in (``--pid``) preview upgrade is
    covered by the tmux UI suite (``test/ui/test_recipe_browse_claude``).
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self._saved_browser = self.r._BROWSER
        self.addCleanup(setattr, self.r, '_BROWSER', self._saved_browser)

    def _install_recorder(self):
        invalidated = []

        class _FakeBrowser:
            def invalidate_preview(self, id_):
                invalidated.append(id_)

        self.r._BROWSER = _FakeBrowser()
        return invalidated

    def test_scope_in_cross_file_invalidates(self):
        # Scope into a ('session', …) row → cache dropped so the next
        # preview pass upgrades to the heavy umbrella cascade.
        invalidated = self._install_recorder()
        session_id = ('session', '/x/s.jsonl')
        self.r._on_scope_change(None, session_id, None, 'in')
        self.assertEqual(invalidated, [session_id])

    def test_scope_in_agent_id_invalidates(self):
        # ``('agent', …)`` rows are cross-file too.
        invalidated = self._install_recorder()
        agent_id = ('agent', '/x/s.jsonl', 'AB')
        self.r._on_scope_change(None, agent_id, ('project', '/x'), 'in')
        self.assertEqual(invalidated, [agent_id])

    def test_scope_in_in_file_id_does_not_invalidate(self):
        # An in-file scope row (``prompt`` / ``msg``) is not cross-file:
        # it already renders the heavy cascade, so leave its cache alone.
        invalidated = self._install_recorder()
        self.r._on_scope_change(None, ('prompt', '/x/s.jsonl', 0),
                                ('session', '/x/s.jsonl'), 'in')
        self.assertEqual(invalidated, [])

    def test_scope_out_does_not_invalidate(self):
        # Scope-OUT never invalidates — even when the id scoped out TO is
        # itself a cross-file id, it was already upgraded when scoped in.
        invalidated = self._install_recorder()
        self.r._on_scope_change(None, ('session', '/x/s.jsonl'),
                                ('prompt', '/x/s.jsonl', 0), 'out')
        self.assertEqual(invalidated, [],
                         'scope-out must not invalidate the preview')

    def test_scope_out_to_root_does_not_invalidate(self):
        # Scoping out to root (``scope_id is None``) is also a no-op.
        invalidated = self._install_recorder()
        self.r._on_scope_change(None, None, '/x/s.jsonl', 'out')
        self.assertEqual(invalidated, [])


class TestSendMessage(unittest.TestCase):
    """Outbound ``SendMessage`` (leader → worker): the assistant tool_use
    is the message, its ``{success, message}`` tool_result is a delivery
    ack. Both render as agent voice, distinct from human / assistant."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _input(self, recipient='worker-7', summary='Address review nits',
               message='Please fix the **three** findings.\n\nDetails here.',
               with_to=False, with_content=False):
        inp = {'recipient': recipient, 'summary': summary, 'message': message}
        if with_to:
            inp.pop('recipient', None)
            inp['to'] = recipient
        if with_content:
            inp.pop('message', None)
            inp['content'] = message
        return inp

    def _send_rec(self, **kw):
        return {
            'type': 'assistant',
            'message': {'role': 'assistant', 'content': [{
                'type': 'tool_use', 'id': 'tu1', 'name': 'SendMessage',
                'input': self._input(**kw),
            }]},
        }

    def _ack_rec(self, success=True,
                 message=('Agent "worker-7" had no active task; resumed from '
                          'transcript in the background with your message. '
                          "You'll be notified when it finishes. "
                          'Output: /tmp/claude-1001/proj/sess/tasks/worker-7.output')):
        return {
            'type': 'user',
            'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'tu1', 'content': '',
            }]},
            'toolUseResult': {'success': success, 'message': message},
        }

    # -- _fmt_tool_use_send_message (full preview) -------------------------

    def test_fmt_send_header_and_markdown(self):
        out = self.r._fmt_tool_use_send_message(self._input())
        self.assertIn('→ worker-7', out)
        self.assertIn('Address review nits', out)
        # message body rendered (md2ansi unavailable in tests → raw md).
        self.assertIn('Please fix the **three** findings.', out)
        self.assertIn('Details here.', out)

    def test_fmt_send_to_content_fallbacks(self):
        out = self.r._fmt_tool_use_send_message(
            self._input(with_to=True, with_content=True))
        self.assertIn('→ worker-7', out)
        self.assertIn('Address review nits', out)
        self.assertIn('Please fix the **three** findings.', out)

    # -- dispatch through the assistant renderer ---------------------------

    def test_render_assistant_dispatches_send_message(self):
        out = self.r._render_assistant(self._send_rec())
        self.assertIn('🔧 SendMessage', out)
        self.assertIn('→ worker-7', out)
        self.assertIn('Address review nits', out)
        self.assertIn('Please fix the **three** findings.', out)

    def test_send_message_registered_in_formatters(self):
        self.assertIn('SendMessage', self.r._TOOL_USE_FORMATTERS)

    # -- one-liner ---------------------------------------------------------

    def test_tool_use_one_line_send(self):
        line = self.r._tool_use_one_line('SendMessage', self._input())
        self.assertEqual(line, '→ worker-7: Address review nits')

    def test_tool_use_one_line_send_to_fallback(self):
        line = self.r._tool_use_one_line(
            'SendMessage', self._input(with_to=True))
        self.assertEqual(line, '→ worker-7: Address review nits')

    def test_summarise_message_send(self):
        # The assistant-with-only-tool_use path routes through the
        # one-liner: 🔧 SendMessage(→ worker-7: Address review nits).
        out = self.r._summarise_message(self._send_rec())
        self.assertIn('🔧 SendMessage', out)
        self.assertIn('→ worker-7: Address review nits', out)

    # -- ack: _fmt_tur_send_message ---------------------------------------

    def test_fmt_tur_send_success_delivered(self):
        # The success ``message`` ("Message sent to X's inbox" / queued …) is
        # generic plumbing — the success line is just ``✓ delivered``; real
        # content (when present) comes from the ``routing`` block instead.
        out = self.r._fmt_tur_send_message(
            {'success': True, 'message': 'queued the message. '
             'Output: /tmp/claude/x/tasks/w.output'})
        self.assertIn('✓ delivered', out)
        # No plumbing leaks (neither the Output: path nor the generic text).
        self.assertNotIn('/tmp/claude/x/tasks/w.output', out)
        self.assertNotIn('queued the message', out)

    def test_fmt_tur_send_failure(self):
        out = self.r._fmt_tur_send_message(
            {'success': False, 'message': 'no such agent'})
        self.assertIn('✗', out)
        self.assertIn('no such agent', out)
        self.assertNotIn('delivered', out)

    def test_fmt_tur_send_failure_trims_output_path(self):
        # The error message still tails with an Output: /tmp path — trimmed.
        out = self.r._fmt_tur_send_message(
            {'success': False, 'message': "No agent named 'x'. "
             'Output: /tmp/claude/x/tasks/x.output'})
        self.assertIn("No agent named 'x'.", out)
        self.assertNotIn('/tmp/claude/x/tasks/x.output', out)

    def test_fmt_tur_send_success_routing_renders_content(self):
        # Success + routing block: status line, sender→target, summary, and
        # the delivered content rendered ONCE (never a raw JSON dump).
        out = self.r._fmt_tur_send_message({
            'success': True,
            'message': "Message sent to worker's inbox",
            'routing': {
                'sender': 'team-lead', 'target': '@worker',
                'targetColor': 'red', 'summary': 'do the thing',
                'content': 'Please do the thing now.',
            },
        })
        self.assertIn('✓ delivered', out)
        self.assertIn('team-lead → @worker', out)
        self.assertIn('do the thing', out)
        self.assertIn('Please do the thing now.', out)
        # Not a raw JSON dump of the routing block.
        self.assertNotIn('"routing"', out)
        self.assertNotIn("'content'", out)

    def test_fmt_tur_send_routing_json_content_colored(self):
        # When the delivered content is itself JSON, it's JSON-colored
        # (pretty-printed) rather than markdown-rendered.
        out = self.r._fmt_tur_send_message({
            'success': True, 'message': 'sent',
            'routing': {'sender': 'a', 'target': '@b',
                        'content': '{"k": 1, "v": "x"}'},
        })
        self.assertIn('"k"', out)
        self.assertIn('"v"', out)

    # -- routing through _fmt_tool_use_result ------------------------------

    def test_tool_use_result_routes_ack_by_key_set(self):
        out = self.r._fmt_tool_use_result(
            {'success': True, 'message': 'delivered it. '
             'Output: /tmp/a/b/tasks/x.output'}, '')
        self.assertIn('✓ delivered', out)
        self.assertNotIn('/tmp/a/b/tasks/x.output', out)

    def test_tool_use_result_routes_success_routing_shape(self):
        # The 3-key success shape ({success, message, routing}) must route to
        # the SendMessage formatter, NOT fall through to the raw-JSON default.
        out = self.r._fmt_tool_use_result({
            'success': True, 'message': "Message sent to w's inbox",
            'routing': {'sender': 's', 'target': '@w',
                        'content': 'hello there'},
        }, '')
        self.assertIn('✓ delivered', out)
        self.assertIn('hello there', out)
        self.assertNotIn('"routing"', out)

    def test_skill_ack_not_shadowed_by_send(self):
        # A skill ack carries commandName alongside success — it must
        # still route to the skill formatter, not the SendMessage one.
        out = self.r._fmt_tool_use_result(
            {'commandName': 'verify', 'success': True}, '')
        self.assertIn('verify', out)
        self.assertNotIn('delivered', out)

    def test_render_tool_result_send_ack(self):
        out = self.r._render_user(self._ack_rec())
        self.assertIn('✓ delivered', out)
        self.assertNotIn('worker-7.output', out)

    # -- _is_voice ---------------------------------------------------------

    def test_is_voice_send_message(self):
        self.assertTrue(self.r._is_voice(self._send_rec()))

    def test_is_voice_send_ack_is_machinery(self):
        # The delivery ack is a status receipt, not voice.
        self.assertFalse(self.r._is_voice(self._ack_rec()))

    # -- _kind_of ----------------------------------------------------------

    def test_kind_of_agent_send(self):
        self.assertEqual(self.r._kind_of(self._send_rec()), 'agent-send')

    def test_kind_of_plain_assistant_unchanged(self):
        rec = {'type': 'assistant',
               'message': {'role': 'assistant',
                           'content': [{'type': 'text', 'text': 'hi'}]}}
        self.assertEqual(self.r._kind_of(rec), 'assistant')

    def test_kind_of_bash_tool_use_unchanged(self):
        rec = {'type': 'assistant',
               'message': {'role': 'assistant', 'content': [{
                   'type': 'tool_use', 'name': 'Bash',
                   'input': {'command': 'ls'}}]}}
        self.assertEqual(self.r._kind_of(rec), 'assistant')

    # -- stripe / tag style registration -----------------------------------

    def test_agent_send_stripe_and_tag_style_registered(self):
        self.assertEqual(self.r._ROW_BG_FOR_KIND.get('agent-send'), 17)
        self.assertIn('agent-send', self.r._TAG_STYLE_FOR_KIND)


class TestTaskNotification(unittest.TestCase):
    """Inbound task-notification reply (worker → leader): a ``user``
    record whose text content is a ``<task-notification>`` wrapper. It
    must render as agent voice (its own ``agent-reply`` stripe), not be
    mis-attributed to the human."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    _NOTIFY = (
        '<task-notification>\n'
        '<task-id>a4276aa253ce276ab</task-id>\n'
        '<tool-use-id>toolu_01Lha6wR4Xgnc21wKyJeM2E9</tool-use-id>\n'
        '<output-file>/tmp/claude-1001/proj/sess/tasks/a4276aa253ce276ab.output'
        '</output-file>\n'
        '<status>completed</status>\n'
        '<summary>Agent "Implement ticket #616" completed</summary>\n'
        '<result>All **three** findings addressed.\n\n'
        '## Report\n\nDetails here.</result>\n'
        '</task-notification>'
    )

    def _notify_rec(self, text=None):
        """A user record carrying the notification as a bare string."""
        return {
            'type': 'user',
            'message': {'role': 'user',
                        'content': self._NOTIFY if text is None else text},
        }

    def _notify_rec_list(self, text=None):
        """Same, but content as a single text part (list shape)."""
        return {
            'type': 'user',
            'message': {'role': 'user', 'content': [
                {'type': 'text', 'text': self._NOTIFY if text is None else text},
            ]},
        }

    def _human_rec(self):
        return {
            'type': 'user',
            'message': {'role': 'user', 'content': 'Just a normal prompt.'},
        }

    # -- _parse_task_notification ------------------------------------------

    def test_parse_all_fields(self):
        d = self.r._parse_task_notification(self._NOTIFY)
        self.assertEqual(d['task_id'], 'a4276aa253ce276ab')
        self.assertEqual(d['tool_use_id'], 'toolu_01Lha6wR4Xgnc21wKyJeM2E9')
        self.assertEqual(
            d['output_file'],
            '/tmp/claude-1001/proj/sess/tasks/a4276aa253ce276ab.output')
        self.assertEqual(d['status'], 'completed')
        self.assertEqual(d['summary'], 'Agent "Implement ticket #616" completed')
        # result kept as raw markdown (not stripped/rendered).
        self.assertIn('All **three** findings addressed.', d['result'])
        self.assertIn('## Report', d['result'])

    def test_parse_missing_tags_tolerant(self):
        d = self.r._parse_task_notification(
            '<task-notification><status>completed</status>'
            '</task-notification>')
        self.assertEqual(d['status'], 'completed')
        # Absent tags resolve to '' (not a KeyError / None surprise).
        self.assertEqual(d['task_id'], '')
        self.assertEqual(d['summary'], '')
        self.assertEqual(d['result'], '')

    # -- _is_task_notification ---------------------------------------------

    def test_is_task_notification_string(self):
        self.assertTrue(self.r._is_task_notification(self._notify_rec()))

    def test_is_task_notification_list(self):
        self.assertTrue(self.r._is_task_notification(self._notify_rec_list()))

    def test_is_task_notification_human_false(self):
        self.assertFalse(self.r._is_task_notification(self._human_rec()))

    def test_is_task_notification_leading_whitespace(self):
        # lstrip() before the prefix check.
        self.assertTrue(
            self.r._is_task_notification(self._notify_rec('\n   ' + self._NOTIFY)))

    # -- _kind_of ----------------------------------------------------------

    def test_kind_of_agent_reply(self):
        self.assertEqual(self.r._kind_of(self._notify_rec()), 'agent-reply')

    def test_kind_of_human_user_unchanged(self):
        self.assertEqual(self.r._kind_of(self._human_rec()), 'user')

    # -- _is_voice (unchanged path) ----------------------------------------

    def test_is_voice_task_notification(self):
        self.assertTrue(self.r._is_voice(self._notify_rec()))

    # -- stripe / tag style registration -----------------------------------

    def test_agent_reply_stripe_matches_assistant(self):
        # Inter-agent voice shares the assistant stripe (17), NOT human's 235.
        kind = self.r._kind_of(self._notify_rec())
        self.assertEqual(self.r._ROW_BG_FOR_KIND.get(kind), 17)
        self.assertIn('agent-reply', self.r._TAG_STYLE_FOR_KIND)

    def test_human_user_stripe_still_235(self):
        kind = self.r._kind_of(self._human_rec())
        self.assertEqual(self.r._ROW_BG_FOR_KIND.get(kind), 235)

    # -- _summarise_message ------------------------------------------------

    def test_summarise_one_liner(self):
        out = self.r._summarise_message(self._notify_rec())
        self.assertIn('←', out)
        self.assertIn('a4276aa253ce276ab', out)
        self.assertIn('completed', out)
        self.assertIn('Agent "Implement ticket #616" completed', out)
        # Must NOT dump the raw XML wrapper.
        self.assertNotIn('<task-notification>', out)
        self.assertNotIn('<result>', out)

    def test_summarise_human_unchanged(self):
        out = self.r._summarise_message(self._human_rec())
        self.assertIn('Just a normal prompt.', out)

    # -- full preview (_render_user) ---------------------------------------

    def test_full_preview_header_and_markdown(self):
        out = self.r._render_user(self._notify_rec())
        self.assertIn('completed', out)
        self.assertIn('Agent "Implement ticket #616" completed', out)
        self.assertIn('a4276aa253ce276ab.output', out)
        # result rendered (md2ansi unavailable in tests → raw md).
        self.assertIn('All **three** findings addressed.', out)
        self.assertIn('## Report', out)
        # Not the raw XML wrapper.
        self.assertNotIn('<task-notification>', out)
        self.assertNotIn('<result>', out)

    def test_full_preview_human_unchanged(self):
        out = self.r._render_user(self._human_rec())
        self.assertIn('▶ user', out)
        self.assertIn('Just a normal prompt.', out)


def _load_recipe_with_md_doc():
    """Reload the recipe with ``recipes/`` on ``sys.path`` so ``md_doc`` resolves.

    The shared ``_load_recipe`` loads with the default path, where neither
    ``md2ansi_lib`` nor ``md_doc`` is importable — so its module's ``_md_doc``
    is ``None`` (the markdown feature is dormant). The markdown-subtree tests
    need the live module, so they reload with the recipes dir prepended (as the
    recipe gets at runtime — the framework prepends its dir), then restore
    ``sys.path`` and drop the transient ``md_doc`` import so other test classes
    keep seeing the no-md baseline.
    """
    recipes_dir = str(_REPO / 'recipes')
    added = recipes_dir not in sys.path
    if added:
        sys.path.insert(0, recipes_dir)
    saved_md_doc = sys.modules.get('md_doc')
    saved_md2ansi = sys.modules.get('md2ansi_lib')
    try:
        return _load_recipe(with_md_doc=True)
    finally:
        if added:
            try:
                sys.path.remove(recipes_dir)
            except ValueError:
                pass
        # Restore the module table so the default (md-less) recipe load other
        # classes rely on isn't perturbed by our transient imports.
        if saved_md_doc is None:
            sys.modules.pop('md_doc', None)
        else:
            sys.modules['md_doc'] = saved_md_doc
        if saved_md2ansi is None:
            sys.modules.pop('md2ansi_lib', None)
        else:
            sys.modules['md2ansi_lib'] = saved_md2ansi


class TestMarkdownSubtrees(unittest.TestCase):
    """#660/#661: markdown document-subtree detection, build, preview, styling.

    Exercises the recipe's ``md_doc`` integration directly: the optimistic
    detection gate (``_md_has_children``), the lazy authoritative subtree build
    (``_md_message_children`` / ``_md_subtree_children``), recursion across
    referenced files, the ``_on_children_loaded`` self-heal, and (#661) the
    ``('md', …)`` preview routing — full document text for a document node, the
    byte-range section slice for a heading node, the ``_MD_COLOR`` toggle, and
    the relative-label rule. The recipe is reloaded with ``md_doc`` live (see
    ``_load_recipe_with_md_doc``); a separate test forces ``_md_doc = None`` to
    assert graceful degradation.

    Robust to shared-module-state pollution: ``md_doc``'s process-wide doc
    cache is cleared and ``_BROWSER`` / ``_TREE_MODE`` / ``_MD_COLOR`` are
    saved/restored in setUp/tearDown.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe_with_md_doc()

    def setUp(self):
        self.assertIsNotNone(
            self.r._md_doc, 'md_doc must be importable for these tests')
        # Save/restore mutable module state so order can't leak between tests.
        self._saved_browser = self.r._BROWSER
        self._saved_tree_mode = self.r._TREE_MODE
        self._saved_md_color = self.r._MD_COLOR
        self.addCleanup(setattr, self.r, '_BROWSER', self._saved_browser)
        self.addCleanup(setattr, self.r, '_TREE_MODE', self._saved_tree_mode)
        self.addCleanup(setattr, self.r, '_MD_COLOR', self._saved_md_color)
        self.r._BROWSER = None
        self.r._TREE_MODE = True
        # The recipe's per-file scan cache and md_doc's shared doc cache both
        # persist across tests — clear both each way.
        self.r._TREE_CACHE.clear()
        self.r._SESSION_CWDS_CACHE.clear()
        self.r._md_doc.clear_cache()
        self.addCleanup(self.r._TREE_CACHE.clear)
        self.addCleanup(self.r._SESSION_CWDS_CACHE.clear)
        self.addCleanup(self.r._md_doc.clear_cache)

    # ---- fixture -------------------------------------------------------

    def _build_project(self, tmp):
        """A temp project + session whose messages exercise every path.

        Layout (``cwd`` = the project source dir ``proj/``)::

            proj/report.md      → references appendix.md  (recursion)
            proj/appendix.md     → a leaf doc (no further refs)
            projects/<enc>/sid.jsonl

        Session records:

          * #0 — headings (``# Summary`` / ``## Risks``) AND a ``report.md``
                  ref in prose: detection True, builds inline + file docs.
          * #1 — plain prose, no heading, no ref: detection False.
          * #2 — a ``Write`` tool path naming ``missing.md`` (does NOT exist)
                  and no heading: detection True (optimistic), but the build
                  yields ``[]`` → self-heal target.

        Returns ``(sess_path, proj_dir, report_md, appendix_md)``.
        """
        import json as _json
        proj = os.path.join(tmp, 'proj')
        os.makedirs(proj)
        appendix = os.path.join(proj, 'appendix.md')
        report = os.path.join(proj, 'report.md')
        with open(appendix, 'w') as f:
            f.write('# Appendix\n\ndetails\n')
        with open(report, 'w') as f:
            f.write('# Findings\n\nbody\n\nSee appendix.md for more.\n'
                    '## Detail\n\nx\n')
        # Encode proj into the Claude project-dir name the recipe resolves
        # cwd/root from (matches _encode_project_path).
        enc = self.r._encode_project_path(proj)
        sess_dir = os.path.join(tmp, 'projects', enc)
        os.makedirs(sess_dir)
        sess = os.path.join(sess_dir, 'sid.jsonl')
        recs = [
            {'type': 'user', 'cwd': proj, 'message': {'content': [
                {'type': 'text',
                 'text': 'Plan\n# Summary\n## Risks\nsee report.md'}]}},
            {'type': 'assistant', 'cwd': proj, 'message': {'content': [
                {'type': 'text', 'text': 'just chatting, nothing here'}]}},
            {'type': 'assistant', 'cwd': proj, 'message': {'content': [
                {'type': 'tool_use', 'name': 'Write',
                 'input': {'file_path': 'missing.md', 'contents': 'x'}}]}},
        ]
        with open(sess, 'w') as f:
            for rc in recs:
                f.write(_json.dumps(rc) + '\n')
        return sess, proj, report, appendix

    # ---- detection -----------------------------------------------------

    def test_detection_heading_message(self):
        rec = {'message': {'content': [
            {'type': 'text', 'text': '# Heading\n\nbody'}]}}
        self.assertTrue(self.r._md_has_children(rec))

    def test_detection_ref_message_prose(self):
        rec = {'message': {'content': [
            {'type': 'text', 'text': 'I wrote it to docs/report.md today'}]}}
        self.assertTrue(self.r._md_has_children(rec))

    def test_detection_ref_message_tool_path(self):
        # The ref lives in a tool_use input, not prose — the string-leaf walk
        # must still find it (covers Write/Read/Edit paths).
        rec = {'message': {'content': [
            {'type': 'tool_use', 'name': 'Read',
             'input': {'file_path': 'notes.md'}}]}}
        self.assertTrue(self.r._md_has_children(rec))

    def test_detection_plain_message_false(self):
        rec = {'message': {'content': [
            {'type': 'text', 'text': 'no heading, no reference at all'}]}}
        self.assertFalse(self.r._md_has_children(rec))

    def test_detection_code_fence_hash_triggers_optimistically(self):
        # A ``#`` only inside a fenced code block fires the cheap regex gate
        # (md_heading_trigger is line-anchored, not fence-aware). This is the
        # intended optimism; the build later self-heals.
        rec = {'message': {'content': [
            {'type': 'text', 'text': '```\n# not a heading\n```'}]}}
        self.assertTrue(self.r._md_has_children(rec))

    def test_detection_quote_not_a_ref(self):
        # ``.md`` inside a JSON-ish quoted token does not pollute the match:
        # the ref regex excludes ``"``. A bare ``"x"`` with no .md is plainly
        # negative; assert a quote-only string with no real path is False.
        rec = {'message': {'content': [
            {'type': 'text', 'text': 'discuss markdown in general'}]}}
        self.assertFalse(self.r._md_has_children(rec))

    def test_detection_graceful_noop_when_md_doc_none(self):
        # Force the feature off; detection must be a hard no-op even for a
        # record that would otherwise trigger.
        saved = self.r._md_doc
        try:
            self.r._md_doc = None
            rec = {'message': {'content': [
                {'type': 'text', 'text': '# Heading and a docs/x.md ref'}]}}
            self.assertFalse(self.r._md_has_children(rec))
        finally:
            self.r._md_doc = saved

    # ---- message-level build: inline + file docs -----------------------

    def test_message_children_inline_plus_refs_umbrella(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            kids = self.r._md_message_children(base)
            self.assertEqual(len(kids), 2, kids)
            inline, refs = kids
            # Inline document node: empty md chain, same-file (boundary off).
            self.assertEqual(inline.id, _md_id(base))
            self.assertEqual(inline.title, 'markdown')
            self.assertFalse(inline.boundary)
            self.assertTrue(inline.has_children)
            self.assertEqual(inline.tag, 'md')
            # References umbrella: same-document grouping (boundary off), [links]
            # tag, id = the ('refs', anchor, ()) for the message.
            self.assertEqual(refs.id, _refs_id(base))
            self.assertEqual(refs.title, 'References')
            self.assertEqual(refs.tag, 'links')
            self.assertEqual(refs.kind, 'md-refs')
            self.assertFalse(refs.boundary)
            self.assertTrue(refs.has_children)
            # The umbrella's OWN children are the file docs: report.md, foreign
            # subtree (boundary on), optimistic has_children, label relative to
            # the project root.
            ref_kids = self.r._md_refs_umbrella_children(refs.id)
            self.assertEqual(len(ref_kids), 1, ref_kids)
            (filedoc,) = ref_kids
            self.assertEqual(filedoc.title, 'report.md')
            self.assertTrue(filedoc.boundary)
            self.assertTrue(filedoc.has_children)
            self.assertEqual(filedoc.tag, 'md')
            self.assertEqual(
                filedoc.id, _md_id(base, [os.path.realpath(report)]))

    def test_message_children_dedup_and_existing_only(self):
        # A message referencing the same existing file twice + a non-existent
        # one yields exactly one file-doc node (deduped, existing-only).
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, 'proj')
            os.makedirs(proj)
            real = os.path.join(proj, 'real.md')
            with open(real, 'w') as f:
                f.write('# R\n')
            enc = self.r._encode_project_path(proj)
            sess_dir = os.path.join(tmp, 'projects', enc)
            os.makedirs(sess_dir)
            sess = os.path.join(sess_dir, 'sid.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({'type': 'user', 'cwd': proj, 'message': {
                    'content': [{'type': 'text', 'text':
                                 'real.md and again real.md plus ghost.md'}]}})
                        + '\n')
            kids = self.r._md_message_children(('msg', sess, 0))
            # No heading in the text → no inline node; just the References
            # umbrella. The dedup/existing-only happens in its OWN children.
            self.assertEqual([k.title for k in kids], ['References'])
            ref_kids = self.r._md_refs_umbrella_children(kids[0].id)
            self.assertEqual([k.title for k in ref_kids], ['real.md'])

    def test_message_children_absolute_tool_path_yields_file_doc(self):
        # Regression (#671): a Write tool_use carrying an ABSOLUTE .md
        # file_path now produces a file-doc child. Before the find_md_refs
        # lookbehind fix the leading '/' was dropped, leaving a token that
        # resolved relative to cwd/project_root — to nothing — so the child
        # was silently lost. The target lives OUTSIDE the project dir, so it
        # can ONLY resolve via resolve_md_ref's absolute branch (rule #1).
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, 'proj')
            os.makedirs(proj)
            # A report file outside the project source tree, named by abspath.
            outside = os.path.join(tmp, 'out')
            os.makedirs(outside)
            report = os.path.join(outside, 'report.md')
            with open(report, 'w') as f:
                f.write('# Findings\n\nbody\n')
            report_abs = os.path.realpath(report)

            enc = self.r._encode_project_path(proj)
            sess_dir = os.path.join(tmp, 'projects', enc)
            os.makedirs(sess_dir)
            sess = os.path.join(sess_dir, 'sid.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({'type': 'assistant', 'cwd': proj,
                    'message': {'content': [
                        {'type': 'tool_use', 'name': 'Write',
                         'input': {'file_path': report, 'contents': 'x'}}]}})
                        + '\n')

            # Pre-fix sanity: the de-slashed token (leading '/' stripped, as a
            # bare \b would have produced) does NOT resolve against any base,
            # i.e. the child genuinely depended on the absolute capture.
            deslashed = report.lstrip('/')
            self.assertIsNone(self.r._md_doc.resolve_md_ref(
                deslashed, doc_dir=proj, cwd=proj, project_root=proj))

            kids = self.r._md_message_children(('msg', sess, 0))
            # No heading in the record → no inline node; just the References
            # umbrella, whose one child is the file doc.
            self.assertEqual([k.title for k in kids], ['References'])
            ref_kids = self.r._md_refs_umbrella_children(kids[0].id)
            self.assertEqual(len(ref_kids), 1, ref_kids)
            (filedoc,) = ref_kids
            self.assertEqual(filedoc.tag, 'md')
            self.assertEqual(filedoc.kind, 'md-doc')
            self.assertTrue(filedoc.boundary)
            self.assertEqual(filedoc.md_abspath, report_abs)
            # Id is abspath-canonical: it carries the absolute realpath,
            # proving the leading '/' survived find_md_refs and resolved via
            # the absolute branch.
            self.assertEqual(
                filedoc.id, _md_id(('msg', sess, 0), [report_abs]))
            # Label is the recipe's own anchor-relative rule (outside the
            # project → home-collapsed absolute), not a hardcoded literal.
            self.assertEqual(
                filedoc.title,
                self.r._md_ref_label(report_abs, proj, proj))

    def test_message_children_sorted_by_label(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, 'proj')
            os.makedirs(proj)
            for n in ('zeta.md', 'alpha.md'):
                with open(os.path.join(proj, n), 'w') as f:
                    f.write('# H\n')
            enc = self.r._encode_project_path(proj)
            sess_dir = os.path.join(tmp, 'projects', enc)
            os.makedirs(sess_dir)
            sess = os.path.join(sess_dir, 'sid.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({'type': 'user', 'cwd': proj, 'message': {
                    'content': [{'type': 'text',
                                 'text': 'first zeta.md then alpha.md'}]}})
                        + '\n')
            kids = self.r._md_message_children(('msg', sess, 0))
            # No heading → just the References umbrella; its children are the
            # refs, sorted by label.
            self.assertEqual([k.title for k in kids], ['References'])
            ref_kids = self.r._md_refs_umbrella_children(kids[0].id)
            self.assertEqual([k.title for k in ref_kids], ['alpha.md', 'zeta.md'])

    def test_message_children_no_inline_when_no_heading(self):
        # A message with a ref but NO heading must not emit an inline doc node.
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, 'proj')
            os.makedirs(proj)
            with open(os.path.join(proj, 'x.md'), 'w') as f:
                f.write('# X\n')
            enc = self.r._encode_project_path(proj)
            sess_dir = os.path.join(tmp, 'projects', enc)
            os.makedirs(sess_dir)
            sess = os.path.join(sess_dir, 'sid.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({'type': 'user', 'cwd': proj, 'message': {
                    'content': [{'type': 'text',
                                 'text': 'see x.md (no heading here)'}]}})
                        + '\n')
            kids = self.r._md_message_children(('msg', sess, 0))
            self.assertNotIn('markdown', [k.title for k in kids])
            self.assertEqual(len(kids), 1)

    # ---- inline document → headings ------------------------------------

    def test_inline_doc_children_are_headings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            inline_id = _md_id(base, [])
            kids = self.r._md_subtree_children(inline_id)
            # Inline text: "Plan" / "# Summary" / "## Risks" / "see report.md"
            # → a dim [text] leaf for the loose intro line "Plan", then one
            # top-level heading (Summary) with Risks nested under it.
            self.assertEqual([k.title for k in kids], ['Plan', 'Summary'])
            text_row, summary = kids
            self.assertEqual(text_row.tag, 'text')
            self.assertEqual(text_row.tag_style, 'dim')
            self.assertEqual(text_row.kind, 'md-text')
            self.assertFalse(text_row.has_children)
            self.assertEqual(summary.tag, 'h1')
            self.assertTrue(summary.has_children)
            self.assertFalse(getattr(summary, 'boundary', False))

    def test_inline_doc_loose_text_is_dim_leaf_with_run_preview(self):
        # A message whose inline markdown opens with a loose body run before its
        # first heading: that run renders as a dim [text] leaf row in FIRST
        # position (before the heading), its preview is the run itself (not the
        # heading section), and it has no children — end-to-end through the
        # get_children / get_preview routers.
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, 'proj')
            os.makedirs(proj)
            enc = self.r._encode_project_path(proj)
            sess_dir = os.path.join(tmp, 'projects', enc)
            os.makedirs(sess_dir)
            sess = os.path.join(sess_dir, 'sid.jsonl')
            # line0: loose intro run; line1: heading; line2: heading body.
            inline = 'intro paragraph line\n# Heading One\nbody under heading'
            with open(sess, 'w') as f:
                f.write(_json.dumps({'type': 'user', 'cwd': proj, 'message': {
                    'content': [{'type': 'text', 'text': inline}]}}) + '\n')
            base = ('msg', sess, 0)
            inline_id = _md_id(base, [])
            kids = self.r.get_children(inline_id)
            # The dim [text] leaf comes first, before the heading row.
            self.assertEqual([k.title for k in kids],
                             ['intro paragraph line', 'Heading One'])
            text_row = kids[0]
            self.assertEqual(text_row.tag, 'text')
            self.assertEqual(text_row.tag_style, 'dim')
            self.assertEqual(text_row.kind, 'md-text')
            self.assertFalse(text_row.has_children)
            # Its id carries the run's line offset (0) within the inline doc.
            self.assertEqual(text_row.id, _md_id(base, [], 0))
            # Leaf: routing the id back through get_children yields [].
            self.assertEqual(self.r.get_children(text_row.id), [])
            # Preview is the loose run itself — NOT the following heading
            # section (the boundary stops at the heading's start).
            self.r._MD_COLOR = False
            out = self.r.get_preview(text_row.id)
            self.assertIn('intro paragraph line', out)
            self.assertNotIn('Heading One', out)
            self.assertNotIn('body under heading', out)

    def test_heading_children_are_subheadings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            # Summary heading sits at inline line_offset 1.
            summary_id = _md_id(base, [], line_offset=1)
            kids = self.r._md_subtree_children(summary_id)
            self.assertEqual([k.title for k in kids], ['Risks'])
            self.assertEqual(kids[0].tag, 'h2')
            self.assertFalse(kids[0].has_children)

    # ---- file document → headings + nested file refs (recursion) -------

    def test_file_doc_children_headings_plus_refs_umbrella(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            report_real = os.path.realpath(report)
            file_id = _md_id(base, [report_real])
            kids = self.r._md_subtree_children(file_id)
            titles = [k.title for k in kids]
            # report.md: "# Findings" (with "## Detail" nested) + a ref to
            # appendix.md → one heading row + one References umbrella.
            self.assertEqual(titles, ['Findings', 'References'])
            heading, refs = kids
            self.assertEqual(heading.tag, 'h1')
            self.assertFalse(getattr(heading, 'boundary', False))
            # Umbrella id = ('refs', anchor, chain); same-document grouping.
            self.assertEqual(refs.id, _refs_id(file_id))
            self.assertEqual(refs.tag, 'links')
            self.assertFalse(refs.boundary)
            self.assertTrue(refs.has_children)
            # The umbrella's OWN child is the nested file-doc, chained onto
            # report.md → appendix.md.
            nested_kids = self.r._md_refs_umbrella_children(refs.id)
            self.assertEqual([k.title for k in nested_kids], ['appendix.md'])
            (nested,) = nested_kids
            self.assertEqual(nested.tag, 'md')
            self.assertTrue(nested.boundary)
            self.assertEqual(
                nested.id,
                _md_id(base, [report_real, os.path.realpath(appendix)]))

    def test_recursion_file_to_file_expands(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            chain = [os.path.realpath(report), os.path.realpath(appendix)]
            appendix_id = _md_id(base, chain)
            kids = self.r._md_subtree_children(appendix_id)
            # appendix.md is a leaf doc: just its "# Appendix" heading, no refs.
            # No further ref → no References umbrella on a leaf doc.
            self.assertEqual([k.title for k in kids], ['Appendix'])
            self.assertEqual(kids[0].tag, 'h1')

    # ---- References umbrella (#702) ------------------------------------

    def test_refs_umbrella_always_wraps_single_ref(self):
        # Even a single ref is grouped under the umbrella — never hung
        # directly under the document.
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, 'proj')
            os.makedirs(proj)
            with open(os.path.join(proj, 'only.md'), 'w') as f:
                f.write('# Only\n')
            enc = self.r._encode_project_path(proj)
            sess_dir = os.path.join(tmp, 'projects', enc)
            os.makedirs(sess_dir)
            sess = os.path.join(sess_dir, 'sid.jsonl')
            with open(sess, 'w') as f:
                f.write(_json.dumps({'type': 'user', 'cwd': proj, 'message': {
                    'content': [{'type': 'text',
                                 'text': 'just one ref: only.md'}]}}) + '\n')
            kids = self.r._md_message_children(('msg', sess, 0))
            self.assertEqual([k.title for k in kids], ['References'])
            self.assertEqual(kids[0].kind, 'md-refs')
            ref_kids = self.r._md_refs_umbrella_children(kids[0].id)
            self.assertEqual([k.title for k in ref_kids], ['only.md'])

    def test_refs_umbrella_preview_is_plain_label_list(self):
        # The umbrella preview is a PLAIN list of the ref labels (one per line,
        # a count header) — NOT routed through md2ansi (no ANSI even with the
        # color toggle ON), and never the file documents' own bodies.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            refs_id = _refs_id(base)
            self.r._MD_COLOR = True   # would colorise a markdown preview
            out = self.r.get_preview(refs_id)
            self.assertNotIn('\x1b', out, 'umbrella preview must be plain text')
            self.assertIn('report.md', out)        # the ref label is listed
            self.assertIn('1 referenced file', out)  # the count header
            # NOT the referenced file's own body (that is the file-doc preview).
            self.assertNotIn('Findings', out)
            self.assertNotIn('body', out)

    def test_refs_umbrella_preview_empty_when_unreadable(self):
        # A refs id whose document is gone yields '' (no crash, no header).
        refs_id = _refs_id(('msg', '/nope/s.jsonl', 3))
        self.assertEqual(self.r.get_preview(refs_id), '')

    def test_is_md_managed_id_covers_refs_umbrella(self):
        # Both umbrella shapes (message-level and file-doc-level) are md-managed
        # so the first-child landing drills INTO them.
        msg_umb = _refs_id(('msg', '/p/s.jsonl', 3))
        file_umb = _refs_id(_md_id(('msg', '/p/s.jsonl', 0), ['/p/x.md']))
        self.assertTrue(self.r._is_md_managed_id(msg_umb))
        self.assertTrue(self.r._is_md_managed_id(file_umb))
        saved = self.r._md_doc
        try:
            self.r._md_doc = None
            self.assertFalse(self.r._is_md_managed_id(msg_umb))
        finally:
            self.r._md_doc = saved

    def test_expand_refs_umbrella_lands_on_first_ref(self):
        # Right-arrow into a References umbrella lands the cursor on its first
        # ref file-doc (the umbrella is md-managed → first-child landing).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            refs = next(k for k in self.r.get_children(base)
                        if k.kind == 'md-refs')
            ref_kids = self.r.get_children(refs.id)
            self.assertTrue(ref_kids, 'umbrella should have ref children')
            first = ref_kids[0].id
            ctx, calls = self._make_jump_ctx(cached={refs.id: ref_kids})
            self.r._on_expand(ctx, [refs.id])
            self.assertEqual(calls, [first],
                             'expanding a References umbrella must land on its '
                             'first ref')

    def test_file_doc_umbrella_routes_by_refs_tag(self):
        # A file-doc-level References umbrella ``('refs', anchor, chain)`` shares
        # its ``(anchor, chain)`` with the file-doc node ``('md', anchor, chain,
        # None)``, but the ``refs`` tag routes it to the umbrella builder in
        # BOTH get_children and get_preview — never the md-subtree builder.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            file_id = _md_id(base, [os.path.realpath(report)])
            refs_id = _refs_id(file_id)
            self.assertEqual(refs_id[0], 'refs')   # tag-routed, no ambiguity
            # get_children: routed to the umbrella builder → the nested ref,
            # NOT the file-doc subtree (which would be ['Findings', 'References']).
            self.assertEqual(
                [k.title for k in self.r.get_children(refs_id)], ['appendix.md'])
            # get_preview: routed to the plain label list, NOT the file body.
            self.r._MD_COLOR = False
            out = self.r.get_preview(refs_id)
            self.assertIn('appendix.md', out)
            self.assertNotIn('Findings', out)   # not the report.md body

    def test_doc_expand_adds_umbrella_without_building_ref_list(self):
        # The lazy short-circuit: a document-expand only adds the umbrella NODE
        # — it does NOT build the full ref list (that is the umbrella's own
        # get_children). Spy on the full-resolve helper to prove it is not
        # called during the document-expand, only on the umbrella-expand.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            calls = []
            real = self.r._md_resolved_refs

            def _spy(*a, **kw):
                calls.append(1)
                return real(*a, **kw)

            self.r._md_resolved_refs = _spy
            self.addCleanup(setattr, self.r, '_md_resolved_refs', real)
            # Document-expand: the umbrella appears, the full resolve does NOT
            # run (only the cheap _has_any_existing_ref short-circuit).
            kids = self.r._md_message_children(base)
            refs = next(k for k in kids if k.kind == 'md-refs')
            self.assertEqual(calls, [], 'doc-expand must not build the ref list')
            # Umbrella-expand: NOW the full resolve runs and yields the refs.
            ref_kids = self.r._md_refs_umbrella_children(refs.id)
            self.assertEqual(len(calls), 1, 'umbrella-expand runs the resolve')
            self.assertEqual([k.title for k in ref_kids], ['report.md'])

    # ---- routing through get_children ----------------------------------

    def test_get_children_routes_message_and_md_ids(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            # Message id → inline doc + References umbrella (same as
            # _md_message_children).
            via_router = self.r.get_children(base)
            self.assertEqual([k.title for k in via_router],
                             ['markdown', 'References'])
            # ('md', …) id → routed to the subtree builder (the dim [text]
            # intro leaf "Plan" then the "Summary" heading).
            inline_id = _md_id(base, [])
            self.assertEqual(
                [k.title for k in self.r.get_children(inline_id)],
                ['Plan', 'Summary'])
            # ('refs', …) umbrella id → routed by tag to the umbrella
            # builder; its child is the file doc.
            refs_id = _refs_id(base)
            self.assertEqual(
                [k.title for k in self.r.get_children(refs_id)], ['report.md'])

    def test_get_children_message_is_leaf_without_md_doc(self):
        # With the feature off, a message id is a leaf again (no md children).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            saved = self.r._md_doc
            try:
                self.r._md_doc = None
                self.assertEqual(self.r.get_children(('msg', sess, 0)), [])
            finally:
                self.r._md_doc = saved

    def test_get_preview_md_id_unreadable_returns_empty(self):
        # A ('md', …) id whose base record / file is unreadable yields '' —
        # and crucially does NOT fall through to the generic message path:
        # routing is by tag (``id[0] == 'md'``), so a missing doc simply
        # produces an empty preview.
        base = '/p/s.jsonl#3'
        heading_id = _md_id(base, [], line_offset=12)
        self.assertEqual(self.r.get_preview(heading_id), '')
        self.assertEqual(
            self.r.get_preview(_md_id(base, [])), '')

    # ---- preview routing: document & heading (#661) --------------------

    def _sections_file(self, tmp):
        """A two-sibling-h1 doc for boundary-slice assertions.

        ``# First`` (with a ``## Sub`` nested under it) then a sibling
        ``# Second``. The h1 section-slice must include the nested sub-section
        but stop before the later sibling — the boundary rule. Returns
        ``(abspath, text)``.
        """
        text = '# First\n\nalpha\n\n## Sub\n\nbeta\n\n# Second\n\ngamma\n'
        path = os.path.join(tmp, 'sections.md')
        with open(path, 'w') as f:
            f.write(text)
        return os.path.realpath(path), text

    def test_preview_inline_document_is_full_text(self):
        # Document node (empty chain) → the message's FULL inline markdown,
        # rendered. With color on the body is ANSI-wrapped, so assert on the
        # plain words that survive md2ansi (every prose token is present).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            self.r._MD_COLOR = False    # raw text → exact substring assertions
            inline_id = _md_id(('msg', sess, 0), [])
            out = self.r.get_preview(inline_id)
            # The whole inline document, not just a summary fragment.
            for token in ('Plan', 'Summary', 'Risks', 'report.md'):
                self.assertIn(token, out)

    def test_preview_file_document_is_full_file_text(self):
        # Document node for a referenced file → that file's full text (via the
        # md_doc cache), rendered. report.md has every section + the onward ref.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            self.r._MD_COLOR = False
            file_id = _md_id(
                ('msg', sess, 0), [os.path.realpath(report)])
            out = self.r.get_preview(file_id)
            for token in ('Findings', 'body', 'appendix.md', 'Detail'):
                self.assertIn(token, out)

    def test_preview_heading_is_section_slice_with_boundaries(self):
        # Heading node → ONLY that heading's byte-range section. The h1 slice
        # includes its own nested sub-section but excludes the next sibling h1.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path, text = self._sections_file(tmp)
            self.r._MD_COLOR = False
            base = '/p/s.jsonl#0'   # base record is irrelevant for a file chain
            # ``# First`` is at line 0; render its section.
            first_id = _md_id(base, [path], line_offset=0)
            out = self.r.get_preview(first_id)
            self.assertIn('First', out)
            self.assertIn('Sub', out)       # nested sub-section IS included
            self.assertIn('beta', out)
            self.assertNotIn('Second', out)  # later sibling is NOT included
            self.assertNotIn('gamma', out)
            # The slice is exactly the section's byte range (raw == slice).
            _, tree = self.r._md_doc.get_doc(path)
            node = self.r._md_doc.node_at_line(tree, 0)
            self.assertEqual(
                out, text[node.byte_offset:node.byte_offset + node.byte_size])
            # The sibling ``# Second`` (line 8) renders its own section.
            second_id = _md_id(
                base, [path], line_offset=8)
            sec = self.r.get_preview(second_id)
            self.assertIn('Second', sec)
            self.assertIn('gamma', sec)
            self.assertNotIn('First', sec)

    def test_preview_heading_missing_line_offset_returns_empty(self):
        # A line offset that matches no heading (stale id) → '' (no crash).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self._sections_file(tmp)
            bad = _md_id(
                '/p/s.jsonl#0', [path], line_offset=999)
            self.assertEqual(self.r.get_preview(bad), '')

    def test_preview_md_color_toggle(self):
        # _MD_COLOR honoured: ON → ANSI in the body; OFF → raw text, no ESC.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            inline_id = _md_id(('msg', sess, 0), [])
            self.r._MD_COLOR = True
            self.assertIn('\x1b', self.r.get_preview(inline_id))
            self.r._md_doc.clear_cache()   # not strictly needed (inline), tidy
            self.r._MD_COLOR = False
            off = self.r.get_preview(inline_id)
            self.assertNotIn('\x1b', off)
            self.assertIn('Summary', off)

    # ---- boundary integration + get_preview reorder (#662) -------------

    def test_is_cross_file_fallback_for_md_file_doc_id(self):
        # An ('md', …) file-doc id is neither ('agent', …) nor ('session', …),
        # so the not-loaded fallback in _is_cross_file_id returns False — the id
        # is never mistaken for a cross-file (session/subagent) row by shape.
        # (The reorder, tested next, is what protects the *loaded* case.)
        file_id = _md_id(
            '/p/s.jsonl#0', ['/abs/report.md'])
        self.assertIsNone(self.r._BROWSER)   # setUp default → fallback path
        self.assertFalse(self.r._is_cross_file_id(file_id))

    def test_get_preview_md_file_doc_routes_to_md_node_despite_boundary(self):
        # #662 reorder regression guard. An md file-doc id carries
        # boundary=True; with a _BROWSER that returns it from get_item, the
        # (now boundary-based) _is_cross_file_id reports True for it. If the
        # cross-file check ran first it would hijack the preview into the
        # metadata/umbrella path. Because the ``tag == 'md'`` branch is checked
        # BEFORE _is_cross_file_id, the node still routes to _preview_md_node
        # and yields the file's own text — never the cross-file card.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            report = os.path.join(tmp, 'report.md')
            with open(report, 'w') as f:
                f.write('# Findings\n\nMD-FILE-DOC-BODY\n')
            file_id = _md_id(
                '/p/s.jsonl#0', [os.path.realpath(report)])

            class _B:
                def __init__(self, item):
                    self._item = item
                def get_item(self, id_):
                    return self._item if id_ == file_id else None

            item = type('I', (), {'id': file_id, 'boundary': True})()
            self.r._BROWSER = _B(item)
            self.r._MD_COLOR = False
            # The predicate really does see this id as cross-file (boundary).
            self.assertTrue(self.r._is_cross_file_id(file_id))
            out = self.r.get_preview(file_id)
            # Routed to the md document preview (the file's text), NOT the
            # cross-file metadata card (which prints 'browse-claude' + a
            # 'path' row and never the body).
            self.assertIn('Findings', out)
            self.assertIn('MD-FILE-DOC-BODY', out)
            self.assertNotIn('browse-claude', out)

    def test_get_preview_inline_md_node_routes_to_md_node(self):
        # The inline (same-file, boundary=False) document node also routes to
        # _preview_md_node — confirms the reorder didn't regress the common
        # case and that the ``tag == 'md'`` branch precedes the generic message
        # branch too.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            self.r._MD_COLOR = False
            inline_id = _md_id(('msg', sess, 0), [])
            out = self.r.get_preview(inline_id)
            self.assertIn('Summary', out)
            self.assertNotIn('browse-claude', out)

    # ---- labels (#661) -------------------------------------------------

    def test_label_relative_to_project_root(self):
        # A file inside the project root is labelled relative to it (the id
        # stays abspath-canonical; only the displayed title is relative).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            kids = self.r._md_message_children(('msg', sess, 0))
            # The file doc lives under the References umbrella, not directly
            # under the message.
            refs = next(k for k in kids if k.kind == 'md-refs')
            ref_kids = self.r._md_refs_umbrella_children(refs.id)
            filedoc = next(k for k in ref_kids if k.tag == 'md' and k.boundary)
            self.assertEqual(filedoc.title, 'report.md')
            # Id carries the absolute path (the chain), not the relative label.
            self.assertEqual(filedoc.id[0], 'md')
            self.assertEqual(list(filedoc.id[2]), [os.path.realpath(report)])

    def test_label_rule_anchor_precedence(self):
        # The label rule (``_md_ref_label``) is a pure function over the two
        # anchors; exercise all three branches directly so we don't depend on
        # the resolver's prose-token handling:
        #   1. inside project_root  -> relative to project_root (preferred),
        #   2. inside cwd only      -> relative to cwd,
        #   3. inside neither       -> ``~``-collapsed absolute path.
        home = os.environ.get('HOME') or os.path.expanduser('~')
        root = '/work/repo'
        cwd = '/work/repo/sub'         # cwd nested under the root
        # (1) file under the root (but not under cwd) -> root-relative.
        self.assertEqual(
            self.r._md_ref_label('/work/repo/docs/a.md', cwd, root),
            'docs/a.md')
        # (2) file under cwd but with a different (non-ancestor) root ->
        #     cwd-relative (root checked first, misses, cwd matches).
        self.assertEqual(
            self.r._md_ref_label('/work/repo/sub/b.md', cwd, '/other/root'),
            'b.md')
        # (3) file under neither anchor -> ``~``-collapsed absolute path,
        #     never a ``../`` relpath.
        ext = os.path.join(home, 'elsewhere', 'c.md')
        label = self.r._md_ref_label(ext, cwd, root)
        self.assertEqual(label, self.r._collapse_home(ext))
        self.assertTrue(label.startswith('~/'))
        self.assertNotIn('../', label)

    def test_inline_node_title_is_markdown(self):
        # The inline (same-file) document node's title is the literal
        # "markdown" — never a path label.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            kids = self.r._md_message_children(('msg', sess, 0))
            inline = next(k for k in kids if not k.boundary)
            self.assertEqual(inline.title, 'markdown')
            self.assertEqual(inline.tag_style, 'dim')

    # ---- self-heal -----------------------------------------------------

    def test_self_heal_flips_has_children_off_on_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            # Message #2 is the optimistic false positive: its only .md token
            # (missing.md) doesn't exist and it has no heading.
            base = ('msg', sess, 2)
            self.assertTrue(self.r._md_has_children(
                self.r._read_jsonl_line(sess, 2)))     # arrow was set
            self.assertEqual(self.r.get_children(base), [])  # build is empty

            fake = _FakeBrowser()
            self.r._BROWSER = fake
            fake._children_cache[base] = []            # settled to no children

            class _Ctx:
                state = type('S', (), {'expanded': set()})()

                def cached_children(self, id_):
                    return fake.cached_children(id_)

                def update_data(self, ops):
                    return fake.update_data(ops)

            self.r._on_children_loaded(_Ctx(), [base])
            # The self-heal pushed exactly a mod(base, has_children=False) op
            # (the _FakeBrowser records ops; applying a 'mod' isn't part of its
            # minimal apply, so we assert on the emitted op — the recipe's
            # contract — rather than on a fixture-applied field).
            flat_ops = [op for batch in fake.update_data_calls for op in batch]
            self.assertTrue(
                any(op[0] == 'mod' and op[1] == base
                    and op[3].get('has_children') is False
                    for op in flat_ops),
                flat_ops)

    def test_self_heal_skips_nonempty_and_non_md_parents(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            fake = _FakeBrowser()
            self.r._BROWSER = fake

            class _Ctx:
                state = type('S', (), {'expanded': set()})()

                def cached_children(self, id_):
                    return fake.cached_children(id_)

                def update_data(self, ops):
                    return fake.update_data(ops)

            ctx = _Ctx()
            # A real message id with NON-empty children: no retraction.
            base0 = ('msg', sess, 0)
            fake._children_cache[base0] = [_RecordingItem('child')]
            # A non-md umbrella id settling empty: must be left alone.
            umbrella = ('prompt', sess, 0)
            fake._children_cache[umbrella] = []
            self.r._on_children_loaded(ctx, [base0, umbrella])
            fake.flush()
            flat_ops = [op for batch in fake.update_data_calls for op in batch]
            self.assertEqual(
                [op for op in flat_ops if op[0] == 'mod'], [],
                'no self-heal mod for non-empty or non-md parents')

    # ---- right-arrow: move INTO the first child (#688) ------------------
    #
    # An umbrella/session moves the cursor INTO its children on a single
    # right-arrow (``_on_expand`` → ``_jump_to_latest_voice`` lands on the
    # latest VOICE child, whose ``row_bg`` is set). A markdown-managed row's
    # children are document/heading nodes with NO ``row_bg``, so the
    # latest-voice lookup returns ``None`` and the cursor used to stay put —
    # right-arrow felt like a no-op. The fix lands the cursor on the FIRST
    # visible child for these rows (the structural analog), reusing the same
    # ``_AWAITING_VOICE_JUMP`` park / ``_on_children_loaded`` settle as the
    # voice-jump. These tests drive the recipe's hook callbacks directly —
    # the same fake-ctx style as ``TestOnExpandJumpComposition`` — but with
    # ``md_doc`` live so the markdown subtree actually builds.

    def _make_jump_ctx(self, *, cached, expanded=None):
        """Fake ctx recording ``cursor_to`` + serving ``cached_children``.

        ``cached`` maps an id → the value ``cached_children`` returns
        (``None`` = fetch in flight, a list = available). ``expanded`` is the
        set ``ctx.state.expanded`` exposes (the still-expanded guard reads
        it); ``None`` means "everything is expanded". Installs a recording
        ``_BROWSER`` (with ``invalidate_preview`` / ``preview_width`` so the
        cross-file-upgrade and md-voice paths don't blow up). Returns
        ``(ctx, cursor_to_calls)``.
        """
        cursor_to_calls = []

        class _AllExpanded:
            def __contains__(self, _id):
                return True

        expanded_set = _AllExpanded() if expanded is None else expanded

        class _FakeBrowser:
            preview_width = 80

            def invalidate_preview(self, _id):
                pass

            def get_item(self, _id):
                return None

        self.r._BROWSER = _FakeBrowser()

        class _State:
            expanded = expanded_set

        class _Ctx:
            state = _State()

            def cached_children(self, id_):
                val = cached.get(id_, None)
                return None if val is None else list(val)

            def cursor_to(self, id_):
                cursor_to_calls.append(id_)

            def update_data(self, _ops):
                # The settle path may self-heal a childless md row; record
                # nothing — these tests only assert on cursor movement.
                pass

        return _Ctx(), cursor_to_calls

    def test_first_visible_child_helper(self):
        # The structural analog of _latest_voice_among_children: first child
        # by listing order, skipping hidden rows; None when no visible child.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)                       # message-with-md
            kids = self.r.get_children(base)
            self.assertTrue(kids, 'fixture should build md children')
            self.assertEqual(
                self.r._first_visible_child(base), kids[0].id)
        # Non-str / no-children both yield None.
        self.assertIsNone(self.r._first_visible_child(None))

    def test_first_visible_child_skips_hidden(self):
        # A hidden leading child is skipped; the first VISIBLE id is returned.
        saved = self.r.get_children
        hidden = _RecordingItem('h'); hidden.hidden = True
        shown = _RecordingItem('v'); shown.hidden = False
        self.r.get_children = lambda _id: [hidden, shown]
        self.addCleanup(setattr, self.r, 'get_children', saved)
        self.assertEqual(
            self.r._first_visible_child(('msg', '/x.jsonl', 0)), 'v')

    def test_is_md_managed_id_predicate(self):
        # md-managed = md_doc live AND id tag in ('md', 'refs', 'msg').
        msg = ('msg', '/p/s.jsonl', 3)
        inline = _md_id(('msg', '/p/s.jsonl', 0))
        self.assertTrue(self.r._is_md_managed_id(msg))
        self.assertTrue(self.r._is_md_managed_id(inline))
        # Umbrellas / sessions / subagent groups are NOT md-managed.
        for not_md in (('prompt', '/p/s.jsonl', 0), ('tool', '/p/s.jsonl', 1),
                       ('span', '/p/s.jsonl', 2), ('agent', '/p/s.jsonl', 'A1'),
                       ('session', '/p/s.jsonl'), None):
            self.assertFalse(self.r._is_md_managed_id(not_md), not_md)
        # Hard no-op when the feature is off.
        saved = self.r._md_doc
        try:
            self.r._md_doc = None
            self.assertFalse(self.r._is_md_managed_id(msg))
            self.assertFalse(self.r._is_md_managed_id(inline))
        finally:
            self.r._md_doc = saved

    def test_expand_md_message_lands_on_first_child(self):
        # Cached expand of a message-with-md: cursor jumps to the inline
        # ``markdown`` node (its first child) in one press — the bug case.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            kids = self.r.get_children(base)
            first = kids[0].id
            self.assertEqual(first, _md_id(base))   # the inline doc node
            ctx, calls = self._make_jump_ctx(cached={base: kids})
            self.r._on_expand(ctx, [base])
            self.assertEqual(calls, [first],
                             'expanding a message-with-md must land on its '
                             'first child')

    def test_expand_inline_md_node_lands_on_first_child(self):
        # Cached expand of the inline ``markdown`` (md) node lands on its
        # first heading row.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            inline = _md_id(('msg', sess, 0))
            kids = self.r.get_children(inline)
            self.assertTrue(kids, 'inline doc should have heading children')
            first = kids[0].id
            ctx, calls = self._make_jump_ctx(cached={inline: kids})
            self.r._on_expand(ctx, [inline])
            self.assertEqual(calls, [first])

    def test_expand_file_doc_lands_on_first_child(self):
        # Cached expand of a file-doc (boundary) node lands on its first
        # heading row. The file doc now sits under the message's References
        # umbrella, so reach it through that.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            refs = next(k for k in self.r.get_children(base)
                        if k.kind == 'md-refs')
            filedoc = next(k for k in self.r.get_children(refs.id)
                           if getattr(k, 'boundary', False))
            kids = self.r.get_children(filedoc.id)
            self.assertTrue(kids, 'file doc should have heading children')
            first = kids[0].id
            ctx, calls = self._make_jump_ctx(cached={filedoc.id: kids})
            self.r._on_expand(ctx, [filedoc.id])
            self.assertEqual(calls, [first])

    def test_uncached_md_message_defers_then_lands_on_first_child(self):
        # The async path: uncached expand parks the id (no jump yet), then
        # _on_children_loaded lands on the first child once the fetch settles
        # — exactly mirroring the voice-jump defer.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 0)
            first = self.r.get_children(base)[0].id
            saved_pending = set(self.r._AWAITING_VOICE_JUMP)
            self.addCleanup(self.r._AWAITING_VOICE_JUMP.clear)
            self.addCleanup(self.r._AWAITING_VOICE_JUMP.update, saved_pending)
            self.r._AWAITING_VOICE_JUMP.clear()
            # Children NOT cached at expand time → defer.
            ctx, calls = self._make_jump_ctx(cached={base: None})
            self.r._on_expand(ctx, [base])
            self.assertEqual(calls, [], 'uncached expand must not jump yet')
            self.assertIn(base, self.r._AWAITING_VOICE_JUMP)
            # Fetch settles → the children-loaded hook lands the cursor.
            self.r._on_children_loaded(ctx, [base])
            self.assertEqual(calls, [first])
            self.assertNotIn(base, self.r._AWAITING_VOICE_JUMP)

    def test_umbrella_still_lands_on_latest_voice_no_regression(self):
        # An umbrella's post-expand jump is UNCHANGED: it lands on the LATEST
        # voice child, not the first. A turn with user / tool_use / asst
        # gives three direct children (#0 user, #1 tool_use, #2 asst), so
        # first (#0) != latest (#2) and the assertion would catch any leak of
        # the first-child fallback into the (non-md) umbrella path.
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess = os.path.join(tmp, 's.jsonl')
            recs = [
                {'type': 'user', 'uuid': 'u1', 'message': {
                    'role': 'user', 'content': 'FIRST_VOICE'}},
                {'type': 'assistant', 'uuid': 'a1', 'message': {
                    'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'name': 'Bash',
                         'input': {'command': 'x'}}]}},
                {'type': 'assistant', 'uuid': 'a2', 'message': {
                    'role': 'assistant', 'content': [
                        {'type': 'text', 'text': 'LAST_VOICE'}]}},
            ]
            with open(sess, 'w') as f:
                for rc in recs:
                    f.write(_json.dumps(rc) + '\n')
            umb = ('prompt', sess, 0)
            self.assertFalse(self.r._is_md_managed_id(umb))
            kids = self.r.get_children(umb)
            latest = self.r._latest_voice_among_children(umb)
            self.assertEqual(latest, ('msg', sess, 2))        # the LAST voice
            self.assertNotEqual(latest, kids[0].id)      # first != latest
            ctx, calls = self._make_jump_ctx(cached={umb: kids})
            self.r._on_expand(ctx, [umb])
            self.assertEqual(calls, [latest],
                             'umbrella must still land on the latest voice')

    def test_false_positive_md_message_does_not_move_cursor(self):
        # A false-positive md row (a ``#`` only inside a code fence) whose
        # authoritative build is empty must NOT move the cursor — neither at
        # expand nor when the (empty) fetch settles (which only self-heals).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, proj, report, appendix = self._build_project(tmp)
            base = ('msg', sess, 2)                       # the optimistic positive
            self.assertTrue(self.r._md_has_children(
                self.r._read_jsonl_line(sess, 2)))   # arrow was set
            self.assertEqual(self.r.get_children(base), [])  # build is empty
            saved_pending = set(self.r._AWAITING_VOICE_JUMP)
            self.addCleanup(self.r._AWAITING_VOICE_JUMP.clear)
            self.addCleanup(self.r._AWAITING_VOICE_JUMP.update, saved_pending)
            self.r._AWAITING_VOICE_JUMP.clear()
            # Settled-to-empty children at fire time.
            ctx, calls = self._make_jump_ctx(cached={base: []})
            self.r._on_expand(ctx, [base])
            self.assertEqual(calls, [],
                             'a childless md row must not move the cursor')
            self.r._on_children_loaded(ctx, [base])
            self.assertEqual(calls, [],
                             'self-heal settle must not move the cursor')

    # ---- subagent report body renders as markdown (#689) ---------------

    _SUBAGENT_TUR = {
        'agentId': 'abcd1234efgh', 'agentType': 'general-purpose',
        'status': 'completed', 'totalDurationMs': 12340,
        'totalTokens': 1234, 'totalToolUseCount': 5,
        'content': [{'type': 'text',
                     'text': '# Report Heading\n\nbody paragraph.'}],
    }

    def test_subagent_report_body_md2ansi_when_color_on(self):
        # #689: the Agent tool_result body IS a markdown report, so with
        # _MD_COLOR on it must render through md2ansi like the voice
        # renderers — NOT be appended raw. md2ansi consumes the ``# ``
        # heading marker and wraps the heading text in its palette escape.
        self.r._MD_COLOR = True   # setUp save/restores; explicit for clarity
        out = self.r._fmt_tur_subagent(self._SUBAGENT_TUR)
        # The heading was rendered: md2ansi's heading-color CSI is present
        # and the literal ``# `` markdown marker is gone from the body.
        self.assertIn('\x1b[0;38;5;226m', out)
        self.assertIn('Report Heading', out)
        self.assertNotIn('# Report Heading', out)

    def test_subagent_report_body_raw_when_color_off(self):
        # #689: with _MD_COLOR off (or md2ansi unavailable) _md_voice is a
        # pass-through, so the body stays the literal markdown source —
        # the ``# `` heading marker survives, unrendered.
        self.r._MD_COLOR = False
        out = self.r._fmt_tur_subagent(self._SUBAGENT_TUR)
        self.assertIn('# Report Heading', out)
        self.assertIn('body paragraph.', out)

    def test_subagent_header_stats_unchanged_by_md_toggle(self):
        # #689: only the report BODY goes through _md_voice — the
        # ``🤖 agentType [status]`` head and the ``Ns · N tools · N tokens``
        # stats line are NOT markdown and must be byte-identical on/off.
        self.r._MD_COLOR = True
        on = self.r._fmt_tur_subagent(self._SUBAGENT_TUR)
        self.r._MD_COLOR = False
        off = self.r._fmt_tur_subagent(self._SUBAGENT_TUR)
        # Salient head/stats fields present regardless of the toggle.
        for token in ('🤖', 'general-purpose', '[completed]', '12.3s',
                      '5 tools', '1,234 tokens'):
            self.assertIn(token, on)
            self.assertIn(token, off)
        # The head + stats prefix (the two lines before the blank-line gap
        # that precedes the body) is identical across the toggle.
        head_stats_on = on.split('\n\n', 1)[0]
        head_stats_off = off.split('\n\n', 1)[0]
        self.assertEqual(head_stats_on, head_stats_off)


class TestBoundaryMigration(unittest.TestCase):
    """#662: ``boundary`` flag integration + the id-shape ``if`` migration.

    Asserts (a) the rows the OLD ``_is_cross_file_id`` matched now carry
    ``boundary=True`` (subagent-group ``('agent', …)`` rows and ``('session', …)``
    session rows; md *inline* and message/umbrella rows do not); (b)
    ``_is_cross_file_id`` is behaviour-equivalent via the boundary lookup,
    with the OLD id-shape predicate preserved as the not-loaded fallback;
    (c) the ``get_preview`` reorder routes an md file-doc (``boundary=True``)
    to ``_preview_md_node`` instead of the cross-file metadata/cascade path;
    (d) ``_walk_umbrella`` skips a ``boundary=True`` child.

    Isolation-robust: ``_BROWSER`` / ``_TREE_MODE`` are saved/restored so
    these pass identically in isolation and under full discover (the recipe
    module is shared across suites).
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self._saved_browser = self.r._BROWSER
        self._saved_tree_mode = self.r._TREE_MODE
        self.addCleanup(setattr, self.r, '_BROWSER', self._saved_browser)
        self.addCleanup(setattr, self.r, '_TREE_MODE', self._saved_tree_mode)
        self.r._BROWSER = None
        self.addCleanup(self.r._TREE_CACHE.clear)

    # ---- fixtures ------------------------------------------------------

    def _project_with_subagent(self, tmp):
        """A project + session with one wired subagent (agent ``A1``).

        Returns ``(sess_path, agent_path)``. The subagent's own .jsonl
        carries a unique sentinel so a preview cascade that wrongly drilled
        into it would be detectable.
        """
        import json as _json
        proj = os.path.join(tmp, '-x')
        os.makedirs(proj)
        sess = os.path.join(proj, 'parent-sid.jsonl')
        with open(sess, 'w') as f:
            f.write(_json.dumps({
                'type': 'user', 'sessionId': 'parent-sid',
                'message': {'role': 'user', 'content': 'PARENT-LEAF-BODY'},
            }) + '\n')
        sub_dir = os.path.join(proj, 'parent-sid', 'subagents')
        os.makedirs(sub_dir)
        agent_path = os.path.join(sub_dir, 'agent-A1.jsonl')
        with open(agent_path, 'w') as f:
            f.write(_json.dumps({
                'type': 'user', 'sessionId': 'parent-sid',
                'message': {'role': 'user', 'content': 'AGENT-INTERNAL-BODY'},
            }) + '\n')
        with open(os.path.join(sub_dir, 'agent-A1.meta.json'), 'w') as fm:
            _json.dump({'agentType': 'general-purpose',
                        'description': 'do a thing'}, fm)
        return sess, agent_path

    # ---- (a) boundary set on the migrated rows -------------------------

    def test_subagent_group_rows_are_boundary(self):
        # Both ('agent', …) builders (the per-session lister and the inline
        # pseudo-item) and orphan rows (which delegate to the lister) must
        # set boundary=True so the migrated predicate matches them.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, agent_path = self._project_with_subagent(tmp)
            (sub,) = self.r._list_subagents_for_session(sess)
            self.assertTrue(sub.boundary)
            self.assertEqual(sub.id[0], 'agent')
            pseudo = self.r._subagent_pseudo_item(sess, 'A1', agent_path)
            self.assertTrue(pseudo.boundary)
            self.assertEqual(pseudo.id[0], 'agent')

    def test_session_rows_are_boundary(self):
        # Bare .jsonl session rows in _list_sessions carry boundary=True.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, _ = self._project_with_subagent(tmp)
            proj = os.path.dirname(sess)
            rows = self.r._list_sessions(proj)
            self.assertTrue(rows)
            for it in rows:
                self.assertEqual(it.id[0], 'session')
                self.assertTrue(it.boundary, it.id)

    # ---- (b) _is_cross_file_id parity (boundary + fallback) ------------

    def test_is_cross_file_via_boundary_when_loaded(self):
        # When the Item IS loaded, the predicate reads its boundary flag.
        item_id = ('agent', '/p/parent-sid.jsonl', 'A1')

        class _B:
            def __init__(self, items):
                self._items = items
            def get_item(self, id_):
                return self._items.get(id_)

        boundary_item = type('I', (), {'id': item_id, 'boundary': True})()
        self.r._BROWSER = _B({item_id: boundary_item})
        self.assertTrue(self.r._is_cross_file_id(item_id))
        # A loaded, NON-boundary item is not cross-file even with a tag
        # the fallback would have matched — the attribute is authoritative.
        bare = ('session', '/p/s.jsonl')
        nonboundary = type('I', (), {'id': bare, 'boundary': False})()
        self.r._BROWSER = _B({bare: nonboundary})
        self.assertFalse(self.r._is_cross_file_id(bare))

    def test_is_cross_file_fallback_when_not_loaded(self):
        # _BROWSER is None (this suite's default) → fall back to the id-tag
        # predicate; parity for ('agent', …) / ('session', …) rows.
        self.assertTrue(self.r._is_cross_file_id(('agent', '/p/s.jsonl', 'A1')))
        self.assertTrue(self.r._is_cross_file_id(('session', '/p/s.jsonl')))
        # Non-cross-file tags stay False.
        self.assertFalse(self.r._is_cross_file_id(('msg', '/p/s.jsonl', 3)))
        self.assertFalse(self.r._is_cross_file_id(('prompt', '/p/s.jsonl', 0)))
        self.assertFalse(self.r._is_cross_file_id(('project', '/some/dir')))
        self.assertFalse(self.r._is_cross_file_id(None))

    def test_is_cross_file_fallback_when_browser_lacks_get_item(self):
        # A Browser stand-in without get_item (older fakes / headless) is
        # treated like "not loaded" → id-tag fallback, never an
        # AttributeError.
        self.r._BROWSER = type('B', (), {})()
        self.assertTrue(self.r._is_cross_file_id(('agent', '/p/s.jsonl', 'A1')))
        self.assertFalse(self.r._is_cross_file_id(('msg', '/p/s.jsonl', 3)))

    # ---- (c) get_preview reorder + md file-doc fallback are exercised in
    #          TestMarkdownSubtrees (which loads the recipe with md_doc live,
    #          so ('md', …) ids can be composed). See its
    #          ``test_get_preview_md_file_doc_routes_to_md_node_despite_boundary``
    #          (THE reorder regression guard) and the file-doc fallback test.

    # ---- (d) _walk_umbrella skips boundary children --------------------

    def test_walk_umbrella_skips_boundary_child(self):
        # A boundary child (here a subagent ('agent', …) row pointing at another
        # file) must NOT have its content folded into the parent's cascade;
        # a same-file leaf sibling still renders.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sess, agent_path = self._project_with_subagent(tmp)
            parent_id = ('prompt', sess, 0)
            agent_id = ('agent', sess, 'A1')
            leaf_id = ('msg', sess, 0)   # the PARENT-LEAF-BODY user line

            boundary_child = type(
                'I', (), {'id': agent_id, 'boundary': True, 'hidden': False})()
            leaf_child = type(
                'I', (), {'id': leaf_id, 'boundary': False, 'hidden': False})()

            class _B:
                def __init__(self, kids):
                    self._kids = kids
                def cached_children(self, pid):
                    return list(self._kids) if pid == parent_id else None

            self.r._BROWSER = _B([boundary_child, leaf_child])
            out = ''.join(self.r._collect_umbrella_preview(parent_id))
            # Same-file leaf rendered; the boundary subagent's own body did
            # NOT bleed in.
            self.assertIn('PARENT-LEAF-BODY', out)
            self.assertNotIn('AGENT-INTERNAL-BODY', out)


class _StopMain(Exception):
    """Raised by the recording Browser stub to halt ``main()`` right after
    the ``BrowserConfig`` is built — before the live-tail thread / ``run()``."""


class TestMainScopeArgv(unittest.TestCase):
    """``main()`` scope resolution from argv — the framework ``--tty`` flag
    (auto-detected by ``Browser.run()``, left in ``sys.argv``) must NOT be
    misread as the positional PROJECT scope, while real positionals still work.

    Drives ``main()`` with a recording ``Browser`` stub that captures the
    constructed ``BrowserConfig`` and raises ``_StopMain`` so the tail
    thread / ``run()`` are never reached. ``_scan_running_sessions`` is
    stubbed to a no-op (no live-session probe under test).
    """

    def setUp(self):
        self.r = _load_recipe()
        self._saved_argv = list(sys.argv)
        self.r._scan_running_sessions = lambda: None

    def tearDown(self):
        sys.argv[:] = self._saved_argv

    def _run_main(self, argv):
        """Drive ``main()`` with ``argv``; return the captured BrowserConfig."""
        captured = {}

        def _rec_browser(config, *a, **kw):
            captured['config'] = config
            raise _StopMain

        self.r.Browser = _rec_browser
        sys.argv[:] = ['browse-claude', *argv]
        try:
            self.r.main()
        except _StopMain:
            pass
        return captured.get('config')

    def test_tty_flag_is_not_the_project_scope(self):
        # ``--tty -`` / ``--tty=-`` / ``--tty /dev/pts/N`` are the framework
        # UI-device flag, not a PROJECT positional: no initial_scope is set
        # (without the fix, ``--tty`` became a bogus ('project', abspath)).
        for argv in (['--tty', '-'], ['--tty=-'], ['--tty', '/dev/pts/9']):
            cfg = self._run_main(argv)
            self.assertIsNotNone(cfg, argv)
            self.assertIsNone(cfg.initial_scope, argv)

    def test_positional_project_still_becomes_scope(self):
        # A real positional PROJECT dir is still seeded as the scope root.
        import tempfile
        with tempfile.TemporaryDirectory() as proj:
            cfg = self._run_main([proj])
            self.assertEqual(cfg.initial_scope,
                             ('project', os.path.abspath(proj)))

    def test_tty_before_positional_project_resolves_the_project(self):
        # ``--tty - PROJECT`` strips the flag/value and still scopes PROJECT
        # (the flag preceding the positional no longer shadows it).
        import tempfile
        with tempfile.TemporaryDirectory() as proj:
            cfg = self._run_main(['--tty', '-', proj])
            self.assertEqual(cfg.initial_scope,
                             ('project', os.path.abspath(proj)))

    def test_positional_jsonl_is_promoted_to_session_scope(self):
        # A ``.jsonl`` positional is promoted to the target session (scope
        # becomes ('session', abspath)), unaffected by a preceding ``--tty``.
        import tempfile
        with tempfile.TemporaryDirectory() as proj:
            jsonl = os.path.join(proj, 'sess.jsonl')
            with open(jsonl, 'w') as f:
                f.write('{}\n')
            cfg = self._run_main(['--tty', '/dev/pts/9', jsonl])
            self.assertEqual(cfg.initial_scope, ('session', jsonl))


class TestToolUmbrellaInfra(unittest.TestCase):
    """Shared dispatch + helpers for purpose-built tool umbrella previews.

    Covers the frame-less JSON colorizer, the delta diff helper, and the
    ``_render_tool_umbrella`` dispatcher's routing contract (registered ->
    formatter; unregistered / declining formatter / in-flight -> None so the
    caller falls back to the cascade).
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    # ---- helpers ----

    def test_maybe_json_color_parses_object(self):
        out = self.r._maybe_json_color('{"a": 1, "b": "x"}')
        self.assertIsNotNone(out)
        # Pretty-printed (a newline appears) and the keys survive.
        self.assertIn('"a"', out)
        self.assertIn('"b"', out)
        self.assertIn('\n', out)

    def test_maybe_json_color_parses_array(self):
        self.assertIsNotNone(self.r._maybe_json_color('[1, 2, 3]'))

    def test_maybe_json_color_rejects_non_json(self):
        self.assertIsNone(self.r._maybe_json_color('hello world'))
        self.assertIsNone(self.r._maybe_json_color('{not valid'))
        self.assertIsNone(self.r._maybe_json_color(''))
        self.assertIsNone(self.r._maybe_json_color(None))

    def test_json_color_falls_back_to_gray_when_unavailable(self):
        # Force the md2ansi_lib path off and confirm the GRAY wrap.
        saved = self.r._M2A_JSON
        try:
            self.r._M2A_JSON = None
            out = self.r._json_color('{"a":1}')
            self.assertIn('{"a":1}', out)
            if self.r.GRAY:
                self.assertIn(self.r.GRAY, out)
        finally:
            self.r._M2A_JSON = saved

    def test_colorize_diff_no_delta_returns_input(self):
        raw = '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
        saved = self.r.HAVE_DELTA
        try:
            self.r.HAVE_DELTA = None
            self.assertEqual(self.r._colorize_diff(raw), raw)
        finally:
            self.r.HAVE_DELTA = saved

    def test_cmd_bg_constant(self):
        self.assertEqual(self.r.CMD_BG, '\x1b[48;5;236m')

    # ---- dispatcher routing ----

    def _umbrella_fixture(self, *, with_result=True, tool_name='Demo',
                          input_=None, tur=None):
        """Write a transcript (user turn root + assistant tool_use + result).

        The leading user prompt opens the turn so ``_scan_tree`` pairs the
        tool_result into ``tool_children``. Returns ``(path, item_id)`` where
        ``item_id`` is the ``('tool', …)`` umbrella id for the assistant
        record at line 1.
        """
        import json as _json
        import tempfile
        records = [
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [{
                 'type': 'tool_use', 'id': 'tu_1', 'name': tool_name,
                 'input': input_ if input_ is not None else {'k': 'v'},
             }]}},
        ]
        if with_result:
            records.append({
                'type': 'user', 'uuid': 'r1', 'parentUuid': 'a1',
                'message': {'role': 'user', 'content': [{
                    'type': 'tool_result', 'tool_use_id': 'tu_1',
                    'content': 'out',
                }]},
                'toolUseResult': tur if tur is not None else {'ok': True},
            })
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl',
                                         delete=False) as f:
            for rec in records:
                f.write(_json.dumps(rec) + '\n')
            path = f.name
        return path, ('tool', path, 1)

    def test_registered_formatter_is_routed(self):
        path, item_id = self._umbrella_fixture(
            tool_name='Demo', input_={'k': 'v'}, tur={'ok': True})
        seen = {}

        def _fmt(inp, tur, raw_content, is_error):
            seen['inp'] = inp
            seen['tur'] = tur
            seen['raw'] = raw_content
            seen['err'] = is_error
            return 'CUSTOM-BLOCK'

        self.r._TOOL_UMBRELLA_FORMATTERS['Demo'] = _fmt
        try:
            out = self.r._render_tool_umbrella(item_id)
            self.assertEqual(out, 'CUSTOM-BLOCK')
            # Formatter received the request input + paired result.
            self.assertEqual(seen['inp'], {'k': 'v'})
            self.assertEqual(seen['tur'], {'ok': True})
            self.assertEqual(seen['raw'], 'out')
            self.assertFalse(seen['err'])
        finally:
            self.r._TOOL_UMBRELLA_FORMATTERS.pop('Demo', None)
            os.unlink(path)

    def test_unregistered_tool_returns_none(self):
        # No formatter for 'Demo' -> None so the caller cascades.
        path, item_id = self._umbrella_fixture(tool_name='Demo')
        try:
            self.assertIsNone(self.r._render_tool_umbrella(item_id))
        finally:
            os.unlink(path)

    def test_formatter_returning_none_falls_through(self):
        path, item_id = self._umbrella_fixture(tool_name='Demo')
        self.r._TOOL_UMBRELLA_FORMATTERS['Demo'] = lambda *a: None
        try:
            self.assertIsNone(self.r._render_tool_umbrella(item_id))
        finally:
            self.r._TOOL_UMBRELLA_FORMATTERS.pop('Demo', None)
            os.unlink(path)

    def test_in_flight_passes_none_result(self):
        # No result record yet: the formatter is still consulted, with
        # ``tur=None`` / ``raw_content=None`` (lets a tool like Bash render
        # just its request). Here the formatter declines -> None.
        path, item_id = self._umbrella_fixture(
            tool_name='Demo', with_result=False)
        seen = {}

        def _fmt(inp, tur, raw_content, is_error):
            seen['tur'] = tur
            seen['raw'] = raw_content
            return None

        self.r._TOOL_UMBRELLA_FORMATTERS['Demo'] = _fmt
        try:
            self.assertIsNone(self.r._render_tool_umbrella(item_id))
            self.assertIsNone(seen['tur'])
            self.assertIsNone(seen['raw'])
        finally:
            self.r._TOOL_UMBRELLA_FORMATTERS.pop('Demo', None)
            os.unlink(path)

    def test_all_parts_must_be_registered(self):
        # Two tool_use parts, only one registered -> None (the cascade
        # handles heterogeneous batches).
        import json as _json
        import tempfile
        records = [
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'tu_1', 'name': 'Demo',
                  'input': {}},
                 {'type': 'tool_use', 'id': 'tu_2', 'name': 'Other',
                  'input': {}},
             ]}},
        ]
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl',
                                         delete=False) as f:
            for rec in records:
                f.write(_json.dumps(rec) + '\n')
            path = f.name
        self.r._TOOL_UMBRELLA_FORMATTERS['Demo'] = lambda *a: 'X'
        try:
            self.assertIsNone(self.r._render_tool_umbrella(('tool', path, 1)))
        finally:
            self.r._TOOL_UMBRELLA_FORMATTERS.pop('Demo', None)
            os.unlink(path)


class TestToolUmbrellaRead(unittest.TestCase):
    """Read umbrella formatter: one header line, no file content."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_header_has_path_range_and_count_no_content(self):
        out = self.r._fmt_tool_umbrella_read(
            {'file_path': '/etc/hosts', 'offset': 10, 'limit': 50},
            {'type': 'text', 'file': {
                'filePath': '/etc/hosts', 'numLines': 42,
                'content': 'SECRET-FILE-BODY-LINE\nmore\n',
            }},
            None, False)
        self.assertIsNotNone(out)
        self.assertIn('🔧 Read', out)
        self.assertIn('/etc/hosts', out)
        self.assertIn('[10..+50]', out)
        self.assertIn('42 lines', out)
        # The file body must NOT be dumped into the umbrella.
        self.assertNotIn('SECRET-FILE-BODY-LINE', out)
        # Single line.
        self.assertNotIn('\n', out)

    def test_range_defaults_when_omitted(self):
        out = self.r._fmt_tool_umbrella_read(
            {'file_path': '/x'},
            {'type': 'text', 'file': {'filePath': '/x', 'numLines': 3}},
            None, False)
        self.assertIn('[0..+eof]', out)

    def test_no_file_degrades_to_none(self):
        # in-flight: no toolUseResult yet
        self.assertIsNone(self.r._fmt_tool_umbrella_read(
            {'file_path': '/x'}, None, None, False))
        # error: toolUseResult present but no 'file'
        self.assertIsNone(self.r._fmt_tool_umbrella_read(
            {'file_path': '/x'}, {'type': 'text'}, None, True))


class TestToolUmbrellaEdit(unittest.TestCase):
    """Edit umbrella formatter: only the resulting delta diff."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _tur(self):
        return {
            'filePath': '/tmp/x.py',
            'userModified': False,
            'structuredPatch': [{
                'oldStart': 1, 'oldLines': 3, 'newStart': 1, 'newLines': 3,
                'lines': [' ctx', '-removed line', '+added line', ' tail'],
            }],
        }

    def test_unified_reconstruction(self):
        unified = self.r._structured_patch_to_unified(
            self._tur()['structuredPatch'], 'tmp/x.py')
        self.assertIn('--- a/tmp/x.py', unified)
        self.assertIn('+++ b/tmp/x.py', unified)
        self.assertIn('@@ -1,3 +1,3 @@', unified)
        self.assertIn('-removed line', unified)
        self.assertIn('+added line', unified)

    def test_unified_derives_counts_when_absent(self):
        unified = self.r._structured_patch_to_unified([{
            'oldStart': 5, 'newStart': 5,
            'lines': [' a', '-b', '-c', '+d'],
        }], '/x')
        # oldLines = lines not starting with '+' = 3; newLines = not '-' = 2
        self.assertIn('@@ -5,3 +5,2 @@', unified)

    def test_unified_empty_patch_is_none(self):
        self.assertIsNone(self.r._structured_patch_to_unified([], '/x'))
        self.assertIsNone(self.r._structured_patch_to_unified(None, '/x'))

    def test_edit_with_delta_renders_diff(self):
        if not self.r.HAVE_DELTA:
            self.skipTest('delta not on PATH')
        out = self.r._fmt_tool_umbrella_edit({}, self._tur(), None, False)
        self.assertIsNotNone(out)
        # Changed content survives delta (delta colors at word granularity,
        # splicing SGR codes mid-line, so assert on the first word only).
        self.assertIn('removed', out)
        self.assertIn('added', out)
        # delta re-rendered: output differs from the raw unified diff text.
        raw = self.r._structured_patch_to_unified(
            self._tur()['structuredPatch'], '/tmp/x.py')
        self.assertNotEqual(out, raw)

    def test_edit_no_delta_uses_git_style(self):
        saved = self.r.HAVE_DELTA
        try:
            self.r.HAVE_DELTA = None
            out = self.r._fmt_tool_umbrella_edit({}, self._tur(), None, False)
            self.assertIsNotNone(out)
            self.assertIn('removed line', out)
            self.assertIn('added line', out)
        finally:
            self.r.HAVE_DELTA = saved

    def test_no_structured_patch_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_edit({}, None, None, True))
        self.assertIsNone(self.r._fmt_tool_umbrella_edit(
            {}, {'filePath': '/x'}, None, False))


class TestToolUmbrellaBash(unittest.TestCase):
    """Bash umbrella formatter (terminal-session look) + stderr coloring fix."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _result(self, **kw):
        base = {'stdout': '', 'stderr': '', 'interrupted': False,
                'isImage': False, 'noOutputExpected': False}
        base.update(kw)
        return base

    def test_command_line_carries_cmd_bg(self):
        out = self.r._fmt_tool_umbrella_bash(
            {'command': 'ls -la', 'description': 'list'},
            self._result(stdout='a\nb\n'), None, False)
        self.assertIn(self.r.CMD_BG, out)
        self.assertIn('$ ls -la', out)
        self.assertIn('# list', out)
        self.assertIn('a', out)

    def test_multiline_command_each_line_banded(self):
        out = self.r._fmt_tool_umbrella_bash(
            {'command': 'echo one\necho two'},
            self._result(stdout='one\ntwo\n'), None, False)
        # Both physical command lines carry the band.
        self.assertEqual(out.count(self.r.CMD_BG), 2)
        self.assertIn('$ echo one', out)

    def test_in_flight_renders_command_block_only(self):
        # No result yet (tur is None) -> still render the $ command block,
        # never declines.
        out = self.r._fmt_tool_umbrella_bash(
            {'command': 'sleep 100'}, None, None, False)
        self.assertIsNotNone(out)
        self.assertIn('$ sleep 100', out)
        self.assertIn(self.r.CMD_BG, out)

    def test_raw_content_fallback_when_no_structured_result(self):
        # No structured toolUseResult, but the raw tool_result content is
        # present -> the command output is recovered from raw_content.
        out = self.r._fmt_tool_umbrella_bash(
            {'command': 'echo hi'}, None, 'RAW_OUTPUT_TEXT', False)
        self.assertIn('$ echo hi', out)
        self.assertIn('RAW_OUTPUT_TEXT', out)

    def test_stderr_with_ansi_not_red_wrapped(self):
        # stderr already carrying ESC -> emitted raw, not wrapped in RED.
        ansi_stderr = '\x1b[31mlinter error\x1b[0m'
        out = self.r._fmt_tur_bash(self._result(stdout='ok', stderr=ansi_stderr))
        self.assertIn(ansi_stderr, out)
        # The RED tint should not lead the stderr body.
        if self.r.RED:
            self.assertNotIn(f'{self.r.RED}{ansi_stderr}', out)

    def test_plain_stderr_is_red_tinted(self):
        out = self.r._fmt_tur_bash(self._result(stdout='ok', stderr='boom'))
        self.assertIn('stderr', out)
        if self.r.RED:
            self.assertIn(f'{self.r.RED}boom', out)

    def test_stderr_rule_present(self):
        out = self.r._fmt_tur_bash(self._result(stderr='x'))
        self.assertIn('stderr', out)


class TestToolUmbrellaWrite(unittest.TestCase):
    """Write umbrella formatter: header + capped raw content."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_short_content_shown_whole_no_footer(self):
        out = self.r._fmt_tool_umbrella_write(
            {'file_path': '/tmp/a.txt', 'content': 'line1\nline2\nline3'},
            None, None, False)
        self.assertIn('🔧 Write', out)
        self.assertIn('/tmp/a.txt', out)
        self.assertIn('3 lines', out)
        self.assertIn('line1', out)
        self.assertIn('line3', out)
        self.assertNotIn('more lines', out)

    def test_long_content_truncated_with_footer(self):
        body = '\n'.join(f'L{i}' for i in range(25))
        out = self.r._fmt_tool_umbrella_write(
            {'file_path': '/tmp/big', 'content': body}, None, None, False)
        self.assertIn('25 lines', out)
        # First 10 shown, 11th onward not.
        self.assertIn('L0', out)
        self.assertIn('L9', out)
        self.assertNotIn('L10', out)
        # Footer with the remaining count + the expand hint.
        self.assertIn('15 more lines', out)
        self.assertIn('expand the Write item', out)

    def test_missing_content_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_write(
            {'file_path': '/x'}, None, None, False))
        self.assertIsNone(self.r._fmt_tool_umbrella_write(
            {'file_path': '/x', 'content': ''}, None, None, False))


class TestToolUmbrellaNotebookEdit(unittest.TestCase):
    """NotebookEdit umbrella formatter: header + diff or capped source."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_header_reflects_mode_and_cell_type(self):
        out = self.r._fmt_tool_umbrella_notebook_edit(
            {'notebook_path': '/n.ipynb', 'cell_id': 'c7',
             'cell_type': 'code', 'edit_mode': 'replace',
             'new_source': 'print(1)'},
            None, None, False)
        self.assertIn('🔧 NotebookEdit', out)
        self.assertIn('/n.ipynb', out)
        self.assertIn('replace', out)
        self.assertIn('code', out)
        self.assertIn('cell c7', out)

    def test_replace_with_patch_renders_diff(self):
        tur = {
            'filePath': '/n.ipynb',
            'structuredPatch': [{
                'oldStart': 1, 'oldLines': 1, 'newStart': 1, 'newLines': 1,
                'lines': ['-print(1)', '+print(2)'],
            }],
        }
        out = self.r._fmt_tool_umbrella_notebook_edit(
            {'notebook_path': '/n.ipynb', 'cell_id': 'c1',
             'cell_type': 'code', 'edit_mode': 'replace',
             'new_source': 'print(2)'},
            tur, None, False)
        self.assertIn('🔧 NotebookEdit', out)
        # A diff was rendered. delta colors at word granularity, splicing
        # SGR mid-token, so the literal 'print(1)' may not be contiguous —
        # assert on the surviving 'print(' stem + both changed digits.
        self.assertIn('print(', out)
        self.assertIn('1', out)
        self.assertIn('2', out)

    def test_no_patch_falls_back_to_capped_source(self):
        body = '\n'.join(f'cell-line-{i}' for i in range(20))
        out = self.r._fmt_tool_umbrella_notebook_edit(
            {'notebook_path': '/n.ipynb', 'cell_id': 'c1',
             'cell_type': 'markdown', 'edit_mode': 'insert',
             'new_source': body},
            None, None, False)
        self.assertIn('markdown', out)
        self.assertIn('cell-line-0', out)
        self.assertIn('cell-line-9', out)
        self.assertNotIn('cell-line-10', out)
        self.assertIn('more lines', out)
        self.assertIn('expand the NotebookEdit item', out)

    def test_no_source_and_no_patch_degrades(self):
        # delete-mode with no new_source and no structured result.
        self.assertIsNone(self.r._fmt_tool_umbrella_notebook_edit(
            {'notebook_path': '/n.ipynb', 'cell_id': 'c1',
             'edit_mode': 'delete'},
            None, None, False))


class TestToolUmbrellaMcp(unittest.TestCase):
    """MCP umbrella formatter: ``server ▸ tool`` header + JSON request/result."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_header_splits_server_and_tool(self):
        out = self.r._fmt_tool_umbrella_mcp(
            'mcp__slack__post_message', {'channel': '#x'},
            None, None, False)
        self.assertIn('🔧 slack ▸ post_message', out)

    def test_tool_segment_keeps_inner_double_underscore(self):
        # The tool name itself may carry ``__`` — only the first two
        # segments are server/prefix; the rest re-join as the tool.
        out = self.r._fmt_tool_umbrella_mcp(
            'mcp__gh__list__issues', {}, None, None, False)
        self.assertIn('🔧 gh ▸ list__issues', out)

    def test_json_request_is_colored(self):
        out = self.r._fmt_tool_umbrella_mcp(
            'mcp__s__t', {'a': 1, 'b': 'x'}, None, None, False)
        # The request fields survive (pretty-printed JSON).
        self.assertIn('"a"', out)
        self.assertIn('"b"', out)

    def test_json_result_detected_and_colored(self):
        out = self.r._fmt_tool_umbrella_mcp(
            'mcp__s__t', {}, '{"rows": 3, "ok": true}', None, False)
        self.assertIn('"rows"', out)
        self.assertIn('"ok"', out)
        # Re-dumped pretty (a newline in the JSON body).
        self.assertIn('\n', out)

    def test_non_json_result_passes_through(self):
        out = self.r._fmt_tool_umbrella_mcp(
            'mcp__s__t', {}, 'plain text result', None, False)
        self.assertIn('plain text result', out)

    def test_tur_string_used_as_result(self):
        out = self.r._fmt_tool_umbrella_mcp(
            'mcp__s__t', {}, 'IGNORED-RAW', None, False)
        # When toolUseResult is a plain string, prefer it over raw_content.
        out2 = self.r._fmt_tool_umbrella_mcp(
            'mcp__s__t', {}, None, [{'type': 'text', 'text': 'from-raw'}],
            False)
        self.assertIn('from-raw', out2)

    def test_malformed_name_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_mcp(
            'mcp__only', {}, None, None, False))
        self.assertIsNone(self.r._fmt_tool_umbrella_mcp(
            'mcp', {}, None, None, False))

    def test_dispatcher_routes_mcp_prefix(self):
        # An mcp__-prefixed part with no registry entry still resolves via
        # the dispatcher's MCP branch (not treated as unregistered).
        import json as _json
        import tempfile
        records = [
            {'type': 'user', 'uuid': 'u1',
             'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [{
                 'type': 'tool_use', 'id': 'tu_1',
                 'name': 'mcp__weather__forecast',
                 'input': {'city': 'NYC'},
             }]}},
            {'type': 'user', 'uuid': 'r1', 'parentUuid': 'a1',
             'message': {'role': 'user', 'content': [{
                 'type': 'tool_result', 'tool_use_id': 'tu_1',
                 'content': 'sunny',
             }]},
             'toolUseResult': None},
        ]
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl',
                                         delete=False) as f:
            for rec in records:
                f.write(_json.dumps(rec) + '\n')
            path = f.name
        try:
            out = self.r._render_tool_umbrella(('tool', path, 1))
            self.assertIsNotNone(out)
            self.assertIn('🔧 weather ▸ forecast', out)
            self.assertIn('sunny', out)
        finally:
            os.unlink(path)


class TestToolUmbrellaTask(unittest.TestCase):
    """Background-task family umbrella formatters (compact header + result)."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _fmt(self, op, inp, tur=None, raw=None):
        return self.r._TOOL_UMBRELLA_FORMATTERS[op](inp, tur, raw, False)

    def test_all_ops_registered(self):
        for op in ('TaskCreate', 'TaskUpdate', 'TaskGet', 'TaskList',
                   'TaskOutput', 'TaskStop'):
            self.assertIn(op, self.r._TOOL_UMBRELLA_FORMATTERS)

    def test_create_header_subject(self):
        # In-tree Task* CLI shape: subject/description/activeForm.
        out = self._fmt('TaskCreate',
                        {'subject': 'Analyze evict_struct.h',
                         'description': 'understand structs',
                         'activeForm': 'Reading'},
                        raw='Task #1 created successfully')
        self.assertIn('🔧 TaskCreate', out)
        self.assertIn('Analyze evict_struct.h', out)
        self.assertIn('Task #1 created', out)

    def test_create_header_subagent_and_prompt(self):
        # SDK-style spawn shape: subagent_type/description/prompt.
        out = self._fmt('TaskCreate',
                        {'subagent_type': 'explorer',
                         'description': 'scan the repo',
                         'prompt': 'first line of prompt\nsecond line'})
        self.assertIn('explorer', out)
        self.assertIn('scan the repo', out)
        self.assertIn('first line of prompt', out)
        self.assertNotIn('second line', out)

    def test_update_header_and_result(self):
        out = self._fmt('TaskUpdate',
                        {'taskId': '1', 'status': 'completed'},
                        tur={'success': True, 'taskId': '1',
                             'updatedFields': ['status'],
                             'statusChange': {'from': 'pending',
                                              'to': 'completed'}})
        self.assertIn('🔧 TaskUpdate', out)
        self.assertIn('#1', out)
        # ``status=`` is MUTE then RESET before the value, so it's not
        # contiguous — assert the label and value separately.
        self.assertIn('status=', out)
        # Result reused -> _fmt_tur_task_update renders the transition.
        self.assertIn('pending', out)
        self.assertIn('completed', out)

    def test_list_dump_result_reused(self):
        out = self._fmt('TaskList', {},
                        tur={'task': {'id': '1', 'subject': 'X'}})
        self.assertIn('🔧 TaskList', out)
        # _fmt_tur_task_dump dumps the task body.
        self.assertIn('subject', out)

    def test_get_header_with_id(self):
        out = self._fmt('TaskGet', {'task_id': '7'})
        self.assertIn('🔧 TaskGet', out)
        self.assertIn('#7', out)

    def test_stop_minimal(self):
        out = self._fmt('TaskStop', {'taskId': '3'})
        self.assertIn('🔧 TaskStop', out)
        self.assertIn('#3', out)

    def test_output_json_result_colored(self):
        # A JSON-looking string result is colored (str tur path).
        out = self._fmt('TaskOutput', {'taskId': '2'},
                        tur='{"lines": 5}')
        self.assertIn('🔧 TaskOutput', out)
        self.assertIn('"lines"', out)


class TestToolUmbrellaGrepGlob(unittest.TestCase):
    """Grep / Glob umbrella formatters: request header + results body."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_grep_query_and_count_result(self):
        out = self.r._fmt_tool_umbrella_grep(
            {'pattern': 'foo', 'path': 'src'},
            {'numFiles': 2, 'numMatches': 5}, None, False)
        self.assertIn('🔧 Grep', out)
        self.assertIn('/foo/', out)
        self.assertIn('src', out)
        self.assertIn('2 files', out)
        self.assertIn('5 matches', out)

    def test_grep_content_mode_result(self):
        out = self.r._fmt_tool_umbrella_grep(
            {'pattern': 'bar', 'output_mode': 'content'},
            {'filenames': ['a.py'], 'numFiles': 1, 'numLines': 3,
             'content': 'a.py:1:bar'},
            None, False)
        self.assertIn('🔧 Grep', out)
        self.assertIn('a.py:1:bar', out)

    def test_grep_no_result_shows_request_only(self):
        out = self.r._fmt_tool_umbrella_grep(
            {'pattern': 'baz'}, None, None, False)
        self.assertIn('🔧 Grep', out)
        self.assertIn('/baz/', out)

    def test_grep_empty_input_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_grep(
            {}, {'numFiles': 1, 'numMatches': 1}, None, False))

    def test_glob_pattern_and_filenames(self):
        out = self.r._fmt_tool_umbrella_glob(
            {'pattern': '**/*.py', 'path': 'lib'},
            {'filenames': ['lib/a.py', 'lib/b.py'], 'numFiles': 2},
            None, False)
        self.assertIn('🔧 Glob', out)
        self.assertIn('**/*.py', out)
        self.assertIn('lib/a.py', out)
        self.assertIn('lib/b.py', out)
        self.assertIn('2 files', out)

    def test_glob_no_result_shows_request_only(self):
        out = self.r._fmt_tool_umbrella_glob(
            {'pattern': '*.md'}, None, None, False)
        self.assertIn('🔧 Glob', out)
        self.assertIn('*.md', out)

    def test_glob_empty_input_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_glob(
            {}, {'filenames': [], 'numFiles': 0}, None, False))


class TestToolUmbrellaWeb(unittest.TestCase):
    """WebFetch / WebSearch umbrella formatters (result lives in raw content)."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_webfetch_header_url_and_prompt(self):
        out = self.r._fmt_tool_umbrella_webfetch(
            {'url': 'https://docs.python.org/3/library/bisect.html',
             'prompt': 'What does bisect_right return?'},
            None,
            '# bisect_right\n\nReturns an insertion point **after** entries.',
            False)
        self.assertIn('🔧 WebFetch', out)
        self.assertIn('https://docs.python.org/3/library/bisect.html', out)
        self.assertIn('What does bisect_right return?', out)
        # The fetched markdown body is present (heading text survives md2ansi).
        self.assertIn('bisect_right', out)
        self.assertIn('insertion point', out)

    def test_webfetch_plain_body_passes_through(self):
        out = self.r._fmt_tool_umbrella_webfetch(
            {'url': 'https://x'}, None, 'just a plain sentence.', False)
        self.assertIn('just a plain sentence.', out)

    def test_webfetch_no_url_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_webfetch(
            {'prompt': 'q'}, None, 'body', False))

    def test_webfetch_no_result_shows_request(self):
        out = self.r._fmt_tool_umbrella_webfetch(
            {'url': 'https://y', 'prompt': 'q'}, None, None, False)
        self.assertIn('🔧 WebFetch', out)
        self.assertIn('https://y', out)

    def test_websearch_header_query_and_results(self):
        body = ('Web search results for query: "git diff"\n\n'
                'Links: [{"title":"Git docs","url":"https://git-scm.com"}]')
        out = self.r._fmt_tool_umbrella_websearch(
            {'query': 'git diff'}, None, body, False)
        self.assertIn('🔧 WebSearch', out)
        self.assertIn('git diff', out)
        self.assertIn('Web search results for query', out)
        self.assertIn('git-scm.com', out)

    def test_websearch_allowed_domains_chip(self):
        out = self.r._fmt_tool_umbrella_websearch(
            {'query': 'q', 'allowed_domains': ['git-scm.com']},
            None, 'No links found.', False)
        self.assertIn('only: git-scm.com', out)

    def test_websearch_no_query_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_websearch(
            {}, None, 'results', False))


class TestToolUmbrellaSearchSkillSend(unittest.TestCase):
    """ToolSearch / Skill / SendMessage umbrella formatters."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_toolsearch_query_and_matches(self):
        out = self.r._fmt_tool_umbrella_toolsearch(
            {'query': 'select:SendMessage', 'max_results': 1},
            {'matches': [{'name': 'SendMessage', 'description': 'send a msg'}],
             'query': 'select:SendMessage', 'total_deferred_tools': 27},
            None, False)
        self.assertIn('🔧 ToolSearch', out)
        self.assertIn('select:SendMessage', out)
        # _fmt_tur_grep_or_search renders the matched tool name.
        self.assertIn('SendMessage', out)
        self.assertIn('send a msg', out)

    def test_toolsearch_no_query_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_toolsearch(
            {}, {'matches': [], 'query': '', 'total_deferred_tools': 0},
            None, False))

    def test_skill_header_and_ack(self):
        out = self.r._fmt_tool_umbrella_skill(
            {'skill': 'code-review', 'args': '--high'},
            {'commandName': 'code-review', 'success': True}, None, False)
        self.assertIn('🔧 Skill', out)
        self.assertIn('code-review', out)
        self.assertIn('--high', out)
        # _fmt_tur_skill renders the ok marker.
        self.assertIn('ok', out)

    def test_skill_empty_input_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_skill(
            {}, {'commandName': 'x', 'success': True}, None, False))

    def test_send_message_header_and_ack(self):
        out = self.r._fmt_tool_umbrella_send_message(
            {'recipient': 'worker-1', 'summary': 'do X',
             'message': 'please do X now'},
            {'success': True, 'message': 'Delivered to worker-1'},
            None, False)
        self.assertIn('🔧 SendMessage', out)
        self.assertIn('worker-1', out)
        self.assertIn('please do X now', out)
        # Delivery status surfaced.
        self.assertIn('delivered', out)

    def test_send_message_content_rendered_once_with_routing(self):
        # The success+routing result echoes the message in routing.content;
        # the umbrella must render the message exactly ONCE (from request),
        # never twice.
        msg = 'UNIQUE-BODY-TOKEN please proceed'
        out = self.r._fmt_tool_umbrella_send_message(
            {'recipient': 'w', 'summary': 's', 'message': msg},
            {'success': True, 'message': "Message sent to w's inbox",
             'routing': {'sender': 'lead', 'target': '@w', 'summary': 's',
                         'content': msg}},
            None, False)
        self.assertEqual(out.count('UNIQUE-BODY-TOKEN'), 1)
        # Delivery status + the sender are surfaced from the result.
        self.assertIn('✓ delivered', out)
        self.assertIn('(from lead)', out)
        # Not a raw JSON dump.
        self.assertNotIn('"routing"', out)

    def test_send_message_in_flight_shows_request_only(self):
        # No result yet (tur=None): just the request side, no status line.
        out = self.r._fmt_tool_umbrella_send_message(
            {'recipient': 'w', 'summary': 's', 'message': 'hi there'},
            None, None, False)
        self.assertIn('🔧 SendMessage', out)
        self.assertIn('→ w', out)
        self.assertIn('hi there', out)
        self.assertNotIn('delivered', out)
        self.assertNotIn('✗', out)

    def test_send_message_error_status(self):
        out = self.r._fmt_tool_umbrella_send_message(
            {'recipient': 'ghost', 'message': 'hello'},
            {'success': False, 'message': "No agent named 'ghost'"},
            None, False)
        self.assertIn('✗', out)
        self.assertIn("No agent named 'ghost'", out)
        # The request message still renders once.
        self.assertEqual(out.count('hello'), 1)
        self.assertNotIn('delivered', out)

    def test_send_message_json_body_colored_once(self):
        out = self.r._fmt_tool_umbrella_send_message(
            {'recipient': 'w', 'message': '{"cmd": "run", "n": 2}'},
            None, None, False)
        self.assertIn('"cmd"', out)
        self.assertIn('"n"', out)

    def test_send_message_empty_input_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_send_message(
            {}, {'success': True, 'message': 'ok'}, None, False))


class TestToolUmbrellaTodoWrite(unittest.TestCase):
    """TodoWrite umbrella formatter: a per-status glyph checklist."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_each_status_renders_its_glyph(self):
        out = self.r._fmt_tool_umbrella_todowrite(
            {'todos': [
                {'content': 'do A', 'status': 'completed',
                 'activeForm': 'doing A'},
                {'content': 'do B', 'status': 'in_progress',
                 'activeForm': 'doing B'},
                {'content': 'do C', 'status': 'pending',
                 'activeForm': 'doing C'},
            ]}, None, None, False)
        self.assertIn('🔧 TodoWrite', out)
        self.assertIn('✓ do A', out)
        self.assertIn('▸ do B', out)
        self.assertIn('☐ do C', out)

    def test_in_progress_yellow_completed_green(self):
        out = self.r._fmt_tool_umbrella_todowrite(
            {'todos': [
                {'content': 'wip', 'status': 'in_progress'},
                {'content': 'done', 'status': 'completed'},
            ]}, None, None, False)
        if self.r.YELLOW:
            self.assertIn(f'{self.r.YELLOW}▸ wip', out)
        if self.r.GREEN:
            self.assertIn(f'{self.r.GREEN}✓ done', out)

    def test_empty_todos_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_todowrite(
            {'todos': []}, None, None, False))

    def test_missing_todos_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_todowrite(
            {}, None, None, False))

    def test_malformed_entries_skipped(self):
        out = self.r._fmt_tool_umbrella_todowrite(
            {'todos': [
                'not-a-dict',
                {'status': 'pending'},          # no content
                {'content': 'real', 'status': 'pending'},
            ]}, None, None, False)
        self.assertIn('☐ real', out)
        self.assertNotIn('not-a-dict', out)

    def test_unknown_status_falls_back_to_box(self):
        out = self.r._fmt_tool_umbrella_todowrite(
            {'todos': [{'content': 'weird', 'status': 'paused'}]},
            None, None, False)
        self.assertIn('☐ weird', out)


class TestToolUmbrellaExitPlanMode(unittest.TestCase):
    """ExitPlanMode umbrella formatter: rendered plan + approval status."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_plan_markdown_rendered(self):
        out = self.r._fmt_tool_umbrella_exit_plan_mode(
            {'plan': '# My Plan\n\n1. step one\n2. step two'},
            None, None, False)
        self.assertIn('🔧 ExitPlanMode', out)
        self.assertIn('My Plan', out)
        self.assertIn('step one', out)

    def test_approval_status_line(self):
        out = self.r._fmt_tool_umbrella_exit_plan_mode(
            {'plan': '# P'}, 'User has approved your plan.', None, False)
        self.assertIn('approved', out)

    def test_rejection_status_line(self):
        out = self.r._fmt_tool_umbrella_exit_plan_mode(
            {'plan': '# P'}, 'The user rejected the plan.', None, False)
        self.assertIn('rejected', out)

    def test_unknown_result_omits_status(self):
        out = self.r._fmt_tool_umbrella_exit_plan_mode(
            {'plan': '# P'}, 'something else', None, False)
        self.assertNotIn('approved', out)
        self.assertNotIn('rejected', out)

    def test_missing_plan_degrades(self):
        self.assertIsNone(self.r._fmt_tool_umbrella_exit_plan_mode(
            {}, 'User has approved your plan.', None, False))
        self.assertIsNone(self.r._fmt_tool_umbrella_exit_plan_mode(
            {'plan': ''}, None, None, False))


if __name__ == '__main__':
    unittest.main()
