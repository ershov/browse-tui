# browse-tui — `update_data` Positioning Design

**Date:** 2026-05-15
**Status:** Draft (pre-implementation)
**Supersedes nothing.** Extends the existing `("upsert", …)` and `("set", …)` ops in `browser.update_data(...)` with an optional positioning descriptor.

## Motivation

Today, `upsert` and `set` only know two child-position rules:

- If the id is already a child of the target parent → keep its current position.
- Otherwise → append at the end of the parent's children.

This is fine for "stream items in arrival order" or "patch an existing row", but recipes that need finer control have no API to express it. Two concrete cases:

1. **Out-of-order streaming** — a source delivers items with explicit position
   (paginated chat history, file lists with a stable sort key, search results
   with re-ranking). The recipe knows where each item should live; today it has
   to fall back to `clear_children` + a full re-emit, which discards subtree
   state and is wasteful.
2. **Reverse insertion** — chat-style "newest at top" / "oldest at top"
   timelines, log tails, undo-stack-style histories. Today the recipe either
   has to buffer everything and emit in the desired order, or emit
   one-at-a-time with a `clear_children` reset between each — also wasteful,
   and the buffering defeats the streaming property entirely.

A positioning descriptor on `upsert` / `set` fixes both without changing any
behaviour for callers that don't opt in.

## Non-goals

- New ops. We extend the two existing positionable ops; we do not add a
  separate `("move", id, position)` op or a `move()` helper. Repositioning is
  expressed as an `upsert` (or `set`) with the appropriate `where=` and the
  `"reposition"` flag.
- Reordering on `set` without `"reposition"`. `set` already replaces the
  `Item` for an existing id; without the flag, the row keeps its current
  position (same convention as today).
- Cross-parent reordering. `where` operates on the children of the op's
  `parent_id`. If `parent_id` differs from the existing parent, the row is
  reparented as today and then `where` decides its position in the new parent.
- Cursor / scroll bookkeeping changes. The existing cursor-anchor machinery
  is already id-keyed and re-snapshots neighbours on every primary hit, so it
  follows a repositioned item naturally without any special-casing.
- `asyncio` semantics or any change to the post-queue model.

---

## API

### The `where` descriptor

A 2- or 3-tuple attached to `upsert` / `set` ops. The position keyword always
sits in slot 0, options always sit in slot 1, the reference (if applicable)
sits in slot 2:

```
(TYPE, OPTIONS [, REFERENCE])

TYPE:      "first" | "last" | "before" | "after"
OPTIONS:   None
         | frozenset of strings   (e.g. frozenset({"reposition"}))
REFERENCE: omitted for "first" / "last"
           required for "before" / "after":
             int  — child index (clamped, see below)
             str  — child id (looked up, see below)
```

Parser, in full:

```python
type_, options = w[0], w[1] or frozenset()
pivot = w[2] if len(w) >= 3 else None
```

#### Position keywords

| Keyword    | Reference        | Result                                                           |
| ---------- | ---------------- | ---------------------------------------------------------------- |
| `"first"`  | (none)           | Insert at the start of `parent_id`'s children list.              |
| `"last"`   | (none)           | Insert at the end.                                               |
| `"before"` | int / str        | Insert immediately before the referenced sibling.                |
| `"after"`  | int / str        | Insert immediately after the referenced sibling.                 |

#### Reference resolution

For `"before"` / `"after"`:

- **`int` index:** clamped using the "collapse-to-edge" rule.
  - Index `< 0` → treat as `"first"` (regardless of direction).
  - Index `> len(children) - 1` → treat as `"last"` (regardless of direction).
  - Index in `[0, len(children) - 1]` → use the normal before/after of that
    child.
- **`str` id:**
  - Id present in `parent_id`'s children → use the normal before/after of
    that child.
  - Id absent (typo, not yet arrived, removed) → treat as missing →
    `"before"` collapses to `"first"`, `"after"` collapses to `"last"`.

The collapse rule is intentionally asymmetric vs. straight Python clamping
(`("after", -10)` lands at position 0, not position 1) so that out-of-range
indices and missing ids behave identically. Recipes can rely on "out-of-range
→ nearest edge" without having to think about direction.

