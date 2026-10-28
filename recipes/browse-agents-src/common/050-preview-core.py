# ---- markdown document subtrees (md_doc integration) ---------------------
#
# When ``md_doc`` is importable, a message that contains markdown headings OR
# references existing ``.md`` files on disk becomes expandable into a document
# subtree: an inline ``markdown`` document node plus one node per referenced
# file, each drillable into its own heading tree, recursively. Detection is a
# cheap regex gate run at item-build time (optimistic — it may over-trigger);
# the authoritative tree is built lazily on expand in ``get_children`` and
# self-heals a stale arrow via ``_on_children_loaded``.
#
# A markdown node id is the recipe-owned tuple ``('md', anchor, chain, line)``
# (``('refs', anchor, chain)`` for the References umbrella), built/destructured
# inline here — ``md_doc`` is codec-free and only contributes single-document
# operations (``get_doc`` / ``node_at_line`` / ``visible_children`` /
# ``find_md_refs`` / ``resolve_md_ref``). ``anchor`` is the host message
# ``('msg', sess, n)``; ``chain`` is a TUPLE of referenced-file abspaths
# followed from the anchor (``()`` = the message's own inline doc); ``line`` is
# a heading ``line_offset`` (int) or ``None`` for the doc root. Routing is
# ``id[0] == 'md'`` / ``id[0] == 'refs'``. The whole section is a no-op when
# ``_md_doc is None``.


def _message_md_text(rec):
    """The record's markdown text: its body plus any tool-result output,
    joined with blank lines.

    This is the inline document's source — what ``md2ansi_doc`` /
    ``md_heading_trigger`` scan for headings. Collects ALL prose
    (``rec.text``) plus the output of each result on a ``tool_result``
    record; tool calls aren't the message's authored markdown. Returns
    ``''`` for a record with no text.
    """
    chunks = [rec.text] if rec.text else []
    if rec.kind == 'tool_result':
        chunks.extend(t['output'] for t in rec.tools if t['output'])
    return '\n\n'.join(chunks)


def _iter_str_leaves(obj):
    """Yield every ``str`` leaf reachable in a nested dict/list ``obj``.

    A short-circuit-friendly depth-first walk over the record's structure —
    the detection gate's cheap alternative to ``json.dumps(rec)``. Dict values
    and list elements are descended; keys are skipped (a JSON key is never a
    document path). The caller can stop pulling at the first match, so a record
    whose first text part already names a ``.md`` file never walks the rest.
    """
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_str_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_str_leaves(v)


def _record_has_md_ref(rec):
    """True iff any string leaf of the source object contains a ``.md``
    reference token.

    Short-circuiting: walks ``rec.raw``'s string leaves (``_iter_str_leaves``)
    and stops at the FIRST ``.md`` hit via ``md_doc.find_md_refs`` — far
    cheaper than serialising the whole record. No ``os.stat`` here (detection
    is optimistic; existence is checked lazily on expand, and a dead token
    self-heals). Sees tool paths and prose alike, whatever the agent's shape.
    """
    if _md_doc is None:
        return False
    for s in _iter_str_leaves(rec.raw):
        if _md_doc.find_md_refs(s):
            return True
    return False


def _md_has_children(rec):
    """Detection gate: should this message row carry an expansion arrow?

    ``True`` when ``md_doc`` is present AND the message either has a markdown
    heading (``md_heading_trigger`` over its extracted text) or references a
    ``.md`` file anywhere in the record (``_record_has_md_ref``). Pure regex,
    no disk I/O — intentionally *optimistic* (a ``#`` inside a code fence, or a
    ``.md`` token that doesn't resolve, both trigger here and self-heal on the
    first expand). A no-op (``False``) when ``_md_doc is None``.
    """
    if _md_doc is None:
        return False
    if _md_doc.md_heading_trigger(_message_md_text(rec)):
        return True
    return _record_has_md_ref(rec)


def _md_ref_label(abspath, cwd, project_root):
    """Display label for a referenced ``.md`` file, against a single anchor.

    Rendered relative to ONE common anchor so a flat sibling list reads cleanly
    without ``../`` noise: relative to ``project_root`` if the file is inside
    it, else relative to ``cwd`` if inside it, else a ``~``-collapsed absolute
    path. The label lives on the ``Item`` (never in the id — the id is
    abspath-canonical).
    """
    for anchor in (project_root, cwd):
        if anchor and (abspath == anchor or abspath.startswith(anchor.rstrip('/') + '/')):
            return os.path.relpath(abspath, anchor)
    return _collapse_home(abspath)


# Heading level (1..6) → row tag / colour style, matching browse-md's map
# (h1 loud red at the top, cycling down to a muted gray h6).
_MD_HEADING_STYLE = {
    1: 'red', 2: 'yellow', 3: 'green', 4: 'blue', 5: 'magenta', 6: 'gray',
}


def _md_heading_item(anchor, chain, node):
    """Map a heading ``M2A_Node`` to a tree-row ``Item``.

    Id = ``('md', anchor, chain, node.line_offset)`` — selecting the heading by
    its line within the chain's last document. ``has_children`` is
    authoritative (we already have the parsed node; counted over the VISIBLE
    children, since every heading carries a filtered-out ``#text`` first
    child), so heading rows never need self-heal. ``boundary`` is left
    ``False`` — a heading is same-document structure, not a foreign subtree.
    """
    item = Item(
        id=('md', anchor, chain, node.line_offset),
        title=node.title,
        tag=f'h{node.level}',
        tag_style=_MD_HEADING_STYLE.get(node.level, ''),
        has_children=bool(_md_doc.visible_children(node.children)),
    )
    item.kind = 'md-heading'
    item.md_level = node.level
    return item


