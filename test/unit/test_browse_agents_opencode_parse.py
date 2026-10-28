"""Unit tests for the OpenCode row → ``Record`` expansion in
``recipes/browse-agents-opencode`` (``_records_v2`` / ``_records_v1`` /
``_expand``), on synthetic rows modelled on the 2.x and 1.x shapes.
"""

import importlib.util
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-agents-opencode'


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
    name = '_browse_agents_opencode_parse'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


T0 = 1_790_000_000_000


def _row(mid, typ, data, seq=0):
    return {'id': mid, 'type': typ, 'seq': seq, 'created': T0, 'updated': T0,
            'data': data, 'raw': data}


def _asst(content, finish='stop', **extra):
    d = {'time': {'created': T0 + 1000}, 'agent': 'build',
         'model': {'id': 'glm', 'providerID': 'fw', 'variant': 'default'},
         'content': content}
    if finish:
        d['finish'] = finish
    d.update(extra)
    return _row('m2', 'assistant', d, 1)


def _tool(name, status='completed', inp=None, content='out', metadata=None, cid='call_1'):
    state = {'status': status, 'input': inp or {}}
    if status in ('completed', 'error'):
        state['content'] = [{'type': 'text', 'text': content}]
        state['metadata'] = metadata or {}
    return {'type': 'tool', 'id': cid, 'name': name, 'state': state,
            'time': {'created': T0 + 2000}}


def _v1(part, role='assistant', finish='stop', last_text=False, pid='p1'):
    msg = {'role': role, 'time': {'created': T0}, 'agent': 'build',
           'providerID': 'fw', 'modelID': 'glm', 'finish': finish,
           'tokens': {'total': 30, 'input': 20, 'output': 10}, 'cost': 0.5}
    return {'id': pid, 'mid': 'm1', 'created': T0, 'updated': T0, 'data': part,
            'message': msg, 'last_text': last_text, 'raw': part}


class OpenCodeParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def level(self, rec):
        return self.r._record_min_level(rec)

    # ---- v2 ---------------------------------------------------------------

    def test_user_row_is_a_turn_root(self):
        recs = self.r._expand('v2', _row('m1', 'user', {
            'time': {'created': T0}, 'text': 'hello there', 'files': [], 'agents': []}))
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual((rec.kind, rec.tag, rec.text, rec.id), ('user_prompt', 'user', 'hello there', 'm1'))
        self.assertTrue(rec.is_voice)
        self.assertTrue(self.r._is_turn_root(rec))
        self.assertAlmostEqual(rec.ts, T0 / 1000)

    def test_assistant_expands_per_element_with_final_text_as_end_turn(self):
        recs = self.r._expand('v2', _asst([
            {'type': 'reasoning', 'text': 'thinking hard', 'time': {'created': T0 + 1100}},
            {'type': 'text', 'text': 'first words'},
            _tool('shell', inp={'command': 'ls'}, content='a b', metadata={'exit': 0}),
            {'type': 'text', 'text': 'Done.'},
        ], cost=0.25, tokens={'input': 10, 'output': 5, 'reasoning': 1, 'cache': {'read': 0, 'write': 0}}))
        kinds = [r.kind for r in recs]
        self.assertEqual(kinds, ['thinking', 'assistant_text', 'tool_call', 'tool_result',
                                 'assistant_text'])
        think, first, call, result, last = recs
        self.assertEqual((think.tag, think.text, think.is_voice), ('reasoning', 'thinking hard', True))
        self.assertEqual(self.level(think), 2)
        self.assertFalse(first.end_turn)
        self.assertEqual(self.level(first), 2)
        self.assertTrue(last.end_turn)
        self.assertEqual(self.level(last), 1)
        self.assertEqual(last.meta, {'model': 'fw/glm', 'agent': 'build', 'tokens': 16, 'cost': '$0.2500'})
        self.assertEqual(first.meta, {'model': 'fw/glm', 'agent': 'build'})
        self.assertEqual((call.tool['canon'], call.tool['name'], call.tool['call_id']),
                         ('bash', 'shell', 'call_1'))
        self.assertEqual(self.r._summarise_message(call), '🔧 shell(ls)')
        self.assertEqual((result.tag, result.tool['output'], result.tool['glance']),
                         ('shell', 'a b', None))
        self.assertEqual(self.r._summarise_message(result), '↳ tool_result: a b')
        self.assertEqual([r.id for r in recs], ['m2#0', 'm2#1', 'm2#2', 'm2#3', 'm2#4'])
        self.assertTrue(all(r.ts >= recs[0].ts for r in recs))

    def test_tool_calls_finish_does_not_end_the_turn(self):
        recs = self.r._expand('v2', _asst([{'type': 'text', 'text': 'Let me look.'},
                                           _tool('read', inp={'path': '/p/a'})],
                                          finish='tool-calls'))
        self.assertFalse(recs[0].end_turn)
        self.assertEqual(self.level(recs[0]), 2)
        self.assertEqual(recs[1].tool['input']['file_path'], '/p/a')
        self.assertEqual(self.r._summarise_message(recs[1]), '🔧 read(/p/a)')

    def test_pending_tool_has_a_placeholder_result(self):
        recs = self.r._expand('v2', _asst([_tool('glob', status='running', inp={'pattern': '*.py'})],
                                          finish=None))
        self.assertEqual([r.kind for r in recs], ['tool_call', 'tool_result'])
        res = recs[1].tool
        self.assertEqual((res['output'], res['glance'], res['diff'], res['is_error'], res['status']),
                         (None, '(running)', None, False, 'running'))
        self.assertEqual(self.r._summarise_message(recs[1]), '↳ tool_result: (running)')

    def test_tool_errors_and_metadata_glances(self):
        r = self.r
        err = r._expand('v2', _asst([_tool('shell', status='error', inp={'command': 'x'},
                                           content='boom', metadata={'exit': 2})]))[1].tool
        self.assertTrue(err['is_error'])
        self.assertEqual(err['glance'], 'exit 2 · boom')
        grep = r._expand('v2', _asst([_tool('grep', inp={'pattern': 'a'}, metadata={'matches': 7})]))[1]
        self.assertEqual(grep.tool['glance'], '7 matches')
        glob = r._expand('v2', _asst([_tool('glob', inp={'pattern': '*'}, metadata={'count': 3})]))[1]
        self.assertEqual(glob.tool['glance'], '3 files')

    def test_edit_result_carries_the_patch_as_diff_at_level_three(self):
        recs = self.r._expand('v2', _asst([_tool(
            'edit', inp={'path': '/p/a.py', 'oldString': 'x', 'newString': 'y', 'replaceAll': True},
            content='Edit applied',
            metadata={'files': [{'file': 'a.py', 'patch': '@@ -1 +1 @@\n-x\n+y\n',
                                 'additions': 1, 'deletions': 1, 'status': 'modified'}]})]))
        call, result = recs
        self.assertEqual(call.tool['canon'], 'edit')
        self.assertEqual(self.level(call), 3)
        self.assertEqual(self.level(result), 3)
        self.assertEqual(call.tool['input']['old_string'], 'x')
        self.assertEqual(call.tool['input']['new_string'], 'y')
        self.assertTrue(call.tool['input']['replace_all'])
        self.assertEqual(result.tool['diff'], '@@ -1 +1 @@\n-x\n+y\n\n')
        self.assertEqual(result.tool['glance'], '1 file(s): a.py')
        self.assertEqual(self.r._summarise_message(call), '🔧 edit(/p/a.py)')

    def test_input_normalisation(self):
        norm = self.r._norm_input
        self.assertEqual(norm({'command': ['ls', '-la']})['command'], 'ls -la')
        self.assertEqual(norm({'filePath': '/p'})['file_path'], '/p')
        self.assertEqual(norm({'name': 'review'})['skill'], 'review')
        self.assertEqual(norm(None), {})
        self.assertEqual(norm('raw'), {'arguments': 'raw'})
        self.assertEqual(self.r._tool_canon('task'), 'spawn')
        self.assertEqual(self.r._tool_canon('list'), 'glob')
        self.assertEqual(self.r._tool_canon('patch'), 'edit')
        self.assertEqual(self.r._tool_canon('todowrite'), 'other')

    def test_task_call_carries_the_description_as_peer(self):
        recs = self.r._expand('v2', _asst([_tool('task', status='running', inp={
            'description': 'explore the repo', 'prompt': 'look', 'subagent_type': 'explore'})]))
        self.assertEqual((recs[0].tool['canon'], recs[0].tool['peer']), ('spawn', 'explore the repo'))
        self.assertEqual(self.r._summarise_message(recs[0]), '🔧 task(explore)')

    def test_empty_assistant_rows(self):
        r = self.r
        meta = r._expand('v2', _asst([], finish='stop'))
        self.assertEqual((meta[0].kind, self.level(meta[0]), meta[0].summary),
                         ('meta', 6, 'assistant · stop'))
        err = r._expand('v2', _asst([], finish=None, error={
            'name': 'APIError', 'data': {'message': 'rate limited'}}))
        self.assertEqual((err[0].kind, err[0].tag, err[0].text, self.level(err[0])),
                         ('system', 'error', 'rate limited', 4))
        self.assertEqual(err[0].summary, 'error: rate limited')

    def test_other_v2_message_types(self):
        r = self.r
        idle = r._expand('v2', _row('m3', 'idle', {'time': {'created': T0}, 'outcome': 'completed'}))[0]
        self.assertEqual((idle.kind, idle.summary, self.level(idle)), ('turn_end', 'idle · completed', 4))
        self.assertTrue(r._is_turn_close(idle))
        system = r._expand('v2', _row('m4', 'system', {'time': {'created': T0}, 'text': 'note\nmore'}))[0]
        self.assertEqual((system.kind, system.tag, system.text, self.level(system)),
                         ('system', 'system', 'note\nmore', 5))
        self.assertEqual(system.summary, 'system: note')
        self.assertFalse(system.is_meta)
        synth = r._expand('v2', _row('m5', 'synthetic', {'text': 'ctx'}))[0]
        self.assertTrue(synth.is_meta)
        self.assertEqual(synth.meta['span_label'], 'synthetic')
        comp = r._expand('v2', _row('m6', 'compaction', {'text': 'summary of it'}))[0]
        self.assertEqual((comp.kind, comp.text), ('compaction', 'summary of it'))
        sel = r._expand('v2', _row('m7', 'model_selected', {'model': {'id': 'x', 'providerID': 'p'}}))[0]
        self.assertEqual((sel.kind, sel.summary, self.level(sel)), ('meta', 'model_selected: p/x', 5))
        ag = r._expand('v2', _row('m8', 'agent_selected', {'agent': 'plan'}))[0]
        self.assertEqual(ag.summary, 'agent_selected: plan')
        unk = r._expand('v2', _row('m9', 'brand_new', {'x': 1}))[0]
        self.assertEqual((unk.kind, unk.tag, self.level(unk)), ('unknown', 'brand_new', 7))
        el = r._expand('v2', _asst([{'type': 'image', 'url': 'x'}]))[0]
        self.assertEqual((el.kind, el.tag), ('unknown', 'content:image'))

    # ---- v1 ---------------------------------------------------------------

    def test_v1_parts(self):
        r = self.r
        user = r._expand('v1', _v1({'type': 'text', 'text': 'hi'}, role='user'))[0]
        self.assertEqual((user.kind, user.text, user.meta), ('user_prompt', 'hi', {}))
        self.assertTrue(r._is_turn_root(user))
        start = r._expand('v1', _v1({'type': 'step-start', 'snapshot': 'abc'}))[0]
        self.assertEqual((start.kind, start.summary, self.level(start)), ('turn_start', 'step start', 5))
        think = r._expand('v1', _v1({'type': 'reasoning', 'text': 'hmm',
                                     'time': {'start': T0 + 100, 'end': T0 + 200}}))[0]
        self.assertEqual((think.kind, think.text, think.meta), ('thinking', 'hmm', {'model': 'fw/glm', 'agent': 'build'}))
        self.assertAlmostEqual(think.ts, (T0 + 100) / 1000)
        call, result = r._expand('v1', _v1({'type': 'tool', 'tool': 'bash', 'callID': 'c9', 'state': {
            'status': 'completed', 'input': {'command': 'pwd'}, 'output': '/p',
            'metadata': {'exit': 1}, 'time': {'start': T0, 'end': T0 + 50}}}))
        self.assertEqual((call.tool['canon'], call.tool['call_id']), ('bash', 'c9'))
        self.assertEqual((result.tool['output'], result.tool['glance']), ('/p', 'exit 1 · /p'))
        self.assertEqual([call.id, result.id], ['p1#0', 'p1#1'])
        mid_text = r._expand('v1', _v1({'type': 'text', 'text': 'partial'}))[0]
        self.assertFalse(mid_text.end_turn)
        last = r._expand('v1', _v1({'type': 'text', 'text': 'final'}, last_text=True))[0]
        self.assertTrue(last.end_turn)
        self.assertEqual(self.level(last), 1)
        not_stop = r._expand('v1', _v1({'type': 'text', 'text': 'x'}, finish='tool-calls', last_text=True))[0]
        self.assertFalse(not_stop.end_turn)
        fin = r._expand('v1', _v1({'type': 'step-finish', 'reason': 'stop', 'cost': 0.5,
                                   'tokens': {'total': 30, 'input': 20, 'output': 10}}))[0]
        self.assertEqual((fin.kind, fin.summary, self.level(fin)), ('turn_end', 'step finish · stop', 4))
        self.assertEqual((fin.meta['tokens'], fin.meta['cost']), (30, '$0.5000'))
        retry = r._expand('v1', _v1({'type': 'retry', 'error': {'message': 'again'}}))[0]
        self.assertEqual((retry.kind, self.level(retry)), ('meta', 5))
        snap = r._expand('v1', _v1({'type': 'snapshot', 'snapshot': 'x'}))[0]
        self.assertEqual((snap.kind, snap.tag, self.level(snap)), ('meta', 'snapshot', 6))
        unk = r._expand('v1', _v1({'type': 'weird'}))[0]
        self.assertEqual((unk.kind, unk.tag), ('unknown', 'weird'))
        self.assertEqual(unk.raw, {'message_id': 'm1', 'part': {'type': 'weird'}})


if __name__ == '__main__':
    unittest.main()
