"""Unit tests for the ``recipes/browse-fs`` columnar list (tickets #661, #1114).

The recipe is a single-file ``--run-py`` script that imports
``browse_tui`` (only available when the binary loads it). To exercise
``fs_chrome`` and ``get_children`` directly we stub ``browse_tui``
in ``sys.modules`` and load the extension-less recipe via
``SourceFileLoader`` — the same pattern as ``test/unit/test_browse_git.py``.

The point of these tests is to verify the recipe *wires* the column
helpers correctly, not to re-test the helpers themselves (``cell_ljust`` /
``cell_rjust`` are covered by Stage 1's ``test_render``). So the stub
provides functional-enough ``cell_ljust`` / ``cell_rjust`` / ``style`` /
chrome atoms (``default_row_selection`` / ``default_row_indent`` /
``default_row_expander``) and a fake ``ctx`` whose
``max_col_width_global(field)`` returns a fixed width per field.

Coverage:

* ``fs_chrome``       — the perms/size/date columns now sit in a LEFT
  *gutter* (between the selection marker and the tree indent), padded to
  their ``max_col_width_global`` width; columns align across rows of
  differing depth; an item WITHOUT ``col_perms`` emits an EMPTY gutter
  (just the default chrome atoms).
* ``get_children``    — stores ``col_perms`` / ``col_size`` / ``col_mtime``
  on each Item and no longer stuffs the size into ``tag``; the error row
  carries no ``col_*`` (so it gets an empty gutter).
"""

import contextlib
import importlib.util
import io
import os
import stat
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-fs'


# ``main()`` auto-detects a piped stdin via ``os.isatty(0)``: a non-tty fd 0
# synthesizes the lone ``-`` (stdin-list mode). The test runner's fd 0 is
# itself a pipe (non-tty), which would spuriously trip that auto-detect for
# the bare/PATH-mode cases below. Pin the whole module to an INTERACTIVE tty
# so those tests keep exercising bare/PATH mode (the historical default); the
# dedicated auto-detect tests opt back into a pipe via ``_piped_stdin``.
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
    tests can simulate ``cmd | browse-fs`` without leaking the False into
    neighbouring bare/PATH-mode cases."""
    with mock.patch('os.isatty', return_value=False):
        yield

# Sentinel the stub ``style('dim')`` returns; ``fs_row_content`` must put
# this exact (fg, bold) pair on every metadata segment.
_DIM = (242, False)


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
    """Insert a ``browse_tui`` stub the recipe can import from.

    Always installs a fresh module so a stub left behind by another
    recipe's unit test doesn't bleed in. ``Item`` keeps its kwargs as
    attributes so ``get_children`` tests can read ``.col_*`` / ``.tag``;
    ``Browser`` / ``BrowserConfig`` / ``Action`` are inert. The column
    helpers (``cell_ljust`` / ``cell_rjust`` / ``style``) and the chrome
    atoms (``default_row_selection`` / ``default_row_indent`` /
    ``default_row_expander``) are functional-but-minimal: the test data is
    plain ASCII, so ``str.ljust`` / ``str.rjust`` measure the same as the
    real cell-aware helpers, and the atoms mirror the framework's segment
    shape closely enough to prove the gutter composition.
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
    mod.cell_rjust = lambda s, width, fill=' ': s.rjust(width, fill)
    mod.style = lambda name: _DIM if name == 'dim' else (None, False)

    # Chrome atoms, mirroring the framework's segment shapes (040-state.py):
    # a single-segment list each, with the meta-row blanking rules. The fake
    # ctx below supplies the kind / selected / depth / expanded it reads.
    def _default_row_selection(item, ctx):
        marker = '  ' if ctx.kind == 'meta' else ('* ' if ctx.selected else '  ')
        return [(marker, None, False)]

    def _default_row_indent(item, ctx):
        return [('  ' * max(ctx.depth, 0), None, False)]

    def _default_row_expander(item, ctx):
        if ctx.kind == 'meta':
            marker = '  '
        elif getattr(item, 'has_children', False):
            marker = '▼ ' if ctx.expanded else '▶ '
        else:
            marker = '  '
        return [(marker, None, False)]

    mod.default_row_selection = _default_row_selection
    mod.default_row_indent = _default_row_indent
    mod.default_row_expander = _default_row_expander
    mod.recipe_argv = _stub_recipe_argv
    sys.modules['browse_tui'] = mod


def _load_recipe():
    """Load (or reload) the browse-fs recipe; returns a fresh module.

    A fresh module is built on every call so tests that mutate
    module-level globals stay isolated.
    """
    recipes_dir = str(_REPO / 'recipes')
    if recipes_dir not in sys.path:
        sys.path.insert(0, recipes_dir)
    _stub_browse_tui()
    name = '_browse_fs_under_test'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _FakeCtx:
    """A ``RowContext`` stand-in for the gutter chrome.

    ``max_col_width_global(field)`` returns a fixed width per field (and
    records the order of lookups in ``calls``). The chrome atoms also read
    ``kind`` / ``selected`` / ``depth`` / ``expanded``; defaults model an
    unselected, top-level, collapsed normal row.
    """

    def __init__(self, widths, *, kind='item', selected=False, depth=0,
                 expanded=False):
        self._widths = widths
        self.calls = []
        self.kind = kind
        self.selected = selected
        self.depth = depth
        self.expanded = expanded

    def max_col_width_global(self, field):
        self.calls.append(field)
        return self._widths[field]


def _make_item(r, **kw):
    """Build a recipe ``Item`` (the stub) carrying ``kw`` as attributes."""
    return r.Item(**kw)