#### Options

A `frozenset` of string flags. Position is always slot 1, even when no
options are needed (caller passes `None`). Slot 1's `None` is converted to
`frozenset()` by the parser.

Currently one flag is defined:

| Flag           | Effect                                                                                                                              |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `"reposition"` | Apply the position even when the id already exists in `parent_id`'s children. Without this flag, existing rows keep their position. |

Unknown flags raise `ValueError` (no silent drop — same posture as unknown
ops).

#### Same-id pivot

If the reference resolves to the id being upserted (e.g.
`upsert("X", parent, where=("before", None, "X"))`):

- Item didn't previously exist → pivot is missing → collapse to `"first"`
  (for `"before"`) or `"last"` (for `"after"`). Standard fallback.
- Item already exists, no `"reposition"` flag → `where` is ignored; existing
  position kept.
- Item already exists, `"reposition"` flag set → no-op on position (moving X
  relative to itself is undefined → we treat as "no change", same as if the
  flag were absent).

### Op tuple shape change

Today:

```
("upsert", id, parent_id, fields)
("set",    id, parent_id, fields)
```

Tomorrow, `where` is an optional 5th element. Both shapes remain valid:

```
("upsert", id, parent_id, fields)                # current — equivalent to where=None
("upsert", id, parent_id, fields, where)         # new
("set",    id, parent_id, fields)                # current
("set",    id, parent_id, fields, where)         # new
```

The 4-tuple form keeps existing recipes working unchanged. The dispatcher in
`apply_ops` looks at `len(op)` (or destructures with a default), reads
`where` if present, and falls through to today's logic when it isn't.

### Helper signatures

`upsert` and `set_item` gain a keyword-only `where=None`:

```python
def upsert(id, parent_id, *, where=None, **fields):
    if where is None:
        return ('upsert', id, parent_id, fields)
    return ('upsert', id, parent_id, fields, where)


def set_item(id, parent_id, *, where=None, **fields):
    if where is None:
        return ('set', id, parent_id, fields)
    return ('set', id, parent_id, fields, where)
```

`where` is keyword-only via `*` so it cannot collide positionally with a
field name. A field literally named `"where"` is still possible via explicit
`fields` dict construction, but the helper-form callers can't hit the
collision by accident.

`Context.upsert` / `Context.set_item` get the same `where=None` parameter
and forward it to the helper.

#### Example call sites

```python
# Prepend a row.
ctx.upsert('chat-msg-42', 'session-9',
           text=msg.text, role=msg.role,
           where=('first', None))

# Insert a row immediately before a known sibling.
ctx.upsert('log-line-99', 'log',
           text=line,
           where=('before', None, 'log-line-100'))

# Insert at a known index with options-slot still required.
ctx.upsert('row-3', 'parent',
           where=('after', None, 2))

# Reposition an existing row to the top.
ctx.upsert('pinned', 'parent',
           pinned=True,
           where=('first', frozenset({'reposition'})))

# Hand-rolled op tuple, no helper.
op = ('upsert', 'x', 'p', {'text': 'hi'}, ('before', None, 'y'))
```

---

## Semantics

### Insertion

Given `parent_id`'s children list at the moment the op is processed:

1. Compute the insertion index from `where`:
   - `("first", _)` → `0`.
   - `("last", _)` → `len(children)`.
   - `("before", _, ref)` → resolve `ref` → if collapses to first, `0`; if collapses to last, `len(children)`; else the resolved child's index.
   - `("after", _, ref)` → resolve `ref` → if collapses to first, `0`; if collapses to last, `len(children)`; else the resolved child's index + 1.
2. **New id:** insert the new Item at the computed index.
3. **Existing id, no `"reposition"`:** ignore the index; keep current position.
4. **Existing id, `"reposition"`:** remove from current position; insert at the index recomputed against the post-removal children list.

### Backwards compatibility

- 4-tuple op shape continues to behave exactly as today.
- Helpers called without `where=` continue to return 4-tuples — recipes that
  introspect tuple length see no change.
