"""browse-tui: data layer (Item type, coercion, caches)."""

import dataclasses
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Any


# ``PreviewRender`` is the per-Item wrap cache used by ``render_preview``
# (050-render.py). It is filled lazily on first paint and dropped eagerly
# whenever its inputs change:
#
#   * ``Item.preview`` is set / appended / cleared
#   * terminal resize
#   * ``preview_ansi`` toggle (Capital-R)
#   * ``update_data`` mod / upsert touching the Item
#
# ``wrapped``             — list[str] of wrapped/SGR-tokenised rows
# ``raw_tail_offset``     — character offset in ``Item.preview`` where the
#                           wrap consumed input up to (for the future
#                           in-place ``append_preview`` fast path).
# ``wrapped_tail_offset`` — number of wrapped rows produced so far (same
#                           reason).
# ``width``, ``ansi_on``  — geometry / SGR-policy the cache was built
#                           against. Defensive fields: eager invalidation
#                           hooks (resize, ansi toggle) should have
#                           dropped the cache already, but a mismatch
#                           triggers regeneration as a safety net.
PreviewRender = namedtuple(
    'PreviewRender',
    ['wrapped', 'raw_tail_offset', 'wrapped_tail_offset',
     'width', 'ansi_on'],
)


# ``ChildrenGridLayout`` is the per-Browser last-computed result of
# ``_sub_layout`` (050-render.py). The grid layout depends only on
# ``(children list, width, show_ids)``. ``Browser.children_grid_layout``
# recomputes on every call (no cache — see #434) and stores the
# resulting namedtuple on the Browser so callers can read it without
# re-deriving inputs.
#
# Fields mirror ``_sub_layout``'s return tuple:
#   * ``num_cols``    — column count for the layout.
#   * ``col_width``   — width of each column (incl. inter-column gap).
#   * ``slot_rows``   — list[int]; rows occupied by each entry.
#   * ``entry_lines`` — list[list[str]]; wrapped lines per entry.
ChildrenGridLayout = namedtuple(
    'ChildrenGridLayout',
    ['num_cols', 'col_width', 'slot_rows', 'entry_lines'],
)