class TestFsChrome(unittest.TestCase):
    """``fs_chrome`` puts perms/size/date in a LEFT gutter (ticket #1114).

    Order is ``selection, <gutter: perms size date>, indent, expander`` — the
    metadata columns sit BEFORE the tree indent/expander so they stay pinned
    at the left edge regardless of depth. The name is rendered by the default
    content (not by ``fs_chrome``), so the chrome ends at the expander.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _widths(self, perms=10, size=4, mtime=12):
        return {'col_perms': perms, 'col_size': size, 'col_mtime': mtime}

    def test_gutter_order_padding_and_no_title(self):
        ctx = _FakeCtx(self._widths(perms=10, size=4, mtime=12))
        item = _make_item(
            self.r, id='/d/f', title='f.txt',
            col_perms='-rw-r--r--', col_size='12K', col_mtime='Jun 03 11:08')
        segs = self.r.fs_chrome(item, ctx)

        # selection + 3 gutter columns + indent + expander = 6 segments.
        # The title is NOT in chrome (the default content renders it).
        self.assertEqual(len(segs), 6)
        sel, perms_seg, size_seg, mtime_seg, indent_seg, expander_seg = segs

        # Selection marker first (unselected, normal row → '  ').
        self.assertEqual(sel, ('  ', None, False))

        # Gutter columns: padded + two-space gap, dim (fg, bold) from
        # style('dim'). perms ljust, size rjust, mtime ljust.
        self.assertEqual(perms_seg, ('-rw-r--r--' + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(size_seg, (' 12K' + '  ', _DIM[0], _DIM[1]))   # rjust 4
        self.assertEqual(mtime_seg, ('Jun 03 11:08' + '  ', _DIM[0], _DIM[1]))

        # Indent (depth 0 → '') and expander (leaf → '  ') come AFTER the
        # gutter — proving the columns sit LEFT of the tree chrome.
        self.assertEqual(indent_seg, ('', None, False))
        self.assertEqual(expander_seg[0], '  ')

        # Columns measured via the GLOBAL width, in column order.
        self.assertEqual(ctx.calls, ['col_perms', 'col_size', 'col_mtime'])

    def test_size_is_right_justified(self):
        # A dir's col_size is '' — it still pads to the column width on the
        # left (rjust) so the size column right-aligns under the files.
        ctx = _FakeCtx(self._widths(perms=10, size=5, mtime=12))
        item = _make_item(
            self.r, id='/d/sub', title='sub/',
            col_perms='drwxr-xr-x', col_size='', col_mtime='Jun 03 11:08')
        # Gutter is segments[1:4] (after the selection marker); size is [2].
        size_seg = self.r.fs_chrome(item, ctx)[2]
        self.assertEqual(size_seg, ('     ' + '  ', _DIM[0], _DIM[1]))  # 5 spaces

    def test_gutter_aligns_across_differing_depths(self):
        # The whole point of max_col_width_global: a deep row's perms column
        # lines up under a shallow row's perms column. The gutter sits BEFORE
        # the indent, so the indent (which differs by depth) does NOT shift
        # the columns — every gutter segment is byte-for-byte equal across
        # rows regardless of depth.
        widths = self._widths(perms=10, size=6, mtime=12)
        shallow = _make_item(
            self.r, id='/d/a', title='a',
            col_perms='-rw-r--r--', col_size='3B', col_mtime='Jun 03 11:08')
        deep = _make_item(
            self.r, id='/d/x/y/b', title='b',
            col_perms='drwxr-xr-x', col_size='123456', col_mtime='May 01 09:00')
        segs_shallow = self.r.fs_chrome(shallow, _FakeCtx(widths, depth=0))
        segs_deep = self.r.fs_chrome(deep, _FakeCtx(widths, depth=3))

        # Gutter columns are indices 1/2/3 (after the selection marker) in
        # BOTH rows — byte-for-byte equal width, so a deep row's columns line
        # up under a shallow row's. The deeper indent comes AFTER the gutter.
        for col in (1, 2, 3):
            self.assertEqual(len(segs_shallow[col][0]), len(segs_deep[col][0]),
                             f'gutter column {col} widths differ across rows')
        # Concrete widths: field width + 2-space gap.
        self.assertEqual(len(segs_shallow[1][0]), 10 + 2)   # perms
        self.assertEqual(len(segs_shallow[2][0]), 6 + 2)    # size
        self.assertEqual(len(segs_shallow[3][0]), 12 + 2)   # mtime

        # The indent segment (index -2) reflects the depth and is LONGER for
        # the deep row, confirming the gutter precedes (is unaffected by) it.
        self.assertEqual(segs_shallow[-2][0], '')
        self.assertEqual(segs_deep[-2][0], '  ' * 3)

    def test_empty_gutter_when_no_col_perms(self):
        # An item without col_perms (error / synthetic) emits an EMPTY gutter:
        # just selection + indent + expander, no metadata columns and no
        # global-width measurement. The default content then renders the row.
        ctx = _FakeCtx(self._widths())
        item = _make_item(self.r, id=('err', '/x'), title='[error] boom',
                          tag='err', tag_style='red')
        segs = self.r.fs_chrome(item, ctx)
        # 3 segments only (selection, indent, expander) — no gutter columns.
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0], ('  ', None, False))   # selection
        self.assertEqual(segs[1], ('', None, False))     # indent (depth 0)
        self.assertEqual(segs[2][0], '  ')               # expander (leaf)
        # The empty-gutter path must not measure columns.
        self.assertEqual(ctx.calls, [])

    def test_explicit_none_col_perms_also_empty_gutter(self):
        # Defensive: col_perms present but None still emits the empty gutter.
        ctx = _FakeCtx(self._widths())
        item = _make_item(self.r, id='/d/x', title='x', col_perms=None)
        segs = self.r.fs_chrome(item, ctx)
        self.assertEqual(len(segs), 3)
        self.assertEqual(ctx.calls, [])

    def test_selected_marker_and_expander_glyph(self):
        # A selected, expanded parent row shows '* ' and '▼ ' around the
        # gutter — the gutter is invariant; the chrome atoms reflect state.
        ctx = _FakeCtx(self._widths(), selected=True, expanded=True)
        item = _make_item(
            self.r, id='/d/sub', title='sub/', has_children=True,
            col_perms='drwxr-xr-x', col_size='', col_mtime='Jun 03 11:08')
        segs = self.r.fs_chrome(item, ctx)
        self.assertEqual(segs[0], ('* ', None, False))   # selected
        self.assertEqual(segs[-1][0], '▼ ')              # expanded parent


class TestGetChildren(unittest.TestCase):
    """``get_children`` stores column strings and drops size-in-tag."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_stores_columns_and_no_size_tag(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, 'subdir'))
            fpath = os.path.join(d, 'file.txt')
            with open(fpath, 'wb') as f:
                f.write(b'hello world')  # 11 bytes -> human_size '11B'

            items = self.r.get_children(d)
            by_name = {os.path.basename(it.id): it for it in items}
            self.assertEqual(set(by_name), {'subdir', 'file.txt'})

            file_it = by_name['file.txt']
            dir_it = by_name['subdir']

            # Column display strings are stored on every (non-error) Item.
            self.assertTrue(file_it.col_perms.startswith('-'))   # regular file
            self.assertTrue(dir_it.col_perms.startswith('d'))    # directory
            self.assertEqual(file_it.col_perms, stat.filemode(os.stat(fpath).st_mode))

            # Size is a column now: the file carries human_size, the dir ''.
            self.assertEqual(file_it.col_size, self.r.human_size(11))
            self.assertEqual(file_it.col_size, '11B')
            self.assertEqual(dir_it.col_size, '')

            # mtime column is a non-empty strftime string on both.
            self.assertTrue(file_it.col_mtime)
            self.assertTrue(dir_it.col_mtime)

            # Size no longer lives in the tag chip: neither row sets a tag,
            # and in particular the file's tag is not its human size.
            for it in (file_it, dir_it):
                self.assertEqual(getattr(it, 'tag', ''), '')
                self.assertNotEqual(getattr(it, 'tag', ''), it.col_size or 'NOPE')

    def test_error_row_has_no_columns(self):
        # A path that can't be scanned yields the single error Item, which
        # must NOT carry col_* so fs_chrome emits an empty gutter for it.
        missing = os.path.join(tempfile.gettempdir(),
                               'definitely-missing-dir-xyz-661')
        # Ensure it truly doesn't exist.
        self.assertFalse(os.path.exists(missing))
        items = self.r.get_children(missing)
        self.assertEqual(len(items), 1)
        err = items[0]
        # The error id is a tagged tuple carrying the failing dir path,
        # not a magic-prefixed string (so a file named '__err__:foo'
        # can't collide with it).
        self.assertEqual(err.id, ('err', missing))
        self.assertEqual(err.tag, 'err')
        self.assertIsNone(getattr(err, 'col_perms', None))
        self.assertIsNone(getattr(err, 'col_size', None))
        self.assertIsNone(getattr(err, 'col_mtime', None))
        # And fs_chrome emits an EMPTY gutter for it (just the three chrome
        # atoms — no metadata columns), so the default content renders it.
        self.assertEqual(len(self.r.fs_chrome(err, _FakeCtx({}))), 3)
        # get_preview routes the error id via its tuple tag (not os.scandir
        # / open) and surfaces the failing path from id[1].
        self.assertEqual(self.r.get_preview(err.id), missing)


