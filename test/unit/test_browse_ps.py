"""Unit tests for the ``recipes/browse-ps`` context menu.

The recipe is a ``--run-py`` script that imports ``browse_tui`` (only a real
module when the binary loads it), so we stub ``browse_tui`` in ``sys.modules``
and load the extension-less recipe via ``SourceFileLoader`` — the same pattern
as ``test/unit/test_browse_git.py`` / ``test_browse_fs.py``.

The pilot context-menu convention (ticket #1033) is that the option list is a
PURE builder, ``context_menu_options(ctx)``, that inspects ``ctx.cursor`` and
returns ``(label, value)`` rows WITHOUT opening a modal. We exercise it against
a REAL headless ``Browser`` / ``Context`` (from ``test.async_._helpers``) with
a known process item under the cursor — not a fake ctx. browse-tui swallows
``on_context_menu`` exceptions and a fake ctx hides bugs, so the real
``Context.cursor`` read path is what we assert against; ``ctx.menu`` itself
short-circuits to ``None`` in headless mode, which is exactly why the builder
is split out and tested directly.
"""

import importlib.util
import subprocess
import sys
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from test.async_._helpers import Browser, BrowserConfig, Context, Item, make_browser


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-ps'


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    The recipe pulls ``Action`` / ``Browser`` / ``BrowserConfig`` / ``Item``
    from ``browse_tui``; none are exercised by the pure builders under test
    (the cursor item comes from the REAL Browser below), so inert stubs are
    enough to let the module load. A fresh module each call keeps a stub left
    by another recipe's test from bleeding in.
    """
    mod = types.ModuleType('browse_tui')

    class _Stub:
        def __init__(self, *a, **kw):
            self._args = a
            for k, v in kw.items():
                setattr(self, k, v)

    mod.Action = _Stub
    mod.Browser = _Stub
    mod.BrowserConfig = _Stub
    mod.Item = _Stub
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-ps recipe; returns a fresh module."""
    _stub_browse_tui()
    name = '_browse_ps_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _browser_with_proc(pid=4242, title='myproc', user='alice'):
    """A real headless Browser whose cursor sits on a single process item.

    Mirrors what the recipe's ``get_children`` produces for a pid row (id is
    the int pid; ``.pid`` / ``.user`` attributes hung on the Item), so the
    builder reads a faithful cursor. The cursor is parked on the pid row via
    ``cursor_to`` after the root children settle.
    """
    item = Item(id=pid, title=title, tag=f'{user} pid={pid}',
                tag_style='dim', has_children=False)
    item.pid = pid
    item.user = user
    b = make_browser(get_children=lambda _id, *, reload=False: [item])
    b.refresh()
    b.run_until_idle()
    b.cursor_to(pid)
    b.run_until_idle()
    return b


class TestContextMenuOptions(unittest.TestCase):
    """``context_menu_options`` returns the right rows against a real cursor."""

    def setUp(self):
        self.r = _load_recipe()
        self.b = _browser_with_proc()
        self.ctx = Context(self.b)

    def tearDown(self):
        self.b.stop_workers()

    def test_cursor_is_the_real_process_item(self):
        # Sanity: the real Context resolves the parked pid row (not a stub).
        self.assertEqual(self.ctx.cursor.id, 4242)
        self.assertEqual(self.ctx.cursor.title, 'myproc')

    def test_top_level_entries_present_in_order(self):
        opts = self.r.context_menu_options(self.ctx)
        labels = [label for label, _value in opts]
        self.assertEqual(labels, [
            'Send signal…',
            'Show open files',
            'Show sockets',
            'Show environment',
            'Show full status',
            'Renice…',
            'Strace',
        ])

    def test_top_level_values_are_dispatch_tokens(self):
        # Every value token routes to a handler in the dispatch table.
        opts = self.r.context_menu_options(self.ctx)
        values = [value for _label, value in opts]
        self.assertEqual(values, [
            'signal', 'lsof', 'sockets', 'environ', 'status', 'renice', 'strace',
        ])
        for value in values:
            self.assertIn(value, self.r._MENU_ACTIONS)

    def test_no_cursor_yields_empty_list(self):
        # An empty tree → no cursor item → the hook opens nothing.
        empty = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            empty.refresh()
            empty.run_until_idle()
            ctx = Context(empty)
            self.assertIsNone(ctx.cursor)
            self.assertEqual(self.r.context_menu_options(ctx), [])
        finally:
            empty.stop_workers()

    def test_no_clipboard_or_copy_entries(self):
        # Convention (#1028): recipe menus carry no clipboard / Copy rows.
        labels = [l for l, _ in self.r.context_menu_options(self.ctx)]
        for label in labels:
            self.assertNotIn('copy', label.lower())


