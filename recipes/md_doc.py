#!/usr/bin/env python3

"""md_doc — shared, framework-agnostic markdown-structure logic.

This module is the single source of truth for turning markdown text into a
navigable *document structure* — a tree of heading (and optionally list-item)
nodes — plus the cross-document plumbing both ``browse-md`` and
``browse-claude`` need: reference detection/resolution between ``.md`` files,
the cheap heading-detection gate, and a process-wide parse cache.

Its STRUCTURAL half deliberately knows nothing about ``browse_tui`` / ``Item``:
it returns plain ``MdNode`` dataclasses, and each recipe maps those onto its own
``Item``/id space, tags, and styling. That keeps the markdown logic unit-testable
in isolation and decoupled from the TUI framework. ``browse-md`` and
``browse-claude`` import THIS module; it never imports them.

A second, ``browse_tui``-AWARE section at the end of the file (after the
structural logic, guarded by the same optional ``from browse_tui import …`` the
plugin-registration block uses) is the single home for the *markdown launcher
rows* shared between recipes: it resolves a document's ``.md`` references to
labelled targets (``resolve_refs`` / ``ref_label``), builds the ``[md]
References`` umbrella + ``[md ↗]`` launcher-row ``Item``s, and owns the one
``launch`` helper that shells out to ``browse-md`` (the embedding flags + the
stdin-vs-file delivery policy live there and nowhere else). The launch *target*
inside a launcher-row id is opaque to ``md_doc`` — the hosting recipe interprets
it at activate time. With ``browse_tui`` absent these helpers are simply not
defined; the structural half above stays importable standalone regardless.

The heading/list tree builder is ``browse-md``'s ``_build_nodes`` generalised
and lifted out: same ``md2ansi_scan`` source-of-truth scan, same line/byte
indexing, same boundary rule, same two-stack nesting — minus the ``Item``
construction and id/anchor bookkeeping, which now live in the recipes.

A note on "byte": the offsets here (``byte_offset`` / ``byte_size``) are
character offsets into the decoded ``str`` text — exactly what
``md2ansi_scan`` reports and what callers slice with
(``text[byte_offset : byte_offset + byte_size]``). The name is kept for
continuity with ``browse-md``'s field names and the design spec; for pure-ASCII
markdown (the common case) chars and bytes coincide.
"""

import bisect
import os
import re
from dataclasses import dataclass, field
from typing import Optional

# Heading-tree building is delegated to the shared markdown grammar in
# ``md2ansi_lib`` — the same scanner the renderer uses, so the structure we
# expose can never drift from what gets rendered. ``md_doc`` is a sibling
# recipe file, so the import resolves once ``recipes/`` is on ``sys.path``
# (the recipes prepend their own directory at runtime; tests do the same).
from md2ansi_lib import md2ansi_scan


# ### Section: Structural node model #######################################

@dataclass(slots=True)
class MdNode:
    """One node in a document's heading/list structure.

    Plain data, no ``Item`` coupling — recipes map these onto their own row
    types. Fields:

      * ``kind``        — ``'heading'``, ``'list-item'``, or ``'text'`` (a dim
                          run of body text preceding a scope's first heading,
                          synthesised by ``build_doc_tree`` when lists are off).
      * ``level``       — heading level (1..6) or list indent level (0-based).
                          A ``'text'`` node borrows the level of the first
                          heading it precedes (so it renders alongside it).
      * ``title``       — the row label: for headings/list-items the source
                          line with the leading sigil stripped (``##``/``-``/
                          ``1.`` + following whitespace) but inline markup
                          (``**bold**``) preserved, matching ``browse-md``'s
                          tree-row titles. For a ``'text'`` node it is the run's
                          first non-blank line, ``.strip()``-ed (markup kept).
      * ``line_offset`` — 0-based line number of the node within the document.
      * ``byte_offset`` — character offset of the node's start in the text.
      * ``byte_size``   — character length of the node's section, per the
                          boundary rule (start of this node to the start of the
                          next sibling-or-shallower node, or EOF). Slicing
                          ``text[byte_offset : byte_offset + byte_size]`` yields
                          the node's full section including descendants. For a
                          ``'text'`` node the section is the body run itself —
                          from its first non-blank line to the start of the
                          heading it precedes.
      * ``children``    — nested ``MdNode``s (sub-headings; list children).
                          Always empty for a ``'text'`` node.
    """
    kind: str
    level: int
    title: str
    line_offset: int
    byte_offset: int
    byte_size: int
    children: list = field(default_factory=list)


