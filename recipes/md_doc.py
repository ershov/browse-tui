#!/usr/bin/env python3

"""md_doc — shared markdown cross-document plumbing for the recipes.

Document STRUCTURE now comes from ``md2ansi_lib``'s own document model
(``md2ansi_doc`` → ``M2A_Doc`` / ``M2A_Node``); this module no longer builds
trees of its own. What it still owns is the plumbing both ``browse-md`` and
``browse-claude`` need AROUND those trees:

  * reference detection/resolution between ``.md`` files (``find_md_refs`` /
    ``resolve_md_ref`` / ``find_git_root``), the cheap heading-detection gate
    (``md_heading_trigger``), and a process-wide parse cache (``get_doc`` /
    ``clear_cache`` — the cached tree is an ``M2A_Doc``);
  * the two tree helpers the recipes share: ``visible_children`` (the ``#text``
    row-set rule deciding which ``M2A_Node``s become rows) and ``node_at_line``
    (row-id line lookup over a built tree).

This half deliberately knows nothing about ``browse_tui`` / ``Item``: each
recipe maps ``M2A_Node``s onto its own ``Item``/id space, tags, and styling.
``browse-md`` and ``browse-claude`` import THIS module; it never imports them.

A second, ``browse_tui``-AWARE section at the end of the file (guarded by the
same optional ``from browse_tui import …`` the plugin-registration block uses)
is the single home for the *markdown launcher rows* shared between recipes: it
resolves a document's ``.md`` references to labelled targets (``resolve_refs``
/ ``ref_label``), builds the ``[md] References`` umbrella + ``[md ↗]``
launcher-row ``Item``s, and owns the one ``launch`` helper that shells out to
``browse-md`` (the embedding flags + the stdin-vs-file delivery policy live
there and nowhere else). The launch *target* inside a launcher-row id is opaque
to ``md_doc`` — the hosting recipe interprets it at activate time. With
``browse_tui`` absent these helpers are simply not defined; the half above
stays importable standalone regardless.
"""

import os
import re

# Document parsing is delegated to the shared markdown grammar in
# ``md2ansi_lib`` — the same scanner the renderer uses, so the structure we
# cache can never drift from what gets rendered. ``md_doc`` is a sibling
# recipe file, so the import resolves once ``recipes/`` is on ``sys.path``
# (the recipes prepend their own directory at runtime; tests do the same).
from md2ansi_lib import md2ansi_doc


# ### Section: Tree helpers over the M2A document model ####################

def visible_children(nodes):
    """The nodes of one children list that a recipe shows as rows.

    The ``#text`` row-set rule, shared by every tree-mapping call site:
    ``md2ansi_doc`` gives EVERY heading a ``#text`` first child (zero-width
    when the body is blank) plus a preamble node at ``tree[0]``, but a text
    run is a row only when it is non-empty AND has at least one heading
    sibling (i.e. it is the intro run of a scope that also has sub-headings).
    Zero-width runs and leaf-chapter bodies are filtered out, so a leaf
    heading stays a leaf and its whole body reads through its own preview.

    ``nodes`` is one children list — ``M2A_Doc.tree`` or an ``M2A_Node``'s
    ``children`` (it holds at most one text run, always first). Returns a new
    list; headings always pass through.
    """
    has_heading = any(n.kind == 'heading' for n in nodes)
    return [n for n in nodes
            if n.kind == 'heading' or (n.byte_size > 0 and has_heading)]


def node_at_line(tree, line_offset):
    """Find the visible ``M2A_Node`` at ``line_offset``, or ``None``.

    Depth-first walk of a built tree — ``M2A_Doc.tree`` and, recursively,
    each node's ``children`` — over the VISIBLE nodes only (the same
    ``visible_children`` row set the recipes materialise), returning the
    first one whose ``line_offset`` *exactly* equals the target. Row ids
    carry a visible node's line, so restricting the walk both matches the
    lookup's purpose and sidesteps the zero-width text runs, whose virtual
    line can coincide with a real row's (e.g. the empty preamble shares
    line 0 with a document's opening heading). A ``line_offset`` that
    matches no visible node — including one before the first node — yields
    ``None``. The tree is one document's structure, so a linear search is
    plenty.
    """
    for node in visible_children(tree):
        if node.line_offset == line_offset:
            return node
        found = node_at_line(node.children, line_offset)
        if found is not None:
            return found
    return None