class TestSignalMenuOptions(unittest.TestCase):
    """The signal submenu lists the six signals; SIGTERM shows the ``(k)`` hint.

    ``signal_menu_options`` is pure and takes no ctx (the signal set is the
    same for every process), so these cases need no Browser — they assert the
    static rows + the ``_SIGNALS`` destructive flags directly.
    """

    def setUp(self):
        self.r = _load_recipe()

    def test_signal_names_and_order(self):
        opts = self.r.signal_menu_options()
        # The value half is the bare signal name; the display may add a hint.
        self.assertEqual([value for _label, value in opts],
                         ['SIGTERM', 'SIGKILL', 'SIGINT',
                          'SIGHUP', 'SIGSTOP', 'SIGCONT'])

    def test_sigterm_row_shows_k_hotkey_hint(self):
        # SIGTERM duplicates the ``k`` action, so its menu row mentions ``(k)``;
        # no other signal row carries a hotkey hint.
        rows = dict((value, label)
                    for label, value in self.r.signal_menu_options())
        self.assertEqual(rows['SIGTERM'], 'SIGTERM (k)')
        for name in ('SIGKILL', 'SIGINT', 'SIGHUP', 'SIGSTOP', 'SIGCONT'):
            self.assertEqual(rows[name], name)
            self.assertNotIn('(k)', rows[name])

    def test_destructive_signals_are_flagged(self):
        # The send path confirms iff the signal is flagged destructive: the
        # strong/disruptive ones do, SIGINT / SIGCONT do not.
        flags = {name: destructive for name, _signum, destructive in self.r._SIGNALS}
        self.assertEqual(flags, {
            'SIGTERM': True, 'SIGKILL': True, 'SIGHUP': True, 'SIGSTOP': True,
            'SIGINT': False, 'SIGCONT': False,
        })


# --- Data layer (B0/B2/B6/B7) -----------------------------------------------

def _ps_completed(stdout):
    """A ``subprocess.run`` stand-in returning canned ``ps`` stdout (rc 0)."""
    def _run(argv, *a, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr='')
    return _run


# Canned ``ps -eo pid=,ppid=,user:32=,pcpu=,rss=,time=,args=`` output. The user
# column is wide (untruncated) and args carries embedded spaces, so the split
# (maxsplit=6) is exercised. cpu_secs come from the ``time`` column.
_PS_OUT = (
    '    1     0 root                              0.5  9164 00:03:29 /sbin/init splash\n'
    '  100     1 alice                            12.0  4096 00:00:30 python3 -m http.server 8000\n'
    '  200   100 averylongusername1234567890       0.0   512 01:02:03 /bin/sleep 99\n'
)


class _DataLayerBase(unittest.TestCase):
    """Shared setup: load the recipe and reset its module-level snapshot state.

    The snapshot/diff state is module-global (so CPU% can diff across reloads),
    so each test reloads a fresh recipe module and the platform/user-width
    probes start from their defaults.
    """

    def setUp(self):
        self.r = _load_recipe()
        # A reload returns a fresh module, but be explicit about the diff state.
        self.r._PREV_SNAPSHOT = None
        self.r._CUR_SNAPSHOT = None
        self.r._PS_USER_WIDTH_OK = None
        self.r._UID_NAMES = {}

    def _snapshot(self, ps_out, *, platform='linux', private=None, reload=True):
        """Run ``_snapshot`` with mocked ``ps`` / platform / smaps reads.

        ``private`` maps pid → private-memory bytes (Linux smaps_rollup result);
        a pid absent from the map (or ``private=None``) reads as ``None`` so the
        snapshot falls back to RSS.
        """
        priv = private or {}
        with mock.patch.object(self.r.subprocess, 'run', _ps_completed(ps_out)), \
             mock.patch.object(self.r.sys, 'platform', platform), \
             mock.patch.object(self.r, '_private_mem_bytes',
                               lambda pid: priv.get(pid)):
            return self.r._snapshot(reload=reload)


