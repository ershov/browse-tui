"""Unit tests for the ``recipes/browse-fs`` columnar list (ticket #661).

The recipe is a single-file ``--run-py`` script that imports
``browse_tui`` (only available when the binary loads it). To exercise
``fs_row_content`` and ``get_children`` directly we stub ``browse_tui``
in ``sys.modules`` and load the extension-less recipe via
``SourceFileLoader`` — the same pattern as ``test/unit/test_browse_git.py``.

The point of these tests is to verify the recipe *wires* the column
helpers correctly, not to re-test the helpers themselves (``cell_ljust`` /
``cell_rjust`` are covered by Stage 1's ``test_render``). So the stub
provides functional-enough ``cell_ljust`` / ``cell_rjust`` / ``style`` /
``default_row_content`` and a fake ``ctx`` whose ``max_col_width(field)``
returns a fixed width per field.

Coverage:

* ``fs_row_content``  — pads each metadata column to its
  ``max_col_width`` and emits the title segment LAST; rows of differing
  perms/size/mtime lengths align (equal per-column segment widths); an
  item WITHOUT ``col_perms`` falls back to exactly
  ``default_row_content(item, ctx)``.
* ``get_children``    — stores ``col_perms`` / ``col_size`` / ``col_mtime``
  on each Item and no longer stuffs the size into ``tag``; the error row
  carries no ``col_*`` (so it falls back).
"""

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


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-fs'

# Sentinel the stub ``style('dim')`` returns; ``fs_row_content`` must put
# this exact (fg, bold) pair on every metadata segment.
_DIM = (242, False)


