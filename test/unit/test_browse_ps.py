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
import threading
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from test.async_._helpers import (
    Browser, BrowserConfig, Context, Item, make_browser,
    mod as _fw_mod, remove as _fw_remove, upsert as _fw_upsert,
)
from test.unit._loader import load


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-ps'


# Real framework render helpers (B3 gutter): the recipe's ``ps_chrome`` /
# ``ps_gutter_segments`` compose the genuine chrome atoms and cell-justifiers,
# so we load them from ``src-tui/`` rather than re-implement them. The chrome
# atoms live in 040-state but reference a couple of render-layer names at call
# time (``_MARKER_COLOR`` / ``_TAG_STYLE`` / ``cell_width``); inject them the
# same way test/unit/test_render.py does for its isolated load.
_FW_DATA = load('_browse_ps_fw_data', '030-data.py')
_FW_TERM = load('_browse_ps_fw_term', '020-terminal.py')
_FW_STATE = load('_browse_ps_fw_state', '040-state.py')
_FW_RENDER = load('_browse_ps_fw_render', '050-render.py')
_FW_RENDER._char_width = _FW_TERM._char_width
_FW_RENDER._visible_len = _FW_TERM._visible_len
_FW_RENDER._ANSI_CSI_RE = _FW_TERM._ANSI_CSI_RE
for _name in ('_TAG_STYLE', '_MARKER_COLOR', 'cell_width'):
    setattr(_FW_STATE, _name, getattr(_FW_RENDER, _name))


def _stub_browse_tui():
    """Insert a ``browse_tui`` stub the recipe can import from.

    The recipe pulls ``Action`` / ``Browser`` / ``BrowserConfig`` / ``Item``
    (inert here — the cursor item comes from the REAL Browser below) plus the
    render helpers ``ps_chrome`` / ``ps_gutter_segments`` compose:
    ``cell_rjust`` / ``cell_ljust`` / ``style`` and the three chrome atoms.
    Those are wired to the GENUINE framework functions (loaded above) so the
    gutter tests exercise real cell-width justification and real atom
    composition, not a re-implementation. A fresh module each call keeps a
    stub left by another recipe's test from bleeding in.
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
    mod.cell_ljust = _FW_RENDER.cell_ljust
    mod.cell_rjust = _FW_RENDER.cell_rjust
    mod.style = _FW_RENDER.style
    mod.default_row_selection = _FW_STATE.default_row_selection
    mod.default_row_indent = _FW_STATE.default_row_indent
    mod.default_row_expander = _FW_STATE.default_row_expander
    # The genuine framework ``recipe_argv`` (strips --tty etc.); the CLI flag
    # tests drive it by patching ``_FW_STATE.sys.argv`` (its argv source).
    mod.recipe_argv = _FW_STATE.recipe_argv
    # The REAL op constructors the incremental ``-d`` tick builds with
    # (``mod`` / ``upsert`` / ``remove``) — genuine tuples so the spied
    # ``b.update_data`` batch is asserted against, and so applying it to a real
    # headless Browser exercises the framework's actual apply path. Sourced from
    # the SAME state module the headless ``Browser`` uses (via ``_helpers``) so
    # ``mod``'s ``KEEP_PARENT`` default is the very sentinel ``apply_ops``
    # checks — a distinct isolated load would carry a non-identical sentinel.
    mod.mod = _fw_mod
    mod.upsert = _fw_upsert
    mod.remove = _fw_remove
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
    the int pid; ``.pid`` / ``.user`` attributes hung on the Item; no ``tag``
    now that pid/user ride in the gutter), so the builder reads a faithful
    cursor. The cursor is parked on the pid row via ``cursor_to`` after the
    root children settle.
    """
    item = Item(id=pid, title=title, has_children=False)
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

    def _snapshot(self, ps_out, *, platform='linux', private=None,
                  cpu_secs=None, reload=True):
        """Run ``_snapshot`` with mocked ``ps`` / platform / per-pid /proc reads.

        ``private`` maps pid → private-memory bytes (smaps_rollup result), and
        ``cpu_secs`` maps pid → fine ``/proc/<pid>/stat`` CPU seconds. A pid
        absent from a map (or the map omitted) reads as ``None``, so the
        snapshot falls back to RSS / the coarse ps TIME column respectively.
        Mocking ``_proc_cpu_secs`` also keeps the suite off real ``/proc`` for
        the fake pids in the canned output.
        """
        priv = private or {}
        cpu = cpu_secs or {}
        with mock.patch.object(self.r.subprocess, 'run', _ps_completed(ps_out)), \
             mock.patch.object(self.r.sys, 'platform', platform), \
             mock.patch.object(self.r, '_private_mem_bytes',
                               lambda pid: priv.get(pid)), \
             mock.patch.object(self.r, '_proc_cpu_secs',
                               lambda pid: cpu.get(pid)):
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
    """CPU% = cumulative-time delta (per-core), with the pcpu lifetime fallback."""

    def test_first_sample_uses_pcpu(self):
        # No prior snapshot → fall back to the ps pcpu lifetime average.
        self._snapshot(_PS_OUT)
        self.assertEqual(self.r._cpu_pct(self.r._CUR_SNAPSHOT[1][100]), 12)

    def test_delta_is_per_core_and_subsecond(self):
        # The fix: fine /proc cpu time gives sub-second deltas, and the formula
        # is per-core (no ncpu divisor). +0.5 cpu over a 1 s wall gap → 50% —
        # the old whole-second ps-TIME source would have deltaed to 0 here.
        self._snapshot(_PS_OUT, cpu_secs={1: 200.0, 100: 30.0})
        prev_wall = self.r._CUR_SNAPSHOT[0]
        with mock.patch.object(self.r.time, 'monotonic', lambda: prev_wall + 1.0):
            self._snapshot(_PS_OUT, cpu_secs={1: 200.0, 100: 30.5})
        self.assertEqual(self.r._cpu_pct(self.r._CUR_SNAPSHOT[1][100]), 50)

    def test_delta_can_exceed_100_per_core(self):
        # +2 cpu-seconds per wall-second = 200% (two cores), per-core convention.
        self._snapshot(_PS_OUT, cpu_secs={1: 200.0, 100: 10.0})
        prev_wall = self.r._CUR_SNAPSHOT[0]
        with mock.patch.object(self.r.time, 'monotonic', lambda: prev_wall + 1.0):
            self._snapshot(_PS_OUT, cpu_secs={1: 200.0, 100: 12.0})
        self.assertEqual(self.r._cpu_pct(self.r._CUR_SNAPSHOT[1][100]), 200)

    def test_negative_delta_falls_back_to_pcpu(self):
        # A recycled pid / counter reset (negative delta) → pcpu, not a bogus %.
        self._snapshot(_PS_OUT, cpu_secs={1: 200.0, 100: 99.0})
        prev_wall = self.r._CUR_SNAPSHOT[0]
        with mock.patch.object(self.r.time, 'monotonic', lambda: prev_wall + 1.0):
            self._snapshot(_PS_OUT, cpu_secs={1: 200.0, 100: 1.0})
        self.assertEqual(self.r._cpu_pct(self.r._CUR_SNAPSHOT[1][100]), 12)  # pcpu

    def test_new_pid_uses_pcpu(self):
        # A pid present only in the current snapshot has no prior → pcpu.
        self._snapshot(_PS_OUT)
        out2 = _PS_OUT + '  300     1 bob  7.0  100 00:00:05 newproc\n'
        procs = self._snapshot(out2)
        self.assertEqual(self.r._cpu_pct(procs[300]), 7)

    def test_linux_uses_fine_cpu_time_over_ps_time(self):
        # On Linux the fine /proc cpu time wins over the coarse ps TIME column.
        procs = self._snapshot(_PS_OUT, cpu_secs={100: 42.5})
        self.assertEqual(procs[100].cpu_secs, 42.5)        # not 30 (TIME column)

    def test_falls_back_to_ps_time_when_proc_unavailable(self):
        # /proc read fails (→ None) → the coarse ps TIME column (whole seconds).
        procs = self._snapshot(_PS_OUT)                    # _proc_cpu_secs → None
        self.assertEqual(procs[100].cpu_secs, 30)          # 00:00:30


class TestProcCpuSecs(_DataLayerBase):
    """``_proc_cpu_secs`` parses utime+stime from /proc/<pid>/stat."""

    def test_parses_utime_stime_past_parenthesised_comm(self):
        # comm (field 2) may contain spaces and ')'; we slice past the LAST ')'.
        # After it: state ppid pgrp session tty tpgid flags minflt cminflt
        # majflt cmajflt utime stime … → utime index 11, stime index 12.
        line = '100 (weird )proc) S 1 100 100 0 -1 4194304 100 0 0 0 1234 567 0 0'
        with mock.patch.object(self.r, '_CLK_TCK', 100), \
             mock.patch('builtins.open', mock.mock_open(read_data=line)):
            self.assertAlmostEqual(self.r._proc_cpu_secs(100), (1234 + 567) / 100)

    def test_none_on_unreadable(self):
        with mock.patch('builtins.open', side_effect=OSError):
            self.assertIsNone(self.r._proc_cpu_secs(99999999))


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
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None), \
             mock.patch.object(self.r, '_proc_cpu_secs', lambda pid: None):
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

    def test_no_tag_chip_on_normal_rows(self):
        # B3 moved pid/user into the gutter; the old ``tag='user pid=…'`` chip
        # is gone (would otherwise render redundantly next to the title).
        for parent in (None, 1, 100):
            for item in self._children(parent, reload=(parent is None)):
                self.assertIsNone(getattr(item, 'tag', None))
                self.assertIsNone(getattr(item, 'tag_style', None))


# --- Flat / tree toggle (B4) ------------------------------------------------

# A small canned table with a two-level hierarchy AND a grandchild, so flat
# mode (every process, one list) is visibly distinct from tree mode (root →
# pid 1; a pid → its direct children only).
_PS_TREE = (
    '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
    '  100     1 root  1.0  200 00:00:02 parent\n'
    '  200   100 root  2.0  300 00:00:03 child\n'
    '  300   200 root  3.0  400 00:00:04 grandchild\n'
)


