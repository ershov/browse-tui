# browse-tui — Interactive Filter (`&`) Design

**Date:** 2026-05-17
**Status:** Draft (pre-implementation)
**Builds on** the per-row `hidden` flag (2026-05-16-row-visibility-design.md)
and the cursor-pin work (2026-05-17-cursor-pin-design.md). Adds an
interactive, user-driven filter overlay with a `less`-like keybinding
(`&`) and stacking semantics.

## Motivation

The framework already gives recipes a *data-level* visibility primitive
(`Item.hidden`) and a *jump-to-match* search (`/`). What's missing is a
user-level **filter**: a way for the user — without recipe support — to
narrow the tree to rows matching a typed pattern, and to refine that
narrowing by stacking further patterns.

This mirrors `less`'s `&pattern` behaviour. The novel pieces are:

- **Tree shape.** Lines in `less` are flat; rows here form a tree. A
  matching descendant should keep its non-matching ancestors visible
  as scaffolding, or the match would be unreachable.
- **Stacking.** Each new filter further narrows (AND of patterns), not
  replaces. The user can layer "open" on top of "today" on top of
  "needs-review".
- **Live as-you-type.** Each keystroke updates the view, like the
  existing search prompt; only on Enter does the filter become a
  committed entry the next `&` can stack on top of.
- **Composition with `hidden`.** Filter doesn't touch the recipe-owned
  `hidden` flag. The two layers are independent: a row appears iff
  *neither* mechanism hides it.

## Goals

1. A `&` keybinding that opens a filter-edit prompt, with live
   re-evaluation as the user types.
2. Stacking: each Enter commits the current edit as a new filter
   entry; the next `&` press starts another entry on top of the
   committed stack.
3. Tree-aware visibility: a row is shown iff it matches **all** active
   filters OR has any descendant that does. Non-matching ancestors of a
   match remain visible as scaffolding.
4. A small recipe API (read + replace) for programmatic filter
   manipulation.
5. Reuse the existing match function (`_search_matches` /
   `_search_text`) so filter and search share the same haystack rules.
6. Reuse the existing cursor displacement (`_apply_hide_displacement`)
   and select-all-visible (WYSIWYG) paths — no new cursor or selection
   code.

## Non-goals

- Negation (`&!pattern` in `less`). Could add a syntax later; not in
  scope.
- Regex / glob patterns. The filter uses the same fragment-AND
  substring matcher as `/` search.
- Replacing or modifying the recipe-owned `Item.hidden` flag. Filter
  writes to a separate per-Item attribute.
- Auto-expand-on-match. If a match lives under a collapsed parent, the
  scaffold parent stays visible but the match remains tucked away
  until the user expands. Explicit expansion is the user's job; auto-
  expand could surprise.
- Predicate-based filters (callables instead of strings). Recipes
  encode arbitrary filterable state into title/tag for now.
- Hooks / callbacks fired on filter change.
- Persistence across runs. Filters reset to empty each invocation.

---

## State

### Mode enum

The existing `_search_mode: bool` flag becomes one branch of a
three-way enum:

```python
class Mode(enum.Enum):
    NORMAL = 'normal'
    SEARCH_EDIT = 'search-edit'
    FILTER_EDIT = 'filter-edit'

# Browser:
self._mode: Mode = Mode.NORMAL
```

`_search_mode` call sites switch to `self._mode is Mode.SEARCH_EDIT`.
Search behaviour itself does not change.

### Browser additions

```python
self._filters: list[str] = []
```

- The list of all filter entries, in stacking order (oldest first).
- While `self._mode is Mode.FILTER_EDIT`, the **last** entry is the
  one currently being typed (the "live" entry). Outside FILTER_EDIT,
  every entry is committed.
- Filter is *active* iff `bool(self._filters)`. No separate flag.
- Empty entries are evaluator-skipped and recipe-invisible (see API
  and Evaluation sections); they exist transiently as the placeholder
  for "user just opened the prompt but hasn't typed yet".

### Item addition

```python
@dataclass
class Item:
    ...
    _filter_hidden: bool = field(
        default=False, init=False, repr=False, compare=False,
    )
```

Framework-internal flag, written by the filter evaluator. Hidden from
recipe-facing surfaces (`init=False` keeps it out of `Item(...)`
construction; `repr=False` and `compare=False` keep it out of debug
output and equality). Recipes never read or write it.

---

## User-facing behaviour

### Keymap (NORMAL → FILTER_EDIT)

| Key | Effect |
|---|---|
| `&` | `mode → FILTER_EDIT`; `self._filters.append('')` (placeholder) |

### Keymap (FILTER_EDIT)