def _md_text_item(anchor, chain, node):
    """Map a ``'text'`` ``M2A_Node`` to a dim-leaf tree-row ``Item``.

    The dim-leaf sibling of :func:`_md_heading_item`: a loose body run that
    precedes a scope's first heading. Id = ``('md', anchor, chain,
    node.line_offset)`` — selecting the run by its line within the chain's last
    document, exactly like a heading (the preview slices its own byte range).
    ``'text'`` nodes are always leaves (``node.children == []``), so
    ``has_children`` is ``False`` and the row never needs self-heal.
    """
    item = Item(
        id=('md', anchor, chain, node.line_offset),
        title=node.title,
        tag='text',
        tag_style='dim',
        has_children=False,
    )
    item.kind = 'md-text'
    return item


def _md_node_item(anchor, chain, node):
    """Map a visible child ``M2A_Node`` to its tree-row ``Item`` by kind.

    The single dispatch ``_md_subtree_children`` uses for both its heading-
    children and top-level-doc loops: a ``'text'`` run becomes a dim leaf
    (:func:`_md_text_item`), anything else (a heading) becomes a heading row
    (:func:`_md_heading_item`). The document model only holds ``'heading'``
    and ``'text'`` nodes, so no other arm is needed.
    """
    if node.kind == 'text':
        return _md_text_item(anchor, chain, node)
    return _md_heading_item(anchor, chain, node)


def _md_file_item(anchor, chain, label):
    """Map a referenced ``.md`` file to a document tree-row ``Item``.

    ``chain`` is the full tuple ending in this file (so the id nests under its
    referrer). ``has_children`` is optimistically ``True`` — we have not read
    the file yet; it self-heals on expand if the file has no headings and no
    further refs. ``boundary=True`` marks it a self-contained foreign subtree
    so recursive/multi-expansion stops at it.
    """
    item = Item(
        id=('md', anchor, chain, None),
        title=label,
        tag='md',
        tag_style='blue',          # cross-file / attachment hue (vs dim inline)
        has_children=True,         # optimistic; self-heals on expand
        boundary=True,
    )
    item.kind = 'md-doc'
    item.md_abspath = chain[-1]
    return item


def _md_resolved_refs(text, doc_dir, cwd, project_root):
    """Resolve the ``.md`` refs in ``text`` to existing abspaths.

    Returns a list of ``(abspath, label)`` pairs: each captured token resolved
    via ``md_doc.resolve_md_ref`` (existing-only — unresolved tokens dropped),
    deduped by abspath (first occurrence wins), and sorted by label so the flat
    sibling list reads stably. ``doc_dir`` is the referencing document's own
    directory (``cwd`` for the inline document, ``dirname(file)`` for a file).
    """
    seen = set()
    out = []
    for captured in _md_doc.find_md_refs(text):
        abspath = _md_doc.resolve_md_ref(
            captured, doc_dir=doc_dir, cwd=cwd, project_root=project_root)
        if abspath is None or abspath in seen:
            continue
        seen.add(abspath)
        out.append((abspath, _md_ref_label(abspath, cwd, project_root)))
    out.sort(key=lambda pair: pair[1])
    return out


def _has_any_existing_ref(text, doc_dir, cwd, project_root):
    """True iff ``text`` references at least one EXISTING ``.md`` file.

    The cheap detection half of :func:`_md_resolved_refs`: short-circuits at the
    FIRST captured ``.md`` token that ``md_doc.resolve_md_ref`` resolves to a
    real path, so a document-expand only has to decide *whether* to hang a
    References umbrella, not build the full (resolved/deduped/sorted) ref list —
    that full build is deferred to the umbrella's own expand. Mirrors
    browse-md's ``_file_has_existing_ref`` short-circuit over the same
    text/anchors ``_md_resolved_refs`` consumes.
    """
    for captured in _md_doc.find_md_refs(text):
        if _md_doc.resolve_md_ref(
                captured, doc_dir=doc_dir, cwd=cwd,
                project_root=project_root) is not None:
            return True
    return False


def _md_refs_umbrella_item(anchor, chain):
    """The synthetic ``[links]`` References umbrella for a document.

    Groups every ``.md`` file a document references under one tree parent —
    mirroring the ``<prompt>`` / ``<tool>`` umbrella idiom — instead of
    interleaving the ref rows as direct document children. The grouped document
    is identified by ``(anchor, chain)``: an empty ``chain`` is the message's
    own inline document; a non-empty ``chain`` is a referenced file-doc. The
    umbrella id is ``('refs', anchor, chain)``; the ref file-docs UNDER it keep
    their own ``('md', anchor, chain + (abspath,), None)`` ids — the umbrella is
    a grouping parent, not a new hop in the ref chain.

    ``boundary=False`` (same-document grouping, like the inline ``markdown``
    node — Alt-Right reveals the ref entries but each ref file-doc under it
    stays a boundary). ``has_children=True`` is authoritative: the caller only
    builds this when a ref actually exists (the ``_has_any_existing_ref``
    short-circuit), so the umbrella is never empty and never needs self-heal.
    """
    item = Item(
        id=('refs', anchor, chain),
        title='References',
        tag='links',
        tag_style='cyan',          # muted cross-file grouping hue
        has_children=True,         # caller built it only when a ref exists
        boundary=False,            # same-document grouping, not a foreign doc
    )
    item.kind = 'md-refs'
    return item