- The `where=None` path inside the helper is the cheapest possible branch
  (no allocation beyond what we do today).

### Batch order

Ops within a single `update_data(...)` batch process left to right. A
`where=("before", None, "X")` referencing an id created by a later op in the
same batch will see "X" as missing and collapse to `"first"`. The contract
is: **the pivot must already be a child by the time the positioning op is
processed.**

Recipes that want to insert a chain in dependency order (e.g. building a
linked-list-style sequence from the head) emit the chain head-first;
tail-first sequences should be expressed as `("before", None, head)` for
each new row, which is the natural prepend idiom.

### Cursor anchor interaction

The existing cursor-anchor design (`_cursor_anchor` + `_apply_cursor_anchor`)
keys on item id and re-snapshots neighbours on every primary hit. Therefore:

- A repositioned item keeps the cursor on it (id match, primary hit).
- The snapshot of neighbours is refreshed at the new position, so subsequent
  fallback (if the cursor's id later disappears) walks the *new* siblings.
- Scroll position is not adjusted — the cursor's row may shift on screen
  when its index changes. This matches the rule that scroll only auto-adjusts
  for `expand` with `autoscroll=True`.

No code changes required in the anchor path.

### Expand-goal interaction

`_apply_expand_goal` is triggered by `expand`, not by mutation. Repositioning
inside an already-loaded subtree does not arm a new goal and does not clear
an existing one. This matches the intent: positioning is a structural
mutation, not a viewport intent.

---

## Validation & error handling

| Condition                                                | Outcome                                             |
| -------------------------------------------------------- | --------------------------------------------------- |
| `where` is not a tuple                                   | `ValueError("where must be a tuple")`               |
| `where` length not in `{2, 3}`                           | `ValueError("where tuple must have 2 or 3 elements")` |
| `where[0]` not in `{"first","last","before","after"}`    | `ValueError("unknown where keyword: …")`            |
| `where[0]` is `"first"`/`"last"` and `len(where) == 3`   | Tolerated — REFERENCE silently ignored. (Forgiving.) |
| `where[0]` is `"before"`/`"after"` and `len(where) == 2` | `ValueError("'before'/'after' requires a reference")` |
| `where[1]` not None and not a `set`/`frozenset`          | `ValueError("options must be None or a frozenset")` |
| `where[2]` not None / int / str                          | `ValueError("reference must be int or str")`        |
| Unknown flag in options set                              | `ValueError("unknown option: …")`                   |

Validation runs once per op, at the top of `_apply_upsert` / `_apply_set`,
before any state mutation — so a malformed op in a batch aborts the batch
cleanly without partial application.

---

## Test plan

Tests live in `test/async_/test_update_data_browser.py` alongside the existing
`update_data` coverage, with a new class (`TestUpdateDataPositioning` or
similar) grouping the new behaviour.

### Position-keyword coverage

- `("first", None)` inserts at index 0 with a non-empty children list.
- `("first", None)` inserts at index 0 with an empty children list.
- `("last", None)` inserts at the end.
- `("before", None, str_id)` with a present id → correct index.
- `("after", None, str_id)` with a present id → correct index.
- `("before", None, int_idx)` with an in-range index → correct index.
- `("after", None, int_idx)` with an in-range index → correct index.

### Clamping & fallback

- `("before", None, -1)` → equivalent to `("first", None)`.
- `("after", None, -1)` → equivalent to `("first", None)`.
- `("before", None, 999)` → equivalent to `("last", None)`.
- `("after", None, 999)` → equivalent to `("last", None)`.
- `("before", None, "missing")` → equivalent to `("first", None)`.
- `("after", None, "missing")` → equivalent to `("last", None)`.
- All of the above with an empty children list.

### `"reposition"` flag

- Existing id without flag, with `where` → position unchanged.
- Existing id with `frozenset({"reposition"})` and `("first", …)` → moves to
  start.
- Existing id with `frozenset({"reposition"})` and `("last", …)` → moves to
  end.
- Existing id with reposition + `("before", …, X)` → moves to before X.
- Existing id with reposition + `("after", …, X)` → moves to after X.
- Reposition with same-id pivot → no-op, position unchanged.

### Same-id pivot

- New id with `("before", None, self_id)` → falls back to `("first", None)`.
- New id with `("after", None, self_id)` → falls back to `("last", None)`.
- Existing id, no flag, with same-id pivot → position unchanged.

### Batch processing order

- `[upsert(A, where=("before",None,"B")), upsert(B)]` →
  A inserts as first (B not yet present); B then appends.
- `[upsert(A), upsert(B, where=("before",None,"A"))]` →
  A appends; B inserts before A. Final order: B, A.
- Reposition op for an id appearing earlier in the same batch.

### Helper / Context

- `upsert(id, p, where=("first", None), name="X")` returns a 5-tuple.
- `upsert(id, p, name="X")` returns the legacy 4-tuple (regression).
- `Context.upsert(..., where=...)` forwards the kwarg.

### Validation

- Each error condition in the validation table raises `ValueError` and does
  not mutate state.
- Malformed op in mid-batch → batch raises, prior ops in the batch already
  applied (matches today's semantics — no rollback).

### Cursor anchor (regression)

- Cursor parked on row X; another op repositions X within the same parent →
  cursor stays on X, neighbours snapshot refreshed.
- Cursor parked on row X; reposition op moves a different sibling → cursor
  unaffected, snapshot still valid.

---

## Open questions

None blocking. Two things to revisit after the first implementation lands:

1. **Reposition-on-`set`.** `set` already implies "replace the Item record"
   — should it also imply "move to the new position when `where=` is given,
   without requiring `"reposition"`"? The conservative answer is no
   (consistent with `upsert`); a future iteration can revisit if recipe
   feedback suggests otherwise.
2. **Hand-rolled 5-tuple ops with `where=None` as the 5th element.** Should
   `("upsert", id, p, fields, None)` be accepted as equivalent to the
   4-tuple? Yes — the dispatcher reads `where` as "5th if present, else
   None", and `None` itself is the no-op marker. The helper just emits the
   shorter form when no `where` was given.

---

## Implementation outline (informational)

Roughly:

1. **State / `apply_ops`**
   - Extend `_apply_upsert` and `_apply_set` to accept an optional `where`
     parameter; default `None` preserves today's branch.
   - Add a small `_resolve_where(state, parent_id, where) -> (index, do_move)`
     helper. `index` is the computed insertion index after the parent's
     children list is read; `do_move` is True iff `"reposition"` is in
     options and the id already exists under `parent_id`.
   - Validate the descriptor at the top of `_apply_upsert` / `_apply_set`
     (or in `_resolve_where`).
   - `apply_ops` unpacks the 5th element with a default:
     `kind, id_, pid, fields, *rest = op; where = rest[0] if rest else None`.

2. **Helpers** (`040-state.py`)
   - Add keyword-only `where=None` to `upsert` / `set_item`.
   - Emit 4-tuple when `where is None`, 5-tuple otherwise (cheaper for
     the common case, no behavioural difference for the dispatcher).

3. **Context** (`060-context.py`)
   - Add `where=None` to `Context.upsert` / `Context.set_item`, forward to
     the helper.

4. **Docs**
   - Update `docs/api.md` table for `update_data` ops with the new tuple
     shape and a short example.
   - Update the helper signature table.

5. **Tests** (per plan above).

No changes are required to:

- Cursor anchor (`_compute_anchor_snapshot`, `_apply_cursor_anchor`,
  `_reanchor_cursor`).
- Expand goal (`_apply_expand_goal`).
- `Browser.expand` / `Browser.refresh` / `Browser.cursor_to` / `Pending`
  machinery.
- Renderer / list-pane / preview.

---

## Recipe migration

None required. All existing recipes continue to work; recipes that want
positioning opt in by passing `where=…` to the helpers (or by appending the
5th element to a hand-rolled tuple).

The first recipe to use this will likely be `browse-claude` for chat
history insertion order, and any future "tail log" / "newest-first" recipes.
