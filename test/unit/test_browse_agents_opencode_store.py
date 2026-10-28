"""Unit tests for the OpenCode store adapter in ``recipes/browse-agents-opencode``
(DB selection, v2 index, v1 fallback, children, source dumps, poll tail).

``make_fake_opencode`` builds a synthetic ``$XDG_DATA_HOME/opencode/opencode.db``
in either schema generation with the columns the store reads. The UI
smoke test reuses it.
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
_RECIPE = _REPO / 'recipes' / 'browse-agents-opencode'

P1, P2 = 'aaaa1111project', 'bbbb2222project'
CWD_A, CWD_B = '/home/test/projA', '/home/test/projB'
S1 = 'ses_0000000000000000000000000a'     # projA, two turns, streaming tail
S2 = 'ses_0000000000000000000000000b'     # projB, system + user
S3 = 'ses_0000000000000000000000000c'     # projA, child of S1
T0 = 1_790_000_000_000                    # ms


def _v2_rows():
    """``(session_id, message_id, type, seq, created, updated, data)`` rows."""
    asst = {
        'time': {'created': T0 + 1000, 'completed': T0 + 9000}, 'agent': 'build',
        'model': {'id': 'glm-latest', 'providerID': 'fireworks-ai', 'variant': 'default'},
        'finish': 'stop', 'cost': 0.0123,
        'tokens': {'input': 100, 'output': 50, 'reasoning': 10, 'cache': {'read': 0, 'write': 0}},
        'content': [
            {'type': 'reasoning', 'text': 'PROBE_REASONING think', 'time': {'created': T0 + 1100}},
            {'type': 'tool', 'id': 'call_shell_1', 'name': 'shell', 'executed': True,
             'state': {'status': 'completed', 'input': {'command': 'make test'},
                       'content': [{'type': 'text', 'text': 'PROBE_STDOUT ok'}],
                       'metadata': {'exit': 0, 'status': 'completed', 'truncated': False}},
             'time': {'created': T0 + 2000, 'ran': T0 + 2100, 'completed': T0 + 3000}},
            {'type': 'tool', 'id': 'call_edit_1', 'name': 'edit', 'executed': True,
             'state': {'status': 'completed',
                       'input': {'path': '/home/test/projA/a.py', 'oldString': 'x', 'newString': 'y'},
                       'content': [{'type': 'text', 'text': 'Edit applied'}],
                       'metadata': {'files': [{'file': 'a.py', 'additions': 1, 'deletions': 1,
                                               'status': 'modified',
                                               'patch': '@@ -1 +1 @@\n-x\n+y\n'}],
                                    'truncated': False}},
             'time': {'created': T0 + 4000, 'completed': T0 + 5000}},
            {'type': 'text', 'text': 'PROBE_ANSWER_ONE all green'},
        ],
    }
    streaming = {
        'time': {'created': T0 + 20000}, 'agent': 'build',
        'model': {'id': 'glm-latest', 'providerID': 'fireworks-ai'},
        'content': [
            {'type': 'tool', 'id': 'call_read_2', 'name': 'read', 'executed': False,
             'state': {'status': 'running', 'input': {'path': '/home/test/projA/b.py'}},
             'time': {'created': T0 + 21000}},
        ],
    }
    return [
        (S1, 'msg_01', 'user', 0, T0, T0,
         {'time': {'created': T0}, 'text': 'PROBE_PROMPT_ONE fix the build', 'files': [], 'agents': []}),
        (S1, 'msg_02', 'assistant', 1, T0 + 1000, T0 + 9000, asst),
        (S1, 'msg_03', 'idle', 2, T0 + 9500, T0 + 9500, {'time': {'created': T0 + 9500}, 'outcome': 'completed'}),
        (S1, 'msg_04', 'user', 3, T0 + 19000, T0 + 19000,
         {'time': {'created': T0 + 19000}, 'text': 'PROBE_PROMPT_TWO now read b'}),
        (S1, 'msg_05', 'assistant', 4, T0 + 20000, T0 + 21000, streaming),
        (S2, 'msg_11', 'system', 0, T0 + 500, T0 + 500,
         {'time': {'created': T0 + 500}, 'text': 'PROBE_SYSTEM injected note'}),
        (S2, 'msg_12', 'user', 1, T0 + 600, T0 + 600,
         {'time': {'created': T0 + 600}, 'text': 'PROBE_PROMPT_B hello'}),
        (S3, 'msg_21', 'user', 0, T0 + 6000, T0 + 6000,
         {'time': {'created': T0 + 6000}, 'text': 'PROBE_CHILD_TASK run the suite'}),
        (S3, 'msg_22', 'assistant', 1, T0 + 7000, T0 + 8000,
         {'time': {'created': T0 + 7000}, 'agent': 'explore',
          'model': {'id': 'm', 'providerID': 'p'}, 'finish': 'stop',
          'content': [{'type': 'text', 'text': 'PROBE_CHILD_ANSWER suite passed'}]}),
    ]


_SESSIONS = {
    # sid: (project, parent, directory, title, agent, model, created, updated)
    S1: (P1, None, CWD_A, 'PROBE_TITLE_ONE fix the build', 'build',
         '{"id":"glm-latest","providerID":"fireworks-ai","variant":"default"}', T0, T0 + 21000),
    S2: (P2, None, CWD_B, 'PROBE_TITLE_B hello', None, None, T0 + 500, T0 + 600),
    S3: (P1, S1, CWD_A, 'PROBE_CHILD_TITLE run the suite', 'explore',
         '{"id":"m","providerID":"p"}', T0 + 6000, T0 + 8000),
}

_PROJECTS = [(P1, CWD_A, 'git', None), (P2, CWD_B, 'git', 'projB'), ('global', '/', None, None)]

_SESSION_DDL = '''
    id text PRIMARY KEY, project_id text NOT NULL, parent_id text, slug text NOT NULL,
    directory text NOT NULL, title text, version text NOT NULL, agent text, model text,
    cost real DEFAULT 0 NOT NULL, tokens_input integer DEFAULT 0 NOT NULL,
    tokens_output integer DEFAULT 0 NOT NULL, time_created integer NOT NULL,
    time_updated integer NOT NULL'''


def _v1_rows():
    """``(message rows, part rows)`` for S1 in the 1.x shape."""
    user = {'role': 'user', 'time': {'created': T0}, 'agent': 'build',
            'model': {'providerID': 'fireworks-ai', 'modelID': 'glm-latest'}}
    asst = {'role': 'assistant', 'parentID': 'msg_01', 'agent': 'build',
            'modelID': 'glm-latest', 'providerID': 'fireworks-ai', 'cost': 0.01,
            'tokens': {'total': 160, 'input': 100, 'output': 50, 'reasoning': 10,
                       'cache': {'read': 0, 'write': 0}},
            'time': {'created': T0 + 1000, 'completed': T0 + 9000}, 'finish': 'stop'}
    messages = [('msg_01', S1, T0, T0, user), ('msg_02', S1, T0 + 1000, T0 + 9000, asst)]
    parts = [
        ('prt_010', 'msg_01', S1, T0, T0, {'type': 'text', 'text': 'PROBE_PROMPT_ONE fix the build'}),
        ('prt_020', 'msg_02', S1, T0 + 1000, T0 + 1000, {'type': 'step-start', 'snapshot': 'abc'}),
        ('prt_021', 'msg_02', S1, T0 + 1100, T0 + 1200,
         {'type': 'reasoning', 'text': 'PROBE_REASONING think', 'time': {'start': T0 + 1100, 'end': T0 + 1200}}),
        ('prt_022', 'msg_02', S1, T0 + 2000, T0 + 3000,
         {'type': 'tool', 'tool': 'read', 'callID': 'call_read_1',
          'state': {'status': 'completed', 'input': {'filePath': '/home/test/projA/a.py'},
                    'output': 'PROBE_FILE_BODY', 'metadata': {'truncated': False}, 'title': 'a.py',
                    'time': {'start': T0 + 2000, 'end': T0 + 3000}}}),
        ('prt_023', 'msg_02', S1, T0 + 4000, T0 + 4000,
         {'type': 'text', 'text': 'PROBE_ANSWER_ONE all green', 'time': {'start': T0 + 4000, 'end': T0 + 4500}}),
        ('prt_024', 'msg_02', S1, T0 + 9000, T0 + 9000,
         {'type': 'step-finish', 'reason': 'stop', 'snapshot': 'def', 'cost': 0.01,
          'tokens': {'total': 160, 'input': 100, 'output': 50, 'reasoning': 10,
                     'cache': {'read': 0, 'write': 0}}}),
    ]
    return messages, parts


def make_fake_opencode(tmpdir, gen='v2', name='opencode.db'):
    """Build ``<tmpdir>/share/opencode/<name>``; returns ``(xdg_data_home, db_path)``."""
    share = os.path.join(tmpdir, 'share')
    d = os.path.join(share, 'opencode')
    os.makedirs(d, exist_ok=True)
    db = os.path.join(d, name)
    con = sqlite3.connect(db)
    con.executescript('''
        CREATE TABLE project (id text PRIMARY KEY, worktree text NOT NULL, vcs text,
            name text, time_created integer NOT NULL, time_updated integer NOT NULL,
            sandboxes text NOT NULL DEFAULT '[]');
        CREATE TABLE kv (key text PRIMARY KEY, value text NOT NULL,
            time_created integer NOT NULL, time_updated integer NOT NULL);
    ''')
    con.executemany('INSERT INTO project (id, worktree, vcs, name, time_created, time_updated) '
                    'VALUES (?,?,?,?,?,?)', [(*p, T0, T0) for p in _PROJECTS])
    sessions = _SESSIONS if gen == 'v2' else {S1: _SESSIONS[S1]}
    table = 'session_v2' if gen == 'v2' else 'session'
    con.execute(f'CREATE TABLE {table} ({_SESSION_DDL})')
    for sid, (proj, parent, directory, title, agent, model, created, updated) in sessions.items():
        con.execute(
            f'INSERT INTO {table} (id, project_id, parent_id, slug, directory, title, version, '
            'agent, model, cost, tokens_input, tokens_output, time_created, time_updated) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (sid, proj, parent, 'slug-' + sid[-1], directory, title, '2.2.0', agent, model,
             0.0123 if sid == S1 else 0, 100 if sid == S1 else 0, 50 if sid == S1 else 0,
             created, updated))
    if gen == 'v2':
        con.execute('''CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL,
            type text NOT NULL, seq integer NOT NULL, time_created integer NOT NULL,
            time_updated integer NOT NULL, data text NOT NULL)''')
        con.executemany('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)', [
            (mid, sid, typ, seq, c, u, json.dumps(data))
            for sid, mid, typ, seq, c, u, data in _v2_rows()])
        con.execute("INSERT INTO kv VALUES ('migration.v1-v2', '1', ?, ?)", (T0, T0))
    else:
        con.executescript('''
            CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
            CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL,
                session_id text NOT NULL, time_created integer NOT NULL,
                time_updated integer NOT NULL, data text NOT NULL);
        ''')
        messages, parts = _v1_rows()
        con.executemany('INSERT INTO message VALUES (?,?,?,?,?)',
                        [(mid, sid, c, u, json.dumps(d)) for mid, sid, c, u, d in messages])
        con.executemany('INSERT INTO part VALUES (?,?,?,?,?,?)',
                        [(pid, mid, sid, c, u, json.dumps(d)) for pid, mid, sid, c, u, d in parts])
    con.commit()
    con.close()
    return share, db


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
    name = '_browse_agents_opencode_store'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    gen = 'v2'

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.share, self.db = make_fake_opencode(self.tmp.name, gen=self.gen)
        self._env = {k: os.environ.get(k) for k in ('XDG_DATA_HOME', 'OPENCODE_DB')}
        os.environ['XDG_DATA_HOME'] = self.share
        os.environ.pop('OPENCODE_DB', None)
        self._argv = sys.argv
        self.r._DB_OVERRIDE = None
        self.r.store_reset()

    def tearDown(self):
        sys.argv = self._argv
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.r._DB_OVERRIDE = None
        self.r.store_reset()
        self.r._TREE_CACHE.clear()
        self.r._TAIL_STATE.clear()
        self.tmp.cleanup()

    def _write(self, sql, params=()):
        con = sqlite3.connect(self.db)
        con.execute(sql, params)
        con.commit()
        con.close()


class OpenCodeV2StoreTests(_Base):

    def test_db_selection(self):
        r = self.r
        self.assertEqual(r._db_path(), self.db)
        channel = os.path.join(self.share, 'opencode', 'opencode-beta.db')
        with open(channel, 'w') as f:
            f.write('x')
        os.utime(self.db, (1, 1))
        self.assertEqual(r._db_path(), channel)           # newest mtime wins
        with open(self.db + '-wal', 'w') as f:            # a fresh WAL counts for its DB
            f.write('x')
        self.assertEqual(r._db_path(), self.db)
        os.unlink(self.db + '-wal')
        os.environ['OPENCODE_DB'] = '/elsewhere/opencode.db'
        self.assertEqual(r._db_path(), '/elsewhere/opencode.db')
        r._DB_OVERRIDE = self.db
        self.assertEqual(r._db_path(), self.db)
        os.environ.pop('OPENCODE_DB')

    def test_projects_and_sessions(self):
        r = self.r
        projects = r.store_list_projects()
        self.assertEqual([p.id for p in projects],
                         [('project', P1), ('project', P2)])      # 'global' has no sessions
        self.assertEqual(projects[0].title, CWD_A)
        self.assertEqual(projects[1].title, f'{CWD_B} (projB)')
        self.assertTrue(projects[0].tag.startswith('1 sess · '))      # child hidden
        sessions = r.store_list_sessions(P1)
        self.assertEqual([s.id for s in sessions], [('session', S1)])
        s = sessions[0]
        self.assertEqual(s.title, 'PROBE_TITLE_ONE fix the build')
        self.assertTrue(s.tag.startswith('build · fireworks-ai/glm-latest · 5 msg · 1 sub · '), s.tag)
        self.assertTrue(s.boundary)
        self.assertEqual((s.kind, s.line_count, s.subagent_count), ('session', 5, 1))
        self.assertEqual(r.store_session_item(S2).title, 'PROBE_TITLE_B hello')
        self.assertIsNone(r.store_session_item('nope'))
        self.assertTrue(r.store_session_exists(S3))
        self.assertFalse(r.store_session_exists('nope'))
        self.assertEqual(r._index()['gen'], 'v2')

    def test_children(self):
        r = self.r
        kids = r.store_child_sessions(S1)
        self.assertEqual([k.id for k in kids], [('agent', S1, S3)])
        kid = kids[0]
        self.assertEqual(kid.title, 'PROBE_CHILD_TITLE run the suite')
        self.assertTrue(kid.tag.startswith('explore · 2 msg · '))
        self.assertEqual((kid.kind, kid.agent_id, kid.agent_type), ('subagent', S3, 'explore'))
        self.assertEqual(kid.row_bg, r._ROW_BG_SPAWN)
        r.store_mark_orphan(kid)
        self.assertIn('orphan', kid.tag)
        self.assertEqual(r.store_child_session(S1, S3), S3)
        self.assertIsNone(r.store_child_session(S2, S3))
        self.assertEqual(r.store_parent_of_child(S3), (S1, S3))
        self.assertIsNone(r.store_parent_of_child(S1))
        self.assertEqual(r.store_child_sessions(S2), [])
        call = r.new_tool(canon='spawn', name='task', call_id='c', input={'prompt': 'x'})
        hit = r.Record('tool_result', {}, tools=[r.new_tool(
            call_id='c', output='done', metadata={'sessionId': S3})])
        self.assertEqual(r.store_resolve_spawned_child(S1, call, hit), (S3, S3))
        miss = r.Record('tool_result', {}, tools=[r.new_tool(call_id='c', output='x')])
        self.assertIsNone(r.store_resolve_spawned_child(S1, call, miss))

    def _pop(self, *argv):
        sys.argv = ['browse-agents-opencode', *argv]
        return self.r.store_pop_args()

    def test_pop_args_and_positional(self):
        r = self.r
        self.assertEqual(self._pop(), {'target': None, 'error': None})
        self.assertEqual(self._pop('--session', S1)['target'], S1)
        self.assertEqual(self._pop('--session', S2[:-1] + 'b')['target'], S2)
        self.assertIn('no session', self._pop('--session', 'ses_0')['error'])   # ambiguous
        self.assertIn('no such file', self._pop('--db', '/nope.db')['error'])
        other = os.path.join(self.tmp.name, 'copy.db')
        with open(self.db, 'rb') as src, open(other, 'wb') as dst:
            dst.write(src.read())
        os.environ['XDG_DATA_HOME'] = '/nowhere'
        self.assertEqual(self._pop('--db', other, '--session', S3)['target'], S3)
        self.assertEqual(r._db_path(), other)
        self.assertEqual(sys.argv, ['browse-agents-opencode'])
        self.assertEqual(r.store_resolve_positional(S1), ('session', S1))
        self.assertEqual(r.store_resolve_positional(CWD_B), ('project', P2))
        self.assertEqual(r.store_resolve_positional('/other'), ('project', '/other'))

    def test_info_dirs_and_paths(self):
        r = self.r
        info = r.store_session_info(S1)
        self.assertEqual((info['path'], info['label'], info['is_child']), (self.db, S1, False))
        self.assertEqual(info['meta'], [
            ('session', S1), ('title', 'PROBE_TITLE_ONE fix the build'), ('directory', CWD_A),
            ('agent', 'build'), ('model', 'fireworks-ai/glm-latest'), ('version', '2.2.0'),
            ('cost', '0.0123'), ('tokens', '150')])
        self.assertAlmostEqual(info['mtime'], (T0 + 21000) / 1000)
        child = r.store_session_info(S3)
        self.assertTrue(child['is_child'])
        self.assertEqual(child['meta'][1], ('parent', S1))
        self.assertEqual(r.store_session_dirs(S1), (CWD_A, CWD_A))
        self.assertEqual(r.store_context_dirs(P1), [('project', CWD_A)])
        self.assertEqual(r.store_context_dirs(P1, S1), [('project', CWD_A), ('cwd', CWD_A)])
        self.assertEqual(r.store_project_of_session(S2), P2)
        self.assertEqual(r.store_project_path(P2), CWD_B)
        self.assertEqual(r.store_project_path('/x'), '/x')
        self.assertEqual(r.store_source_locator(S1, 3), (self.db, None))
        self.assertEqual(r.store_name(), 'opencode')
        self.assertIn('--db PATH', r.store_help_usage())

    def test_tail_open_poll_and_dumps(self):
        r = self.r
        records, state = r.store_tail_open(S1)
        self.assertEqual([k for k, _ in records], list(range(11)))
        self.assertEqual([rec.kind for _, rec in records], [
            'user_prompt', 'thinking', 'tool_call', 'tool_result', 'tool_call', 'tool_result',
            'assistant_text', 'turn_end', 'user_prompt', 'tool_call', 'tool_result'])
        self.assertEqual(state.wm, (5, 4, T0 + 21000))
        self.assertEqual(state.locs[2:4], [('msg_02', 1), ('msg_02', 2)])
        pending = records[10][1]
        self.assertEqual((pending.tool['output'], pending.tool['glance']), (None, '(running)'))
        self.assertEqual(r.store_tail_poll(S1, state), ([], []))
        # The streaming assistant row completes its tool and gains a text.
        rows = {mid: data for sid, mid, _t, _s, _c, _u, data in _v2_rows() if sid == S1}
        grown = rows['msg_05']
        grown['content'][0]['state'] = {
            'status': 'completed', 'input': {'path': '/home/test/projA/b.py'},
            'content': [{'type': 'text', 'text': 'PROBE_B_BODY'}], 'metadata': {}}
        grown['content'].append({'type': 'text', 'text': 'PROBE_ANSWER_TWO b is fine'})
        grown['finish'] = 'stop'
        self._write('UPDATE session_message SET data = ?, time_updated = ? WHERE id = ?',
                    (json.dumps(grown), T0 + 25000, 'msg_05'))
        self._write('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)',
                    ('msg_06', S1, 'idle', 5, T0 + 26000, T0 + 26000,
                     json.dumps({'time': {'created': T0 + 26000}, 'outcome': 'completed'})))
        new, changed = r.store_tail_poll(S1, state)
        self.assertEqual([(k, rec.kind) for k, rec in changed],
                         [(9, 'tool_call'), (10, 'tool_result')])
        self.assertEqual(changed[1][1].tool['output'], 'PROBE_B_BODY')
        self.assertEqual([(k, rec.kind, rec.text or rec.summary) for k, rec in new], [
            (11, 'assistant_text', 'PROBE_ANSWER_TWO b is fine'),
            (12, 'turn_end', 'idle · completed')])
        self.assertTrue(new[0][1].end_turn)
        self.assertEqual(state.wm, (6, 5, T0 + 26000))
        self.assertEqual(r.store_tail_poll(S1, state), ([], []))
        dump = r.store_source_path(S1)
        with open(dump) as f:
            docs = json.load(f)
        self.assertEqual([d['id'] for d in docs], [f'msg_0{i}' for i in range(1, 7)])
        self.assertEqual(r.store_source_path(S1), dump)      # cached while unchanged
        tmp = r.store_source_excerpt({S1: [0, 3]})
        try:
            with open(tmp) as f:
                text = f.read()
        finally:
            os.unlink(tmp)
        self.assertIn('PROBE_PROMPT_ONE', text)
        self.assertIn('call_shell_1', text)
        self.assertIsNone(r.store_source_excerpt({'nope': [0]}))
        self.assertEqual(r.store_tail_open('nope'), ([], None))
        self.assertEqual(r.store_tail_interval(), 1.0)

    def test_tree_fold_of_a_session(self):
        r = self.r
        td = r._scan_tree(S1)
        roots = [(e['kind'], e.get('line')) for e in td.roots_in_order]
        self.assertEqual(roots, [('turn', 0), ('turn', 8)])
        self.assertEqual(sorted(td.tool_children), [2, 4, 9])
        self.assertEqual([rec.key for rec in td.tool_children[4]], [5])
        self.assertEqual(td.records[6].meta, {'model': 'fireworks-ai/glm-latest', 'agent': 'build',
                                              'tokens': 160, 'cost': '$0.0123'})
        self.assertEqual(td.latest_voice_line, 8)
        have_delta = r.HAVE_DELTA
        r.HAVE_DELTA = False          # plain unified-diff colouring
        try:
            umbrella = r._render_tool_umbrella(('tool', S1, 4))
        finally:
            r.HAVE_DELTA = have_delta
        self.assertIn('+y', umbrella)
        self.assertNotIn('Edit applied', umbrella)

    def test_missing_db_yields_nothing(self):
        os.environ['XDG_DATA_HOME'] = '/nowhere'
        self.r.store_reset()
        self.assertIsNone(self.r._index()['gen'])
        self.assertEqual(self.r.store_list_projects(), [])
        self.assertEqual(self.r.store_tail_open(S1), ([], None))


class OpenCodeV1StoreTests(_Base):
    gen = 'v1'

    def test_v1_fallback_lists_and_reads(self):
        r = self.r
        self.assertEqual(r._index()['gen'], 'v1')
        sessions = r.store_list_sessions(P1)
        self.assertEqual([s.id for s in sessions], [('session', S1)])
        self.assertTrue(sessions[0].tag.startswith('build · fireworks-ai/glm-latest · 2 msg · '))
        records, state = r.store_tail_open(S1)
        self.assertEqual([rec.kind for _, rec in records], [
            'user_prompt', 'turn_start', 'thinking', 'tool_call', 'tool_result',
            'assistant_text', 'turn_end'])
        self.assertEqual(state.wm, (6, T0 + 9000))
        self.assertEqual(records[3][1].tool['input']['file_path'], '/home/test/projA/a.py')
        self.assertEqual(records[4][1].tool['output'], 'PROBE_FILE_BODY')
        self.assertTrue(records[5][1].end_turn)
        self.assertEqual(records[6][1].meta, {'model': 'fireworks-ai/glm-latest', 'agent': 'build',
                                              'tokens': 160, 'cost': '$0.0100'})
        self.assertEqual(r.store_tail_poll(S1, state), ([], []))
        self._write('UPDATE part SET data = ?, time_updated = ? WHERE id = ?',
                    (json.dumps({'type': 'text', 'text': 'PROBE_ANSWER_ONE all green, really'}),
                     T0 + 9500, 'prt_023'))
        self._write('INSERT INTO part VALUES (?,?,?,?,?,?)',
                    ('prt_025', 'msg_02', S1, T0 + 9600, T0 + 9600,
                     json.dumps({'type': 'snapshot', 'snapshot': 'xyz'})))
        new, changed = r.store_tail_poll(S1, state)
        self.assertEqual([(k, rec.text) for k, rec in changed],
                         [(5, 'PROBE_ANSWER_ONE all green, really')])
        self.assertEqual([(k, rec.kind, rec.tag) for k, rec in new], [(7, 'meta', 'snapshot')])
        dump = r.store_source_path(S1)
        with open(dump) as f:
            docs = json.load(f)
        self.assertEqual(len(docs), 7)
        self.assertEqual(docs[0]['part']['type'], 'text')


if __name__ == '__main__':
    unittest.main()