def _md_refs_umbrella_children(item_id):
    """Children of a ``[links]`` References umbrella — the full ref build.

    THIS is the lazy point: ``_md_message_children`` / ``_md_subtree_children``
    only short-circuit-detect a ref to decide whether to hang the umbrella;
    the full resolve (resolve all, dedup by abspath, sort by label) runs here,
    once, on the umbrella's own expand. ``item_id`` is ``('refs', anchor,
    chain)``; we recover the grouped document's text + resolution anchors, then
    build one ``[md]`` file-doc per existing ref — same ids (``('md', anchor,
    chain + (abspath,), None)``) and ``boundary=True`` the direct children
    carried before the umbrella was introduced, so expansion below the umbrella
    is unchanged.

    Two document shapes, mirroring the two callers:

      * **message base** (``chain == ()``) → scan the whole record's string
        leaves (prose + tool paths), anchored on ``store_session_cwd_and_root`` with
        ``doc_dir = cwd or project_root`` — the message-level rule. Refs chain
        directly off the message anchor.
      * **file-doc** (``chain`` non-empty) → the file's own text
        (``_md_chain_doc``), anchored on ``dirname`` of the file — the file-doc
        rule. Refs nest onto the existing ``chain``.

    Returns ``[]`` for an unreadable document (the umbrella is only built when a
    ref exists, so an empty result here means the doc became unreadable since).
    """
    _, anchor, chain = item_id
    sess = anchor[1]
    cwd, project_root = store_session_cwd_and_root(sess)
    if chain:
        # File-doc umbrella: resolve the file's own refs, nested onto its chain.
        text, doc = _md_chain_doc(anchor, chain)
        if doc is None:
            return []
        doc_dir = os.path.dirname(chain[-1])
        return [
            _md_file_item(anchor, chain + (ref_abspath,), label)
            for ref_abspath, label in _md_resolved_refs(
                text, doc_dir, cwd or '', project_root or '')
        ]
    # Message-level umbrella: resolve every existing ref across the record.
    rec = _record_at(sess, anchor[2])
    if rec is None:
        return []
    doc_dir = cwd or project_root
    record_text = '\n'.join(_iter_str_leaves(rec.raw))
    return [
        _md_file_item(anchor, (abspath,), label)
        for abspath, label in _md_resolved_refs(
            record_text, doc_dir, cwd or '', project_root or '')
    ]


def _md_message_children(anchor):
    """Children of a markdown message row ``anchor`` (``('msg', sess, n)``).

    The authoritative build (lazy, on expand): an inline ``markdown`` document
    node (only when the message's own text actually parses to ≥1 heading) plus a
    single ``[links]`` References umbrella WHEN the record references ≥1
    *existing* ``.md`` file ANYWHERE — prose or tool path. The umbrella groups
    those refs (the build of the full list is deferred to its own expand, in
    ``get_children``); here we only run the cheap ``_has_any_existing_ref``
    short-circuit to decide whether to hang it. A ref that appears in the inline
    text surfaces under the umbrella, not under the ``markdown`` node (which
    stays a pure heading index). Children are thus
    ``[markdown (if headings), References (if any existing ref)]``; ``[]`` when
    neither holds (the optimistic detection arrow then self-heals).
    """
    path, n = anchor[1], anchor[2]
    rec = _record_at(path, n)
    if rec is None:
        return []

    out = []
    # Inline document node — only when the message's own text has a heading
    # (the visible row set is non-empty iff ≥1 heading exists: a text run
    # only qualifies as a row alongside a heading sibling).
    text = _message_md_text(rec)
    if _md_doc.visible_children(_md2ansi_doc(text).tree):
        inline = Item(
            id=('md', anchor, (), None),
            title='markdown',
            tag='md',
            tag_style='dim',       # muted same-file index (vs blue file docs)
            has_children=True,     # confirmed ≥1 heading
            boundary=False,        # inline content is same-file, not foreign
        )
        inline.kind = 'md-doc'
        out.append(inline)

    # References umbrella — one node grouping every existing ``.md`` ref in the
    # whole record. The full resolve happens lazily when the umbrella expands
    # (its ``('refs', …)`` id routes to ``_md_refs_umbrella_children`` in
    # ``get_children``); here we only short-circuit on the FIRST existing ref.
    # The same concatenated string leaves (prose + tool paths) feed both halves.
    cwd, project_root = store_session_cwd_and_root(path)
    doc_dir = cwd or project_root
    record_text = '\n'.join(_iter_str_leaves(rec.raw))
    if _has_any_existing_ref(record_text, doc_dir, cwd or '', project_root or ''):
        out.append(_md_refs_umbrella_item(anchor, ()))
    return out


def _md_chain_doc(anchor, chain):
    """Resolve a markdown ``chain``'s *last* document to ``(text, doc)``.

    ``doc`` is the ``M2A_Doc`` document model. The chain's terminal document
    is what both the subtree build and the preview route operate on: a
    non-empty ``chain`` ends in a referenced file (read + parsed once via
    ``md_doc.get_doc``'s process-wide cache); an empty chain is the message's
    own inline content (``_message_md_text`` of the ``anchor`` record, parsed
    with ``md2ansi_doc``). Returns ``(None, None)`` when the document is
    unreadable — the callers treat that as "no children / empty preview".
    """
    if chain:
        try:
            return _md_doc.get_doc(chain[-1])
        except OSError:
            return None, None
    rec = _record_at(anchor[1], anchor[2])
    if rec is None:
        return None, None
    text = _message_md_text(rec)
    return text, _md2ansi_doc(text)


def _md_subtree_children(item_id):
    """Children for a ``('md', anchor, chain, line)`` node — the lazy build.

    Routes on the destructured id fields:

      * **heading** (``line`` set) → the sub-headings nested under it (walk the
        chain's last document tree to the node at ``line``, return its
        children). No reference children — refs live at the document level, not
        under a heading. A ``line`` naming a dim ``'text'`` run resolves here
        too, but a text node is a leaf (``children == []``) so it yields ``[]``.
      * **inline document** (``chain == ()``, ``line`` None) → the top-level
        rows of the message's inline text (heading rows, plus a dim ``[text]``
        leaf for a leading body run). Its refs already surface at the message
        level above, so none are added here.
      * **file document** (``chain`` non-empty, ``line`` None) → the top-level
        rows of that file (headings + any leading text) PLUS a single
        ``[links]`` References umbrella when THAT file references ≥1 existing
        ``.md`` (resolved against the file's own directory). The umbrella groups
        the nested refs; building the full ref list is deferred to its own
        expand, so here we only run the ``_has_any_existing_ref`` short-circuit.
        Recursion (file → file) falls out of this uniform rule once the
        umbrella expands; expand-in-place, no cycle/alias detection.

    File text + tree come from ``md_doc.get_doc`` (process-wide cache). Returns
    ``[]`` for an unreadable/heading-less document (self-heals the arrow).
    """
    _, anchor, chain, line = item_id

    text, doc = _md_chain_doc(anchor, chain)
    if doc is None:
        return []

    # Heading node → its nested sub-headings (no ref children).
    if line is not None:
        node = _md_doc.node_at_line(doc.tree, line)
        if node is None:
            return []
        return [_md_node_item(anchor, chain, ch)
                for ch in _md_doc.visible_children(node.children)]

    # Document node → top-level rows (headings + any leading text run), then
    # (file docs only) a References umbrella grouping the file's own refs. The
    # full resolve is deferred to the umbrella's expand; here we just
    # short-circuit on the first one.
    out = [_md_node_item(anchor, chain, node)
           for node in _md_doc.visible_children(doc.tree)]
    if chain:
        cwd, project_root = store_session_cwd_and_root(anchor[1])
        doc_dir = os.path.dirname(chain[-1])
        if _has_any_existing_ref(text, doc_dir, cwd or '', project_root or ''):
            out.append(_md_refs_umbrella_item(anchor, chain))
    return out