# ### Section: Line / byte indexing ########################################

# Lifted verbatim from ``browse-md`` (``_line_starts`` / ``_line_of``): the
# offset→line conversion that ``build_doc_tree`` uses to stamp each node with a
# stable ``line_offset`` (callers select a heading by it).

def _line_starts(text):
    """Character offset of the start of each line in ``text``.

    Index ``i`` holds the offset of line ``i`` (0-based). Uses ``re.finditer``
    so the scan runs in the C-level regex engine rather than a Python loop.
    """
    return [0] + [m.end() for m in re.finditer(r'\n', text)]


def _line_of(byte_offset, line_starts):
    """0-based line number for ``byte_offset`` given precomputed line starts."""
    # ``bisect_right - 1`` lands on the line whose start is <= offset.
    return bisect.bisect_right(line_starts, byte_offset) - 1


# ### Section: Title stripping #############################################

# Drop the leading sigil from a row label — the kind/tag (``[h1]``/``[ul]``)
# already conveys it visually, so the title text shouldn't repeat the marker.
# Inline formatting markers (``**bold**`` etc.) are preserved: these only touch
# the prefix. Lifted from ``browse-md`` (``_HEADING_PREFIX_RE`` /
# ``_LIST_PREFIX_RE``).
_HEADING_PREFIX_RE = re.compile(r'^#+\s*')
_LIST_PREFIX_RE = re.compile(r'^([-*+]|\d+\.)\s+')

# Per-line list-item regex — lifted from ``md2ansi_lib`` (``_m2a_fmt_list``)
# and ``browse-md`` (``_LIST_ITEM_RE``). Used to fan one list span into one
# node per marker line and to read each item's indent level.
_LIST_ITEM_RE = re.compile(r'^([ \t]*)([-*+]|\d+\.)[ \t]+(.*)$')


# ### Section: Heading-tree building #######################################

# Internal event kinds. Headings carry their level in the kind (``'h3'``);
# list items are ``'ul'`` / ``'ol'``. These mirror ``browse-md``'s parser
# events so the lifted boundary/nesting passes read unchanged.
_HEADING_EVENT_KINDS = frozenset(('h1', 'h2', 'h3', 'h4', 'h5', 'h6'))

# Scan-kind sets requested from ``md2ansi_scan``. The full grammar runs
# internally either way (code / blockquote / table / frontmatter / hr stay
# masked, so a ``#`` inside a fenced block is NOT a heading); the only
# difference is whether ``'list'`` spans are yielded. So headings-only and
# with-lists agree byte-for-byte on the headings.
_SCAN_KINDS_HEADINGS = frozenset(('heading',))
_SCAN_KINDS_WITH_LISTS = frozenset(('heading', 'list'))


def _walk_list(base, text, line_starts):
    """Fan one ``md2ansi_scan`` list span into one event per marker line.

    ``md2ansi_scan`` reports a whole top-level list block as a single span,
    and guarantees every line of that span starts with a list marker. So we
    split on ``\\n`` and run the per-line regex with no further validity
    checks. ``base`` is the span's start offset (``span.start``); ``text`` is
    the span body. Each event's ``byte_offset`` is ``base`` plus the line's
    relative offset; ``line_offset`` is derived against the file-wide
    ``line_starts``. Lifted from ``browse-md`` (``_walk_list``).
    """
    events = []
    offset = 0
    for line in text.split('\n'):
        m = _LIST_ITEM_RE.match(line)
        if m is not None:
            indent = m.group(1)
            marker = m.group(2)
            byte_offset = base + offset
            kind = 'ol' if marker[-1] == '.' else 'ul'
            events.append((kind, {
                'byte_offset': byte_offset,
                'line_offset': _line_of(byte_offset, line_starts),
                'level': len(indent.expandtabs(4)) // 2,
                'source': line,
            }))
        offset += len(line) + 1
    return events