class TestGetPreviewDir(unittest.TestCase):
    """``get_preview`` on a directory matches the children view ordering.

    Directories first, then files; each sorted case-insensitively by
    name; directory lines get a trailing ``/`` and files do not. This is
    the same ``(not _is_dir, name.lower())`` ordering and ``/`` suffix
    that ``get_children`` uses.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_dirs_first_with_slash_suffix_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            # Mixed case across dirs and files to prove case-insensitive
            # sort and dirs-before-files regardless of raw byte order.
            os.mkdir(os.path.join(d, 'Bravo'))
            os.mkdir(os.path.join(d, 'alpha'))
            for name in ('Zeta.txt', 'apple.txt'):
                with open(os.path.join(d, name), 'wb') as f:
                    f.write(b'x')

            lines = self.r.get_preview(d).split('\n')

            # Dirs first (case-insensitive: alpha < Bravo), each with '/'.
            # Then files (apple < Zeta), no suffix.
            self.assertEqual(lines, ['alpha/', 'Bravo/', 'apple.txt', 'Zeta.txt'])


class _ActionCtx:
    """A minimal ``ctx`` for the e/o/d actions.

    Records ``run_external`` argv lists and ``error`` messages; ``confirm``
    is auto-answered (default ``True`` — the recipe uses the ``(label, value)``
    mapping and proceeds on a truthy return) and remembered so a test can
    assert it was never reached for a no-op. ``cursor`` is ``targets[0]``.
    """

    def __init__(self, targets, *, confirm=True):
        self.targets = targets
        self.cursor = targets[0] if targets else None
        self._confirm_answer = confirm
        self.external = []
        self.errors = []
        self.confirmed = False
        self.refreshed = False

    def run_external(self, argv):
        self.external.append(argv)

    def confirm(self, _msg, _buttons=None):
        self.confirmed = True
        return self._confirm_answer

    def error(self, msg):
        self.errors.append(msg)

    def refresh(self):
        self.refreshed = True


class TestActionsOnErrorRow(unittest.TestCase):
    """The e/o/d actions are a safe no-op on the synthetic error row.

    Its id is the tuple ``('err', path)``, not a filesystem path, so
    feeding it to argv / ``os.path.isdir`` / ``os.remove`` would raise
    ``TypeError`` (which ``delete``'s ``except OSError`` would NOT catch).
    Each action must skip non-``str`` ids instead of crashing.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _err_item(self):
        return _make_item(self.r, id=('err', '/nope'), title='[error] boom',
                          tag='err', tag_style='red')

    def test_edit_error_row_is_noop(self):
        ctx = _ActionCtx([self._err_item()])
        self.r.edit(ctx)                      # must not raise
        self.assertEqual(ctx.external, [])    # no editor launched

    def test_open_error_row_is_noop(self):
        ctx = _ActionCtx([self._err_item()])
        self.r.open_(ctx)                     # must not raise
        self.assertEqual(ctx.external, [])    # nothing handed to xdg-open

    def test_delete_error_row_is_noop(self):
        ctx = _ActionCtx([self._err_item()])
        self.r.delete(ctx)                    # must not raise (was TypeError)
        # Nothing deleted, nothing refreshed, and confirm was never even
        # reached (no real targets to act on).
        self.assertFalse(ctx.confirmed)
        self.assertFalse(ctx.refreshed)
        self.assertEqual(ctx.errors, [])

    def test_delete_skips_error_row_but_acts_on_real_targets(self):
        # A mixed selection (one real file + the error row) deletes only
        # the real file; the tuple id is filtered out without crashing.
        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, 'real.txt')
            with open(fpath, 'wb') as f:
                f.write(b'x')
            real = _make_item(self.r, id=fpath, title='real.txt')
            ctx = _ActionCtx([real, self._err_item()])
            self.r.delete(ctx)
            self.assertTrue(ctx.confirmed)        # a real target was present
            self.assertFalse(os.path.exists(fpath))  # only the file went
            self.assertTrue(ctx.refreshed)
            self.assertEqual(ctx.errors, [])

    def test_edit_real_cursor_still_launches_editor(self):
        # The str guard must not block a normal path id.
        real = _make_item(self.r, id='/tmp/x.txt', title='x.txt')
        ctx = _ActionCtx([real])
        self.r.edit(ctx)
        self.assertEqual(len(ctx.external), 1)
        self.assertEqual(ctx.external[0][-1], '/tmp/x.txt')


class _RaiseOnRead:
    """A stdin stand-in whose ``read()`` raises — proves stdin is NOT
    consumed in the non-stdin invocation modes (bare / PATH)."""

    def read(self):  # pragma: no cover - only hit on a regression
        raise AssertionError('sys.stdin.read() called outside stdin mode')


