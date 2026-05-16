# browse-claude — Voice-Only Filter Design

**Date:** 2026-05-16
**Status:** Draft (pre-implementation)
**Scope:** Recipe-level (`recipes/browse-claude`). Relies on the
framework's `Item.hidden` / `mod` op (see
`2026-05-16-row-visibility-design.md`).

## Motivation

Claude session transcripts mix conversational records (user prompts,
assistant text replies) with machinery (tool_use, tool_result, hook
output, system records, queue ops without content). When a user wants
to skim a session for "what was said," the machinery noise dominates.

This feature adds a recipe-level toggle that hides everything except
voice records (and structural anchors that contain voice). It works in
both flat and tree modes, plays correctly with live tail updates, and
relies entirely on the new framework primitives — no destructive
removes, no rebuild, no manual cursor walk.

## Goals

1. A `h` hotkey that toggles voice-only filtering live.
2. A `--show-all` / `--no-show-all` CLI flag setting the initial
   state. Default: `--show-all` (filter off, full transcript visible).
3. Filtering works in both flat and tree modes.
4. Subagent umbrella rows (`#agent:AID`) are always shown, irrespective
   of the filter.
5. In tree mode: any umbrella whose subtree contains a voice record
   (within the same file) stays visible; umbrellas with no voice
   descendants are hidden.
6. Live updates: new records arrive with the correct `hidden` flag set
   at construction; if a new voice record promotes a previously-hidden
   umbrella to voice-bearing, the umbrella is also revealed in the
   same `update_data` batch.
7. Cursor displacement on filter-on is handled by the framework's
   hide-displacement logic — recipe does nothing.
8. Selection state is preserved across toggles. The framework's
   already-WYSIWYG `_select_all_visible` (ctrl-A) gives the correct
   semantic for "select all" against hidden rows.

## Non-goals

- Persisting the toggle state across sessions. CLI flag sets initial
  state; in-session toggles are session-local.
- Replacing or extending search-mode (`/`). The two compose via the
  framework's documented AND rule: `hidden` is absolute, search never
  elevates hidden rows.
- Changing what `_is_voice` considers voice. The existing classifier
  in `recipes/browse-claude:821` is authoritative.
- Filtering at the `_TreeData` level. In-memory state always keeps all
  records; only rendering is filtered, via `Item.hidden`.

---

## API surface

### CLI

```python
parser.add_argument(
    '--show-all', dest='show_all',
    action=argparse.BooleanOptionalAction, default=True,
    help='Show all records (default). Pair with --no-show-all to '
         'start in voice-only mode.',
)
```

The parsed value is written to the module-level
`_FILTER_VOICE_ONLY = not args.show_all` at startup.

### Hotkey

```python
Action('h', 'Toggle voice-only filter', _action_toggle_filter, 'none'),
```

Lives in the action table in the recipe; help text gets one line
documenting it.

### Module-level state

```python
_FILTER_VOICE_ONLY: bool = False
```

Single global, owned by the recipe.

---

## Voice-bearing predicate

Computed on demand per id from data already cached on `_TreeData` —
no extra precomputation pass, no new fields on `_TreeData`.