def _scan_events(text, line_starts, include_lists):
    """Flat ordered list of ``(kind, payload)`` parser events for ``text``.

    A thin adapter over ``md2ansi_scan`` — one structural-scan pass over the
    same combined grammar the renderer uses (single source of truth). Heading
    spans become ``('h1'..'h6', {...})`` events; a ``'list'`` span is fanned
    by ``_walk_list`` into per-marker-line ``('ul'|'ol', {...})`` events, and
    only requested when ``include_lists`` is set. Lifted from ``browse-md``'s
    ``_parse``.
    """
    kinds = _SCAN_KINDS_WITH_LISTS if include_lists else _SCAN_KINDS_HEADINGS
    events = []
    for span in md2ansi_scan(text, kinds=kinds):
        if span.kind == 'heading':
            byte_offset = span.start
            events.append((span.subtype, {     # subtype is 'h1'..'h6'
                'byte_offset': byte_offset,
                'line_offset': _line_of(byte_offset, line_starts),
                'source': span.text,
            }))
        else:
            # ``'list'`` — only present when include_lists is on.
            events.extend(_walk_list(span.start, span.text, line_starts))
    return events


def _is_blank_line(text, line_starts, line):
    """True if ``line`` (0-based) holds only whitespace (or is empty).

    The line body is ``text[line_starts[line] : line_starts[line + 1]]`` (a
    trailing line with no terminating ``\\n`` slices to EOF). A blank gap line
    contributes no body run, so it is skipped when hunting the first content
    line before a scope's opening heading.
    """
    start = line_starts[line]
    end = line_starts[line + 1] if line + 1 < len(line_starts) else len(text)
    return not text[start:end].strip()


def _add_text_nodes(scope_children, content_start_line, text, line_starts):
    """Synthesise leading ``'text'`` nodes into a built heading scope (in place).

    ``scope_children`` is a scope's child list — at call time still ALL heading
    nodes — and ``content_start_line`` is the scope's first body line (0 for the
    root scope, ``scope_heading.line_offset + 1`` for a heading scope). For each
    scope with >=1 heading child we look at the gap before the first heading:
    the FIRST non-blank line there becomes a dim ``'text'`` node inserted as the
    scope's new first child. We recurse into each heading child FIRST (so its
    own children list is still heading-only, and the just-inserted text node is
    never re-descended) and only then insert at index 0.
    """
    # Recurse into the real heading children before we mutate this list, so
    # each nested scope is processed with its heading-only children.
    for child in scope_children:
        _add_text_nodes(child.children, child.line_offset + 1, text, line_starts)
    if not scope_children:
        return
    first_child = scope_children[0]
    for line in range(content_start_line, first_child.line_offset):
        if _is_blank_line(text, line_starts, line):
            continue
        byte_offset = line_starts[line]
        # The title is just this first non-blank line (sigil-free already, since
        # it is body text); inline markup is kept, matching heading titles.
        line_body = text[byte_offset:line_starts[line + 1]]
        scope_children.insert(0, MdNode(
            kind='text',
            level=first_child.level,
            title=line_body.rstrip('\n').strip(),
            line_offset=line,
            byte_offset=byte_offset,
            byte_size=first_child.byte_offset - byte_offset,
        ))
        break  # only the first non-blank line of the gap