class TestTreeFlatDispatch(_DataLayerBase):
    """``get_children`` dispatches on ``_TREE_MODE`` (B4)."""

    def _children(self, parent, *, tree, ps_out=_PS_TREE, reload=True):
        self.r._TREE_MODE = tree
        with mock.patch.object(self.r.subprocess, 'run', _ps_completed(ps_out)), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None), \
             mock.patch.object(self.r, '_proc_cpu_secs', lambda pid: None):
            return self.r.get_children(parent, reload=reload)

    def test_tree_root_is_pid1(self):
        # tree mode root → just the init process (current behavior).
        kids = self._children(None, tree=True)
        self.assertEqual([k.id for k in kids], [1])
        self.assertTrue(kids[0].has_children)

    def test_tree_child_listing(self):
        # tree mode: a pid → its DIRECT children only (pid 100 → pid 200).
        kids = self._children(100, tree=True, reload=False)
        self.assertEqual([k.id for k in kids], [200])
        self.assertTrue(kids[0].has_children)             # 200 has child 300

    def test_flat_root_is_all_processes_pid_sorted(self):
        # flat mode root → every process, ascending pid, all leaves.
        kids = self._children(None, tree=False)
        self.assertEqual([k.id for k in kids], [1, 100, 200, 300])
        for k in kids:
            self.assertFalse(k.has_children)

    def test_flat_non_root_is_empty(self):
        # flat rows don't expand: any non-None pid → [].
        self.assertEqual(self._children(100, tree=False, reload=False), [])
        self.assertEqual(self._children(1, tree=False, reload=False), [])

    def test_flat_rows_carry_the_same_columns(self):
        # The shared ``_proc_item`` builder gives flat rows the col_* gutter
        # fields and the full-args title, exactly like tree rows.
        kids = self._children(None, tree=False)
        row = next(k for k in kids if k.id == 100)
        self.assertEqual(row.title, 'parent')
        self.assertEqual(row.col_pid, '100')
        self.assertEqual(row.col_user, 'root')
        self.assertEqual(row.col_mem, self.r.human_size(200 * 1024))
        self.assertEqual(row.pid, 100)

    def test_flat_empty_ps_yields_no_children(self):
        # The graceful ps-error → [] behaviour holds in flat mode too.
        self.assertEqual(self._children(None, tree=False, ps_out=''), [])


class TestToggleTreeAction(unittest.TestCase):
    """The ``t`` action flips ``_TREE_MODE``, flashes, refreshes, re-homes."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._TREE_MODE = True
        # A get_children that reads the live _TREE_MODE so a refresh actually
        # rebuilds the list per the new mode (and the same pid survives the
        # switch, so cursor restore has a target to land on).
        def _kids(_id, *, reload=False):
            if not self.r._TREE_MODE and _id is not None:
                return []
            item = Item(id=42, title='proc', has_children=False)
            return [item]
        self.b = make_browser(get_children=_kids)
        self.b.refresh()
        self.b.run_until_idle()
        self.b.cursor_to(42)
        self.b.run_until_idle()
        self.ctx = Context(self.b)

    def tearDown(self):
        self.b.stop_workers()

    def test_flips_mode_and_flashes(self):
        self.assertTrue(self.r._TREE_MODE)
        self.r._action_toggle_tree(self.ctx)
        self.b.run_until_idle()
        self.assertFalse(self.r._TREE_MODE)
        self.assertEqual(self.b._notice.text, 'view: flat')
        # And back again.
        self.r._action_toggle_tree(self.ctx)
        self.b.run_until_idle()
        self.assertTrue(self.r._TREE_MODE)
        self.assertEqual(self.b._notice.text, 'view: tree')

    def test_cursor_restored_on_same_pid_after_refresh(self):
        # The cursor sits on pid 42 before the toggle; after the refresh +
        # chained cursor_to it must still sit on pid 42.
        self.assertEqual(self.ctx.cursor.id, 42)
        self.r._action_toggle_tree(self.ctx)
        self.b.run_until_idle()
        self.assertEqual(self.ctx.cursor.id, 42)

    def test_refresh_is_invoked(self):
        # The action must refresh() so the new mode's children are refetched.
        with mock.patch.object(self.ctx, 'refresh',
                               wraps=self.ctx.refresh) as spy:
            self.r._action_toggle_tree(self.ctx)
            self.b.run_until_idle()
        spy.assert_called_once()

    def test_no_cursor_still_flips_and_refreshes(self):
        # An empty list (no cursor) must not crash the toggle: it flips,
        # flashes and refreshes, just without a cursor restore.
        empty = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            empty.refresh()
            empty.run_until_idle()
            ctx = Context(empty)
            self.assertIsNone(ctx.cursor)
            self.r._action_toggle_tree(ctx)
            empty.run_until_idle()
            self.assertFalse(self.r._TREE_MODE)
            self.assertEqual(empty._notice.text, 'view: flat')
        finally:
            empty.stop_workers()


class TestViewFlagsCli(unittest.TestCase):
    """``--tree`` / ``--no-tree`` set the initial mode (parsed via recipe_argv)."""

    def setUp(self):
        self.r = _load_recipe()

    def _apply(self, argv):
        self.r._TREE_MODE = True
        # ``_apply_view_flags`` reads the genuine ``recipe_argv()``, which
        # sources from the framework module's ``sys.argv`` — patch that.
        with mock.patch.object(_FW_STATE.sys, 'argv', ['browse-ps', *argv]):
            self.r._apply_view_flags()
        return self.r._TREE_MODE

    def test_default_is_tree(self):
        self.assertTrue(self._apply([]))

    def test_no_tree_selects_flat(self):
        self.assertFalse(self._apply(['--no-tree']))

    def test_tree_selects_tree(self):
        # Starting from flat, --tree restores tree mode.
        self.r._TREE_MODE = False
        with mock.patch.object(_FW_STATE.sys, 'argv', ['browse-ps', '--tree']):
            self.r._apply_view_flags()
        self.assertTrue(self.r._TREE_MODE)

    def test_tree_wins_when_both_given(self):
        self.assertTrue(self._apply(['--no-tree', '--tree']))

    def test_framework_tty_flag_not_misread(self):
        # recipe_argv strips --tty + its value; --no-tree after it still wins
        # (and the device path is never mistaken for a positional / flag).
        self.assertFalse(self._apply(['--tty', '/dev/pts/3', '--no-tree']))


# --- Sort modes (B5) --------------------------------------------------------

# Canned table whose pid / cpu% / mem / cpu_secs / user orderings are all
# DISTINCT, so each sort key produces a different sequence. Every row is a
# direct child of pid 1 (so the SAME set is sorted in both flat mode — the whole
# list — and tree mode — pid 1's children). pids 100/200/300 carry deliberately
# UN-pid-ordered metrics; pcpu doubles as cpu% here because there is no prior
# snapshot (the lifetime-average fallback), so cpu% == round(pcpu).
#
#   pid  ppid user   pcpu  rss  time      → cpu%  mem(KiB) cpu_secs
#   1    0    root    0.0  100  00:00:00       0     100        0
#   100  1    Bob    10.0  300  00:00:30      10     300       30
#   200  1    alice  30.0  100  00:02:00      30     100      120
#   300  1    carol   5.0  500  00:00:10       5     500       10
_PS_SORT = (
    '    1     0 root   0.0  100 00:00:00 /sbin/init\n'
    '  100     1 Bob   10.0  300 00:00:30 b\n'
    '  200     1 alice 30.0  100 00:02:00 a\n'
    '  300     1 carol  5.0  500 00:00:10 c\n'
)

# A pid-tie table: two rows share the SAME primary metric (mem 200 KiB) so the
# pid tie-break decides their relative order. pids intentionally descending in
# file order to prove the tie-break sorts them ascending.
_PS_TIE = (
    '    1     0 root  0.0  100 00:00:00 /sbin/init\n'
    '  300     1 root  0.0  200 00:00:00 c\n'
    '  200     1 root  0.0  200 00:00:00 b\n'
)


class TestSortProcs(_DataLayerBase):
    """``_sort_procs`` / ``get_children`` order by the active key (B5).

    The sort state is module-global, so reset it to the seed defaults after each
    test (the per-test ``_load_recipe`` already gives a fresh module, but be
    explicit per the ticket).
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore_sort)
        self._seed_key = self.r._SORT_KEY
        self._seed_dir = dict(self.r._SORT_DIR)

    def _restore_sort(self):
        self.r._SORT_KEY = self._seed_key
        self.r._SORT_DIR = dict(self._seed_dir)

    def _flat(self, ps_out=_PS_SORT):
        # Flat mode root → the whole list, ordered by the active sort key.
        self.r._TREE_MODE = False
        with mock.patch.object(self.r.subprocess, 'run', _ps_completed(ps_out)), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None), \
             mock.patch.object(self.r, '_proc_cpu_secs', lambda pid: None):
            return [k.id for k in self.r.get_children(None, reload=True)]

    def _tree_children(self, ps_out=_PS_SORT):
        # Tree mode: pid 1's direct children, ordered by the active sort key.
        self.r._TREE_MODE = True
        with mock.patch.object(self.r.subprocess, 'run', _ps_completed(ps_out)), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None), \
             mock.patch.object(self.r, '_proc_cpu_secs', lambda pid: None):
            self.r.get_children(None, reload=True)          # samples the snapshot
            return [k.id for k in self.r.get_children(1, reload=False)]

    def test_defaults_are_seeded(self):
        # The module seeds _SORT_KEY=pid and the spec per-key directions.
        self.assertEqual(self.r._SORT_KEY, 'pid')
        self.assertEqual(self.r._SORT_DIR, {
            'pid': False, 'cpu_pct': True, 'mem_bytes': True,
            'cpu_secs': True, 'user': False,
        })

    def test_pid_ascending(self):
        self.r._SORT_KEY = 'pid'
        self.assertEqual(self._flat(), [1, 100, 200, 300])

    def test_cpu_descending(self):
        # cpu% desc: 200(30) > 100(10) > 300(5) > 1(0).
        self.r._SORT_KEY = 'cpu_pct'
        self.assertEqual(self._flat(), [200, 100, 300, 1])

    def test_mem_descending(self):
        # mem desc: 300(500K) > 100(300K) > {1,200 both 100K → pid asc} → 1,200.
        self.r._SORT_KEY = 'mem_bytes'
        self.assertEqual(self._flat(), [300, 100, 1, 200])

    def test_cpu_secs_descending(self):
        # cpu_secs desc: 200(120) > 100(30) > 300(10) > 1(0).
        self.r._SORT_KEY = 'cpu_secs'
        self.assertEqual(self._flat(), [200, 100, 300, 1])

    def test_user_ascending_case_insensitive(self):
        # user asc, case-insensitive: alice(200) < Bob(100) < carol(300) < root(1).
        # If it were case-SENSITIVE, capital 'Bob' would sort before 'alice'.
        self.r._SORT_KEY = 'user'
        self.assertEqual(self._flat(), [200, 100, 300, 1])

    def test_reverse_direction_flips_order(self):
        # The same key with the opposite direction reverses the sequence
        # (modulo the always-ascending pid tie-break, not exercised here since
        # cpu_secs values are distinct). cpu_secs is a stored attribute (unlike
        # cpu%, which would become a zero delta on a second identical sample),
        # so each fresh sample yields the same ordering.
        self.r._SORT_KEY = 'cpu_secs'
        self.r._SORT_DIR['cpu_secs'] = True               # descending
        self.assertEqual(self._flat(), [200, 100, 300, 1])   # 120>30>10>0
        self.r._SORT_DIR['cpu_secs'] = False              # ascending
        self.assertEqual(self._flat(), [1, 300, 100, 200])   # 0<10<30<120

    def test_pid_tiebreak_is_ascending_regardless_of_direction(self):
        # Two rows tie on the primary metric (mem 200K). The pid tie-break is
        # ascending (200 before 300) in BOTH directions — only the primary key
        # honours the direction, so the tie-break stays deterministic.
        self.r._SORT_KEY = 'mem_bytes'
        self.r._SORT_DIR['mem_bytes'] = True              # descending
        self.assertEqual(self._flat(_PS_TIE), [200, 300, 1])
        self.r._SORT_DIR['mem_bytes'] = False             # ascending
        self.assertEqual(self._flat(_PS_TIE), [1, 200, 300])

    def test_sort_applies_in_tree_scope(self):
        # The SAME ordering applies to a parent's children in tree mode.
        self.r._SORT_KEY = 'cpu_pct'
        self.assertEqual(self._tree_children(), [200, 100, 300])
        self.r._SORT_KEY = 'mem_bytes'
        self.assertEqual(self._tree_children(), [300, 100, 200])
        self.r._SORT_KEY = 'pid'
        self.assertEqual(self._tree_children(), [100, 200, 300])