@dataclass
class Item:
    """A single node in the hierarchy.

    Required: ``id`` (any hashable). Optional: ``title`` (defaults to
    ``str(id)`` via ``__post_init__``), ``tag``, ``tag_style``,
    ``has_children``. Arbitrary extra attributes are permitted —
    ``Item`` is non-slotted by design so recipes can attach
    domain-specific fields like ``size``, ``mtime``, ``path``. Those
    extras survive across the full pipeline (rendering, search, action
    env vars).

    ``tag_style`` accepts one of ``'green'``, ``'red'``, ``'yellow'``,
    ``'gray'``, ``'cyan'``, ``'blue'``, ``'magenta'``, ``'dim'`` — or
    the empty string for no styling. Unknown names render as plain text.

    ``has_children`` controls the ``▼/▶`` marker and whether expansion
    is offered on the row.

    ``hidden`` (default ``False``) is a per-row visibility flag.
    Hidden rows are skipped at render time; a hidden expandable parent
    hides its entire subtree (render-only cascade — descendants' own
    ``hidden`` values are preserved). See ``docs/superpowers/specs/
    2026-05-16-row-visibility-design.md`` for the full semantics.

    ``boundary`` (default ``False``) marks a node that heads a
    *self-contained foreign subtree* — content sourced from outside the
    current document (a referenced file, a subagent transcript, a bare
    session). Recursive / multi-expansion — the Alt-Right action
    (``_expand_recursive`` in ``070-actions.py``) and ``expand_subtree``
    (``040-state.py``) — *reveals* a boundary but never *expands* it: a
    boundary reached as a descendant of the walk is left out of
    ``state.expanded`` (its row stays visible under its expanded parent,
    but collapsed), and the walk never recurses *through* it into its
    children, **even when they are already cached** from a prior manual
    expand. This keeps a same-document bulk expand from dragging the
    foreign subtree in, and — when the boundary's children are *not*
    cached — avoids stranding a ``⧗ loading…`` placeholder that no
    auto-dispatch would resolve (the cursor-prefetch fetches the cursor
    row only). The node stays manually expandable: a single ``→`` / ``l``
    on it — or ``expand_subtree`` called *directly* on it — opens that
    one node, since that is an explicit "open this" rather than a walk
    that merely passed over it (it still does not recurse through). The
    other half of the contract — *not folding a boundary's descendants
    into an ancestor's preview cascade* — is honoured by recipes that
    build such cascades; the framework has no cross-item preview concept.

    ``meta`` (default ``False``) marks a *non-content* row — a divider,
    section header, or structural connector line. The cursor skips it
    (best-effort: explicit ``cursor_to`` or an all-meta list may still
    land on it, which is not an error), it is never selectable, and it is
    excluded from search/filter by default. ``has_children`` is ignored
    on a meta row — it is always a leaf. See ``docs/superpowers/specs/
    2026-06-05-meta-rows-design.md`` for the full semantics.

    ``_filter_hidden`` is a framework-internal flag written by the
    filter evaluator (see ``docs/superpowers/specs/2026-05-17-filter-
    design.md``). Recipes do not see or set it: ``init=False`` keeps
    it out of constructor signatures, ``repr=False`` hides it from
    debug dumps, ``compare=False`` keeps it out of ``__eq__`` /
    ``__hash__``.

    ``preview`` is the per-item preview text cache (string or
    ``None``). Lives on the Item rather than a side-table dict so it
    survives the Item's lifetime naturally and is dropped with it.
    Excluded from ``__eq__`` / ``__hash__`` / ``repr`` and ``init`` —
    it's a derived cache slot, not part of the Item's identity. Recipes
    populate it via ``Browser.set_preview`` / ``append_preview`` /
    ``clear_preview`` / ``invalidate_preview``; the worker delivery
    path writes it directly.

    ``preview_render`` is the per-Item wrap cache (a ``PreviewRender``
    namedtuple or ``None``) consumed by ``render_preview``. Filled
    lazily on first paint; dropped to ``None`` eagerly on any input
    change (preview text mutation, terminal resize, ``preview_ansi``
    toggle, ``update_data`` mod/upsert). Same identity exclusion as
    ``preview``.
    """

    id: Any
    title: str = ''
    tag: str = ''
    tag_style: str = ''
    has_children: bool = False
    hidden: bool = False
    boundary: bool = False
    meta: bool = False
    _filter_hidden: bool = field(
        default=False, init=False, repr=False, compare=False,
    )
    preview: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    # Wrap cache for ``preview`` — see ``PreviewRender`` above. Lazily
    # filled by ``render_preview`` on first paint; eagerly invalidated
    # (set back to ``None``) on any input change (text mutation, resize,
    # ansi toggle, mod/upsert). Excluded from identity for the same
    # reason as ``preview``.
    preview_render: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    # Optional alternative title used by the renderer for the
    # ``scope_root`` row only — for items whose listing label is a
    # short id (e.g. a session GUID) but whose scope-header should
    # carry more context (full path, qualified name). ``None`` falls
    # back to ``title`` so the field is a pure opt-in for recipes that
    # want the distinction. Placed after the cache fields to keep the
    # field declaration order grouped (identity/data fields, then the
    # derived cache slots, then this opt-in); recipes set it by keyword
    # or via attribute write.
    scope_title: Any = None
    # Framework-owned provenance flag. ``True`` for Items fabricated by
    # ``visible_items`` as a stub when a scope-root id has no real Item
    # yet (recipe-pre-pushed ``initial_scope``, deep ``scope_stack``
    # injected by a recipe, lazy-fetched alt-up into an uncached
    # ancestor, post-refresh window before re-fetch). Cleared by
    # ``_promote_synthetic`` when the parent's children fetch delivers
    # a real Item with the matching id. Recipes do not set this; the
    # default carries the right semantics for everything else.
    synthetic: bool = field(
        default=False, init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if not self.title:
            self.title = str(self.id)


def to_item(x: Any) -> Item:
    """Coerce a flexible input shape into an ``Item``.

    *Any hashable is an id.* Accepted shapes:
      - ``Item`` — returned unchanged (identity).
      - ``dict`` — kwargs payload that must carry an ``'id'`` key. Known
        dataclass fields become constructor args; any extra keys land as
        arbitrary attributes on the resulting Item (``Item`` is non-slotted).
      - anything else — taken as the id verbatim: ``Item(id=x)``. This
        subsumes ``str``, ``int``, ``tuple``, ``frozenset``, a frozen
        ``dataclass``, and every other hashable. There is no positional-
        tuple shorthand: ``('a', 'Apple')`` is an id, not ``(id, title)``.

    A genuinely unhashable id (``list``/``dict``-but-not-a-payload/``set``)
    is *not* rejected here — the hash is taken (and a clear error raised)
    only when the Item enters the id index, at ``_index_set``. Callers
    iterating over a heterogeneous source invoke ``to_item`` element-wise;
    ``to_item`` itself never iterates.
    """
    if isinstance(x, Item):
        return x
    if isinstance(x, dict):
        if 'id' not in x:
            raise TypeError("to_item: dict must contain 'id' key")
        # Split known dataclass fields from extras so we can attach the
        # rest as arbitrary attributes (Item is intentionally non-slotted).
        known = {'id', 'title', 'tag', 'tag_style', 'has_children',
                 'hidden', 'boundary', 'meta'}
        fields = {k: v for k, v in x.items() if k in known}
        extras = {k: v for k, v in x.items() if k not in known}
        item = Item(**fields)
        for k, v in extras.items():
            setattr(item, k, v)
        return item
    return Item(id=x)


def _split_path_row(row: Any):
    """Extract ``(id, extras, explicit_title, title)`` from a raw row.

    Runs *before* ``to_item`` coercion so an explicit ``title`` can be
    told apart from the ``str(id)`` default. ``extras`` are the carried
    metadata fields (everything but ``id``/``title``) destined for the
    leaf. ``title`` is meaningful only when ``explicit_title`` is True.
    Mirrors ``to_item``'s accepted shapes.
    """
    if isinstance(row, Item):
        extras = {'tag': row.tag, 'tag_style': row.tag_style}
        extras.update(_item_extras(row))
        return row.id, extras, row.title != str(row.id), row.title
    if isinstance(row, str):
        return row, {}, False, None
    if isinstance(row, tuple):
        if not 1 <= len(row) <= 6:
            raise TypeError(
                f'expand_path_rows: tuple must have 1-6 elements, '
                f'got {len(row)}'
            )
        # Positional (id, title, tag, tag_style, has_children, hidden);
        # has_children/hidden are dropped (structure is derived).
        extras = dict(zip(('tag', 'tag_style'), row[2:4]))
        title = row[1] if len(row) >= 2 else None
        return row[0], extras, len(row) >= 2, title
    if isinstance(row, dict):
        if 'id' not in row:
            raise TypeError("expand_path_rows: dict must contain 'id' key")
        extras = {k: v for k, v in row.items() if k != 'id'}
        explicit_title = 'title' in extras
        title = extras.pop('title', None)
        return row['id'], extras, explicit_title, title
    raise TypeError(
        f'expand_path_rows: unsupported row type {type(row).__name__}; '
        f'expected Item, str, tuple, or dict'
    )


def _item_extras(it: Item) -> dict:
    """Carry an ``Item``'s recipe-attached extra attributes onto a leaf.

    Excludes *all* declared dataclass fields (derived from the dataclass
    itself, so cache/provenance slots like ``preview``, ``preview_render``,
    ``synthetic`` and ``scope_title`` never leak) plus underscore-prefixed
    framework internals. ``tag``/``tag_style`` are declared fields and so
    excluded here — ``_split_path_row`` adds them explicitly for Item rows.
    """
    declared = {f.name for f in dataclasses.fields(it)}
    return {
        k: v for k, v in vars(it).items()
        if not k.startswith('_') and k not in declared
    }


def expand_path_rows(rows, sep: str) -> list:
    """Expand path-like ids into a list of node-row dicts.

    Each input row (``Item``/``str``/``tuple``/``dict``, per ``to_item``)
    has its ``id`` split on the non-empty separator ``sep`` to synthesize
    a tree. Returns ``list[dict]`` with ``id``, ``parent`` and ``title``
    set — one dict per leaf and per intermediate prefix node — ordered so
    grouping by ``parent`` yields first-seen sibling order. ``parent`` is
    ``None`` for top-level nodes. ``has_children`` is *not* set here; the
    consumer derives it from the parent links.

    Empty-segment handling is path-aware: a leading separator is
    preserved (``/etc/x`` ≠ ``etc/x``), while doubled and trailing
    separators collapse. Entries with no non-empty segment are skipped.
    A prefix that is also an explicit input row merges its carried fields
    and explicit title onto the already-emitted node (explicit wins).
    """
    nodes: dict = {}  # id -> emitted dict (insertion order = first-seen)
    for row in rows:
        rid, extras, explicit_title, title = _split_path_row(row)
        rid = str(rid)
        lead = sep if rid.startswith(sep) else ''
        segs = [s for s in rid.split(sep) if s]
        if not segs:
            continue  # empty / all-separator entry → no node
        last = len(segs)
        parent = None
        for k in range(1, last + 1):
            nid = lead + sep.join(segs[:k])
            node = nodes.get(nid)
            if node is None:
                node = nodes[nid] = {
                    'id': nid, 'parent': parent, 'title': segs[k - 1],
                }
            if k == last:
                # Leaf level for this row: attach its carried metadata,
                # and let an explicit title override the segment. The
                # synthesized ``id``/``parent`` links are structural and
                # never clobbered by a carried column of the same name.
                for ek, ev in extras.items():
                    if ek not in ('id', 'parent'):
                        node[ek] = ev
                if explicit_title:
                    node['title'] = title
            parent = nid
    return list(nodes.values())
