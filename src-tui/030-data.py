"""browse-tui: data layer (Item type, coercion, caches)."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Item:
    """A single node in the hierarchy.

    Required: ``id`` (any hashable). Optional: ``title`` (defaults to
    ``str(id)`` via ``__post_init__``), ``tag``, ``tag_style``,
    ``has_children``. Arbitrary extra attributes are permitted —
    ``Item`` is non-slotted by design so recipes can attach
    domain-specific fields like ``size``, ``mtime``, ``path``.
    """

    id: Any
    title: str = ''
    tag: str = ''
    tag_style: str = ''
    has_children: bool = False

    def __post_init__(self):
        if not self.title:
            self.title = str(self.id)


def to_item(x):
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
        if not 1 <= len(x) <= 5:
            raise TypeError(
                f'to_item: tuple must have 1-5 elements, got {len(x)}'
            )
        return Item(*x)
    if isinstance(x, dict):
        if 'id' not in x:
            raise TypeError("to_item: dict must contain 'id' key")
        # Split known dataclass fields from extras so we can attach the
        # rest as arbitrary attributes (Item is intentionally non-slotted).
        known = {'id', 'title', 'tag', 'tag_style', 'has_children'}
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