class TestTimeParse(_DataLayerBase):
    """``_parse_time`` handles the ``[[DD-]HH:]MM:SS`` shapes."""

    def test_shapes(self):
        cases = {
            '00:00': 0,
            '00:00:00': 0,
            '00:03:29': 209,          # ps HH:MM:SS
            '10:30': 630,             # POSIX MM:SS
            '1-02:03:04': 93784,      # BSD/long DD-HH:MM:SS
        }
        for field, secs in cases.items():
            with self.subTest(field=field):
                self.assertEqual(self.r._parse_time(field), secs)

    def test_garbage_is_zero(self):
        # Unparseable input degrades to 0 (the row still renders).
        self.assertEqual(self.r._parse_time('not-a-time'), 0)
        self.assertEqual(self.r._parse_time('x-01:02'), 0)


class TestSnapshotParsing(_DataLayerBase):
    """``_snapshot`` splits 7 fields keeping args intact and parses each."""

    def test_args_kept_intact(self):
        procs = self._snapshot(_PS_OUT)
        self.assertEqual(procs[100].args, 'python3 -m http.server 8000')
        self.assertEqual(procs[1].args, '/sbin/init splash')

    def test_fields_parsed(self):
        procs = self._snapshot(_PS_OUT)
        self.assertEqual(procs[1].ppid, 0)
        self.assertEqual(procs[100].ppid, 1)
        self.assertEqual(procs[100].pcpu, 12.0)
        self.assertEqual(procs[100].rss_bytes, 4096 * 1024)
        self.assertEqual(procs[200].cpu_secs, 3723)   # 01:02:03

    def test_malformed_lines_skipped(self):
        # Too few fields / non-int pid are dropped, not fatal.
        out = _PS_OUT + 'garbage line\n' + '  abc 1 root 0 0 00:00:00 x\n'
        procs = self._snapshot(out)
        self.assertEqual(set(procs), {1, 100, 200})


class TestUsername(_DataLayerBase):
    """Untruncated usernames: wide ps column, else uid+pwd fallback."""

    def test_wide_column_untruncated(self):
        # The :32 form is honoured (max width > 8): names pass through verbatim.
        procs = self._snapshot(_PS_OUT)
        self.assertEqual(procs[200].user, 'averylongusername1234567890')
        self.assertIs(self.r._PS_USER_WIDTH_OK, True)

    def test_truncated_column_falls_back_to_uid(self):
        # A procps variant that ignores :width truncates every name to 8 chars.
        # The probe latches the fallback and re-runs with uid=, resolved via pwd.
        trunc = (
            '    1     0 root      0.0  100 00:00:01 /sbin/init\n'
            '  100     1 alicelon  0.0  100 00:00:01 app\n'   # truncated at 8
        )
        uid_out = (
            '    1     0 0    0.0  100 00:00:01 /sbin/init\n'
            '  100     1 1000 0.0  100 00:00:01 app\n'
        )
        calls = []

        def _run(argv, *a, **kw):
            calls.append(argv)
            # First call asks for user:32 (truncated variant); second for uid=.
            spec = argv[2]
            out = uid_out if 'uid=' in spec else trunc
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr='')

        with mock.patch.object(self.r.subprocess, 'run', _run), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None), \
             mock.patch.dict(sys.modules,
                             {'pwd': types.SimpleNamespace(
                                 getpwuid=lambda u: types.SimpleNamespace(
                                     pw_name={0: 'root', 1000: 'alice'}[int(u)]))}):
            procs = self.r._snapshot(reload=True)

        self.assertIs(self.r._PS_USER_WIDTH_OK, False)
        self.assertEqual(procs[1].user, 'root')
        self.assertEqual(procs[100].user, 'alice')
        # Two ps invocations: the probe, then the uid= re-run.
        self.assertEqual(len(calls), 2)
        self.assertIn('uid=', calls[1][2])


class TestMemorySelection(_DataLayerBase):
    """Private memory on Linux (smaps), RSS on macOS / when smaps absent."""

    def test_linux_private_when_available(self):
        # smaps_rollup gives private bytes → used instead of RSS.
        procs = self._snapshot(_PS_OUT, platform='linux',
                               private={100: 2 * 1024 * 1024})
        self.assertEqual(procs[100].mem_bytes, 2 * 1024 * 1024)
        # pid 1 has no private figure → falls back to RSS (9164 KiB).
        self.assertEqual(procs[1].mem_bytes, 9164 * 1024)

    def test_macos_uses_rss(self):
        # On darwin smaps is never consulted; RSS from the ps column is used.
        procs = self._snapshot(_PS_OUT, platform='darwin',
                               private={100: 2 * 1024 * 1024})
        self.assertEqual(procs[100].mem_bytes, 4096 * 1024)