def build_doc_tree(text, *, include_lists=False):
    """Build the heading (and optionally list) structure of ``text``.

    Returns a list of top-level ``MdNode``s in document order, each with its
    nested children populated. There is NO synthetic file root — that wrapper
    belongs to each recipe's id/Item space, not to the shared structure. A
    document with no headings (and, when ``include_lists`` is off, regardless
    of its list content) yields ``[]``.

    ``include_lists=False`` (the default) scans headings only — the
    table-of-contents view ``browse-claude`` always wants. ``True`` adds
    list-item nodes (``browse-md``'s ``-l`` flag); the heading nodes are
    byte-for-byte identical either way because ``md2ansi_scan`` runs the full
    grammar internally and only filters at yield time.

    This is ``browse-md``'s ``_build_nodes`` generalised and lifted out, minus
    its ``Item`` construction and ``by_id``/``by_line`` indexing. Passes:

      1. Per-event ``MdNode`` construction (heading kinds → ``'heading'``
         nodes, ``ul``/``ol`` → ``'list-item'`` nodes), with the title sigil
         stripped. ``byte_size`` is filled by pass 2.
      2. Boundary rule (single linear pass): for node ``i``, find the next
         node ``j`` that is sibling-or-shallower and set ``byte_size =
         nodes[j].byte_offset - nodes[i].byte_offset`` (EOF → ``len(text)``).
         For a heading, ``j`` is the next heading at level ≤ L (lists never
         close a heading scope). For a list item, ``j`` is the next list item
         at indent ≤ L, OR any heading (a heading boundary always resets list
         scope).
      3. Tree linking via two stacks. The heading stack pops on level-≥
         comparisons (so an h3 attaches under the nearest open h1/h2). The
         list stack mirrors that on indent and resets on every heading
         boundary; an orphan list item (no shallower list ancestor) attaches
         to the surrounding heading, else becomes a top-level node.
      4. Leading body-text nodes (only when ``include_lists`` is off): for each
         scope with >=1 heading child, the body run before its first heading
         becomes a dim ``'text'`` node inserted as the scope's first child (see
         ``_add_text_nodes``). With lists on this pass is skipped, so the
         with-lists tree is byte-for-byte unchanged.
    """
    line_starts = _line_starts(text)
    events = _scan_events(text, line_starts, include_lists)
    bytes_eof = len(text)

    # --- Pass 1: per-event MdNode construction ---
    nodes = []  # parallel to events; same indexing
    for kind, payload in events:
        source = payload['source']
        # Drop leading ``#``s / list marker + immediate whitespace; the tag
        # conveys the kind, so the title shouldn't repeat the sigil. Inline
        # markup is preserved (the regex only touches the prefix).
        line = source.rstrip('\n').lstrip()
        if kind in _HEADING_EVENT_KINDS:
            level = int(kind[1])  # 'h3' -> 3
            node = MdNode(
                kind='heading',
                level=level,
                title=_HEADING_PREFIX_RE.sub('', line),
                line_offset=payload['line_offset'],
                byte_offset=payload['byte_offset'],
                byte_size=0,  # set by pass 2
            )
        else:
            node = MdNode(
                kind='list-item',
                level=payload['level'],
                title=_LIST_PREFIX_RE.sub('', line),
                line_offset=payload['line_offset'],
                byte_offset=payload['byte_offset'],
                byte_size=0,  # set by pass 2
            )
        nodes.append(node)

    # --- Pass 2: boundary rule (byte_size) ---
    n = len(nodes)
    for i in range(n):
        cur = nodes[i]
        cur_is_heading = cur.kind == 'heading'
        cur_level = cur.level
        j_bo = bytes_eof
        for j in range(i + 1, n):
            nxt = nodes[j]
            if cur_is_heading:
                if nxt.kind == 'heading' and nxt.level <= cur_level:
                    j_bo = nxt.byte_offset
                    break
            else:
                # list-item — any heading closes the list scope, plus any
                # list item at indent ≤ cur_level.
                if nxt.kind == 'heading':
                    j_bo = nxt.byte_offset
                    break
                if nxt.kind == 'list-item' and nxt.level <= cur_level:
                    j_bo = nxt.byte_offset
                    break
        cur.byte_size = j_bo - cur.byte_offset

    # --- Pass 3: tree linking via heading + list stacks ---
    roots = []  # top-level nodes, in document order
    # Heading stack: each entry is ``(level, node)``; top is the innermost
    # open heading, empty means "top level".
    heading_stack = []
    # List stack: each entry is ``(indent_level, node)``; reset on every
    # heading boundary so a section's list items can't nest under a prior
    # section's list.
    list_stack = []

    for node in nodes:
        if node.kind == 'heading':
            list_stack = []
            L = node.level
            while heading_stack and heading_stack[-1][0] >= L:
                heading_stack.pop()
            if heading_stack:
                heading_stack[-1][1].children.append(node)
            else:
                roots.append(node)
            heading_stack.append((L, node))
        else:
            L = node.level
            while list_stack and list_stack[-1][0] >= L:
                list_stack.pop()
            if list_stack:
                list_stack[-1][1].children.append(node)
            elif heading_stack:
                heading_stack[-1][1].children.append(node)
            else:
                roots.append(node)
            list_stack.append((L, node))

    # --- Pass 4: leading body-text nodes (headings-only view) ---
    # When lists are off, prepend a dim ``'text'`` node for the body run that
    # precedes each scope's first heading, so the table-of-contents shows the
    # intro paragraph. Gated on ``not include_lists`` so the with-lists tree is
    # byte-for-byte unchanged.
    if not include_lists:
        _add_text_nodes(roots, 0, text, line_starts)

    return roots


