"""Unit tests for the Codex rollout-line → ``Record`` parser in
``recipes/browse-agents-codex`` (``_parse_record`` with its ``_ParseCtx``).

Fixtures are small synthetic lines modelled on the 0.104 shape (raw
``response_item`` lines plus ``event_msg/user_message``) and the 0.161
shape (``ordinal``, ``event_msg/item_completed`` twins).
"""

import importlib.util
import json
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-agents-codex'


def _stub_browse_tui():
    mod = types.ModuleType('browse_tui')

    class _Stub:
        def __init__(self, *a, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    mod.Action = mod.Browser = mod.BrowserConfig = mod.Item = _Stub
    mod.upsert = lambda id_, parent_id, *, where=None, **fields: (
        'upsert', id_, parent_id, fields, where)
    mod.mod = lambda id_, parent_id=None, *, where=None, **fields: (
        'mod', id_, parent_id, fields, where)
    mod.set_preview_op = lambda id_, text: ('set_preview', id_, text)
    mod.visible_items = lambda state: []
    mod.recipe_argv = lambda argv=None: list(sys.argv[1:] if argv is None else argv)
    sys.modules['browse_tui'] = mod


def _load_recipe():
    _stub_browse_tui()
    name = '_browse_agents_codex_parse'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_TS = '2026-10-08T00:53:57.637Z'


def _line(type_, payload, ordinal=None):
    raw = {'timestamp': _TS, 'type': type_, 'payload': payload}
    if ordinal is not None:
        raw['ordinal'] = ordinal
    return raw


def _ri(**payload):
    return _line('response_item', payload)


def _ev(**payload):
    return _line('event_msg', payload)


def _item(item, turn_id='turn-1'):
    return _ev(type='item_completed', turn_id=turn_id, item=item)


_EXEC_JS = ('const r = await tools.exec_command({"cmd":"ls -la \\"x y\\"",'
            '"workdir":"/w","yield_time_ms":10000,"max_output_tokens":20000});'
            '\ntext(r.output);\n')
_PATCH = ('*** Begin Patch\n*** Update File: src/a.py\n@@\n-old\n+new\n'
          '*** Add File: docs/b.md\n+hello\n*** End Patch\n')
_PATCH_JS = ('const patch = ' + json.dumps(_PATCH) + ';\n'
             'const r = await tools.apply_patch(patch);\n'
             'text(typeof r === "string" ? r : JSON.stringify(r));\n')


class CodexParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self.ctx = self.r._ParseCtx()
        self.n = 0

    def parse(self, raw):
        rec = self.r._parse_record(raw, self.n, self.ctx)
        self.n += 1
        return rec

    def level(self, rec):
        return self.r._record_min_level(rec)

    # ---- framing ---------------------------------------------------------

    def test_session_meta_is_detailed_meta(self):
        rec = self.parse(_line('session_meta', {
            'id': 'thread-1', 'cwd': '/w', 'cli_version': '0.161.0',
            'originator': 'codex_cli_rs', 'source': 'cli'}))
        self.assertEqual(rec.kind, 'meta')
        self.assertEqual(self.level(rec), 5)
        self.assertIn('codex 0.161.0', rec.summary)
        import datetime
        want = datetime.datetime(2026, 10, 8, 0, 53, 57, 637000,
                                 tzinfo=datetime.timezone.utc).timestamp()
        self.assertAlmostEqual(rec.ts, want, places=2)

    def test_turn_context_sets_model_for_later_assistant_records(self):
        rec = self.parse(_line('turn_context', {
            'turn_id': 'turn-1', 'model': 'gpt-5.6', 'approval_policy': 'never',
            'sandbox_policy': {'type': 'danger-full-access'}}))
        self.assertEqual((rec.kind, self.level(rec)), ('meta', 5))
        self.assertIn('model gpt-5.6', rec.summary)
        self.assertIn('sandbox danger-full-access', rec.summary)
        asst = self.parse(_item({'type': 'AgentMessage', 'id': 'm1',
                                 'content': [{'type': 'text', 'text': 'hi'}],
                                 'phase': 'commentary'}))
        self.assertEqual(asst.meta.get('model'), 'gpt-5.6')

    def test_turn_boundaries(self):
        start = self.parse(_ev(type='task_started', turn_id='turn-1'))
        self.assertEqual((start.kind, start.turn_id), ('turn_start', 'turn-1'))
        self.assertEqual(self.level(start), 5)
        self.parse(_ev(type='token_count', info={
            'total_token_usage': {'total_tokens': 12345},
            'last_token_usage': {'total_tokens': 100},
            'model_context_window': 400000},
            rate_limits={'primary': {'used_percent': 12.5}}))
        end = self.parse(_ev(type='task_complete', turn_id='turn-1',
                             last_agent_message='done', duration_ms=15700))
        self.assertEqual(end.kind, 'turn_end')
        self.assertEqual(self.level(end), 4)
        self.assertEqual(end.meta['duration_ms'], 15700)
        self.assertEqual(end.meta['tokens'], 12345)
        self.assertEqual(end.summary, 'turn complete · 15.7s')
        self.assertTrue(self.r._is_turn_close(end))

    def test_turn_aborted_and_failed(self):
        ab = self.parse(_ev(type='turn_aborted', turn_id='t', reason='interrupted',
                            duration_ms=1500))
        self.assertEqual(ab.kind, 'turn_end')
        self.assertEqual(ab.summary, 'turn aborted (interrupted) · 1.5s')
        fail = self.parse(_ev(type='task_complete', turn_id='t',
                              error={'message': 'token expired'}))
        self.assertEqual(fail.kind, 'turn_end')
        self.assertEqual(fail.summary, 'turn failed: token expired')
        self.assertNotIn('duration_ms', fail.meta)

    def test_token_count_is_level_six_meta_with_summary(self):
        rec = self.parse(_ev(type='token_count', info={
            'total_token_usage': {'total_tokens': 2500},
            'last_token_usage': {'total_tokens': 50},
            'model_context_window': 1000},
            rate_limits={'primary': {'used_percent': 3.0}}))
        self.assertEqual((rec.kind, rec.tag, self.level(rec)),
                         ('meta', 'token_count', 6))
        self.assertEqual(rec.summary,
                         'tokens · total 2,500 · last 50 · window 1,000 · rate 3%')

    def test_token_usage_record_world_state_settings(self):
        usage = self.parse(_line('token_usage_record', {
            'turn_id': 't', 'usage': {'input_tokens': 10, 'output_tokens': 5,
                                      'total_tokens': 15}}))
        self.assertEqual((usage.kind, self.level(usage)), ('meta', 6))
        self.assertEqual(usage.summary, 'usage · in 10 · out 5 · total 15')
        ws = self.parse(_line('world_state', {'full': True, 'state': {}}))
        self.assertEqual((ws.kind, self.level(ws), ws.summary),
                         ('meta', 6, 'world_state (full)'))
        st = self.parse(_ev(type='thread_settings_applied', thread_id='x',
                            thread_settings={'model': 'gpt-5.7',
                                             'reasoning_effort': 'high'}))
        self.assertEqual((st.kind, self.level(st)), ('meta', 5))
        self.assertEqual(st.summary, 'settings · model gpt-5.7 · reasoning_effort high')
        self.assertEqual(self.ctx.model, 'gpt-5.7')

    def test_unknown_shapes(self):
        top = self.parse(_line('something_new', {'a': 1}))
        self.assertEqual((top.kind, self.level(top)), ('unknown', 7))
        ev = self.parse(_ev(type='brand_new_event'))
        self.assertEqual((ev.kind, ev.tag), ('unknown', 'event:brand_new_event'))
        ri = self.parse(_ri(type='web_search_call', id='w1'))
        self.assertEqual((ri.kind, ri.tag), ('unknown', 'response_item:web_search_call'))
        item = self.parse(_item({'type': 'Plan', 'id': 'p1'}))
        self.assertEqual((item.kind, item.tag), ('unknown', 'item:Plan'))

    # ---- prompts ---------------------------------------------------------

    def test_user_message_event_is_the_prompt_in_the_0104_shape(self):
        raw = self.parse(_ri(type='message', role='user', content=[
            {'type': 'input_text', 'text': '<environment_context>…</environment_context>'},
            {'type': 'input_text', 'text': 'fix the build'}]))
        self.assertEqual((raw.kind, self.level(raw)), ('meta', 6))
        self.assertTrue(raw.is_meta)
        self.assertFalse(self.r._is_turn_root(raw))
        self.assertIn('fix the build', raw.text)
        prompt = self.parse(_ev(type='user_message', message='fix the build',
                                images=[]))
        self.assertEqual((prompt.kind, prompt.tag, prompt.text),
                         ('user_prompt', 'user', 'fix the build'))
        self.assertTrue(prompt.is_voice)
        self.assertTrue(self.r._is_turn_root(prompt))
        self.assertEqual(self.level(prompt), 1)

    def test_user_message_item_is_the_prompt_in_the_0161_shape(self):
        self.parse(_ri(type='message', id='rs_1', role='user', content=[
            {'type': 'input_text', 'text': 'fix the build'}]))
        prompt = self.parse(_item({'type': 'UserMessage', 'id': 'u-1', 'content': [
            {'type': 'text', 'text': 'fix the build', 'text_elements': []}]}))
        self.assertEqual((prompt.kind, prompt.id, prompt.text),
                         ('user_prompt', 'u-1', 'fix the build'))
        self.assertTrue(self.r._is_turn_root(prompt))
        self.assertEqual(prompt.turn_id, 'turn-1')
        self.assertEqual(self.r._summarise_message(prompt), 'user: fix the build')

    def test_developer_message_is_injected_system_text(self):
        rec = self.parse(_ri(type='message', role='developer', content=[
            {'type': 'input_text', 'text': 'AGENTS.md says\nbe nice'}]))
        self.assertEqual((rec.kind, rec.tag), ('system', 'developer'))
        self.assertTrue(rec.is_meta)
        self.assertEqual(self.level(rec), 5)
        self.assertEqual(rec.summary, 'developer: AGENTS.md says')
        self.assertEqual(rec.meta['span_label'], 'developer')

    # ---- assistant text and reasoning ------------------------------------

    def test_agent_message_item_then_raw_twin_is_deduplicated(self):
        item = self.parse(_item({'type': 'AgentMessage', 'id': 'msg_1',
                                 'content': [{'type': 'text', 'text': 'All done.'}],
                                 'phase': 'final_answer'}))
        self.assertEqual((item.kind, item.text, item.end_turn),
                         ('assistant_text', 'All done.', True))
        self.assertTrue(item.is_voice)
        self.assertEqual(self.level(item), 1)   # final answers are the summary
        dup = self.parse(_ri(type='message', id='msg_1', role='assistant', content=[
            {'type': 'output_text', 'text': 'All done.'}], phase='final_answer'))
        self.assertEqual((dup.kind, dup.tag, self.level(dup)), ('meta', 'raw', 6))
        self.assertFalse(dup.is_voice)
        self.assertIn('assistant message', dup.summary)

    def test_raw_assistant_message_without_item_is_voice(self):
        rec = self.parse(_ri(type='message', id='msg_9', role='assistant', content=[
            {'type': 'output_text', 'text': 'Working on it'}], phase='commentary'))
        self.assertEqual((rec.kind, rec.text, rec.end_turn),
                         ('assistant_text', 'Working on it', False))
        self.assertTrue(rec.is_voice)
        self.assertEqual(self.level(rec), 2)

    def test_encrypted_reasoning_is_a_placeholder_at_level_six(self):
        item = self.parse(_item({'type': 'Reasoning', 'id': 'rs_1',
                                 'summary_text': [], 'raw_content': []}))
        self.assertEqual((item.kind, item.tag, item.text), ('thinking', 'reasoning', ''))
        self.assertTrue(item.meta['encrypted'])
        self.assertFalse(item.is_voice)
        self.assertEqual(self.level(item), 6)
        self.assertEqual(self.r._summarise_message(item), '🧠 reasoning (encrypted)')
        dup = self.parse(_ri(type='reasoning', id='rs_1', summary=[],
                             encrypted_content='…'))
        self.assertEqual((dup.kind, self.level(dup)), ('meta', 6))

    def test_readable_reasoning_summary_is_voice(self):
        item = self.parse(_item({'type': 'Reasoning', 'id': 'rs_2',
                                 'summary_text': ['Plan first', 'then act']}))
        self.assertEqual((item.kind, item.text), ('thinking', 'Plan first\n\nthen act'))
        self.assertTrue(item.is_voice)
        self.assertEqual(self.level(item), 2)
        raw = self.parse(_ri(type='reasoning', id='rs_3', summary=[
            {'type': 'summary_text', 'text': 'raw summary'}]))
        self.assertEqual((raw.kind, raw.text), ('thinking', 'raw summary'))

    # ---- tools -----------------------------------------------------------

    def test_exec_custom_tool_unwraps_to_bash_canon(self):
        rec = self.parse(_ri(type='custom_tool_call', id='ctc_1', call_id='call_1',
                             name='exec', input=_EXEC_JS, status='completed'))
        self.assertEqual((rec.kind, rec.tag), ('tool_call', 'assistant'))
        tool = rec.tool
        self.assertEqual((tool['canon'], tool['name'], tool['call_id']),
                         ('bash', 'exec_command', 'call_1'))
        self.assertEqual(tool['input'], {'command': 'ls -la "x y"', 'cwd': '/w'})
        self.assertEqual(self.r._summarise_message(rec),
                         '🔧 exec_command(ls -la "x y")')
        self.assertEqual(self.level(rec), 4)
        self.assertEqual(self.ctx.pending, [('call_1', 'bash')])

    def test_exec_custom_tool_unwraps_apply_patch_to_edit_canon(self):
        rec = self.parse(_ri(type='custom_tool_call', id='ctc_2', call_id='call_2',
                             name='exec', input=_PATCH_JS))
        tool = rec.tool
        self.assertEqual((tool['canon'], tool['name']), ('edit', 'apply_patch'))
        self.assertEqual(tool['input']['file_path'], 'src/a.py, docs/b.md')
        self.assertEqual(tool['input']['patch'], _PATCH)
        self.assertEqual(self.level(rec), 3)
        self.assertEqual(self.r._summarise_message(rec),
                         '🔧 apply_patch(src/a.py, docs/b.md)')

    def test_exec_with_unrecognised_script_keeps_the_script(self):
        rec = self.parse(_ri(type='custom_tool_call', call_id='c', name='exec',
                             input='console.log(1)'))
        self.assertEqual((rec.tool['canon'], rec.tool['name']), ('other', 'exec'))
        self.assertEqual(rec.tool['input'], {'script': 'console.log(1)'})

    def test_function_call_arguments_are_parsed(self):
        rec = self.parse(_ri(type='function_call', call_id='fc_1', name='shell',
                             arguments=json.dumps({'command': ['bash', '-lc', 'ls'],
                                                   'workdir': '/w'})))
        self.assertEqual(rec.tool['canon'], 'bash')
        self.assertEqual(rec.tool['input']['command'], 'bash -lc ls')
        spawn = self.parse(_ri(type='function_call', call_id='fc_2',
                               name='spawn_agent', arguments='{"task": "x"}'))
        self.assertEqual((spawn.tool['canon'], spawn.tool['input']), ('spawn', {'task': 'x'}))
        mcp = self.parse(_ri(type='function_call', call_id='fc_3',
                             name='mcp__jira__search', arguments='not json'))
        self.assertEqual((mcp.tool['canon'], mcp.tool['input']),
                         ('mcp', {'arguments': 'not json'}))

    def test_command_execution_item_answers_the_pending_call(self):
        self.parse(_ri(type='custom_tool_call', id='ctc_1', call_id='call_1',
                       name='exec', input=_EXEC_JS))
        res = self.parse(_item({
            'type': 'CommandExecution', 'id': 'exec-1',
            'command': ['/bin/bash', '-lc', 'ls -la "x y"'], 'cwd': '/w',
            'parsed_cmd': [{'type': 'unknown', 'cmd': 'ls -la "x y"'}],
            'status': 'completed', 'stdout': 'a\nb\n', 'stderr': '',
            'aggregated_output': 'a\nb\n', 'exit_code': 0,
            'duration': {'secs': 1, 'nanos': 500000000}}))
        self.assertEqual((res.kind, res.tag), ('tool_result', 'command'))
        tool = res.tool
        self.assertEqual((tool['call_id'], tool['output'], tool['is_error']),
                         ('call_1', 'a\nb\n', False))
        self.assertEqual(tool['glance'], 'a\nb')
        self.assertEqual(tool['exit_code'], 0)
        self.assertEqual(self.ctx.pending, [])
        self.assertEqual(self.r._summarise_message(res), '↳ tool_result: a\nb')
        out = self.parse(_ri(type='custom_tool_call_output', id='exec-1',
                             call_id='call_1', output=[
                                 {'type': 'input_text', 'text': 'Script completed'}]))
        self.assertEqual((out.kind, self.level(out)), ('meta', 6))
        self.assertIn('tool output', out.summary)

    def test_failed_command_is_an_error_with_exit_code_glance(self):
        self.parse(_ri(type='custom_tool_call', call_id='call_1', name='exec',
                       input=_EXEC_JS))
        res = self.parse(_item({
            'type': 'CommandExecution', 'id': 'exec-2', 'command': ['x'],
            'status': 'failed', 'stdout': '', 'stderr': 'boom', 'exit_code': 2}))
        self.assertTrue(res.tool['is_error'])
        self.assertEqual(res.tool['output'], 'boom')
        self.assertEqual(res.tool['glance'], 'exit 2 · boom')

    def test_file_change_item_carries_a_unified_diff_with_headers(self):
        self.parse(_ri(type='custom_tool_call', call_id='call_2', name='exec',
                       input=_PATCH_JS))
        res = self.parse(_item({
            'type': 'FileChange', 'id': 'fc-1', 'status': 'completed',
            'stdout': 'Success.', 'stderr': '',
            'changes': {
                '/p/src/a.py': {'type': 'update', 'unified_diff': '@@ -1 +1 @@\n-old\n+new\n',
                                'move_path': None},
                '/p/docs/b.md': {'type': 'add', 'unified_diff': '@@ -0,0 +1 @@\n+hello\n',
                                 'move_path': None},
                '/p/gone.txt': {'type': 'delete', 'unified_diff': '@@ -1 +0,0 @@\n-x\n',
                                'move_path': None},
            }}))
        self.assertEqual((res.kind, res.tag), ('tool_result', 'file_change'))
        self.assertEqual(self.level(res), 3)   # alongside its apply_patch call
        tool = res.tool
        self.assertEqual((tool['canon'], tool['name'], tool['call_id']),
                         ('edit', 'apply_patch', 'call_2'))
        self.assertEqual(tool['diff'].split('\n')[:5], [
            '--- /p/src/a.py', '+++ /p/src/a.py', '@@ -1 +1 @@', '-old', '+new'])
        self.assertIn('--- /dev/null\n+++ /p/docs/b.md\n', tool['diff'])
        self.assertIn('--- /p/gone.txt\n+++ /dev/null\n', tool['diff'])
        self.assertEqual(tool['glance'], '3 file(s): /p/src/a.py, /p/docs/b.md, /p/gone.txt')
        self.assertFalse(tool['is_error'])
        self.assertEqual(tool['output'], 'Success.')

    def test_interrupted_call_does_not_pair_across_turns(self):
        self.parse(_ri(type='custom_tool_call', call_id='call_old', name='exec',
                       input=_EXEC_JS))
        self.parse(_ev(type='turn_aborted', turn_id='turn-1', reason='interrupted'))
        self.assertEqual(self.ctx.pending, [])
        self.parse(_ev(type='task_started', turn_id='turn-2'))
        self.parse(_ri(type='custom_tool_call', call_id='call_new', name='exec',
                       input=_EXEC_JS))
        res = self.parse(_item({'type': 'CommandExecution', 'id': 'exec-2',
                                'command': ['x'], 'status': 'completed',
                                'stdout': 'ok', 'exit_code': 0}, turn_id='turn-2'))
        self.assertEqual((res.kind, res.tool['call_id']), ('tool_result', 'call_new'))
        self.assertEqual(self.ctx.pending, [])
        self.assertEqual(self.ctx.answered, {'call_new'})

    def test_items_answer_only_calls_of_their_canon(self):
        self.parse(_ri(type='custom_tool_call', call_id='call_patch', name='exec',
                       input=_PATCH_JS))
        self.parse(_ri(type='custom_tool_call', call_id='call_cmd', name='exec',
                       input=_EXEC_JS))
        cmd = self.parse(_item({'type': 'CommandExecution', 'id': 'e', 'command': ['x'],
                                'status': 'completed', 'stdout': '', 'exit_code': 0}))
        self.assertEqual(cmd.tool['call_id'], 'call_cmd')
        patch = self.parse(_item({'type': 'FileChange', 'id': 'f', 'status': 'completed',
                                  'changes': {'/p/x': {'type': 'update',
                                                       'unified_diff': '@@ -1 +1 @@\n'}}}))
        self.assertEqual(patch.tool['call_id'], 'call_patch')
        self.assertEqual(self.ctx.pending, [])

    def test_raw_output_without_item_is_the_result(self):
        self.parse(_ri(type='custom_tool_call', call_id='call_7', name='exec',
                       input=_EXEC_JS))
        out = self.parse(_ri(type='custom_tool_call_output', call_id='call_7',
                             output=[{'type': 'input_text', 'text': 'done'}]))
        self.assertEqual((out.kind, out.tool['call_id'], out.tool['output']),
                         ('tool_result', 'call_7', 'done'))
        self.assertEqual(self.ctx.pending, [])
        fo = self.parse(_ri(type='function_call_output', call_id='fc_1',
                            output='plain string'))
        self.assertEqual((fo.kind, fo.tool['output']), ('tool_result', 'plain string'))

    def test_item_without_pending_call_becomes_a_call(self):
        rec = self.parse(_item({
            'type': 'CommandExecution', 'id': 'exec-9',
            'command': ['/bin/bash', '-lc', 'make test'], 'cwd': '/w',
            'status': 'completed', 'stdout': 'ok', 'exit_code': 0}))
        self.assertEqual(rec.kind, 'tool_call')
        self.assertEqual(rec.tool['input'], {'command': 'make test', 'cwd': '/w'})
        self.assertEqual(rec.tool['call_id'], 'exec-9')
        fc = self.parse(_item({'type': 'FileChange', 'id': 'fc-9', 'status': 'completed',
                               'changes': {'/p/x': {'type': 'update', 'unified_diff': ''}}}))
        self.assertEqual((fc.kind, fc.tool['canon'], fc.tool['input']['file_path']),
                         ('tool_call', 'edit', '/p/x'))

    # ---- a whole turn through the fold ----------------------------------

    def test_turn_folds_calls_results_and_dedupes(self):
        r = self.r
        lines = [
            _line('session_meta', {'id': 't', 'cwd': '/w', 'cli_version': '0.161.0'}),
            _ev(type='task_started', turn_id='turn-1'),
            _ri(type='message', role='developer', content=[{'type': 'input_text', 'text': 'rules'}]),
            _line('turn_context', {'turn_id': 'turn-1', 'model': 'gpt-5.6'}),
            _ri(type='message', id='rs_u', role='user', content=[{'type': 'input_text', 'text': 'go'}]),
            _item({'type': 'UserMessage', 'id': 'u1', 'content': [{'type': 'text', 'text': 'go'}]}),
            _item({'type': 'Reasoning', 'id': 'rs_1', 'summary_text': []}),
            _ri(type='reasoning', id='rs_1', summary=[], encrypted_content='x'),
            _ri(type='custom_tool_call', id='ctc_1', call_id='call_1', name='exec', input=_EXEC_JS),
            _line('token_usage_record', {'usage': {'total_tokens': 1}}),
            _item({'type': 'CommandExecution', 'id': 'exec-1', 'command': ['x'],
                   'status': 'completed', 'stdout': 'listing', 'exit_code': 0}),
            _ri(type='custom_tool_call_output', call_id='call_1',
                output=[{'type': 'input_text', 'text': 'Script completed'}]),
            _ev(type='token_count', info={'total_token_usage': {'total_tokens': 900}}),
            _item({'type': 'AgentMessage', 'id': 'msg_1', 'phase': 'final_answer',
                   'content': [{'type': 'text', 'text': 'Done'}]}),
            _ri(type='message', id='msg_1', role='assistant', phase='final_answer',
                content=[{'type': 'output_text', 'text': 'Done'}]),
            _ev(type='task_complete', turn_id='turn-1', duration_ms=2000),
        ]
        recs = [self.parse(raw) for raw in lines]
        kinds = [rec.kind for rec in recs]
        self.assertEqual(kinds, [
            'meta', 'turn_start', 'system', 'meta', 'meta', 'user_prompt',
            'thinking', 'meta', 'tool_call', 'meta', 'tool_result', 'meta',
            'meta', 'assistant_text', 'meta', 'turn_end'])
        td = r._TreeData()
        cur = r._ScanCursor()
        r._fold_records(td, cur, list(enumerate(recs)), '/x.jsonl')
        roots = [(e['kind'], e.get('line', e.get('start'))) for e in td.roots_in_order]
        self.assertEqual(roots, [('span', 0), ('turn', 5)])
        self.assertEqual([rec.key for rec in td.tool_children[8]], [10, 11])
        self.assertEqual(td.records[15].meta['tokens'], 900)
        self.assertEqual(td.records[13].meta['model'], 'gpt-5.6')
        self.assertEqual(td.latest_voice_line, 13)
        r._TREE_CACHE['/x.jsonl'] = td
        try:
            umbrella = r._render_tool_umbrella(('tool', '/x.jsonl', 8))
        finally:
            r._TREE_CACHE.pop('/x.jsonl', None)
        self.assertIn('ls -la', umbrella)
        self.assertIn('listing', umbrella)
        self.assertIn('exit 0', umbrella)


if __name__ == '__main__':
    unittest.main()