class TestStdinRoots(unittest.TestCase):
    """``browse-fs -`` displays the stdin path list as the root level.

    ``main()`` reads the newline-separated paths from ``sys.stdin`` once,
    before the UI starts, into ``_STDIN_ROOTS`` and runs with
    ``root_id=None`` so ``get_children(None)`` serves that list. These
    tests drive ``main()`` with a stubbed stdin and the no-op
    ``browse_tui`` stub (the run loop is never reached — ``b.watch`` /
    ``b.run`` raise ``AttributeError`` past which all the state we assert
    on has already landed), then inspect the module globals / the
    constructed Browser config, plus ``get_children`` / ``get_preview``
    directly — the same loader + stub pattern as the rest of the file.
    """

    def setUp(self):
        self.r = _load_recipe()

    def _run_main(self, stdin, argv=('browse-fs', '-')):
        """Drive ``main()`` with ``stdin`` piped in; return the Browser.

        ``stdin`` is either the raw text to slurp (wrapped so ``.read()``
        yields it, matching the recipe's ``sys.stdin.read()``) OR a
        ready-made stand-in that already has ``.read`` (e.g.
        ``_RaiseOnRead`` for the modes that must NOT touch stdin).
        ``SystemExit`` (the ``-``-plus-PATH usage error) and
        ``AttributeError`` (the stub Browser lacking ``watch`` / ``run``)
        are swallowed; the return value is ``self.r._BROWSER`` (set just
        before ``b.watch`` / ``b.run``), or ``None`` if main exited
        earlier.
        """
        fake = stdin if hasattr(stdin, 'read') else io.StringIO(stdin)
        saved_stdin = self.r.sys.stdin
        self.r.sys.argv[:] = list(argv)
        try:
            self.r.sys.stdin = fake
            try:
                self.r.main()
            except (SystemExit, AttributeError):
                pass
        finally:
            self.r.sys.stdin = saved_stdin
        return self.r._BROWSER

    def _config(self, b):
        # The recipe calls ``Browser(BrowserConfig(...))``; the stub
        # Browser stashes that config as its sole positional arg.
        return b._args[0]

    # -- piped path list becomes the root level -----------------------

    def test_roots_are_the_piped_paths_in_order(self):
        # A mixed list: a file, a dir, a missing path, a path with spaces,
        # and a duplicate of the first file. Roots come out in stdin order,
        # the blank line is skipped, and the duplicate is dropped (first
        # occurrence kept).
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, 'adir'))
            with open(os.path.join(d, 'plain.txt'), 'w') as f:
                f.write('body')
            with open(os.path.join(d, 'a file.txt'), 'w') as f:
                f.write('spaced')
            cwd = os.getcwd()
            os.chdir(d)
            try:
                b = self._run_main(
                    'plain.txt\n'
                    '\n'                       # blank line: skipped
                    'adir\n'
                    'missing-xyz\n'
                    'a file.txt\n'             # path with spaces: verbatim
                    'plain.txt\n')             # duplicate: dropped
                # root_id is None (multi-root mode), not a single dir.
                self.assertIsNone(self._config(b).root_id)
                roots = self.r.get_children(None)
            finally:
                os.chdir(cwd)

        # Titles are the verbatim stdin lines (the missing row carries
        # its marker in the tag chip, not the title), in order, duplicate
        # gone, blank skipped.
        titles = [it.title for it in roots]
        self.assertEqual(titles,
                         ['plain.txt', 'adir', 'missing-xyz', 'a file.txt'])
        # The missing row is tagged (dim) while the real rows are not.
        by_title = {it.title: it for it in roots}
        self.assertEqual(by_title['missing-xyz'].tag, 'missing')
        self.assertEqual(by_title['missing-xyz'].tag_style, 'dim')

    def test_dir_root_expands_to_real_children(self):
        # A directory among the roots carries its real abspath id, so
        # expanding it re-enters get_children and lists its contents.
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, 'adir'))
            with open(os.path.join(d, 'adir', 'child.txt'), 'w') as f:
                f.write('x')
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self._run_main('adir\n')
                roots = self.r.get_children(None)
                adir = next(it for it in roots if it.title == 'adir')
                self.assertTrue(adir.has_children)
                self.assertEqual(adir.id, os.path.join(d, 'adir'))
                kids = self.r.get_children(adir.id)
            finally:
                os.chdir(cwd)
        self.assertEqual([it.title for it in kids], ['child.txt'])

    def test_file_root_previews_and_has_no_children(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'plain.txt'), 'w') as f:
                f.write('hello body')
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self._run_main('plain.txt\n')
                roots = self.r.get_children(None)
                f_it = next(it for it in roots if it.title == 'plain.txt')
                self.assertFalse(f_it.has_children)
                # The file id is its abspath; preview is the file head.
                self.assertEqual(f_it.id, os.path.join(d, 'plain.txt'))
                self.assertEqual(self.r.get_preview(f_it.id), 'hello body')
            finally:
                os.chdir(cwd)

    def test_missing_path_is_a_dim_row_with_sensible_preview(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self._run_main('ghost.txt\n')
                roots = self.r.get_children(None)
            finally:
                os.chdir(cwd)
        self.assertEqual(len(roots), 1)
        miss = roots[0]
        # A tagged-tuple id (not a str path) so the e/o/d actions skip it
        # exactly as they skip the synthetic error row; dim tag style.
        self.assertEqual(miss.id[0], 'missing')
        self.assertEqual(miss.id[1], 'ghost.txt')
        self.assertFalse(isinstance(miss.id, str))
        self.assertEqual(miss.tag, 'missing')
        self.assertEqual(miss.tag_style, 'dim')
        self.assertEqual(miss.title, 'ghost.txt')      # verbatim label
        # No metadata columns ⇒ fs_row_content falls back (and the row
        # renders without crashing on absent col_*).
        self.assertIsNone(getattr(miss, 'col_perms', None))
        # Preview shows the label and the stat error, not a crash.
        preview = self.r.get_preview(miss.id)
        self.assertIn('ghost.txt', preview)
        self.assertIn('[missing]', preview)

    def test_broken_symlink_is_existing_not_missing(self):
        # A broken symlink lstats fine, so it is a normal (existing) row
        # with metadata columns — NOT a missing row, and not a crash.
        with tempfile.TemporaryDirectory() as d:
            link = os.path.join(d, 'broken')
            os.symlink(os.path.join(d, 'no-such-target'), link)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self._run_main('broken\n')
                roots = self.r.get_children(None)
            finally:
                os.chdir(cwd)
        self.assertEqual(len(roots), 1)
        it = roots[0]
        self.assertIsInstance(it.id, str)              # a real path id
        self.assertEqual(it.title, 'broken')           # no (missing) marker
        self.assertTrue(it.col_perms.startswith('l'))  # symlink perms

    def test_relative_paths_resolve_against_cwd(self):
        # Labels stay verbatim; the abspath id is cwd-resolved so the
        # right file is read even though only a bare name was piped.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'rel.txt'), 'w') as f:
                f.write('relbody')
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self._run_main('rel.txt\n')
                roots = self.r.get_children(None)
                it = roots[0]
                self.assertEqual(it.title, 'rel.txt')        # verbatim label
                self.assertEqual(it.id, os.path.join(d, 'rel.txt'))
                self.assertEqual(self.r.get_preview(it.id), 'relbody')
            finally:
                os.chdir(cwd)

    def test_empty_stdin_is_an_empty_root_list(self):
        b = self._run_main('')
        self.assertIsNone(self._config(b).root_id)
        self.assertEqual(self.r._STDIN_ROOTS, [])
        # No crash, no items.
        self.assertEqual(self.r.get_children(None), [])

    def test_trailing_newline_optional(self):
        # The last line need not be newline-terminated.
        with tempfile.TemporaryDirectory() as d:
            for name in ('one.txt', 'two.txt'):
                with open(os.path.join(d, name), 'w') as f:
                    f.write('x')
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self._run_main('one.txt\ntwo.txt')   # no trailing \n
                roots = self.r.get_children(None)
            finally:
                os.chdir(cwd)
        self.assertEqual([it.title for it in roots], ['one.txt', 'two.txt'])

    # -- '-' + PATH is rejected ---------------------------------------

    def test_dash_plus_path_is_a_usage_error(self):
        # ``-`` cannot be combined with a path argument: main() exits via
        # SystemExit with a usage message, and stdin is never read.
        with self.assertRaises(SystemExit) as cm:
            saved = self.r.sys.stdin
            self.r.sys.stdin = _RaiseOnRead()
            self.r.sys.argv[:] = ['browse-fs', '-', 'extra']
            try:
                self.r.main()
            finally:
                self.r.sys.stdin = saved
        # A non-zero / message exit (SystemExit with a str message → the
        # framework prints it and exits 1).
        self.assertIn('cannot be combined', str(cm.exception.code))
        self.assertIsNone(self.r._STDIN_ROOTS)

    # -- auto-detect: a piped (non-tty) stdin synthesizes ``-`` --------

    def test_piped_no_positional_engages_stdin_without_dash(self):
        # ``cmd | browse-fs`` (no explicit ``-``): a non-tty fd 0 makes
        # main() synthesize ``-`` and read the path list, exactly as if
        # ``-`` had been typed.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'real.txt'), 'w') as f:
                f.write('x')
            cwd = os.getcwd()
            os.chdir(d)
            try:
                with _piped_stdin():
                    b = self._run_main('real.txt\n', argv=('browse-fs',))
                self.assertIsNone(self._config(b).root_id)
                roots = self.r.get_children(None)
            finally:
                os.chdir(cwd)
        self.assertEqual([it.title for it in roots], ['real.txt'])

    def test_piped_with_positional_is_a_usage_error(self):
        # ``cmd | browse-fs PATH``: the synthesized ``-`` collides with the
        # path positional, so the existing ``- + PATH`` usage error fires
        # (and stdin is never read).
        with self.assertRaises(SystemExit) as cm:
            saved = self.r.sys.stdin
            self.r.sys.stdin = _RaiseOnRead()
            self.r.sys.argv[:] = ['browse-fs', 'somepath']
            try:
                with _piped_stdin():
                    self.r.main()
            finally:
                self.r.sys.stdin = saved
        self.assertIn('cannot be combined', str(cm.exception.code))
        self.assertIsNone(self.r._STDIN_ROOTS)

    def test_piped_empty_is_an_empty_root_list(self):
        # A non-tty empty stdin synthesizes ``-`` and flows into the
        # existing empty handling: an empty (clean "no items") root list,
        # no crash. No emptiness special-casing.
        with _piped_stdin():
            b = self._run_main('', argv=('browse-fs',))
        self.assertIsNone(self._config(b).root_id)
        self.assertEqual(self.r._STDIN_ROOTS, [])
        self.assertEqual(self.r.get_children(None), [])

    def test_piped_help_flag_is_exempt_from_auto_detect(self):
        # ``cmd | browse-fs --help``: the synthesized ``-`` must NOT be
        # injected (it would trip the ``- + PATH`` error before the
        # framework's -h/--help auto-detect). stdin is left untouched and
        # no usage error fires for the dash. (End-to-end help output is
        # covered in test/ui/test_help_text.py.)
        saved = self.r.sys.stdin
        self.r.sys.stdin = _RaiseOnRead()
        self.r.sys.argv[:] = ['browse-fs', '--help']
        try:
            with _piped_stdin():
                # Stub Browser has no help-detecting ``run`` — it raises
                # AttributeError past construction; the point is that we
                # reach bare mode WITHOUT injecting ``-`` / reading stdin.
                with self.assertRaises(AttributeError):
                    self.r.main()
        finally:
            self.r.sys.stdin = saved
        self.assertIsNone(self.r._STDIN_ROOTS)

    # -- bare / PATH modes never touch stdin --------------------------

    def test_bare_invocation_browses_cwd_and_ignores_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                # _RaiseOnRead would fire if main() read stdin here.
                b = self._run_main(_RaiseOnRead(), argv=('browse-fs',))
                self.assertEqual(self._config(b).root_id, os.getcwd())
            finally:
                os.chdir(cwd)
        self.assertIsNone(self.r._STDIN_ROOTS)

    def test_path_mode_uses_abspath_and_ignores_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            b = self._run_main(_RaiseOnRead(), argv=('browse-fs', d))
            self.assertEqual(self._config(b).root_id, os.path.abspath(d))
        self.assertIsNone(self.r._STDIN_ROOTS)

    # -- --tty - is the framework UI-over-streams flag, not stdin -------

    def test_tty_dash_value_is_not_the_stdin_positional(self):
        # ``--tty -`` is the framework's UI-over-std-streams flag value
        # (auto-detected by Browser.run(), left in sys.argv): the recipe
        # must drop it, fall through to bare mode (browse cwd), and never
        # read stdin. Same for the one-token ``--tty=-`` spelling and the
        # device-path form ``--tty /dev/pts/N`` (the path is not a root).
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                for argv in (('browse-fs', '--tty', '-'),
                             ('browse-fs', '--tty=-'),
                             ('browse-fs', '--tty', '/dev/pts/9')):
                    self.r = _load_recipe()
                    # _RaiseOnRead fires if main() touches stdin here.
                    b = self._run_main(_RaiseOnRead(), argv=argv)
                    self.assertEqual(self._config(b).root_id, os.getcwd(),
                                     argv)
                    self.assertIsNone(self.r._STDIN_ROOTS, argv)
            finally:
                os.chdir(cwd)
        # Contrast: a true positional ``-`` still enters stdin mode and
        # reads the piped list (proving the strip didn't disarm ``-``).
        self.r = _load_recipe()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'real.txt'), 'w') as f:
                f.write('x')
            cwd = os.getcwd()
            os.chdir(d)
            try:
                b = self._run_main('real.txt\n')
                self.assertIsNone(self._config(b).root_id)
                self.assertEqual([it.title for it in self.r.get_children(None)],
                                 ['real.txt'])
            finally:
                os.chdir(cwd)

    def test_tty_dash_combined_with_path_still_a_usage_error(self):
        # Stripping ``--tty -`` must not weaken the ``- + PATH`` guard:
        # ``browse-fs --tty - somepath -`` is still a genuine ``-`` plus a
        # path positional, so it must exit via the usage error with stdin
        # untouched.
        with self.assertRaises(SystemExit) as cm:
            saved = self.r.sys.stdin
            self.r.sys.stdin = _RaiseOnRead()
            self.r.sys.argv[:] = ['browse-fs', '--tty', '-', 'somepath', '-']
            try:
                self.r.main()
            finally:
                self.r.sys.stdin = saved
        self.assertIn('cannot be combined', str(cm.exception.code))
        self.assertIsNone(self.r._STDIN_ROOTS)