def _stub_browse_tui():
    """Insert a ``browse_tui`` stub the recipe can import from.

    Always installs a fresh module so a stub left behind by another
    recipe's unit test doesn't bleed in. ``Item`` keeps its kwargs as
    attributes so ``get_children`` tests can read ``.col_*`` / ``.tag``;
    ``Browser`` / ``BrowserConfig`` / ``Action`` are inert. The column
    helpers (``cell_ljust`` / ``cell_rjust`` / ``style`` /
    ``default_row_content``) are functional-but-minimal: the test data is
    plain ASCII, so ``str.ljust`` / ``str.rjust`` measure the same as the
    real cell-aware helpers, and that is enough to prove the wiring.
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

    def _default_row_content(item, ctx):
        # A recognisable sentinel so the fallback path is unambiguous.
        return [('DEFAULT', getattr(item, 'id', None), getattr(item, 'title', None))]

    mod.default_row_content = _default_row_content
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
    """A ``RowContext`` stand-in: ``max_col_width(field)`` → fixed width."""

    def __init__(self, widths):
        self._widths = widths
        self.calls = []

    def max_col_width(self, field, parent_id=None):
        self.calls.append(field)
        return self._widths[field]


def _make_item(r, **kw):
    """Build a recipe ``Item`` (the stub) carrying ``kw`` as attributes."""
    return r.Item(**kw)


class TestFsRowContent(unittest.TestCase):
    """``fs_row_content`` assembles padded columns with the title last."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _widths(self, perms=10, size=4, mtime=12):
        return {'col_perms': perms, 'col_size': size, 'col_mtime': mtime}

    def test_columns_padded_and_title_last(self):
        ctx = _FakeCtx(self._widths(perms=10, size=4, mtime=12))
        item = _make_item(
            self.r, id='/d/f', title='f.txt',
            col_perms='-rw-r--r--', col_size='12K', col_mtime='Jun 03 11:08')
        segs = self.r.fs_row_content(item, ctx)

        # Four segments: perms, size, mtime, title.
        self.assertEqual(len(segs), 4)
        perms_seg, size_seg, mtime_seg, title_seg = segs

        # Each metadata segment is the padded column + a two-space gap,
        # carries the dim (fg, bold) from style('dim').
        self.assertEqual(perms_seg, ('-rw-r--r--' + '  ', _DIM[0], _DIM[1]))
        self.assertEqual(size_seg, (' 12K' + '  ', _DIM[0], _DIM[1]))   # rjust to 4
        self.assertEqual(mtime_seg, ('Jun 03 11:08' + '  ', _DIM[0], _DIM[1]))

        # Title comes LAST and is the plain item title (no fg, not bold),
        # so a narrow pane truncates the name not the metadata.
        self.assertEqual(title_seg, ('f.txt', None, False))

        # Widths were sourced from max_col_width for each column field.
        self.assertEqual(ctx.calls, ['col_perms', 'col_size', 'col_mtime'])

    def test_size_is_right_justified(self):
        # A dir's col_size is '' — it still pads to the column width on the
        # left (rjust) so the size column right-aligns under the files.
        ctx = _FakeCtx(self._widths(perms=10, size=5, mtime=12))
        item = _make_item(
            self.r, id='/d/sub', title='sub/',
            col_perms='drwxr-xr-x', col_size='', col_mtime='Jun 03 11:08')
        size_seg = self.r.fs_row_content(item, ctx)[1]
        self.assertEqual(size_seg, ('     ' + '  ', _DIM[0], _DIM[1]))  # 5 spaces

    def test_rows_align_across_differing_lengths(self):
        # Two rows whose raw perms/size/mtime differ in length must, once
        # padded to the per-column max, yield equal segment widths — that
        # is the whole point of max_col_width-driven alignment.
        widths = self._widths(perms=10, size=6, mtime=12)
        ctx = _FakeCtx(widths)
        a = _make_item(
            self.r, id='/d/a', title='a',
            col_perms='-rw-r--r--', col_size='3B', col_mtime='Jun 03 11:08')
        b = _make_item(
            self.r, id='/d/bbbb', title='bbbb',
            col_perms='drwxr-xr-x', col_size='123456', col_mtime='May 01 09:00')
        segs_a = self.r.fs_row_content(a, _FakeCtx(widths))
        segs_b = self.r.fs_row_content(b, ctx)

        # Per metadata column (perms/size/mtime → indices 0/1/2) the text
        # length is identical across the two rows.
        for col in range(3):
            self.assertEqual(len(segs_a[col][0]), len(segs_b[col][0]),
                             f'column {col} widths differ between rows')
        # Concrete widths: column field width + 2-space gap.
        self.assertEqual(len(segs_a[0][0]), 10 + 2)
        self.assertEqual(len(segs_a[1][0]), 6 + 2)
        self.assertEqual(len(segs_a[2][0]), 12 + 2)

    def test_fallback_when_no_col_perms(self):
        # An item without col_perms (error / synthetic) must return EXACTLY
        # default_row_content(item, ctx) — no columns, no max_col_width call.
        ctx = _FakeCtx(self._widths())
        item = _make_item(self.r, id=('err', '/x'), title='[error] boom',
                          tag='err', tag_style='red')
        segs = self.r.fs_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(segs, [('DEFAULT', ('err', '/x'), '[error] boom')])
        # The fallback path must not measure columns.
        self.assertEqual(ctx.calls, [])

    def test_explicit_none_col_perms_also_falls_back(self):
        # Defensive: col_perms present but None still takes the fallback.
        ctx = _FakeCtx(self._widths())
        item = _make_item(self.r, id='/d/x', title='x', col_perms=None)
        segs = self.r.fs_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(ctx.calls, [])


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
        # must NOT carry col_* so fs_row_content falls back for it.
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
        # And fs_row_content takes the fallback for it.
        self.assertEqual(self.r.fs_row_content(err, _FakeCtx({})),
                         self.r.default_row_content(err, _FakeCtx({})))
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
    is auto-answered (default yes) and remembered so a test can assert it
    was never reached for a no-op. ``cursor`` is ``targets[0]``.
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

    def confirm(self, _msg):
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


if __name__ == '__main__':
    unittest.main()