# ### Section: Reference detection & resolution ############################

# Captures a ``.md`` reference token. The body excludes whitespace and the
# chars most likely to be noise around a path rather than part of it:
# ``"`` and ``\`` (JSON string quotes / escapes), ``$`` (shell variables),
# ``*`` (globs), the inline-code backtick, and the markdown link / autolink /
# wiki-link delimiters ``( ) [ ] < >``. So a path embedded in a raw JSONL line,
# a shell command, or prose all match cleanly while ``"foo.md"`` captures
# ``foo.md`` (not the quote), ``$X/y.md`` stops at the ``$``, and the markdown
# link ``[docs/cli.md](docs/cli.md)`` yields ``docs/cli.md`` twice (label +
# target) instead of the unresolvable blob ``[docs/cli.md](docs/cli.md``.
# Likewise ``<docs/api.md>`` -> ``docs/api.md``, `` `report.md` `` ->
# ``report.md``, ``[x]: docs/ref.md`` -> ``docs/ref.md``, and
# ``[[docs/wiki.md]]`` -> ``docs/wiki.md``. Case: both ``.md`` and ``.MD``.
# Trade-off: a filename literally containing ``( ) [ ] < >`` or a backtick is
# no longer captured — that is exceedingly rare, and surfacing the COMMON
# markdown-link form is far more valuable.
#
# The leading ``(?<![^\s"`\\$*()\[\]<>])`` is a negative lookbehind asserting
# the previous char is one of the excluded separators (whitespace / ``"`` /
# backtick / ``\`` / ``$`` / ``*`` / ``( ) [ ] < >``) OR the start of the
# string — i.e. the token begins at the first non-separator char, INCLUDING a
# leading ``/`` or ``~``. A plain ``\b`` would not: a word boundary anchors on
# the first *word* char, silently dropping a leading ``/`` (``/abs/x.md`` ->
# ``abs/x.md``) or ``~`` (``~/n.md`` -> ``n.md``), which made
# ``resolve_md_ref``'s absolute/``~`` branch unreachable. The trailing ``\b``
# is kept so ``.mdx`` and a trailing ``.`` are still excluded.
_MD_REF_RE = re.compile(r'(?<![^\s"`\\$*()\[\]<>])[^\s"`\\$*()\[\]<>]+\.(?:md|MD)\b')

# Cheap heading-detection gate: a run of ``#`` at the start of a line (after
# optional indent) FOLLOWED by a mandatory space/tab — matching the authoritative
# grammar (``_MD_H1``..``_MD_H6`` all require ``[ \t]+``), so ``#foo`` is not a
# heading but ``# foo`` is. Pure regex — NO full grammar scan — so it is fast
# enough to run on every item at delivery time. It is intentionally *optimistic*:
# it also fires on a ``#`` that lives only inside a fenced code block, which the
# authoritative ``md2ansi_doc`` parse later rejects (the recipe self-heals the
# stale arrow).
_MD_HEADING_TRIGGER_RE = re.compile(r'(?:^|\n)[ \t]*#+[ \t]')


def find_md_refs(text):
    """Ordered list of captured ``.md`` reference strings in ``text``.

    Each result is the raw captured token (e.g. ``docs/report.md``,
    ``~/notes.MD``, ``/abs/x.md``), in document order, with duplicates kept —
    de-duplication is the caller's job (and is best done by resolved abspath,
    not by raw token, since two tokens can resolve to the same file). Returns
    ``[]`` when nothing matches.
    """
    return _MD_REF_RE.findall(text)


