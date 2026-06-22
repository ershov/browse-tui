"""Binary-level importability test for the chrome atoms (design sec A1).

The three chrome atoms — ``default_row_selection`` / ``default_row_indent`` /
``default_row_expander`` — must be importable ``from browse_tui import ...``,
exactly like ``default_row_content`` / ``default_row`` already are. That alias
only exists when the real concatenated binary loads a recipe (it does
``sys.modules['browse_tui'] = sys.modules[__name__]`` in ``--run-py``), so this
asserts the end-to-end export by running a tiny recipe through the built
binary, rather than against an isolated module load.
"""

import os
import subprocess
import unittest


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')

# A self-contained recipe: imports the atoms by name and prints a sentinel.
_RECIPE = '''
from browse_tui import (
    default_row_selection, default_row_indent, default_row_expander,
    default_row_chrome, default_row_content, default_row,
)
import browse_tui as bt
assert callable(default_row_selection)
assert callable(default_row_indent)
assert callable(default_row_expander)
# The global column measurement lives on RowContext (design sec A2).
assert hasattr(bt.RowContext, 'max_col_width_global')
print('ATOMS_IMPORTABLE')
'''


def setUpModule():
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestChromeAtomImports(unittest.TestCase):
    def test_atoms_importable_from_browse_tui(self):
        # Write the recipe to a temp file the binary can ``--run-py``.
        import tempfile
        with tempfile.NamedTemporaryFile(
                'w', suffix='.py', delete=False) as f:
            f.write(_RECIPE)
            path = f.name
        try:
            out = subprocess.run(
                [_BIN, '--run-py', path],
                capture_output=True, text=True, timeout=30,
            )
        finally:
            os.unlink(path)
        self.assertEqual(out.returncode, 0,
                         f'recipe failed: {out.stderr or out.stdout}')
        self.assertIn('ATOMS_IMPORTABLE', out.stdout)


if __name__ == '__main__':
    unittest.main()
