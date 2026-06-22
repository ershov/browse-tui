"""Unit tests for the ``recipes/browse-git`` helpers.

The recipe is a single-file ``--run-py`` script that imports
``browse_tui`` (only available when the binary loads it). To exercise
the diff colorizer and the positional classifier directly we stub
``browse_tui`` in ``sys.modules`` and load the extension-less recipe via
``SourceFileLoader`` — the same pattern as ``test/unit/test_browse_md.py``.

Row ids are tagged tuples (``('commit', sha)``, ``('file', sha, path)``,
…) built directly by the construction sites; there is no id-string parser
to test in isolation, so the id-shape coverage lives in the per-builder
tests below (each asserts the tuple its rows carry) and the ``id[0]``
dispatch is covered end-to-end against a real headless Browser in
``test/ui/test_recipe_browse_git.py``.

Coverage (ticket #616 — structural backbone):

* ``_colorize_diff``       ANSI on both the delta and git-fallback paths
* ``_classify_positionals``  path / rev / ``--`` / unknown(→exit)

Coverage (ticket #617 — commits mode end-to-end):

* ``_parse_decorations``   ``%D`` → ref chips (HEAD/branch/remote/tag)
* ``_parse_name_status``   A/M/D letters + rename → status + new path

Coverage (ticket #618 — reflog mode):

* ``_reflog_row``          NUL record → reflog Item (id/chips), malformed→None

Coverage (ticket #619 — status mode):

* ``_parse_porcelain_z``   NUL porcelain → (XY, path), incl. rename
* ``_status_tag``          XY → one-letter tag (X-or-Y, ``?`` for ``??``)
* ``_status_diff_plan``    XY → staged/unstaged/untracked diff command(s)

Coverage (ticket #620 — stash mode):

* ``_stash_index``         ``stash@{n}`` → ``n`` int (or None)
* ``_stash_row``           NUL record → stash Item (id/tag/title/chips)

Coverage (ticket #621 — branches mode):

* ``_parse_for_each_ref_line``  full+short refname → (kind, short),
  kind classified from the refs/heads|remotes|tags prefix

Coverage (ticket #662 — commits columnar list):

* ``_commit_log_items``    stores ``col_sha`` / ``col_author`` /
  ``col_date`` and NO sha ``tag``; ``chips`` is the ``%D`` decorations
  only (no author·date chip)
* ``git_row_content``      commit rows → padded sha/author/date columns,
  decoration chips, then the subject LAST; rows of differing lengths
  align per-column; a non-commit row (no ``col_sha``) falls back to
  exactly ``default_row_content`` and never measures a column

Coverage (ticket #701 — tree-mode commit graph):

* ``_graph_translate``     sanitises git's coloured art (keep only SGR) then
  maps the ``*|_`` glyphs to their box/block glyphs (diagonals ``/`` ``\\``
  pass through) — ANSI-safely, only on the plain runs between SGR sequences —
  preserves internal spacing, rstrips trailing pad
* ``_commit_graph_items``  ``git log --graph`` lines → commit Items (with
  ``col_graph``) interleaved with ``meta=True`` filler Items
  (``('filler', ns, n)``, ``has_children`` False, no ``col_sha``); git line
  order preserved
* ``_log_items``           routes to the graph builder when ``_tree_mode``
  else the plain ``_commit_log_items`` (off-path unchanged)
* ``git_row_content``      commit row inserts the graph after the date
  column; filler row = blank pad (sha+author+date span) then the art;
  a tree-off commit row (no ``col_graph``) is byte-identical to before
* ``_pop_tree_arg``        pops ``--tree`` / ``--no-tree`` (last wins)
* ``_pop_mode_flag``       pops a per-mode flag (``--status`` etc., derived
  from ``_MODES``); >1 distinct mode is a usage error (exit 2)
* ``toggle_tree``          flips ``_tree_mode`` and refreshes

Coverage (ticket #862 — ``browse-git -``, git output from stdin):

* ``_sniff_stdin_kind``      first non-blank line → diff / log / porcelain /
  human / None (colored input sniffs the same; prose is unrecognized)
* ``_parse_stdin_diff``      per-file block split + (letter, path) rows —
  A/M/D/R letters, new-path display, headerless ``--- a/`` fragments,
  GNU ``\\t<timestamp>`` suffixes, ``--color=always`` input
* ``_parse_stdin_log``       ``commit <sha>`` blocks → (sha, author, date,
  subject, deco) rows; --stat / -p payload stays in the block
* ``_parse_porcelain_lines`` line porcelain → (XY, path), rename arrow
* ``_parse_human_status``    human sections / verbs → porcelain-style rows
* stdin tree + previews      the root builders / ``get_preview`` serve the
  parsed text with git fully poisoned (the no-git guarantee), and the
  ``_status_root`` seam reuses the repo-mode leaf/sentinel shape
* ``main()``                 ``-`` ingest end-to-end (no git at startup);
  empty / unrecognized / combined-args errors (exit 2, stderr only);
  bare and ``--help`` invocations never read stdin; ``--tty -`` /
  ``--tty=-`` (the framework flag's value) is NOT the stdin positional
* action gating              `` ` `` / ``t`` flash in stdin mode (no state
  change); ``E`` covers ('sfile', i, path) rows; the stdin window title
* ``_run_git`` gitless       a missing git binary folds into a failed
  CompletedProcess (rc 127) — ``E`` degrades to the cwd-relative path
  and previews surface the message instead of crashing the key dispatch

The stdin parsers are additionally exercised against REAL ``git diff`` /
``log`` / ``log -p`` / ``log --stat`` / ``status --porcelain`` /
``status`` output captured from a throwaway temp repo
(``TestStdinRealGitOutputs``), so the literal fixtures can't drift from
what git actually emits.

Filler rows are ``meta=True`` (ticket #741): the framework skips the cursor
over them (preventively) and never selects them — the recipe no longer
hand-rolls an ``on_cursor_change`` bounce or an ``on_selection_change`` strip,
so the old ``_skip_fillers`` / ``_on_selection_change`` / ``_graph_rows_by_ns``
machinery and its unit tests are gone. The cursor-skip + unselectability is
covered end-to-end against a real headless Browser in
``test/ui/test_recipe_browse_git.py``.
"""

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-git'


# ``main()`` auto-detects a piped stdin via ``os.isatty(0)``: a non-tty fd 0
# synthesizes the lone ``-`` (stdin ingest). The test runner's fd 0 is itself
# a pipe (non-tty), which would spuriously trip that auto-detect for the bare/
# repo-mode cases below. Pin the whole module to an INTERACTIVE tty so those
# tests keep exercising bare/repo mode (the historical default); the dedicated
# auto-detect tests opt back into a pipe via ``_piped_stdin``.
_isatty_patch = None


def setUpModule():
    global _isatty_patch
    _isatty_patch = mock.patch('os.isatty', return_value=True)
    _isatty_patch.start()


def tearDownModule():
    if _isatty_patch is not None:
        _isatty_patch.stop()


@contextlib.contextmanager
def _piped_stdin():
    """Within the block, ``os.isatty(0)`` is False (a piped/redirected stdin).

    Restores the module-wide interactive default on exit, so the auto-detect
    tests can simulate ``git diff | browse-git`` without leaking the False
    into neighbouring bare/repo-mode cases."""
    with mock.patch('os.isatty', return_value=False):
        yield


# Sentinel the stub ``style('dim')`` / ``style('yellow')`` return; the
# columns in ``git_row_content`` must carry these exact (fg, bold) pairs.
_DIM = (242, False)
_YELLOW = (3, False)


def _stub_recipe_argv(argv=None):
    """Stub of the framework's ``recipe_argv`` (mirrors 040-state.py):
    ``sys.argv[1:]`` (or ``argv``) minus the framework's ``--tty VALUE`` /
    ``--tty=VALUE`` flag. Tests patch ``sys.argv`` before driving ``main()``,
    so reading it here matches what the recipe sees."""
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


def _stub_browse_tui():
    """Insert a no-op ``browse_tui`` module so the recipe can import.

    Always installs a fresh module so a stub left behind by another
    recipe's unit test doesn't bleed in. ``Item`` keeps its kwargs as
    attributes so the children-builder tests can read ``.id`` / ``.tag``
    if needed; ``Browser`` / ``BrowserConfig`` / ``Action`` are inert.

    The column helpers (``cell_ljust`` / ``cell_width`` / ``style`` /
    ``default_row_content``) are functional-but-minimal — the test data is
    plain ASCII so ``str.ljust`` / ``len`` measure the same as the real
    cell-aware helpers, which is enough to prove ``git_row_content`` (and
    ``_untracked_stat``) wire them correctly. They mirror the stub in
    ``test_browse_fs.py``.

    ``sanitize_ansi`` mirrors the framework's escape-sanitiser (050-render)
    1:1 — keep complete SGR (``\\e[…m``), drop every other CSI / bare ESC —
    so the recipe's coloured-graph path is exercised faithfully under the
    stub (the recipe imports it for ``_graph_translate``).
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

    mod.cell_ljust = lambda s, width, fill=' ': s.ljust(width, fill)
    mod.cell_width = len  # test data is plain ASCII → len == display width

    def _style(name):
        if name == 'dim':
            return _DIM
        if name == 'yellow':
            return _YELLOW
        return (None, False)

    mod.style = _style

    def _default_row_content(item, ctx):
        # A recognisable sentinel so the fallback path is unambiguous.
        return [('DEFAULT', getattr(item, 'id', None), getattr(item, 'title', None))]

    mod.default_row_content = _default_row_content

    _sanitize_re = re.compile(r'\x1b\[[^@-~]*([@-~])|\x1b\[[^@-~]*\Z|\x1b')

    def sanitize_ansi(s):
        if '\x1b' not in s:
            return s
        return _sanitize_re.sub(
            lambda m: m.group(0) if m.group(1) == 'm' else '', s)

    mod.sanitize_ansi = sanitize_ansi
    mod.recipe_argv = _stub_recipe_argv
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-git recipe; returns a fresh module.

    ``recipes/`` is put on ``sys.path`` so the recipe's optional
    ``from md2ansi_lib import ...`` resolves to the real library, just
    as ``--run-py`` does by prepending the recipe directory at runtime.
    A fresh module is built on every call so tests that mutate
    module-level globals (``_revs`` / ``_paths`` / ``_MD_COLOR``) stay
    isolated.
    """
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
    _stub_browse_tui()
    name = '_browse_git_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _FakeCtx:
    """A ``RowContext`` stand-in: ``max_col_width(field)`` → fixed width.

    Records every field measured in ``calls`` so a test can assert the
    fallback path never touches a column.
    """

    def __init__(self, widths):
        self._widths = widths
        self.calls = []

    def max_col_width(self, field, parent_id=None):
        self.calls.append(field)
        return self._widths[field]


class TestColorizeDiff(unittest.TestCase):
    """``_colorize_diff`` returns ANSI on both delta and fallback paths."""

    def setUp(self):
        self.r = _load_recipe()
        # A minimal git-colored diff (caller's contract: already colored).
        self.colored = (
            '\x1b[1mdiff --git a/x b/x\x1b[m\n'
            '\x1b[31m--- a/x\x1b[m\n'
            '\x1b[32m+++ b/x\x1b[m\n'
            '@@ -1 +1 @@\n'
            '\x1b[31m-old\x1b[m\n'
            '\x1b[32m+new\x1b[m\n'
        )

    def _capture_run(self, width):
        """Run ``_colorize_diff`` with delta forced on at preview ``width``.

        Swaps the recipe module's ``subprocess`` for a fake that records
        the delta argv (never spawning a real process) and ``_browser``
        for a stub reporting ``width``; returns the captured argv list.
        """
        self.r.HAVE_DELTA = '/usr/bin/delta'
        self.r._browser = types.SimpleNamespace(preview_width=width)
        captured = []

        def fake_run(cmd, **kw):
            captured.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout='\x1b[34mrendered\x1b[0m\n', stderr='')

        self.r.subprocess = types.SimpleNamespace(
            run=fake_run, CompletedProcess=subprocess.CompletedProcess)
        self.r._colorize_diff(self.colored)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_fallback_path_returns_colored_text(self):
        # Force the no-delta branch by clearing the module-level
        # ``HAVE_DELTA`` probe (resolved once at load); the helper now
        # gates on it, not a per-render ``shutil.which`` call.
        self.r.HAVE_DELTA = None
        # A subprocess that would explode if the no-delta branch ever
        # spawned — proving it spawns nothing.
        self.r.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: self.fail('no-delta path must not spawn'))
        out = self.r._colorize_diff(self.colored)
        self.assertIn('\x1b[', out)
        # Fallback returns the caller's already-colored text unchanged.
        self.assertEqual(out, self.colored)

    def test_delta_path_returns_ansi(self):
        if shutil.which('delta') is None:
            self.skipTest('delta not on PATH')
        # ``HAVE_DELTA`` is truthy at load (delta on PATH); the helper
        # pipes through the genuine ``subprocess`` module, exercising the
        # real delta binary end to end.
        out = self.r._colorize_diff(self.colored)
        self.assertIn('\x1b[', out)

    def test_delta_path_monkeypatched(self):
        # Prove the delta branch produces ANSI-bearing text without
        # depending on a delta install: force ``HAVE_DELTA`` truthy + a
        # fake subprocess namespace on the recipe module only (never the
        # shared ``subprocess`` module, which would leak into other tests).
        self.r.HAVE_DELTA = '/usr/bin/delta'

        def fake_run(cmd, **kw):
            self.assertEqual(cmd[0], 'delta')
            return subprocess.CompletedProcess(
                cmd, 0, stdout='\x1b[34mrendered\x1b[0m\n', stderr='')

        fake_subprocess = types.SimpleNamespace(
            run=fake_run, CompletedProcess=subprocess.CompletedProcess)
        self.r.subprocess = fake_subprocess
        out = self.r._colorize_diff(self.colored)
        self.assertIn('\x1b[', out)
        self.assertIn('rendered', out)

    # --- side-by-side gating (>=160) -------------------------------------
    # delta renders two-column at a wide preview; the recipe appends
    # ``--side-by-side`` + ``--line-fill-method=spaces`` iff width >= 160.
    # ``--width <width>`` is passed in BOTH modes (delta splits it into two
    # columns for side-by-side), so it must always be present and the only
    # difference across the threshold is the two extra flags.

    _SBS_FLAGS = ('--side-by-side', '--line-fill-method=spaces')

    def test_below_threshold_is_unified_no_sbs_flags(self):
        # 159 < 160 → unified: neither side-by-side flag, width still passed.
        argv = self._capture_run(159)
        for flag in self._SBS_FLAGS:
            self.assertNotIn(flag, argv)
        self.assertIn('--width', argv)
        self.assertEqual(argv[argv.index('--width') + 1], '159')

    def test_at_threshold_adds_side_by_side_flags(self):
        # 160 is the inclusive boundary → side-by-side on.
        argv = self._capture_run(160)
        for flag in self._SBS_FLAGS:
            self.assertIn(flag, argv)
        self.assertIn('--width', argv)
        self.assertEqual(argv[argv.index('--width') + 1], '160')

    def test_wide_adds_side_by_side_flags(self):
        # Comfortably wide → side-by-side on; width passed once (delta
        # splits it), not doubled.
        argv = self._capture_run(200)
        for flag in self._SBS_FLAGS:
            self.assertIn(flag, argv)
        self.assertEqual(argv.count('--width'), 1)
        self.assertEqual(argv[argv.index('--width') + 1], '200')


class _ResizeCtx:
    """A ``RowContext`` stand-in for ``_on_resize``.

    ``preview_width`` is set by the test before each fire; every
    ``drop_preview_cache`` is counted (and its ``id`` argument recorded —
    ``None`` = full drop, an id = targeted drop) so a test can assert how
    many fires dropped the cache and how broadly.
    """

    def __init__(self):
        self.preview_width = 0
        self.drops = 0
        self.drop_ids = []

    def drop_preview_cache(self, id=None):
        self.drops += 1
        self.drop_ids.append(id)


class TestOnResize(unittest.TestCase):
    """``_on_resize`` drops preview cache only for width-dependent renders:
    everything when a width change flips delta's layout (side-by-side
    is/was active and the width moved), or just the piped-diff umbrella
    (whose --stat re-flows on any width change) in stdin diff mode.

    The handler keeps a module-level ``_prev_pw`` baseline and fires from
    the run loop (main thread). Each case resets that baseline and pins
    ``HAVE_DELTA`` on the recipe module so the matrix is deterministic.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.r._prev_pw = None

    def _fire(self, ctx, width):
        """Drive one ``on_resize`` fire at preview ``width``; return drops."""
        ctx.preview_width = width
        before = ctx.drops
        self.r._on_resize(ctx, 0, 0)  # cols/rows unused by the handler
        return ctx.drops - before

    def test_first_fire_sets_baseline_no_drop(self):
        # prev is None on the first fire → baseline only, never a drop,
        # and ``_prev_pw`` advances to the seen width.
        self.r.HAVE_DELTA = '/usr/bin/delta'
        ctx = _ResizeCtx()
        self.assertEqual(self._fire(ctx, 170), 0)
        self.assertEqual(self.r._prev_pw, 170)

    def test_same_width_does_not_drop(self):
        # Height-only resizes re-fire at an unchanged width → no drop even
        # well above the threshold; baseline still tracks the width.
        self.r.HAVE_DELTA = '/usr/bin/delta'
        ctx = _ResizeCtx()
        self._fire(ctx, 200)            # baseline
        self.assertEqual(self._fire(ctx, 200), 0)
        self.assertEqual(self.r._prev_pw, 200)

    def test_both_below_threshold_does_not_drop(self):
        # A width change that stays entirely unified (both < 160) does not
        # change delta's layout → no drop.
        self.r.HAVE_DELTA = '/usr/bin/delta'
        ctx = _ResizeCtx()
        self._fire(ctx, 100)            # baseline
        self.assertEqual(self._fire(ctx, 120), 0)
        self.assertEqual(self.r._prev_pw, 120)

    def test_cross_up_drops(self):
        # 150 → 170 crosses 160 upward (unified → side-by-side) → drop.
        self.r.HAVE_DELTA = '/usr/bin/delta'
        ctx = _ResizeCtx()
        self._fire(ctx, 150)            # baseline
        self.assertEqual(self._fire(ctx, 170), 1)
        self.assertEqual(self.r._prev_pw, 170)

    def test_cross_down_drops(self):
        # 170 → 150 crosses 160 downward (side-by-side → unified) → drop.
        self.r.HAVE_DELTA = '/usr/bin/delta'
        ctx = _ResizeCtx()
        self._fire(ctx, 170)            # baseline
        self.assertEqual(self._fire(ctx, 150), 1)
        self.assertEqual(self.r._prev_pw, 150)

    def test_wider_both_above_threshold_drops(self):
        # 170 → 200 stays side-by-side but the column widths change → drop.
        self.r.HAVE_DELTA = '/usr/bin/delta'
        ctx = _ResizeCtx()
        self._fire(ctx, 170)            # baseline
        self.assertEqual(self._fire(ctx, 200), 1)
        self.assertEqual(self.r._prev_pw, 200)

    def test_no_delta_never_drops(self):
        # With delta absent the diff text never depends on width, so no
        # width change drops — across the full matrix — yet the baseline
        # still advances each fire. (Repo mode: no piped diff umbrella.)
        self.r.HAVE_DELTA = None
        ctx = _ResizeCtx()
        self.assertEqual(self._fire(ctx, 170), 0)   # first fire
        self.assertEqual(self._fire(ctx, 170), 0)   # same width
        self.assertEqual(self._fire(ctx, 120), 0)   # both <160 (170→120)
        self.assertEqual(self._fire(ctx, 200), 0)   # cross-up 120→200
        self.assertEqual(self._fire(ctx, 150), 0)   # cross-down 200→150
        self.assertEqual(self._fire(ctx, 100), 0)   # wider/both moot
        self.assertEqual(self.r._prev_pw, 100)

    def test_stdin_diff_drops_umbrella_only_on_any_width_change(self):
        # In piped-diff mode the ('sdiff',) umbrella's --stat layout is
        # scaled to the preview width, so ANY genuine width change must
        # re-render it — even a unified-only change (both < 160) and even
        # with delta absent. But the per-file previews are NOT
        # width-dependent then, so the drop is TARGETED at the umbrella's
        # id, leaving the rest of the cache warm. The first (baseline)
        # and same-width fires still never drop.
        self.r.HAVE_DELTA = None
        self.r._STDIN_KIND = 'diff'
        ctx = _ResizeCtx()
        self.assertEqual(self._fire(ctx, 100), 0)   # baseline only
        self.assertEqual(self._fire(ctx, 100), 0)   # same width — no drop
        self.assertEqual(self._fire(ctx, 120), 1)   # both <160 → drop here
        self.assertEqual(self._fire(ctx, 90), 1)    # narrower → drop
        self.assertEqual(ctx.drop_ids, [('sdiff',), ('sdiff',)])
        self.assertEqual(self.r._prev_pw, 90)

    def test_stdin_diff_delta_flip_still_drops_everything(self):
        # When the width change ALSO flips delta's side-by-side layout,
        # every per-file diff re-renders too — the full (id=None) drop
        # wins over the targeted umbrella drop.
        self.r.HAVE_DELTA = '/usr/bin/delta'
        self.r._STDIN_KIND = 'diff'
        ctx = _ResizeCtx()
        self._fire(ctx, 150)            # baseline
        self.assertEqual(self._fire(ctx, 170), 1)   # crosses 160 → full
        self.assertEqual(ctx.drop_ids, [None])

    def test_stdin_log_does_not_drop_on_unified_change(self):
        # A piped LOG (not diff) has no width-scaled umbrella, so it
        # behaves like repo mode: a unified-only change with delta absent
        # does not drop.
        self.r.HAVE_DELTA = None
        self.r._STDIN_KIND = 'log'
        ctx = _ResizeCtx()
        self._fire(ctx, 100)            # baseline
        self.assertEqual(self._fire(ctx, 120), 0)
        self.assertEqual(self.r._prev_pw, 120)


class TestDispatchRoundTrip(unittest.TestCase):
    """``get_children`` / ``get_preview`` / ``edit_file`` route on ``id[0]``.

    The recipe carries every row's tagged tuple straight back through the
    framework, so dispatch is a flat ``id[0]`` match with direct field access
    (no string parsing). These tests stub each leaf builder to capture the
    decoded fields and assert the tag routes to the right builder with the
    fields intact — including the former colon-in-path / colon-or-slash-in-ref
    hazards, which are now clean tuple fields — and that the root, sentinel,
    and filler ids stay inert.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.r._paths = []

    def test_get_children_routes_each_tag_to_its_builder(self):
        shas = []
        calls = {}
        self.r._commit_files = lambda sha: shas.append(sha) or []
        # The commit branch prepends the md-refs umbrella; that has its own
        # tests (TestMdLauncherCommitMessage) — here stub it inert so this
        # routing check neither shells out for the message nor concatenates a
        # None return from the _commit_files stub above.
        self.r._commit_md_prefix = lambda sha: []
        self.r._log_items = (
            lambda revs, paths, ns: calls.__setitem__('ref', (revs, ns)))
        self.r._worktree_files = (
            lambda bucket, paths: calls.__setitem__('wc', bucket))
        self.r._stash_files = lambda n: calls.__setitem__('stash', n)

        # commit / reflog both drill the file list off a sha (id[1] / id[2]).
        self.r.get_children(('commit', 'abc123'))
        self.r.get_children(('reflog', 3, 'deadbeef'))
        self.assertEqual(shas, ['abc123', 'deadbeef'])

        # A ref drills into its commits; the full refname (slashes/colons and
        # all) is the sole rev, and the id itself is threaded as the ns.
        self.r.get_children(('ref', 'origin/feature/x'))
        self.assertEqual(
            calls['ref'], (['origin/feature/x'], ('ref', 'origin/feature/x')))

        # A worktree group routes by bucket; a stash node by its int index.
        self.r.get_children(('wc', 'staged'))
        self.assertEqual(calls['wc'], 'staged')
        self.r.get_children(('stash', 2))
        self.assertEqual(calls['stash'], 2)

    def test_get_children_leaves_and_sentinels_are_inert(self):
        # Root → root children (stub the per-mode builder); everything else
        # below is a leaf / sentinel / filler → empty, no builder reached.
        self.r._root_children = lambda: ['ROOT']
        self.assertEqual(self.r.get_children(None), ['ROOT'])
        for leaf in (('file', 'sha', 'a/b:c.txt'), ('status', 'M ', 'p.txt'),
                     ('stash', 0, 'p.txt'), ('status_clean',),
                     ('stash_none',), ('err',), ('filler', 'root', 0)):
            self.assertEqual(self.r.get_children(leaf), [], leaf)

    def test_get_preview_routes_each_tag_with_fields_intact(self):
        commit_shas = []
        seen = {}
        self.r._commit_preview = lambda sha: commit_shas.append(sha)
        self.r._file_preview = (
            lambda sha, path: seen.__setitem__('file', (sha, path)))
        self.r._status_preview = (
            lambda xy, path: seen.__setitem__('status', (xy, path)))
        self.r._worktree_preview = (
            lambda bucket, paths: seen.__setitem__('wc', bucket))
        self.r._stash_preview = lambda n: seen.__setitem__('stash', n)
        self.r._stash_file_preview = (
            lambda n, path: seen.__setitem__('stashfile', (n, path)))

        # commit + ref both resolve as a commit-ish → _commit_preview; a
        # colon-bearing refname reaches it verbatim (no string splitting).
        self.r.get_preview(('commit', 'abc'))
        self.r.get_preview(('ref', 'weird:ref'))
        self.assertEqual(commit_shas, ['abc', 'weird:ref'])

        self.r.get_preview(('file', 'sha', 'a/b:c.txt'))  # colon-bearing path
        self.r.get_preview(('status', 'M ', 'x:y.py'))
        self.r.get_preview(('wc', 'tracked'))
        self.r.get_preview(('stash', 1))
        self.r.get_preview(('stash', 1, 'a:b.py'))
        self.assertEqual(seen, {
            'file': ('sha', 'a/b:c.txt'),
            'status': ('M ', 'x:y.py'),
            'wc': 'tracked',
            'stash': 1,
            'stashfile': (1, 'a:b.py'),
        })

    def test_get_preview_root_and_sentinels_are_empty(self):
        for inert in (None, ('status_clean',), ('stash_none',), ('err',),
                      ('filler', 'root', 0)):
            self.assertEqual(self.r.get_preview(inert), '', inert)

    def test_edit_file_reads_path_field_for_file_and_status(self):
        targets = []
        self.r._run_git = lambda *a: subprocess.CompletedProcess(a, 1, '', '')

        class Ctx:
            cursor = None

            def run_external(self, argv):
                targets.append(argv)

        ctx = Ctx()
        # file / status both carry the path at id[2]; a colon-bearing path is
        # handed to $EDITOR verbatim (no repo root resolved here, rc != 0).
        ctx.cursor = self.r.Item(id=('file', 'sha', 'a/b:c.txt'))
        self.r.edit_file(ctx)
        ctx.cursor = self.r.Item(id=('status', 'M ', 'x.py'))
        self.r.edit_file(ctx)
        self.assertEqual([argv[-1] for argv in targets], ['a/b:c.txt', 'x.py'])

        # A commit / ref / stash row maps to no working-tree path → no-op.
        targets.clear()
        for nonfile in (('commit', 'sha'), ('ref', 'main'), ('stash', 0)):
            ctx.cursor = self.r.Item(id=nonfile)
            self.r.edit_file(ctx)
        self.assertEqual(targets, [])