class TestCpuPercent(_DataLayerBase):
    """CPU% = cumulative-time delta, with the pcpu lifetime fallback."""

    def test_first_sample_uses_pcpu(self):
        # No prior snapshot → fall back to the ps pcpu lifetime average.
        self._snapshot(_PS_OUT)
        self.assertEqual(self.r._cpu_pct(self.r._CUR_SNAPSHOT[1][100]), 12)

    def test_delta_overrides_pcpu(self):
        # Two snapshots with a known wall + cpu_secs gap → instantaneous %.
        self._snapshot(_PS_OUT)
        # Advance: pid 100 burns +4 cpu_secs over a 1s wall gap on 8 cores
        # → 100 * 4 / (1 * 8) = 50%.
        prev_wall = self.r._CUR_SNAPSHOT[0]
        out2 = (
            '    1     0 root  0.5  9164 00:03:29 /sbin/init\n'
            '  100     1 alice 12.0 4096 00:00:34 python3\n'   # 30s → 34s = +4
        )
        with mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r.subprocess, 'run', _ps_completed(out2)), \
             mock.patch.object(self.r.os, 'cpu_count', lambda: 8), \
             mock.patch.object(self.r.time, 'monotonic', lambda: prev_wall + 1.0):
            self.r._snapshot(reload=True)
        self.assertEqual(self.r._cpu_pct(self.r._CUR_SNAPSHOT[1][100]), 50)

    def test_new_pid_uses_pcpu(self):
        # A pid present only in the current snapshot has no prior → pcpu.
        self._snapshot(_PS_OUT)
        out2 = _PS_OUT + '  300     1 bob  7.0  100 00:00:05 newproc\n'
        procs = self._snapshot(out2)
        self.assertEqual(self.r._cpu_pct(procs[300]), 7)


class TestSnapshotReuse(_DataLayerBase):
    """Resample on reload / first call; reuse the cache on reload=False."""

    def test_reuse_when_not_reload(self):
        first = self._snapshot(_PS_OUT, reload=True)
        # A reload=False call must NOT re-run ps; it returns the same map.
        def _boom(*a, **kw):
            raise AssertionError('ps re-run on reload=False')
        with mock.patch.object(self.r.subprocess, 'run', _boom):
            again = self.r._snapshot(reload=False)
        self.assertIs(again, first)

    def test_first_call_samples_even_without_reload(self):
        # No snapshot yet → resample regardless of the reload flag.
        procs = self._snapshot(_PS_OUT, reload=False)
        self.assertEqual(set(procs), {1, 100, 200})

    def test_reload_rotates_previous(self):
        self._snapshot(_PS_OUT, reload=True)
        cur = self.r._CUR_SNAPSHOT
        self._snapshot(_PS_OUT, reload=True)
        self.assertIs(self.r._PREV_SNAPSHOT, cur)


class TestGetChildrenColumns(_DataLayerBase):
    """``get_children`` sets title=args and the col_* display fields."""

    def _children(self, parent, ps_out=_PS_OUT, **kw):
        with mock.patch.object(self.r.subprocess, 'run', _ps_completed(ps_out)), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None):
            return self.r.get_children(parent, **kw)

    def test_root_is_pid1_with_columns(self):
        kids = self._children(None, reload=True)
        self.assertEqual([k.id for k in kids], [1])
        item = kids[0]
        self.assertEqual(item.title, '/sbin/init splash')      # full args title
        self.assertEqual(item.col_pid, '1')
        self.assertEqual(item.col_user, 'root')
        self.assertEqual(item.col_cpu, '0%')                   # round(0.5)
        self.assertEqual(item.col_mem, self.r.human_size(9164 * 1024))
        self.assertTrue(item.has_children)                     # pid 100 is a child

    def test_child_listing(self):
        # pid 100's child is pid 200 (untruncated username column).
        kids = self._children(100, reload=False)
        self.assertEqual([k.id for k in kids], [200])
        self.assertEqual(kids[0].col_user, 'averylongusername1234567890')
        self.assertEqual(kids[0].title, '/bin/sleep 99')

    def test_empty_ps_yields_no_children(self):
        # The graceful ps-error → [] behaviour is preserved.
        kids = self._children(None, ps_out='', reload=True)
        self.assertEqual(kids, [])


if __name__ == '__main__':
    unittest.main()