def _preview_md_node(item_id):
    """Preview body for a ``('md', anchor, chain, line)`` document/heading node.

    Reads ONLY the node's own document — never a cross-item cascade:

      * **document node** (``line`` None) → that document's full text: the
        message's inline content (empty chain) or the referenced file's text
        (``md_doc.get_doc`` cache).
      * **heading node** (``line`` set) → just that heading's section, sliced
        ``text[byte_offset : byte_offset + byte_size]`` from the cached
        ``M2A_Doc`` (the boundary rule includes the heading's own sub-sections
        and stops at the next sibling-or-shallower heading).

    Both are rendered through ``_md_voice`` — md2ansi when the ``_MD_COLOR``
    toggle is on, raw text otherwise — which already falls back to the raw
    string if md2ansi raises. Returns ``''`` for an unreadable document or a
    line offset that matches no heading.
    """
    _, anchor, chain, line = item_id
    text, doc = _md_chain_doc(anchor, chain)
    if text is None:
        return ''
    if line is None:
        return _md_voice(text)
    node = _md_doc.node_at_line(doc.tree, line)
    if node is None:
        return ''
    return _md_voice(text[node.byte_offset:node.byte_offset + node.byte_size])


def _preview_md_refs_umbrella(item_id):
    """Preview body for a ``[links]`` References umbrella.

    A PLAIN, non-markdown listing of the grouped refs' labels — one per line
    under a short count header — NEVER routed through ``_md_voice`` / md2ansi
    (the labels are file paths, not markdown to colorise). Reuses the umbrella's
    own ``get_children`` build so the labels match the rows exactly. Returns
    ``''`` when the document is unreadable / has no resolvable ref left.
    """
    kids = _md_refs_umbrella_children(item_id)
    if not kids:
        return ''
    header = f'{len(kids)} referenced file' + ('s' if len(kids) != 1 else '')
    return '\n'.join([header, ''] + [k.title for k in kids])


def _rule(title='', color=None, *, width=None, bg=None):
    """A '── title ──' separator line. Default colour is MUTE.

    Pass ``color=YELLOW`` (or any palette constant) to make a rule
    pop — used by the tree-mode preview cascade to clearly mark the
    boundary between the cursor row's content and each of its
    appended children.

    ``width`` overrides the fixed 60-column bar (callers pass the live
    ``_BROWSER.preview_width`` for a rule that spans the pane). ``bg``
    is a background SGR painted behind the WHOLE bar — combined with a
    full ``width`` it reads as a solid band. With neither set the output
    is byte-identical to the historical fixed-width, fg-only rule.
    """
    w = 60 if width is None else width
    bar = '─' * max(0, w - len(title) - 4)
    body = f'── {title} {bar}' if title else '─' * w
    return f'{bg or ""}{color or MUTE}{body}{RESET}'


def _band_block(text, bg):
    """Paint a continuous background ``bg`` behind a multi-line block.

    The preview pane re-emits SGR across the wrapped sub-rows of one
    logical line but drops it at every ``\n`` (each logical line wraps
    with a fresh SGR state), so a single leading ``bg`` only colors the
    first line. Re-assert ``bg`` at the start of every logical line, and
    after any hard reset within a line (chrome ``\x1b[0m`` would
    otherwise punch a hole), then close with one final reset. Empty
    ``bg`` (NO_COLOR) is a passthrough. Pair with ``current_style=<bg
    params>`` on the colorer so md2ansi's own span resets stay on the
    band too.
    """
    if not bg:
        return text
    return '\n'.join(
        bg + line.replace(RESET, RESET + bg) for line in text.split('\n')
    ) + RESET


# Dark-grey background for the Bash ``$ command`` line so it reads as a
# distinct prompt against the command's output below it. The SGR params
# are kept separate so ``_bash_color`` can pass them as md2ansi's
# ``current_style`` — token resets then land back on the grey band
# instead of a hard ``\x1b[0m`` that would punch a hole in it.
CMD_BG_PARAMS = '48;5;236'
CMD_BG = f'\x1b[{CMD_BG_PARAMS}m'

HAVE_DELTA = shutil.which('delta')