class TestSortActions(unittest.TestCase):
    """The sort actions flip state, flash the key+arrow, and refresh (B5).

    Uses a real headless Browser/Context (flash lands on ``b._notice``, refresh
    re-runs get_children) — the same harness the ``t`` toggle test uses.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.addCleanup(self._restore_sort)
        self._seed_key = self.r._SORT_KEY
        self._seed_dir = dict(self.r._SORT_DIR)
        self.b = make_browser(
            get_children=lambda _id, *, reload=False:
                [Item(id=1, title='proc', has_children=False)])
        self.b.refresh()
        self.b.run_until_idle()
        self.ctx = Context(self.b)

    def tearDown(self):
        self.b.stop_workers()

    def _restore_sort(self):
        self.r._SORT_KEY = self._seed_key
        self.r._SORT_DIR = dict(self._seed_dir)

    def test_switching_key_sets_state_and_flashes_remembered_dir(self):
        # P → cpu_pct, its remembered (default) direction is descending → ↓.
        self.r._sort_action('cpu_pct')(self.ctx)
        self.b.run_until_idle()
        self.assertEqual(self.r._SORT_KEY, 'cpu_pct')
        self.assertEqual(self.b._notice.text, 'sort: cpu% ↓')

    def test_active_key_repress_reverses_direction(self):
        # cpu_pct starts descending; activating it then re-pressing flips to asc.
        self.r._sort_action('cpu_pct')(self.ctx)
        self.b.run_until_idle()
        self.assertTrue(self.r._SORT_DIR['cpu_pct'])           # descending
        self.r._sort_action('cpu_pct')(self.ctx)              # re-press
        self.b.run_until_idle()
        self.assertFalse(self.r._SORT_DIR['cpu_pct'])          # now ascending
        self.assertEqual(self.b._notice.text, 'sort: cpu% ↑')

    def test_switching_away_does_not_reverse_inactive_key(self):
        # Activate cpu_pct, reverse it (→ asc), switch to mem, then back to
        # cpu_pct: it must restore the ascending direction it was LEFT at, not
        # reset to its default descending.
        a = self.r._sort_action
        a('cpu_pct')(self.ctx)                                 # desc (default)
        a('cpu_pct')(self.ctx)                                 # → asc (reversed)
        a('mem_bytes')(self.ctx)                               # switch away
        self.b.run_until_idle()
        self.assertEqual(self.r._SORT_KEY, 'mem_bytes')
        a('cpu_pct')(self.ctx)                                 # switch back
        self.b.run_until_idle()
        # Switching back is NOT a re-press → no reverse; remembered = ascending.
        self.assertFalse(self.r._SORT_DIR['cpu_pct'])
        self.assertEqual(self.b._notice.text, 'sort: cpu% ↑')

    def test_pid_action_ascending_arrow(self):
        # N → pid. pid is the DEFAULT active key, so switch away first; pressing
        # N then switches back to pid at its remembered ascending direction → ↑.
        self.r._sort_action('cpu_pct')(self.ctx)
        self.r._sort_action('pid')(self.ctx)
        self.b.run_until_idle()
        self.assertEqual(self.r._SORT_KEY, 'pid')
        self.assertEqual(self.b._notice.text, 'sort: pid ↑')

    def test_each_action_refreshes(self):
        # Every sort action must refresh() so get_children re-sorts.
        for key in ('pid', 'cpu_pct', 'mem_bytes', 'cpu_secs', 'user'):
            with self.subTest(key=key), \
                 mock.patch.object(self.ctx, 'refresh',
                                   wraps=self.ctx.refresh) as spy:
                self.r._sort_action(key)(self.ctx)
                self.b.run_until_idle()
                spy.assert_called_once()


# --- Gutter columns (B3) ----------------------------------------------------

class _FakeCtx:
    """A ``RowContext`` stand-in for the gutter/chrome render hooks.

    ``max_col_width_global(field)`` returns a fixed width per field (the GLOBAL
    column width the recipe sizes its gutter to); ``kind`` / ``depth`` /
    ``selected`` / ``expanded`` feed the framework chrome atoms the recipe
    composes (``ps_chrome``).
    """

    def __init__(self, widths, *, depth=0, selected=False, expanded=False,
                 kind='item'):
        self._widths = widths
        self.calls = []
        self.depth = depth
        self.selected = selected
        self.expanded = expanded
        self.kind = kind

    def max_col_width_global(self, field):
        self.calls.append(field)
        return self._widths[field]


def _ps_item(r, pid, user, cpu, mem, *, title='proc', has_children=False):
    """Build a recipe ``Item`` carrying the four gutter columns."""
    item = r.Item(id=pid, title=title, has_children=has_children)
    item.col_pid = str(pid)
    item.col_user = user
    item.col_cpu = cpu
    item.col_mem = mem
    return item


# Global column widths wide enough that every sample value pads (so the
# justification is actually exercised): pid 5, user 8, cpu 4, mem 5.
_GW = {'col_pid': 5, 'col_user': 8, 'col_cpu': 4, 'col_mem': 5}


class TestPsGutterSegments(unittest.TestCase):
    """``ps_gutter_segments`` builds the dim pid·user·cpu%·mem gutter columns."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()
        cls.dim = cls.r.style('dim')          # the real (fg, bold) dim pair

    def test_four_dim_columns_justified_and_spaced(self):
        ctx = _FakeCtx(_GW)
        item = _ps_item(self.r, 1, 'root', '10%', '100M')
        segs = self.r.ps_gutter_segments(item, ctx)

        self.assertEqual(len(segs), 4)
        dfg, dbold = self.dim
        # pid / cpu% / mem RIGHT-justified, user LEFT-justified, each + ' '.
        self.assertEqual(segs[0], ('    1' + ' ', dfg, dbold))   # rjust 5
        self.assertEqual(segs[1], ('root    ' + ' ', dfg, dbold))  # ljust 8
        self.assertEqual(segs[2], (' 10%' + ' ', dfg, dbold))    # rjust 4
        self.assertEqual(segs[3], (' 100M' + ' ', dfg, dbold))   # rjust 5
        # Widths came from the GLOBAL measurement for each column field.
        self.assertEqual(ctx.calls,
                         ['col_pid', 'col_user', 'col_cpu', 'col_mem'])

    def test_columns_align_across_rows(self):
        # Two rows with differing raw values must, once padded to the global
        # column width, yield equal per-column segment widths — the point of
        # max_col_width_global-driven gutter alignment. The global widths are
        # the max over both rows (pid 5, user 13, cpu 4, mem 6), exactly as the
        # framework's max_col_width_global would report for this loaded set.
        gw = {'col_pid': 5, 'col_user': 13, 'col_cpu': 4, 'col_mem': 6}
        a = _ps_item(self.r, 1, 'root', '0%', '1M')
        b = _ps_item(self.r, 32109, 'averylonguser', '100%', '12345M')
        segs_a = self.r.ps_gutter_segments(a, _FakeCtx(gw))
        segs_b = self.r.ps_gutter_segments(b, _FakeCtx(gw))
        for col in range(4):
            self.assertEqual(len(segs_a[col][0]), len(segs_b[col][0]),
                             f'gutter column {col} widths differ between rows')

    def test_missing_columns_yield_empty_gutter(self):
        # A synthetic/edge row lacking col_* emits no gutter (so the default
        # content still renders); no width measurement happens.
        ctx = _FakeCtx(_GW)
        bare = self.r.Item(id=0, title='synthetic', has_children=False)
        self.assertEqual(self.r.ps_gutter_segments(bare, ctx), [])
        self.assertEqual(ctx.calls, [])


class _ModeCtx:
    """A minimal ``ctx`` for the display-mode switch actions.

    Records ``flash`` text, ``redraw`` panes, and any ``refresh`` calls —
    the side-effects ``_set_display_mode`` performs besides flipping the
    module global. A mode switch must REPAINT (``redraw``) the loaded
    rows, never refetch the snapshot (``refresh``).
    """

    def __init__(self):
        self.flashes = []
        self.redraws = []
        self.refreshed = 0

    def flash(self, text, log=False):
        self.flashes.append(text)

    def redraw(self, panes='all'):
        self.redraws.append(panes)

    def refresh(self):
        self.refreshed += 1


