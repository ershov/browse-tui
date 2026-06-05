# Hashable object IDs — beyond strings (Epic)

**Status:** Proposed (future epic — not part of the meta-rows work)
**Date:** 2026-06-05

## Idea

`Item.id` is documented as "any hashable," but in practice recipes encode
structured information into **string** ids and re-parse it everywhere:

- `browse-git` — `'filler:<ns>:<n>'`, `'commit:<sha>'`, and `partition`/prefix
  checks on those strings.
- `browse-claude` — `'<session_path>#agent:<agent_id>'`, with `'#agent:' in cid`
  routing and `partition('#agent:')` splits littered through `get_children`.
- path-split recipes — separator-joined ids built by `expand_path_rows`.

Let recipes use any **hashable** object as an id — `tuple`, `namedtuple`,
`frozenset`, frozen `dataclass` — so structured / namespaced ids need no
string-encoding or string-parsing. A recipe could write
`Item(id=('agent', session_path, agent_id))` and match on `id[0] == 'agent'`
instead of substring checks.

## Hard requirement: hashable, not `==`-only

IDs are used as **dict keys and set members on the hot path** — `_items_by_id`,
`_children`, `_parent_of_id`, `state.expanded`, `state.selected`, preview-cache
keys, `scope_stack`, cursor anchors. They must stay **hashable** for O(1)
lookups. Allowing merely `==`-comparable (non-hashable: `list`, `dict`, `set`)
would force O(n) equality scans across these structures — a real regression for
thousand-row lists. **Reject `==`-only.** Hashable is the contract.

## Framework scope (modest)

Treat the id as an **opaque hashable value** everywhere it flows, and stringify
**only at boundaries**. Audit and define the stringification at each:

- **`show_ids` display** — the id segment is `'{} '.format(item.id)`
  (`default_row_content`); decide how a non-str id renders (likely `str(id)`,
  but tuples look verbose — recipes may prefer a custom `format_row_content`).
- **Search text** — `_search_text` includes the id; uses `str(id)`.
- **Action env vars** — ids passed to external shell commands must be encoded as
  strings. **The biggest open question:** `str()`, a recipe-supplied formatter,
  or JSON? Affects the action contract.
- **Debug / error messages** — already `repr`/`str`; fine.

## Corner cases

- **`to_item` bare-tuple shorthand** treats a raw `tuple` as positional *fields*
  (`(id, title, …)`), so a tuple **id** must be passed via `Item(id=…)` /
  `dict(id=…)`. Document, or add an unambiguous construction path.
- **`expand_path_rows` / path-split tree** assumes string ids + a separator;
  stays string-based (or grows a structured variant).
- **Recipe-side id-shape logic** (`startswith('filler:')`, `'#agent:' in cid`,
  `partition(...)`) must migrate to structured-field checks. **This is the bulk
  of the work** — per-recipe refactors, each with its own tests.
- **Equality/hash stability** — ids must be immutable once assigned (tuples of
  hashables qualify; mutable members do not).

## Effort

Framework changes are small (the boundary audit above). The weight is
**refactoring each recipe's id scheme and id-shape logic**, best done per-recipe
as separate tickets under this epic. Other work — including the meta-rows
change — **keeps string IDs** until this epic lands.