def _colorize_diff(raw):
    """Render a unified-diff string for the preview pane via ``delta``.

    Ported from browse-git. ``raw`` is plain (or git-colored) unified-diff
    text. When ``delta`` is on PATH (``HAVE_DELTA``) it's piped through for
    terminal-native rendering; ``delta`` re-emits its own SGR. When ``delta``
    is absent — or exits non-zero — ``raw`` is returned unchanged, so the
    caller always gets back displayable text and never generates the diff
    twice.

    ``--no-gitconfig`` ignores the user's theme and the OSC-8 hyperlinks the
    preview pane can't render; ``--paging never`` stops delta spawning a
    pager off-tty; ``--width`` matches the pane (read from the module-level
    ``_BROWSER`` since ``get_preview`` gets no ``ctx``). At a wide pane
    (``width >= 160``) delta lays the diff out side-by-side, splitting that
    one ``--width`` into two columns (``--line-fill-method=spaces`` pads each
    side so the gutter aligns).
    """
    if not HAVE_DELTA:
        return raw
    width = (_BROWSER.preview_width if _BROWSER else 0) or 80
    argv = ['delta', '--no-gitconfig', '--paging', 'never',
            '--width', str(width)]
    if width >= 160:
        # Wide pane: two columns out of the one --width above.
        argv += ['--side-by-side', '--line-fill-method=spaces']
    try:
        proc = subprocess.run(
            argv, input=raw, capture_output=True, text=True, check=False)
    except Exception:
        return raw
    if proc.returncode == 0:
        return proc.stdout
    # delta failed unexpectedly — fall back to the raw diff text.
    return raw


def _json_color(text):
    """Syntax-color a JSON string FRAME-LESS (no ``--- json`` chrome).

    Routes through md2ansi_lib's JavaScript code context — its object /
    string / number lexer matches JSON closely enough. Falls back to a flat
    ``GRAY`` wrap on any failure or when md2ansi_lib wasn't importable.
    """
    if _M2A_JSON is None:
        return f'{GRAY}{text}{RESET}'
    try:
        return _M2A_JSON(text, "0", _M2A_JS, _M2A_DocumentState())
    except Exception:
        return f'{GRAY}{text}{RESET}'


# Bounds for the nested JSON-in-string decode: how many decoded
# sections a preview may append, and how many decode levels deep the
# walk follows (a decoded value can itself contain JSON strings —
# Jira-style results nest twice).
_NESTED_JSON_MAX = 8
_NESTED_JSON_DEPTH = 3


def _nested_json_strings(value, path='', out=None, depth=0):
    """Collect ``(path, parsed)`` for string leaves that parse as JSON.

    Walks dicts/lists; a string leaf that parses to a NON-EMPTY dict or
    list is recorded under its dotted path (``.result``, ``.a[0].b``)
    and the walk recurses into the decoded value at ``depth + 1`` so
    doubly-encoded payloads unwrap too. Bounded by ``_NESTED_JSON_MAX``
    sections and ``_NESTED_JSON_DEPTH`` decode levels. Container
    descent recurses one frame per structural level unbounded by
    ``depth`` — safe because ``json.loads`` rejects deeper nesting than
    Python's recursion limit before this walker ever sees it.
    """
    if out is None:
        out = []
    if depth > _NESTED_JSON_DEPTH or len(out) >= _NESTED_JSON_MAX:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            _nested_json_strings(v, f'{path}.{k}', out, depth)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _nested_json_strings(v, f'{path}[{i}]', out, depth)
    elif isinstance(value, str) and re.match(r'^\s*[\[{]', value):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return out
        if isinstance(parsed, (dict, list)) and parsed:
            out.append((path, parsed))
            _nested_json_strings(parsed, path, out, depth + 1)
    return out


def _maybe_json_color(text):
    """Pretty-print + color ``text`` when it parses as JSON, else ``None``.

    Cheap prefilter (``re.match`` on the first non-space char) before the
    full ``json.loads`` so non-JSON text bails immediately. On success the
    parsed value is re-dumped with a 2-space indent and run through
    ``_json_color``; otherwise the caller keeps ``text`` as-is.

    String fields that THEMSELVES parse as JSON (tools like Jira return
    JSON-encoded strings inside JSON) are appended decoded after the main
    block, each under a small ``<path> =`` header.
    """
    if not isinstance(text, str) or not re.match(r'^\s*[\[{]', text):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return _json_color_value(parsed)


def _json_color_value(parsed):
    """Colored pretty-print of an already-parsed JSON value, nested
    JSON-strings appended decoded — the shared tail of
    ``_maybe_json_color``, also used for dict-shaped ``toolUseResult``
    payloads that skip the string round-trip."""
    out = [_json_color(json.dumps(parsed, indent=2, ensure_ascii=False))]
    for path, sub in _nested_json_strings(parsed):
        out.append(f'\n{MUTE}{path} ={RESET}')
        out.append(_json_color(json.dumps(sub, indent=2,
                                          ensure_ascii=False)))
    return '\n'.join(out)


def _bash_color(cmd):
    """Syntax-color a shell command FRAME-LESS, on the grey ``CMD_BG`` band.

    Routes through md2ansi_lib's bash code context. ``current_style`` is
    ``CMD_BG_PARAMS`` so each token's reset returns to the grey band
    rather than a hard ``\\x1b[0m`` — the band stays unbroken across the
    whole (possibly multi-line) command. Falls back to a flat ``WHITE``
    wrap on any failure or when md2ansi_lib's bash context wasn't
    importable.
    """
    if _M2A_BASH is None or not callable(_m2a_render):
        return f'{WHITE}{cmd}{RESET}'
    try:
        return _m2a_render(cmd, CMD_BG_PARAMS, _M2A_BASH, _M2A_DocumentState())
    except Exception:
        return f'{WHITE}{cmd}{RESET}'


# Lightweight markdown sniff: a leading heading / bullet / fence / blockquote
# on any line, or an inline ``[text](url)`` link. Used to decide whether a
# fetched body reads better through ``_md_voice`` than as plain text.
_MD_HINT_RE = re.compile(
    r'(?m)^(#{1,6}\s|[*\-+]\s|>\s|```|\d+\.\s)|\[[^\]]+\]\([^)]+\)')


def _looks_markdown(text):
    return isinstance(text, str) and bool(_MD_HINT_RE.search(text))


# ---- get_preview ---------------------------------------------------------


def _record_at(sess, n):
    """The record with key ``n`` in session ``sess``, or ``None``.

    Served from the cached ``_TreeData`` (read on first use), so a
    preview is an O(1) lookup. ``None`` for an unknown session, a key
    out of range and placeholder records.
    """
    if sess not in _TREE_CACHE and not store_session_exists(sess):
        return None
    td = _scan_tree(sess)
    if 0 <= n < len(td.records):
        return td.records[n]
    return None


