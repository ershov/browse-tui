# Item-verb dispatch: per-item action overrides, global hooks, forwardable defaults

**Date:** 2026-06-14
**Status:** discussion captured — direction agreed in principle, NOT yet
scheduled. Records the design exploration around how a recipe customizes
"activate / drill into this row" (e.g. browse-fs launcher rows opening
browse-md). The concrete `--quit-on-scope-up` flag that came out of the same
conversation is being implemented separately (see its plan tickets); this doc
is only the *hook/dispatch* design that we chose to defer.

## Motivation

Streamlining the sub-recipe experience raised two UX ideas: bind drill-down
(Alt-Down / scope-in) to launching a sub-recipe (so a launcher row opens in
browse-md on both Enter and Alt-Down), and have the sub-recipe quit when
scope-up is attempted at its top level (so Alt-Up backs out to the parent).
The second is a self-contained flag (`--quit-on-scope-up`, being built now).
The first opened a broader question: **what is the right, reusable mechanism
for a recipe to override what a key does *on a specific row*** — without
re-implementing framework behavior. browse-git (and possibly browse-claude)
will want the same, so it should be framework-owned.

## Conceptual model: layered item-verb dispatch

Some keys are **item-verbs** — they act on the focused row:

- `open` (Enter / activate)
- `drill` (Alt-Down / scope-in)
- possibly `expand` later

These are distinct from **navigation verbs** (up/down/page/home/end), which
move the cursor regardless of the row and are NOT per-item overridable.

For each item-verb, resolution is a three-layer fallback:

```
per-item override  →  recipe-global hook  →  framework default
   (Layer 2)            (Layer 1)              (Layer 0)
```

The three points discussed map onto these layers; they compose rather than
compete.

### Layer 0 — framework defaults as forwardable APIs (point 1)

Make each default key action a callable public API so a recipe that *does*
override a key globally can forward to the original instead of
re-implementing it.

- Partially exists already: `ctx.scope_into` / `scope_out` / `expand` /
  `collapse` / `nav_home` / `nav_end` / `collapse_all` / `expand_subtree`.
- Gap: the *action-handler* behavior (e.g. `_scope_down` does scope-in +
  cursor-land + fetch-if-uncached + `_last_expanded` rebaseline) isn't all
  exposed; `ctx.scope_into` is a subset.
- Scope: a separate, independently-useful **epic** — fill the gaps and
  document the set as *the* override-forwarding surface.
- Note: **not a prerequisite** for drill-to-launch, because per-item dispatch
  (Layer 2) avoids the forwarding problem entirely (see below). Schedule it on
  its own merits (it's the right tool for *global* key wrapping).

### Layer 1 — recipe-global hooks (point 2)

A recipe-wide handler for a verb, branching on row type internally.

- `on_enter` already IS the global `open` hook.
- `on_scope_down(ctx, item) -> bool` would be the global `drill` hook
  (pre-intercept: return True = handled, else framework scopes branches /
  no-ops leaves). Useful when a recipe wants to intercept *all* drills.
- Decision: **keep in the model, defer building** `on_scope_down` until a
  global drill-interception need appears — Layer 2 + the existing `on_enter`
  cover the launcher case without it.

### Layer 2 — per-item action overrides (point 3) — the leading direction

The focused row may carry overrides for item-verbs; the framework consults
them before the global hook / default.

**Verbs:** start with `open` + `drill` (exactly what the launcher needs);
`expand` extensible later.

**Expression.** `Item` is already **non-slotted by design** (recipes attach
arbitrary domain attributes). Attributes set post-init are NOT dataclass
fields, so a callable override does NOT affect `Item.__eq__` / `__hash__`,
caching, or `update_data`. So "callables on data" is a non-issue
*mechanically*; the usual data/behavior objection is largely moot here.
Still, formalize as declared fields (e.g. `on_open` / `on_drill`,
`init=False, compare=False, repr=False`, default `None`, signature
`(ctx, item) -> None`) so they're documented and discoverable rather than a
loose convention.

**Why per-item beats a global key override (the decisive point).** A global
*override* of a key forces the recipe to own the whole key and re-handle every
other row (forward to the default) — which is the only reason Layer 0 would be
needed here. A **per-item** override is scoped to the row: the framework
checks the focused item, and if it has no override it runs its own default. So
un-overridden rows flow to the default with **no recipe forwarding at all**.
That's cleaner than a key override AND removes the Layer-0 dependency for this
feature.

**Precedence.** per-item `on_open` → `on_enter` → print-exit default;
per-item `on_drill` → (global drill hook, if it exists) → scope-in / no-op
default. Per-item is most specific, so it wins.

**What browse-fs becomes (simpler):** drop the `on_enter` dispatcher; set
`on_enter='action:e'` (edit is the plain default) and attach
`on_open = on_drill = launch` to launcher rows only. Enter on a launcher →
launch; Enter on a file → edit; Alt-Down on a launcher → launch; Alt-Down on a
dir → scope. One behavior, co-located with the row, reached by both keys.
browse-git reuses the mechanism unchanged.

**Honest costs.** (a) Discoverability — global help still says "Alt-Down:
scope into item" while a launcher launches (the same mismatch any internal
branching has; a future extension could let an item override its help label
too). (b) "What does Enter do?" is answered in two places (global `on_enter`
+ per-item overrides) — more readable per-row, slightly less centralized.
Neither is a blocker.

## Recommendation

When the drill-to-launch feature is scheduled, build the **per-item layer for
`open` + `drill`** (formalized `on_open` / `on_drill` Item fields; framework
consults them at `_scope_down`'s leaf point — `070-actions.py` `if not
has_children: return` — and in the Enter / `on_enter` path; precedence
per-item → global → default), and wire browse-fs's launcher rows to it. Keep
`on_enter` as the global `open` layer. Treat Layer 0 (forwardable defaults) as
a separate foundational epic, and Layer 1's `on_scope_down` as
designed-but-unbuilt until a global-drill need appears.

## Open decisions (to settle before implementation)

1. Per-item layer now, or start with the global `on_scope_down` hook and defer
   per-item?
2. Verb set: `open` + `drill` only to start, or include `expand`?
3. Field naming/shape: `on_open` / `on_drill` callable `(ctx, item) -> None`,
   or other names?
4. Layer-0 epic scheduling: now (foundational) or after the launcher work
   (given it's no longer a prerequisite)?

## Relationship to current work

- `--quit-on-scope-up` (sub-recipe quits on scope-up at the top level) is
  being implemented now as its own flag; it is independent of this dispatch
  design.
- Until the per-item layer lands, browse-fs's launcher rows are activated by
  Enter via the recipe's `on_enter`; Alt-Down does not yet launch.