def md_heading_trigger(text):
    """True if ``text`` *might* contain a markdown heading (cheap gate).

    A pure-regex pre-filter for the detection path — matches a ``#`` at the
    start of any line. Optimistic by design: a ``#`` inside a fenced code
    block also matches here (only the real ``md2ansi_doc`` parse can tell them
    apart), so a ``True`` result means "worth building the real tree", not
    "definitely has a heading".
    """
    return _MD_HEADING_TRIGGER_RE.search(text) is not None


def resolve_md_ref(captured, *, doc_dir, cwd, project_root, extra_bases=()):
    """Resolve a captured ``.md`` token to an existing absolute path, or None.

    Tries candidate bases in order and returns the FIRST one that exists on
    disk (as a real path):

      1. absolute path, or a ``~``-prefixed path — taken as-is via
         ``expanduser`` (already rooted, so no base is joined);
      2. relative to ``doc_dir`` — the referencing document's own directory
         (the CommonMark norm; for an inline document ``doc_dir == cwd``);
      3. relative to each of ``extra_bases`` in order — caller-supplied
         candidate roots (e.g. ``browse-md``'s repeatable ``--root DIR``), so a
         document whose real root is somewhere the defaults don't reach still
         resolves. Empty by default, which leaves the candidate list — and so
         every resolution — exactly as it was before this hook existed;
      4. relative to ``cwd`` — the record's working directory;
      5. relative to ``project_root`` — the git/project root.

    Returns ``os.path.realpath`` of the first existing candidate (canonical, so
    callers can dedup by plain string compare), or ``None`` if none exists.
    De-duplication across a document's refs is the caller's job.
    """
    expanded = os.path.expanduser(captured)
    if os.path.isabs(expanded):
        # Absolute (or ``~``-expanded to absolute) — used directly, no base.
        candidates = [expanded]
    else:
        # ``doc_dir`` first (CommonMark norm), then the caller's extra roots,
        # then the ``cwd`` / ``project_root`` fallbacks. With no extra roots the
        # list is the original ``[doc_dir, cwd, project_root]``.
        candidates = [os.path.join(doc_dir, captured)]
        candidates += [os.path.join(base, captured) for base in extra_bases]
        candidates += [
            os.path.join(cwd, captured),
            os.path.join(project_root, captured),
        ]
    for cand in candidates:
        if os.path.exists(cand):
            return os.path.realpath(cand)
    return None


def find_git_root(start):
    """Nearest ancestor of ``start`` (inclusive) holding a ``.git`` entry.

    Walks parents until a ``.git`` directory **or file** is found (a file is
    how git worktrees and submodules record their gitdir), stopping at the
    filesystem root. Returns the containing directory, or ``None`` when no
    ``.git`` is found along the way (or ``start`` is falsy).

    The recipes feed the result (falling back to ``start`` itself) as the
    ``project_root`` anchor for ``resolve_md_ref``: ``browse-md`` walks up from
    a file's own directory, ``browse-claude`` from a session's recorded cwd.
    Lifted verbatim from the recipes' ``_find_git_root`` (both carried an
    identical copy).
    """
    if not start:
        return None
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, '.git')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


# ### Section: Process-wide parse cache ####################################

# A referenced file is read and scanned once regardless of how many places (or
# how many times along a reference cycle) reach it: ``get_doc`` memoises
# ``abspath -> (text, M2A_Doc)``. The recipes clear this on their reload paths
# (``Ctrl-R`` / ``_bust_caches_for``) via ``clear_cache``.

_DOC_CACHE = {}


def get_doc(abspath):
    """Return ``(text, doc)`` for ``abspath``, reading + parsing once.

    ``text`` is the file's full decoded contents; ``doc`` is the ``M2A_Doc``
    document model from ``md2ansi_doc(text)`` (its ``.tree`` holds the
    ``M2A_Node`` roots). Subsequent calls for the same path return the cached
    pair without re-reading or re-parsing. The path is used as the cache key
    verbatim — callers pass the canonical abspath from ``resolve_md_ref`` so
    two routes to one file share an entry.

    I/O errors are not swallowed here: a caller resolves the ref to an existing
    path before calling, and a genuine read failure (permission, race) is worth
    surfacing. Decoding, though, is best-effort — ``errors='replace'`` so a
    referenced ``.md`` with a stray non-UTF-8 byte still parses its headings
    (a substituted U+FFFD never reads as a structural ``#``) rather than
    raising ``UnicodeDecodeError`` (a ``ValueError`` that would slip past a
    caller's ``except OSError`` guard and surface an error banner).
    """
    cached = _DOC_CACHE.get(abspath)
    if cached is not None:
        return cached
    with open(abspath, encoding='utf-8', errors='replace') as f:
        text = f.read()
    pair = (text, md2ansi_doc(text))
    _DOC_CACHE[abspath] = pair
    return pair