class TestDisplayModes(unittest.TestCase):
    """Display modes (number keys 1/2/3) gate the gutter column set (#1119).

    ``_DISPLAY_MODE`` selects which leading SUBSET of the pid·user·cpu%·mem
    columns ``ps_gutter_segments`` emits: mode 1 = empty gutter (command line
    only), mode 2 = pid only, mode 3 = pid·user·cpu%·mem (the default). The
    ``col_*`` strings are always on every item; the mode only chooses how many
    the gutter renders, so a switch is a pure re-render. Each test drives
    ``_DISPLAY_MODE`` directly and restores it via ``addCleanup`` (it is a
    module global).
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()
        cls.dim = cls.r.style('dim')          # the real (fg, bold) dim pair

    def setUp(self):
        self._saved_mode = self.r._DISPLAY_MODE
        self.addCleanup(setattr, self.r, '_DISPLAY_MODE', self._saved_mode)
        # _CPU_AVAILABLE is a load-time probe (True on Linux/this box); pin it so
        # the per-mode assertions are platform-independent.
        self._saved_cpu = self.r._CPU_AVAILABLE
        self.addCleanup(setattr, self.r, '_CPU_AVAILABLE', self._saved_cpu)

    # -- the per-mode gutter column set ------------------------------------

    def test_mode_1_is_empty_gutter(self):
        # Mode 1 = command line only → no gutter columns and no width
        # measurement (the row is just selection + indent + expander + cmdline).
        self.r._DISPLAY_MODE = 1
        ctx = _FakeCtx(_GW)
        item = _ps_item(self.r, 1, 'root', '10%', '100M')
        self.assertEqual(self.r.ps_gutter_segments(item, ctx), [])
        self.assertEqual(ctx.calls, [])
        # And the whole chrome is just selection + indent + expander = 3.
        self.assertEqual(len(self.r.ps_chrome(item, _FakeCtx(_GW))), 3)

    def test_mode_2_is_pid_only(self):
        # Mode 2 = pid only: one right-justified, dim, space-trailed column.
        self.r._DISPLAY_MODE = 2
        ctx = _FakeCtx(_GW)
        item = _ps_item(self.r, 1, 'root', '10%', '100M')
        segs = self.r.ps_gutter_segments(item, ctx)
        dfg, dbold = self.dim
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0], ('    1' + ' ', dfg, dbold))   # rjust 5
        self.assertEqual(ctx.calls, ['col_pid'])

    def test_mode_3_is_full_set(self):
        # Mode 3 = pid · user · cpu% · mem (default) when a fine CPU source
        # exists: the full four columns (pid/cpu/mem rjust, user ljust).
        self.r._DISPLAY_MODE = 3
        self.r._CPU_AVAILABLE = True
        ctx = _FakeCtx(_GW)
        item = _ps_item(self.r, 1, 'root', '10%', '100M')
        segs = self.r.ps_gutter_segments(item, ctx)
        dfg, dbold = self.dim
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[0], ('    1' + ' ', dfg, dbold))     # rjust 5
        self.assertEqual(segs[1], ('root    ' + ' ', dfg, dbold))  # ljust 8
        self.assertEqual(segs[2], (' 10%' + ' ', dfg, dbold))      # rjust 4
        self.assertEqual(segs[3], (' 100M' + ' ', dfg, dbold))     # rjust 5
        self.assertEqual(ctx.calls,
                         ['col_pid', 'col_user', 'col_cpu', 'col_mem'])

    def test_mode_3_drops_cpu_when_no_fine_source(self):
        # No /proc (macOS &c.): the instantaneous cpu% can't be computed, so the
        # cpu% column is dropped from mode 3 → pid · user · mem (#1124). The
        # other columns and their order are unchanged.
        self.r._DISPLAY_MODE = 3
        self.r._CPU_AVAILABLE = False
        ctx = _FakeCtx(_GW)
        item = _ps_item(self.r, 1, 'root', '10%', '100M')
        segs = self.r.ps_gutter_segments(item, ctx)
        dfg, dbold = self.dim
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0], ('    1' + ' ', dfg, dbold))     # pid  rjust 5
        self.assertEqual(segs[1], ('root    ' + ' ', dfg, dbold))  # user ljust 8
        self.assertEqual(segs[2], (' 100M' + ' ', dfg, dbold))     # mem  rjust 5
        self.assertEqual(ctx.calls, ['col_pid', 'col_user', 'col_mem'])  # no cpu

    def test_modes_2_and_3_share_the_pid_column(self):
        # Mode 2's gutter is exactly mode 3's leading prefix — same order, a
        # strict subset (no reordering, no chrome change).
        item = _ps_item(self.r, 1, 'root', '10%', '100M')
        self.r._DISPLAY_MODE = 3
        full = self.r.ps_gutter_segments(item, _FakeCtx(_GW))
        self.r._DISPLAY_MODE = 2
        pid_only = self.r.ps_gutter_segments(item, _FakeCtx(_GW))
        self.assertEqual(pid_only, full[:1])

    # -- the mode-switch actions -------------------------------------------

    def test_set_display_mode_flips_global_flashes_and_repaints(self):
        # Each action sets _DISPLAY_MODE, flashes the mode, and REPAINTS the
        # row panes (list + children) — a pure re-render, no refetch (#1119).
        for mode in (1, 2, 3):
            ctx = _ModeCtx()
            self.r._set_display_mode(ctx, mode)
            self.assertEqual(self.r._DISPLAY_MODE, mode)
            self.assertEqual(ctx.redraws, [['list', 'children']])
            self.assertEqual(ctx.refreshed, 0)   # repaint, never refetch
            self.assertEqual(len(ctx.flashes), 1)
            # The flash names the active mode's column set.
            self.assertIn(self.r._MODE_LABELS[mode], ctx.flashes[0])

    def test_default_display_mode_is_3(self):
        # Freshly loaded, the recipe defaults to mode 3 (the full gutter).
        fresh = _load_recipe()
        self.assertEqual(fresh._DISPLAY_MODE, 3)


class TestPsChrome(unittest.TestCase):
    """``ps_chrome`` puts the gutter between the selection marker and indent."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    @staticmethod
    def _text(segs):
        return ''.join(text for text, _fg, _bold in segs)

    def test_order_selection_gutter_indent_expander(self):
        # selection · pid · user · cpu% · mem · indent · expander; the NAME is
        # NOT here (default content renders the flexible last column).
        ctx = _FakeCtx(_GW, depth=0, expanded=True)
        item = _ps_item(self.r, 1, 'root', '10%', '100M', has_children=True)
        segs = self.r.ps_chrome(item, ctx)
        # 1 selection + 4 gutter + 1 indent + 1 expander = 7 segments.
        self.assertEqual(len(segs), 7)
        text = self._text(segs)
        # The gutter sits immediately after the 2-cell selection marker and
        # before the expander glyph; the title never appears in chrome.
        self.assertEqual(text, '      1 root      10%  100M ▼ ')
        self.assertNotIn('proc', text)

    def test_gutter_left_of_indent_regardless_of_depth(self):
        # A deep row's gutter occupies the SAME leading cells as a shallow
        # row's (the gutter is left of the indent, so depth pushes only the
        # indent/expander/name rightward — not the columns).
        shallow = self.r.ps_chrome(
            _ps_item(self.r, 1, 'root', '1%', '1M', has_children=True),
            _FakeCtx(_GW, depth=0, expanded=True))
        deep = self.r.ps_chrome(
            _ps_item(self.r, 999, 'bob', '5%', '9M'),
            _FakeCtx(_GW, depth=3))

        # The selection marker + four gutter segments (indices 0..4) are the
        # leading chrome; their combined width is identical across depths, so
        # the pid column's right edge aligns regardless of tree depth.
        lead = lambda segs: ''.join(t for t, _f, _b in segs[:5])
        self.assertEqual(len(lead(shallow)), len(lead(deep)))
        # And the indent segment (index 5) is what grows with depth.
        self.assertEqual(deep[5][0], '  ' * 3)
        self.assertEqual(shallow[5][0], '')

    def test_synthetic_row_has_empty_gutter(self):
        # No col_* → empty gutter, but the structural chrome still composes
        # (selection + indent + expander), so default content can render.
        ctx = _FakeCtx(_GW, depth=1)
        bare = self.r.Item(id=0, title='synthetic', has_children=False)
        segs = self.r.ps_chrome(bare, ctx)
        # selection + (no gutter) + indent + expander = 3 segments.
        self.assertEqual(len(segs), 3)
        self.assertEqual(self._text(segs), '    ' + '  ')  # sel + 1-level indent


# --- Background updater (B8) -------------------------------------------------

class TestUpdateIntervalCli(unittest.TestCase):
    """``-d <seconds>`` parsing (B8) via the genuine ``recipe_argv``.

    Mirrors ``TestViewFlagsCli``: ``_update_interval`` reads ``recipe_argv()``,
    which sources the framework module's ``sys.argv`` — so the cases drive it by
    patching ``_FW_STATE.sys.argv``.
    """

    def setUp(self):
        self.r = _load_recipe()

    def _interval(self, argv):
        with mock.patch.object(_FW_STATE.sys, 'argv', ['browse-ps', *argv]):
            return self.r._update_interval()

    def test_default_is_four_seconds(self):
        # Absent -d → auto-update on at the 4.0 s default.
        self.assertEqual(self._interval([]), 4.0)
        self.assertEqual(self.r._DEFAULT_UPDATE_INTERVAL, 4.0)

    def test_fractional_value(self):
        self.assertEqual(self._interval(['-d', '2.5']), 2.5)

    def test_equals_form(self):
        # ``-d=2.5`` (single token) parses the same as ``-d 2.5``.
        self.assertEqual(self._interval(['-d=2.5']), 2.5)

    def test_integer_value(self):
        self.assertEqual(self._interval(['-d', '10']), 10.0)

    def test_zero_disables(self):
        # <= 0 disables updates (interval not > 0 → no thread started).
        self.assertEqual(self._interval(['-d', '0']), 0.0)

    def test_negative_disables(self):
        self.assertEqual(self._interval(['-d', '-1']), -1.0)

    def test_malformed_falls_back_to_default(self):
        # A non-numeric value degrades to the default rather than aborting.
        self.assertEqual(self._interval(['-d', 'nope']), 4.0)

    def test_bare_trailing_flag_falls_back_to_default(self):
        # ``-d`` with no following token → no value → default.
        self.assertEqual(self._interval(['-d']), 4.0)

    def test_last_occurrence_wins(self):
        # A repeated -d takes the last value (consistent with the scan order).
        self.assertEqual(self._interval(['-d', '1', '-d', '3']), 3.0)

    def test_framework_tty_flag_not_misread(self):
        # recipe_argv strips --tty + its value; -d after it still parses.
        self.assertEqual(self._interval(['--tty', '/dev/pts/3', '-d', '2']), 2.0)