class TestClassifyPositionals(unittest.TestCase):
    """``_classify_positionals`` sorts args into revs / paths / exit."""

    def setUp(self):
        self.r = _load_recipe()
        self._orig_argv = sys.argv

    def tearDown(self):
        sys.argv = self._orig_argv

    def _run(self, *args):
        sys.argv = ['browse-git', *args]
        self.r._revs = []
        self.r._paths = []
        self.r._classify_positionals()
        return self.r._revs, self.r._paths

    def test_existing_path_is_pathspec(self):
        # The recipe file itself certainly exists.
        revs, paths = self._run(str(_RECIPE))
        self.assertEqual(revs, [])
        self.assertEqual(paths, [str(_RECIPE)])

    def test_after_double_dash_is_pathspec(self):
        revs, paths = self._run('--', 'does/not/exist.py', 'also/missing')
        self.assertEqual(revs, [])
        self.assertEqual(paths, ['does/not/exist.py', 'also/missing'])

    def test_rev_is_classified_as_rev(self):
        # Stub git rev-parse so 'HEAD' classifies as a rev without a repo.
        def fake_run_git(*git_args):
            if 'rev-parse' in git_args:
                return subprocess.CompletedProcess(git_args, 0, '', '')
            return subprocess.CompletedProcess(git_args, 1, '', '')

        self.r._run_git = fake_run_git
        revs, paths = self._run('HEAD')
        self.assertEqual(revs, ['HEAD'])
        self.assertEqual(paths, [])

    def test_unknown_exits_2(self):
        # Neither an existing path nor a valid rev -> SystemExit(2).
        def fake_run_git(*git_args):
            return subprocess.CompletedProcess(git_args, 1, '', '')

        self.r._run_git = fake_run_git
        with self.assertRaises(SystemExit) as cm:
            self._run('definitely-not-a-real-ref-or-path-xyz')
        self.assertEqual(cm.exception.code, 2)

    def test_flag_tokens_are_skipped(self):
        # -h / --help before -- are left for the framework, not exited on.
        revs, paths = self._run('-h')
        self.assertEqual(revs, [])
        self.assertEqual(paths, [])

    def test_tty_flag_and_value_are_not_positionals(self):
        # The framework's ``--tty VALUE`` is dropped via ``recipe_argv()``
        # before classification: neither the flag nor its value is taken
        # as a rev/pathspec. The value used here is an EXISTING path
        # (``_RECIPE`` certainly exists), so a regression that failed to
        # strip it would wrongly land it in ``_paths`` — the assertion
        # below would catch it. ``--tty=`` (one token) is covered too.
        for args in (['--tty', str(_RECIPE)], [f'--tty={_RECIPE}']):
            revs, paths = self._run(*args)
            self.assertEqual(revs, [], args)
            self.assertEqual(paths, [], args)


@unittest.skipUnless(shutil.which('git'), 'git not available')
class TestPopRepoDir(unittest.TestCase):
    """``_pop_repo_dir`` redirects the cwd into a leading repo-dir positional.

    Uses real throwaway repos (the helper shells out to ``git rev-parse
    --show-toplevel``) and restores ``sys.argv`` / the cwd after each test.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._orig_argv = sys.argv
        self._orig_cwd = os.getcwd()

    def tearDown(self):
        sys.argv = self._orig_argv
        os.chdir(self._orig_cwd)

    @staticmethod
    def _make_repo():
        d = tempfile.mkdtemp()
        env = {**os.environ,
               'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        subprocess.run(['git', '-C', d, 'init', '-q'], check=True,
                       capture_output=True, env=env)
        subprocess.run(['git', '-C', d, 'commit', '-q', '--allow-empty',
                        '-m', 'c0'], check=True, capture_output=True, env=env)
        return os.path.realpath(d)

    def _run(self, *args):
        sys.argv = ['browse-git', *args]
        self.r._pop_repo_dir()
        return sys.argv[1:]

    def test_repo_dir_from_non_repo_cwd_chdirs_and_drops_arg(self):
        repo = self._make_repo()
        nonrepo = tempfile.mkdtemp()
        os.chdir(nonrepo)
        rest = self._run(repo)
        self.assertEqual(rest, [])
        self.assertEqual(os.path.realpath(os.getcwd()), repo)

    def test_other_repo_dir_redirects_and_keeps_trailing_args(self):
        repo_a = self._make_repo()
        repo_b = self._make_repo()
        os.chdir(repo_a)
        rest = self._run(repo_b, 'HEAD')
        # The dir is consumed; the trailing rev stays for _classify_positionals.
        self.assertEqual(rest, ['HEAD'])
        self.assertEqual(os.path.realpath(os.getcwd()), repo_b)

    def test_subdir_of_current_repo_stays_pathspec(self):
        repo = self._make_repo()
        sub = os.path.join(repo, 'sub')
        os.mkdir(sub)
        os.chdir(repo)
        rest = self._run('sub')
        # Same toplevel -> no redirect; the arg is left as a pathspec filter.
        self.assertEqual(rest, ['sub'])
        self.assertEqual(os.path.realpath(os.getcwd()), repo)

    def test_after_double_dash_is_never_a_repo_dir(self):
        repo = self._make_repo()
        nonrepo = tempfile.mkdtemp()
        os.chdir(nonrepo)
        rest = self._run('--', repo)
        self.assertEqual(rest, ['--', repo])
        self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(nonrepo))

    def test_leading_flag_is_skipped_dir_still_honored(self):
        repo = self._make_repo()
        nonrepo = tempfile.mkdtemp()
        os.chdir(nonrepo)
        rest = self._run('-x', repo)
        self.assertEqual(rest, ['-x'])
        self.assertEqual(os.path.realpath(os.getcwd()), repo)

    def test_non_repo_dir_arg_stays_pathspec(self):
        repo = self._make_repo()
        plain = tempfile.mkdtemp()
        os.chdir(repo)
        rest = self._run(plain)
        self.assertEqual(rest, [plain])
        self.assertEqual(os.path.realpath(os.getcwd()), repo)

    def test_leading_rev_is_not_a_repo_dir(self):
        repo = self._make_repo()
        os.chdir(repo)
        rest = self._run('HEAD')
        self.assertEqual(rest, ['HEAD'])
        self.assertEqual(os.path.realpath(os.getcwd()), repo)


class TestParseDecorations(unittest.TestCase):
    """``_parse_decorations`` turns a ``%D`` string into colored chips."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_mixed_refs(self):
        # HEAD -> branch, remote, tag, and a slash-bearing local branch.
        # Remotes={'origin'} so origin/main is blue while feature/x stays
        # a cyan local branch.
        chips = self.r._parse_decorations(
            'HEAD -> main, origin/main, tag: v1.0, feature/x',
            remotes={'origin'})
        self.assertEqual(chips, [
            ('HEAD', 'green'),
            ('main', 'cyan'),
            ('origin/main', 'blue'),
            ('v1.0', 'yellow'),
            ('feature/x', 'cyan'),
        ])

    def test_empty_decoration(self):
        self.assertEqual(self.r._parse_decorations('', remotes=set()), [])
        self.assertEqual(self.r._parse_decorations(None, remotes=set()), [])

    def test_detached_head(self):
        self.assertEqual(
            self.r._parse_decorations('HEAD', remotes=set()),
            [('HEAD', 'green')])

    def test_tag_only(self):
        self.assertEqual(
            self.r._parse_decorations('tag: v2.3', remotes=set()),
            [('v2.3', 'yellow')])

    def test_remote_needs_known_remote(self):
        # Without 'origin' in remotes, a slash ref is treated as a local
        # branch (cyan), not blue.
        self.assertEqual(
            self.r._parse_decorations('origin/main', remotes=set()),
            [('origin/main', 'cyan')])
        self.assertEqual(
            self.r._parse_decorations('origin/main', remotes={'origin'}),
            [('origin/main', 'blue')])


class TestParseNameStatus(unittest.TestCase):
    """``_parse_name_status`` maps status lines to (letter, display path)."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_add_modify_delete(self):
        out = self.r._parse_name_status('A\tnew.py\nM\ta.py\nD\tgone.py\n')
        self.assertEqual(out, [
            ('A', 'new.py'),
            ('M', 'a.py'),
            ('D', 'gone.py'),
        ])

    def test_rename_shows_new_path(self):
        # 'R100\told\tnew' -> status 'R', new path is what we display + id.
        out = self.r._parse_name_status('R100\told.txt\tnew.txt\n')
        self.assertEqual(out, [('R', 'new.txt')])

    def test_copy_shows_new_path(self):
        out = self.r._parse_name_status('C75\tsrc.txt\tcopy.txt\n')
        self.assertEqual(out, [('C', 'copy.txt')])

    def test_blank_lines_ignored(self):
        self.assertEqual(self.r._parse_name_status('\n\n'), [])

    def test_status_letter_styles(self):
        # The recipe maps each letter to the spec'd palette color.
        self.assertEqual(self.r._STATUS_LETTER_STYLE['A'], 'green')
        self.assertEqual(self.r._STATUS_LETTER_STYLE['M'], 'yellow')
        self.assertEqual(self.r._STATUS_LETTER_STYLE['D'], 'red')
        self.assertEqual(self.r._STATUS_LETTER_STYLE['R'], 'cyan')


class TestReflogRow(unittest.TestCase):
    """``_reflog_row`` turns a NUL reflog record into a decorated Item."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _record(self, sha, selector, reldate, deco, subject):
        return '\x00'.join([sha, selector, reldate, deco, subject])

    def test_full_record(self):
        line = self._record(
            'deadbeef0000000000000000000000000000abcd',
            'HEAD@{0}', '2 days ago', 'HEAD -> main', 'commit: two')
        item = self.r._reflog_row(0, line)
        # id is the tagged tuple ('reflog', n, sha) — n is the int index.
        self.assertEqual(
            item.id, ('reflog', 0, 'deadbeef0000000000000000000000000000abcd'))
        self.assertEqual(item.tag, 'deadbee')
        self.assertEqual(item.tag_style, 'yellow')
        self.assertEqual(item.title, 'commit: two')
        self.assertTrue(item.has_children)
        # Selector + reldate are dim chips, then the %D decoration chips.
        self.assertEqual(item.chips, [
            ('HEAD@{0}', 'dim'),
            ('2 days ago', 'dim'),
            ('HEAD', 'green'),
            ('main', 'cyan'),
        ])

    def test_index_is_carried(self):
        # Same sha at two reflog positions -> distinct ids (no collapse).
        sha = 'cafe00000000000000000000000000000000babe'
        line = self._record(sha, 'HEAD@{3}', '1 hour ago', '', 'reset: moving')
        item = self.r._reflog_row(3, line)
        self.assertEqual(item.id, ('reflog', 3, sha))
        self.assertEqual(item.chips, [('HEAD@{3}', 'dim'), ('1 hour ago', 'dim')])

    def test_malformed_returns_none(self):
        self.assertIsNone(self.r._reflog_row(0, 'only\x00three\x00fields'))

    def test_empty_returns_none(self):
        self.assertIsNone(self.r._reflog_row(0, ''))


class TestPorcelainParse(unittest.TestCase):
    """``_parse_porcelain_z`` turns NUL porcelain into ``[(XY, path)]``."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_one_sided_and_two_sided_codes(self):
        # Each NUL-terminated entry is 'XY<space><path>'. XY may carry a
        # space for one-sided changes; '??' is untracked.
        data = ('MM both.txt\x00'
                ' D tracked_del.txt\x00'
                ' M tracked_mod.txt\x00'
                'M  tracked_staged.txt\x00'
                'A  added.txt\x00'
                '?? untracked.txt\x00')
        self.assertEqual(self.r._parse_porcelain_z(data), [
            ('MM', 'both.txt'),
            (' D', 'tracked_del.txt'),
            (' M', 'tracked_mod.txt'),
            ('M ', 'tracked_staged.txt'),
            ('A ', 'added.txt'),
            ('??', 'untracked.txt'),
        ])

    def test_rename_uses_new_path_and_skips_old(self):
        # For a rename, '-z' emits the new path then a SECOND NUL field
        # carrying the old path; we keep the new path and drop the old.
        data = 'R  renamed_new.txt\x00renamed_old.txt\x00 M after.txt\x00'
        self.assertEqual(self.r._parse_porcelain_z(data), [
            ('R ', 'renamed_new.txt'),
            (' M', 'after.txt'),
        ])

    def test_copy_skips_old_path_too(self):
        data = 'C  copy_new.txt\x00copy_src.txt\x00'
        self.assertEqual(self.r._parse_porcelain_z(data),
                         [('C ', 'copy_new.txt')])

    def test_path_with_spaces_survives(self):
        # '-z' never quotes — a path with spaces is intact.
        data = ' M a file with spaces.txt\x00'
        self.assertEqual(self.r._parse_porcelain_z(data),
                         [(' M', 'a file with spaces.txt')])

    def test_empty_is_clean(self):
        self.assertEqual(self.r._parse_porcelain_z(''), [])


class TestStatusTag(unittest.TestCase):
    """``_status_tag`` chooses the one-letter status tag from ``XY``."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_staged_letter_wins(self):
        self.assertEqual(self.r._status_tag('M '), 'M')
        self.assertEqual(self.r._status_tag('A '), 'A')

    def test_worktree_letter_when_unstaged(self):
        self.assertEqual(self.r._status_tag(' M'), 'M')
        self.assertEqual(self.r._status_tag(' D'), 'D')

    def test_two_sided_prefers_staged(self):
        self.assertEqual(self.r._status_tag('MM'), 'M')
        self.assertEqual(self.r._status_tag('MD'), 'M')

    def test_untracked(self):
        self.assertEqual(self.r._status_tag('??'), '?')

    def test_question_mark_has_a_style(self):
        self.assertEqual(self.r._STATUS_LETTER_STYLE['?'], 'dim')


class TestStatusDiffPlan(unittest.TestCase):
    """``_status_diff_plan`` maps ``XY`` to the diff command(s) to run."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_staged_only(self):
        self.assertEqual(self.r._status_diff_plan('M ', 'f.txt'),
                         [('staged', ['diff', '--cached', '--', 'f.txt'])])

    def test_worktree_only(self):
        self.assertEqual(self.r._status_diff_plan(' M', 'f.txt'),
                         [('unstaged', ['diff', '--', 'f.txt'])])

    def test_both_sides(self):
        self.assertEqual(self.r._status_diff_plan('MM', 'f.txt'), [
            ('staged', ['diff', '--cached', '--', 'f.txt']),
            ('unstaged', ['diff', '--', 'f.txt']),
        ])

    def test_added_staged(self):
        self.assertEqual(self.r._status_diff_plan('A ', 'f.txt'),
                         [('staged', ['diff', '--cached', '--', 'f.txt'])])

    def test_deleted_worktree(self):
        self.assertEqual(self.r._status_diff_plan(' D', 'f.txt'),
                         [('unstaged', ['diff', '--', 'f.txt'])])

    def test_untracked_uses_no_index(self):
        self.assertEqual(
            self.r._status_diff_plan('??', 'f.txt'),
            [('untracked',
              ['diff', '--no-index', '--', '/dev/null', 'f.txt'])])


class TestStashIndex(unittest.TestCase):
    """``_stash_index`` extracts the 0-based index from a ``%gd`` selector."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_zero(self):
        # The index is a real int now (no longer the digit string).
        self.assertEqual(self.r._stash_index('stash@{0}'), 0)

    def test_double_digit(self):
        self.assertEqual(self.r._stash_index('stash@{12}'), 12)

    def test_non_index_selector(self):
        self.assertIsNone(self.r._stash_index('garbage'))
        self.assertIsNone(self.r._stash_index(''))


class TestStashRow(unittest.TestCase):
    """``_stash_row`` turns a ``%gd %cr %gs`` NUL record into a stash Item."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _record(self, selector, reldate, subject):
        return '\x00'.join([selector, reldate, subject])

    def test_full_record(self):
        item = self.r._stash_row(
            self._record('stash@{0}', '2 hours ago', 'WIP on main: abc init'))
        self.assertEqual(item.id, ('stash', 0))
        self.assertEqual(item.tag, 'stash@{0}')
        self.assertEqual(item.tag_style, 'yellow')
        self.assertEqual(item.title, 'WIP on main: abc init')
        self.assertTrue(item.has_children)
        self.assertEqual(item.chips, [('2 hours ago', 'dim')])

    def test_index_from_selector(self):
        item = self.r._stash_row(
            self._record('stash@{3}', '1 day ago', 'On main: hotfix'))
        # id keys on the index extracted from the selector, not enumeration.
        self.assertEqual(item.id, ('stash', 3))
        self.assertEqual(item.tag, 'stash@{3}')

    def test_malformed_returns_none(self):
        self.assertIsNone(self.r._stash_row('stash@{0}\x00only-two'))

    def test_bad_selector_returns_none(self):
        # A record whose selector has no extractable index is skipped.
        self.assertIsNone(
            self.r._stash_row('garbage\x002 hours ago\x00WIP'))

    def test_empty_returns_none(self):
        self.assertIsNone(self.r._stash_row(''))


class TestForEachRefParse(unittest.TestCase):
    """``_parse_for_each_ref_line`` classifies a ref by its full prefix."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _line(self, full, short):
        return f'{full}\x00{short}'

    def test_local_branch(self):
        self.assertEqual(
            self.r._parse_for_each_ref_line(
                self._line('refs/heads/main', 'main')),
            ('branch', 'main'))

    def test_local_branch_with_slash(self):
        # A slash-bearing local branch is a branch (kind from the prefix,
        # not the short name shape).
        self.assertEqual(
            self.r._parse_for_each_ref_line(
                self._line('refs/heads/feature/x', 'feature/x')),
            ('branch', 'feature/x'))

    def test_remote(self):
        self.assertEqual(
            self.r._parse_for_each_ref_line(
                self._line('refs/remotes/origin/main', 'origin/main')),
            ('remote', 'origin/main'))

    def test_tag(self):
        self.assertEqual(
            self.r._parse_for_each_ref_line(
                self._line('refs/tags/v1.0', 'v1.0')),
            ('tag', 'v1.0'))

    def test_kind_style_palette(self):
        # The recipe colors each kind word via _REF_KIND_STYLE.
        self.assertEqual(self.r._REF_KIND_STYLE['branch'], 'cyan')
        self.assertEqual(self.r._REF_KIND_STYLE['remote'], 'blue')
        self.assertEqual(self.r._REF_KIND_STYLE['tag'], 'yellow')

    def test_unknown_namespace_is_skipped(self):
        # e.g. refs/stash and the like aren't part of the three views.
        self.assertIsNone(
            self.r._parse_for_each_ref_line(self._line('refs/stash', 'stash')))

    def test_blank_and_malformed(self):
        self.assertIsNone(self.r._parse_for_each_ref_line(''))
        self.assertIsNone(
            self.r._parse_for_each_ref_line('refs/heads/main'))  # no NUL


# A representative ``git worktree list --porcelain`` dump: the main
# worktree first (on 'main'), a nested linked worktree on a slash-bearing
# branch, a sibling linked worktree, then a detached and a bare stanza.
# The bare stanza has NO ``HEAD`` line — real git omits it for a bare
# worktree (just ``worktree <path>`` + ``bare``), so ``head`` stays None.
_WT_PORCELAIN = (
    'worktree /repo\n'
    'HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'
    'branch refs/heads/main\n'
    '\n'
    'worktree /repo/.claude/worktrees/foo\n'
    'HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n'
    'branch refs/heads/feature/x\n'
    '\n'
    'worktree /sibling\n'
    'HEAD cccccccccccccccccccccccccccccccccccccccc\n'
    'branch refs/heads/release\n'
    '\n'
    'worktree /repo/.claude/worktrees/det\n'
    'HEAD dddddddddddddddddddddddddddddddddddddddd\n'
    'detached\n'
    '\n'
    'worktree /repo/bare\n'
    'bare\n'
)