| Key | Effect |
|---|---|
| Printable char | Append to last entry; recompute |
| Backspace | Drop last char of last entry (no-op if already empty); recompute |
| Enter, last entry non-empty | `mode → NORMAL` (last entry committed) |
| Enter, last entry empty | `self._filters = []`; `mode → NORMAL` (clear-all) |
| Ctrl-X | `self._filters = []`; `mode → NORMAL` (clear-all, any state) |
| Ctrl-C / Esc | `self._filters.pop()`; `mode → NORMAL` (cancel current edit) |

Notes:

- Enter-on-empty is `less`-compatible: typing `&` then Enter clears
  all filters. Ctrl-X is the more direct alternative; both yield the
  same result.
- Ctrl-C / Esc cancels the in-progress edit only. Previously committed
  filters survive. Pop the placeholder if no typing happened, else pop
  the half-typed entry.
- Backspace cannot eat past the start of the in-progress entry. The
  user cannot edit prior committed filters from within filter-edit;
  that's deliberately out of scope (Future work).

### Fall-through for non-overridden keys

Keys that are **not** in the table above fall through to NORMAL-mode
dispatch. The user can navigate (`Up`, `Down`, `PageUp`, `PageDown`,
arrow keys for the cursor, scope-in / scope-out, expand / collapse,
etc.) while a filter edit is in progress; the prompt stays open and
the last entry continues accumulating typed characters.

This mirrors the existing SEARCH_EDIT behaviour. The general rule for
both edit modes: the mode owns a small set of explicit overrides
(printable input, the prompt-control keys listed above); everything
else dispatches as if the user were in NORMAL mode.

Note that `Enter` **is** explicitly overridden in FILTER_EDIT (commit
or clear-all). It does *not* fall through, so any NORMAL-mode binding
on `Enter` (e.g., recipe `on_enter` handlers) does not fire while the
filter prompt is open. The user must Enter-out of FILTER_EDIT first
before `Enter` activates its normal action.

### Status row

While `self._filters` non-empty *or* `self._mode is Mode.FILTER_EDIT`,
the info row shows the filter state. Format:

- Outside FILTER_EDIT: `Filter: foo & bar`
- In FILTER_EDIT mid-typing: `Filter: foo & bar & ba_` (cursor on the
  last entry).
- In FILTER_EDIT empty placeholder: `Filter: foo & bar & _`.

Exact rendering is a UX detail; the data is available from
`self._filters` + `self._mode`.

---

## Evaluation

```python
def _recompute_filter_hidden(state, filters):
    """Single bottom-up pass; writes Item._filter_hidden + state._filter_active."""
    active = [q for q in filters if q]
    state._filter_active = bool(active)
    if not active:
        # Existing _filter_hidden flags are stale but inert: the
        # renderer guards on state._filter_active. We skip the O(N)
        # clear pass; flags get rewritten next time a filter activates.
        return

    def visit(item):
        children = state._children.get(item.id, [])
        any_desc_passes = False
        for child in children:
            if visit(child):
                any_desc_passes = True
        if item.has_children and item.id not in state._children:
            any_desc_passes = True   # optimistic: un-cached children
        text = _search_text(item)
        self_passes = all(_search_matches(text, q) for q in active)
        item._filter_hidden = not (self_passes or any_desc_passes)
        return self_passes or any_desc_passes

    for root in state._children.get(None, []):
        visit(root)
```

The streaming/pending-children optimism is baked into the same pass
(see *Streaming / pending children* below).

### Streaming / pending children

If a parent has `has_children=True` but its children are not yet
cached in `state._children`, the recursive `visit` sees no children
and the parent's `_filter_hidden` is decided on self-match alone.
That can prematurely hide a parent whose still-loading children would
match.

**Resolution:** treat an un-cached `has_children` parent as a
"don't-know-yet" scaffold — keep it visible:

```python
if item.has_children and item.id not in state._children:
    any_desc_passes = True   # optimistic: maybe matching children
                              # arrive later; recompute will fix it
```

Once children stream in, the next `update_data` batch retriggers
recompute and the parent's state is corrected.

### Active flag

`active = [q for q in filters if q]` is computed once per recompute.
The empty placeholder during FILTER_EDIT contributes nothing to
evaluation, so a freshly-opened prompt with no typing leaves the view
unchanged.

---

## Render integration

In `_emit_children` (state.py), one new check next to the existing
`hidden` skip:

```python
if getattr(child, 'hidden', False):
    continue
if state._filter_active and getattr(child, '_filter_hidden', False):
    continue
out.append(VisibleEntry(item=child, depth=d, kind='normal'))
```

`state._filter_active: bool` is a small flag set by the evaluator in
lockstep with the recompute pass:

```python
state._filter_active = bool(active)
```

The flag, not the filter list itself, lives on `State` because the
renderer already reads `State` and we want to avoid threading Browser
through `_emit_children`. The authoritative list of filter strings
stays on `Browser` (`self._filters`); the flag is just the renderer-
visible boolean derived from it.