class TestUpdateWorkerTick(unittest.TestCase):
    """A worker tick fetches off-thread + ``b.post``s a callback; NEVER refreshes.

    The incremental updater (#1121) replaced the old full ``b.refresh()`` tick:
    each tick fetches a snapshot via ``_fetch_procs`` (the slow ``ps`` half, here
    stubbed so no real subprocess runs) then ``b.post``s ``_apply_tick`` — the
    rotate/diff/``update_data`` happens on the UI thread. We assert the tick
    NEVER calls ``b.refresh()`` (the no-flicker contract) and that the posted
    callable is the marshalled apply. Uses a real ``threading.Event`` for the
    stop flag and a Mock Browser; module globals restored via ``addCleanup``. No
    test sleeps for the real default interval.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._seed_stop = self.r._UPDATE_STOP
        self._seed_thread = self.r._UPDATE_THREAD
        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        self.r._UPDATE_STOP = self._seed_stop
        self.r._UPDATE_THREAD = self._seed_thread

    def test_tick_posts_callback_and_never_refreshes(self):
        # Drive the worker directly with a tiny interval: the FIRST wait()
        # returns False (timeout) → one tick; the ``_fetch_procs`` stub sets the
        # stop event so the SECOND wait() exits at once. The tick must ``b.post``
        # (the marshalled apply) and must NOT call ``b.refresh()``.
        b = mock.Mock()
        self.r._UPDATE_STOP = threading.Event()
        fetched = {1: object()}

        def _fetch():
            self.r._UPDATE_STOP.set()      # stop after the first tick
            return fetched
        with mock.patch.object(self.r, '_fetch_procs', _fetch):
            self.r._update_worker(b, 0.001)
        b.refresh.assert_not_called()      # no full teardown — no flicker
        b.post.assert_called_once()

    def test_posted_callback_runs_apply_tick_with_the_fetched_procs(self):
        # The posted callable marshals ``_apply_tick(b, <fetched procs>)`` onto
        # the UI thread — calling it directly (as a test can) must invoke
        # ``_apply_tick`` with exactly the map ``_fetch_procs`` returned.
        b = mock.Mock()
        self.r._UPDATE_STOP = threading.Event()
        fetched = {1: object(), 2: object()}

        def _fetch():
            self.r._UPDATE_STOP.set()
            return fetched
        seen = []
        with mock.patch.object(self.r, '_fetch_procs', _fetch), \
             mock.patch.object(self.r, '_apply_tick',
                               lambda br, procs: seen.append((br, procs))):
            self.r._update_worker(b, 0.001)
            posted = b.post.call_args.args[0]
            posted()                        # run the marshalled callback
        self.assertEqual(seen, [(b, fetched)])

    def test_set_event_before_first_tick_yields_no_work(self):
        # A stop event set before the loop runs → wait() returns True
        # immediately → zero fetches/posts (prompt shutdown, no spurious tick).
        b = mock.Mock()
        self.r._UPDATE_STOP = threading.Event()
        self.r._UPDATE_STOP.set()
        with mock.patch.object(self.r, '_fetch_procs',
                               side_effect=AssertionError('fetched on stop')):
            self.r._update_worker(b, 0.001)
        b.post.assert_not_called()
        b.refresh.assert_not_called()

    def test_fetch_exception_does_not_kill_worker(self):
        # A transient fetch failure is swallowed; the loop survives to the next
        # wait() (which we trip via the stop event to end the test). Nothing is
        # posted for the failed tick.
        b = mock.Mock()
        self.r._UPDATE_STOP = threading.Event()
        calls = []

        def _fetch():
            calls.append(1)
            self.r._UPDATE_STOP.set()      # end after this (raising) tick
            raise RuntimeError('boom')
        with mock.patch.object(self.r, '_fetch_procs', _fetch):
            self.r._update_worker(b, 0.001)  # must NOT propagate
        self.assertEqual(len(calls), 1)
        b.post.assert_not_called()


class TestUpdateThreadLifecycle(unittest.TestCase):
    """``main`` starts the daemon thread when interval > 0 and joins it promptly.

    Patches ``Browser`` to a Mock and ``sys.exit`` to capture the return code,
    so ``main`` runs without a real terminal. ``b.run`` blocks on a one-shot
    event until the test releases it; the worker thread is asserted live during
    that window, then ``main``'s ``finally`` join must return promptly. Module
    globals restored via ``addCleanup``; no test sleeps for the default interval.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._seed_stop = self.r._UPDATE_STOP
        self._seed_thread = self.r._UPDATE_THREAD
        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        self.r._UPDATE_STOP = self._seed_stop
        self.r._UPDATE_THREAD = self._seed_thread

    def _run_main(self, argv):
        """Run ``main`` with a Mock Browser whose ``run`` returns 0 at once.

        Patches ``Browser`` / ``BrowserConfig`` / ``_apply_view_flags`` so
        ``main`` runs headless, and ``sys.exit`` to capture the return code. The
        real worker thread starts here, so ``_fetch_procs`` is stubbed to ``{}``
        — the lifecycle tests are about thread start/join, not the tick, and the
        stub keeps a real ``ps`` from running in a tight loop. Returns
        ``(browser_mock, exit_spy)``.
        """
        b = mock.Mock()
        b.run.return_value = 0
        with mock.patch.object(self.r, 'Browser', return_value=b), \
             mock.patch.object(self.r, 'BrowserConfig', return_value=object()), \
             mock.patch.object(self.r, '_apply_view_flags', lambda: None), \
             mock.patch.object(self.r, '_fetch_procs', lambda: {}), \
             mock.patch.object(_FW_STATE.sys, 'argv', ['browse-ps', *argv]), \
             mock.patch.object(self.r.sys, 'exit') as exit_spy:
            self.r.main()
        return b, exit_spy

    def test_disabled_when_interval_not_positive(self):
        # -d 0 → no thread started; main still runs and exits with run()'s rc.
        b, exit_spy = self._run_main(['-d', '0'])
        self.assertIsNone(self.r._UPDATE_THREAD)
        b.run.assert_called_once_with()
        exit_spy.assert_called_once_with(0)

    def test_thread_started_and_joined_for_positive_interval(self):
        # interval > 0 → a daemon thread starts; main's finally sets the stop
        # event and joins it. run() returns at once here (no real-interval
        # wait), so the worker — parked on its first stop_event.wait(interval) —
        # is woken by the finally's set() and the join completes promptly.
        b, exit_spy = self._run_main(['-d', '0.001'])
        self.assertIsNotNone(self.r._UPDATE_THREAD)
        self.assertTrue(self.r._UPDATE_STOP.is_set())     # finally set it
        self.assertFalse(self.r._UPDATE_THREAD.is_alive())  # join completed
        self.assertTrue(self.r._UPDATE_THREAD.daemon)
        exit_spy.assert_called_once_with(0)

    def test_thread_is_alive_during_run_then_dies(self):
        # Hold run() open on an event, confirm the worker thread is alive, then
        # release run() and let main's finally join it. No real-interval sleep:
        # the worker waits on the stop event, which main sets in finally.
        release = threading.Event()
        captured = {}

        def _blocking_run():
            captured['thread'] = self.r._UPDATE_THREAD
            captured['alive_during_run'] = self.r._UPDATE_THREAD.is_alive()
            release.wait(timeout=5.0)
            return 0

        b = mock.Mock()
        b.run.side_effect = _blocking_run
        done = threading.Event()

        def _main():
            with mock.patch.object(self.r, 'Browser', return_value=b), \
                 mock.patch.object(self.r, 'BrowserConfig',
                                   return_value=object()), \
                 mock.patch.object(self.r, '_apply_view_flags', lambda: None), \
                 mock.patch.object(self.r, '_fetch_procs', lambda: {}), \
                 mock.patch.object(_FW_STATE.sys, 'argv',
                                   ['browse-ps', '-d', '0.001']), \
                 mock.patch.object(self.r.sys, 'exit'):
                self.r.main()
            done.set()

        driver = threading.Thread(target=_main, daemon=True)
        driver.start()
        try:
            # Wait until run() has captured the worker state (it runs once main
            # has started the thread); a generous timeout, not a fixed sleep.
            deadline = time.monotonic() + 5.0
            while 'thread' not in captured and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertIn('thread', captured)
            self.assertTrue(captured['alive_during_run'])
        finally:
            release.set()          # let run() return → main's finally joins
        done.wait(timeout=5.0)
        self.assertTrue(done.is_set())                    # main returned
        self.assertFalse(captured['thread'].is_alive())   # worker joined


# --- New/finished highlighting (B9) -----------------------------------------

# A two-level tree: pid 1 → pid 100 (a parent) → pid 200 (a leaf), plus pid 300
# a second leaf directly under pid 1. Lets us prove leaf-death tombstones (200 /
# 300 are leaves) vs. a parent-death (100 has child 200 → NOT tombstoned).
_PS_HL = (
    '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
    '  100     1 root  1.0  200 00:00:02 parent\n'
    '  200   100 root  2.0  300 00:00:03 child\n'
    '  300     1 root  3.0  400 00:00:04 sibling\n'
)


