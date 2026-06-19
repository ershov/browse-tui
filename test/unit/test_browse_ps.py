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

    def test_no_tag_chip_on_normal_rows(self):
        # B3 moved pid/user into the gutter; the old ``tag='user pid=…'`` chip
        # is gone (would otherwise render redundantly next to the title).
        for parent in (None, 1, 100):
            for item in self._children(parent, reload=(parent is None)):
                self.assertIsNone(getattr(item, 'tag', None))
                self.assertIsNone(getattr(item, 'tag_style', None))


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


if __name__ == '__main__':
    unittest.main()