Key property: the renderer treats filter-hidden as *per-row*. The
subtree-cascade falls out of the evaluator (a filter-hidden row by
definition has no passing descendants, so none of its children pass
either); we don't need a second cascade path in the renderer.

---

## Recompute triggers

`_recompute_filter_hidden` fires:

1. **Each keystroke in FILTER_EDIT** that mutates the last entry
   (printable, Backspace).
2. **Mode transitions** that change `self._filters`:
   - `&` (NORMAL → FILTER_EDIT) — placeholder added, but no
     evaluation work (placeholder filters to empty `active`).
   - Enter, Ctrl-X, Ctrl-C / Esc.
3. **End of `update_data` batches.** The same hook that today calls
   `_apply_hide_displacement` calls recompute first, then cursor
   displacement, then `_apply_cursor_anchor`. New items may match or
   un-match; existing items may have moved subtrees.
4. **Recipe writes** via `set_filters` (see API).

---

## Cursor and selection integration

### Cursor displacement

When recompute writes `_filter_hidden = True` on an item that's
currently under the cursor, the existing `_apply_hide_displacement`
handles the walk-back. Same code path as for the `hidden` flag — both
are "row vanished from `visible_items`" events.

The order within an `update_data._apply` extends from:

```python
1. apply ops
2. snapshot pre-vis if needed
3. _apply_hide_displacement (for hidden flag)
4. _apply_cursor_anchor
```

to:

```python
1. apply ops
2. snapshot pre-vis if needed
3. _recompute_filter_hidden (new)
4. _apply_hide_displacement (covers both hidden and filter-hidden)
5. _apply_cursor_anchor
```

`_apply_hide_displacement` walks the pre-batch visible list backward
from the pre-batch cursor index until it finds an id still in the
post-batch visible list. With filter recompute landing before the
displacement pass, the post-batch visible list already reflects
filter-hidden rows, so the existing walk Just Works.

Pin (`PIN_FIRST` / `PIN_LAST`) short-circuits hide-displacement
exactly as before: pinned cursors don't need a walk-back; they
re-bind to the new first/last visible row at anchor time.

### Selection

Filter-hidden rows are not in `visible_items`. The existing
`select_all_visible` (clear-then-add-visible) therefore drops filter-
hidden rows from the selection — same WYSIWYG semantics that already
apply to `hidden`-flagged rows and collapsed children.

`state.selected` itself is not pruned on filter changes; ids stay in
the set but don't contribute to display. This mirrors the `hidden`
treatment.

### Search

Search (`/`) walks `visible_items`. With filter active, the visible
list is already narrowed, so search runs only over filter-passing
rows. This matches the `less` mental model: `&` narrows the corpus,
`/` finds within the narrowed view.

---

## Recipe API

### Browser methods

All thread-safe via `post`:

```python
@property
def filters(self) -> tuple[str, ...]:
    return tuple(q for q in self._filters if q)

def set_filters(self, filters: Iterable[str]) -> None: ...
def add_filter(self, text: str) -> None: ...
def clear_filters(self) -> None: ...
```

### Context passthroughs

Identical names: `ctx.filters`, `ctx.set_filters`, `ctx.add_filter`,
`ctx.clear_filters`.

### Read semantics

`b.filters` returns *all non-empty* entries in order — including the
in-progress entry while the user is typing. Rationale: the live
filter already affects what the user sees, so a recipe rendering
"current filter state" should reflect it.

The empty placeholder (open prompt, no typing) is filtered out. It's
a UI mechanism, not a filter the recipe should ever see.

### Write semantics

`set_filters(filters)` replaces the list with the given iterable
(empty strings dropped silently). If `self._mode is Mode.FILTER_EDIT`
at write time, the mode is forced to `Mode.NORMAL` (the in-progress
edit is discarded). Recipe writes are authoritative; the rare race
with user typing yields to the recipe.

After the write, `_recompute_filter_hidden` is fired before the next
render.

`add_filter(text)` appends `text` if non-empty, no-op otherwise. If
the user is in FILTER_EDIT at the time of the call, mode is forced to
NORMAL first (the in-progress placeholder is dropped), then the new
entry is appended. This avoids the fragile case of inserting before
the placeholder. Consistent with `set_filters`'s authoritative-write
posture.

`clear_filters()` is `set_filters([])`.

### Not exposed

- `Mode` enum / `ctx.mode` — recipes don't need to know whether the
  user is typing. Could surface later behind a use case.
- `Item._filter_hidden` — internal; recipes compute their own
  predicates if needed.
- Per-keystroke change hooks — defer.

---

## Composition with `hidden`

The two layers combine at render time:

| `Item.hidden` | `Item._filter_hidden` (with filter active) | Result |
|---|---|---|
| False | False | visible |
| False | True | hidden by filter |
| True | False | hidden by recipe (subtree cascade) |
| True | True | hidden by both (subtree cascade still applies) |

