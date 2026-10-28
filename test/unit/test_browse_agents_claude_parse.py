"""Unit tests for the Claude raw-record → ``Record`` parser in
``recipes/browse-agents-claude`` (``_parse_record``).

Loads the generated recipe with a stubbed ``browse_tui`` exactly like
``test_browse_agents_claude_render`` (full stub, fresh module every load).
"""

import importlib.util
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-agents-claude'


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
    name = '_browse_agents_claude_parse'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _asst(parts, stop=None, **extra):
    msg = {'role': 'assistant', 'content': parts}
    if stop:
        msg['stop_reason'] = stop
    return {'type': 'assistant', 'uuid': 'a1', 'message': msg, **extra}


def _user(content, **extra):
    return {'type': 'user', 'uuid': 'u1',
            'message': {'role': 'user', 'content': content}, **extra}


class TestParseRecord(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def parse(self, raw, key=0):
        rec = self.r._parse_record(raw, key)
        self.assertIn(rec.kind, self.r._KINDS)
        for t in rec.tools:
            self.assertIn(t['canon'], self.r._TOOL_CANONS)
        return rec

    def test_user_prompt(self):
        rec = self.parse(_user('hello', timestamp='2026-01-02T03:04:05Z'))
        self.assertEqual(rec.kind, 'user_prompt')
        self.assertEqual((rec.key, rec.id, rec.tag), (0, 'u1', 'user'))
        self.assertEqual(rec.text, 'hello')
        self.assertTrue(rec.is_voice)
        self.assertEqual(rec.row_bg, 17)
        self.assertEqual(rec.min_level, 1)
        self.assertEqual(rec.ts, 1767323045.0)
        self.assertTrue(self.r._is_turn_root(rec))

    def test_meta_user_text_is_not_a_turn_root(self):
        rec = self.parse(_user('injected', isMeta=True))
        self.assertEqual(rec.kind, 'user_prompt')
        self.assertTrue(rec.is_meta)
        self.assertEqual(rec.min_level, 6)
        self.assertFalse(self.r._is_turn_root(rec))
        self.assertEqual(self.r._summarise_message(rec), 'user/meta: injected')

    def test_assistant_text_end_turn(self):
        rec = self.parse(_asst([{'type': 'text', 'text': 'Done.'}],
                               stop='end_turn', attributionAgent='Explore'))
        self.assertEqual(rec.kind, 'assistant_text')
        self.assertTrue(rec.end_turn)
        self.assertEqual(rec.meta['agent'], 'Explore')
        self.assertEqual(rec.min_level, 1)
        self.assertEqual(self.r._row_tag(rec), 'assistant · Explore · end_turn')

    def test_tool_call_canon_and_input(self):
        rec = self.parse(_asst([
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
             'input': {'command': 'ls'}},
            {'type': 'tool_use', 'id': 't2', 'name': 'mcp__gh__issues',
             'input': {}},
        ]))
        self.assertEqual(rec.kind, 'tool_call')
        self.assertFalse(rec.is_voice)
        self.assertEqual([(t['canon'], t['name'], t['call_id']) for t in rec.tools],
                         [('bash', 'Bash', 't1'), ('mcp', 'mcp__gh__issues', 't2')])
        self.assertEqual(rec.tool['input'], {'command': 'ls'})
        self.assertEqual(self.r._summarise_message(rec), '🔧 Bash(ls)')
        self.assertTrue(self.r._issues_tool_call(rec))

    def test_canon_table(self):
        for name, canon in (('Read', 'read'), ('Edit', 'edit'),
                            ('MultiEdit', 'edit'), ('NotebookEdit', 'edit'),
                            ('Write', 'write'), ('Grep', 'grep'),
                            ('Glob', 'glob'), ('Agent', 'spawn'),
                            ('Task', 'spawn'), ('SendMessage', 'message'),
                            ('AskUserQuestion', 'question'),
                            ('WebFetch', 'webfetch'), ('WebSearch', 'websearch'),
                            ('Skill', 'skill'), ('TodoWrite', 'other')):
            self.assertEqual(self.r._tool_canon(name), canon, name)

    def test_edit_tool_is_level_3(self):
        rec = self.parse(_asst([{'type': 'tool_use', 'id': 't1', 'name': 'Edit',
                                 'input': {'file_path': 'a.py'}}]))
        self.assertEqual(rec.tool['canon'], 'edit')
        self.assertEqual(self.r._record_min_level(rec), 3)

    def test_text_with_tool_call_is_assistant_text_carrying_tools(self):
        rec = self.parse(_asst([
            {'type': 'text', 'text': 'Looking'},
            {'type': 'tool_use', 'id': 't1', 'name': 'Read',
             'input': {'file_path': 'x'}},
        ]))
        self.assertEqual(rec.kind, 'assistant_text')
        self.assertTrue(rec.is_voice)
        self.assertEqual((rec.tag_style, rec.row_bg), ('green', 235))
        self.assertEqual(rec.tool['canon'], 'read')
        self.assertTrue(self.r._issues_tool_call(rec))

    def test_thinking(self):
        rec = self.parse(_asst([{'type': 'thinking', 'thinking': 'hmm',
                                 'signature': 's'}]))
        self.assertEqual((rec.kind, rec.text), ('thinking', 'hmm'))
        self.assertTrue(rec.is_voice)
        self.assertEqual(rec.min_level, 2)
        self.assertEqual(self.r._row_tag(rec), 'assistant · thinking')
        empty = self.parse(_asst([{'type': 'thinking', 'thinking': '',
                                   'signature': 's'}]))
        self.assertEqual((empty.kind, empty.text), ('thinking', ''))
        self.assertTrue(empty.meta.get('encrypted'))
        self.assertEqual(empty.min_level, 6)

    def test_tool_result(self):
        rec = self.parse(_user(
            [{'type': 'tool_result', 'tool_use_id': 't1',
              'content': [{'type': 'text', 'text': 'out'}], 'is_error': True}],
            sourceToolAssistantUUID='a1',
            toolUseResult={'stdout': 'out', 'stderr': '', 'interrupted': False},
        ))
        self.assertEqual(rec.kind, 'tool_result')
        self.assertEqual(rec.caller_id, 'a1')
        tool = rec.tool
        self.assertEqual((tool['call_id'], tool['output'], tool['is_error']),
                         ('t1', 'out', True))
        self.assertEqual(rec.min_level, 4)
        self.assertEqual(tool['glance'], 'out')
        self.assertEqual(self.r._summarise_message(rec), '↳ tool_result: out')

    def test_tool_result_glance_and_diff(self):
        tur = {'filePath': 'a.py', 'structuredPatch': [
            {'oldStart': 1, 'oldLines': 1, 'newStart': 1, 'newLines': 1,
             'lines': ['-a', '+b']}]}
        rec = self.parse(_user(
            [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}],
            toolUseResult=tur))
        self.assertIn('-a', rec.tool['diff'])
        self.assertIn('+b', rec.tool['diff'])
        files = self.parse(_user(
            [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x'}],
            toolUseResult={'filenames': ['a'], 'numFiles': 3}))
        self.assertEqual(self.r._summarise_message(files), '↳ tool_result: 3 files')

    def test_spawn_and_message_tools_carry_peer(self):
        rec = self.parse(_asst([
            {'type': 'tool_use', 'id': 't1', 'name': 'Agent',
             'input': {'name': 'w1', 'subagent_type': 'general-purpose'}},
            {'type': 'tool_use', 'id': 't2', 'name': 'SendMessage',
             'input': {'to': 'w2', 'summary': 'go', 'message': 'body'}},
        ]))
        spawn, send = rec.tools
        self.assertEqual((spawn['canon'], spawn['peer']), ('spawn', 'w1'))
        self.assertEqual((send['canon'], send['peer'], send['text']),
                         ('message', 'w2', 'body'))
        self.assertEqual(rec.tag, 'agent-send')
        self.assertTrue(rec.is_voice)
        self.assertEqual(rec.row_bg, 235)

    def test_inbound_peer_message(self):
        rec = self.parse(_user(
            '<teammate-message teammate_id="w1">\nfirst result\n</teammate-message>'))
        self.assertEqual((rec.kind, rec.agent_voice, rec.peer),
                         ('user_prompt', 'peer', 'w1'))
        self.assertEqual(rec.text, 'first result')
        self.assertEqual(rec.tag, 'agent-peer')
        self.assertEqual(rec.min_level, 2)
        self.assertFalse(self.r._is_turn_root(rec))
        self.assertEqual(self.r._summarise_message(rec), '← w1: first result')

    def test_idle_notice(self):
        body = '{"type": "idle_notification", "idleReason": "done", "summary": "s"}'
        rec = self.parse(_user(
            f'<agent-message from="w1">\n{body}\n</agent-message>'))
        self.assertEqual(rec.agent_voice, 'idle')
        self.assertTrue(self.r._is_inbound_agent_voice(rec))

    def test_task_notification(self):
        rec = self.parse(_user(
            '<task-notification>\n<task-id>7</task-id>\n<status>completed</status>\n'
            '<summary>did it</summary>\n<result>\n# Report\n</result>\n'
            '</task-notification>'))
        self.assertEqual(rec.agent_voice, 'reply')
        self.assertEqual(rec.text, '# Report')
        self.assertEqual(rec.tag, 'agent-reply')
        self.assertEqual(self.r._summarise_message(rec), '← 7 · completed · did it')

    def test_queue_operation_is_voice_meta(self):
        rec = self.parse({'type': 'queue-operation', 'operation': 'enqueue',
                          'content': 'next please'})
        self.assertEqual(rec.kind, 'meta')
        self.assertTrue(rec.is_voice)
        self.assertEqual(rec.row_bg, 17)
        self.assertEqual(rec.min_level, 1)
        self.assertFalse(self.r._is_turn_root(rec))
        self.assertEqual(self.r._summarise_message(rec), 'queue enqueue: next please')

    def test_system_kinds(self):
        close = self.parse({'type': 'system', 'subtype': 'turn_duration',
                            'durationMs': 1500, 'messageCount': 2})
        self.assertEqual(close.kind, 'turn_end')
        self.assertEqual(close.meta['duration_ms'], 1500)
        self.assertTrue(self.r._is_turn_close(close))
        self.assertEqual(self.r._summarise_message(close), '⏱ turn 1.50s · 2 msgs')
        compact = self.parse({'type': 'system', 'subtype': 'compact_boundary',
                              'content': 'Compacted'})
        self.assertEqual((compact.kind, compact.text), ('compaction', 'Compacted'))
        self.assertEqual(compact.min_level, 2)
        other = self.parse({'type': 'system', 'subtype': 'api_error'})
        self.assertEqual((other.kind, other.tag, other.min_level), ('system', 'system', 4))
        self.assertEqual(other.meta['span_label'], 'sys:api_error')

    def test_attachment_is_system_with_call_id(self):
        rec = self.parse({'type': 'attachment', 'attachment': {
            'type': 'hook_success', 'hookName': 'lint', 'hookEvent': 'PreToolUse',
            'exitCode': 0, 'toolUseID': 't1'}})
        self.assertEqual((rec.kind, rec.tag, rec.call_id), ('system', 'attachment', 't1'))
        self.assertEqual(rec.meta['span_label'], 'att:hook_success')
        self.assertEqual(rec.min_level, 5)
        self.assertEqual(self.r._summarise_message(rec), '✓ hook lint (PreToolUse)')

    def test_metadata_titles(self):
        ai = self.parse({'type': 'ai-title', 'aiTitle': 'Fix tests'})
        self.assertEqual((ai.kind, ai.tag), ('meta', 'ai-title'))
        self.assertEqual((ai.meta['title'], ai.meta['title_by']), ('Fix tests', 'agent'))
        custom = self.parse({'type': 'custom-title', 'customTitle': 'Mine'})
        self.assertEqual((custom.meta['title'], custom.meta['title_by']), ('Mine', 'user'))
        self.assertEqual(self.r._summarise_message(custom), 'custom-title: Mine')
        self.assertEqual(ai.min_level, 6)

    def test_unknown(self):
        rec = self.parse({'type': 'banana'})
        self.assertEqual((rec.kind, rec.tag, rec.min_level), ('unknown', 'banana', 7))
        self.assertEqual(self.r._summarise_message(rec), 'banana')
        self.assertIs(rec.raw['type'], 'banana')

    def test_default_presentation_when_parser_leaves_it_unset(self):
        Record = self.r.Record
        voice = Record('user_prompt', {}, is_voice=True)
        self.assertEqual(self.r._record_tag_style(voice), 'cyan')
        self.assertEqual(self.r._record_row_bg(voice), 17)
        call = Record('tool_call', {})
        self.assertEqual(self.r._record_tag_style(call), 'green')
        self.assertIsNone(self.r._record_row_bg(call))
        plain = Record('meta', {}, tag_style='')
        self.assertEqual(self.r._record_tag_style(plain), '')

    def test_default_levels_when_parser_leaves_min_level_unset(self):
        Record = self.r.Record
        self.assertEqual(self.r._record_min_level(Record('user_prompt', {})), 1)
        self.assertEqual(self.r._record_min_level(Record('meta', {})), 6)
        edit = Record('tool_call', {}, tools=[self.r.new_tool(canon='write')])
        self.assertEqual(self.r._record_min_level(edit), 3)


if __name__ == '__main__':
    unittest.main()