```python
def _passes_filter(item_id, td):
    """True iff `item_id` should be visible under the current filter.

    Always True when `_FILTER_VOICE_ONLY` is False. Otherwise:
      - voice-leaf (`<path>#<n>`): `_is_voice(td.records[n])`
      - `#prompt:N`: True (a turn opens at a user-voice record by
        definition of how turns are bounded)
      - `#tool:N`: `_is_voice(td.records[N])` (mixed text+tool_use
        case passes; pure tool calls fail)
      - `#span:N`: `any(_is_voice(r) for r in td.span_records.get(N, []))`
      - `#agent:...`: True (subagent umbrella; always shown)
    """
```

The predicate is a few dict / list reads — cheap enough to invoke at
every Item construction and on every dirty-parent check during tail
updates.

---

## Item construction

Every builder that produces an `Item` consulted by a lister sets
`hidden` at construction time:

```python
hidden = _FILTER_VOICE_ONLY and not _passes_filter(item_view)
```

The builders touched:

- `_tree_item` (message leaves in tree mode)
- `_prompt_umbrella_item`, `_tool_umbrella_item`, `_span_umbrella_item`
- `_subagent_pseudo_item` — always `hidden=False`
- The flat-mode message builder inside `_list_messages`

Listers themselves return *all* items; the framework drops hidden
subtrees at render time.

---

## Toggle action — `_action_toggle_filter`

```python
def _action_toggle_filter(ctx):
    global _FILTER_VOICE_ONLY
    _FILTER_VOICE_ONLY = not _FILTER_VOICE_ONLY
    ops = []
    for id_, item in ctx._browser._state._items_by_id.items():
        td = _td_for_id(id_)              # None for synthetic / unknown
        new_hidden = (
            _FILTER_VOICE_ONLY
            and td is not None
            and not _passes_filter_for_id(id_, td)
        )
        if bool(item.hidden) != new_hidden:
            ops.append(mod(id_, hidden=new_hidden))
    if ops:
        ctx._browser.update_data(ops)
    ctx.message(
        f"voice-only filter: {'on' if _FILTER_VOICE_ONLY else 'off'}"
    )
```

Notes:

- Iterates the framework's `_items_by_id` to find every loaded item.
  Items not yet loaded (children of collapsed umbrellas) will be built
  with the correct `hidden` when their parent expands — `_tree_item`
  reads the live `_FILTER_VOICE_ONLY`.
- `mod` is used (never `upsert`) so we cannot accidentally re-create a
  removed id. `KEEP_PARENT` is the default — we do not reparent.
- The cursor moves automatically: the framework's post-`apply_ops`
  hide-displacement walks back through the pre-batch visible list to
  the first row that survives, falling back to row 0 if none. Recipe
  contributes nothing here.
- Selection survives untouched. `_select_all_visible` is already
  WYSIWYG against `visible_items`.

---

## Live updates

### Worker — flat mode (`_push_flat_inserts`)

Each new item is constructed via the existing path; `hidden` flows
from the builder. Under filter:

- Voice records arrive with `hidden=False`.
- Non-voice records arrive with `hidden=True`.
- Subagent rows arrive with `hidden=False` always.

Positioning rules (the existing `after last subagent` / first-slot
behavior) are unchanged.

### Worker — tree mode (`_push_tail_diffs`)

`_read_new_records` keeps its existing `(new_records, dirty_parents)`
contract — no third value, no precomputed transitions.

The tail worker batch construction:

1. For each `parent_id` in `dirty_parents`: if it exists in
   `_items_by_id` with `hidden=True` *and* `_passes_filter(parent_id,
   td)` is now True, emit `mod(parent_id, hidden=False)` first.
   (The only umbrella kind whose predicate can flip from False to
   True via a tail append is `#span:N` — but the recipe doesn't
   special-case kinds; it just compare-and-mods.)
2. For each new child item: emit the existing `upsert` op with
   `hidden` resolved at construction.

These ops go into a single `update_data` batch, parent-first ordering
guaranteed by the loop.

If the filter is off (`_FILTER_VOICE_ONLY == False`), both passes
produce `hidden=False` items; the transition pass is a no-op (parent
was visible already). The same code path serves both modes.

---

## Search-mode interaction

Inherited from the framework: search and `hidden` AND-compose.
Hidden rows stay hidden during a search. If a recipe user wants to
search across machinery, they toggle `h` first; the filter and search
remain independent state.

---

## Edge cases

- **Filter on at startup (`--no-show-all`).** Items are built with
  the correct `hidden` from the first render; no toggle ops needed.
- **Subagent transcript browsing.** When expanding a subagent
  umbrella, `_scan_tree` runs against its `.jsonl` file. The same
  filter rules apply to those items at construction. Nothing special
  happens at the boundary.
- **Empty turn (hypothetical: `#prompt:` with only machinery).**
  Doesn't occur in practice — every turn opens at a user-voice
  record. The predicate returns True unconditionally for `#prompt:`
  ids, which is fine because such umbrellas don't exist.
- **Toggle while a search is active.** Both states compose. The
  framework's AND-rule is the source of truth.
- **Toggle on, all rows hidden (degenerate session with no voice).**
  Cursor parks at row 0 per the framework's documented fallback. The
  recipe message banner reports the new filter state regardless.
- **`ctrl-R` refresh while filtered.** Existing path: clears
  `_TREE_CACHE` + `_TAIL_STATE`, framework re-fetches via
  `get_children(None)`. Builders read the live `_FILTER_VOICE_ONLY`
  and rebuild items with the correct `hidden`.

---

## Test plan

### Unit (`test/unit/test_browse_claude_render.py`)

New `TestVoiceOnlyFilter` class. Coverage:

- `_passes_filter` truth table:
  - voice leaf → True
  - non-voice leaf → False
  - `#prompt:N` → True
  - `#tool:N` whose tool_use record has assistant text → True
  - `#tool:N` on pure tool_use → False
  - `#span:N` with at least one voice record → True
  - `#span:N` with no voice → False
  - `#agent:...` → True
- Item builders set `hidden=True` when `_FILTER_VOICE_ONLY` is on and
  the item fails the predicate; `hidden=False` otherwise.
- `_action_toggle_filter` emits a `mod` batch with the correct
  `hidden` flips for currently-loaded items; no `remove` ops.
- Live tail under filter: non-voice record arrives `hidden=True`,
  voice record arrives `hidden=False`.
- Live tail promotes an umbrella to voice-bearing: the batch contains
  `mod(parent_id, hidden=False)` before the child's `upsert`.
- CLI flag plumbing: `--show-all` (default) → `_FILTER_VOICE_ONLY ==
  False`; `--no-show-all` → `True`.
- Help text contains the `h` line.

### UI (`test/ui/test_recipe_browse_claude.py`)

Three new tests:

- **`test_toggle_filter_hides_non_voice_rows`**: open a session with
  mixed voice + tool records, press `h`, assert the visible list is
  voice + subagent rows only and cursor is parked on a voice row.
- **`test_toggle_filter_restores_full_view`**: press `h` twice,
  assert the full transcript is back and cursor is on the same voice
  row it migrated to on the first toggle.
- **`test_live_tail_under_filter_reveals_voice_bearing_parent`**:
  start with filter on against a fixture whose tail will append a
  voice record under a previously-hidden turn umbrella; trigger the
  worker tick; assert both the parent and the new leaf become
  visible.

### Framework-side regression

No framework changes are required. The framework's
`_select_all_visible` (already WYSIWYG) and hide-displacement hook
are exercised by the existing framework unit suite.

---

## Implementation outline (informational)

1. **`recipes/browse-claude`**
   - Add `_FILTER_VOICE_ONLY` module global.
   - Extend argparse with `--show-all` / `--no-show-all`; set the
     global at startup.
   - Add `_passes_filter(item_id, td)` helper computing the predicate
     on demand from `td.records` and `td.span_records`.
   - Modify every Item builder to set `hidden` accordingly.
   - Add `_action_toggle_filter`; wire `h` into the action table.
   - Extend `_push_tail_diffs` to re-evaluate `_passes_filter` for
     each dirty parent and emit `mod(parent_id, hidden=False)` when
     a currently-hidden parent now passes — parent-first in the batch.
   - Help text update.

2. **Tests** per plan above.

3. **Docs**
   - `docs/recipes.md` (if it documents browse-claude features) gets
     a line under hotkeys.

### Files unaffected

- Framework source. The feature is entirely recipe-level.
- Existing recipe state (`_TREE_CACHE`, `_TAIL_STATE`, message
  classification helpers).
- Search-mode and any other recipe action.

---

## Open questions

None.