`Item.hidden` cascades over the subtree (the entire subtree is
skipped in `_emit_children`). `Item._filter_hidden` is per-row, but
because the evaluator only sets it on rows whose entire subtree fails,
the effect is indistinguishable from a subtree cascade — it just
doesn't need a second cascade path.

A row hidden by `Item.hidden` is still visited by the filter
evaluator (the evaluator reads from `state._children`, not from
`visible_items`). That means a filter can keep a recipe-hidden
ancestor "wanting to be visible" — but the renderer short-circuits on
`Item.hidden` first, so the recipe wins. The recipe-hidden row stays
gone. This is the correct precedence: recipe-driven hide is
authoritative over user-driven filter.

---

## Future work

- Negation: `&!pattern` to invert a filter entry. Straightforward
  evaluator change once syntax is decided.
- Editing prior committed entries — currently out of scope. The user
  must Ctrl-C / clear-all / re-enter the stack. Could later surface
  arrow-key navigation in FILTER_EDIT to walk entries.
- A "show hidden rows" toggle that bypasses both `Item.hidden` and
  `_filter_hidden` at render time. Trivial extension: a Browser flag
  short-circuits both checks in `_emit_children`.
- Predicate-based filters (callable predicates instead of strings).
- Persistence of filter state across runs.
- Auto-expand-on-match (a match-promotes-ancestors mode where the
  scaffold path also auto-expands).
- Per-keystroke change hooks for recipes.

---

## Test plan

Unit (filter evaluator + state):

- Empty `_filters` → no rows hidden.
- Single filter, leaf match → match visible, non-matches hidden.
- Single filter, deep match → ancestors kept as scaffold; non-
  matching siblings of scaffold ancestors hidden.
- Multiple filters → AND semantics; row passes iff every filter
  matches.
- Empty entries in `_filters` are skipped.
- `Item.hidden=True` doesn't prevent the evaluator from visiting a
  row, but the renderer still hides it (recipe precedence).
- `has_children=True` with no cached children → parent stays visible
  (optimistic).
- `update_data` batch triggers recompute; new matching items unhide
  their scaffold ancestors.

Unit (filter API):

- `ctx.filters` returns non-empty entries including the live one.
- `ctx.set_filters` replaces; in-progress edit dropped; mode forced
  to NORMAL.
- `ctx.add_filter('')` is a no-op; `ctx.add_filter('foo')` appends.
- `ctx.clear_filters()` empties.

Action / dispatch (FILTER_EDIT mode):

- `&` from NORMAL appends placeholder, switches mode.
- Typing mutates last entry; Backspace eats last char only.
- Enter on non-empty commits; on empty clears all and exits.
- Ctrl-X clears all regardless of last entry.
- Ctrl-C / Esc cancels current edit, keeps committed filters.
- Non-overridden keys (arrows, PageUp/Down, scope-in/out, expand)
  fall through to NORMAL-mode dispatch while the prompt stays open
  and the last entry remains unchanged. Mirrors SEARCH_EDIT.
- Enter does **not** fall through: recipe `on_enter` handlers do not
  fire while in FILTER_EDIT.

Async (Browser-level):

- Cursor hide-displacement fires when filter hides the cursor row;
  cursor walks back through pre-batch visible.
- `PIN_FIRST` / `PIN_LAST` survive filter-induced row changes;
  cursor re-binds to new first/last visible row.
- Select-all-visible drops filter-hidden ids from `state.selected`
  (clear-then-add-visible).
- Search jumps within the filter-narrowed visible list.

---

## Implementation order

1. **Mode enum.** Refactor `_search_mode: bool` → `_mode: Mode`.
   Touch search-mode call sites. No behaviour change; verify all
   existing tests pass.
2. **`Item._filter_hidden` field.** Add the dataclass attribute;
   no readers/writers yet. Verify dataclass equality / construction
   unaffected.
3. **Evaluator.** `_recompute_filter_hidden(state, filters)`.
   Standalone, no integration. Unit tests for the bottom-up pass.
4. **Browser state + API.** `self._filters`, `set_filters`,
   `add_filter`, `clear_filters`, `filters` property.
5. **Render integration.** Hook in `_emit_children`. Verify rendering
   with hand-set `_filter_hidden` flags.
6. **Recompute triggers.** Wire to `update_data._apply` (after ops,
   before hide-displacement). Wire to API writes.
7. **FILTER_EDIT keymap.** New dispatch path. `&` entry, keystroke
   handling, Enter / Ctrl-X / Ctrl-C / Esc.
8. **Status row.** Filter indicator in the info pane.
9. **Tests.** Per the test plan above.
10. **Docs.** Update `docs/api.md` (Mode enum, filter API, Item
    `_filter_hidden` note, status row, key bindings).
