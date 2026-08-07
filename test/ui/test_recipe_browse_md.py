"""UI tests for the ``recipes/browse-md`` markdown browser.

The recipe takes one or more markdown files (or a directory) on the
command line and walks each file's heading / content structure. To keep
the tests hermetic we write a markdown file into a temp directory and
drive the recipe under tmux, asserting on the rendered preview.

The shebang ``#!/usr/bin/env -S browse-tui --run-py`` requires the
binary to be on PATH, which is fragile in tests; instead we invoke
``./browse-tui --run-py recipes/browse-md`` directly so the tests are
independent of the user's PATH.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-md')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


# A two-column markdown table whose natural (un-shrunk) layout is ~113
# columns wide. Both columns hold long, wrappable prose, so md2ansi's
# shrink-to-fit re-packs BOTH column widths (not just the right one) as the
# target ``line_width`` narrows — at width 120 the left column is laid out
# 56 cells wide; at ~80 it drops to ~38. Crucially that left-column width is
# a property of md2ansi's table layout, NOT of the framework's generic
# display-wrap: display-wrap only folds an already-laid-out logical line onto
# extra display rows, it can never move an interior ``┬`` divider. So the
# left-column width is a clean witness for "did md2ansi re-run at the new
# preview width" — frozen-cache leaves it pinned at the original width,
# while a correct refetch shrinks it.
#
# (This is the same fixture table the browse-claude resize test uses; the
# discriminator is a property of md2ansi, so it transfers verbatim.)
_TABLE_BODY = (
    '| Primary Configuration Column Heading | '
    'Secondary Configuration Column Heading |\n'
    '|---|---|\n'
    '| the quick brown fox jumps over the lazy dog repeatedly | '
    'the five boxing wizards jump quickly past the gate |\n'
    '| pack my box with five dozen liquor jugs every morning | '
    'how vexingly quick daft zebras jump across the field |\n'
)

# A single markdown file whose body contains the wide table. browse-md
# opens a lone file with the file row auto-expanded and the cursor on that
# file root, whose preview is the WHOLE file body rendered through md2ansi
# — so the table (the first ``┌ … ┬`` border on screen) is the cursor's
# width-dependent preview. The surrounding heading / prose just give the
# file some structure; only the table is the discriminator.
_DOC = (
    '# Resize Demo\n'
    '\n'
    'Intro paragraph for the resize fixture.\n'
    '\n'
    '## Config Table\n'
    '\n'
    + _TABLE_BODY +
    '\n'
    'Some trailing prose after the table.\n'
)


class TestBrowseMdPreviewResize(unittest.TestCase):
    """#830: a width-dependent (table) markdown preview re-renders to the
    new preview width after a pane-layout change.

    The recipe registers ``on_resize=lambda ctx, c, r: ctx.drop_preview_cache()``
    so that on ANY pane-layout change (broadened ``on_resize`` — #828) the
    width-keyed-by-nothing preview cache is dropped and the framework
    refetches ``get_preview``, which re-lays the table through md2ansi at the
    now-current ``ctx.preview_width``. Without that registration the cached
    md2ansi text (laid out at the old width) survives and the framework's
    generic display-wrap merely folds the too-wide table onto extra rows —
    the column widths stay frozen, which this test detects.

    Two legs, because a SIGWINCH-only fix would pass the first and fail the
    second:
      * terminal resize  — changes the (full-width 'h') preview width via a
        real terminal-size change;
      * split toggle alt-1/alt-2 — changes the preview width with NO
        terminal-size change (vertical split gives the preview a narrow
        right-hand pane), so only the broadened on_resize catches it.
    """

    def _make_doc(self, tmp):
        """Write the wide-table markdown file and return its path."""
        path = os.path.join(tmp, 'resize.md')
        with open(path, 'w') as f:
            f.write(_DOC)
        return path

    @staticmethod
    def _table_left_col_width(cap):
        """Width of the table's FIRST column as md2ansi laid it out.

        Reads the first border row carrying ``┌ … ┬`` and returns the run
        length between them. Works whether the preview is full-width or in a
        right-hand pane (the ``┌``/``┬`` pair sit on one captured row in both
        the frozen and the re-rendered states for this fixture). ``None`` when
        no such border row is on screen yet (still loading / mid-repaint).
        """
        for line in cap.splitlines():
            plain = re.sub(r'\x1b\[[0-9;]*m', '', line)
            i = plain.find('┌')
            if i < 0:
                continue
            j = plain.find('┬', i)
            if j > i:
                return j - i - 1
        return None

    def _settle_left_col(self, t, *, timeout=6.0):
        """Poll until the table's left-column width is stable.

        A pane-layout change drops the preview cache and the refetch +
        re-render lands a few loop iterations later. Crucially this injects
        NO ``redraw()`` / keypress: the broadened ``on_resize`` (#828) must
        self-complete on its own after #834 — the framework wakes its own
        loop so the fire → ``drop_preview_cache`` → refetch → repaint chain
        runs with no user input. We only watch the screen until three
        consecutive captures agree.
        """
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            time.sleep(0.12)
            seen.append(self._table_left_col_width(t.capture()))
            if (len(seen) >= 3 and seen[-1] is not None
                    and seen[-1] == seen[-2] == seen[-3]):
                return seen[-1]
        return seen[-1] if seen else None

    def test_table_preview_reflows_on_resize_and_split_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._make_doc(tmp)
            with TmuxFixture(cols=120, rows=40) as t:
                # Launch on the lone file; browse-md auto-expands it and
                # lands the cursor on the file root, whose preview is the
                # whole file body (the table included).
                t.launch(_BIN, '--run-py', _RECIPE, doc)
                t.wait_for('Configuration Column', timeout=5.0)
                t.wait_stable(timeout=3.0)

                # Baseline: full-width 'h' preview at 120 cols. The wide
                # table fits, so md2ansi lays the left column out at its
                # natural width (56 cells for this fixture).
                base = self._settle_left_col(t)
                self.assertEqual(
                    base, 56,
                    f'unexpected baseline left-column width {base!r} at '
                    f'120 cols (expected 56) — fixture/layout drift; '
                    f'capture:\n{t.capture()}')

                # ---- Leg 1: terminal resize 120 -> 80 -------------------
                # Narrows the full-width preview. With on_resize registered
                # the cache drops and md2ansi re-lays the table to ~80 cols,
                # shrinking the left column. Frozen (no registration) it
                # would stay 56 and the framework would only display-wrap
                # the still-56-wide table onto extra rows.
                t.resize(80, 40)
                resized = self._settle_left_col(t)
                self.assertIsNotNone(
                    resized,
                    'table border vanished after resize; capture:\n'
                    + t.capture())
                self.assertLess(
                    resized, base,
                    f'preview did NOT re-render after terminal resize: '
                    f'left-column width stayed {resized!r} (baseline {base}). '
                    f'The md2ansi table layout is frozen at the old width — '
                    f'on_resize -> drop_preview_cache is not wired. '
                    f'capture:\n{t.capture()}')

                # Restore the terminal width so the split-toggle leg starts
                # from a known full-width baseline again.
                t.resize(120, 40)
                restored = self._settle_left_col(t)
                self.assertEqual(
                    restored, base,
                    f'left-column width did not return to {base} after '
                    f'resizing back to 120 (got {restored!r}); capture:\n'
                    + t.capture())

                # ---- Leg 2: split toggle alt-1 (vertical) ---------------
                # Vertical split puts the preview in a narrow right-hand
                # pane — the terminal size is UNCHANGED, so SIGWINCH never
                # fires. Only the broadened on_resize (layout-signature
                # based) catches this. With the handler the table re-lays to
                # the narrow pane width (left column shrinks); without it the
                # 56-wide table is merely display-wrapped in the pane.
                t.send('M-1')
                toggled = self._settle_left_col(t)
                self.assertIsNotNone(
                    toggled,
                    'table border vanished after alt-1 split toggle; '
                    'capture:\n' + t.capture())
                self.assertLess(
                    toggled, base,
                    f'preview did NOT re-render after the alt-1 split '
                    f'toggle: left-column width stayed {toggled!r} '
                    f'(baseline {base}). The split changes the preview width '
                    f'with no terminal resize, so a SIGWINCH-only fix would '
                    f'miss it — the broadened on_resize + drop_preview_cache '
                    f'is what re-renders here. capture:\n{t.capture()}')

                t.send('q')


# The stdin document's root row is titled ``-`` (matching the ``browse-md
# -`` invocation). A naked ``-`` on the rendered screen is a weak match
# (dashes turn up in separators / UI chrome), so we anchor on the *row*:
# the framework's row chrome is ``<sel-marker><indent><expander><title>``,
# and the per-file root carries no id / tag (``show_ids='never'`` and the
# root Item has no tag), so the title ``-`` sits immediately after the
# expander glyph. With at least one heading the root is expandable
# (``▼``/``▶`` glyph), giving a tight ``"▼ -"``/``"▶ -"`` witness. An empty
# document has no children, so its expander is blank — there the row is a
# line that is whitespace-only up to the lone ``-`` title (and the
# childlessness is what the empty-doc test really asserts anyway).
_RE_STDIN_ROW = re.compile(r'[▼▶] -(?:\s|$)', re.MULTILINE)
_RE_STDIN_ROW_EMPTY = re.compile(r'^\s*-\s*$', re.MULTILINE)


class TestBrowseMdStdin(unittest.TestCase):
    """``browse-md -`` reads ONE document from stdin (spec §3.3 / §3.7).

    End-to-end against the shipped binary: we launch the recipe with its
    stdin redirected from a temp file (``... browse-md - < doc.md``).
    Because ``browse-md`` slurps ``sys.stdin`` BEFORE the UI starts, the
    redirect's EOF is reached during ingest and the parsed document drives
    the tree — the UI itself stays on the tmux pane's terminal. This is
    the faithful piped-input shape without needing a separate content fd:
    a file redirect is just a pipe that is already closed.
    """

    def _launch_stdin(self, t, doc_path):
        """Send ``browse-tui --run-py browse-md - < doc_path`` to the pane."""
        line = '{bin} --run-py {recipe} - < {doc}'.format(
            bin=shlex.quote(_BIN),
            recipe=shlex.quote(_RECIPE),
            doc=shlex.quote(doc_path),
        )
        t.send_line(line)

    def test_piped_document_tree_title_and_preview(self):
        # A small doc with a lone h1 (which the single-heading startup
        # cascade auto-expands) plus a nested h2 and a body run.
        body = (
            '# Piped Heading\n'
            '\n'
            'intro body text\n'
            '\n'
            '## Sub Section\n'
            'section body\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, 'doc.md')
            with open(doc, 'w') as f:
                f.write(body)
            with TmuxFixture(cols=100, rows=40) as t:
                self._launch_stdin(t, doc)
                # The top-level row is titled ``-`` (no file name). Match the
                # rendered row (expander glyph + the ``-`` title) rather than a
                # bare ``-`` substring, which UI chrome would also satisfy.
                t.wait_for(_RE_STDIN_ROW, timeout=6.0)
                # The heading tree is built from the piped text: the lone
                # h1 auto-expands, revealing its body run and the h2.
                t.wait_for('Piped Heading', timeout=4.0)
                t.wait_for('Sub Section', timeout=4.0)
                # The preview pane shows the document body (cursor lands on
                # the stdin root, whose preview is the whole text).
                t.wait_for('intro body text', timeout=4.0)
                t.send('q')

    def test_piped_without_dash_auto_engages_stdin(self):
        # ``cmd | browse-md`` with NO ``-`` on the command line: the recipe
        # auto-detects the piped (non-tty) stdin and browses the document
        # exactly as the explicit ``-`` form. Same redirect shape, just the
        # token dropped from the launch line.
        body = '# Auto Piped\nintro body text\n'
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, 'doc.md')
            with open(doc, 'w') as f:
                f.write(body)
            line = '{bin} --run-py {recipe} < {doc}'.format(
                bin=shlex.quote(_BIN),
                recipe=shlex.quote(_RECIPE),
                doc=shlex.quote(doc),
            )
            with TmuxFixture(cols=100, rows=40) as t:
                t.send_line(line)
                # The ``-`` root row appears (stdin mode) without us typing it.
                t.wait_for(_RE_STDIN_ROW, timeout=6.0)
                t.wait_for('Auto Piped', timeout=4.0)
                t.wait_for('intro body text', timeout=4.0)
                t.send('q')

    def test_empty_stdin_is_an_empty_document(self):
        # Empty input behaves exactly like an empty .md file: the ``-`` row
        # shows with no expansion arrow / children. With no heading the root
        # is childless, so its expander glyph is blank — the row is a
        # whitespace-only line ending in the lone ``-`` title. We assert that
        # row appears and stays childless (no ``[h*]`` row).
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, 'empty.md')
            with open(doc, 'w') as f:
                f.write('')
            with TmuxFixture(cols=100, rows=40) as t:
                self._launch_stdin(t, doc)
                cap = t.wait_for(_RE_STDIN_ROW_EMPTY, timeout=6.0)
                t.wait_stable(timeout=3.0)
                cap = t.capture()
                # No heading rows — an empty document has no structure.
                self.assertNotRegex(
                    cap, r'\[h[1-6]\]',
                    f'empty stdin should yield a childless document; '
                    f'capture:\n{cap}')
                t.send('q')


class TestBrowseMdRoot(unittest.TestCase):
    """``--root DIR`` extends reference resolution to extra base directories.

    End-to-end against the shipped binary: a document references a ``.md`` file
    that exists ONLY under a supplied ``--root`` (not the document's own
    directory / cwd / git-root), so without the flag the reference would be
    silently unresolvable. With it the ``[links]`` References umbrella appears
    and expands into the referenced file. Covers both the file-mode doc and the
    stdin (``-``) doc — for stdin the flag is what lifts the otherwise-total
    reference suppression.

    The doc and the root live in SEPARATE temp dirs (neither under a ``.git``),
    so the referenced file is outside the doc's resolution defaults and its
    display label is the absolute path — which still contains ``target.md`` as
    a screen witness.
    """

    def _make_fixture(self, tmp):
        """Write ``docdir/main.md`` (refs ``target.md``) + ``rootA/target.md``.

        Returns ``(main_md, rootA)``. ``main.md`` has a single h1 and a bare
        relative ``target.md`` token; ``target.md`` lives only under ``rootA``
        and carries its own heading so the expanded ref shows structure.
        """
        docdir = os.path.join(tmp, 'docdir')
        rootA = os.path.join(tmp, 'rootA')
        os.makedirs(docdir)
        os.makedirs(rootA)
        main_md = os.path.join(docdir, 'main.md')
        with open(main_md, 'w') as f:
            f.write('# Main heading\nsee target.md for the rest\n')
        with open(os.path.join(rootA, 'target.md'), 'w') as f:
            f.write('# Target heading\nbody\n')
        return main_md, rootA

    def _expand_references(self, t):
        """Move the cursor onto the ``References`` umbrella row and expand it.

        The single-file startup auto-expands the file root, so its children —
        the lone ``[h1]`` heading and the ``[links] References`` umbrella — are
        on screen. Step Down twice (root → h1 → References) and press Right to
        expand the umbrella, revealing the referenced file row.
        """
        t.wait_for('References', timeout=6.0)
        t.send('Down')   # file root -> [h1] Main heading
        t.send('Down')   # -> [links] References
        t.send('Right')  # expand the umbrella

    def test_file_mode_ref_resolves_only_via_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            main_md, rootA = self._make_fixture(tmp)
            with TmuxFixture(cols=100, rows=40) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--root', rootA, main_md)
                # The file's own heading renders, and — because the ref
                # resolves via --root — a References umbrella appears.
                t.wait_for('Main heading', timeout=6.0)
                self._expand_references(t)
                # The referenced file (resolved under rootA) shows up; its
                # label is the abspath, which contains 'target.md'.
                t.wait_for('target.md', timeout=4.0)
                t.send('q')

    def test_file_mode_without_root_has_no_references(self):
        # Control: the SAME doc without --root has no resolvable ref, so no
        # References umbrella ever renders (today's behavior, unchanged).
        with tempfile.TemporaryDirectory() as tmp:
            main_md, _rootA = self._make_fixture(tmp)
            with TmuxFixture(cols=100, rows=40) as t:
                t.launch(_BIN, '--run-py', _RECIPE, main_md)
                t.wait_for('Main heading', timeout=6.0)
                cap = t.wait_stable(timeout=3.0)
                self.assertNotIn(
                    'References', cap,
                    f'no References umbrella without --root; capture:\n{cap}')
                t.send('q')

    def test_stdin_doc_ref_resolves_via_root(self):
        # The piped doc's refs are suppressed by default; --root lifts that and
        # resolves them against the root, surfacing the References umbrella.
        with tempfile.TemporaryDirectory() as tmp:
            main_md, rootA = self._make_fixture(tmp)
            with TmuxFixture(cols=100, rows=40) as t:
                line = '{bin} --run-py {recipe} --root {root} - < {doc}'.format(
                    bin=shlex.quote(_BIN),
                    recipe=shlex.quote(_RECIPE),
                    root=shlex.quote(rootA),
                    doc=shlex.quote(main_md),
                )
                t.send_line(line)
                # The stdin row is titled ``-``; its heading renders, and the
                # ref resolves via --root into a References umbrella.
                t.wait_for(_RE_STDIN_ROW, timeout=6.0)
                t.wait_for('Main heading', timeout=4.0)
                self._expand_references(t)
                t.wait_for('target.md', timeout=4.0)
                t.send('q')


class TestBrowseMdEditSection(unittest.TestCase):
    """``E`` edits the section under the cursor through ``$EDITOR``.

    End-to-end against the shipped binary: ``$EDITOR`` is a non-interactive
    script that overwrites its file argument with a fixed replacement
    section, so pressing ``E`` on a heading row exercises the whole
    pipeline — extent capture → temp file → editor → hash-addressed splice
    → file write-back → reparse — without a real interactive editor.
    Asserted on the re-rendered preview AND the exact bytes written back
    to disk (an edit landing on the wrong row would splice a different
    final document).
    """

    _DOC = (
        '# Alpha\n'
        '\n'
        'alpha body\n'
        '\n'
        '## Section Two\n'
        '\n'
        'section two body\n'
    )
    _EDITED = '## Section Two\n\nEDITED BODY LINE\n'

    def test_edit_heading_section_via_editor_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, 'doc.md')
            with open(doc, 'w') as f:
                f.write(self._DOC)
            ed = os.path.join(tmp, 'fake-editor')
            with open(ed, 'w') as f:
                f.write('#!/bin/sh\n'
                        'cat > "$1" <<\'EOF\'\n'
                        '## Section Two\n'
                        '\n'
                        'EDITED BODY LINE\n'
                        'EOF\n')
            os.chmod(ed, 0o755)
            with TmuxFixture(cols=100, rows=40) as t:
                line = 'EDITOR={ed} {bin} --run-py {recipe} {doc}'.format(
                    ed=shlex.quote(ed),
                    bin=shlex.quote(_BIN),
                    recipe=shlex.quote(_RECIPE),
                    doc=shlex.quote(doc),
                )
                t.send_line(line)
                # Single-file startup auto-expands the file row, and the
                # lone-h1 cascade expands Alpha too — so the [text] run and
                # the h2 are already rows. Sync on rendered content before
                # sending keys.
                t.wait_for('Section Two', timeout=6.0)
                t.send('Down')   # file root -> [h1] Alpha
                t.send('Down')   # -> [text] alpha body
                t.send('Down')   # -> [h2] Section Two
                t.send('E')
                # The editor script returns immediately; the apply chain
                # splices, writes back, and the refreshed preview shows the
                # new section body under the (unmoved) cursor.
                t.wait_for('EDITED BODY LINE', timeout=6.0)
                with open(doc) as f:
                    final = f.read()
                self.assertEqual(
                    final, '# Alpha\n\nalpha body\n\n' + self._EDITED)
                t.send('q')


class TestBrowseMdInsertSection(unittest.TestCase):
    """``a`` inserts a new section at the marker through ``$EDITOR``.

    End-to-end against the shipped binary: pressing ``a`` on a heading
    row enters marker mode (the ``-- insert section --`` marker row
    renders below the cursor), Enter confirms the default gap (sibling
    after the cursor row), and the scripted ``$EDITOR`` replaces the
    template with a fixed new section — exercising marker mode, the
    template seed, the splice, the write-back and the cursor landing on
    the new row. Asserted on the re-rendered pane AND the exact bytes on
    disk.
    """

    _DOC = (
        '# Alpha\n'
        '\n'
        'alpha body\n'
        '\n'
        '## Section Two\n'
        '\n'
        'section two body\n'
    )
    _NEW = '## Section Three\n\nINSERTED BODY LINE\n'

    def test_insert_after_heading_via_marker_and_editor_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, 'doc.md')
            with open(doc, 'w') as f:
                f.write(self._DOC)
            ed = os.path.join(tmp, 'fake-editor')
            with open(ed, 'w') as f:
                f.write('#!/bin/sh\n'
                        'cat > "$1" <<\'EOF\'\n'
                        '## Section Three\n'
                        '\n'
                        'INSERTED BODY LINE\n'
                        'EOF\n')
            os.chmod(ed, 0o755)
            with TmuxFixture(cols=100, rows=40) as t:
                line = 'EDITOR={ed} {bin} --run-py {recipe} {doc}'.format(
                    ed=shlex.quote(ed),
                    bin=shlex.quote(_BIN),
                    recipe=shlex.quote(_RECIPE),
                    doc=shlex.quote(doc),
                )
                t.send_line(line)
                t.wait_for('Section Two', timeout=6.0)
                t.send('Down')   # file root -> [h1] Alpha
                t.send('Down')   # -> [text] alpha body
                t.send('Down')   # -> [h2] Section Two
                t.send('a')
                # Marker mode is on: the labelled marker row renders.
                t.wait_for('insert section', timeout=4.0)
                # Confirm the default gap — sibling right after the
                # cursor row → relation 'after' on Section Two.
                t.send('Enter')
                # Editor script runs, the splice lands, the tree
                # refreshes and the cursor lands on the new section.
                t.wait_for('Section Three', timeout=6.0)
                t.wait_for('INSERTED BODY LINE', timeout=6.0)
                with open(doc) as f:
                    final = f.read()
                self.assertEqual(final, self._DOC + self._NEW)
                t.send('q')


class TestBrowseMdMoveSection(unittest.TestCase):
    """``x`` moves the cursor section to the marker — no editor involved.

    End-to-end against the shipped binary: pressing ``x`` on a heading
    row enters marker mode with the ``-- move here --`` marker, moving
    the marker below the next section and confirming cuts the section's
    bytes and re-inserts them there. Asserted on the re-rendered pane
    AND the exact bytes on disk (a forward move — the direction whose
    surgery re-bases the block past the cut).
    """

    _DOC = (
        '# Doc\n'
        '\n'
        '## First\n'
        '\n'
        'FIRST BODY LINE\n'
        '\n'
        '## Second\n'
        '\n'
        'second body\n'
    )
    _MOVED = (
        '# Doc\n'
        '\n'
        '## Second\n'
        '\n'
        'second body\n'
        '## First\n'
        '\n'
        'FIRST BODY LINE\n'
        '\n'
    )

    def test_move_forward_via_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = os.path.join(tmp, 'doc.md')
            with open(doc, 'w') as f:
                f.write(self._DOC)
            with TmuxFixture(cols=100, rows=40) as t:
                line = '{bin} --run-py {recipe} {doc}'.format(
                    bin=shlex.quote(_BIN),
                    recipe=shlex.quote(_RECIPE),
                    doc=shlex.quote(doc),
                )
                t.send_line(line)
                t.wait_for('Second', timeout=6.0)
                t.send('Down')   # file root -> [h1] Doc
                t.send('Down')   # -> [h2] First
                t.send('x')
                # Marker mode is on: the labelled marker row renders.
                t.wait_for('move here', timeout=4.0)
                # Default gap is right below First; one step down lands
                # past Second → relation 'after' on Second.
                t.send('Down')
                t.send('Enter')
                # Both titles render before AND after the move, so the
                # screen offers no unique needle — poll the ground truth
                # instead: the file's moved bytes, then the re-rendered
                # tree listing Second above First. The ``[h2]``-tagged
                # forms are the TREE rows (preview text carries no tag
                # chip), so the order check can't trip on the preview.
                deadline = time.time() + 6.0
                final = pane = None
                while time.time() < deadline:
                    with open(doc) as f:
                        final = f.read()
                    pane = t.capture()
                    if (final == self._MOVED and '[h2] Second' in pane
                            and (pane.index('[h2] Second')
                                 < pane.index('[h2] First'))):
                        break
                    time.sleep(0.05)
                self.assertEqual(final, self._MOVED)
                self.assertLess(pane.index('[h2] Second'),
                                pane.index('[h2] First'))
                t.send('q')


class TestBrowseMdMoveSectionCrossDocument(unittest.TestCase):
    """``x`` moves a section into a DIFFERENT document via the marker.

    End-to-end against the shipped binary: the primary doc references a
    second ``.md`` file; expanding the References umbrella reveals the
    ``[md]`` row, and confirming the marker in the gap below that
    (collapsed) row resolves to 'after' the referenced doc's ROOT — the
    file's bottom. The destination insert and the source cut are
    asserted on BOTH files' final bytes (ground truth), plus the
    tag-anchored tree no longer listing the moved ``[h2]`` row.
    """

    _MAIN = ('# Main\n'
             '\n'
             'See other.md for details.\n'
             '\n'
             '## Movable\n'
             '\n'
             'MOVABLE BODY LINE\n')
    _OTHER = ('# Other\n'
              '\n'
              'OTHER BODY LINE\n')
    _MOVED = '## Movable\n\nMOVABLE BODY LINE\n'
    _MAIN_AFTER = '# Main\n\nSee other.md for details.\n\n'

    def test_move_into_referenced_doc_via_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, 'main.md')
            other = os.path.join(tmp, 'other.md')
            with open(main, 'w') as f:
                f.write(self._MAIN)
            with open(other, 'w') as f:
                f.write(self._OTHER)
            with TmuxFixture(cols=100, rows=40) as t:
                t.launch(_BIN, '--run-py', _RECIPE, main)
                # Startup auto-expand + lone-h1 cascade: the [text] run,
                # [h2] Movable and the [links] References umbrella are
                # all rows. Sync on rendered content before sending keys.
                t.wait_for('Movable', timeout=6.0)
                t.wait_for('References', timeout=4.0)
                # Reveal the [md] other.md row (it stays collapsed), then
                # park the cursor back on Movable. The [md]-tagged form is
                # the TREE row — the preview also contains 'other.md' (the
                # doc's own prose), so the tag anchors the wait.
                t.send('Down')   # main.md -> [h1] Main
                t.send('Down')   # -> [text] See other.md...
                t.send('Down')   # -> [h2] Movable
                t.send('Down')   # -> [links] References
                t.send('Right')  # expand the umbrella
                t.wait_for('[md] other.md', timeout=4.0)
                t.send('Up')     # back to [h2] Movable
                t.send('x')
                t.wait_for('move here', timeout=4.0)
                # Default gap sits right below Movable; two steps down
                # walk past the umbrella's child gap ('first' References)
                # to the gap below the collapsed [md] row — 'after' the
                # referenced doc's ROOT, i.e. that file's bottom.
                t.send('Down')
                t.send('Down')
                t.send('Enter')
                # Ground truth first: the destination gained the section
                # and the source lost it; then the refreshed tree no
                # longer lists the [h2] Movable row (the tag chip is
                # tree-only, so the check can't trip on preview text).
                deadline = time.time() + 6.0
                final_main = final_other = pane = None
                while time.time() < deadline:
                    with open(main) as f:
                        final_main = f.read()
                    with open(other) as f:
                        final_other = f.read()
                    pane = t.capture()
                    if (final_other == self._OTHER + self._MOVED
                            and final_main == self._MAIN_AFTER
                            and '[h2] Movable' not in pane):
                        break
                    time.sleep(0.05)
                self.assertEqual(final_other, self._OTHER + self._MOVED)
                self.assertEqual(final_main, self._MAIN_AFTER)
                self.assertNotIn('[h2] Movable', pane)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