class TestParseWorktreeList(unittest.TestCase):
    """``_parse_worktree_list`` turns porcelain stanzas into ``_Worktree``s."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_records_and_order(self):
        wts = self.r._parse_worktree_list(_WT_PORCELAIN)
        # One record per stanza, in emission order.
        self.assertEqual(
            [(w.path, w.branch, w.is_main) for w in wts],
            [('/repo', 'main', True),
             ('/repo/.claude/worktrees/foo', 'feature/x', False),
             ('/sibling', 'release', False),
             ('/repo/.claude/worktrees/det', None, False),
             ('/repo/bare', None, False)])

    def test_first_is_main_rest_are_linked(self):
        wts = self.r._parse_worktree_list(_WT_PORCELAIN)
        self.assertTrue(wts[0].is_main)
        self.assertFalse(any(w.is_main for w in wts[1:]))

    def test_head_sha_captured(self):
        first = self.r._parse_worktree_list(_WT_PORCELAIN)[0]
        self.assertEqual(first.head, 'a' * 40)

    def test_refs_heads_prefix_stripped(self):
        # branch is the short name, not the full refs/heads/... ref.
        foo = self.r._parse_worktree_list(_WT_PORCELAIN)[1]
        self.assertEqual(foo.branch, 'feature/x')

    def test_detached_and_bare_have_no_branch(self):
        wts = self.r._parse_worktree_list(_WT_PORCELAIN)
        self.assertIsNone(wts[3].branch)   # detached
        self.assertIsNone(wts[4].branch)   # bare

    def test_bare_worktree_has_no_head(self):
        # Real git emits no HEAD line for a bare worktree, so head is None
        # (a detached worktree still has its HEAD sha).
        wts = self.r._parse_worktree_list(_WT_PORCELAIN)
        self.assertEqual(wts[3].head, 'd' * 40)  # detached keeps its sha
        self.assertIsNone(wts[4].head)           # bare has none

    def test_empty_input_yields_no_records(self):
        self.assertEqual(self.r._parse_worktree_list(''), [])

    def test_trailing_stanza_without_blank_line(self):
        # A final stanza with no terminating blank line is still flushed.
        text = ('worktree /repo\nHEAD ' + 'a' * 40 + '\nbranch refs/heads/main')
        wts = self.r._parse_worktree_list(text)
        self.assertEqual(len(wts), 1)
        self.assertEqual((wts[0].path, wts[0].branch), ('/repo', 'main'))


class TestWorktrees(unittest.TestCase):
    """``_worktrees`` parses git output and degrades to [] on failure."""

    def setUp(self):
        self.r = _load_recipe()

    def test_parses_run_git_output(self):
        self.r._run_git = lambda *a: subprocess.CompletedProcess(
            a, 0, _WT_PORCELAIN, '')
        wts = self.r._worktrees()
        self.assertEqual([w.branch for w in wts],
                         ['main', 'feature/x', 'release', None, None])

    def test_git_failure_degrades_to_empty(self):
        # Non-zero rc (e.g. missing binary -> rc 127) -> [], never raises.
        self.r._run_git = lambda *a: subprocess.CompletedProcess(
            a, 127, '', 'git not found on PATH')
        self.assertEqual(self.r._worktrees(), [])


class TestWorktreeRelpath(unittest.TestCase):
    """``_worktree_relpath`` is os.path.relpath; main root -> '.'."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_main_root_maps_to_dot(self):
        self.assertEqual(self.r._worktree_relpath('/repo', '/repo'), '.')

    def test_nested_worktree_is_relative(self):
        self.assertEqual(
            self.r._worktree_relpath('/repo/.claude/worktrees/foo', '/repo'),
            '.claude/worktrees/foo')

    def test_sibling_worktree_uses_dotdot(self):
        self.assertEqual(
            self.r._worktree_relpath('/sibling', '/repo'), '../sibling')


class TestBranchesRootWorktreeChips(unittest.TestCase):
    """``_branches_root`` chips linked-worktree branches with their relpath."""

    def setUp(self):
        self.r = _load_recipe()

    def _stub_git(self, for_each_ref, worktree_porcelain):
        """Stub ``_run_git`` to answer for-each-ref vs worktree-list."""
        def fake(*args):
            if 'for-each-ref' in args:
                return subprocess.CompletedProcess(args, 0, for_each_ref, '')
            if 'worktree' in args:
                return subprocess.CompletedProcess(
                    args, 0, worktree_porcelain, '')
            return subprocess.CompletedProcess(args, 1, '', '')
        self.r._run_git = fake

    @staticmethod
    def _ref_line(full, short):
        return f'{full}\x00{short}'

    def _chips(self, item):
        return getattr(item, 'chips', None)

    def test_linked_branch_gets_relpath_chip(self):
        # main on 'main'; a linked worktree checks out 'feature/x'.
        refs = '\n'.join([
            self._ref_line('refs/heads/main', 'main'),
            self._ref_line('refs/heads/feature/x', 'feature/x'),
        ])
        self._stub_git(refs, _WT_PORCELAIN)
        by_id = {it.id: it for it in self.r._branches_root()}
        # The linked branch carries one magenta chip = its relpath.
        self.assertEqual(self._chips(by_id[('ref', 'feature/x')]),
                         [('.claude/worktrees/foo', 'magenta')])
        # The main worktree's branch is checked out here -> no path chip.
        self.assertIn(self._chips(by_id[('ref', 'main')]), (None, []))

    def test_chip_style_distinct_from_ref_kind_palette(self):
        # The worktree path chip uses 'magenta' — distinct from the
        # cyan/blue/yellow branch/remote/tag chip palette.
        self.assertNotIn('magenta', self.r._REF_KIND_STYLE.values())

    def test_unchecked_branch_has_no_chip(self):
        # 'idle' isn't checked out anywhere -> no path chip.
        refs = '\n'.join([
            self._ref_line('refs/heads/main', 'main'),
            self._ref_line('refs/heads/idle', 'idle'),
        ])
        self._stub_git(refs, _WT_PORCELAIN)
        by_id = {it.id: it for it in self.r._branches_root()}
        self.assertIn(self._chips(by_id[('ref', 'idle')]), (None, []))

    def test_remote_and_tag_never_get_path_chip(self):
        # Even if a remote/tag short-name collides with a linked branch
        # name, only the 'branch' row is chipped.
        refs = '\n'.join([
            self._ref_line('refs/remotes/origin/feature/x', 'origin/feature/x'),
            self._ref_line('refs/tags/feature/x', 'feature/x'),
            self._ref_line('refs/heads/feature/x', 'feature/x'),
        ])
        self._stub_git(refs, _WT_PORCELAIN)
        items = self.r._branches_root()
        remote = next(it for it in items if it.tag == 'remote')
        tag = next(it for it in items if it.tag == 'tag')
        branch = next(it for it in items if it.tag == 'branch')
        self.assertIn(self._chips(remote), (None, []))
        self.assertIn(self._chips(tag), (None, []))
        self.assertEqual(self._chips(branch),
                         [('.claude/worktrees/foo', 'magenta')])

    def test_sibling_worktree_relpath_uses_dotdot(self):
        # 'release' is checked out in a sibling worktree (/sibling).
        refs = self._ref_line('refs/heads/release', 'release')
        self._stub_git(refs, _WT_PORCELAIN)
        item, = self.r._branches_root()
        self.assertEqual(self._chips(item), [('../sibling', 'magenta')])

    def test_worktree_list_failure_means_no_chips(self):
        # for-each-ref succeeds but worktree list fails -> branches still
        # render, just without any path chips (graceful degrade).
        def fake(*args):
            if 'for-each-ref' in args:
                return subprocess.CompletedProcess(
                    args, 0, self._ref_line('refs/heads/feature/x', 'feature/x'),
                    '')
            return subprocess.CompletedProcess(args, 1, '', 'boom')
        self.r._run_git = fake
        item, = self.r._branches_root()
        self.assertIn(self._chips(item), (None, []))


class TestWorktreesRoot(unittest.TestCase):
    """``_worktrees_root`` = one ``('worktree', abspath, label)`` row each."""

    def setUp(self):
        self.r = _load_recipe()

    def _stub_worktrees(self):
        # Feed the shared porcelain fixture (main + nested/slash branch +
        # sibling + detached + bare) through the real parser.
        wts = self.r._parse_worktree_list(_WT_PORCELAIN)
        self.r._worktrees = lambda: wts
        return wts

    def test_main_first_relpath_dot_and_branch_label(self):
        self._stub_worktrees()
        items = self.r._worktrees_root()
        # Main worktree leads, titled '. <branch>' with the main tag.
        self.assertEqual(items[0].title, '. main')
        self.assertEqual(items[0].id, ('worktree', '/repo', 'main'))
        self.assertEqual(items[0].tag, 'main')
        self.assertTrue(items[0].has_children)

    def test_linked_worktree_relpath_and_id_shape(self):
        self._stub_worktrees()
        items = self.r._worktrees_root()
        # The nested linked worktree: '<relpath> <branch>', id carries the
        # absolute path + branch label, tagged linked.
        foo = items[1]
        self.assertEqual(foo.title, '.claude/worktrees/foo feature/x')
        self.assertEqual(
            foo.id, ('worktree', '/repo/.claude/worktrees/foo', 'feature/x'))
        self.assertEqual(foo.tag, 'linked')
        # A sibling worktree's relpath uses '..'.
        sibling = items[2]
        self.assertEqual(sibling.title, '../sibling release')

    def test_detached_worktree_uses_short_head_sha_label_and_drills(self):
        self._stub_worktrees()
        items = self.r._worktrees_root()
        # Detached worktree: no branch -> short HEAD sha (head[:7]) is the
        # label, in the title AND as the drill rev in the id; still drillable.
        det = items[3]
        self.assertEqual(det.title, '.claude/worktrees/det ddddddd')
        self.assertEqual(
            det.id, ('worktree', '/repo/.claude/worktrees/det', 'ddddddd'))
        self.assertTrue(det.has_children)

    def test_bare_worktree_labelled_bare_and_is_leaf(self):
        # A bare worktree has no HEAD line (head=None) and no branch: it must
        # NOT crash on head[:7], renders as '<relpath> (bare)', and — having
        # nothing to log — is a non-drillable leaf.
        self._stub_worktrees()
        items = self.r._worktrees_root()   # must not raise
        bare = items[4]
        self.assertEqual(bare.title, 'bare (bare)')
        self.assertEqual(bare.id, ('worktree', '/repo/bare', '(bare)'))
        self.assertFalse(bare.has_children)

    def test_all_worktrees_emitted_in_order(self):
        wts = self._stub_worktrees()
        items = self.r._worktrees_root()
        self.assertEqual(len(items), len(wts))
        self.assertEqual([it.id[1] for it in items], [w.path for w in wts])

    def test_empty_worktrees_yields_error_row(self):
        # git failure -> _worktrees() returns [] -> a single error row, no crash.
        self.r._worktrees = lambda: []
        items = self.r._worktrees_root()
        self.assertEqual([it.id for it in items], [('err',)])


class TestInitialCursorId(unittest.TestCase):
    """``_initial_cursor_id`` targets the checked-out row per mode, else None.

    Stubs ``_run_git`` (rev-parse) / ``_worktrees`` and sets ``_mode`` /
    ``_STDIN_KIND`` on the freshly loaded module; the returned id must match
    the row id shape each builder emits (full sha for commits, short name
    for branches, ``('worktree', path, label)`` for the main worktree).
    """

    def setUp(self):
        self.r = _load_recipe()
        self.r._STDIN_KIND = None

    def _stub_rev_parse(self, out, rc=0):
        # Stub _run_git so any rev-parse returns ``out`` (rc ``rc``); other
        # git calls would fail (unused by this helper's path).
        def fake(*args):
            if args and args[0] == 'rev-parse':
                return subprocess.CompletedProcess(args, rc, out, '')
            return subprocess.CompletedProcess(args, 1, '', '')
        self.r._run_git = fake

    # ---- commits -------------------------------------------------------
    def test_commits_returns_full_head_sha_commit_id(self):
        self.r._mode = 'commits'
        full = 'a' * 40
        self._stub_rev_parse(full + '\n')           # rev-parse HEAD -> full sha
        self.assertEqual(self.r._initial_cursor_id(), ('commit', full))

    def test_commits_none_when_rev_parse_fails(self):
        self.r._mode = 'commits'
        self._stub_rev_parse('', rc=128)            # e.g. unborn HEAD / gitless
        self.assertIsNone(self.r._initial_cursor_id())

    def test_commits_none_when_sha_blank(self):
        self.r._mode = 'commits'
        self._stub_rev_parse('   \n')               # rc 0 but empty output
        self.assertIsNone(self.r._initial_cursor_id())

    # ---- branches ------------------------------------------------------
    def test_branches_returns_current_branch_ref_id(self):
        self.r._mode = 'branches'
        self._stub_rev_parse('main\n')              # --abbrev-ref HEAD
        self.assertEqual(self.r._initial_cursor_id(), ('ref', 'main'))

    def test_branches_none_when_detached(self):
        self.r._mode = 'branches'
        self._stub_rev_parse('HEAD\n')              # detached -> literal 'HEAD'
        self.assertIsNone(self.r._initial_cursor_id())

    def test_branches_none_when_rev_parse_fails(self):
        self.r._mode = 'branches'
        self._stub_rev_parse('', rc=128)
        self.assertIsNone(self.r._initial_cursor_id())

    # ---- worktrees -----------------------------------------------------
    def test_worktrees_returns_main_row_id_with_label(self):
        self.r._mode = 'worktrees'
        # Reuse the shared porcelain: main is /repo on branch 'main'. The id
        # must equal _worktrees_root's main row id exactly.
        wts = self.r._parse_worktree_list(_WT_PORCELAIN)
        self.r._worktrees = lambda: wts
        self.assertEqual(self.r._initial_cursor_id(),
                         ('worktree', '/repo', 'main'))
        self.assertEqual(self.r._initial_cursor_id(),
                         self.r._worktrees_root()[0].id)

    def test_worktrees_detached_main_uses_short_head_label(self):
        # A detached main worktree (no branch) -> the label is head[:7], so
        # the id carries the short HEAD sha — matching _worktree_label.
        self.r._mode = 'worktrees'
        self.r._worktrees = lambda: [
            self.r._Worktree('/repo', 'e' * 40, None, True)]
        self.assertEqual(self.r._initial_cursor_id(),
                         ('worktree', '/repo', 'eeeeeee'))

    def test_worktrees_none_when_empty(self):
        self.r._mode = 'worktrees'
        self.r._worktrees = lambda: []              # git worktree list failed
        self.assertIsNone(self.r._initial_cursor_id())

    # ---- modes with no checked-out concept -----------------------------
    def test_status_reflog_stash_return_none(self):
        # No rev-parse / _worktrees should even be consulted for these.
        self.r._run_git = lambda *a: (_ for _ in ()).throw(
            AssertionError('_run_git must not be called'))
        self.r._worktrees = lambda: (_ for _ in ()).throw(
            AssertionError('_worktrees must not be called'))
        for mode in ('status', 'reflog', 'stash'):
            self.r._mode = mode
            self.assertIsNone(self.r._initial_cursor_id(), mode)

    # ---- stdin mode short-circuits before any mode check ---------------
    def test_stdin_mode_returns_none_regardless_of_mode(self):
        self.r._run_git = lambda *a: (_ for _ in ()).throw(
            AssertionError('_run_git must not be called in stdin mode'))
        for kind in ('diff', 'log', 'status'):
            self.r._STDIN_KIND = kind
            for mode in ('commits', 'branches', 'worktrees'):
                self.r._mode = mode
                self.assertIsNone(self.r._initial_cursor_id(), (kind, mode))


