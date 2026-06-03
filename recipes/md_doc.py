#!/usr/bin/env python3

"""md_doc — shared, framework-agnostic markdown-structure logic.

This module is the single source of truth for turning markdown text into a
navigable *document structure* — a tree of heading (and optionally list-item)
nodes — plus the cross-document plumbing both ``browse-md`` and
``browse-claude`` need: reference detection/resolution between ``.md`` files,
the ``#md:`` id codec that chains documents into a recursive subtree, the
cheap heading-detection gate, and a process-wide parse cache.

It deliberately knows nothing about ``browse_tui`` / ``Item``: it returns plain
structural ``MdNode`` dataclasses, and each recipe maps those onto its own
``Item``/id space, tags, and styling. That keeps the markdown logic unit-testable
in isolation and decoupled from the TUI framework. ``browse-md`` and
``browse-claude`` import THIS module; it never imports them.

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
import urllib.parse
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

      * ``kind``        — ``'heading'`` or ``'list-item'``.
      * ``level``       — heading level (1..6) or list indent level (0-based).
      * ``title``       — the row label: source line with the leading sigil
                          stripped (``##``/``-``/``1.`` + following whitespace)
                          but inline markup (``**bold**``) preserved, matching
                          ``browse-md``'s tree-row titles.
      * ``line_offset`` — 0-based line number of the node within the document.
      * ``byte_offset`` — character offset of the node's start in the text.
      * ``byte_size``   — character length of the node's section, per the
                          boundary rule (start of this node to the start of the
                          next sibling-or-shallower node, or EOF). Slicing
                          ``text[byte_offset : byte_offset + byte_size]`` yields
                          the node's full section including descendants.
      * ``children``    — nested ``MdNode``s (sub-headings; list children).
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
# stable ``line_offset`` (the id codec selects a heading by it).

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
    its ``Item`` construction and ``by_id``/``by_line`` indexing. Three passes:

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

    return roots


def node_at_line(tree, line_offset):
    """Find the ``MdNode`` at ``line_offset`` in a doc tree, or ``None``.

    Depth-first walk of an (already-built) ``MdNode`` tree — the list of
    top-level nodes from ``build_doc_tree`` and, recursively, each node's
    ``children``. Returns the first node whose ``line_offset`` *exactly*
    equals the target (so the codec's heading selector is an O(nodes) lookup
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

# Cheap heading-detection gate: a ``#`` at the start of a line (after optional
# indent). Pure regex — NO ``md2ansi_scan`` — so it is fast enough to run on
# every item at delivery time. It is intentionally *optimistic*: it also fires
# on a ``#`` that lives only inside a fenced code block, which the authoritative
# ``build_doc_tree`` later rejects (the recipe self-heals the stale arrow).
_MD_HEADING_TRIGGER_RE = re.compile(r'(?:^|\n)[ \t]*#')


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


def resolve_md_ref(captured, *, doc_dir, cwd, project_root):
    """Resolve a captured ``.md`` token to an existing absolute path, or None.

    Tries candidate bases in order and returns the FIRST one that exists on
    disk (as a real path):

      1. absolute path, or a ``~``-prefixed path — taken as-is via
         ``expanduser`` (already rooted, so no base is joined);
      2. relative to ``doc_dir`` — the referencing document's own directory
         (the CommonMark norm; for an inline document ``doc_dir == cwd``);
      3. relative to ``cwd`` — the record's working directory;
      4. relative to ``project_root`` — the git/project root.

    Returns ``os.path.realpath`` of the first existing candidate (canonical, so
    callers can dedup by plain string compare), or ``None`` if none exists.
    De-duplication across a document's refs is the caller's job.
    """
    expanded = os.path.expanduser(captured)
    if os.path.isabs(expanded):
        # Absolute (or ``~``-expanded to absolute) — used directly, no base.
        candidates = [expanded]
    else:
        candidates = [
            os.path.join(doc_dir, captured),
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


# ### Section: #md: id codec ###############################################

# An id naming a markdown subtree appends a ``#md:`` selector onto the
# recipe's untouched base id (``<session_path>#<n>`` for browse-claude,
# ``<file_path>`` for browse-md), per the existing ``#keyword:`` convention so
# routing stays a substring test. Shapes:
#
#   inline document     <base>#md:
#   inline heading      <base>#md:#<lineoffset>
#   file document       <base>#md:<enc>
#   file heading        <base>#md:<enc>#<lineoffset>
#   nested file doc     <base>#md:<enc1>#md:<enc2>
#   nested file heading <base>#md:<enc1>#md:<enc2>#<lineoffset>
#
# where ``<enc>`` = ``quote('file://' + abspath, safe='')`` — ``safe=''``
# percent-encodes EVERY delimiter, notably ``#`` → ``%23``, so an encoded
# segment never contains a raw ``#``. That is what makes parsing unambiguous:
# the chain splits cleanly on ``#md:``, and a trailing ``#<digits>`` on the
# last segment can only be the line offset. The ``file://`` prefix is
# cosmetic-but-consistent flavour; resolution decodes the last segment back to
# the abspath directly, so no lookup map is ever needed.

_MD_SELECTOR = '#md:'


def compose_md_id(base, abspaths, line_offset=None):
    """Compose a ``#md:`` id from ``base``, a chain of file ``abspaths``, and an
    optional heading ``line_offset``.

    ``abspaths`` is the ordered chain of referenced files from the base
    document down to the document this id names; an EMPTY chain means the
    base's own inline document. Each abspath is encoded as
    ``quote('file://' + abspath, safe='')`` and the segments are joined under
    ``#md:`` markers. A non-``None`` ``line_offset`` (a heading's 0-based line
    in the LAST document of the chain) is appended as ``#<lineoffset>``.

    Examples (``base = 'sess#3'``)::

        compose_md_id('sess#3', [])                 -> 'sess#3#md:'
        compose_md_id('sess#3', [], 12)             -> 'sess#3#md:#12'
        compose_md_id('sess#3', ['/a/x.md'])        -> 'sess#3#md:file%3A...'
        compose_md_id('sess#3', ['/a/x.md'], 4)     -> 'sess#3#md:file%3A...#4'

    Inverse of ``parse_md_id`` — the round-trip is exact.
    """
    segs = [urllib.parse.quote('file://' + p, safe='') for p in abspaths]
    out = base + _MD_SELECTOR + _MD_SELECTOR.join(segs)
    if line_offset is not None:
        out += f'#{line_offset}'
    return out


def parse_md_id(item_id):
    """Decompose a ``#md:`` id into ``(base, abspaths, line_offset)``.

    Finds the FIRST ``#md:`` in ``item_id``: everything before it is the
    untouched ``base`` (still human-readable); everything after is the chain.
    The chain is split on ``#md:`` into encoded segments; because an encoded
    segment never contains a raw ``#`` (``safe=''`` turned it into ``%23``), a
    trailing ``#<digits>`` on the LAST segment can only be the line offset, so
    it is peeled off first. Each remaining segment is decoded back to its
    abspath (the cosmetic ``file://`` prefix is stripped). An empty chain
    (``<base>#md:``) yields ``abspaths == []`` (the inline document).

    Returns ``(base, abspaths, line_offset)`` where ``abspaths`` is a list of
    decoded absolute paths and ``line_offset`` is an ``int`` or ``None``.
    Raises ``ValueError`` if ``item_id`` contains no ``#md:`` (it is not a
    markdown id — callers gate on ``'#md:' in item_id`` first).

    Inverse of ``compose_md_id`` — the round-trip is exact.
    """
    marker = item_id.find(_MD_SELECTOR)
    if marker < 0:
        raise ValueError(f'parse_md_id: not a #md: id: {item_id!r}')
    base = item_id[:marker]
    chain = item_id[marker + len(_MD_SELECTOR):]

    # Peel a trailing ``#<digits>`` line offset off the whole chain. It can
    # only sit on the last segment, and encoded segments have no raw ``#``,
    # so a single rsplit on the final ``#`` is unambiguous.
    line_offset = None
    hash_pos = chain.rfind('#')
    if hash_pos >= 0 and chain[hash_pos + 1:].isdigit():
        line_offset = int(chain[hash_pos + 1:])
        chain = chain[:hash_pos]

    # Empty chain → inline document (no file segments). A non-empty chain
    # splits on the ``#md:`` marker into one encoded segment per file.
    if chain == '':
        abspaths = []
    else:
        abspaths = [
            urllib.parse.unquote(seg)[len('file://'):]
            for seg in chain.split(_MD_SELECTOR)
        ]
    return base, abspaths, line_offset


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