def clear_cache():
    """Drop every cached document. Called on the recipes' reload paths."""
    _DOC_CACHE.clear()


# ### Section: Reference label + filesystem reference resolution ###########

# These two helpers sit just above the ``browse_tui``-aware launcher block but
# need no ``Item`` — they are pure path logic, so they stay in the
# framework-agnostic half and the launcher builders below call them.

def ref_label(abspath, project_root):
    """Display label for a launcher target, anchored on ``project_root``.

    Relative to ``project_root`` when the target is inside it, else a
    ``~``-collapsed absolute path — so a flat launcher list reads cleanly
    without ``../`` noise. Lifted from browse-fs's ``_md_ref_label``; it is
    also the natural sort key for ``resolve_refs``.
    """
    if project_root and (abspath == project_root
                         or abspath.startswith(project_root.rstrip('/') + '/')):
        return os.path.relpath(abspath, project_root)
    home = os.path.expanduser('~')
    if abspath == home or abspath.startswith(home + os.sep):
        return '~' + abspath[len(home):]
    return abspath


def resolve_refs(text, *, doc_dir, cwd, project_root):
    """Distinct existing ``.md`` references in ``text`` as ``(abspath, label)``.

    Runs ``find_md_refs`` over ``text``, resolves each captured token with
    ``resolve_md_ref`` (FILESYSTEM resolution only — a caller that follows
    git-tree blobs does its own resolution), drops the ones that don't exist or
    repeat an already-seen file (deduped by canonical abspath), and returns the
    survivors sorted by display ``label`` (``ref_label`` against
    ``project_root``). The referencing document's own path is NOT excluded — a
    caller that lists a self-open row first dedups it via the ``seen`` set it
    threads in (browse-fs seeds ``seen`` with the file itself).

    ``doc_dir`` / ``cwd`` / ``project_root`` are the resolution bases, same
    meaning as ``resolve_md_ref``'s. Returns ``[]`` when nothing resolves.
    """
    seen = set()
    out = []
    for captured in find_md_refs(text):
        ab = resolve_md_ref(captured, doc_dir=doc_dir, cwd=cwd,
                            project_root=project_root)
        if ab is None or ab in seen:
            continue
        seen.add(ab)
        out.append((ab, ref_label(ab, project_root)))
    out.sort(key=lambda pair: pair[1])
    return out


# ### Section: browse_tui-aware launcher rows ##############################

# Everything below needs the framework's ``Item`` (and, for the launch helper,
# a live ``ctx``). The import is guarded exactly like the plugin-registration
# block: under a browse-tui interpreter these names are defined and recipes use
# them; as a standalone library (``browse_tui`` not on the path) they are simply
# absent and only the structural half above is importable.
#
# The launcher-row id convention is generic and recipe-routable:
#
#     ('launch', anchor, *spec)
#
# ``anchor`` ties the row to the thing it expanded from (a parent path, a
# message id, …) so sibling rows stay distinct; ``*spec`` is the launch target,
# OPAQUE to md_doc — the recipe's Enter handler unpacks it and calls ``launch``.
# browse-fs uses ``('launch', parent_path, 'md-file', target_abspath)``. Ids
# stay hashable (flat tuple, never a list) and store *what* to launch, not a
# command line, so they survive rebuilds and environment changes.