class _HighlightBase(unittest.TestCase):
    """Shared setup for the highlight diff/display tests (B9).

    Drives a FAKE clock (``_now`` patched to read ``self.clock``) so retention
    is exercised WITHOUT sleeping. Resets the snapshot + highlight module globals
    and restores them via ``addCleanup`` per the ticket.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.clock = 1000.0
        # _now is the single clock the snapshot wall-time + highlight timers read.
        self._orig_now = self.r._now
        self.r._now = lambda: self.clock
        self.addCleanup(self._restore)
        # Explicit reset (a fresh module already defaults these, but be explicit).
        self.r._PREV_SNAPSHOT = None
        self.r._CUR_SNAPSHOT = None
        self.r._PS_USER_WIDTH_OK = None
        self.r._UID_NAMES = {}
        self.r._APPEARED_AT = {}
        self.r._TOMBSTONES = {}
        self.r._HIGHLIGHT_MODE = False

    def _restore(self):
        self.r._now = self._orig_now
        self.r._APPEARED_AT = {}
        self.r._TOMBSTONES = {}
        self.r._HIGHLIGHT_MODE = False

    def _resample(self, ps_out):
        """Take a fresh snapshot from canned ``ps`` output (reload=True).

        Mocks ``ps`` / platform / smaps so only the diff is under test; the diff
        runs inside ``_snapshot`` against the current ``self.clock``.
        """
        with mock.patch.object(self.r.subprocess, 'run', _ps_completed(ps_out)), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None), \
             mock.patch.object(self.r, '_proc_cpu_secs', lambda pid: None):
            return self.r._snapshot(reload=True)


class TestSnapshotDiff(_HighlightBase):
    """The always-on diff tracks appeared pids and finished-leaf tombstones."""

    def test_first_sample_marks_nothing(self):
        # No baseline → no pid is "new", nothing is tombstoned (else the whole
        # tree would glow on launch).
        self._resample(_PS_HL)
        self.assertEqual(self.r._APPEARED_AT, {})
        self.assertEqual(self.r._TOMBSTONES, {})

    def test_appeared_pid_recorded_with_time(self):
        # Second sample adds pid 400 (a new leaf under pid 1) → _APPEARED_AT[400].
        self._resample(_PS_HL)
        self.clock += 1.0
        self._resample(_PS_HL + '  400     1 root 0.0 100 00:00:00 newproc\n')
        self.assertIn(400, self.r._APPEARED_AT)
        self.assertEqual(self.r._APPEARED_AT[400], 1001.0)
        # Pre-existing pids are NOT (re)marked.
        for pid in (1, 100, 200, 300):
            self.assertNotIn(pid, self.r._APPEARED_AT)

    def test_existing_pid_keeps_original_appeared_time(self):
        # A pid appears, then survives a later resample: its timestamp is the
        # FIRST-seen time, not refreshed on each sample.
        self._resample(_PS_HL)
        self.clock += 1.0
        with_new = _PS_HL + '  400     1 root 0.0 100 00:00:00 newproc\n'
        self._resample(with_new)
        self.assertEqual(self.r._APPEARED_AT[400], 1001.0)
        self.clock += 1.0
        self._resample(with_new)                       # 400 still present
        self.assertEqual(self.r._APPEARED_AT[400], 1001.0)   # unchanged

    def test_finished_leaf_is_tombstoned_with_former_ppid(self):
        # Leaf pid 300 (child of pid 1) vanishes → tombstone with former_ppid=1.
        self._resample(_PS_HL)
        self.clock += 1.0
        gone_300 = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
            '  200   100 root  2.0  300 00:00:03 child\n'
        )
        self._resample(gone_300)
        self.assertIn(300, self.r._TOMBSTONES)
        info, former_ppid, vanished_at = self.r._TOMBSTONES[300]
        self.assertEqual(former_ppid, 1)
        self.assertEqual(vanished_at, 1001.0)
        self.assertEqual(info.pid, 300)
        self.assertEqual(info.args, 'sibling')          # saved ProcInfo columns

    def test_pid_with_children_is_not_tombstoned(self):
        # pid 100 HAD a child (200) when it died, so it is NOT tombstoned: its
        # row just vanishes (200 would reparent on the next build). Killing 100
        # AND 200 together leaves only 200 untombstoned (its parent 100 is gone)?
        # — here we kill ONLY 100's subtree-parent: drop 100 but keep 200 present
        # so 100 clearly "had children" in the previous snapshot.
        self._resample(_PS_HL)
        self.clock += 1.0
        gone_100 = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  200   100 root  2.0  300 00:00:03 child\n'   # orphan kept alive
            '  300     1 root  3.0  400 00:00:04 sibling\n'
        )
        self._resample(gone_100)
        self.assertNotIn(100, self.r._TOMBSTONES)       # had children → no tomb

    def test_respawned_pid_clears_then_regreens(self):
        # A leaf dies (tombstoned), then a pid with the same number reappears:
        # the tombstone-clearing pops its stale _APPEARED_AT, and the reappearance
        # records a fresh appeared-time.
        self._resample(_PS_HL)
        self.clock += 1.0
        # pid 300 dies.
        self._resample(
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
            '  200   100 root  2.0  300 00:00:03 child\n')
        self.assertIn(300, self.r._TOMBSTONES)
        self.clock += 1.0
        # pid 300 reappears (a recycled number).
        self._resample(_PS_HL)
        self.assertIn(300, self.r._APPEARED_AT)
        self.assertEqual(self.r._APPEARED_AT[300], 1002.0)
        # And its tombstone is gone (the parent is live + it reappeared).
        self.assertNotIn(300, self.r._TOMBSTONES)


class TestRetention(_HighlightBase):
    """The 3.5 s retention window prunes appeared/tombstone entries."""

    def test_appeared_within_window_survives(self):
        self._resample(_PS_HL)
        self.clock += 1.0
        self._resample(_PS_HL + '  400     1 root 0.0 100 00:00:00 newproc\n')
        # 2.0 s later (< 3.5) the green is still live.
        self.clock += 2.0
        self._resample(_PS_HL + '  400     1 root 0.0 100 00:00:00 newproc\n')
        self.assertIn(400, self.r._APPEARED_AT)

    def test_appeared_past_window_dropped(self):
        self._resample(_PS_HL)
        self.clock += 1.0
        self._resample(_PS_HL + '  400     1 root 0.0 100 00:00:00 newproc\n')
        # Jump well past 3.5 s and resample → the appeared entry expires.
        self.clock += 4.0
        self._resample(_PS_HL + '  400     1 root 0.0 100 00:00:00 newproc\n')
        self.assertNotIn(400, self.r._APPEARED_AT)

    def test_tombstone_within_window_survives_intervening_refresh(self):
        # A tombstone persists across an intervening resample within the window
        # (it must not blink away between auto-update ticks).
        self._resample(_PS_HL)
        self.clock += 1.0
        gone_300 = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
            '  200   100 root  2.0  300 00:00:03 child\n'
        )
        self._resample(gone_300)
        self.assertIn(300, self.r._TOMBSTONES)
        # 2.0 s later, another resample (300 still absent) keeps the tombstone.
        self.clock += 2.0
        self._resample(gone_300)
        self.assertIn(300, self.r._TOMBSTONES)

    def test_tombstone_past_window_dropped(self):
        self._resample(_PS_HL)
        self.clock += 1.0
        gone_300 = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
            '  200   100 root  2.0  300 00:00:03 child\n'
        )
        self._resample(gone_300)
        self.clock += 4.0                              # past 3.5 s
        self._resample(gone_300)
        self.assertNotIn(300, self.r._TOMBSTONES)

    def test_tombstone_dropped_when_parent_vanishes(self):
        # A tombstone whose FORMER PARENT is no longer live is dropped EARLY
        # (before the time window), since it has nowhere to render in tree mode.
        # pid 100 has TWO leaf children (200, 250) so it always "has children":
        # killing 200 tombstones it; later killing 100 (still parent of 250)
        # leaves 100 untombstoned (had children) yet drops 200's tombstone.
        base = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
            '  200   100 root  2.0  300 00:00:03 child\n'
            '  250   100 root  2.0  300 00:00:03 child2\n'
        )
        self._resample(base)
        self.clock += 1.0
        # pid 200 (leaf, child of 100) dies → tombstone with former_ppid=100.
        gone_200 = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
            '  250   100 root  2.0  300 00:00:03 child2\n'
        )
        self._resample(gone_200)
        self.assertIn(200, self.r._TOMBSTONES)
        self.assertEqual(self.r._TOMBSTONES[200][1], 100)   # former_ppid
        # Now the parent (pid 100) dies too — only 0.5 s later (well within 3.5).
        # It still had child 250 at death, so it is NOT tombstoned.
        self.clock += 0.5
        gone_100_too = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
        )
        self._resample(gone_100_too)
        # 200's tombstone is dropped early (former parent 100 no longer live);
        # 100 had a live child (250) → never tombstoned.
        self.assertNotIn(200, self.r._TOMBSTONES)
        self.assertNotIn(100, self.r._TOMBSTONES)


class TestHighlightDisplayGating(_HighlightBase):
    """``get_children`` colours/tombstones ONLY when highlight mode is on."""

    def _children(self, parent, reload=False):
        with mock.patch.object(self.r.subprocess, 'run',
                               _ps_completed(_PS_HL)), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None):
            return self.r.get_children(parent, reload=reload)

    def _arm_appeared_and_tombstone(self):
        """Set up state: pid 400 freshly appeared + pid 300 tombstoned (leaf)."""
        # Sample 1: baseline.
        self._resample(_PS_HL)
        self.clock += 1.0
        # Sample 2: pid 300 dies (tombstone), pid 400 appears (green). Both are
        # leaves directly under pid 1.
        self._resample(
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
            '  200   100 root  2.0  300 00:00:03 child\n'
            '  400     1 root  0.0  100 00:00:00 newproc\n')
        # The display reads the CURRENT snapshot, so re-prime ps to that table.
        self._cur_ps = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
            '  200   100 root  2.0  300 00:00:03 child\n'
            '  400     1 root  0.0  100 00:00:00 newproc\n')

    def _children_cur(self, parent, reload=False):
        with mock.patch.object(self.r.subprocess, 'run',
                               _ps_completed(self._cur_ps)), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None):
            return self.r.get_children(parent, reload=reload)

    def test_off_no_row_fg_no_tombstones(self):
        # Highlight OFF: appeared pids carry no row_fg and tombstone rows are
        # absent (dead processes just disappear).
        self.r._HIGHLIGHT_MODE = False
        self._arm_appeared_and_tombstone()
        kids = self._children_cur(1, reload=False)     # pid 1's children
        ids = [k.id for k in kids]
        self.assertNotIn(300, ids)                     # tombstone NOT shown
        self.assertIn(400, ids)                        # live new pid IS shown
        for k in kids:
            self.assertIsNone(getattr(k, 'row_fg', None))

    def test_on_green_on_new_red_on_tombstone(self):
        # Highlight ON: pid 400 (new) gets the soft-green row_fg; pid 300
        # (finished leaf) is included as a soft-red, non-expandable tombstone.
        self.r._HIGHLIGHT_MODE = True
        self._arm_appeared_and_tombstone()
        kids = self._children_cur(1, reload=False)
        by_id = {k.id: k for k in kids}
        self.assertIn(400, by_id)
        self.assertEqual(by_id[400].row_fg, self.r._HL_NEW_FG)
        self.assertIn(300, by_id)                      # tombstone row present
        self.assertEqual(by_id[300].row_fg, self.r._HL_GONE_FG)
        self.assertFalse(by_id[300].has_children)      # tombstones are leaves
        # The tombstone renders its saved columns (the finished process's args).
        self.assertEqual(by_id[300].title, 'sibling')
        # A live, unchanged pid carries no colour.
        self.assertIsNone(getattr(by_id[100], 'row_fg', None))

    def test_soft_colors_are_not_the_harsh_bright_ansi(self):
        # The chosen highlight colours are the soft 256-palette values, not the
        # bright ANSI 2 (green) / 1 (red).
        self.assertNotIn(self.r._HL_NEW_FG, (2, 10))
        self.assertNotIn(self.r._HL_GONE_FG, (1, 9))
        self.assertEqual(self.r._HL_NEW_FG, 108)
        self.assertEqual(self.r._HL_GONE_FG, 174)


class TestHighlightPlacement(_HighlightBase):
    """Tombstones land under their former parent (tree) / in the sorted flat list."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore_sort)
        self._seed_key = self.r._SORT_KEY
        self._seed_dir = dict(self.r._SORT_DIR)
        self.r._HIGHLIGHT_MODE = True

    def _restore_sort(self):
        self.r._SORT_KEY = self._seed_key
        self.r._SORT_DIR = dict(self._seed_dir)

    def _arm(self):
        """Baseline _PS_HL, then kill leaf 200 (under 100) and leaf 300 (under 1)."""
        self._resample(_PS_HL)
        self.clock += 1.0
        self._cur = (
            '    1     0 root  0.0  100 00:00:01 /sbin/init\n'
            '  100     1 root  1.0  200 00:00:02 parent\n'
        )
        self._resample(self._cur)

    def _children(self, parent, reload=False):
        with mock.patch.object(self.r.subprocess, 'run',
                               _ps_completed(self._cur)), \
             mock.patch.object(self.r.sys, 'platform', 'linux'), \
             mock.patch.object(self.r, '_private_mem_bytes', lambda pid: None):
            return self.r.get_children(parent, reload=reload)

    def test_tree_tombstone_under_former_parent(self):
        # Tree mode: 200's tombstone shows under pid 100 (its former parent);
        # 300's tombstone shows under pid 1.
        self.r._TREE_MODE = True
        self._arm()
        root_kids = [k.id for k in self._children(None, reload=True)]
        self.assertIn(1, root_kids)
        under_1 = [k.id for k in self._children(1, reload=False)]
        self.assertIn(300, under_1)                    # leaf under pid 1
        self.assertIn(100, under_1)                    # live parent
        under_100 = [k.id for k in self._children(100, reload=False)]
        self.assertIn(200, under_100)                  # leaf under pid 100

    def test_tree_parent_with_only_tombstone_child_is_expandable(self):
        # pid 100's only surviving child (200) is a tombstone; it must still
        # report has_children so the user can expand to the tombstone.
        self.r._TREE_MODE = True
        self._arm()
        self._children(None, reload=True)
        under_1 = {k.id: k for k in self._children(1, reload=False)}
        self.assertTrue(under_1[100].has_children)

    def test_flat_tombstones_sorted_into_the_list(self):
        # Flat mode: every tombstone shows at the root, sorted in by the active
        # key alongside the live rows. Sort by pid ascending → 1,100,200,300.
        self.r._TREE_MODE = False
        self.r._SORT_KEY = 'pid'
        self._arm()
        ids = [k.id for k in self._children(None, reload=True)]
        self.assertEqual(ids, [1, 100, 200, 300])
        by_id = {k.id: k for k in self._children(None, reload=False)}
        # 200 & 300 are the red tombstones; 1 & 100 are live.
        self.assertEqual(by_id[200].row_fg, self.r._HL_GONE_FG)
        self.assertEqual(by_id[300].row_fg, self.r._HL_GONE_FG)
        self.assertIsNone(getattr(by_id[1], 'row_fg', None))