def get_preview(item_id):
    """Preview-pane content for the cursor item.

    * ``('project', dir)``     : human-readable summary + session list
    * ``('session', sess)``    : concatenated child previews (same machinery
                                  as the umbrella previews — uniform body
                                  cascade, honours the detail filter
                                  transparently via ``hidden`` on children).
    * ``('agent', sess, aid)`` : same uniform cascade against the subagent's
                                  own transcript.
    * ``('prompt'|'tool'|'span', …)`` umbrella: concatenated bodies of the
                                  umbrella's direct children.
    * ``('msg', sess, n)``     : the rendered record.
    * ``('md'|'refs', …)``     : its own document / label-list preview.
    """
    if not isinstance(item_id, tuple) or not item_id:
        return ''
    tag = item_id[0]
    if tag == 'refs':
        # ``[links]`` References umbrella — a plain label list, not a document.
        return _preview_md_refs_umbrella(item_id) if _md_doc is not None else ''
    if tag == 'md':
        # Markdown document/heading node → its own document preview (inline OR
        # file, regardless of its boundary flag). A no-op when ``_md_doc is
        # None`` (no md id can exist then, but the guard keeps it self-contained).
        return _preview_md_node(item_id) if _md_doc is not None else ''
    if tag == 'err':
        return ''
    if tag == 'trunc':
        return (
            f'(only the first {_message_limit} messages are listed)\n'
            f'press e on this row to open the full file in $EDITOR'
        )
    # Cross-file rows (subagent group, bare session): heavy umbrella
    # cascade only when committed (scope_root or expanded inline);
    # cheap metadata card otherwise so scrolling stays cheap.
    if _is_cross_file_id(item_id):
        if _item_is_active(item_id):
            return _preview_umbrella(item_id)
        sess = _scope_card_session(item_id)
        return _preview_session_card(sess) if sess else ''
    if tag in ('prompt', 'tool', 'span'):
        return _preview_umbrella(item_id)
    if tag == 'msg':
        return _preview_message(item_id[1], item_id[2])
    if tag == 'project':
        return store_project_preview(item_id[1])
    return ''


def _item_is_active(item_id):
    """True iff the cursor row should render its heavy cascade preview.

    Heavy = full umbrella cascade + scope card. Cheap = metadata-only
    card (path / sessionId / agentType / etc.). The rule is uniform
    across cross-file row types (``('session', …)`` rows and
    ``('agent', …)`` subagent umbrellas): the user has committed iff the
    row is the current scope_root or is currently expanded inline.

    ``_BROWSER is None`` (tests, headless mode) defaults to heavy so
    existing behaviour is preserved.
    """
    if _BROWSER is None:
        return True
    state = _BROWSER._state
    if state.scope_stack and state.scope_stack[-1] == item_id:
        return True
    return item_id in state.expanded


def _preview_session_card(sess):
    """Cheap metadata-only preview for a cross-file row.

    Built from ``store_session_info`` (first record + one stat; never a
    full read), so scrolling through a project of large transcripts
    (or a session with many subagents) does not block on parsing.
    """
    info = store_session_info(sess)
    rows = [('path', info['path'])] + list(info['meta'])
    if info['mtime'] is not None:
        rows.append(('mtime', _human_time(info['mtime'])))
    w = max(len(k) for k, _ in rows)
    body = '\n'.join(
        f'   {MUTE}{k.ljust(w)}:{RESET} {v}' for k, v in rows
    )
    hint = (f'\n\n{MUTE}(expand or scope in to see the transcript){RESET}')
    label = _prog_name() + (' (subagent)' if info['is_child'] else '')
    return f'{_rule(label, BLUE)}\n{body}\n{_rule()}{hint}'


def _preview_message(sess, n):
    """Per-line preview for a leaf message: classify + dispatch + chrome.

    Each renderer returns a body str with ANSI inlined. The chrome
    footer (uuid, cwd, timestamp, …) is uniform and appended here. In
    the new umbrella tree model leaves don't have children, so the
    preview no longer concatenates anything extra — the umbrella's own
    preview is where multi-child concatenation lives.
    """
    rec = _record_at(sess, n)
    if rec is None:
        return ''
    obj = rec.raw
    kind = _classify(obj)
    body = _RENDERERS.get(kind, _render_unknown)(obj)
    chrome = _fmt_chrome(obj, _turn_root_ts(sess, n))
    return body + ('\n' + chrome if chrome else '')


def _scope_card_session(item_id):
    """The session whose scope card should prepend the preview, or None.

    Two id kinds earn the card: a ``('session', sess)`` row (top row in
    the project's session list, or when a transcript is opened
    directly) and an ``('agent', sess, aid)`` row whose preview is
    composed from the subagent's own transcript.
    """
    if not isinstance(item_id, tuple) or not item_id:
        return None
    if item_id[0] == 'agent':
        return store_child_session(item_id[1], item_id[2])
    if item_id[0] == 'session':
        sess = item_id[1]
        return sess if store_session_exists(sess) else None
    return None


def _fmt_scope_card(sess, td):
    """One-block header identifying the session backing the synthetic top row.

    Store facts (``store_session_info``: path, child metadata, the
    session stamps) plus what the scan knows — the session name
    captured during the fold (custom-title outranks ai-title), the
    record and voice counts. Returns a string ready to prepend to the
    cascade body.
    """
    info = store_session_info(sess)
    rows = [('path', info['path'])]
    name = _clean_title(td.custom_title or td.ai_title)
    if name:
        rows.append(('name', name))
    rows.extend(info['meta'])
    n_lines = sum(1 for r in td.records if r is not None)
    rows.append(('lines', str(n_lines)))
    if td.voice_count is None:
        td.voice_count = sum(
            1 for r in td.records if r is not None and r.is_voice
        )
    rows.append(('voice', str(td.voice_count)))
    if info['mtime'] is not None:
        rows.append(('mtime', _human_time(info['mtime'])))
    label = _prog_name() + (' (subagent)' if info['is_child'] else '')
    w = max(len(k) for k, _ in rows)
    body = '\n'.join(
        f'   {MUTE}{k.ljust(w)}:{RESET} {v}' for k, v in rows
    )
    return f'{_rule(label, BLUE)}\n{body}\n{_rule()}'


