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
        item = _make_item(self.r, id='__err__:/x', title='[error] boom',
                          tag='err', tag_style='red')
        segs = self.r.fs_row_content(item, ctx)
        self.assertEqual(segs, self.r.default_row_content(item, ctx))
        self.assertEqual(segs, [('DEFAULT', '__err__:/x', '[error] boom')])
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
        self.assertTrue(err.id.startswith('__err__:'))
        self.assertEqual(err.tag, 'err')
        self.assertIsNone(getattr(err, 'col_perms', None))
        self.assertIsNone(getattr(err, 'col_size', None))
        self.assertIsNone(getattr(err, 'col_mtime', None))
        # And fs_row_content takes the fallback for it.
        self.assertEqual(self.r.fs_row_content(err, _FakeCtx({})),
                         self.r.default_row_content(err, _FakeCtx({})))


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


if __name__ == '__main__':
    unittest.main()