class _EnterCtx:
    """A ``ctx`` stand-in for ``_on_enter`` / launch tests.

    ``cursor`` is the row Enter fires on; ``run_external`` records the argv
    and the ``keep_screen`` flag each call receives so a test can assert
    both what was launched and how.
    """

    def __init__(self, cursor):
        self.cursor = cursor
        self.calls = []
        self.keep_screen = None

    def run_external(self, cmd, env=None, *, keep_screen=False):
        self.calls.append(cmd)
        self.keep_screen = keep_screen
        return 0


class TestMdLauncher(unittest.TestCase):
    """browse-fs markdown launcher rows: a .md row opens in browse-md (#968)."""

    def setUp(self):
        self.r = _load_recipe()
        self.r._MD_COLOR = False  # raw previews keep assertions ANSI-free
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.d, ignore_errors=True))

    def _w(self, name, text=''):
        p = os.path.join(self.d, name)
        with open(p, 'w') as f:
            f.write(text)
        return p

    # ---- has_children gate ------------------------------------------------

    def test_md_file_row_gets_arrow_others_do_not(self):
        self._w('a.md', '# A\n')
        self._w('x.txt', 'hi\n')
        os.mkdir(os.path.join(self.d, 'sub'))
        rows = {r.title: r for r in self.r.get_children(self.d)}
        self.assertTrue(rows['a.md'].has_children)      # .md → launcher arrow
        self.assertFalse(rows['x.txt'].has_children)    # plain file → leaf
        self.assertTrue(rows['sub/'].has_children)      # dir → unchanged

    def test_md_capital_extension_also_arrows(self):
        self._w('READ.MD', '# x\n')
        rows = {r.title: r for r in self.r.get_children(self.d)}
        self.assertTrue(rows['READ.MD'].has_children)

    def test_stdin_root_md_gets_arrow(self):
        p = self._w('note.md', '# n\n')
        item = self.r._stdin_root_item('note.md', p)
        self.assertTrue(item.has_children)

    def test_inert_when_md_doc_absent(self):
        self._w('a.md', '# A\nlinks [b](b.md)\n')
        self._w('b.md', '# B\n')
        self.r._md_doc = None
        rows = {r.title: r for r in self.r.get_children(self.d)}
        self.assertFalse(rows['a.md'].has_children)     # no arrow
        # And the path no longer intercepts: a .md "expanded" falls through
        # to scandir, which errors on a non-dir (a plain leaf in practice).
        self.assertFalse(self.r._md_launchable(os.path.join(self.d, 'a.md')))

    # ---- launcher children ------------------------------------------------

    def test_self_open_row_first_then_links(self):
        a = self._w('a.md', '# A\nSee [b](b.md).\n')
        b = self._w('b.md', '# B\n')
        rows = self.r._md_launcher_children(a)
        self.assertEqual(len(rows), 2)
        # Self-open row first, target == the file itself.
        self.assertEqual(rows[0].id, ('launch', a, 'md-file', a))
        self.assertEqual(rows[1].id[3], os.path.realpath(b))
        # All launcher rows: leaf, [md ↗] tag, bare relative-label title.
        for row in rows:
            self.assertFalse(row.has_children)
            self.assertEqual(row.tag, 'md ↗')
            self.assertEqual(row.tag_style, 'yellow')
        self.assertEqual(rows[0].title, 'a.md')   # self, labelled as the file
        self.assertEqual(rows[1].title, 'b.md')

    def test_links_deduped_and_sorted_by_label(self):
        # Reference z then a (and b twice) — links come out sorted, deduped,
        # after the self row.
        a = self._w('a.md', 'see z.md and b.md then b.md again and a.md\n')
        self._w('z.md', '')
        self._w('b.md', '')
        rows = self.r._md_launcher_children(a)
        labels = [r.title for r in rows]
        self.assertEqual(labels, ['a.md', 'b.md', 'z.md'])  # self, then sorted

    def test_nonexistent_and_non_md_refs_dropped(self):
        a = self._w('a.md', 'missing nope.md, code config.txt, real real.md\n')
        self._w('real.md', '')
        rows = self.r._md_launcher_children(a)
        targets = [os.path.basename(r.id[3]) for r in rows]
        self.assertEqual(targets, ['a.md', 'real.md'])  # self + the one existing .md

    def test_self_reference_not_duplicated(self):
        a = self._w('a.md', 'I link to myself: a.md\n')
        rows = self.r._md_launcher_children(a)
        self.assertEqual(len(rows), 1)                  # only the self-open row
        self.assertEqual(rows[0].id[3], a)

    def test_no_links_yields_lone_self_row(self):
        a = self._w('a.md', '# just headings\n## no refs\n')
        rows = self.r._md_launcher_children(a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, ('launch', a, 'md-file', a))

    def test_unreadable_file_still_yields_self_row(self):
        a = os.path.join(self.d, 'gone.md')  # never created
        rows = self.r._md_launcher_children(a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id[3], a)

    def test_get_children_intercepts_md_path(self):
        a = self._w('a.md', 'see b.md\n')
        self._w('b.md', '')
        via_children = [r.id for r in self.r.get_children(a)]
        via_builder = [r.id for r in self.r._md_launcher_children(a)]
        self.assertEqual(via_children, via_builder)

    # ---- preview ----------------------------------------------------------

    def test_preview_of_launch_id_shows_target(self):
        a = self._w('a.md', '# A\n')
        b = self._w('b.md', 'BODY-OF-B\n')
        rows = self.r._md_launcher_children(self._w('a.md', 'see b.md\n'))
        link = next(r for r in rows if r.id[3] == os.path.realpath(b))
        self.assertIn('BODY-OF-B', self.r.get_preview(link.id))

    def test_capital_md_preview_colored_like_lowercase(self):
        # The preview color gate uses _MD_EXTS, so a .MD file colors like .md
        # (it now gets launcher rows / previews, so the two must agree).
        if self.r._md2ansi_fn is None:
            self.skipTest('md2ansi_lib not available')
        self.r._MD_COLOR = True
        p = self._w('R.MD', '# Heading\n')
        self.assertIn('\x1b[', self.r.get_preview(p))   # md2ansi fired for .MD

    # ---- Enter dispatch / launch -----------------------------------------

    def test_enter_on_launcher_launches_browse_md(self):
        a = self._w('a.md', '# A\n')
        row = self.r._md_launcher_children(a)[0]   # the self-open row
        ctx = _EnterCtx(row)
        self.r._on_enter(ctx)
        self.assertEqual(len(ctx.calls), 1)
        cmd = ctx.calls[0]
        self.assertEqual(cmd[0], 'browse-md')
        self.assertIn(a, cmd)                  # the target file
        self.assertIn('--root', cmd)
        # --root value is the project root (here: the file's own dir).
        self.assertEqual(cmd[cmd.index('--root') + 1], self.r._project_root_for(a))
        # Switch-free handoff: child renders without its own alt-screen switch
        # (--no-alt-screen) and the parent keeps the alt screen (keep_screen).
        self.assertIn('--no-alt-screen', cmd)
        # Alt-Up at the top of the launched browse-md returns here.
        self.assertIn('--quit-on-scope-up', cmd)
        self.assertTrue(ctx.keep_screen)

    def test_enter_on_regular_file_row_edits(self):
        a = self._w('a.md', '# A\n')
        ctx = _EnterCtx(self.r.Item(id=a))   # a plain str-id row (not a launcher)
        with mock.patch.dict(os.environ, {'EDITOR': 'ed'}):
            self.r._on_enter(ctx)
        self.assertEqual(ctx.calls, [['ed', a]])

    def test_enter_with_no_cursor_is_noop(self):
        ctx = _EnterCtx(None)
        self.r._on_enter(ctx)                # must not raise
        self.assertEqual(ctx.calls, [])