# Embedding flags for a browse-md launched from inside another browse-tui:
# ``--no-alt-screen`` renders on the parent's alternate screen without the
# child's own switch (paired with ``run_external(keep_screen=True)`` so neither
# the launch nor the return flashes the primary screen), and
# ``--quit-on-scope-up`` makes Alt-Up at the child's top quit it (returning to
# the parent) rather than no-op'ing. This tuple + the stdin policy in ``launch``
# are the single home for "how a recipe embeds browse-md".
_LAUNCH_FLAGS = ('--no-alt-screen', '--quit-on-scope-up')

try:
    from browse_tui import Item

    def references_umbrella(anchor):
        """A ``[md] References`` umbrella ``Item`` over a document's links.

        Expandable grouping row (``has_children=True``) whose id is
        ``('md-refs', anchor)`` — distinct from the ``('launch', …)`` leaf ids
        so a recipe routes the two apart. The caller supplies the umbrella's
        children (one ``launcher_row`` per ``resolve_refs`` result). Provided
        for callers that group references under a parent; browse-fs lists its
        launcher rows flat and does not use it.
        """
        return Item(id=('md-refs', anchor), title='References',
                    tag='md', has_children=True)

    def launcher_row(anchor, spec, label):
        """One ``[md ↗]`` launcher row titled ``label``.

        Leaf row (``has_children=False``) with the generic, recipe-routable id
        ``('launch', anchor, *spec)`` — ``spec`` is the opaque launch target
        (e.g. ``('md-file', abspath)``) the recipe unpacks at Enter time. The
        ``↗`` in the ``[md ↗]`` tag chip signals that Enter launches an external
        browser rather than expanding or editing the row. The id is a structural
        routing tuple, so ``show_ids='auto'`` never displays it.
        """
        return Item(id=('launch', anchor, *spec), title=label,
                    tag='md ↗', tag_style='yellow', has_children=False)

    def launch(ctx, *, path=None, content=None, label=None, roots=()):
        """Open a markdown document in ``browse-md`` as an external process.

        The single home for the embedding flags + the stdin-vs-file delivery
        policy. Exactly one of ``path`` / ``content`` is given:

          * ``path`` — an on-disk ``.md`` file. Launched by plain argv:
            ``['browse-md', *flags, path, '--root', *roots]``.
          * ``content`` — markdown text NOT backed by a file (e.g. a transcript
            message). Fed to ``browse-md -`` on its **stdin via a real pipe**
            (``run_external(stdin_text=…)``): ``['browse-md', '-', *flags,
            --root …]`` with the document written to the child's stdin. A pipe
            has no size limit, so an arbitrarily large document is fine — an
            earlier env-var delivery hit ``MAX_ARG_STRLEN`` → ``E2BIG``. A stdin
            document's own reference-following is suppressed unless ``--root``
            bases are supplied, so ``roots`` is precisely what lets its refs
            resolve.

        ``roots`` is an ordered sequence of ``--root`` resolution bases (the
        file's own directory is always browse-md's first base, so these are
        tried after it). ``label`` is accepted for symmetry / future surfacing
        and otherwise unused. The handoff keeps the parent on the alternate
        screen (``keep_screen=True``); see ``_LAUNCH_FLAGS`` for the flags.
        """
        flags = list(_LAUNCH_FLAGS)
        root_args = []
        for r in roots:
            root_args += ['--root', r]
        if path is not None:
            ctx.run_external(['browse-md', *flags, path, *root_args],
                             keep_screen=True)
            return
        # ``content`` form: feed the document to ``browse-md -`` on a real
        # stdin pipe — no argv/env size limit (see run_external ``stdin_text``).
        ctx.run_external(['browse-md', '-', *flags, *root_args],
                         keep_screen=True, stdin_text=(content or ''))

except ImportError:
    pass


# ### Section: Plugin registration #########################################

# Make this file double as a browse-tui plugin: when imported under a
# browse-tui interpreter (recipe / --plugin), self-register so the framework
# knows we're loaded. The import is guarded so the module stays importable as a
# standalone library when ``browse_tui`` isn't on the path — exactly as
# ``md2ansi_lib`` does it.

try:
    from browse_tui import register_plugin, PluginConfig
    register_plugin(PluginConfig(name='md_doc'))
except ImportError:
    pass