class TestHighlightToggleAction(unittest.TestCase):
    """The ``h`` action flips ``_HIGHLIGHT_MODE``, flashes, and refreshes (B9).

    Uses a real headless Browser/Context (flash lands on ``b._notice``), mirroring
    the ``t`` / sort action tests. Module global restored via ``addCleanup``.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._seed_mode = self.r._HIGHLIGHT_MODE
        self.addCleanup(lambda: setattr(self.r, '_HIGHLIGHT_MODE', self._seed_mode))
        self.r._HIGHLIGHT_MODE = False
        self.b = make_browser(
            get_children=lambda _id, *, reload=False:
                [Item(id=1, title='proc', has_children=False)])
        self.b.refresh()
        self.b.run_until_idle()
        self.ctx = Context(self.b)

    def tearDown(self):
        self.b.stop_workers()

    def test_flips_mode_and_flashes(self):
        self.assertFalse(self.r._HIGHLIGHT_MODE)
        self.r._action_toggle_highlight(self.ctx)
        self.b.run_until_idle()
        self.assertTrue(self.r._HIGHLIGHT_MODE)
        self.assertEqual(self.b._notice.text, 'highlight: on')
        # And back off again.
        self.r._action_toggle_highlight(self.ctx)
        self.b.run_until_idle()
        self.assertFalse(self.r._HIGHLIGHT_MODE)
        self.assertEqual(self.b._notice.text, 'highlight: off')

    def test_refresh_is_invoked(self):
        with mock.patch.object(self.ctx, 'refresh',
                               wraps=self.ctx.refresh) as spy:
            self.r._action_toggle_highlight(self.ctx)
            self.b.run_until_idle()
        spy.assert_called_once()

    def test_h_action_registered_in_config(self):
        # ``main`` builds a BrowserConfig whose actions include an ``h`` binding
        # routed to the toggle handler. Run main headless (stub Browser/run) and
        # inspect the captured config's ``actions`` (the Action stub keeps its
        # positional args in ``_args``: key, label, handler, gate).
        captured = {}

        def _capture_config(*a, **kw):
            cfg = types.SimpleNamespace(actions=kw.get('actions', []))
            captured['cfg'] = cfg
            return cfg

        b = mock.Mock()
        b.run.return_value = 0
        with mock.patch.object(self.r, 'Browser', return_value=b), \
             mock.patch.object(self.r, 'BrowserConfig', side_effect=_capture_config), \
             mock.patch.object(self.r, '_apply_view_flags', lambda: None), \
             mock.patch.object(_FW_STATE.sys, 'argv', ['browse-ps', '-d', '0']), \
             mock.patch.object(self.r.sys, 'exit'):
            self.r.main()

        actions = captured['cfg'].actions
        by_key = {act._args[0]: act for act in actions}
        self.assertIn('h', by_key)
        self.assertIs(by_key['h']._args[2], self.r._action_toggle_highlight)
        # Sanity: ``h`` does not collide with the recipe's other bindings.
        recipe_keys = [act._args[0] for act in actions]
        self.assertEqual(recipe_keys.count('h'), 1)


# --- Incremental ``-d`` tick (#1121) ----------------------------------------

class _TickBase(unittest.TestCase):
    """Shared setup for the incremental-tick tests (#1121).

    The ``-d`` tick rotates the snapshot, diffs it, and pushes ONE
    ``update_data`` batch instead of a full ``refresh`` — so these drive a FAKE
    clock (``_now`` patched to read ``self.clock``) for the 3.5 s tombstone
    retention WITHOUT sleeping, seed the snapshot globals directly, and restore
    them via ``addCleanup``. ``_apply_tick`` reads ``_CUR_SNAPSHOT`` as the
    "previous" sample and installs the map handed in, so a test transition is
    "seed prev, call ``_apply_tick(b, cur)``".
    """

    def setUp(self):
        self.r = _load_recipe()
        self.clock = 1000.0
        self._orig_now = self.r._now
        self.r._now = lambda: self.clock
        self.addCleanup(self._restore)
        self.r._PREV_SNAPSHOT = None
        self.r._CUR_SNAPSHOT = None
        self.r._APPEARED_AT = {}
        self.r._TOMBSTONES = {}
        self.r._HIGHLIGHT_MODE = False
        self.r._TREE_MODE = True
        self._seed_key = self.r._SORT_KEY
        self._seed_dir = dict(self.r._SORT_DIR)

    def _restore(self):
        self.r._now = self._orig_now
        self.r._PREV_SNAPSHOT = None
        self.r._CUR_SNAPSHOT = None
        self.r._APPEARED_AT = {}
        self.r._TOMBSTONES = {}
        self.r._HIGHLIGHT_MODE = False
        self.r._TREE_MODE = True
        self.r._SORT_KEY = self._seed_key
        self.r._SORT_DIR = dict(self._seed_dir)

    def _info(self, pid, ppid, *, user='root', pcpu=0.0, mem_kb=100,
              cpu_secs=0, args=None):
        """Build one ``ProcInfo`` (mem given in KiB, like the ps RSS column)."""
        info = self.r.ProcInfo(pid, ppid, user, pcpu, mem_kb * 1024, cpu_secs,
                               args or f'proc{pid}')
        return info

    def _seed_prev(self, infos, *, wall=None):
        """Install ``infos`` (a list of ProcInfo) as the current snapshot.

        This becomes the "previous" sample the next ``_apply_tick`` rotates out.
        ``_PREV_SNAPSHOT`` is left ``None`` so CPU% falls back to ``pcpu`` (the
        diff treats a ``None`` prev as the baseline — see ``_diff_snapshot``).
        """
        procs = {i.pid: i for i in infos}
        self.r._CUR_SNAPSHOT = (wall if wall is not None else self.clock, procs)
        return procs

    def _ops_by_kind(self, ops):
        """Group an op-tuple batch into ``{kind: [op, ...]}`` for assertions."""
        out = {}
        for op in ops:
            out.setdefault(op[0], []).append(op)
        return out


class TestSharedFieldBuilder(_TickBase):
    """``_proc_fields`` is the single source of truth for both row paths (#1121).

    The incremental tick patches rows with ``_proc_fields``; the full rebuild
    (``_proc_item``) sets the same fields. They MUST be byte-identical for the
    same ProcInfo or incremental and rebuilt rows would drift.
    """

    def test_fields_match_proc_item(self):
        info = self._info(4242, 1, user='alice', pcpu=12.0, mem_kb=4096,
                          args='python3 -m http.server 8000')
        # The snapshot must be primed so ``_cpu_pct`` (read by both paths) has a
        # current sample to read; first sample → pcpu fallback (12% here).
        self._seed_prev([info])
        fields = self.r._proc_fields(info)
        item = self.r._proc_item(info, has_children=False)
        for key, value in fields.items():
            with self.subTest(field=key):
                self.assertEqual(getattr(item, key), value)
        # And the dict carries exactly the row display fields (no row_fg — the
        # highlight is decided separately by ``_row_highlight_fg``).
        self.assertEqual(set(fields),
                         {'title', 'col_pid', 'col_user', 'col_cpu', 'col_mem'})

    def test_cpu_and_mem_strings(self):
        info = self._info(7, 1, pcpu=3.4, mem_kb=2048)
        self._seed_prev([info])
        fields = self.r._proc_fields(info)
        self.assertEqual(fields['col_cpu'], '3%')           # round(3.4)
        self.assertEqual(fields['col_mem'], self.r.human_size(2048 * 1024))
        self.assertEqual(fields['col_pid'], '7')
        self.assertEqual(fields['title'], 'proc7')


class TestTickAlive(_TickBase):
    """An alive pid whose cpu%/mem changed emits a ``mod`` carrying the new values."""

    def test_alive_changed_emits_mod_with_updated_cols(self):
        # Prev: pid 100 at 30.0 cpu_secs / 4 MiB. Cur: +0.5 cpu_secs over a 1 s
        # wall gap → 50% (per-core delta path), and memory grew to 8 MiB.
        prev = self._info(100, 1, cpu_secs=30.0, mem_kb=4096)
        self._seed_prev([self._info(1, 0), prev], wall=self.clock)
        self.clock += 1.0
        cur_100 = self._info(100, 1, cpu_secs=30.5, mem_kb=8192)
        cur = {1: self._info(1, 0), 100: cur_100}
        b = mock.Mock()
        self.r._apply_tick(b, cur)
        ops = b.update_data.call_args.args[0]
        mods = {op[1]: op for op in ops if op[0] == 'mod'}
        self.assertIn(100, mods)
        fields = mods[100][3]
        self.assertEqual(fields['col_cpu'], '50%')
        self.assertEqual(fields['col_mem'], self.r.human_size(8192 * 1024))
        # No row was added/removed — only in-place mods this tick.
        self.assertNotIn('upsert', self._ops_by_kind(ops))
        self.assertNotIn('remove', self._ops_by_kind(ops))

    def test_apply_tick_uses_update_data_not_refresh(self):
        # The no-flicker contract: the tick converges via update_data, never the
        # full-teardown refresh.
        self._seed_prev([self._info(1, 0)])
        b = mock.Mock()
        self.r._apply_tick(b, {1: self._info(1, 0)})
        b.refresh.assert_not_called()
        # An unchanged single root still emits its (idempotent) mod batch.
        b.update_data.assert_called_once()


class TestTickNew(_TickBase):
    """A freshly-appeared pid is ``upsert``ed under the right parent, in order."""

    def test_new_pid_tree_mode_under_ppid_when_loaded(self):
        self.r._TREE_MODE = True
        self._seed_prev([self._info(1, 0), self._info(100, 1)])
        # Parent pid 1's children are loaded (cached); pid 300 appears under it.
        b = mock.Mock()
        b.cached_children = lambda pid: [] if pid == 1 else None
        cur = {1: self._info(1, 0), 100: self._info(100, 1),
               300: self._info(300, 1, args='newproc')}
        self.r._apply_tick(b, cur)
        ops = b.update_data.call_args.args[0]
        ups = {op[1]: op for op in ops if op[0] == 'upsert'}
        self.assertIn(300, ups)
        _kind, _id, parent_id, fields = ups[300][:4]
        self.assertEqual(parent_id, 1)                      # ppid in tree mode
        self.assertEqual(fields['title'], 'newproc')
        self.assertEqual(fields['col_pid'], '300')

    def test_new_pid_tree_mode_skipped_when_parent_unloaded(self):
        # A new pid under a not-yet-expanded parent must NOT be upserted (it
        # would create a partial child list the framework treats as complete);
        # it appears the normal way on expand.
        self.r._TREE_MODE = True
        self._seed_prev([self._info(1, 0), self._info(100, 1)])
        b = mock.Mock()
        b.cached_children = lambda pid: None        # nothing expanded
        cur = {1: self._info(1, 0), 100: self._info(100, 1),
               300: self._info(300, 100)}           # under the (unloaded) pid 100
        self.r._apply_tick(b, cur)
        ops = b.update_data.call_args.args[0]
        self.assertNotIn('upsert', self._ops_by_kind(ops))

    def test_new_pid_flat_mode_under_root_in_sorted_position(self):
        # Flat mode: root children are always loaded → the new pid upserts under
        # the root (None) anchored before its sorted successor. Sort by pid asc:
        # inserting 150 between loaded 100 and 200 → before the loaded INDEX of
        # its successor 200 (index 1). pids are int ids and the ``where`` API
        # reads an int ref as a positional index, so it carries the index, not
        # the successor pid.
        self.r._TREE_MODE = False
        self.r._SORT_KEY = 'pid'
        self._seed_prev([self._info(100, 1), self._info(200, 1)])
        b = mock.Mock()
        # Loaded root children in display (sorted) order, with real ids so the
        # placement can locate the successor's index.
        b.cached_children = lambda pid: [mock.Mock(id=100), mock.Mock(id=200)]
        cur = {100: self._info(100, 1), 150: self._info(150, 1),
               200: self._info(200, 1)}
        self.r._apply_tick(b, cur)
        ops = b.update_data.call_args.args[0]
        up = next(op for op in ops if op[0] == 'upsert' and op[1] == 150)
        self.assertIsNone(up[2])                            # parent = flat root
        self.assertEqual(up[4], ('before', None, 1))        # before successor's index

    def test_new_pid_last_when_it_sorts_to_the_end(self):
        self.r._TREE_MODE = False
        self.r._SORT_KEY = 'pid'
        self._seed_prev([self._info(100, 1), self._info(200, 1)])
        b = mock.Mock()
        b.cached_children = lambda pid: [object(), object()]
        cur = {100: self._info(100, 1), 200: self._info(200, 1),
               300: self._info(300, 1)}                     # sorts last
        self.r._apply_tick(b, cur)
        ops = b.update_data.call_args.args[0]
        up = next(op for op in ops if op[0] == 'upsert' and op[1] == 300)
        self.assertEqual(up[4], ('last', None))


class TestTickGone(_TickBase):
    """A vanished pid is removed at once, or kept as a red tombstone then removed."""

    def test_gone_pid_highlight_off_removed_immediately(self):
        self.r._HIGHLIGHT_MODE = False
        self._seed_prev([self._info(1, 0), self._info(300, 1)])
        b = mock.Mock()
        b.cached_children = lambda pid: None
        cur = {1: self._info(1, 0)}                          # pid 300 gone
        self.r._apply_tick(b, cur)
        ops = b.update_data.call_args.args[0]
        removes = [op[1] for op in ops if op[0] == 'remove']
        self.assertIn(300, removes)
        # The diff still records the tombstone (bookkeeping always runs), but
        # with highlight off the tick removes the row outright — no red mod, to
        # match ``_scoped_tombstones`` showing nothing when off.
        mods = {op[1] for op in ops if op[0] == 'mod'}
        self.assertNotIn(300, mods)

    def test_gone_leaf_highlight_on_becomes_red_tombstone_then_removed(self):
        # Highlight ON + the gone pid was a leaf → keep it as a soft-red
        # tombstone (a ``mod`` setting row_fg), NOT removed yet.
        self.r._HIGHLIGHT_MODE = True
        self._seed_prev([self._info(1, 0), self._info(300, 1, args='sibling')])
        b = mock.Mock()
        b.cached_children = lambda pid: None
        cur = {1: self._info(1, 0)}                          # leaf 300 dies
        self.r._apply_tick(b, cur)
        ops = b.update_data.call_args.args[0]
        kinds = self._ops_by_kind(ops)
        self.assertNotIn(300, [op[1] for op in kinds.get('remove', [])])
        tomb_mod = next(op for op in ops if op[0] == 'mod' and op[1] == 300)
        self.assertEqual(tomb_mod[3]['row_fg'], self.r._HL_GONE_FG)
        # Its saved columns still render (the finished process's args/title).
        self.assertEqual(tomb_mod[3]['title'], 'sibling')
        self.assertIn(300, self.r._TOMBSTONES)               # retained

        # Advance past the 3.5 s window; the NEXT tick prunes the tombstone and
        # emits the deferred ``remove`` (cur unchanged otherwise).
        self.clock += self.r._HIGHLIGHT_SECS + 0.1
        b2 = mock.Mock()
        b2.cached_children = lambda pid: None
        self.r._apply_tick(b2, {1: self._info(1, 0)})
        ops2 = b2.update_data.call_args.args[0]
        self.assertIn(300, [op[1] for op in ops2 if op[0] == 'remove'])
        self.assertNotIn(300, self.r._TOMBSTONES)

    def test_gone_parent_with_children_removed_not_tombstoned(self):
        # A vanished pid that HAD a child in the previous snapshot is NOT a
        # tombstone (its row just goes); highlight on or not, it's a remove.
        self.r._HIGHLIGHT_MODE = True
        self._seed_prev([self._info(1, 0), self._info(100, 1),
                         self._info(200, 100)])
        b = mock.Mock()
        b.cached_children = lambda pid: None
        # Both 100 (parent) and 200 (its child) vanish at once.
        cur = {1: self._info(1, 0)}
        self.r._apply_tick(b, cur)
        ops = b.update_data.call_args.args[0]
        removes = {op[1] for op in ops if op[0] == 'remove'}
        self.assertIn(100, removes)                          # parent → removed
        self.assertNotIn(100, self.r._TOMBSTONES)            # not tombstoned


class TestTickRowFgGating(_TickBase):
    """The ops carry row_fg (green new / red tombstone) only when highlight on."""

    def test_new_pid_green_when_on_none_when_off(self):
        # New pid 300 under the loaded root in flat mode.
        self.r._TREE_MODE = False
        base = [self._info(100, 1)]

        # Highlight ON → soft-green row_fg on the new upsert.
        self.r._HIGHLIGHT_MODE = True
        self._seed_prev(base)
        b = mock.Mock(); b.cached_children = lambda pid: [object()]
        self.r._apply_tick(b, {100: self._info(100, 1), 300: self._info(300, 1)})
        up = next(op for op in b.update_data.call_args.args[0]
                  if op[0] == 'upsert' and op[1] == 300)
        self.assertEqual(up[3]['row_fg'], self.r._HL_NEW_FG)

        # Highlight OFF → row_fg is None (identical to a rebuilt uncoloured row).
        self.r._HIGHLIGHT_MODE = False
        self.r._APPEARED_AT = {}
        self._seed_prev(base)
        b2 = mock.Mock(); b2.cached_children = lambda pid: [object()]
        self.r._apply_tick(b2, {100: self._info(100, 1), 300: self._info(300, 1)})
        up2 = next(op for op in b2.update_data.call_args.args[0]
                   if op[0] == 'upsert' and op[1] == 300)
        self.assertIsNone(up2[3]['row_fg'])

    def test_alive_mod_clears_stale_green_after_window(self):
        # A pid that appeared (green) must lose its green once the highlight
        # window lapses: the alive ``mod`` carries row_fg=None then.
        self.r._HIGHLIGHT_MODE = True
        self._seed_prev([self._info(1, 0)])
        b = mock.Mock(); b.cached_children = lambda pid: [object()]
        # Tick 1: pid 400 appears → green.
        self.r._apply_tick(b, {1: self._info(1, 0), 400: self._info(400, 1)})
        up = next(op for op in b.update_data.call_args.args[0]
                  if op[0] == 'upsert' and op[1] == 400)
        self.assertEqual(up[3]['row_fg'], self.r._HL_NEW_FG)
        # Tick 2 past the window: 400 is alive (mod) and no longer in
        # _APPEARED_AT → row_fg cleared to None.
        self.clock += self.r._HIGHLIGHT_SECS + 0.1
        b2 = mock.Mock(); b2.cached_children = lambda pid: [object()]
        self.r._apply_tick(b2, {1: self._info(1, 0), 400: self._info(400, 1)})
        mod_400 = next(op for op in b2.update_data.call_args.args[0]
                       if op[0] == 'mod' and op[1] == 400)
        self.assertIsNone(mod_400[3]['row_fg'])
        self.assertNotIn(400, self.r._APPEARED_AT)


class TestTickAppliesToRealBrowser(_TickBase):
    """End-to-end: a posted tick mutates a REAL headless Browser's loaded rows.

    Proves the op batch the tick builds actually drives the framework apply path
    (not just tuple shapes): a loaded row's columns change in place and a new pid
    is inserted — with the cursor untouched (no refresh).
    """

    def _seed_browser(self, infos):
        """Headless flat-mode Browser whose root children are ``infos`` rows.

        Builds GENUINE framework ``Item``s (not the recipe's stubbed ``Item``)
        carrying the same ``_proc_fields`` the recipe paints, so the rows the
        framework stores and the ``update_data`` batch later patches are real.
        """
        self.r._TREE_MODE = False

        def _row(info):
            fields = self.r._proc_fields(info)
            it = Item(id=info.pid, title=fields['title'], has_children=False)
            for k, v in fields.items():
                setattr(it, k, v)
            it.pid = info.pid
            it.user = info.user
            return it

        items = [_row(i) for i in infos]
        b = make_browser(get_children=lambda _id, *, reload=False: list(items))
        b.refresh()
        b.run_until_idle()
        self.addCleanup(b.stop_workers)
        return b

    def test_in_place_update_and_insert_no_cursor_jump(self):
        self.r._SORT_KEY = 'pid'
        a = self._info(100, 1, cpu_secs=10, mem_kb=1024, args='a')
        c = self._info(300, 1, cpu_secs=10, mem_kb=1024, args='c')
        self._seed_prev([a, c], wall=self.clock)
        b = self._seed_browser([a, c])
        b.cursor_to(300)
        b.run_until_idle()
        ctx = Context(b)
        self.assertEqual(ctx.cursor.id, 300)

        # Tick: pid 100 grows memory, pid 200 appears between 100 and 300.
        self.clock += 1.0
        cur = {100: self._info(100, 1, cpu_secs=10, mem_kb=4096, args='a'),
               200: self._info(200, 1, cpu_secs=10, mem_kb=2048, args='b'),
               300: self._info(300, 1, cpu_secs=10, mem_kb=1024, args='c')}
        b.post(lambda: self.r._apply_tick(b, cur))
        b.run_until_idle()

        # The loaded row 100 repainted in place (new memory column).
        self.assertEqual(b.get_item(100).col_mem,
                         self.r.human_size(4096 * 1024))
        # The new pid 200 was inserted in sorted position (between 100 and 300).
        root_kids = [it.id for it in b.cached_children(None)]
        self.assertEqual(root_kids, [100, 200, 300])
        # Cursor stayed on pid 300 — no full teardown.
        self.assertEqual(ctx.cursor.id, 300)


if __name__ == '__main__':
    unittest.main()