class _ModeCtx:
    """A minimal ``ctx`` for the display-mode switch actions.

    Records ``flash`` text and ``refresh`` calls — the two side-effects
    ``_set_display_mode`` performs besides flipping the module global.
    """

    def __init__(self):
        self.flashes = []
        self.refreshed = 0

    def flash(self, text, log=False):
        self.flashes.append(text)

    def refresh(self):
        self.refreshed += 1


class TestDisplayModes(unittest.TestCase):
    """Display modes (number keys 1/2/3) gate the gutter column set (#1115).

    ``_DISPLAY_MODE`` selects which columns ``_fs_gutter_segments`` emits:
    mode 1 = empty gutter, mode 2 = perms/size/date, mode 3 = + user/group.
    The strings are always computed in ``_set_columns`` (incl. col_user /
    col_group); the mode only chooses which the gutter renders.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        # Each test drives ``_DISPLAY_MODE`` explicitly; restore the default
        # afterwards so tests stay isolated (it is a module global).
        self._saved_mode = self.r._DISPLAY_MODE
        self.addCleanup(setattr, self.r, '_DISPLAY_MODE', self._saved_mode)

    def _widths(self, perms=10, size=4, mtime=12, user=5, group=5):
        return {'col_perms': perms, 'col_size': size, 'col_mtime': mtime,
                'col_user': user, 'col_group': group}

    def _item(self):
        return _make_item(
            self.r, id='/d/f', title='f.txt',
            col_perms='-rw-r--r--', col_size='12K', col_mtime='Jun 03 11:08',
            col_user='alice', col_group='staff')

    # -- the per-mode gutter column set ------------------------------------

    def test_mode_1_is_empty_gutter(self):
        # Mode 1 = name only → no gutter columns and no width measurement.
        self.r._DISPLAY_MODE = 1
        ctx = _FakeCtx(self._widths())
        segs = self.r._fs_gutter_segments(self._item(), ctx)
        self.assertEqual(segs, [])
        self.assertEqual(ctx.calls, [])
        # And the whole chrome is just selection + indent + expander.
        self.assertEqual(len(self.r.fs_chrome(self._item(), _FakeCtx(self._widths()))), 3)

    def test_mode_2_is_perms_size_date(self):
        # Mode 2 = the default set: perms · size · date, in that order.
        self.r._DISPLAY_MODE = 2
        ctx = _FakeCtx(self._widths(perms=10, size=4, mtime=12))
        segs = self.r._fs_gutter_segments(self._item(), ctx)
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0], ('-rw-r--r--' + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(segs[1], (' 12K' + '  ', _DIM[0], _DIM[1]))    # rjust 4
        self.assertEqual(segs[2], ('Jun 03 11:08' + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(ctx.calls, ['col_perms', 'col_size', 'col_mtime'])

    def test_mode_3_appends_user_then_group(self):
        # Mode 3 = perms · size · date · user · group; user/group are
        # left-justified, dim, sized via max_col_width_global.
        self.r._DISPLAY_MODE = 3
        ctx = _FakeCtx(self._widths(perms=10, size=4, mtime=12, user=6, group=7))
        segs = self.r._fs_gutter_segments(self._item(), ctx)
        self.assertEqual(len(segs), 5)
        # First three unchanged from mode 2.
        self.assertEqual(segs[0], ('-rw-r--r--' + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(segs[1], (' 12K' + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(segs[2], ('Jun 03 11:08' + '  ', _DIM[0], _DIM[1]))
        # user (ljust 6) then group (ljust 7), both dim.
        self.assertEqual(segs[3], ('alice'.ljust(6) + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(segs[4], ('staff'.ljust(7) + '  ', _DIM[0], _DIM[1]))
        # Measured in column order, user before group.
        self.assertEqual(ctx.calls,
                         ['col_perms', 'col_size', 'col_mtime',
                          'col_user', 'col_group'])

    # -- user / group resolution + numeric fallback ------------------------

    def test_set_columns_resolves_user_and_group(self):
        # _set_columns stores resolved owner / group names from st_uid/st_gid.
        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, 'f.txt')
            with open(fpath, 'wb') as f:
                f.write(b'x')
            st = os.stat(fpath)
            item = _make_item(self.r, id=fpath, title='f.txt')
            self.r._set_columns(item, st, is_dir=False)
            import pwd as _pwd
            import grp as _grp
            self.assertEqual(item.col_user, _pwd.getpwuid(st.st_uid).pw_name)
            self.assertEqual(item.col_group, _grp.getgrgid(st.st_gid).gr_name)

    def test_unresolvable_uid_gid_fall_back_to_numeric(self):
        # A uid/gid with no passwd/group entry (getpwuid/getgrgid raise
        # KeyError) falls back to the numeric id rendered as a string.
        self.r._UID_NAMES.clear()
        self.r._GID_NAMES.clear()
        with mock.patch('pwd.getpwuid', side_effect=KeyError), \
                mock.patch('grp.getgrgid', side_effect=KeyError):
            self.assertEqual(self.r._uid_name(4242), '4242')
            self.assertEqual(self.r._gid_name(7777), '7777')

    def test_uid_gid_resolution_is_cached(self):
        # Per-uid / per-gid caching: a second lookup does NOT re-hit
        # pwd/grp (so dozens of same-owner rows resolve once each).
        self.r._UID_NAMES.clear()
        self.r._GID_NAMES.clear()
        with mock.patch('pwd.getpwuid',
                        return_value=types.SimpleNamespace(pw_name='bob')) as pw, \
                mock.patch('grp.getgrgid',
                           return_value=types.SimpleNamespace(gr_name='wheel')) as gr:
            self.assertEqual(self.r._uid_name(1000), 'bob')
            self.assertEqual(self.r._uid_name(1000), 'bob')
            self.assertEqual(self.r._gid_name(20), 'wheel')
            self.assertEqual(self.r._gid_name(20), 'wheel')
        self.assertEqual(pw.call_count, 1)
        self.assertEqual(gr.call_count, 1)

    # -- the mode-switch actions -------------------------------------------

    def test_set_display_mode_flips_global_flashes_and_refreshes(self):
        # Each action sets _DISPLAY_MODE, flashes the mode, and refreshes.
        for mode in (1, 3, 2):
            ctx = _ModeCtx()
            self.r._set_display_mode(ctx, mode)
            self.assertEqual(self.r._DISPLAY_MODE, mode)
            self.assertEqual(ctx.refreshed, 1)
            self.assertEqual(len(ctx.flashes), 1)
            # The flash names the active mode's column set.
            self.assertIn(self.r._MODE_LABELS[mode], ctx.flashes[0])

    def test_default_display_mode_is_2(self):
        # Freshly loaded, the recipe defaults to mode 2 (perms/size/date).
        fresh = _load_recipe()
        self.assertEqual(fresh._DISPLAY_MODE, 2)


class _FakeDirEntry:
    """A minimal ``os.DirEntry`` stand-in: name + path, used to drive
    ``_sort_rows`` (which reads only ``.name`` and ``.path``)."""

    def __init__(self, name):
        self.name = name
        self.path = '/d/' + name


def _row(name, *, is_dir=False, size=0, mtime=0.0, uid=0):
    """Build a ``(dirent, stat)`` row for ``_sort_rows``.

    The fake stat carries only the fields the sort keys read — mode (for the
    dirs-first split), size, mtime, and uid (the user key resolves it via
    ``_uid_name``)."""
    mode = (stat.S_IFDIR if is_dir else stat.S_IFREG) | 0o644
    st = types.SimpleNamespace(st_mode=mode, st_size=size, st_mtime=mtime,
                              st_uid=uid)
    return (_FakeDirEntry(name), st)


class TestSortModes(unittest.TestCase):
    """Sort modes (capital keys N/S/T/U + D toggle) order each directory's
    children in ``get_children`` (ticket #1116).

    ``_FS_SORT_KEY`` / ``_FS_SORT_DIR`` / ``_DIRS_FIRST`` drive ``_sort_rows``;
    the action handlers (``_set_sort`` / ``_toggle_dirs_first``) mutate that
    state, flash, and refresh. Tests drive the globals directly and restore
    them via ``addCleanup`` (they are module globals)."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        r = self.r
        self._saved = (r._FS_SORT_KEY, dict(r._FS_SORT_DIR), r._DIRS_FIRST)
        self.addCleanup(self._restore)

    def _restore(self):
        r = self.r
        key, dirmap, dirs_first = self._saved
        r._FS_SORT_KEY = key
        r._FS_SORT_DIR.clear()
        r._FS_SORT_DIR.update(dirmap)
        r._DIRS_FIRST = dirs_first

    def _names(self, rows):
        return [d.name for d, _st in rows]

    def _sorted(self, rows):
        """Return the names after ``_sort_rows`` (which sorts in place)."""
        rows = list(rows)
        self.r._sort_rows(rows)
        return self._names(rows)

    # -- defaults ----------------------------------------------------------

    def test_defaults(self):
        # Fresh: sort by name ascending, directories first.
        fresh = _load_recipe()
        self.assertEqual(fresh._FS_SORT_KEY, 'name')
        self.assertTrue(fresh._FS_SORT_DIR['name'])
        self.assertFalse(fresh._FS_SORT_DIR['size'])
        self.assertFalse(fresh._FS_SORT_DIR['mtime'])
        self.assertTrue(fresh._FS_SORT_DIR['user'])
        self.assertTrue(fresh._DIRS_FIRST)

    # -- each key orders correctly -----------------------------------------

    def _mixed(self):
        # Two dirs + three files with distinct names/sizes/mtimes/uids.
        return [
            _row('Bravo', is_dir=True,  size=0,    mtime=300.0, uid=10),
            _row('alpha', is_dir=True,  size=0,    mtime=100.0, uid=30),
            _row('big.txt',   size=900, mtime=200.0, uid=20),
            _row('mid.txt',   size=500, mtime=400.0, uid=10),
            _row('small.txt', size=100, mtime=150.0, uid=30),
        ]

    def test_name_ascending_default_case_insensitive(self):
        # Default state: dirs first (alpha < Bravo, case-insensitive), then
        # files by name asc.
        self.assertEqual(
            self._sorted(self._mixed()),
            ['alpha', 'Bravo', 'big.txt', 'mid.txt', 'small.txt'])

    def test_size_descending(self):
        # Size key, default direction descending: files big>mid>small. Dirs
        # first (size 0, tied) → name tie-break, which follows the primary
        # direction (descending) so the dirs list Bravo before alpha.
        self.r._FS_SORT_KEY = 'size'
        self.r._FS_SORT_DIR['size'] = False
        self.assertEqual(
            self._sorted(self._mixed()),
            ['Bravo', 'alpha', 'big.txt', 'mid.txt', 'small.txt'])

    def test_mtime_descending(self):
        # mtime desc: files newest-first (mid 400 > big 200 > small 150);
        # dirs first, ordered among themselves by mtime desc (Bravo 300 >
        # alpha 100).
        self.r._FS_SORT_KEY = 'mtime'
        self.r._FS_SORT_DIR['mtime'] = False
        self.assertEqual(
            self._sorted(self._mixed()),
            ['Bravo', 'alpha', 'mid.txt', 'big.txt', 'small.txt'])

    def test_user_ascending_by_resolved_name(self):
        # User key sorts by the RESOLVED owner name (not the raw uid). Map
        # the uids to names whose alpha order differs from the numeric order
        # to prove the resolution path is used.
        self.r._FS_SORT_KEY = 'user'
        self.r._FS_SORT_DIR['user'] = True
        names = {10: 'carol', 20: 'alice', 30: 'bob'}
        with mock.patch.object(self.r, '_uid_name', side_effect=lambda u: names[u]):
            ordered = self._sorted(self._mixed())
        # Dirs first by user asc: alpha(uid30→bob) vs Bravo(uid10→carol) →
        # bob < carol → alpha, Bravo. Files by user asc:
        # big(alice) < small(bob) < mid(carol).
        self.assertEqual(
            ordered, ['alpha', 'Bravo', 'big.txt', 'small.txt', 'mid.txt'])

    # -- reverse-on-repeat / remembered direction --------------------------

    def test_name_reverses_within_groups_keeping_dirs_first(self):
        # Name descending: dirs stay first but reversed among themselves
        # (Bravo > alpha), files reversed too.
        self.r._FS_SORT_KEY = 'name'
        self.r._FS_SORT_DIR['name'] = False
        self.assertEqual(
            self._sorted(self._mixed()),
            ['Bravo', 'alpha', 'small.txt', 'mid.txt', 'big.txt'])

    def test_size_ascending_when_reversed(self):
        self.r._FS_SORT_KEY = 'size'
        self.r._FS_SORT_DIR['size'] = True   # ascending
        self.assertEqual(
            self._sorted(self._mixed()),
            ['alpha', 'Bravo', 'small.txt', 'mid.txt', 'big.txt'])

    # -- the D toggle: dirs-first vs in-line -------------------------------

    def test_dirs_inline_sorts_dirs_and_files_together(self):
        # With _DIRS_FIRST off, dirs and files interleave by the active key.
        # Name asc, all together: alpha, big.txt, Bravo, mid.txt, small.txt.
        self.r._DIRS_FIRST = False
        self.r._FS_SORT_KEY = 'name'
        self.r._FS_SORT_DIR['name'] = True
        self.assertEqual(
            self._sorted(self._mixed()),
            ['alpha', 'big.txt', 'Bravo', 'mid.txt', 'small.txt'])

    def test_dirs_inline_by_size_desc(self):
        # In-line + size desc: dirs (size 0) sink below the files; ties among
        # the dirs broken by name, which follows the descending direction
        # (Bravo before alpha). big(900) > mid(500) > small(100) > [dirs].
        self.r._DIRS_FIRST = False
        self.r._FS_SORT_KEY = 'size'
        self.r._FS_SORT_DIR['size'] = False
        self.assertEqual(
            self._sorted(self._mixed()),
            ['big.txt', 'mid.txt', 'small.txt', 'Bravo', 'alpha'])

    # -- tie-break stability -----------------------------------------------

    def test_equal_primary_values_tie_break_by_name(self):
        # Several same-size files sort by name (asc) as the tie-break, even
        # when the primary key is size; reversing the size direction reverses
        # the tie-break too (descending name) so the order is fully defined.
        rows = [_row('c.txt', size=10), _row('a.txt', size=10),
                _row('b.txt', size=10)]
        self.r._FS_SORT_KEY = 'size'
        self.r._FS_SORT_DIR['size'] = True            # asc
        self.assertEqual(self._sorted(rows), ['a.txt', 'b.txt', 'c.txt'])
        self.r._FS_SORT_DIR['size'] = False           # desc → name desc too
        self.assertEqual(self._sorted(rows), ['c.txt', 'b.txt', 'a.txt'])

    # -- the action handlers (state + flash + refresh) ---------------------

    def test_set_sort_switch_uses_remembered_direction(self):
        # Start on name; switching to size adopts size's REMEMBERED direction
        # (not name's) — the key is activated without flipping it.
        self.r._FS_SORT_KEY = 'name'
        self.r._FS_SORT_DIR.update({'name': True, 'size': False})
        ctx = _ModeCtx()
        self.r._set_sort(ctx, 'size')
        self.assertEqual(self.r._FS_SORT_KEY, 'size')
        self.assertFalse(self.r._FS_SORT_DIR['size'])   # unchanged (remembered)
        self.assertTrue(self.r._FS_SORT_DIR['name'])    # untouched
        self.assertEqual(ctx.refreshed, 1)
        self.assertEqual(len(ctx.flashes), 1)
        self.assertIn('size', ctx.flashes[0])

    def test_set_sort_active_key_reverses(self):
        # Pressing the ACTIVE key flips just that key's direction.
        self.r._FS_SORT_KEY = 'size'
        self.r._FS_SORT_DIR['size'] = False
        ctx = _ModeCtx()
        self.r._set_sort(ctx, 'size')
        self.assertEqual(self.r._FS_SORT_KEY, 'size')
        self.assertTrue(self.r._FS_SORT_DIR['size'])    # reversed
        # Press again → flips back.
        self.r._set_sort(_ModeCtx(), 'size')
        self.assertFalse(self.r._FS_SORT_DIR['size'])

    def test_switch_back_restores_each_keys_own_direction(self):
        # name asc, size reversed to asc (by pressing it twice), then back to
        # name → name still asc; size remembered as asc.
        self.r._FS_SORT_KEY = 'name'
        self.r._FS_SORT_DIR.update({'name': True, 'size': False})
        self.r._set_sort(_ModeCtx(), 'size')   # active→size, dir flipped? no, switch
        # switching to size does NOT flip (remembered desc)
        self.assertFalse(self.r._FS_SORT_DIR['size'])
        self.r._set_sort(_ModeCtx(), 'size')   # now active → reverse to asc
        self.assertTrue(self.r._FS_SORT_DIR['size'])
        self.r._set_sort(_ModeCtx(), 'name')   # switch back to name
        self.assertEqual(self.r._FS_SORT_KEY, 'name')
        self.assertTrue(self.r._FS_SORT_DIR['name'])    # name's own dir intact
        self.r._set_sort(_ModeCtx(), 'size')   # switch to size again
        self.assertTrue(self.r._FS_SORT_DIR['size'])    # size remembered asc

    def test_toggle_dirs_first_flips_flashes_and_refreshes(self):
        self.r._DIRS_FIRST = True
        ctx = _ModeCtx()
        self.r._toggle_dirs_first(ctx)
        self.assertFalse(self.r._DIRS_FIRST)
        self.assertEqual(ctx.refreshed, 1)
        self.assertEqual(len(ctx.flashes), 1)
        self.assertIn('off', ctx.flashes[0])
        ctx2 = _ModeCtx()
        self.r._toggle_dirs_first(ctx2)
        self.assertTrue(self.r._DIRS_FIRST)
        self.assertIn('on', ctx2.flashes[0])

    # -- integration: get_children honours the active sort -----------------

    def test_get_children_applies_active_sort(self):
        # Drive get_children on a real fixture dir with distinct sizes, and
        # confirm size-desc reorders the listing (dirs first).
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, 'zdir'))
            for name, body in (('big.txt', b'x' * 100),
                               ('small.txt', b'x'),
                               ('mid.txt', b'x' * 50)):
                with open(os.path.join(d, name), 'wb') as f:
                    f.write(body)
            self.r._FS_SORT_KEY = 'size'
            self.r._FS_SORT_DIR['size'] = False   # descending
            names = [os.path.basename(it.id) for it in self.r.get_children(d)]
        # Dir first, then files by size desc.
        self.assertEqual(names, ['zdir', 'big.txt', 'mid.txt', 'small.txt'])

    def test_stdin_roots_are_never_sorted(self):
        # The stdin-list mode returns the piped paths in INPUT order — the
        # configurable sort must NOT touch them, even when an aggressive sort
        # mode is active. (The mode's contract is verbatim input order.)
        with tempfile.TemporaryDirectory() as d:
            for name in ('zzz.txt', 'aaa.txt', 'mmm.txt'):
                with open(os.path.join(d, name), 'w') as f:
                    f.write('x')
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.r._STDIN_ROOTS = [
                    ('zzz.txt', os.path.join(d, 'zzz.txt')),
                    ('aaa.txt', os.path.join(d, 'aaa.txt')),
                    ('mmm.txt', os.path.join(d, 'mmm.txt')),
                ]
                self.addCleanup(setattr, self.r, '_STDIN_ROOTS', None)
                self.r._FS_SORT_KEY = 'name'
                self.r._FS_SORT_DIR['name'] = True   # name asc — would reorder
                titles = [it.title for it in self.r.get_children(None)]
            finally:
                os.chdir(cwd)
        # Verbatim input order, NOT sorted (which would be aaa, mmm, zzz).
        self.assertEqual(titles, ['zzz.txt', 'aaa.txt', 'mmm.txt'])


if __name__ == '__main__':
    unittest.main()