# Records per side-effect flush from the streaming umbrella generator.
# The framework's preview worker drains ``_preview_umbrella`` one chunk
# at a time; rather than flushing eager-push upserts and leaf-preview
# writes after every chunk (one ``update_data`` per record — pipe-wake
# storm) or only at the end (a mid-stream cursor-move abandon would
# lose all of them), we batch in small windows. 25 is small enough
# that an abandoned umbrella still leaves most visited leaves cached;
# large enough that the post queue stays quiet.
STREAM_BATCH = 25


def _preview_umbrella(item_id):
    """Generator — yields rendered chunks for an umbrella row's descendants.

    First yield (when applicable) is the scope card for the umbrella's
    session; subsequent yields are ``<rendered_body>\\n\\n`` per leaf in
    document order, with nested same-file umbrellas folded in
    transparently. Subagent-group children are skipped (the dispatch
    pair already renders in the parent file).

    The framework's streaming preview worker drains each chunk into the
    ``Item.preview`` cache via ``append_preview``, pauses at the
    screen-derived cap, resumes on demand or when the tail-pin is
    engaged, and clears the cache on a cursor-move abandon
    (``GeneratorExit``).

    Side effects (preserved from #424 / #446 — see
    ``_collect_umbrella_preview`` for the batching helper):
      * Children discovered during the descent that aren't yet in the
        framework's cache are eager-pushed via batched
        ``_BROWSER.update_data(ops)`` calls (one per STREAM_BATCH
        records, plus a ``finally:`` flush on exit).
      * Each leaf record's rendered body is cached on its Item via a
        ``set_preview_op(cid, text)`` folded into the same batch.

    Per-leaf renderer errors surface as a red error stub so one bad
    row doesn't blank the entire preview.
    """
    card_sess = _scope_card_session(item_id)
    if card_sess is not None:
        try:
            td = _scan_tree(card_sess)
            yield _fmt_scope_card(card_sess, td) + '\n\n'
        except Exception as e:
            yield _rule(f'error in scope card: {e}', RED) + '\n\n'
    if item_id[0] == 'tool':
        # Tool umbrellas with fully-registered parts render one fused
        # request+result block instead of the raw leaf cascade. Any gap
        # (unregistered tool, in-flight part, declining formatter) returns
        # None and we fall through to the cascade below.
        custom = _render_tool_umbrella(item_id)
        if custom is not None:
            yield custom
            return
    yield from _collect_umbrella_preview(item_id)


def _collect_umbrella_preview(item_id, _state=None):
    """Generator — yields ``<rendered_body>\\n\\n`` per leaf in document order.

    Recurses through same-file ``<prompt>`` / ``<tool>`` / ``<span>``
    umbrellas so a turn-root preview includes every underlying line.
    Stops at subagent-group boundaries.

    Side-effect ops (eager-push upserts + ``set_preview_op`` writes
    for visited leaves) accumulate in ``_state`` — passed through the
    recursion so a single top-level helper handles flushing. Every
    ``STREAM_BATCH`` records the helper posts the buffered ops via
    ``_BROWSER.update_data``; a ``finally:`` block on the top-level
    call flushes any remainder on ``StopIteration`` (clean drain) or
    ``GeneratorExit`` (cursor-move abandon).

    ``_state`` is the top-level call's flush state — ``None`` means
    "this is the top frame, create it and own the ``finally:``
    flush". Recursive calls thread the same state through so all
    descendants share one batching window.
    """
    top_level = _state is None
    if top_level:
        _state = {
            'ops': [],
            'count': 0,
        }

    def _flush(state, force=False):
        if not (force or state['count'] >= STREAM_BATCH):
            return
        if _BROWSER is None:
            state['ops'].clear()
            state['count'] = 0
            return
        ops = state['ops']
        if ops:
            # Re-evaluate ``hidden`` against the live filter for every
            # upsert at flush time, not at descent time. Otherwise a
            # filter toggle that lands mid-stream (e.g., user presses
            # ``h`` while the umbrella is paused at the cap) would have
            # its mod-ops clobbered by the abandoned generator's
            # ``finally:`` flush, which carries upserts whose hidden
            # flags reflect the filter state at descent. The bug
            # manifested as the first ``h`` press not re-filtering the
            # umbrella preview — the OLD items' stale hidden flags
            # were restored by the finally-flush.
            ops = [_refresh_hidden_in_op(op) for op in ops]
            _BROWSER.update_data(ops)
        state['ops'] = []
        state['count'] = 0

    try:
        yield from _walk_umbrella(item_id, _state, _flush)
    finally:
        if top_level:
            _flush(_state, force=True)