class TestGetChildrenWorktree(unittest.TestCase):
    """``get_children(('worktree', ...))`` drills the label via ``_log_items``."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._paths = []

    def test_branch_worktree_drills_its_branch_rev(self):
        captured = {}
        self.r._log_items = (
            lambda revs, paths, ns: captured.update(revs=revs, ns=ns) or [])
        item_id = ('worktree', '/repo/.claude/worktrees/foo', 'feature/x')
        self.r.get_children(item_id)
        # The label (branch short-name) is the sole rev; the id is the ns so
        # two drilled-in worktrees don't share a filler counter.
        self.assertEqual(captured['revs'], ['feature/x'])
        self.assertEqual(captured['ns'], item_id)

    def test_detached_worktree_drills_its_head_sha(self):
        captured = {}
        self.r._log_items = (
            lambda revs, paths, ns: captured.update(revs=revs) or [])
        # A detached worktree's label is its short HEAD sha — a valid rev.
        self.r.get_children(('worktree', '/repo/.claude/worktrees/det', 'ddddddd'))
        self.assertEqual(captured['revs'], ['ddddddd'])


class TestCommitLogItems(unittest.TestCase):
    """``_commit_log_items`` stores sha/author/date columns, no sha tag."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._log_limit = 1000

    def _stub_git_log(self, records):
        """Stub ``_run_git`` so ``log`` returns ``records`` (NUL fields).

        ``remote`` returns empty (no remotes) so ``_parse_decorations``
        classifies slash refs as local branches without shelling out.
        """
        out = '\n'.join('\x00'.join(rec) for rec in records)

        def fake_run_git(*args):
            if args and args[0] == 'log':
                return subprocess.CompletedProcess(args, 0, out, '')
            if args and args[0] == 'remote':
                return subprocess.CompletedProcess(args, 0, '', '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git

    def test_columns_stored_and_no_sha_tag(self):
        # A commit with a HEAD -> main decoration: the row stores the
        # column display strings, sets no tag, and chips are the %D
        # decorations only (no trailing author·date chip).
        self._stub_git_log([
            ('deadbeefcafe1234567890abcdef000000000000',
             'HEAD -> main', 'Alice', '2 days ago', 'first subject'),
        ])
        items = self.r._commit_log_items([], [])
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it.id,
                         ('commit', 'deadbeefcafe1234567890abcdef000000000000'))
        self.assertEqual(it.title, 'first subject')
        self.assertTrue(it.has_children)

        # Sha / author / date are columns now.
        self.assertEqual(it.col_sha, 'deadbee')  # short sha (7)
        self.assertEqual(it.col_author, 'Alice')
        self.assertEqual(it.col_date, '2 days ago')

        # The sha no longer lives in the tag chip; no tag is set at all.
        self.assertEqual(getattr(it, 'tag', ''), '')
        self.assertEqual(getattr(it, 'tag_style', ''), '')

        # chips are ONLY the %D decorations — the dim author·date chip is
        # gone (author/date are columns now).
        self.assertEqual(it.chips, [('HEAD', 'green'), ('main', 'cyan')])

    def test_no_decoration_yields_empty_chips(self):
        # A bare commit (empty %D) carries no chips at all.
        self._stub_git_log([
            ('0123456789abcdef0123456789abcdef01234567',
             '', 'Bob', '5 minutes ago', 'plain subject'),
        ])
        it = self.r._commit_log_items([], [])[0]
        self.assertEqual(it.col_sha, '0123456')
        self.assertEqual(it.col_author, 'Bob')
        self.assertEqual(it.col_date, '5 minutes ago')
        self.assertEqual(it.chips, [])
        self.assertEqual(getattr(it, 'tag', ''), '')

    def test_log_failure_returns_error_row(self):
        # A non-zero git log still yields a single error Item (unchanged).
        def fake_run_git(*args):
            return subprocess.CompletedProcess(args, 1, '', 'boom')

        self.r._run_git = fake_run_git
        items = self.r._commit_log_items([], [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, ('err',))
        # The error row has no col_sha → git_row_content falls back for it.
        self.assertIsNone(getattr(items[0], 'col_sha', None))


class TestGitRowContent(unittest.TestCase):
    """``git_row_content`` builds padded columns with the subject last."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _widths(self, sha=7, author=5, date=12):
        return {'col_sha': sha, 'col_author': author, 'col_date': date}

    def _commit_item(self, **kw):
        defaults = dict(id=('commit', 'deadbee'), title='subj',
                        col_sha='deadbee', col_author='Alice',
                        col_date='2 days ago', chips=[])
        defaults.update(kw)
        return self.r.Item(**defaults)

    def test_columns_padded_then_subject_last(self):
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self._commit_item(
            col_sha='deadbee', col_author='Al', col_date='2 days ago',
            title='the subject', chips=[])
        segs = self.r.git_row_content(item, ctx)

        # Three column segments + the subject (no chips here).
        self.assertEqual(len(segs), 4)
        sha_seg, author_seg, date_seg, subject_seg = segs

        # sha column: yellow, left-justified to width 7 + 2-space gap.
        self.assertEqual(sha_seg, ('deadbee' + '  ', _YELLOW[0], _YELLOW[1]))
        # author column: dim, left-justified to width 5 ('Al' -> 'Al   ').
        self.assertEqual(author_seg, ('Al   ' + '  ', _DIM[0], _DIM[1]))
        # date column: dim, left-justified to width 12.
        self.assertEqual(date_seg,
                         ('2 days ago  ' + '  ', _DIM[0], _DIM[1]))

        # Subject comes LAST, plain (no fg, not bold) so a narrow pane
        # truncates the subject rather than the metadata columns.
        self.assertEqual(subject_seg, ('the subject', None, False))

        # Widths sourced from max_col_width per column field, in order.
        self.assertEqual(ctx.calls, ['col_sha', 'col_author', 'col_date'])

    def test_decoration_chips_between_date_and_subject(self):
        # The %D decorations render as ``[text] `` segments after the date
        # column and before the subject, styled by name.
        ctx = _FakeCtx(self._widths())
        item = self._commit_item(
            title='decorated',
            chips=[('HEAD', 'green'), ('main', 'cyan')])
        segs = self.r.git_row_content(item, ctx)
        # 3 columns + 2 chips + subject.
        self.assertEqual(len(segs), 6)
        head_seg, branch_seg = segs[3], segs[4]
        self.assertEqual(head_seg, ('[HEAD] ', *self.r.style('green')))
        self.assertEqual(branch_seg, ('[main] ', *self.r.style('cyan')))
        # Subject is still last.
        self.assertEqual(segs[-1], ('decorated', None, False))

    def test_rows_align_across_differing_lengths(self):
        # Two commits whose raw sha/author/date differ in length must, once
        # padded to the per-column max, yield equal segment widths.
        widths = self._widths(sha=7, author=7, date=12)
        a = self._commit_item(
            col_sha='abc1234', col_author='Al', col_date='2 days ago',
            title='a', chips=[])
        b = self._commit_item(
            col_sha='def5678', col_author='Bernard', col_date='3 weeks ago',
            title='bbbb', chips=[])
        segs_a = self.r.git_row_content(a, _FakeCtx(widths))
        segs_b = self.r.git_row_content(b, _FakeCtx(widths))
        # Per metadata column (sha/author/date → indices 0/1/2) the text
        # length is identical across the two rows.
        for col in range(3):
            self.assertEqual(len(segs_a[col][0]), len(segs_b[col][0]),
                             f'column {col} widths differ between rows')
        # Concrete widths: column field width + 2-space gap.
        self.assertEqual(len(segs_a[0][0]), 7 + 2)
        self.assertEqual(len(segs_a[1][0]), 7 + 2)
        self.assertEqual(len(segs_a[2][0]), 12 + 2)

    def test_worktree_group_row_aligns_label_under_subject(self):
        # A synthetic worktree-group row carries EMPTY column strings, so it
        # stays on the column path (not the fallback) and pads the three
        # leading columns to the commit widths — the label then begins at
        # the same offset as a commit subject (no decoration chips).
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self.r.Item(id=('wc', 'untracked'), title='Untracked changes',
                           col_sha='', col_author='', col_date='',
                           has_children=True)
        segs = self.r.git_row_content(item, ctx)
        # Three padded (empty) columns + the label, no chips.
        self.assertEqual(len(segs), 4)
        # Each column is just its gap-padded width of spaces.
        self.assertEqual(segs[0], (' ' * 7 + '  ', _YELLOW[0], _YELLOW[1]))
        self.assertEqual(segs[1], (' ' * 5 + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(segs[2], (' ' * 12 + '  ', _DIM[0], _DIM[1]))
        # The label is the last (subject) segment, plain — same slot a
        # commit subject occupies, so they line up vertically.
        self.assertEqual(segs[-1], ('Untracked changes', None, False))
        # Leading text width matches a commit's three columns exactly.
        commit = self._commit_item(
            col_sha='deadbee', col_author='Al', col_date='2 days ago',
            title='subj', chips=[])
        csegs = self.r.git_row_content(commit, _FakeCtx(
            self._widths(sha=7, author=5, date=12)))
        lead = lambda s: sum(len(seg[0]) for seg in s[:3])
        self.assertEqual(lead(segs), lead(csegs))

    def test_non_commit_row_falls_back(self):
        # A status/stash/ref/file row (no col_sha) must return EXACTLY
        # default_row_content(item, ctx) and never measure a column.
        ctx = _FakeCtx(self._widths())
        item = self.r.Item(id=('status', 'M ', 'beta.txt'), title='beta.txt',
                           tag='M', tag_style='yellow', has_children=False)
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(segs,
                         [('DEFAULT', ('status', 'M ', 'beta.txt'), 'beta.txt')])
        # The fallback path must not measure columns.
        self.assertEqual(ctx.calls, [])

    def test_explicit_none_col_sha_also_falls_back(self):
        # Defensive: col_sha present but None still takes the fallback.
        ctx = _FakeCtx(self._widths())
        item = self.r.Item(id=('err',), title='boom', col_sha=None)
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(ctx.calls, [])


class TestIsConflict(unittest.TestCase):
    """``_is_conflict`` flags the seven porcelain unmerged ``XY`` codes."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_all_unmerged_codes_are_conflicts(self):
        for xy in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'):
            self.assertTrue(self.r._is_conflict(xy), xy)

    def test_non_conflict_codes(self):
        for xy in ('MM', 'M ', ' M', '??', 'A ', ' D', 'R '):
            self.assertFalse(self.r._is_conflict(xy), xy)


class TestClassifyWorktree(unittest.TestCase):
    """``_classify_worktree`` buckets ``(XY, path)`` rows by group."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_conflict_codes_are_exclusive(self):
        # A conflict row lands ONLY in conflicts — never staged/tracked,
        # even though e.g. 'AA'/'DD' have both columns set.
        for xy in ('UU', 'AA', 'DD'):
            buckets = self.r._classify_worktree([(xy, 'c.txt')])
            self.assertEqual(buckets['conflicts'], [(xy, 'c.txt')], xy)
            self.assertEqual(buckets['staged'], [], xy)
            self.assertEqual(buckets['tracked'], [], xy)
            self.assertEqual(buckets['untracked'], [], xy)

    def test_two_sided_code_is_both_staged_and_tracked(self):
        buckets = self.r._classify_worktree([('MM', 'both.txt')])
        self.assertEqual(buckets['staged'], [('MM', 'both.txt')])
        self.assertEqual(buckets['tracked'], [('MM', 'both.txt')])
        self.assertEqual(buckets['untracked'], [])
        self.assertEqual(buckets['conflicts'], [])

    def test_untracked_only(self):
        buckets = self.r._classify_worktree([('??', 'new.txt')])
        self.assertEqual(buckets['untracked'], [('??', 'new.txt')])
        self.assertEqual(buckets['staged'], [])
        self.assertEqual(buckets['tracked'], [])
        self.assertEqual(buckets['conflicts'], [])

    def test_staged_only(self):
        buckets = self.r._classify_worktree([('M ', 's.txt')])
        self.assertEqual(buckets['staged'], [('M ', 's.txt')])
        self.assertEqual(buckets['tracked'], [])
        self.assertEqual(buckets['untracked'], [])
        self.assertEqual(buckets['conflicts'], [])

    def test_tracked_only(self):
        buckets = self.r._classify_worktree([(' M', 'w.txt')])
        self.assertEqual(buckets['tracked'], [(' M', 'w.txt')])
        self.assertEqual(buckets['staged'], [])
        self.assertEqual(buckets['untracked'], [])
        self.assertEqual(buckets['conflicts'], [])

    def test_empty_input_all_buckets_empty(self):
        buckets = self.r._classify_worktree([])
        self.assertEqual(buckets, {
            'untracked': [],
            'tracked': [],
            'staged': [],
            'conflicts': [],
        })


class TestStatusDiffPlanConflict(unittest.TestCase):
    """``_status_diff_plan`` shows one combined diff for unmerged codes."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_each_unmerged_code_yields_single_conflict_diff(self):
        for xy in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'):
            self.assertEqual(
                self.r._status_diff_plan(xy, 'f.txt'),
                [('conflict', ['diff', '--', 'f.txt'])], xy)


class TestUnmergedTagStyle(unittest.TestCase):
    """Unmerged status letters resolve to a styled tag."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_u_letter_has_a_style(self):
        self.assertIn('U', self.r._STATUS_LETTER_STYLE)
        self.assertTrue(self.r._STATUS_LETTER_STYLE['U'])

    def test_existing_conflict_letters_still_styled(self):
        # 'AA'->'A', 'DD'->'D', 'UU'->'U' all resolve to a non-empty style.
        for xy, letter in (('AA', 'A'), ('DD', 'D'), ('UU', 'U')):
            self.assertEqual(self.r._status_tag(xy), letter, xy)
            self.assertTrue(self.r._STATUS_LETTER_STYLE[letter], letter)


class TestWorktreeGroups(unittest.TestCase):
    """``_worktree_groups`` emits one expandable row per non-empty bucket."""

    def setUp(self):
        self.r = _load_recipe()

    def test_ordering_and_labels(self):
        # All four buckets non-empty: rows follow _WC_GROUPS order, ids are
        # ('wc', bucket), titles are the group labels, all expandable.
        self.r._worktree_status = lambda paths: [
            ('??', 'new.txt'),
            (' M', 'w.txt'),
            ('M ', 's.txt'),
            ('UU', 'c.txt'),
        ]
        items = self.r._worktree_groups([])
        self.assertEqual(
            [(it.id, it.title) for it in items],
            [(('wc', 'untracked'), 'Untracked changes'),
             (('wc', 'tracked'), 'Tracked changes'),
             (('wc', 'staged'), 'Staged changes'),
             (('wc', 'conflicts'), 'Conflicts')])
        self.assertTrue(all(it.has_children for it in items))

    def test_rows_carry_empty_alignment_columns(self):
        # Each row leaves col_sha/col_author/col_date empty so
        # git_row_content aligns the label under the commit subjects.
        self.r._worktree_status = lambda paths: [('??', 'new.txt')]
        item, = self.r._worktree_groups([])
        self.assertEqual(
            (item.col_sha, item.col_author, item.col_date), ('', '', ''))

    def test_only_non_empty_buckets_appear(self):
        # Only untracked + staged have files → only those two rows, in order.
        self.r._worktree_status = lambda paths: [
            ('??', 'new.txt'),
            ('M ', 's.txt'),
        ]
        items = self.r._worktree_groups([])
        self.assertEqual([it.id for it in items],
                         [('wc', 'untracked'), ('wc', 'staged')])

    def test_clean_tree_yields_no_rows(self):
        # A clean tree (status → []) produces no synthetic rows at all.
        self.r._worktree_status = lambda paths: []
        self.assertEqual(self.r._worktree_groups([]), [])

    def test_groups_constant_shape(self):
        # _WC_GROUPS defines BOTH order and labels for the four buckets.
        self.assertEqual(self.r._WC_GROUPS, [
            ('untracked', 'Untracked changes'),
            ('tracked', 'Tracked changes'),
            ('staged', 'Staged changes'),
            ('conflicts', 'Conflicts'),
        ])


class TestCommitsRootWorktreeScope(unittest.TestCase):
    """``_commits_root`` prepends worktree rows ONLY for a clean (no-rev) log."""

    def setUp(self):
        self.r = _load_recipe()
        # Worktree-row prepending is tree-independent; pin tree mode off
        # (default is now on) so the mocked plain ``_commit_log_items``
        # seam is the active builder and the assertions stay deterministic.
        self.r._tree_mode = False

    def test_revs_suppress_worktree_rows(self):
        # A positional rev makes the log historical — no live wc rows.
        self.r._revs = ['HEAD~1']
        self.r._paths = []
        sentinel = self.r.Item(id=('commit', 'sentinel'), title='s',
                               has_children=True)
        self.r._commit_log_items = lambda revs, paths: [sentinel]
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        items = self.r._commits_root()
        ids = [getattr(it, 'id', None) for it in items]
        self.assertIn(('commit', 'sentinel'), ids)
        self.assertFalse(
            any(isinstance(i, tuple) and i[0] == 'wc' for i in ids))

    def test_no_revs_prepends_worktree_rows(self):
        # With no rev, the wc rows appear BEFORE the commit rows.
        self.r._revs = []
        self.r._paths = []
        sentinel = self.r.Item(id=('commit', 'sentinel'), title='s',
                               has_children=True)
        self.r._commit_log_items = lambda revs, paths: [sentinel]
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        items = self.r._commits_root()
        ids = [getattr(it, 'id', None) for it in items]
        self.assertEqual(ids, [('wc', 'staged'), ('commit', 'sentinel')])


class TestCommitsRootAllBranches(unittest.TestCase):
    """``--all`` (``_all_branches``) spans the commits log over every branch."""

    def setUp(self):
        self.r = _load_recipe()
        # Pin tree mode off so the plain ``_commit_log_items`` seam is the
        # active builder and the captured revs stay deterministic.
        self.r._tree_mode = False

    def test_appends_branches_rev_and_suppresses_worktree_rows(self):
        # With no positional rev, --all spans the log over all local branches
        # by passing ``--branches`` to git log — and, like passing branch
        # names positionally, the live worktree rows are suppressed.
        self.r._revs = []
        self.r._paths = []
        self.r._all_branches = True
        captured = {}
        sentinel = self.r.Item(id=('commit', 'sentinel'), title='s',
                               has_children=True)

        def fake_log(revs, paths):
            captured['revs'] = list(revs)
            return [sentinel]

        self.r._commit_log_items = fake_log
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        items = self.r._commits_root()
        self.assertEqual(captured['revs'], ['--branches'])
        ids = [getattr(it, 'id', None) for it in items]
        self.assertEqual(ids, [('commit', 'sentinel')])

    def test_unions_with_positional_revs(self):
        # An explicit rev plus --all → both reach git log (git log <rev>
        # --branches), matching ``browse-git <rev> $(git branch …)``.
        self.r._revs = ['HEAD~3']
        self.r._paths = []
        self.r._all_branches = True
        captured = {}

        def fake_log(revs, paths):
            captured['revs'] = list(revs)
            return []

        self.r._commit_log_items = fake_log
        self.r._commits_root()
        self.assertEqual(captured['revs'], ['HEAD~3', '--branches'])

    def test_off_by_default_no_branches_rev(self):
        # Without --all the revs reach git log untouched (no ``--branches``).
        self.r._revs = []
        self.r._paths = []
        captured = {}

        def fake_log(revs, paths):
            captured['revs'] = list(revs)
            return []

        self.r._commit_log_items = fake_log
        self.r._worktree_status = lambda paths: []
        self.r._commits_root()
        self.assertEqual(captured['revs'], [])


class TestWorktreeFiles(unittest.TestCase):
    """``_worktree_files`` returns one bucket's files as ``status:`` leaves."""

    def setUp(self):
        self.r = _load_recipe()

    def test_returns_only_that_buckets_files(self):
        self.r._worktree_status = lambda paths: [
            ('M ', 's.txt'),
            (' M', 'w.txt'),
            ('??', 'new.txt'),
        ]
        items = self.r._worktree_files('staged', [])
        self.assertEqual([it.id for it in items], [('status', 'M ', 's.txt')])
        it = items[0]
        self.assertEqual(it.title, 's.txt')
        self.assertEqual(it.tag, 'M')
        self.assertEqual(it.tag_style, 'yellow')
        self.assertFalse(it.has_children)

    def test_unknown_bucket_is_empty(self):
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        self.assertEqual(self.r._worktree_files('nope', []), [])

    def test_empty_bucket_is_empty(self):
        self.r._worktree_status = lambda paths: [('M ', 's.txt')]
        self.assertEqual(self.r._worktree_files('conflicts', []), [])


class TestWorktreePreviewStat(unittest.TestCase):
    """``_worktree_preview`` summarises a bucket with ``--stat`` (like a commit).

    The per-file diff now lives on the child ``('status', …)`` leaves, so the
    bucket row previews a stat summary — git-colored but NOT piped through
    ``delta`` (a diff renderer), exactly like ``_commit_preview``.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.calls = []

        def fake_git_color(*args):
            self.calls.append(args)
            return 'STAT\n'

        self.r._git_color = fake_git_color
        # Mark delta so a test can prove the stat is NOT routed through it.
        self.r._colorize_diff = lambda raw: 'DELTA(' + raw + ')'

    def _set_status(self, rows):
        self.r._worktree_status = lambda paths: rows

    def test_staged_uses_diff_cached_stat(self):
        self._set_status([('M ', 'a.txt')])
        out = self.r._worktree_preview('staged', [])
        self.assertEqual(self.calls,
                         [('diff', '--cached', '--stat', '--', 'a.txt')])
        self.assertEqual(out, 'STAT\n')        # raw stat …
        self.assertNotIn('DELTA', out)         # … not piped through delta

    def test_tracked_uses_diff_stat(self):
        self._set_status([(' M', 'b.txt')])
        out = self.r._worktree_preview('tracked', [])
        self.assertEqual(self.calls, [('diff', '--stat', '--', 'b.txt')])
        self.assertNotIn('DELTA', out)

    def test_conflicts_uses_diff_stat(self):
        self._set_status([('UU', 'c.txt')])
        out = self.r._worktree_preview('conflicts', [])
        self.assertEqual(self.calls, [('diff', '--stat', '--', 'c.txt')])
        self.assertNotIn('DELTA', out)

    def test_empty_bucket_is_empty(self):
        self._set_status([])
        self.assertEqual(self.r._worktree_preview('staged', []), '')
        self.assertEqual(self.calls, [])


class TestUntrackedStat(unittest.TestCase):
    """Untracked bucket combines per-file stats into one cohesive summary."""

    def setUp(self):
        self.r = _load_recipe()

    def test_strips_prefix_aligns_and_collapses_footers(self):
        # Untracked files have no index entry, so each is statted alone via
        # diff --no-index; git emits a `/dev/null =>` rename prefix and its
        # own footer per file (a single git call would never split them).
        # DIFFERENT name lengths so the `|` re-alignment is exercised.
        per_file = {
            'a.txt': (' /dev/null => a.txt | 3 \x1b[32m+++\x1b[m\n'
                      ' 1 file changed, 3 insertions(+)\n'),
            'longer.txt': (' /dev/null => longer.txt | 1 \x1b[32m+\x1b[m\n'
                           ' 1 file changed, 1 insertion(+)\n'),
        }

        def fake_git_color(*args):
            return per_file[args[-1]]  # last arg is the path

        self.r._git_color = fake_git_color
        self.r._worktree_status = lambda paths: [('??', 'a.txt'),
                                                 ('??', 'longer.txt')]
        out = self.r._worktree_preview('untracked', [])
        lines = out.splitlines()
        self.assertNotIn('/dev/null =>', out)            # rename arrow gone
        self.assertIn('\x1b[32m', out)                   # colored bars kept
        # Both files listed; their `|` separators line up in one column
        # (shorter path padded) so it reads like a single git --stat.
        sep_cols = [ln.index('|') for ln in lines if '|' in ln]
        self.assertEqual(len(sep_cols), 2)
        self.assertEqual(sep_cols[0], sep_cols[1])
        self.assertTrue(any('a.txt' in ln for ln in lines))
        self.assertTrue(any('longer.txt' in ln for ln in lines))
        # The two per-file footers collapse into ONE combined footer.
        footers = [ln for ln in lines if 'changed' in ln]
        self.assertEqual(footers, [' 2 files changed, 4 insertions(+)'])


@unittest.skipUnless(shutil.which('git'), 'git not available')
class TestUntrackedStatRealGit(unittest.TestCase):
    """End-to-end guard: real ``git diff --no-index --stat`` for untracked.

    The stubbed ``TestUntrackedStat`` bakes in git's ``/dev/null => `` line
    format; this runs the real command so a cross-version change in that
    prefix / footer can't slip past the unit suite unnoticed. ``_untracked_stat``
    is the only spot that PARSES git's stat text (the other buckets pass a
    single ``git diff --stat`` through untouched), so it's the one that needs
    a real-git guard.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._orig_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._orig_cwd)

    def test_untracked_preview_is_a_clean_aligned_stat(self):
        d = tempfile.mkdtemp()
        env = {**os.environ,
               'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}
        subprocess.run(['git', '-C', d, 'init', '-q'], check=True,
                       capture_output=True, env=env)
        # Two untracked files of different name length / line count.
        Path(d, 'a.txt').write_text('one\ntwo\nthree\n')
        Path(d, 'a-longer-name.txt').write_text('x\n')
        os.chdir(d)
        out = self.r._worktree_preview('untracked', [])
        plain = [re.sub(r'\x1b\[[0-9;]*m', '', ln) for ln in out.splitlines()]
        # git's /dev/null rename arrow is fully stripped; both files listed.
        self.assertNotIn('/dev/null', out)
        self.assertTrue(any('a.txt' in ln for ln in plain))
        self.assertTrue(any('a-longer-name.txt' in ln for ln in plain))
        # The `|` separators align into one column (the short name padded).
        seps = [ln.index('|') for ln in plain if '|' in ln]
        self.assertEqual(len(seps), 2)
        self.assertEqual(seps[0], seps[1])
        # Exactly one combined footer summing both files (3 + 1 insertions).
        footers = [ln for ln in plain if 'changed' in ln]
        self.assertEqual(len(footers), 1)
        self.assertIn('2 files changed', footers[0])
        self.assertIn('4 insertions(+)', footers[0])


class TestStashPreviewStat(unittest.TestCase):
    """``_stash_preview`` summarises a stash with ``--stat`` (like a commit)."""

    def setUp(self):
        self.r = _load_recipe()
        self.calls = []

        def fake_git_color(*args):
            self.calls.append(args)
            return 'STASHSTAT\n'

        self.r._git_color = fake_git_color
        self.r._colorize_diff = lambda raw: 'DELTA(' + raw + ')'

    def test_uses_stash_show_stat_not_patch(self):
        out = self.r._stash_preview(2)
        self.assertEqual(self.calls,
                         [('stash', 'show', '--stat', 'stash@{2}')])
        self.assertEqual(out, 'STASHSTAT\n')
        self.assertNotIn('DELTA', out)
        self.assertNotIn('-p', self.calls[0])  # diff lives on the leaves now


class TestStatusLeafDedup(unittest.TestCase):
    """``_status_root`` and ``_worktree_files`` share ``_status_leaf``."""

    def setUp(self):
        self.r = _load_recipe()

    def test_status_root_builds_status_leaf_items(self):
        # Stub _run_git so status --porcelain -z returns canned -z text;
        # the rows _status_root builds must equal _status_leaf for the same
        # (xy, path) — proving both paths share the one constructor.
        data = 'M  s.txt\x00 M w.txt\x00?? new.txt\x00'

        def fake_run_git(*args):
            if args and args[0] == 'status':
                return subprocess.CompletedProcess(args, 0, data, '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git
        items = self.r._status_root()
        expected = [self.r._status_leaf(xy, path) for xy, path in (
            ('M ', 's.txt'), (' M', 'w.txt'), ('??', 'new.txt'))]
        self.assertEqual(
            [(it.id, it.tag, it.tag_style, it.title, it.has_children)
             for it in items],
            [(e.id, e.tag, e.tag_style, e.title, e.has_children)
             for e in expected])

    def test_status_leaf_shape(self):
        leaf = self.r._status_leaf('??', 'new.txt')
        self.assertEqual(leaf.id, ('status', '??', 'new.txt'))
        self.assertEqual(leaf.title, 'new.txt')
        self.assertEqual(leaf.tag, '?')
        self.assertEqual(leaf.tag_style, 'dim')
        self.assertFalse(leaf.has_children)

    def test_colon_in_path_is_a_clean_field(self):
        # The old string-id codec had to split a 'status:XY:path' string, so a
        # path (or XY space) containing ':' was a documented hazard. As a tuple
        # field the path rides verbatim — no splitting, no ambiguity, and the
        # XY column (which carries a space) stays its own field.
        leaf = self.r._status_leaf('M ', 'weird:name:with:colons.txt')
        self.assertEqual(
            leaf.id, ('status', 'M ', 'weird:name:with:colons.txt'))


class TestGraphTranslate(unittest.TestCase):
    """``_graph_translate`` sanitises + glyph-substitutes git's graph art.

    The art now carries git's native ANSI colour (``--color=always``);
    ``_graph_translate`` runs it through the shared ``sanitize_ansi`` (keep
    only SGR) and glyph-substitutes the plain runs **between** SGR sequences,
    so a colour run is never split and no glyph lands inside an escape.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_mapped_glyphs_substituted(self):
        # The node / lane / horizontal art chars are substituted 1:1.
        self.assertEqual(self.r._graph_translate('*'), '•')  # • node
        self.assertEqual(self.r._graph_translate('|'), '│')  # │ vertical
        self.assertEqual(self.r._graph_translate('_'), '▁')  # ▁ horizontal

    def test_sgr_preserved_and_glyph_substituted_inside_run(self):
        # A coloured lane char: git wraps the '|' in SGR (\e[31m … \e[m).
        # The escape is preserved verbatim and the '|' inside it becomes '│'.
        self.assertEqual(
            self.r._graph_translate('\x1b[31m|\x1b[m'),
            '\x1b[31m│\x1b[m')
        # A coloured merge fan-out: red '|' then green '\' — each kept in its
        # own SGR run; '|' -> '│', the diagonal passes through.
        self.assertEqual(
            self.r._graph_translate('\x1b[31m|\x1b[m\x1b[32m\\\x1b[m  '),
            '\x1b[31m│\x1b[m\x1b[32m\\\x1b[m')
        # Mixed: a coloured lane then a PLAIN node (git leaves '*' uncoloured).
        self.assertEqual(
            self.r._graph_translate('\x1b[33m|\x1b[m * '),
            '\x1b[33m│\x1b[m •')

    def test_non_sgr_escapes_sanitised_out(self):
        # The art is treated as external input: a non-SGR CSI (cursor move /
        # erase) and a bare ESC are stripped, while the SGR colour survives
        # and the lane glyph is still substituted.
        self.assertEqual(
            self.r._graph_translate('\x1b[2J\x1b[31m|\x1b[m\x1b[H'),
            '\x1b[31m│\x1b[m')
        self.assertEqual(self.r._graph_translate('\x1b|'), '│')

    def test_diagonals_pass_through(self):
        # The merge diagonals are left as git's own ASCII art.
        self.assertEqual(self.r._graph_translate('/'), '/')    # asc diag
        self.assertEqual(self.r._graph_translate('\\'), '\\')  # desc diag

    def test_internal_spacing_preserved(self):
        # A multi-lane row keeps its inter-lane spaces (git's alignment);
        # only the trailing pad is stripped.
        self.assertEqual(
            self.r._graph_translate('| * | '),
            '│ • │')

    def test_merge_fanout_mixes_box_and_ascii(self):
        # A typical merge fan-out: the vertical lane is substituted while
        # the diagonal passes through — '|\' -> '│\', '|/' -> '│/'.
        self.assertEqual(self.r._graph_translate('|\\  '), '│\\')
        self.assertEqual(self.r._graph_translate('|/  '), '│/')

    def test_trailing_spaces_only_rstripped(self):
        # Leading/internal spaces stay; trailing run goes.
        self.assertEqual(self.r._graph_translate('  *   '), '  •')

    def test_unmapped_chars_pass_through(self):
        # Chars outside the map are untouched.
        self.assertEqual(self.r._graph_translate('* x'), '• x')


class TestCommitGraphItems(unittest.TestCase):
    """``_commit_graph_items`` builds commit + inert filler rows from --graph."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._log_limit = 1000

    def _stub_graph_log(self, lines):
        """Stub ``_run_git`` so ``log --graph`` returns ``lines`` verbatim.

        Each element of ``lines`` is one already-formed output line (art +
        optional ``\\x1f``-joined fields); they're joined with newlines.
        ``remote`` returns empty so decoration parsing needs no shell-out.
        """
        out = '\n'.join(lines)

        def fake_run_git(*args):
            if args and args[0] == 'log':
                # The --graph flag must be present in tree mode.
                self.assertIn('--graph', args)
                return subprocess.CompletedProcess(args, 0, out, '')
            if args and args[0] == 'remote':
                return subprocess.CompletedProcess(args, 0, '', '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git

    def _commit_line(self, art, sha, an, ar, s, d):
        # Mirror the recipe format: <art>\x1f%H\x1f%an\x1f%ar\x1f%s\x1f%D.
        return art + '\x1f'.join(['', sha, an, ar, s, d])

    def test_commit_line_builds_columnar_item_with_graph(self):
        sha = 'deadbeefcafe1234567890abcdef000000000000'
        self._stub_graph_log([
            self._commit_line('* ', sha, 'Alice', '2 days ago',
                              'first subject', 'HEAD -> main'),
        ])
        items = self.r._commit_graph_items([], [], 'root')
        self.assertEqual(len(items), 1)
        it = items[0]
        # Same columnar commit Item as the plain builder…
        self.assertEqual(it.id, ('commit', sha))
        self.assertEqual(it.title, 'first subject')
        self.assertTrue(it.has_children)
        self.assertEqual(it.col_sha, 'deadbee')
        self.assertEqual(it.col_author, 'Alice')
        self.assertEqual(it.col_date, '2 days ago')
        self.assertEqual(it.chips, [('HEAD', 'green'), ('main', 'cyan')])
        # …plus the translated graph art ('* ' -> '•').
        self.assertEqual(it.col_graph, '•')

    def test_filler_line_builds_meta_item(self):
        # A pure-art line (no \x1f) is a filler: a meta row (cursor-skipped +
        # unselectable by the framework), no col_sha, art only.
        sha = '0123456789abcdef0123456789abcdef01234567'
        self._stub_graph_log([
            self._commit_line('* ', sha, 'Bob', '1 hour ago', 'subj', ''),
            '|\\  ',
        ])
        items = self.r._commit_graph_items([], [], 'root')
        self.assertEqual(len(items), 2)
        filler = items[1]
        # Filler ids are the tuple ('filler', ns, n) — ns='root' here, n the
        # per-build running counter (an int).
        self.assertEqual(filler.id, ('filler', 'root', 0))
        self.assertEqual(filler.title, '')
        self.assertFalse(filler.has_children)
        # meta=True is what makes the framework skip the cursor over the row
        # (preventively) and never select it.
        self.assertTrue(filler.meta)
        # No col_sha on a filler (so git_row_content takes the filler path).
        self.assertIsNone(getattr(filler, 'col_sha', None))
        # The whole line is the (translated) art: '|\' -> '│\' (the lane
        # becomes box-vertical, the diagonal passes through).
        self.assertEqual(filler.col_graph, '│\\')
        # The 'filler' tag matches no get_children / get_preview branch, so the
        # row is inert everywhere — no drill-down, no preview.
        self.assertEqual(self.r.get_children(filler.id), [])
        self.assertEqual(self.r.get_preview(filler.id), '')

    def test_order_preserved_and_filler_indices_run(self):
        # Commits + fillers interleave in git's emitted order; filler ids
        # carry the build's ns then a unique running index.
        s1 = 'a' * 40
        s2 = 'b' * 40
        self._stub_graph_log([
            self._commit_line('* ', s1, 'A', 'now', 's1', ''),
            '|\\  ',
            self._commit_line('| * ', s2, 'B', 'now', 's2', ''),
            '|/  ',
        ])
        items = self.r._commit_graph_items([], [], 'root')
        self.assertEqual([it.id for it in items], [
            ('commit', s1), ('filler', 'root', 0),
            ('commit', s2), ('filler', 'root', 1),
        ])
        # The second commit's art keeps its leading lane: '| * ' -> '│ •'.
        self.assertEqual(items[2].col_graph, '│ •')

    def test_log_failure_returns_error_row(self):
        def fake_run_git(*args):
            return subprocess.CompletedProcess(args, 1, '', 'boom')

        self.r._run_git = fake_run_git
        items = self.r._commit_graph_items([], [], 'root')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, ('err',))

    def test_revs_and_paths_threaded_into_args(self):
        # The rev/path args + the -n limit reach git log (alongside --graph).
        captured = {}

        def fake_run_git(*args):
            if args and args[0] == 'log':
                captured['args'] = args
                return subprocess.CompletedProcess(args, 0, '', '')
            if args and args[0] == 'remote':
                return subprocess.CompletedProcess(args, 0, '', '')
            return subprocess.CompletedProcess(args, 1, '', '')

        self.r._run_git = fake_run_git
        self.r._log_limit = 42
        self.r._commit_graph_items(['HEAD~5'], ['src/'], 'root')
        args = captured['args']
        self.assertIn('--graph', args)
        # Native colour: git draws the graph + decorations in its own ANSI.
        self.assertIn('--color=always', args)
        self.assertNotIn('--no-color', args)
        self.assertIn('-n', args)
        self.assertIn('42', args)
        self.assertIn('HEAD~5', args)
        # Pathspec passed after a '--' sentinel.
        self.assertIn('--', args)
        self.assertEqual(args[args.index('--') + 1], 'src/')


class TestLogItemsRouting(unittest.TestCase):
    """``_log_items`` picks the graph vs plain builder on ``_tree_mode``."""

    def setUp(self):
        self.r = _load_recipe()

    def test_routes_to_plain_when_tree_off(self):
        # Tree off: the plain builder (no namespace) is used; ns is ignored.
        self.r._tree_mode = False
        self.r._commit_log_items = lambda revs, paths: ['PLAIN', revs, paths]
        self.r._commit_graph_items = (
            lambda revs, paths, ns: ['GRAPH', revs, paths, ns])
        self.assertEqual(self.r._log_items(['r'], ['p'], ns='root'),
                         ['PLAIN', ['r'], ['p']])

    def test_routes_to_graph_when_tree_on_threading_ns(self):
        # Tree on: the graph builder is used and the ns is threaded through
        # verbatim — a ref drill-down passes its ('ref', refname) id as ns.
        self.r._tree_mode = True
        self.r._commit_log_items = lambda revs, paths: ['PLAIN', revs, paths]
        self.r._commit_graph_items = (
            lambda revs, paths, ns: ['GRAPH', revs, paths, ns])
        self.assertEqual(self.r._log_items(['r'], ['p'], ns=('ref', 'feat')),
                         ['GRAPH', ['r'], ['p'], ('ref', 'feat')])

    def test_tree_mode_defaults_on(self):
        # The commit-graph column is ON by default (toggle off with
        # --no-tree / 't'); a fresh recipe load reflects that default.
        self.assertIs(self.r._tree_mode, True)


class TestGitRowContentGraph(unittest.TestCase):
    """``git_row_content`` renders the graph column + filler blank-pad."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _widths(self, sha=7, author=5, date=12):
        return {'col_sha': sha, 'col_author': author, 'col_date': date}

    def test_commit_graph_inserted_after_date_before_chips(self):
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self.r.Item(
            id=('commit', 'deadbee'), title='subj', col_sha='deadbee',
            col_author='Al', col_date='2 days ago',
            chips=[('HEAD', 'green')], col_graph='•')
        segs = self.r.git_row_content(item, ctx)
        # sha, author, date, GRAPH, [HEAD], subject.
        self.assertEqual(len(segs), 6)
        graph_seg = segs[3]
        # Graph art + a single trailing space; fg=None so git's own ANSI
        # colour (carried in the art text) shows through unmodified.
        self.assertEqual(graph_seg, ('• ', None, False))
        # The chip follows the graph, the subject is still last.
        self.assertEqual(segs[4], ('[HEAD] ', *self.r.style('green')))
        self.assertEqual(segs[-1], ('subj', None, False))

    def test_tree_off_commit_row_unchanged(self):
        # A commit row WITHOUT col_graph (tree off) is byte-identical to the
        # pre-feature output: exactly sha/author/date + chips + subject, no
        # graph segment anywhere.
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self.r.Item(
            id=('commit', 'deadbee'), title='subj', col_sha='deadbee',
            col_author='Al', col_date='2 days ago', chips=[])
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(segs, [
            ('deadbee' + '  ', *_YELLOW),
            ('Al   ' + '  ', *_DIM),
            ('2 days ago  ' + '  ', *_DIM),
            ('subj', None, False),
        ])

    def test_filler_row_blank_pad_then_art(self):
        # A filler (col_graph set, no col_sha) blank-pads the sha+author+date
        # span then renders its art; both segments use fg=None so git's own
        # colour codes in the art show through.
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        item = self.r.Item(id=('filler', 'root', 0), title='',
                           has_children=False, col_graph='│\\')
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(len(segs), 2)
        pad_seg, art_seg = segs
        # pad width = 7+2 + 5+2 + 12+2 = 30 spaces.
        expected_pad = ' ' * (7 + 2 + 5 + 2 + 12 + 2)
        self.assertEqual(pad_seg, (expected_pad, None, False))
        self.assertEqual(art_seg, ('│\\', None, False))
        # The filler measured exactly the three metadata columns.
        self.assertEqual(ctx.calls, ['col_sha', 'col_author', 'col_date'])

    def test_filler_pad_aligns_with_commit_graph_column(self):
        # The filler's blank pad must equal the commit row's sha+author+date
        # prefix width so the two graph columns line up vertically.
        widths = self._widths(sha=7, author=7, date=12)
        commit = self.r.Item(
            id=('commit', 'abc1234'), title='c', col_sha='abc1234',
            col_author='Bernard', col_date='3 weeks ago', chips=[],
            col_graph='•')
        filler = self.r.Item(id=('filler', 'root', 0), title='',
                             has_children=False, col_graph='│')
        c_segs = self.r.git_row_content(commit, _FakeCtx(widths))
        f_segs = self.r.git_row_content(filler, _FakeCtx(widths))
        # Sum of the commit's three metadata column widths == filler pad len.
        prefix = sum(len(c_segs[i][0]) for i in range(3))
        self.assertEqual(len(f_segs[0][0]), prefix)

    def test_non_commit_non_filler_still_falls_back(self):
        # A row with neither col_sha nor col_graph (status/ref/etc.) still
        # falls back to default_row_content and measures no column.
        ctx = _FakeCtx(self._widths())
        item = self.r.Item(id=('status', 'M ', 'beta.txt'), title='beta.txt',
                           tag='M', tag_style='yellow', has_children=False)
        segs = self.r.git_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(ctx.calls, [])


class TestPopTreeArg(unittest.TestCase):
    """``_pop_tree_arg`` pops --tree / --no-tree (last wins)."""

    def setUp(self):
        self.r = _load_recipe()
        self._orig_argv = sys.argv

    def tearDown(self):
        sys.argv = self._orig_argv

    def test_absent_returns_default(self):
        sys.argv = ['browse-git', 'HEAD']
        self.assertFalse(self.r._pop_tree_arg(False))
        self.assertTrue(self.r._pop_tree_arg(True))
        # argv untouched when the flag is absent.
        self.assertEqual(sys.argv, ['browse-git', 'HEAD'])

    def test_tree_sets_true_and_pops(self):
        sys.argv = ['browse-git', '--tree', 'HEAD']
        self.assertTrue(self.r._pop_tree_arg(False))
        self.assertEqual(sys.argv, ['browse-git', 'HEAD'])

    def test_no_tree_sets_false_and_pops(self):
        sys.argv = ['browse-git', '--no-tree']
        self.assertFalse(self.r._pop_tree_arg(True))
        self.assertEqual(sys.argv, ['browse-git'])

    def test_last_flag_wins_and_all_popped(self):
        sys.argv = ['browse-git', '--tree', '--no-tree']
        self.assertFalse(self.r._pop_tree_arg(False))
        self.assertEqual(sys.argv, ['browse-git'])
        sys.argv = ['browse-git', '--no-tree', '--tree']
        self.assertTrue(self.r._pop_tree_arg(False))
        self.assertEqual(sys.argv, ['browse-git'])


class TestPopAllArg(unittest.TestCase):
    """``_pop_all_arg`` pops ``--all`` and reports whether it was present."""

    def setUp(self):
        self.r = _load_recipe()
        self._orig_argv = sys.argv

    def tearDown(self):
        sys.argv = self._orig_argv

    def test_absent_returns_false_argv_untouched(self):
        sys.argv = ['browse-git', 'HEAD']
        self.assertFalse(self.r._pop_all_arg())
        self.assertEqual(sys.argv, ['browse-git', 'HEAD'])

    def test_all_sets_true_and_pops(self):
        sys.argv = ['browse-git', '--all', 'HEAD']
        self.assertTrue(self.r._pop_all_arg())
        self.assertEqual(sys.argv, ['browse-git', 'HEAD'])

    def test_every_occurrence_popped(self):
        sys.argv = ['browse-git', '--all', '--all']
        self.assertTrue(self.r._pop_all_arg())
        self.assertEqual(sys.argv, ['browse-git'])


class TestPopModeFlag(unittest.TestCase):
    """``_pop_mode_flag`` picks a mode from a per-mode flag (--status etc.).

    The accepted flags are derived from ``_MODES`` — one ``--{mode}`` each —
    so the set tracks ``_MODES`` automatically. At most one DISTINCT mode is
    allowed; two or more is a usage error (stderr + exit 2).
    """

    def setUp(self):
        self.r = _load_recipe()
        self._orig_argv = sys.argv

    def tearDown(self):
        sys.argv = self._orig_argv

    def test_absent_returns_none_argv_untouched(self):
        sys.argv = ['browse-git', 'HEAD']
        self.assertIsNone(self.r._pop_mode_flag())
        self.assertEqual(sys.argv, ['browse-git', 'HEAD'])

    def test_each_mode_flag_selects_its_mode_and_pops(self):
        # Every mode in _MODES has a working --{mode} flag, and the flag is
        # removed so it never leaks into the positional classifier.
        for mode in self.r._MODES:
            sys.argv = ['browse-git', f'--{mode}', 'HEAD']
            self.assertEqual(self.r._pop_mode_flag(), mode, mode)
            self.assertEqual(sys.argv, ['browse-git', 'HEAD'], mode)

    def test_repeated_same_flag_is_not_an_error_and_all_popped(self):
        # Only DISTINCT modes conflict; a repeat of one flag is fine and
        # every occurrence is removed.
        sys.argv = ['browse-git', '--status', '--status']
        self.assertEqual(self.r._pop_mode_flag(), 'status')
        self.assertEqual(sys.argv, ['browse-git'])

    def test_two_distinct_modes_exit_2_with_at_most_one_message(self):
        sys.argv = ['browse-git', '--status', '--reflog']
        err = io.StringIO()
        saved_err = self.r.sys.stderr
        try:
            self.r.sys.stderr = err
            with self.assertRaises(SystemExit) as cm:
                self.r._pop_mode_flag()
        finally:
            self.r.sys.stderr = saved_err
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('choose at most one mode flag', err.getvalue())

    def test_unknown_mode_like_flag_is_left_alone(self):
        # ``--mode`` is gone: it is now just an unknown flag, left in argv
        # for the positional classifier (which skips flag-like tokens) and
        # selecting no mode (None -> the commits default holds).
        sys.argv = ['browse-git', '--mode', 'status']
        self.assertIsNone(self.r._pop_mode_flag())
        self.assertEqual(sys.argv, ['browse-git', '--mode', 'status'])


class TestWindowTitleAll(unittest.TestCase):
    """``_window_title`` surfaces ``--all`` in the commits-mode title."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._mode = 'commits'

    def test_all_branches_shown_as_all(self):
        self.r._all_branches = True
        self.r._revs = []
        self.r._paths = []
        self.assertEqual(self.r._window_title(), 'browse-git [commits: --all]')

    def test_all_combines_with_paths(self):
        self.r._all_branches = True
        self.r._revs = []
        self.r._paths = ['src/']
        self.assertEqual(self.r._window_title(),
                         'browse-git [commits: --all src/]')

    def test_no_all_unchanged(self):
        self.r._all_branches = False
        self.r._revs = ['HEAD']
        self.r._paths = []
        self.assertEqual(self.r._window_title(), 'browse-git [commits: HEAD]')


# ---- browse-git - (stdin ingest) -------------------------------------------


# A four-kind ``git diff``: modify, add, delete, pure rename (no hunks).
_DIFF_TEXT = """\
diff --git a/keep.txt b/keep.txt
index 0000001..0000002 100644
--- a/keep.txt
+++ b/keep.txt
@@ -1 +1 @@
-keep v1
+keep v2
diff --git a/brand.txt b/brand.txt
new file mode 100644
index 0000000..0000003
--- /dev/null
+++ b/brand.txt
@@ -0,0 +1 @@
+brand new
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 0000004..0000000
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone v1
diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
"""

# A two-commit human ``git log`` (decoration on the newest, multi-line
# message whose body must NOT become the subject, merge header skipped).
_LOG_TEXT = """\
commit deadbeefcafe1234567890abcdef000000000000 (HEAD -> main, tag: v1.0)
Merge: 0123456 fedcba9
Author: Alice Dev <alice@example.com>
Date:   Thu Jun 11 10:00:00 2026 +0000

    second subject line

    body paragraph that is not the subject

commit 0123456789abcdef0123456789abcdef01234567
Author: Bob <bob@example.com>
Date:   Wed Jun 10 09:00:00 2026 +0000

    first subject
"""

# Human ``git status`` covering every section + verb the parser maps.
_HUMAN_STATUS_TEXT = """\
On branch main
Your branch is up to date with 'origin/main'.

Changes to be committed:
  (use "git restore --staged <file>..." to unstage)
\tmodified:   gamma.txt
\tnew file:   fresh.txt
\trenamed:    old.txt -> new.txt

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
\tmodified:   beta.txt
\tdeleted:    dropped.txt

Unmerged paths:
  (use "git add <file>..." to mark resolution)
\tboth modified:   conflict.txt

Untracked files:
  (use "git add <file>..." to include in what will be committed)
\tuntracked.txt

no changes added to commit (use "git add" and/or "git commit -a")
"""


def _colored(text, prefixes):
    """Wrap every line starting with one of ``prefixes`` in bold SGR.

    Mimics ``--color=always`` output closely enough for the sniff/split
    tests: classification must look at the SGR-stripped line.
    """
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip('\n')
        if body.startswith(prefixes):
            out.append(f'\x1b[1m{body}\x1b[m\n')
        else:
            out.append(line)
    return ''.join(out)


class TestSniffStdinKind(unittest.TestCase):
    """``_sniff_stdin_kind`` classifies by the first non-blank line."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_diff_header(self):
        self.assertEqual(self.r._sniff_stdin_kind(_DIFF_TEXT), 'diff')

    def test_diff_headerless_fragment(self):
        frag = '--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-o\n+n\n'
        self.assertEqual(self.r._sniff_stdin_kind(frag), 'diff')

    def test_log_commit_block(self):
        self.assertEqual(self.r._sniff_stdin_kind(_LOG_TEXT), 'log')

    def test_log_short_sha_also_matches(self):
        self.assertEqual(
            self.r._sniff_stdin_kind('commit deadbee\nAuthor: A <a@a>\n'),
            'log')

    def test_human_status_on_branch(self):
        self.assertEqual(
            self.r._sniff_stdin_kind(_HUMAN_STATUS_TEXT), 'human')

    def test_human_status_detached_head(self):
        self.assertEqual(
            self.r._sniff_stdin_kind('HEAD detached at deadbee\n'
                                     'nothing to commit\n'),
            'human')

    def test_porcelain_lines(self):
        for first in (' M beta.txt', 'M  alpha.txt', '?? untracked.txt',
                      'MM both.txt', 'R  old.txt -> new.txt'):
            self.assertEqual(
                self.r._sniff_stdin_kind(f'{first}\n'), 'porcelain', first)

    def test_porcelain_z_blob(self):
        # The -z form has no newlines; its first "line" is the whole
        # record stream and still starts with a valid XY code.
        self.assertEqual(
            self.r._sniff_stdin_kind('M  a.txt\x00?? u.txt\x00'),
            'porcelain')

    def test_leading_blank_lines_skipped(self):
        self.assertEqual(
            self.r._sniff_stdin_kind('\n   \n' + _DIFF_TEXT), 'diff')

    def test_colored_input_sniffs_the_same(self):
        self.assertEqual(
            self.r._sniff_stdin_kind(_colored(_DIFF_TEXT, ('diff --git',))),
            'diff')
        self.assertEqual(
            self.r._sniff_stdin_kind(_colored(_LOG_TEXT, ('commit ',))),
            'log')

    def test_unrecognized_inputs_are_none(self):
        for text in ('hello world\n',           # prose
                     'abc1234 subject\n',       # git log --oneline
                     '   indented prose\n',     # must not look porcelain
                     ' beta.txt | 2 +-\n',      # bare --stat output
                     '## main...origin/main\n', # status -sb branch line
                     '', '   \n\n'):            # empty / whitespace-only
            self.assertIsNone(self.r._sniff_stdin_kind(text), repr(text))


class TestParseStdinDiff(unittest.TestCase):
    """``_parse_stdin_diff`` splits per-file blocks and derives rows."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_four_kinds_rows_and_block_alignment(self):
        rows, blocks = self.r._parse_stdin_diff(_DIFF_TEXT)
        self.assertEqual(rows, [
            ('M', 'keep.txt'),
            ('A', 'brand.txt'),
            ('D', 'gone.txt'),
            ('R', 'new.txt'),
        ])
        self.assertEqual(len(blocks), 4)
        # Each block carries its own file's content and nobody else's.
        self.assertIn('+keep v2', blocks[0])
        self.assertNotIn('brand new', blocks[0])
        self.assertIn('+brand new', blocks[1])
        self.assertIn('-gone v1', blocks[2])
        self.assertIn('rename to new.txt', blocks[3])
        # Blocks are verbatim slices: joining them restores the text.
        self.assertEqual(''.join(blocks), _DIFF_TEXT)

    def test_headerless_fragment_is_one_block(self):
        frag = '--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-o\n+n\n'
        rows, blocks = self.r._parse_stdin_diff(frag)
        self.assertEqual(rows, [('M', 'x.txt')])
        self.assertEqual(blocks, [frag])

    def test_gnu_diff_timestamp_suffix_stripped(self):
        # Plain unified diffs suffix the +++/--- paths with a tab +
        # timestamp and use no a/ b/ prefixes.
        frag = ('--- a/x.txt\t2026-06-11 10:00:00\n'
                '+++ b/x.txt\t2026-06-11 10:00:01\n'
                '@@ -1 +1 @@\n-o\n+n\n')
        rows, _blocks = self.r._parse_stdin_diff(frag)
        self.assertEqual(rows, [('M', 'x.txt')])

    def test_colored_input_splits_and_classifies(self):
        colored = _colored(
            _DIFF_TEXT,
            ('diff --git', '---', '+++', 'new file', 'deleted file',
             'rename from', 'rename to'))
        rows, blocks = self.r._parse_stdin_diff(colored)
        self.assertEqual([row[0] for row in rows], ['M', 'A', 'D', 'R'])
        self.assertEqual([row[1] for row in rows],
                         ['keep.txt', 'brand.txt', 'gone.txt', 'new.txt'])
        # Blocks keep the original colour for the preview pane.
        self.assertEqual(len(blocks), 4)
        self.assertIn('\x1b[1m', blocks[0])


# A binary file block (git's default "Binary files … differ" form).
_DIFF_BINARY = """\
diff --git a/img.bin b/img.bin
index 6164d9f..0a71165 100644
Binary files a/img.bin and b/img.bin differ
"""

# A binary file block in the GIT-binary-patch (``--binary``) form.
_DIFF_BINARY_PATCH = """\
diff --git a/img.bin b/img.bin
index 6164d9f..0a71165 100644
GIT binary patch
literal 11
ScmZQzWMXDvWn=&U?=JubsRI`P
"""

# A hunkless pure rename (no content change → no +/- body lines).
_DIFF_RENAME_HUNKLESS = """\
diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
"""

# A rename that also changes content (carries a hunk under the rename).
_DIFF_RENAME_HUNKS = """\
diff --git a/old.txt b/new.txt
similarity index 80%
rename from old.txt
rename to new.txt
index 1111111..2222222 100644
--- a/old.txt
+++ b/new.txt
@@ -1,2 +1,2 @@
 keep
-old line
+new line
"""

# Two files of very different sizes — drives the histogram-scaling tests.
# ``big.txt`` has 9 added + 3 removed (12 changes); ``small.txt`` has 1
# added + 1 removed (2 changes).
_DIFF_SCALING = """\
diff --git a/big.txt b/big.txt
index 1111111..2222222 100644
--- a/big.txt
+++ b/big.txt
@@ -1,5 +1,11 @@
 ctx
-r1
-r2
-r3
+a1
+a2
+a3
+a4
+a5
+a6
+a7
+a8
+a9
diff --git a/small.txt b/small.txt
index 3333333..4444444 100644
--- a/small.txt
+++ b/small.txt
@@ -1,2 +1,2 @@
 ctx
-old
+new
"""

# A hunk whose BODY lines literally start '+++ ' / '--- ': with the diff
# prefix they render '++++ x' / '+--- y' / '-+++ a' / '---- b' — all
# normal change lines that git numstat counts (3 added, 2 removed here).
# Only the file-header ---/+++ (before the @@) are excluded.
_DIFF_LITERAL_MARKERS = """\
diff --git a/lit.txt b/lit.txt
index 5555555..6666666 100644
--- a/lit.txt
+++ b/lit.txt
@@ -1,3 +1,4 @@
 base
-+++ a
---- b
++++ x
+--- y
+normal
"""


class TestStdinDiffUmbrella(unittest.TestCase):
    """The piped-diff ``('sdiff',)`` umbrella: counts, title, stat preview.

    Counting (``_diff_block_counts``) and the synthesised ``--stat``
    table (``_stdin_diff_stat``) are pure text transforms over the
    already-split blocks, so the tests drive them on canned diffs / by
    seeding the ``_STDIN_*`` state directly. ``_git_color`` / ``_run_git``
    are poisoned where the tree is built — no git runs in stdin mode.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.r._run_git = _GitPoison(self)
        self.r._git_color = _GitPoison(self)

    # ---- counting --------------------------------------------------------

    def test_counts_plain_multi_file(self):
        # keep.txt 1/1, brand.txt (added) 1/0, gone.txt (deleted) 0/1,
        # rename 0/0 — the +++/--- headers are never counted.
        _rows, blocks = self.r._parse_stdin_diff(_DIFF_TEXT)
        self.assertEqual([self.r._diff_block_counts(b) for b in blocks], [
            (1, 1, False),     # keep.txt
            (1, 0, False),     # brand.txt (new file)
            (0, 1, False),     # gone.txt (deleted)
            (0, 0, False),     # hunkless rename
        ])

    def test_counts_rename_with_hunks(self):
        _rows, blocks = self.r._parse_stdin_diff(_DIFF_RENAME_HUNKS)
        self.assertEqual(self.r._diff_block_counts(blocks[0]), (1, 1, False))

    def test_counts_hunkless_rename_is_zero_zero(self):
        _rows, blocks = self.r._parse_stdin_diff(_DIFF_RENAME_HUNKLESS)
        self.assertEqual(self.r._diff_block_counts(blocks[0]), (0, 0, False))

    def test_counts_binary_differ_and_patch_forms(self):
        for text in (_DIFF_BINARY, _DIFF_BINARY_PATCH):
            _rows, blocks = self.r._parse_stdin_diff(text)
            self.assertEqual(self.r._diff_block_counts(blocks[0]),
                             (0, 0, True), text)

    def test_counts_single_file(self):
        _rows, blocks = self.r._parse_stdin_diff(_DIFF_RENAME_HUNKS)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(self.r._diff_block_counts(blocks[0]), (1, 1, False))

    def test_counts_ignore_sgr_color(self):
        # Counting strips SGR first, so coloured +/- lines count the same.
        colored = _colored(_DIFF_TEXT, ('+', '-'))
        _rows, blocks = self.r._parse_stdin_diff(colored)
        self.assertEqual([self.r._diff_block_counts(b) for b in blocks],
                         [(1, 1, False), (1, 0, False),
                          (0, 1, False), (0, 0, False)])

    def test_counts_literal_marker_content_lines(self):
        # Hunk-body lines whose CONTENT starts '+++ '/'--- ' ('++++ x',
        # '---- b' with the diff prefix) are normal change lines and must
        # count — the header exclusion is positional (before the first
        # @@), not a prefix match. git numstat agrees: 3 added, 2 removed.
        _rows, blocks = self.r._parse_stdin_diff(_DIFF_LITERAL_MARKERS)
        self.assertEqual(self.r._diff_block_counts(blocks[0]), (3, 2, False))
        # And the umbrella title carries the corrected totals.
        self._seed(_DIFF_LITERAL_MARKERS)
        self.assertEqual(self._title(), 'diff: 1 file +3 -2')

    # ---- umbrella title + auto-expand -----------------------------------

    def _seed(self, text):
        rows, blocks = self.r._parse_stdin_diff(text)
        self.r._STDIN_KIND = 'diff'
        self.r._STDIN_ROWS, self.r._STDIN_BLOCKS = rows, blocks
        return rows, blocks

    def _title(self):
        """The umbrella row title, SGR-stripped (the +X/-Y are colored)."""
        return re.sub(r'\x1b\[[0-9;]*m', '',
                      self.r.get_children(None)[0].title)

    def test_umbrella_title_short_form(self):
        self._seed(_DIFF_TEXT)        # 4 files, +2 (1+1) / -2 (1+1)
        root = self.r.get_children(None)
        self.assertEqual(len(root), 1)
        self.assertEqual(root[0].id, ('sdiff',))
        self.assertEqual(self._title(), 'diff: 4 files +2 -2')
        self.assertTrue(root[0].has_children)

    def test_umbrella_title_counts_exclude_binary(self):
        # Binary file contributes a file to the count but no +/- lines.
        self._seed(_DIFF_TEXT + _DIFF_BINARY)
        self.assertEqual(self._title(), 'diff: 5 files +2 -2')

    def test_umbrella_title_singular_one_file(self):
        # One file → 'file', matching the footer's pluralisation.
        self._seed(_DIFF_RENAME_HUNKS)
        self.assertEqual(self._title(), 'diff: 1 file +1 -1')

    def test_umbrella_children_are_the_sfile_leaves(self):
        rows, _blocks = self._seed(_DIFF_TEXT)
        kids = self.r.get_children(('sdiff',))
        self.assertEqual([it.id for it in kids],
                         [('sfile', i, p) for i, (_l, p) in enumerate(rows)])

    def test_main_auto_expands_the_umbrella(self):
        # main() issues a posted ``b.expand(('sdiff',))`` before run() for
        # a piped diff so the file rows are visible immediately. The stub
        # Browser records every ``expand`` call.
        expanded = []

        class _ExpandStub:
            def __init__(self, *a, **kw):
                self._args = a
                for k, v in kw.items():
                    setattr(self, k, v)

            def expand(self, id):
                expanded.append(id)

            def run(self):
                return 0

        self.r.Browser = _ExpandStub
        self.r._run_git = _GitPoison(self)
        self.r._git_color = _GitPoison(self)
        self.r.shutil = types.SimpleNamespace(which=_GitPoison(self))
        saved_in = self.r.sys.stdin
        saved_argv = list(self.r.sys.argv)
        try:
            self.r.sys.stdin = io.StringIO(_DIFF_TEXT)
            self.r.sys.argv[:] = ['browse-git', '-']
            with self.assertRaises(SystemExit):
                self.r.main()
        finally:
            self.r.sys.stdin = saved_in
            self.r.sys.argv[:] = saved_argv
        self.assertEqual(expanded, [('sdiff',)])

    def test_main_does_not_auto_expand_for_log_or_status(self):
        # The umbrella + auto-expand are diff-only. A log / status stdin
        # builds no umbrella and issues no expand.
        for text in (_LOG_TEXT, ' M beta.txt\n'):
            self.r = _load_recipe()
            expanded = []

            class _ExpandStub:
                def __init__(self, *a, **kw):
                    self._args = a
                    for k, v in kw.items():
                        setattr(self, k, v)

                def expand(self, id):
                    expanded.append(id)

                def run(self):
                    return 0

            self.r.Browser = _ExpandStub
            saved_in = self.r.sys.stdin
            saved_argv = list(self.r.sys.argv)
            try:
                self.r.sys.stdin = io.StringIO(text)
                self.r.sys.argv[:] = ['browse-git', '-']
                with self.assertRaises(SystemExit):
                    self.r.main()
            finally:
                self.r.sys.stdin = saved_in
                self.r.sys.argv[:] = saved_argv
            self.assertEqual(expanded, [], text)

    # ---- stat preview ----------------------------------------------------

    @staticmethod
    def _plain(line):
        """The SGR-stripped form of a stat row (drops the +/- colors)."""
        return re.sub(r'\x1b\[[0-9;]*m', '', line)

    def test_stat_preview_routes_through_sdiff_id(self):
        self._seed(_DIFF_TEXT)
        self.r._browser = types.SimpleNamespace(preview_width=80)
        seen = {}
        self.r._stdin_diff_stat = (
            lambda w: seen.__setitem__('w', w) or 'STAT')
        self.assertEqual(self.r.get_preview(('sdiff',)), 'STAT')
        self.assertEqual(seen['w'], 80)        # current preview width

    def test_stat_rows_and_summary(self):
        # Option B (#938): per file ``<path> | +N -M`` then the footer.
        self._seed(_DIFF_TEXT)
        out = self.r._stdin_diff_stat(80).splitlines()
        # One row per file (4) + the summary footer.
        self.assertEqual(len(out), 5)
        plain = [self._plain(ln) for ln in out]
        self.assertEqual(plain[0], ' keep.txt | +1 -1')
        self.assertEqual(plain[1], ' brand.txt | +1 -0')
        self.assertEqual(plain[2], ' gone.txt | +0 -1')
        self.assertEqual(plain[3], ' new.txt | +0 -0')   # hunkless rename
        self.assertTrue(plain[-1].startswith(' 4 files changed'))
        self.assertIn('2 insertions(+)', plain[-1])
        self.assertIn('2 deletions(-)', plain[-1])

    def test_stat_counts_are_colored_green_add_red_remove(self):
        # The ``+N`` carries the green SGR, the ``-M`` the red SGR — the
        # raw escapes, not a screenshot. (The framework strips them when
        # ANSI is off; see the row-render test in the UI suite.)
        self._seed(_DIFF_TEXT)
        rows = self.r._stdin_diff_stat(80).splitlines()
        self.assertIn('\x1b[32m+1\x1b[m', rows[0])    # keep.txt added
        self.assertIn('\x1b[31m-1\x1b[m', rows[0])    # keep.txt removed
        # A zero side is still colored (the number is the signal — #938).
        self.assertIn('\x1b[31m-0\x1b[m', rows[1])    # brand.txt: -0
        self.assertIn('\x1b[32m+0\x1b[m', rows[2])    # gone.txt: +0

    def test_stat_counts_survive_sanitize_ansi(self):
        # The colors are complete SGR sequences, so the framework's
        # escape-sanitiser (kept SGR, dropped other CSI) leaves them whole
        # — the green/red reaches the rendered preview unchanged.
        self._seed(_DIFF_TEXT)
        out = self.r._stdin_diff_stat(80)
        self.assertEqual(self.r.sanitize_ansi(out), out)

    def test_stat_no_histogram_bars(self):
        # Option B dropped the bars: after the colored ``+N -M`` there is
        # no trailing run of bare '+'/'-' glyphs.
        self._seed(_DIFF_SCALING)        # big.txt 9/3, small.txt 1/1
        for ln in self.r._stdin_diff_stat(80).splitlines()[:-1]:
            self.assertRegex(self._plain(ln), r'\| \+\d+ -\d+$')

    def test_stat_long_path_is_front_elided(self):
        self.r._STDIN_KIND = 'diff'
        long_path = 'very/deeply/nested/dir/longfilename.txt'
        self.r._STDIN_ROWS = [('M', long_path)]
        self.r._STDIN_BLOCKS = [
            f'diff --git a/{long_path} b/{long_path}\n'
            f'--- a/{long_path}\n+++ b/{long_path}\n'
            '@@ -1 +1 @@\n-old\n+new\n']
        row = self._plain(self.r._stdin_diff_stat(30).splitlines()[0])
        # The readable tail is kept behind a ``...`` prefix (no '/'-snap):
        # whatever fits the name budget, ending at the path's tail.
        self.assertTrue(row.lstrip().startswith('...'), row)
        self.assertIn('ilename.txt', row)        # the path's own tail
        # The counts always follow, whatever the path length.
        self.assertRegex(row, r'\| \+1 -1$')
        # A wider pane keeps more of the path.
        wide = self._plain(self.r._stdin_diff_stat(80).splitlines()[0])
        self.assertEqual(wide, f' {long_path} | +1 -1')

    def test_stat_short_path_is_not_padded(self):
        # No fixed-width name column any more — a short path is verbatim.
        self._seed(_DIFF_RENAME_HUNKS)       # new.txt, 1/1
        row = self._plain(self.r._stdin_diff_stat(80).splitlines()[0])
        self.assertEqual(row, ' new.txt | +1 -1')

    def test_stat_binary_row_shows_Bin_no_counts(self):
        self._seed(_DIFF_BINARY)
        rows = self.r._stdin_diff_stat(80).splitlines()
        self.assertEqual(self._plain(rows[0]), ' img.bin | Bin')
        # No +/- counts for a binary file, and no color either.
        self.assertNotIn('\x1b[', rows[0])
        # A binary-only diff still prints both zero clauses, like git.
        self.assertEqual(rows[-1],
                         ' 1 file changed, 0 insertions(+), 0 deletions(-)')

    def test_stat_summary_omits_zero_parts_like_git(self):
        # insertions-only → deletions clause dropped.
        self.assertEqual(self.r._diff_stat_summary(1, 3, 0),
                         ' 1 file changed, 3 insertions(+)')
        # deletions-only → insertions clause dropped; singular wording.
        self.assertEqual(self.r._diff_stat_summary(1, 0, 1),
                         ' 1 file changed, 1 deletion(-)')
        # both nonzero → both shown, plural.
        self.assertEqual(self.r._diff_stat_summary(2, 12, 5),
                         ' 2 files changed, 12 insertions(+), 5 deletions(-)')
        # all-zero (e.g. binary-only / pure rename) → both zero clauses.
        self.assertEqual(self.r._diff_stat_summary(1, 0, 0),
                         ' 1 file changed, 0 insertions(+), 0 deletions(-)')

    def test_stat_hunkless_rename_row_shows_zero_counts(self):
        self._seed(_DIFF_RENAME_HUNKLESS)
        rows = self.r._stdin_diff_stat(80).splitlines()
        # The rename file's count is +0 -0 (no hunks → no changes).
        self.assertEqual(self._plain(rows[0]), ' new.txt | +0 -0')

    def test_stat_empty_diff_is_just_the_summary(self):
        self.r._STDIN_KIND = 'diff'
        self.r._STDIN_ROWS, self.r._STDIN_BLOCKS = [], []
        self.assertEqual(self.r._stdin_diff_stat(80),
                         ' 0 files changed, 0 insertions(+), 0 deletions(-)')

    # ---- umbrella title color -------------------------------------------

    def test_umbrella_title_totals_are_colored(self):
        # The row title's ``+X`` / ``-Y`` carry the same green/red SGR as
        # the preview (rendered as row-text SGR, like the commit graph).
        self._seed(_DIFF_TEXT)           # +2 -2
        title = self.r.get_children(None)[0].title
        self.assertIn('\x1b[32m+2\x1b[m', title)
        self.assertIn('\x1b[31m-2\x1b[m', title)
        # SGR-stripped, the title reads as before.
        self.assertEqual(
            re.sub(r'\x1b\[[0-9;]*m', '', title), 'diff: 4 files +2 -2')


class TestParseStdinLog(unittest.TestCase):
    """``_parse_stdin_log`` splits commit blocks and extracts the fields."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_rows_fields_and_block_alignment(self):
        rows, blocks = self.r._parse_stdin_log(_LOG_TEXT)
        self.assertEqual(rows, [
            ('deadbeefcafe1234567890abcdef000000000000', 'Alice Dev',
             'Thu Jun 11 10:00:00 2026 +0000', 'second subject line',
             'HEAD -> main, tag: v1.0'),
            ('0123456789abcdef0123456789abcdef01234567', 'Bob',
             'Wed Jun 10 09:00:00 2026 +0000', 'first subject', ''),
        ])
        self.assertEqual(len(blocks), 2)
        # The whole block — message body included — is the preview source.
        self.assertIn('body paragraph that is not the subject', blocks[0])
        self.assertNotIn('first subject', blocks[0])
        self.assertEqual(''.join(blocks), _LOG_TEXT)

    def test_stat_payload_stays_in_block_not_fields(self):
        text = (_LOG_TEXT.split('\ncommit ')[0]
                + '\n a.txt | 2 +-\n 1 file changed, 1 insertion(+)\n')
        rows, blocks = self.r._parse_stdin_log(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 'second subject line')
        self.assertIn(' a.txt | 2 +-', blocks[0])

    def test_patch_payload_stays_in_block(self):
        # log -p: the diff rides in the block; commit fields untouched.
        text = _LOG_TEXT + 'diff --git a/x b/x\n--- a/x\n+++ b/x\n+new\n'
        rows, blocks = self.r._parse_stdin_log(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][3], 'first subject')
        self.assertIn('diff --git a/x b/x', blocks[1])

    def test_colored_input_parses_the_same(self):
        colored = _colored(_LOG_TEXT, ('commit ',))
        rows, _blocks = self.r._parse_stdin_log(colored)
        self.assertEqual([row[3] for row in rows],
                         ['second subject line', 'first subject'])
        self.assertEqual(rows[0][4], 'HEAD -> main, tag: v1.0')


class TestParsePorcelainLines(unittest.TestCase):
    """``_parse_porcelain_lines`` parses newline porcelain into (XY, path)."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_codes_and_untracked(self):
        text = ('M  alpha.txt\n'
                ' M beta.txt\n'
                'MM both.txt\n'
                '?? untracked.txt\n')
        self.assertEqual(self.r._parse_porcelain_lines(text), [
            ('M ', 'alpha.txt'),
            (' M', 'beta.txt'),
            ('MM', 'both.txt'),
            ('??', 'untracked.txt'),
        ])

    def test_rename_arrow_keeps_new_path(self):
        self.assertEqual(
            self.r._parse_porcelain_lines('R  old.txt -> new.txt\n'),
            [('R ', 'new.txt')])
        # The rename+modified two-sided code too.
        self.assertEqual(
            self.r._parse_porcelain_lines('RM old.txt -> new.txt\n'),
            [('RM', 'new.txt')])

    def test_arrow_in_a_non_rename_path_is_kept(self):
        # ' -> ' only splits rename/copy entries; an M path keeps it.
        self.assertEqual(
            self.r._parse_porcelain_lines(' M a -> b.txt\n'),
            [(' M', 'a -> b.txt')])

    def test_blank_and_malformed_lines_skipped(self):
        self.assertEqual(
            self.r._parse_porcelain_lines('\nnot porcelain\n M ok.txt\n'),
            [(' M', 'ok.txt')])


class TestParseHumanStatus(unittest.TestCase):
    """``_parse_human_status`` maps the prose sections to porcelain rows."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_all_sections_and_verbs(self):
        self.assertEqual(self.r._parse_human_status(_HUMAN_STATUS_TEXT), [
            ('M ', 'gamma.txt'),       # staged modify
            ('A ', 'fresh.txt'),       # staged add ("new file")
            ('R ', 'new.txt'),         # staged rename → new path
            (' M', 'beta.txt'),        # unstaged modify
            (' D', 'dropped.txt'),     # unstaged delete
            ('UU', 'conflict.txt'),    # any unmerged entry
            ('??', 'untracked.txt'),   # untracked (bare path, no verb)
        ])

    def test_clean_tree_yields_no_rows(self):
        clean = ('On branch main\n'
                 "Your branch is up to date with 'origin/main'.\n"
                 '\n'
                 'nothing to commit, working tree clean\n')
        self.assertEqual(self.r._parse_human_status(clean), [])

    def test_hint_lines_never_become_rows(self):
        rows = self.r._parse_human_status(_HUMAN_STATUS_TEXT)
        self.assertFalse(any('use "git' in path for _xy, path in rows))

    def test_colon_in_path_survives(self):
        text = ('On branch main\n'
                'Changes not staged for commit:\n'
                '\tmodified:   weird:name.txt\n')
        self.assertEqual(self.r._parse_human_status(text),
                         [(' M', 'weird:name.txt')])


class _GitPoison:
    """Callable that fails the test if any git helper runs in stdin mode."""

    def __init__(self, test):
        self._test = test

    def __call__(self, *args, **kwargs):
        self._test.fail(f'git invoked in stdin mode: {args!r}')


class TestStdinTreeAndPreviews(unittest.TestCase):
    """The stdin root builders / previews serve the parsed text, never git.

    Sets the ``_STDIN_*`` module state directly (the parsers have their
    own tests above) and poisons ``_run_git`` / ``_git_color`` so any
    code path that shells out fails the test — the no-git guarantee of
    ``browse-git -``.
    """

    def setUp(self):
        self.r = _load_recipe()
        self.r._run_git = _GitPoison(self)
        self.r._git_color = _GitPoison(self)
        self.r.HAVE_DELTA = None       # _colorize_diff → verbatim text

    def test_diff_root_is_one_umbrella_over_sfile_leaves(self):
        rows, blocks = self.r._parse_stdin_diff(_DIFF_TEXT)
        self.r._STDIN_KIND = 'diff'
        self.r._STDIN_ROWS, self.r._STDIN_BLOCKS = rows, blocks
        # Root is a single synthetic ('sdiff',) umbrella, expandable.
        root = self.r.get_children(None)
        self.assertEqual([it.id for it in root], [('sdiff',)])
        self.assertTrue(root[0].has_children)
        # Its children are the per-file ('sfile', …) leaves.
        items = self.r.get_children(('sdiff',))
        self.assertEqual([it.id for it in items], [
            ('sfile', 0, 'keep.txt'),
            ('sfile', 1, 'brand.txt'),
            ('sfile', 2, 'gone.txt'),
            ('sfile', 3, 'new.txt'),
        ])
        self.assertEqual([it.tag for it in items], ['M', 'A', 'D', 'R'])
        self.assertEqual(items[0].tag_style, 'yellow')
        self.assertEqual(items[1].tag_style, 'green')
        self.assertTrue(all(not it.has_children for it in items))
        # Leaves: drilling yields nothing; preview is the file's block.
        self.assertEqual(self.r.get_children(items[0].id), [])
        self.assertEqual(self.r.get_preview(items[0].id), blocks[0])
        self.assertIn('+brand new', self.r.get_preview(items[1].id))

    def test_log_rows_carry_columns_chips_and_block_previews(self):
        rows, blocks = self.r._parse_stdin_log(_LOG_TEXT)
        self.r._STDIN_KIND = 'log'
        self.r._STDIN_ROWS, self.r._STDIN_BLOCKS = rows, blocks
        items = self.r.get_children(None)
        self.assertEqual([it.id for it in items],
                         [('slog', 0), ('slog', 1)])
        first = items[0]
        self.assertEqual(first.title, 'second subject line')
        self.assertEqual(first.col_sha, 'deadbee')
        self.assertEqual(first.col_author, 'Alice Dev')
        self.assertEqual(first.col_date, 'Thu Jun 11 10:00:00 2026 +0000')
        # Decorations parse without git (remotes=set()): HEAD chip green,
        # branch cyan, tag yellow.
        self.assertEqual(first.chips, [
            ('HEAD', 'green'), ('main', 'cyan'), ('v1.0', 'yellow')])
        self.assertFalse(first.has_children)
        # A plain log block previews verbatim (no delta involved).
        self.assertEqual(self.r.get_preview(('slog', 0)), blocks[0])

    def test_log_p_block_routes_through_colorize_diff(self):
        text = _LOG_TEXT + 'diff --git a/x b/x\n--- a/x\n+++ b/x\n+new\n'
        rows, blocks = self.r._parse_stdin_log(text)
        self.r._STDIN_KIND = 'log'
        self.r._STDIN_ROWS, self.r._STDIN_BLOCKS = rows, blocks
        seen = []
        self.r._colorize_diff = lambda raw: seen.append(raw) or 'RENDERED'
        # Block 1 carries the patch → goes through the diff pipeline.
        self.assertEqual(self.r.get_preview(('slog', 1)), 'RENDERED')
        self.assertEqual(seen, [blocks[1]])
        # Block 0 is patch-free → verbatim, pipeline untouched.
        self.assertEqual(self.r.get_preview(('slog', 0)), blocks[0])
        self.assertEqual(len(seen), 1)

    def test_status_rows_reuse_status_leaf_shape_and_text_preview(self):
        self.r._STDIN_KIND = 'status'
        self.r._STDIN_ROWS = [('M ', 'alpha.txt'), ('??', 'untracked.txt')]
        items = self.r.get_children(None)
        # The shared ('status', xy, path) leaves — same shape as repo mode.
        self.assertEqual([it.id for it in items], [
            ('status', 'M ', 'alpha.txt'),
            ('status', '??', 'untracked.txt'),
        ])
        self.assertEqual([it.tag for it in items], ['M', '?'])
        self.assertTrue(all(not it.has_children for it in items))
        # Piped status has no diff text — preview is the entry line.
        self.assertEqual(self.r.get_preview(items[0].id), 'M  alpha.txt')
        self.assertEqual(self.r.get_preview(items[1].id), '?? untracked.txt')

    def test_status_empty_rows_serve_the_clean_sentinel(self):
        self.r._STDIN_KIND = 'status'
        self.r._STDIN_ROWS = []
        items = self.r.get_children(None)
        self.assertEqual([it.id for it in items], [('status_clean',)])

    def test_repo_mode_status_preview_still_routes_to_git_builder(self):
        # With no stdin state the status branch routes to _status_preview
        # exactly as before — the seam changes nothing in repo mode.
        self.assertIsNone(self.r._STDIN_KIND)
        seen = {}
        self.r._status_preview = (
            lambda xy, path: seen.__setitem__('args', (xy, path)) or 'REPO')
        self.assertEqual(self.r.get_preview(('status', 'M ', 'x.py')), 'REPO')
        self.assertEqual(seen['args'], ('M ', 'x.py'))


@unittest.skipUnless(shutil.which('git'), 'git not available')
class TestStdinRealGitOutputs(unittest.TestCase):
    """Sniff + parse REAL git output captured from a throwaway temp repo.

    Guards the literal fixtures above against drift: a temp repo is
    built once with two commits and a dirty worktree (staged modify,
    unstaged modify, staged rename, untracked file), and each canned
    command's actual output must sniff to the right kind and parse to
    the expected rows. ``LC_ALL=C`` pins the human-status prose.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()
        cls.repo = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.repo, ignore_errors=True)
        env = {**os.environ, 'LC_ALL': 'C',
               'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}

        def git(*args):
            return subprocess.run(
                ['git', '-C', cls.repo, *args], check=True,
                capture_output=True, text=True, env=env).stdout

        cls.git = staticmethod(git)
        for name in ('alpha.txt', 'beta.txt', 'gamma.txt'):
            with open(os.path.join(cls.repo, name), 'w') as f:
                f.write(f'{name} v1\n')
        git('init', '-q', '-b', 'main')
        git('add', '.')
        git('commit', '-q', '-m', 'first commit')
        with open(os.path.join(cls.repo, 'delta.txt'), 'w') as f:
            f.write('delta v1\n')
        git('add', 'delta.txt')
        git('commit', '-q', '-m', 'second commit adds delta')
        # Dirty worktree: staged modify / unstaged modify / staged
        # rename / untracked.
        with open(os.path.join(cls.repo, 'alpha.txt'), 'w') as f:
            f.write('alpha v2\n')
        git('add', 'alpha.txt')
        with open(os.path.join(cls.repo, 'beta.txt'), 'w') as f:
            f.write('beta v2\n')
        git('mv', 'gamma.txt', 'moved.txt')
        with open(os.path.join(cls.repo, 'untracked.txt'), 'w') as f:
            f.write('new\n')

    def test_real_diff(self):
        text = self.git('diff')
        self.assertEqual(self.r._sniff_stdin_kind(text), 'diff')
        rows, blocks = self.r._parse_stdin_diff(text)
        self.assertEqual(rows, [('M', 'beta.txt')])
        self.assertIn('+beta v2', blocks[0])

    def test_real_diff_with_added_file(self):
        text = self.git('diff', 'HEAD~1', 'HEAD')
        self.assertEqual(self.r._sniff_stdin_kind(text), 'diff')
        rows, _blocks = self.r._parse_stdin_diff(text)
        self.assertEqual(rows, [('A', 'delta.txt')])

    def test_real_diff_cached_rename(self):
        text = self.git('diff', '--cached')
        rows, _blocks = self.r._parse_stdin_diff(text)
        self.assertIn(('M', 'alpha.txt'), rows)
        self.assertIn(('R', 'moved.txt'), rows)

    def test_real_log_and_variants(self):
        for args in (['log'], ['log', '-p'], ['log', '--stat'],
                     ['log', '--decorate']):
            text = self.git(*args)
            self.assertEqual(self.r._sniff_stdin_kind(text), 'log', args)
            rows, blocks = self.r._parse_stdin_log(text)
            self.assertEqual(
                [row[3] for row in rows],
                ['second commit adds delta', 'first commit'], args)
            self.assertEqual([row[1] for row in rows], ['T', 'T'], args)
            self.assertEqual(len(blocks), 2, args)
        # -p blocks carry their patch; --decorate carries the HEAD deco.
        _rows, blocks = self.r._parse_stdin_log(self.git('log', '-p'))
        self.assertIn('diff --git a/delta.txt b/delta.txt', blocks[0])
        rows, _blocks = self.r._parse_stdin_log(self.git('log', '--decorate'))
        self.assertIn('HEAD', rows[0][4])

    def test_real_porcelain_line_and_z_forms_agree(self):
        line_text = self.git('status', '--porcelain')
        z_text = self.git('status', '--porcelain', '-z')
        self.assertEqual(self.r._sniff_stdin_kind(line_text), 'porcelain')
        self.assertEqual(self.r._sniff_stdin_kind(z_text), 'porcelain')
        line_rows = self.r._parse_porcelain_lines(line_text)
        z_rows = self.r._parse_porcelain_z(z_text)
        self.assertEqual(sorted(line_rows), sorted(z_rows))
        self.assertIn(('M ', 'alpha.txt'), line_rows)
        self.assertIn((' M', 'beta.txt'), line_rows)
        self.assertIn(('R ', 'moved.txt'), line_rows)   # arrow → new path
        self.assertIn(('??', 'untracked.txt'), line_rows)

    def test_real_human_status(self):
        text = self.git('status')
        self.assertEqual(self.r._sniff_stdin_kind(text), 'human')
        rows = self.r._parse_human_status(text)
        self.assertIn(('M ', 'alpha.txt'), rows)
        self.assertIn((' M', 'beta.txt'), rows)
        self.assertIn(('R ', 'moved.txt'), rows)
        self.assertIn(('??', 'untracked.txt'), rows)


class _RaiseOnRead:
    """A stdin stand-in whose ``read()`` raises — proves stdin is NOT
    consumed outside the explicit ``-`` mode."""

    def read(self):  # pragma: no cover - only hit on a regression
        raise AssertionError('sys.stdin.read() called outside - mode')


class TestStdinMain(unittest.TestCase):
    """``main()`` wires ``-`` to the ingest path and errors out cleanly.

    Drives ``main()`` with a stubbed stdin / stderr and the no-op
    ``browse_tui`` stub (the run loop is never reached — the stub
    Browser lacking ``run`` raises ``AttributeError``, by which point
    everything asserted on has landed) — the harness mirrors the other
    recipes' stdin-mode unit tests.
    """

    def setUp(self):
        self.r = _load_recipe()
        self._argv = list(sys.argv)

    def tearDown(self):
        sys.argv[:] = self._argv

    def _run_main(self, stdin, argv):
        """Drive ``main()``; return ``(exit_code_or_None, stderr_text)``."""
        fake = stdin if hasattr(stdin, 'read') else io.StringIO(stdin)
        err = io.StringIO()
        saved_in, saved_err = self.r.sys.stdin, self.r.sys.stderr
        self.r.sys.argv[:] = list(argv)
        code = None
        try:
            self.r.sys.stdin, self.r.sys.stderr = fake, err
            try:
                self.r.main()
            except SystemExit as e:
                code = e.code
            except AttributeError:
                pass        # stub Browser has no .run — past everything asserted
        finally:
            self.r.sys.stdin, self.r.sys.stderr = saved_in, saved_err
        return code, err.getvalue()

    def test_stdin_diff_ingests_without_any_git(self):
        # Poison EVERY git seam: a `-` startup must touch none of them
        # (no `git` binary and no repo are required for piped input).
        self.r._run_git = _GitPoison(self)
        self.r._git_color = _GitPoison(self)
        self.r.shutil = types.SimpleNamespace(which=_GitPoison(self))
        code, err = self._run_main(_DIFF_TEXT, ['browse-git', '-'])
        self.assertIsNone(code)
        self.assertEqual(err, '')
        self.assertEqual(self.r._STDIN_KIND, 'diff')
        self.assertEqual([row[1] for row in self.r._STDIN_ROWS],
                         ['keep.txt', 'brand.txt', 'gone.txt', 'new.txt'])
        # The Browser was constructed with the stdin window title.
        self.assertEqual(self.r._browser._args[0].title,
                         'browse-git [stdin: diff]')

    def test_stdin_log_and_status_set_their_kinds(self):
        code, _err = self._run_main(_LOG_TEXT, ['browse-git', '-'])
        self.assertIsNone(code)
        self.assertEqual(self.r._STDIN_KIND, 'log')

        self.r = _load_recipe()
        code, _err = self._run_main(' M beta.txt\n', ['browse-git', '-'])
        self.assertIsNone(code)
        self.assertEqual(self.r._STDIN_KIND, 'status')
        self.assertEqual(self.r._STDIN_ROWS, [(' M', 'beta.txt')])

        self.r = _load_recipe()
        code, _err = self._run_main(_HUMAN_STATUS_TEXT, ['browse-git', '-'])
        self.assertIsNone(code)
        self.assertEqual(self.r._STDIN_KIND, 'status')
        self.assertIn(('??', 'untracked.txt'), self.r._STDIN_ROWS)

    def test_unrecognized_stdin_exits_2_with_stderr_only(self):
        out = io.StringIO()
        saved_out = self.r.sys.stdout
        try:
            self.r.sys.stdout = out
            code, err = self._run_main('not git output at all\n',
                                       ['browse-git', '-'])
        finally:
            self.r.sys.stdout = saved_out
        self.assertEqual(code, 2)
        self.assertIn('unrecognized stdin input', err)
        self.assertEqual(out.getvalue(), '')      # nothing on stdout
        self.assertIsNone(self.r._STDIN_KIND)
        self.assertIsNone(self.r._browser)        # the UI never started

    def test_empty_stdin_exits_2(self):
        code, err = self._run_main('', ['browse-git', '-'])
        self.assertEqual(code, 2)
        self.assertIn('empty stdin input', err)
        self.assertIsNone(self.r._browser)

    def test_dash_with_other_args_is_a_usage_error_stdin_unread(self):
        for argv in (['browse-git', '-', 'HEAD'],
                     ['browse-git', 'HEAD', '-'],
                     ['browse-git', '--status', '-'],
                     ['browse-git', '-n', '5', '-'],
                     ['browse-git', '-', '--all']):
            self.r = _load_recipe()
            code, err = self._run_main(_RaiseOnRead(), argv)
            self.assertEqual(code, 2, argv)
            self.assertIn('cannot be combined', err)
            self.assertIsNone(self.r._STDIN_KIND)

    def test_bare_invocation_never_reads_stdin(self):
        # Preconditions are stubbed green (git present, inside a work
        # tree) so main() runs through to the Browser; the raise-on-read
        # stdin proves the bare form never touches the stream (D8).
        self.r.shutil = types.SimpleNamespace(
            which=lambda name: f'/usr/bin/{name}')
        self.r._run_git = (
            lambda *a: subprocess.CompletedProcess(a, 0, '', ''))
        code, err = self._run_main(_RaiseOnRead(), ['browse-git'])
        self.assertIsNone(code)
        self.assertEqual(err, '')
        self.assertIsNone(self.r._STDIN_KIND)
        self.assertEqual(self.r._browser._args[0].title,
                         'browse-git [commits]')

    def test_tty_dash_value_is_not_the_stdin_positional(self):
        # ``--tty -`` is the framework's UI-over-std-streams flag value
        # (consumed by Browser.run()), not the stdin positional: main()
        # must fall through to repo mode — no usage error, no ingest,
        # stdin untouched. Same for the one-token ``--tty=-`` spelling.
        for argv in (['browse-git', '--tty', '-'],
                     ['browse-git', '--tty=-']):
            self.r = _load_recipe()
            self.r.shutil = types.SimpleNamespace(
                which=lambda name: f'/usr/bin/{name}')
            self.r._run_git = (
                lambda *a: subprocess.CompletedProcess(a, 0, '', ''))
            code, err = self._run_main(_RaiseOnRead(), argv)
            self.assertIsNone(code, argv)
            self.assertEqual(err, '', argv)
            self.assertIsNone(self.r._STDIN_KIND, argv)
        # Contrast: a true positional ``-`` still enters stdin mode.
        self.r = _load_recipe()
        code, _err = self._run_main(' M beta.txt\n', ['browse-git', '-'])
        self.assertIsNone(code)
        self.assertEqual(self.r._STDIN_KIND, 'status')

    def test_tty_device_path_is_consumed_not_a_positional(self):
        # ``browse-git --tty /dev/pts/N`` runs in normal repo mode: the
        # ``--tty`` value (a terminal device path, consumed by
        # Browser.run()) must NOT be classified as a pathspec/rev. Stub
        # git present + inside a work tree so main() reaches the Browser;
        # the raise-on-read stdin proves repo mode (no ``-`` ingest), and
        # the empty rev/path filters + plain ``[commits]`` title prove the
        # device path was not taken as a positional. ``--tty=PATH`` too.
        for argv in (['browse-git', '--tty', '/dev/pts/9'],
                     ['browse-git', '--tty=/dev/pts/9']):
            self.r = _load_recipe()
            self.r.shutil = types.SimpleNamespace(
                which=lambda name: f'/usr/bin/{name}')
            self.r._run_git = (
                lambda *a: subprocess.CompletedProcess(a, 0, '', ''))
            code, err = self._run_main(_RaiseOnRead(), argv)
            self.assertIsNone(code, argv)
            self.assertEqual(err, '', argv)
            self.assertIsNone(self.r._STDIN_KIND, argv)
            self.assertEqual(self.r._revs, [], argv)
            self.assertEqual(self.r._paths, [], argv)
            self.assertEqual(self.r._browser._args[0].title,
                             'browse-git [commits]', argv)

    def test_help_invocation_never_reads_stdin(self):
        code, _err = self._run_main(_RaiseOnRead(), ['browse-git', '--help'])
        self.assertIsNone(code)
        self.assertIsNone(self.r._STDIN_KIND)

    # -- auto-detect: a piped (non-tty) stdin synthesizes ``-`` --------

    def test_piped_no_positional_ingests_without_dash(self):
        # ``git diff | browse-git`` (no explicit ``-``): a non-tty fd 0
        # makes main() synthesize ``-`` and ingest the piped output —
        # skipping the git/repo preconditions, exactly as the lone ``-``
        # does. Poison every git seam to prove none is touched.
        self.r._run_git = _GitPoison(self)
        self.r._git_color = _GitPoison(self)
        self.r.shutil = types.SimpleNamespace(which=_GitPoison(self))
        with _piped_stdin():
            code, err = self._run_main(_DIFF_TEXT, ['browse-git'])
        self.assertIsNone(code)
        self.assertEqual(err, '')
        self.assertEqual(self.r._STDIN_KIND, 'diff')
        self.assertEqual(self.r._browser._args[0].title,
                         'browse-git [stdin: diff]')

    def test_piped_with_positional_is_a_usage_error(self):
        # ``git diff | browse-git HEAD``: the synthesized ``-`` collides
        # with the rev/path positional, so the existing combine error
        # fires (exit 2) and stdin is never read.
        with _piped_stdin():
            code, err = self._run_main(_RaiseOnRead(), ['browse-git', 'HEAD'])
        self.assertEqual(code, 2)
        self.assertIn('cannot be combined', err)
        self.assertIsNone(self.r._STDIN_KIND)

    def test_piped_empty_exits_2(self):
        # A non-tty empty stdin synthesizes ``-`` and flows into the
        # existing empty handling: the "empty stdin input" error (exit 2).
        # No emptiness special-casing.
        with _piped_stdin():
            code, err = self._run_main('', ['browse-git'])
        self.assertEqual(code, 2)
        self.assertIn('empty stdin input', err)
        self.assertIsNone(self.r._browser)

    def test_piped_help_flag_is_exempt_from_auto_detect(self):
        # ``git diff | browse-git --help``: the synthesized ``-`` must NOT
        # be injected (it would trip the combine error before the
        # framework's -h/--help auto-detect). stdin is left untouched and
        # no usage error fires. (End-to-end help output is covered in
        # test/ui/test_help_text.py.)
        with _piped_stdin():
            code, err = self._run_main(_RaiseOnRead(), ['browse-git', '--help'])
        self.assertIsNone(code)
        self.assertEqual(err, '')
        self.assertIsNone(self.r._STDIN_KIND)


class TestStdinActionGating(unittest.TestCase):
    """In stdin mode the git-rerunning actions flash instead of acting."""

    def setUp(self):
        self.r = _load_recipe()

    def _ctx(self):
        calls = {'flashes': [], 'refresh': 0}

        class Ctx:
            def flash(self, text, log=False):
                calls['flashes'].append(text)

            def refresh(self, id=None, on_complete=None):
                calls['refresh'] += 1

            def collapse_all(self):
                pass

            def pick(self, *_a, **_kw):
                raise AssertionError('pick reached in stdin mode')

        return Ctx(), calls

    def test_switch_mode_flashes_and_stays(self):
        self.r._STDIN_KIND = 'diff'
        ctx, calls = self._ctx()
        self.r.switch_mode(ctx)
        self.assertEqual(calls['flashes'],
                         ['mode switch not available for piped input'])
        self.assertEqual(calls['refresh'], 0)
        self.assertEqual(self.r._mode, 'commits')   # unchanged

    def test_toggle_tree_flashes_and_keeps_flag(self):
        self.r._STDIN_KIND = 'log'
        before = self.r._tree_mode
        ctx, calls = self._ctx()
        self.r.toggle_tree(ctx)
        self.assertEqual(calls['flashes'],
                         ['commit graph not available for piped input'])
        self.assertEqual(calls['refresh'], 0)
        self.assertEqual(self.r._tree_mode, before)

    def test_repo_mode_toggle_tree_unaffected_by_gate(self):
        self.assertIsNone(self.r._STDIN_KIND)
        ctx, calls = self._ctx()
        self.r.toggle_tree(ctx)
        self.assertEqual(calls['refresh'], 1)       # still refreshes

    def test_edit_file_covers_sfile_rows(self):
        # A piped-diff file row maps to a working-tree path at id[2];
        # outside a repo the rev-parse fails and the path opens as-is.
        self.r._run_git = (
            lambda *a: subprocess.CompletedProcess(a, 1, '', ''))
        opened = []

        class Ctx:
            cursor = self.r.Item(id=('sfile', 0, 'keep.txt'))

            def run_external(self, argv):
                opened.append(argv)

        ctx = Ctx()
        self.r.edit_file(ctx)
        self.assertEqual([argv[-1] for argv in opened], ['keep.txt'])
        # A piped-log commit row has no path → no-op.
        ctx.cursor = self.r.Item(id=('slog', 0))
        self.r.edit_file(ctx)
        self.assertEqual(len(opened), 1)

    def test_window_title_shows_stdin_kind(self):
        for kind in ('diff', 'log', 'status'):
            self.r._STDIN_KIND = kind
            self.assertEqual(self.r._window_title(),
                             f'browse-git [stdin: {kind}]')

    def test_on_enter_toggles_the_umbrella_expand_collapse(self):
        # The ('sdiff',) umbrella has children, so Enter flips its
        # expand/collapse exactly like any expandable row — never quits.
        expanded = set()
        collapsed = []

        class Ctx:
            cursor = self.r.Item(id=('sdiff',), has_children=True)
            state = types.SimpleNamespace(expanded=expanded)

            def expand(self, id, autoscroll=False):
                expanded.add(id)

            def collapse(self, id):
                collapsed.append(id)
                expanded.discard(id)

        ctx = Ctx()
        self.r.on_enter(ctx)                       # closed → expand
        self.assertIn(('sdiff',), expanded)
        self.r.on_enter(ctx)                       # open → collapse
        self.assertEqual(collapsed, [('sdiff',)])

    def test_edit_file_is_a_no_op_on_the_umbrella(self):
        # ``E`` only maps file/status/sfile rows to a path; the umbrella's
        # ('sdiff',) id falls through to a clean no-op (no git, no open).
        self.r._run_git = _GitPoison(self)
        opened = []

        class Ctx:
            cursor = self.r.Item(id=('sdiff',))

            def run_external(self, argv):
                opened.append(argv)

        self.r.edit_file(Ctx())
        self.assertEqual(opened, [])


class TestRunGitWithoutGit(unittest.TestCase):
    """A missing git binary degrades, never raises (gitless regression).

    ``browse-git -`` starts without the git-on-PATH precondition, so a
    gitless environment is in-contract there. ``_run_git`` folds the
    spawn's ``FileNotFoundError`` into the failed-CompletedProcess shape
    (rc 127, message in stderr) every caller already branches on —
    before this, ``E`` on a piped-diff row crashed the whole UI through
    the unguarded key dispatch.
    """

    def setUp(self):
        self.r = _load_recipe()

        def no_git(cmd, **kwargs):
            raise FileNotFoundError(2, 'No such file or directory', 'git')

        # Same recipe-module-only subprocess swap as TestColorizeDiff;
        # CompletedProcess stays real for the fold-to-failure branch.
        self.r.subprocess = types.SimpleNamespace(
            run=no_git, CompletedProcess=subprocess.CompletedProcess)

    def test_run_git_returns_failed_completedprocess(self):
        result = self.r._run_git('rev-parse', '--show-toplevel')
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, '')
        self.assertIn('git not found', result.stderr)

    def test_edit_file_on_sfile_degrades_to_cwd_relative_path(self):
        # E in a gitless stdin session: no exception, the work-tree-root
        # resolve fails quietly, and the cwd-relative path opens in
        # $EDITOR — the session stays alive.
        self.r._STDIN_KIND = 'diff'
        opened = []

        class Ctx:
            cursor = self.r.Item(id=('sfile', 0, 'keep.txt'))

            def run_external(self, argv):
                opened.append(argv)

        self.r.edit_file(Ctx())
        self.assertEqual([argv[-1] for argv in opened], ['keep.txt'])

    def test_git_color_surfaces_the_message_not_an_exception(self):
        # Preview helpers built on _git_color show the message as text.
        self.assertIn('git not found', self.r._git_color('show', 'HEAD'))


class TestToggleTree(unittest.TestCase):
    """``toggle_tree`` flips ``_tree_mode`` and refreshes the root."""

    def setUp(self):
        self.r = _load_recipe()

    def test_flip_and_refresh(self):
        calls = {'refresh': 0, 'flashes': []}

        class Ctx:
            def flash(self, text, log=False):
                calls['flashes'].append(text)

            def refresh(self, id=None, on_complete=None):
                calls['refresh'] += 1

        ctx = Ctx()
        self.r._tree_mode = False
        self.r.toggle_tree(ctx)
        self.assertTrue(self.r._tree_mode)
        self.assertEqual(calls['refresh'], 1)
        self.assertEqual(calls['flashes'], ['commit graph: on'])
        # A second toggle flips it back and refreshes again.
        self.r.toggle_tree(ctx)
        self.assertFalse(self.r._tree_mode)
        self.assertEqual(calls['refresh'], 2)
        self.assertEqual(calls['flashes'][-1], 'commit graph: off')


class _FakeGit:
    """A stub ``_run_git`` driven by a {argv-tuple: (rc, stdout)} table.

    Maps the EXACT git arg tuple to a CompletedProcess; an unlisted arg
    tuple is a failed (rc 1) process, so a path absent from the canned
    ``cat-file`` / ``show`` set simply "doesn't resolve" rather than raising.
    Records every invocation in ``calls`` for argv assertions.
    """

    def __init__(self, table):
        self._table = {tuple(k): v for k, v in table.items()}
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        rc, out = self._table.get(args, (1, ''))
        return subprocess.CompletedProcess(['git', *args], rc, out, '')


class _LaunchCtx:
    """A ``ctx`` stand-in for ``on_enter`` / ``_md_launch`` tests.

    Records ``run_external`` calls (argv-or-shell-string, env, ``keep_screen``)
    and any ``flash`` text, plus the expand/collapse a toggle would issue.
    """

    def __init__(self, cursor, expanded=None):
        self.cursor = cursor
        self.calls = []          # (cmd, env, keep_screen, stdin_text)
        self.flashes = []
        self.expanded = expanded if expanded is not None else set()
        self.collapsed = []
        self.state = types.SimpleNamespace(expanded=self.expanded)

    def run_external(self, cmd, env=None, *, keep_screen=False, stdin_text=None):
        self.calls.append((cmd, env, keep_screen, stdin_text))
        return 0

    def flash(self, text, log=False):
        self.flashes.append(text)

    def expand(self, id, autoscroll=False):
        self.expanded.add(id)

    def collapse(self, id):
        self.collapsed.append(id)
        self.expanded.discard(id)


class TestMdLauncherCommitMessage(unittest.TestCase):
    """Commit-message ``[md] References`` umbrella, resolved in the TREE (#1017).

    A commit's message ``.md`` refs are resolved against the COMMIT TREE
    (``git cat-file -e sha:path``), NOT the working tree — git is stubbed so
    the tests pin which paths exist in the revision, never the filesystem.
    """

    def setUp(self):
        # Force a launcher-capable md_doc: drop any stubless md_doc cached by
        # another module so _load_recipe's import (under its browse_tui stub)
        # redefines the launcher block. See TESTING.md's import-order note.
        sys.modules.pop('md_doc', None)
        self.r = _load_recipe()
        self.assertTrue(hasattr(self.r._md_doc, 'launcher_row'),
                        'md_doc launcher block must be importable for these tests')

    def _git(self, message, *, tree=(), root='/repo'):
        """Stub ``_run_git``: ``message`` is the commit body, ``tree`` the set
        of paths that exist at ``SHA``; ``--show-toplevel`` reports ``root``."""
        table = {
            ('show', '-s', '--format=%B', 'SHA'): (0, message),
            ('rev-parse', '--show-toplevel'): (0, root + '\n'),
        }
        for p in tree:
            table[('cat-file', '-e', f'SHA:{p}')] = (0, '')
        self.r._run_git = _FakeGit(table)

    def test_refs_resolve_against_tree_not_filesystem(self):
        # docs/real.md exists in the tree → kept; gone.md does not → dropped;
        # the captured tokens come straight from the message prose.
        self._git('See docs/real.md and gone.md for details\n',
                  tree=('docs/real.md',))
        refs = self.r._commit_md_refs('SHA')
        self.assertEqual([p for p, _ in refs], ['docs/real.md'])
        self.assertEqual(refs[0][1], 'docs/real.md')   # label = repo-rel path

    def test_dotslash_normalised_and_deduped(self):
        # ``./a.md`` and ``a.md`` are the same tree path: the leading ./ is
        # normalised off so the dedup key + the launch ``sha:path`` agree.
        self._git('first ./a.md then a.md again\n', tree=('a.md',))
        refs = self.r._commit_md_refs('SHA')
        self.assertEqual([p for p, _ in refs], ['a.md'])   # one entry, no ./

    def test_no_resolving_refs_yields_no_umbrella(self):
        self._git('plain subject, no markdown links\n')
        self.assertEqual(self.r._commit_md_refs('SHA'), [])
        self.assertEqual(self.r._commit_md_prefix('SHA'), [])

    def test_prefix_is_the_references_umbrella(self):
        self._git('see r.md\n', tree=('r.md',))
        prefix = self.r._commit_md_prefix('SHA')
        self.assertEqual(len(prefix), 1)
        umb = prefix[0]
        self.assertEqual(umb.id, ('md-refs', 'SHA'))
        self.assertEqual(umb.tag, 'md')
        self.assertTrue(umb.has_children)

    def test_get_children_commit_prepends_umbrella_before_files(self):
        # The commit row's children = umbrella FIRST, then its files.
        self._git('refs a.md\n', tree=('a.md',))
        self.r._commit_files = lambda sha: [
            self.r.Item(id=('file', sha, 'src/x.py'), title='src/x.py')]
        rows = self.r.get_children(('commit', 'SHA'))
        self.assertEqual([r.id for r in rows],
                         [('md-refs', 'SHA'), ('file', 'SHA', 'src/x.py')])

    def test_get_children_commit_no_refs_is_files_only(self):
        self._git('no links here\n')
        self.r._commit_files = lambda sha: ['FILES']
        self.assertEqual(self.r.get_children(('commit', 'SHA')), ['FILES'])

    def test_umbrella_children_are_blob_launcher_rows(self):
        # Expanding ('md-refs', SHA) yields one [md ↗] row per resolving ref,
        # each carrying a ('blob', SHA, path) spec for a stdin launch.
        self._git('see a.md and docs/b.md\n', tree=('a.md', 'docs/b.md'))
        rows = self.r.get_children(('md-refs', 'SHA'))
        self.assertEqual([r.id for r in rows], [
            ('launch', 'SHA', 'blob', 'SHA', 'a.md'),
            ('launch', 'SHA', 'blob', 'SHA', 'docs/b.md'),
        ])
        for r in rows:
            self.assertFalse(r.has_children)
            self.assertEqual(r.tag, 'md ↗')

    def test_inert_when_md_doc_absent(self):
        self._git('see a.md\n', tree=('a.md',))
        self.r._md_doc = None
        self.assertEqual(self.r._commit_md_refs('SHA'), [])
        self.assertEqual(self.r._commit_md_prefix('SHA'), [])


class TestMdLauncherFileRows(unittest.TestCase):
    """A ``.md`` file row gets one ``→ browse`` child; spec depends on kind."""

    def setUp(self):
        sys.modules.pop('md_doc', None)
        self.r = _load_recipe()
        self.assertTrue(hasattr(self.r._md_doc, 'launcher_row'))

    def test_md_leaves_become_expandable_others_stay_leaves(self):
        # _md_launchable gates has_children on the three .md leaf builders.
        self.assertTrue(self.r._md_launchable('docs/x.md'))
        self.assertTrue(self.r._md_launchable('R.MD'))
        self.assertFalse(self.r._md_launchable('src/x.py'))
        self.assertFalse(self.r._md_launchable(('file', 'sha', 'x.md')))  # not a str

        # _status_leaf is the shared working-tree/commit leaf builder.
        md_leaf = self.r._status_leaf('M ', 'notes.md')
        txt_leaf = self.r._status_leaf('M ', 'code.py')
        self.assertTrue(md_leaf.has_children)
        self.assertFalse(txt_leaf.has_children)

    def test_committed_md_file_child_is_blob_spec(self):
        # ('file', sha, path).md → ('blob', sha, path): extracted from the rev.
        rows = self.r.get_children(('file', 'SHA', 'docs/g.md'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id,
                         ('launch', ('file', 'SHA', 'docs/g.md'), 'blob', 'SHA', 'docs/g.md'))
        self.assertEqual(rows[0].title, '→ browse')
        self.assertEqual(rows[0].tag, 'md ↗')
        self.assertFalse(rows[0].has_children)

    def test_worktree_md_file_child_is_wtfile_spec(self):
        # ('status', xy, path).md → ('wtfile', <root>/<path>): the on-disk file.
        self.r._run_git = _FakeGit(
            {('rev-parse', '--show-toplevel'): (0, '/repo\n')})
        rows = self.r.get_children(('status', 'M ', 'a.md'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id,
                         ('launch', ('status', 'M ', 'a.md'), 'wtfile', '/repo/a.md'))

    def test_untracked_md_file_child_is_wtfile_spec(self):
        # Untracked rows are ('status', '??', path) too → same wtfile path.
        self.r._run_git = _FakeGit(
            {('rev-parse', '--show-toplevel'): (0, '/repo\n')})
        rows = self.r.get_children(('status', '??', 'new.md'))
        self.assertEqual(rows[0].id[2:], ('wtfile', '/repo/new.md'))

    def test_worktree_md_child_path_relative_when_no_repo_root(self):
        # Outside a repo (rev-parse fails) the path opens cwd-relative (as-is).
        self.r._run_git = _FakeGit({})   # rev-parse → rc 1
        rows = self.r.get_children(('status', 'M ', 'a.md'))
        self.assertEqual(rows[0].id[2:], ('wtfile', 'a.md'))

    def test_stash_md_file_child_is_blob_with_stash_rev(self):
        # ('stash', n, path).md → ('blob', 'stash@{n}', path).
        rows = self.r.get_children(('stash', 2, 'd.md'))
        self.assertEqual(rows[0].id[2:], ('blob', 'stash@{2}', 'd.md'))

    def test_non_md_file_rows_stay_leaves(self):
        # A non-.md file/status/stash leaf still routes to no children.
        for leaf in (('file', 'SHA', 'x.py'), ('status', 'M ', 'y.txt'),
                     ('stash', 0, 'z.rs')):
            self.assertEqual(self.r.get_children(leaf), [], leaf)

    def test_inert_when_md_doc_absent(self):
        self.r._md_doc = None
        # No arrow on the leaf, and an "expanded" .md leaf falls through to [].
        self.assertFalse(self.r._status_leaf('M ', 'a.md').has_children)
        self.assertEqual(self.r.get_children(('file', 'SHA', 'a.md')), [])


class TestMdLauncherEnterDispatch(unittest.TestCase):
    """Enter launches a ``[md ↗]`` row, else keeps the expand/collapse toggle."""

    def setUp(self):
        sys.modules.pop('md_doc', None)
        self.r = _load_recipe()
        self.assertTrue(hasattr(self.r._md_doc, 'launcher_row'))

    def test_enter_on_blob_launcher_pipes_git_show_on_stdin(self):
        # A blob launcher row: Enter fetches ``git show rev:path`` and launches
        # it on stdin (content form), with the repo root as --root.
        self.r._run_git = _FakeGit({
            ('rev-parse', '--show-toplevel'): (0, '/repo\n'),
            ('show', 'SHA:docs/a.md'): (0, '# Doc A\nbody\n'),
        })
        row = self.r.Item(id=('launch', 'SHA', 'blob', 'SHA', 'docs/a.md'))
        ctx = _LaunchCtx(row)
        self.r.on_enter(ctx)
        self.assertEqual(len(ctx.calls), 1)
        cmd, env, keep, stdin_text = ctx.calls[0]
        # Content form: plain argv reading from stdin (`-`); the blob text rides
        # the stdin pipe, NOT argv or env (the E2BIG-safe channel).
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[:2], ['browse-md', '-'])
        self.assertEqual(stdin_text, '# Doc A\nbody\n')
        self.assertIsNone(env)
        self.assertNotIn('# Doc A\nbody\n', cmd)
        self.assertEqual(cmd[cmd.index('--root') + 1], '/repo')
        self.assertTrue(keep)               # parent keeps the alt screen
        self.assertIn('--no-alt-screen', cmd)
        self.assertIn('--quit-on-scope-up', cmd)

    def test_enter_on_wtfile_launcher_opens_by_path(self):
        # A wtfile launcher row: Enter opens the on-disk file by argv (no git
        # show), repo root as --root.
        self.r._run_git = _FakeGit(
            {('rev-parse', '--show-toplevel'): (0, '/repo\n')})
        row = self.r.Item(id=('launch', ('status', 'M ', 'a.md'),
                              'wtfile', '/repo/a.md'))
        ctx = _LaunchCtx(row)
        self.r.on_enter(ctx)
        self.assertEqual(len(ctx.calls), 1)
        cmd, env, keep, stdin_text = ctx.calls[0]
        self.assertIsInstance(cmd, list)        # path form is plain argv
        self.assertEqual(cmd[0], 'browse-md')
        self.assertIn('/repo/a.md', cmd)
        self.assertIsNone(env)
        self.assertIsNone(stdin_text)           # path form: no stdin pipe
        self.assertEqual(cmd[cmd.index('--root') + 1], '/repo')
        self.assertTrue(keep)

    def test_enter_on_missing_blob_flashes_and_does_not_launch(self):
        # The blob no longer resolves (git show fails) → flash, no launch.
        self.r._run_git = _FakeGit(
            {('rev-parse', '--show-toplevel'): (0, '/repo\n')})   # show absent
        row = self.r.Item(id=('launch', 'SHA', 'blob', 'SHA', 'gone.md'))
        ctx = _LaunchCtx(row)
        self.r.on_enter(ctx)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(len(ctx.flashes), 1)
        self.assertIn('gone.md', ctx.flashes[0])

    def test_enter_on_expandable_non_launcher_toggles(self):
        # A normal expandable row keeps the expand/collapse toggle — no launch.
        item = self.r.Item(id=('commit', 'SHA'), has_children=True)
        ctx = _LaunchCtx(item)
        self.r._run_git = _GitPoison(self)      # toggling must not shell out
        self.r.on_enter(ctx)                    # closed → expand
        self.assertIn(('commit', 'SHA'), ctx.expanded)
        self.assertEqual(ctx.calls, [])
        self.r.on_enter(ctx)                    # open → collapse
        self.assertEqual(ctx.collapsed, [('commit', 'SHA')])

    def test_enter_on_leaf_is_noop(self):
        item = self.r.Item(id=('file', 'SHA', 'x.py'), has_children=False)
        ctx = _LaunchCtx(item)
        self.r.on_enter(ctx)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(ctx.collapsed, [])
        self.assertEqual(ctx.expanded, set())

    def test_enter_with_no_cursor_is_noop(self):
        ctx = _LaunchCtx(None)
        self.r.on_enter(ctx)                    # must not raise
        self.assertEqual(ctx.calls, [])

    def test_launch_inert_when_md_doc_absent(self):
        self.r._md_doc = None
        row = self.r.Item(id=('launch', 'SHA', 'blob', 'SHA', 'a.md'))
        ctx = _LaunchCtx(row)
        self.r.on_enter(ctx)                    # guarded no-op
        self.assertEqual(ctx.calls, [])


class TestMdLauncherPreview(unittest.TestCase):
    """get_preview renders the markdown a launcher row would open."""

    def setUp(self):
        sys.modules.pop('md_doc', None)
        self.r = _load_recipe()
        self.assertTrue(hasattr(self.r._md_doc, 'launcher_row'))
        self.r._MD_COLOR = False   # raw text so we can assert content directly

    def test_preview_wtfile_launcher_shows_file_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'a.md')
            with open(p, 'w', encoding='utf-8') as f:
                f.write('# Working tree\nhello\n')
            pv = self.r.get_preview(('launch', ('status', 'M ', 'a.md'),
                                     'wtfile', p))
        self.assertIn('# Working tree', pv)
        self.assertIn('hello', pv)

    def test_preview_blob_launcher_shows_git_show_content(self):
        self.r._run_git = _FakeGit({('show', 'SHA:docs/a.md'): (0, '# Blob\nx\n')})
        pv = self.r.get_preview(('launch', 'SHA', 'blob', 'SHA', 'docs/a.md'))
        self.assertIn('# Blob', pv)
        self.assertIn('x', pv)

    def test_preview_wtfile_unreadable_returns_error(self):
        pv = self.r.get_preview(('launch', ('status', '??', 'gone.md'),
                                 'wtfile', '/no/such/gone.md'))
        self.assertIn('error', pv.lower())

    def test_preview_blob_missing_returns_empty(self):
        self.r._run_git = _FakeGit({})   # git show fails → None → ''
        pv = self.r.get_preview(('launch', 'SHA', 'blob', 'SHA', 'gone.md'))
        self.assertEqual(pv, '')

    def test_preview_md_refs_umbrella_delegates_to_commit(self):
        self.r._commit_preview = lambda sha: f'COMMIT {sha}'
        self.assertEqual(self.r.get_preview(('md-refs', 'SHA')), 'COMMIT SHA')


class TestDisplayMode(unittest.TestCase):
    """``_DISPLAY_MODE`` (the 1/2/3 keys) gates ``git_row_content``'s LEADING
    columns; the commit-row emission and the filler-row pad share one notion
    of which leading columns are active (``_LEAD_FIELDS``) so they cannot
    drift. The graph column (``_tree_mode``) is INDEPENDENT of the mode.
    """

    def setUp(self):
        self.r = _load_recipe()
        # The mode is a module global flipped by the actions; restore it so a
        # mutating test never bleeds into another.
        self.addCleanup(setattr, self.r, '_DISPLAY_MODE', self.r._DISPLAY_MODE)

    def _widths(self, sha=7, author=5, date=12):
        return {'col_sha': sha, 'col_author': author, 'col_date': date}

    def _commit(self, **kw):
        defaults = dict(id=('commit', 'deadbee'), title='subj',
                        col_sha='deadbee', col_author='Al',
                        col_date='2 days ago', chips=[])
        defaults.update(kw)
        return self.r.Item(**defaults)

    def test_default_mode_is_3(self):
        # A freshly loaded recipe defaults to the full sha/author/date set.
        self.assertEqual(self.r._DISPLAY_MODE, 3)

    def test_lead_fields_table(self):
        # The single source of truth: mode 3 = sha+author+date, mode 2 = sha,
        # mode 1 = none, each in render order.
        self.assertEqual(self.r._LEAD_FIELDS, {
            3: ('col_sha', 'col_author', 'col_date'),
            2: ('col_sha',),
            1: (),
        })

    def test_mode3_emits_sha_author_date_then_subject(self):
        self.r._DISPLAY_MODE = 3
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        segs = self.r.git_row_content(self._commit(title='the subject'), ctx)
        # sha (yellow), author (dim), date (dim), subject (plain, last).
        self.assertEqual(segs, [
            ('deadbee' + '  ', *_YELLOW),
            ('Al   ' + '  ', *_DIM),
            ('2 days ago  ' + '  ', *_DIM),
            ('the subject', None, False),
        ])
        # Measured exactly the three leading columns, in order.
        self.assertEqual(ctx.calls, ['col_sha', 'col_author', 'col_date'])

    def test_mode2_emits_sha_then_subject_only(self):
        self.r._DISPLAY_MODE = 2
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        segs = self.r.git_row_content(self._commit(title='the subject'), ctx)
        # Only the sha column (yellow) survives, then the subject — no
        # author/date columns anywhere.
        self.assertEqual(segs, [
            ('deadbee' + '  ', *_YELLOW),
            ('the subject', None, False),
        ])
        # Only the sha width was measured (author/date untouched).
        self.assertEqual(ctx.calls, ['col_sha'])

    def test_mode1_emits_subject_only(self):
        self.r._DISPLAY_MODE = 1
        ctx = _FakeCtx(self._widths(sha=7, author=5, date=12))
        segs = self.r.git_row_content(self._commit(title='the subject'), ctx)
        # No leading columns at all — the subject is the sole segment.
        self.assertEqual(segs, [('the subject', None, False)])
        # No leading column was measured.
        self.assertEqual(ctx.calls, [])

    def test_chips_and_subject_order_preserved_each_mode(self):
        # Across every mode the chips follow the (gated) leading columns and
        # the subject stays LAST.
        for mode in (3, 2, 1):
            self.r._DISPLAY_MODE = mode
            ctx = _FakeCtx(self._widths())
            segs = self.r.git_row_content(
                self._commit(title='subj',
                             chips=[('HEAD', 'green'), ('main', 'cyan')]), ctx)
            # Two chips precede the subject regardless of leading-column count.
            self.assertEqual(segs[-3], ('[HEAD] ', *self.r.style('green')),
                             f'mode {mode}: HEAD chip slot')
            self.assertEqual(segs[-2], ('[main] ', *self.r.style('cyan')),
                             f'mode {mode}: main chip slot')
            self.assertEqual(segs[-1], ('subj', None, False),
                             f'mode {mode}: subject not last')

    def test_filler_pad_tracks_active_mode_leading_columns(self):
        # The filler row's blank pad spans EXACTLY the active mode's leading
        # columns (each width + 2-space gap): mode 3 = sha+author+date,
        # mode 2 = sha, mode 1 = 0 — so the graph art aligns under the
        # commits in every mode.
        widths = self._widths(sha=7, author=5, date=12)
        cases = {
            3: (7 + 2) + (5 + 2) + (12 + 2),
            2: (7 + 2),
            1: 0,
        }
        measured = {
            3: ['col_sha', 'col_author', 'col_date'],
            2: ['col_sha'],
            1: [],
        }
        for mode, expected in cases.items():
            self.r._DISPLAY_MODE = mode
            ctx = _FakeCtx(dict(widths))
            filler = self.r.Item(id=('filler', 'root', 0), title='',
                                 has_children=False, col_graph='│\\')
            segs = self.r.git_row_content(filler, ctx)
            pad_seg, art_seg = segs
            self.assertEqual(pad_seg, (' ' * expected, None, False),
                             f'mode {mode}: filler pad width')
            self.assertEqual(art_seg, ('│\\', None, False),
                             f'mode {mode}: filler art')
            self.assertEqual(ctx.calls, measured[mode],
                             f'mode {mode}: filler measured columns')

    def test_filler_pad_aligns_with_commit_graph_each_mode(self):
        # Tie it together: in every mode the filler pad equals the commit
        # row's leading-column prefix width, so the two graph columns line up.
        widths = self._widths(sha=7, author=7, date=12)
        for mode in (3, 2, 1):
            self.r._DISPLAY_MODE = mode
            commit = self._commit(col_sha='abc1234', col_author='Bernard',
                                  col_date='3 weeks ago', col_graph='•')
            filler = self.r.Item(id=('filler', 'root', 0), title='',
                                 has_children=False, col_graph='│')
            c_segs = self.r.git_row_content(commit, _FakeCtx(dict(widths)))
            f_segs = self.r.git_row_content(filler, _FakeCtx(dict(widths)))
            n_lead = len(self.r._LEAD_FIELDS[mode])
            prefix = sum(len(c_segs[i][0]) for i in range(n_lead))
            self.assertEqual(len(f_segs[0][0]), prefix,
                             f'mode {mode}: filler pad != commit lead prefix')

    def test_graph_emitted_in_every_display_mode(self):
        # The commit graph column is INDEPENDENT of the display mode: when a
        # commit carries ``col_graph`` (tree mode on) it is emitted right
        # after the leading columns in all three modes.
        for mode in (3, 2, 1):
            self.r._DISPLAY_MODE = mode
            ctx = _FakeCtx(self._widths())
            segs = self.r.git_row_content(
                self._commit(title='subj', chips=[], col_graph='•'), ctx)
            n_lead = len(self.r._LEAD_FIELDS[mode])
            # Graph sits immediately after the (gated) leading columns.
            self.assertEqual(segs[n_lead], ('• ', None, False),
                             f'mode {mode}: graph not after leading columns')
            self.assertEqual(segs[-1], ('subj', None, False),
                             f'mode {mode}: subject not last')

    def test_non_commit_row_falls_back_every_mode(self):
        # A row with neither col_sha nor col_graph falls back to
        # default_row_content (and measures no column) regardless of mode.
        for mode in (3, 2, 1):
            self.r._DISPLAY_MODE = mode
            ctx = _FakeCtx(self._widths())
            item = self.r.Item(id=('status', 'M ', 'beta.txt'),
                               title='beta.txt', tag='M', has_children=False)
            segs = self.r.git_row_content(item, ctx)
            self.assertEqual(segs, self.r.default_row_content(item, ctx),
                             f'mode {mode}: not the fallback')
            self.assertEqual(ctx.calls, [], f'mode {mode}: measured a column')

    def test_set_display_mode_flips_flashes_refreshes(self):
        # The 1/2/3 actions set _DISPLAY_MODE, flash the mode label, and
        # refresh (a pure re-render — no refetch).
        calls = {'refresh': 0, 'flashes': []}

        class Ctx:
            def flash(self, text, log=False):
                calls['flashes'].append(text)

            def refresh(self, id=None, on_complete=None):
                calls['refresh'] += 1

        ctx = Ctx()
        for mode, label in (
            (1, 'display: subject only'),
            (2, 'display: sha · subject'),
            (3, 'display: sha · author · date · subject'),
        ):
            self.r._set_display_mode(ctx, mode)
            self.assertEqual(self.r._DISPLAY_MODE, mode)
        self.assertEqual(calls['refresh'], 3)
        self.assertEqual(calls['flashes'], [
            'display: subject only',
            'display: sha · subject',
            'display: sha · author · date · subject',
        ])

    def test_actions_register_1_2_3_to_set_display_mode(self):
        # End-to-end: the recipe registers bare-digit 1/2/3 actions wired to
        # _set_display_mode (proving the keys are FREE in browse-git — the
        # framework binds only alt-1..4 — and bound to the handler). Drive
        # main() in repo mode with git stubbed, capture the config the stub
        # Browser is built with, then fire each digit handler.
        captured = {}

        class _Stub:
            def __init__(self, *a, **kw):
                self._args = a
                for k, v in kw.items():
                    setattr(self, k, v)
                captured['cfg'] = a[0] if a else None

            def run(self):
                return 0

        ok = subprocess.CompletedProcess([], 0, '', '')
        self.r.Browser = _Stub
        self.r._run_git = lambda *a, **k: ok        # rev-parse → inside work tree
        self.r.shutil = types.SimpleNamespace(which=lambda _: '/usr/bin/git')
        saved_argv = list(self.r.sys.argv)
        try:
            self.r.sys.argv[:] = ['browse-git']
            with self.assertRaises(SystemExit):     # main() ends in sys.exit(run())
                self.r.main()
        finally:
            self.r.sys.argv[:] = saved_argv

        # ``Action`` is the inert stub: its positional fields land in _args
        # (key, label, handler, requires).
        by_key = {a._args[0]: a for a in captured['cfg'].actions}
        for key in ('1', '2', '3'):
            self.assertIn(key, by_key, f'{key!r} not registered')

        class Ctx:
            def flash(self, text, log=False):
                pass

            def refresh(self, id=None, on_complete=None):
                pass

        for key, mode in (('1', 1), ('2', 2), ('3', 3)):
            by_key[key]._args[2](Ctx())             # the handler
            self.assertEqual(self.r._DISPLAY_MODE, mode,
                             f'action {key!r} set mode {self.r._DISPLAY_MODE}')


if __name__ == '__main__':
    unittest.main()