def node_at_line(tree, line_offset):
    """Find the ``MdNode`` at ``line_offset`` in a doc tree, or ``None``.

    Depth-first walk of an (already-built) ``MdNode`` tree — the list of
    top-level nodes from ``build_doc_tree`` and, recursively, each node's
    ``children``. Returns the first node whose ``line_offset`` *exactly*
    equals the target (so a heading selector is an O(nodes) lookup
    that needs no by-line index stashed on the structural nodes); a
    ``line_offset`` that matches no node — including one before the first
    node — yields ``None``. The tree is one document's structure, so a linear
    search is plenty.

    Lifted verbatim from the recipes' ``_md_node_at_line`` (both
    ``browse-claude`` and ``browse-md`` carried an identical copy).
    """
    for node in tree:
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
# heading but ``# foo`` is. Pure regex — NO ``md2ansi_scan`` — so it is fast
# enough to run on every item at delivery time. It is intentionally *optimistic*:
# it also fires on a ``#`` that lives only inside a fenced code block, which the
# authoritative ``build_doc_tree`` later rejects (the recipe self-heals the stale
# arrow).
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
    block also matches here (only ``build_doc_tree`` can tell them apart), so a
    ``True`` result means "worth building the real tree", not "definitely has a
    heading".
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
# ``abspath -> (text, doc_tree)``. The recipes clear this on their reload paths
# (``Ctrl-R`` / ``_bust_caches_for``) via ``clear_cache``. Headings-only is the
# cached shape — it is what both recipes' reference-following uses; a caller
# that needs lists (``browse-md``'s ``-l`` over its primary files) builds those
# trees itself rather than polluting this shared cache with a second variant.

_DOC_CACHE = {}


def get_doc(abspath):
    """Return ``(text, doc_tree)`` for ``abspath``, reading + parsing once.

    ``text`` is the file's full decoded contents; ``doc_tree`` is
    ``build_doc_tree(text)`` (headings only). Subsequent calls for the same
    path return the cached pair without re-reading or re-parsing. The path is
    used as the cache key verbatim — callers pass the canonical abspath from
    ``resolve_md_ref`` so two routes to one file share an entry.

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
    doc_tree = build_doc_tree(text)
    pair = (text, doc_tree)
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

# Env var carrying the stdin document for the shell-string ``launch`` form.
# Passing the document through the environment (rather than quoting it onto the
# command line) keeps it off ``argv`` — bounded by ARG_MAX (~2 MB), past which
# the launch fails through ``run_external``'s normal error path.
LAUNCH_STDIN_ENV = 'BROWSE_MD_STDIN'

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
        browser rather than expanding or editing the row.
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
            message). Piped to ``browse-md -`` on stdin via ``run_external``'s
            shell-string form, with the document handed through the
            ``BROWSE_MD_STDIN`` env var rather than quoted onto the command
            line: ``printf '%s' "$BROWSE_MD_STDIN" | browse-md - *flags --root
            …`` (roots shell-quoted). Env-var delivery is ARG_MAX-bounded
            (~2 MB); a pathological document fails the launch through
            ``run_external``'s normal ``ctx.error`` path. A stdin document's
            own reference-following is suppressed unless ``--root`` bases are
            supplied, so ``roots`` is precisely what lets its refs resolve.

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
        # ``content`` form: pipe via stdin, document carried in the environment.
        import shlex
        cmd = (f'printf \'%s\' "${LAUNCH_STDIN_ENV}" | browse-md - '
               + ' '.join(shlex.quote(a) for a in (*flags, *root_args)))
        ctx.run_external(cmd, {LAUNCH_STDIN_ENV: content or ''},
                         keep_screen=True)

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