def _walk_umbrella(item_id, _state, _flush):
    """Inner generator — descent body shared by top-level + recursive calls.

    Kept separate from ``_collect_umbrella_preview`` so the ``finally:``
    on the top frame fires after the whole descent exhausts (or the
    consumer closes us), not on every recursive ``yield from``.
    """
    # Read-path: prefer the framework's cached children if available.
    # ``cached_children`` returns None when the parent hasn't been
    # fetched yet (or when we're running outside a real Browser, e.g.
    # in unit tests). A non-None list — even empty — is authoritative.
    children = None
    cached = None
    if _BROWSER is not None:
        try:
            cached = _BROWSER.cached_children(item_id)
        except Exception:
            cached = None
    if cached is not None:
        children = cached
    else:
        try:
            children = get_children(item_id)
        except Exception as e:
            yield (_rule(f'error listing {item_id}', RED)
                   + f'\n{type(e).__name__}: {e}\n\n')
            return
        # Stage upserts so subsequent recursion / expansion sees the
        # children without re-running ``get_children``. The framework's
        # ``upsert`` op is a no-op if the child id is already present
        # with matching parent + fields, but checking via
        # ``items_by_id`` first lets us skip the work entirely.
        if _BROWSER is not None:
            try:
                index = _BROWSER.items_by_id
            except Exception:
                index = None
            for child in children:
                cid = getattr(child, 'id', None)
                if not isinstance(cid, tuple):
                    continue
                if index is not None and cid in index:
                    continue
                _state['ops'].append(upsert(
                    cid, item_id, **_item_to_upsert_kwargs(child),
                ))
    for child in children:
        cid = getattr(child, 'id', None)
        if not isinstance(cid, tuple):
            continue
        # Honour the recipe's detail filter: a hidden child (e.g.,
        # a ``tool`` umbrella below the current level, or a tool_result
        # leaf) is invisible in the list and shouldn't bleed into the
        # parent's preview either.
        if getattr(child, 'hidden', False):
            continue
        # Boundary child — a self-contained foreign subtree (subagent
        # group, referenced .md file): never drill into another file's
        # content from a parent's preview cascade. For a subagent the
        # dispatching Agent / Task tool_use + matching tool_result
        # already live in the parent file and render through the normal
        # leaf path under the dispatching turn, so we don't even emit a
        # stub — would just duplicate that pair. (md docs are never in
        # this cascade, so this only fires for the ``agent`` rows it
        # used to match by id shape.)
        if getattr(child, 'boundary', False):
            continue
        try:
            # One rule for every child: emit its own composed unit if it
            # has one (a leaf's body + rule, or a tool umbrella's fused
            # request+result block + its ``─── tool: <Name> ───`` rule),
            # else recurse so a same-file ``prompt`` / ``span`` (or a
            # tool whose formatter declined) still contributes every
            # underlying line. ``_node_own_body`` owns all type knowledge
            # and the compose-time rule; the walker stays tool-agnostic.
            #
            # Composed units are rendered fresh and body-only: we
            # deliberately do NOT consult ``get_cached_preview(cid)`` nor
            # write the rendered body back into a child's ``Item.preview``
            # via set_preview_op. A direct cursor-visit caches body +
            # chrome there; the cascade wants body-only (chrome between
            # blocks is noise), so sharing one slot would bleed chrome
            # in (or strip it out) depending on visit order. Record read +
            # render is cheap enough to redo per compose.
            unit = _node_own_body(cid)
            if unit is not None:
                yield unit + '\n\n'
                _state['count'] += 1
                _flush(_state)
            else:
                # Pass the shared flush state so the whole descent stays
                # in one batching window.
                yield from _collect_umbrella_preview(cid, _state)
        except Exception as e:
            yield (_rule(f'error rendering child {cid}', RED)
                   + f'\n{type(e).__name__}: {e}\n\n')


def _render_record_with_rule(sess, n):
    """Read one record, render its body, prefix with a section rule.

    Most leaves get the fixed ~60-wide bright-yellow rule. A
    turn-opening user prompt gets a FULL-WIDTH dark-blue band for its
    divider (bold-white title) AND has its whole prompt painted on the
    same dark-blue background — ``current_style`` keeps the band behind
    the voice markdown's styled spans, ``_band_block`` re-asserts it per
    logical line — so successive turns stand out as the visual anchors
    of a composed multi-turn preview.
    """
    rec = _record_at(sess, n)
    if rec is None:
        return None
    obj = rec.raw
    kind = _classify(obj)
    if _is_turn_root(rec):
        # A turn root is always a 'user' record (``_is_turn_root``), so
        # ``_render_user`` is the right renderer; thread the band's SGR
        # params as md2ansi's reset baseline, then band every line.
        width = getattr(_BROWSER, 'preview_width', 0) or 60
        rule = _rule(kind, BOLD + WHITE, width=width, bg=BLUE_BG)
        cs = BLUE_BG_PARAMS if BLUE_BG else '0'
        body = _band_block(_render_user(obj, bg=cs), BLUE_BG)
        return rule + '\n' + body
    body = _RENDERERS.get(kind, _render_unknown)(obj)
    rule = _rule(kind, YELLOW)
    return rule + '\n' + body


def _tool_label(cid):
    """Tool name for a composed ``('tool', sess, key)`` block's section rule.

    The dispatching record's first call name — the same source the
    ``<tool:Name>`` umbrella title prefix uses. Defensive: any malformed
    id / scan failure / missing entry falls back to ``'tool'`` so the
    rule still renders.
    """
    try:
        _tag, sess, assistant_line = cid
        rec = _scan_tree(sess).records[assistant_line]
        return rec.tool['name'] or 'tool'
    except Exception:
        return 'tool'


def _node_own_body(cid):
    """The node's own composed unit — body WITH its section rule — or ``None``.

    ``None`` means "this node has no self-contained block; compose from its
    children" and the walker recurses. The ``─── … ───`` rule is a
    composition-time separator (matching how leaves get theirs only when
    embedded): standalone previews call the underlying rule-less renderers
    directly, so a standalone tool/leaf pane shows no redundant top rule.

      * ``msg``  → ``_render_record_with_rule`` (already body + rule).
      * ``tool`` → the rule-less ``_render_tool_umbrella`` block, prefixed
        here with a ``─── tool: <Name> ───`` rule; ``None`` (unregistered
        tool, in-flight part, declining formatter) falls through to recurse.
      * prompt / span / cross-file / anything else → ``None`` (recurse).
    """
    tag = cid[0]
    if tag == 'msg':
        return _render_record_with_rule(cid[1], cid[2])
    if tag == 'tool':
        block = _render_tool_umbrella(cid)
        if block is None:
            return None
        return _rule(f'tool: {_tool_label(cid)}', YELLOW) + '\n' + block
    return None
