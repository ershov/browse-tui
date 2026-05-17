"""browse-tui: data layer (Item type, coercion, caches)."""

from dataclasses import dataclass, field
from typing import Any


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

    ``_filter_hidden`` is a framework-internal flag written by the
    filter evaluator (see ``docs/superpowers/specs/2026-05-17-filter-
    design.md``). Recipes do not see or set it: ``init=False`` keeps
    it out of constructor signatures, ``repr=False`` hides it from
    debug dumps, ``compare=False`` keeps it out of ``__eq__`` /
    ``__hash__``.
    """

    id: Any
    title: str = ''
    tag: str = ''
    tag_style: str = ''
    has_children: bool = False
    hidden: bool = False
    _filter_hidden: bool = field(
        default=False, init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if not self.title:
            self.title = str(self.id)


def to_item(x: Any) -> Item:
    """Coerce a flexible input shape into an ``Item``.

    Accepted shapes:
      - ``Item`` — returned unchanged (identity).
      - ``str`` — ``Item(id=x)`` (leaf; title defaults to the same string).
      - ``tuple`` — positional dataclass init: 1-5 elements matching the
        field order ``(id, title, tag, tag_style, has_children)``. Empty
        tuples and tuples with 6+ elements raise ``TypeError``.
      - ``dict`` — ``Item(**x)``. The dict must contain an ``'id'`` key;
        extra keys land as arbitrary attributes on the resulting Item.

    Anything else (including ``int``, ``None``, ``list``, ``set``) raises
    ``TypeError``. Callers iterating over a heterogeneous source should
    invoke ``to_item`` element-wise — ``to_item`` itself never iterates.
    """
    if isinstance(x, Item):
        return x
    if isinstance(x, str):
        return Item(id=x)
    if isinstance(x, tuple):
        if not 1 <= len(x) <= 6:
            raise TypeError(
                f'to_item: tuple must have 1-6 elements, got {len(x)}'
            )
        return Item(*x)
    if isinstance(x, dict):
        if 'id' not in x:
            raise TypeError("to_item: dict must contain 'id' key")
        # Split known dataclass fields from extras so we can attach the
        # rest as arbitrary attributes (Item is intentionally non-slotted).
        known = {'id', 'title', 'tag', 'tag_style', 'has_children', 'hidden'}
        fields = {k: v for k, v in x.items() if k in known}
        extras = {k: v for k, v in x.items() if k not in known}
        item = Item(**fields)
        for k, v in extras.items():
            setattr(item, k, v)
        return item
    raise TypeError(
        f'to_item: unsupported type {type(x).__name__}; '
        f'expected Item, str, tuple, or dict'
    )
