"""Unit tests for the Codex store adapter in ``recipes/browse-agents-codex``
(discovery, SQLite index with its scan fallback, children, tail).

``make_fake_codex`` builds a synthetic ``$CODEX_HOME``: three rollouts in
two working directories (one a spawned child of another) plus a
``state_5.sqlite`` with the ``threads`` / ``thread_spawn_edges`` columns
the store reads. The UI smoke test reuses it.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-agents-codex'

T1 = '01a11900-0000-7000-8000-000000000001'   # projA, 0.161 shape, parent
T2 = '019ddc84-0000-7000-8000-000000000002'   # projB, 0.104 shape
T3 = '01a11903-0000-7000-8000-000000000003'   # projA, child of T1
CWD_A = '/home/test/projA'
CWD_B = '/home/test/projB'
NAME_TWO = 'PROBE_NAME_TWO'       # threads.name set on T2 only

_EXEC_JS = ('const r = await tools.exec_command({"cmd":"make test",'
            '"workdir":"/home/test/projA","yield_time_ms":10000});\n'
            'text(r.output);\n')


def _ts(i):
    return f'2026-10-08T00:{i // 60:02d}:{i % 60:02d}.000Z'


def _lines(tid, cwd, version, shape, prompt, answer):
    """Synthetic rollout lines for one thread."""
    meta = {'id': tid, 'timestamp': _ts(0), 'cwd': cwd, 'originator': 'codex_cli_rs',
            'cli_version': version, 'source': 'cli', 'model_provider': 'openai',
            'git': {'branch': 'main', 'commit_hash': '0' * 40}}
    out = [{'timestamp': _ts(0), 'type': 'session_meta', 'payload': meta}]
    ev = lambda i, **p: {'timestamp': _ts(i), 'type': 'event_msg', 'payload': p}
    ri = lambda i, **p: {'timestamp': _ts(i), 'type': 'response_item', 'payload': p}
    item = lambda i, it: ev(i, type='item_completed', turn_id='turn-1', item=it)
    out.append(ri(1, type='message', role='developer',
                  content=[{'type': 'input_text', 'text': '# AGENTS.md\nbe brief'}]))
    out.append(ev(2, type='task_started', turn_id='turn-1'))
    out.append({'timestamp': _ts(3), 'type': 'turn_context', 'payload': {
        'turn_id': 'turn-1', 'cwd': cwd, 'model': 'gpt-5.6',
        'approval_policy': 'never', 'sandbox_policy': {'type': 'danger-full-access'}}})
    out.append(ri(4, type='message', id='rs_user', role='user', content=[
        {'type': 'input_text', 'text': '<environment_context>cwd</environment_context>'},
        {'type': 'input_text', 'text': prompt}]))
    if shape == '0.104':
        out.append(ev(5, type='user_message', message=prompt, images=[]))
        out.append(ev(6, type='task_complete', turn_id='turn-1', last_agent_message=None))
    else:
        out.append(item(5, {'type': 'UserMessage', 'id': f'{tid}-u1',
                            'content': [{'type': 'text', 'text': prompt}]}))
        out.append(item(6, {'type': 'Reasoning', 'id': 'rs_1', 'summary_text': [],
                            'raw_content': []}))
        out.append(ri(6, type='reasoning', id='rs_1', summary=[], encrypted_content='x'))
        out.append(ri(7, type='custom_tool_call', id='ctc_1', call_id='call_1',
                      name='exec', input=_EXEC_JS, status='completed'))
        out.append(item(8, {'type': 'CommandExecution', 'id': 'exec-1',
                            'command': ['/bin/bash', '-lc', 'make test'], 'cwd': cwd,
                            'parsed_cmd': [{'type': 'test', 'cmd': 'make test'}],
                            'status': 'completed', 'stdout': 'PROBE_STDOUT ok\n',
                            'stderr': '', 'aggregated_output': 'PROBE_STDOUT ok\n',
                            'exit_code': 0, 'duration': {'secs': 1, 'nanos': 0}}))
        out.append(ri(8, type='custom_tool_call_output', id='exec-1', call_id='call_1',
                      output=[{'type': 'input_text', 'text': 'Script completed'}]))
        out.append(ev(9, type='token_count', info={
            'total_token_usage': {'total_tokens': 1234},
            'last_token_usage': {'total_tokens': 100}, 'model_context_window': 400000},
            rate_limits={'primary': {'used_percent': 5.0}}))
        out.append(item(10, {'type': 'AgentMessage', 'id': 'msg_1', 'phase': 'final_answer',
                             'content': [{'type': 'text', 'text': answer}]}))
        out.append(ri(10, type='message', id='msg_1', role='assistant', phase='final_answer',
                      content=[{'type': 'output_text', 'text': answer}]))
        out.append(ev(11, type='task_complete', turn_id='turn-1',
                      last_agent_message=answer, duration_ms=9000))
    if shape == '0.161':
        for i, rec in enumerate(out):
            rec['ordinal'] = i
    return out


_THREADS = {
    # tid: (cwd, version, shape, prompt, answer, title, mtime offset)
    T1: (CWD_A, '0.161.0', '0.161', 'PROBE_PROMPT_ONE fix the build',
         'PROBE_ANSWER_ONE all green', 'PROBE_TITLE_ONE fix the build', 300),
    T2: (CWD_B, '0.104.0', '0.104', 'PROBE_PROMPT_TWO explain this',
         None, 'PROBE_TITLE_TWO explain this', 100),
    T3: (CWD_A, '0.161.0', '0.161', 'PROBE_CHILD_TASK run the suite',
         'PROBE_CHILD_ANSWER suite passed', 'PROBE_CHILD_TITLE run the suite', 200),
}


def make_fake_codex(tmpdir, with_db=True):
    """Build ``<tmpdir>/.codex`` and return ``(codex_home, {tid: rollout_path})``."""
    home = os.path.join(tmpdir, '.codex')
    day = os.path.join(home, 'sessions', '2026', '10', '08')
    os.makedirs(day)
    base = 1_790_000_000
    paths = {}
    for tid, (cwd, version, shape, prompt, answer, _title, off) in _THREADS.items():
        path = os.path.join(day, f'rollout-2026-10-08T00-00-00-{tid}.jsonl')
        with open(path, 'w') as f:
            for rec in _lines(tid, cwd, version, shape, prompt, answer):
                f.write(json.dumps(rec) + '\n')
        os.utime(path, (base + off, base + off))
        paths[tid] = path
    if with_db:
        con = sqlite3.connect(os.path.join(home, 'state_5.sqlite'))
        con.executescript('''
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                source TEXT NOT NULL, model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL, title TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                git_branch TEXT, cli_version TEXT NOT NULL DEFAULT '',
                first_user_message TEXT NOT NULL DEFAULT '', model TEXT, name TEXT);
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL PRIMARY KEY, status TEXT NOT NULL);
        ''')
        for tid, (cwd, version, _shape, prompt, _answer, title, off) in _THREADS.items():
            con.execute(
                'INSERT INTO threads (id, rollout_path, created_at, updated_at, source, '
                'model_provider, cwd, title, git_branch, cli_version, first_user_message, '
                'model, name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (tid, paths[tid], base, base + off, 'cli', 'openai', cwd, title,
                 'main', version, prompt, 'gpt-5.6', NAME_TWO if tid == T2 else None))
        con.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', (T1, T3, 'completed'))
        con.commit()
        con.close()
    return home, paths


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
    name = '_browse_agents_codex_store'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class CodexStoreTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home, self.paths = make_fake_codex(self.tmp.name)
        self._old_env = os.environ.get('CODEX_HOME')
        os.environ['CODEX_HOME'] = self.home
        self._old_argv = sys.argv
        self.r.store_reset()

    def tearDown(self):
        sys.argv = self._old_argv
        if self._old_env is None:
            os.environ.pop('CODEX_HOME', None)
        else:
            os.environ['CODEX_HOME'] = self._old_env
        self.r.store_reset()
        self.tmp.cleanup()

    # ---- projects and sessions --------------------------------------------

    def test_projects_group_by_cwd_newest_first(self):
        projects = self.r.store_list_projects()
        self.assertEqual([p.id for p in projects],
                         [('project', CWD_A), ('project', CWD_B)])
        self.assertEqual(projects[0].title, CWD_A)
        self.assertTrue(projects[0].tag.startswith('1 thread · '))   # child hidden
        self.assertTrue(all(p.has_children for p in projects))

    def test_sessions_hide_children_and_use_the_db_title(self):
        sessions = self.r.store_list_sessions(CWD_A)
        self.assertEqual([s.id for s in sessions], [('session', self.paths[T1])])
        s = sessions[0]
        self.assertEqual(s.title, 'PROBE_TITLE_ONE fix the build')
        self.assertEqual(s.scope_title, self.paths[T1])
        self.assertTrue(s.tag.startswith('gpt-5.6 · 15 rec · 1 sub · '), s.tag)
        self.assertTrue(s.boundary)
        self.assertEqual((s.kind, s.thread_id, s.line_count), ('session', T1, 15))
        self.assertEqual(self.r.store_list_sessions('/nowhere'), [])
        item = self.r.store_session_item(self.paths[T2])
        self.assertEqual(item.title, NAME_TWO)     # the thread name outranks the title
        info = self.r.store_session_info(self.paths[T2])
        self.assertEqual(info['meta'][1:3], [
            ('name', NAME_TWO), ('title', 'PROBE_TITLE_TWO explain this')])

    def test_child_sessions_and_parent_links(self):
        r = self.r
        kids = r.store_child_sessions(self.paths[T1])
        self.assertEqual([k.id for k in kids], [('agent', self.paths[T1], T3)])
        kid = kids[0]
        self.assertEqual(kid.title, 'PROBE_CHILD_TITLE run the suite')
        self.assertTrue(kid.tag.startswith('completed · 15 rec · '))
        self.assertEqual((kid.kind, kid.agent_id, kid.agent_type),
                         ('subagent', T3, 'completed'))
        self.assertEqual(kid.row_bg, r._ROW_BG_SPAWN)
        r.store_mark_orphan(kid)
        self.assertIn('orphan', kid.tag)
        self.assertEqual(r.store_child_session(self.paths[T1], T3), self.paths[T3])
        self.assertIsNone(r.store_child_session(self.paths[T2], T3))
        self.assertIsNone(r.store_child_session(self.paths[T1], 'nope'))
        self.assertEqual(r.store_parent_of_child(self.paths[T3]), (self.paths[T1], T3))
        self.assertIsNone(r.store_parent_of_child(self.paths[T1]))
        self.assertEqual(r.store_child_sessions(self.paths[T2]), [])

    def test_resolve_spawned_child_by_id_in_result(self):
        r = self.r
        call = r.new_tool(canon='spawn', name='spawn_agent', call_id='c',
                          input={'task': 'run'})
        hit = r.Record('tool_result', {}, tools=[r.new_tool(
            call_id='c', output=json.dumps({'agent_id': T3}))])
        self.assertEqual(r.store_resolve_spawned_child(self.paths[T1], call, hit),
                         (T3, self.paths[T3]))
        miss = r.Record('tool_result', {}, tools=[r.new_tool(call_id='c', output='x')])
        self.assertIsNone(r.store_resolve_spawned_child(self.paths[T1], call, miss))
        self.assertIsNone(r.store_resolve_spawned_child(self.paths[T2], call, hit))

    # ---- fallbacks -----------------------------------------------------------

    def test_without_the_db_rollouts_are_scanned(self):
        os.unlink(os.path.join(self.home, 'state_5.sqlite'))
        self.r.store_reset()
        projects = self.r.store_list_projects()
        self.assertEqual([p.id[1] for p in projects], [CWD_A, CWD_B])
        self.assertTrue(projects[0].tag.startswith('2 thread · '))   # no edges
        sessions = self.r.store_list_sessions(CWD_A)
        self.assertEqual([s.title for s in sessions], [
            'PROBE_PROMPT_ONE fix the build', 'PROBE_CHILD_TASK run the suite'])
        self.assertTrue(sessions[0].tag.startswith('15 rec · '))
        two = self.r.store_list_sessions(CWD_B)[0]
        self.assertEqual(two.title, 'PROBE_PROMPT_TWO explain this')   # 0.104 event
        self.assertEqual(self.r.store_child_sessions(self.paths[T1]), [])
        info = self.r.store_session_info(self.paths[T1])
        self.assertIn(('version', '0.161.0'), info['meta'])
        self.assertIn(('branch', 'main'), info['meta'])

    def test_without_the_sqlite_module_rollouts_are_scanned(self):
        saved = self.r.sqlite3
        self.r.sqlite3 = None
        try:
            self.r.store_reset()
            self.assertEqual(len(self.r.store_list_sessions(CWD_A)), 2)
        finally:
            self.r.sqlite3 = saved

    def test_rollout_outside_the_index_is_read_from_its_head(self):
        other = os.path.join(self.tmp.name, 'copied.jsonl')
        with open(self.paths[T2]) as src, open(other, 'w') as dst:
            dst.write(src.read())
        self.assertEqual(self.r.store_project_of_session(other), CWD_B)
        self.assertEqual(self.r.store_session_dirs(other), (CWD_B, CWD_B))
        self.assertEqual(self.r.store_session_info(other)['label'], T2)
        self.assertEqual(self.r.store_session_item(other).title,
                         'PROBE_PROMPT_TWO explain this')   # head prompt, no DB row

    # ---- args and facts ----------------------------------------------------

    def _pop(self, *argv):
        sys.argv = ['browse-agents-codex', *argv]
        return self.r.store_pop_args()

    def test_pop_args(self):
        self.assertEqual(self._pop(), {'target': None, 'error': None})
        self.assertEqual(self._pop('--thread', T1)['target'], self.paths[T1])
        self.assertEqual(self._pop(f'--thread={T2[:12]}')['target'], self.paths[T2])
        self.assertIn('no thread', self._pop('--thread', T1[:6])['error'])   # T1/T3 prefix
        self.assertEqual(self._pop('--file', self.paths[T3])['target'], self.paths[T3])
        self.assertIn('no such file', self._pop('--file', '/nope.jsonl')['error'])
        self.assertIn('mutually exclusive',
                      self._pop('--thread', T1, '--file', self.paths[T1])['error'])
        self.assertEqual(sys.argv, ['browse-agents-codex'])

    def test_resolve_positional(self):
        self.assertEqual(self.r.store_resolve_positional(self.paths[T1]),
                         ('session', self.paths[T1]))
        self.assertEqual(self.r.store_resolve_positional('/home/test/projA'),
                         ('project', '/home/test/projA'))

    def test_session_info_dirs_and_context(self):
        r = self.r
        info = r.store_session_info(self.paths[T1])
        self.assertEqual((info['path'], info['label'], info['is_child']),
                         (self.paths[T1], T1, False))
        self.assertEqual(info['meta'][:4], [
            ('thread', T1), ('title', 'PROBE_TITLE_ONE fix the build'),
            ('cwd', CWD_A), ('model', 'gpt-5.6')])
        self.assertIsNotNone(info['mtime'])
        child = r.store_session_info(self.paths[T3])
        self.assertTrue(child['is_child'])
        self.assertEqual(child['meta'][1], ('parent', T1))
        self.assertEqual(r.store_session_dirs(self.paths[T1]), (CWD_A, CWD_A))
        self.assertEqual(r.store_context_dirs(CWD_A), [('project', CWD_A)])
        self.assertEqual(r.store_context_dirs(CWD_A, self.paths[T1]),
                         [('project', CWD_A), ('cwd', CWD_A)])
        self.assertEqual(r.store_project_of_session(self.paths[T2]), CWD_B)
        self.assertEqual(r.store_project_path(CWD_B), CWD_B)
        self.assertTrue(r.store_session_exists(self.paths[T1]))
        self.assertFalse(r.store_session_exists('/nope.jsonl'))
        self.assertEqual(r.store_source_locator(self.paths[T1], 4), (self.paths[T1], 5))
        self.assertEqual(r.store_name(), 'codex')
        self.assertIn('--thread ID', r.store_help_usage())

    # ---- tail ---------------------------------------------------------------

    def test_tail_open_poll_and_excerpt(self):
        r = self.r
        path = self.paths[T1]
        records, state = r.store_tail_open(path)
        self.assertEqual([k for k, _ in records], list(range(15)))
        self.assertEqual([rec.kind for _, rec in records], [
            'meta', 'system', 'turn_start', 'meta', 'meta', 'user_prompt',
            'thinking', 'meta', 'tool_call', 'tool_result', 'meta', 'meta',
            'assistant_text', 'meta', 'turn_end'])
        self.assertEqual(state.ctx.pending, [])
        self.assertEqual(records[9][1].tool['glance'], 'PROBE_STDOUT ok')
        self.assertEqual(records[14][1].meta, {'duration_ms': 9000, 'tokens': 1234})
        self.assertEqual(r.store_tail_poll(path, state), ([], []))
        with open(path, 'a') as f:
            f.write(json.dumps({'timestamp': _ts(20), 'type': 'event_msg', 'payload': {
                'type': 'item_completed', 'turn_id': 'turn-2',
                'item': {'type': 'UserMessage', 'id': 'u2',
                         'content': [{'type': 'text', 'text': 'PROBE_APPENDED'}]}}}))
            f.write('\n{"partial": ')
        new, changed = r.store_tail_poll(path, state)
        self.assertEqual(changed, [])
        self.assertEqual([(k, rec.kind, rec.text) for k, rec in new],
                         [(15, 'user_prompt', 'PROBE_APPENDED')])
        self.assertEqual(r.store_tail_poll(path, state), ([], []))   # partial line waits
        with open(path, 'a') as f:
            f.write('1}\n')
        new, _ = r.store_tail_poll(path, state)
        self.assertEqual([(k, rec.kind) for k, rec in new], [(16, 'unknown')])
        tmp = r.store_source_excerpt({path: [0, 5]})
        try:
            with open(tmp) as f, open(path) as src:
                lines = src.read().split('\n')
                self.assertEqual(f.read(), lines[0] + '\n' + lines[5] + '\n')
        finally:
            os.unlink(tmp)
        self.assertEqual(r.store_tail_open('/nope.jsonl'), ([], None))
        self.assertEqual(r.store_tail_interval(), 5.0)

    def test_0104_rollout_folds_into_one_turn(self):
        r = self.r
        path = self.paths[T2]
        td = r._scan_tree(path)
        try:
            roots = [e['kind'] for e in td.roots_in_order]
            self.assertEqual(roots, ['span', 'turn'])
            prompt = td.records[td.roots_in_order[1]['line']]
            self.assertEqual(prompt.text, 'PROBE_PROMPT_TWO explain this')
            self.assertEqual(td.records[-1].kind, 'turn_end')
        finally:
            r._TREE_CACHE.pop(path, None)
            r._TAIL_STATE.pop(path, None)


if __name__ == '__main__':
    unittest.main()
