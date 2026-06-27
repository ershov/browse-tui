# browse-claude — Umbrella-aware preview composition

## Status & context

Builds directly on the just-landed tool-umbrella previews
(`d9ae25c Merge worktree-claude-tool-previews`), which added
`_render_tool_umbrella(item_id)` — a per-tool, request+result *fused*
preview block (Bash+stdout, Edit-as-diff, Read-as-head, MCP/Task/Grep/…).

Today that rich block is used **only** when a `('tool', …)` row is the
direct cursor target. Higher-level previews (a turn/`prompt`, a
`session`, a subagent `agent`) still flatten all the way to leaf records
and ignore the block. This spec changes higher-level composition to use
each node's own rich preview as an atomic unit instead of descending.

## Motivation

A session- or turn-level preview is currently the concatenation of **leaf
bodies** in document order (`_walk_umbrella`). Now that tool umbrellas
render a compact, readable, complete request+result block, descending
past them into raw `tool_use` / `tool_result` leaves produces a worse
preview than the block we already know how to generate. We want the rich
block to appear wherever a tool umbrella is composed into a parent.

## Goals

- Higher-level previews (`prompt`, `session`, `agent`, and any future
  umbrella) compose from their children's **own** previews; a child with
  a rich self-contained preview contributes that block as a unit rather
  than being flattened to its leaves.
- Apply this uniformly at **every** detail level (1–4) — no level-special
  composition logic (Option A; see "Decision: A vs B").
- Keep the cascade free of per-tool knowledge: the walker asks each child
  "do you have your own body?" and otherwise recurses. Tool-specifics stay
  in the renderer dispatch.
- Preserve all existing preview-engine behavior: streaming/batching,
  eager child upserts, `hidden`/`boundary` skipping, and the
  body-vs-standalone chrome distinction.

## Non-goals

- No new rich renderers for `prompt` / `span` umbrellas in this change —
  `tool` is the only node with a rich renderer today; the others keep
  composing from children. (The mechanism extends to them for free
  later.)
- No change to the detail-level semantics or the `1`–`4` / `--detail`
  filter shipped in #1151.
- No new `Item` property. (Considered and rejected — see "Resolved
  decisions".)
- No change to crossing `boundary` subtrees (subagent groups, referenced
  `.md` files) — those are never composed into a parent preview, as today.
- No level-aware block *content* — the block could read `_DETAIL_LEVEL`
  to vary verbosity (it is regenerated per preview), but that is
  explicitly deferred.

## Background: how previews compose today

`get_preview(item_id)` dispatches by tag (`recipes/browse-claude:get_preview`):

- `('msg', path, n)` → `_preview_message` = **body + chrome**
  (`_fmt_chrome`: uuid / timestamps / usage / flags), **no `───` rule**.
- `('prompt'|'tool'|'span', …)` and the cross-file `('session', …)` /
  `('agent', …)` rows → `_preview_umbrella` → `_collect_umbrella_preview`
  → `_walk_umbrella`.

`_walk_umbrella` walks the visible children of an umbrella and, per child:

- `boundary` child → skip entirely (foreign-file subtree).
- `hidden` child → skip (honors the detail filter).
- same-file umbrella (`prompt`/`tool`/`span`) → **recurse** via
  `_collect_umbrella_preview(cid, _state)`.
- `('msg', …)` leaf → render **body-only + a `─── kind ───` rule** via
  `_render_record_with_rule` (no chrome) and yield it.

So only leaves emit text; intermediate nodes contribute nothing of their
own. The rich tool block is wired into `_preview_umbrella` for a
**directly-targeted** tool row only, and the cascade's recursion goes
through `_collect_umbrella_preview` (not `_preview_umbrella`), so nested
tool umbrellas bypass the block.

### Chrome / cache / rule invariants (important)

Two renderings of the same leaf coexist on purpose:

- **Standalone** (direct cursor visit): `_preview_message` = body **+**
  chrome, **no `───` rule**. The framework caches whatever `get_preview`
  returns, so a visited leaf's `Item.preview` cache **includes chrome**.
- **Embedded** (inside a parent cascade): `_render_record_with_rule` =
  body **+ a `─── kind ───` separator**, **no chrome**; the cascade
  deliberately neither reads nor writes the leaf's `Item.preview` cache
  (note at `_walk_umbrella`).

Two consequences this design relies on:

1. **Composition uses a body-only producer and must not reuse children's
   per-item cached previews** (a leaf's cache carries chrome). Chrome and
   the cross-file scope card stay standalone-only wrappers, applied once
   at the top. The parent umbrella's *composed* output is what gets
   cached (on the parent `Item`).
2. **The `───` separator is a composition-time device, not an intrinsic
   property of a node's preview.** Standalone leaves carry no rule;
   composition adds it. Tool blocks will follow the same rule (see
   "Section rule").

## Decision: A vs B (apply at all levels vs raw leaves at level 4)

Two candidates were considered:

- **B** — rich block at levels 1–3, but descend to raw leaves at level 4
  ("all"), preserving level 4 as a raw escape hatch.
- **A (chosen)** — rich block at **all** levels.

A is correct because **detail level governs which *rows* exist in the
tree; previews are a separate axis.** At level 4 the individual
`tool_use` / `tool_result` / hook leaf rows still exist and remain
inspectable: cursor onto a leaf (raw JSON via `_preview_message`), expand
a collapsed tool to reveal its leaves, or use `V` / `E` to dump source.
So A does not remove raw access — it only makes the *composed* parent
preview readable instead of a raw firehose, which is an improvement even
for a level-4 user. A also avoids a `_DETAIL_LEVEL` branch in the
composition path, keeping one rule for all levels.

The one apparent cost of A — a composed block could *omit* sub-content a
raw descent would have shown — is not a real loss: the recipe owns both
the umbrella's contents and its preview renderer, so the block *is* the
umbrella's preview by definition, and every record still exists as an
inspectable row. What the block shows is a content decision (next
section), not a correctness gap.

## Graceful degradation (the `None` fallback)

There is **no completeness gate**. Because the recipe owns both what lives
under an umbrella and the umbrella's preview renderer, the block *is* the
umbrella's preview — it cannot "miss" a sub-item, and "all" detail level
means every record exists as an inspectable **row**, not that every record
must appear in a composed preview. What a block surfaces (including
whether it shows hook/attachment output) is a **content decision owned by
the renderer author**.

`_render_tool_umbrella` keeps its existing narrow `None` semantics, for
the rare cases where it genuinely *cannot* form a block — an unregistered
tool, an in-flight part with no result yet, or a malformed shape /
formatter exception. `None` falls back to descending into children, so a
preview is never blank. These are the only fallback cases; no per-compose
verification is performed.

## Mechanism: uniform recursion via `_node_own_body`

Collapse the walker's per-tag branches into one rule: **for each visible
child, emit its own composed unit if it has one, else recurse.** A single
dispatch function decides "own unit vs recurse" and owns the
composition-time `───` separator, localizing all type knowledge (the
walker no longer mentions `tool`).

```python
def _node_own_body(cid):
    """The node's composed unit — body WITH its section rule — or None to
    compose from children.

    The `───` rule is a composition-time separator (matching leaves):
    standalone previews call the underlying rule-less renderers directly,
    so a standalone tool pane shows no redundant top separator.
    """
    tag = cid[0]
    if tag == 'msg':
        return _render_record_with_rule(cid[1], cid[2])   # already body+rule
    if tag == 'tool':
        block = _render_tool_umbrella(cid)                # rule-less block or None
        if block is None:
            return None                                   # → recurse
        return _rule(f'tool: {_tool_label(cid)}', YELLOW) + '\n' + block
    return None   # prompt / span / cross-file → always recurse
```

`_walk_umbrella`'s body becomes:

```python
for child in children:
    cid = getattr(child, 'id', None)
    if not isinstance(cid, tuple):           continue
    if getattr(child, 'hidden', False):      continue
    if getattr(child, 'boundary', False):    continue
    unit = _node_own_body(cid)
    if unit is not None:
        yield unit + '\n\n'
        _state['count'] += 1
        _flush(_state)
    else:
        yield from _collect_umbrella_preview(cid, _state)   # recurse
```

Notes:

- This is *smaller* than today's walker (the `msg` vs umbrella branches
  merge into one). The `prompt`/`span` recursion is unchanged in effect.
- **Section rule (approach b).** The `─── tool: <Name> ───` separator is
  added here in `_node_own_body`, not inside `_render_tool_umbrella` —
  mirroring how leaves get their rule only at compose time
  (`_render_record_with_rule`) while standalone leaves
  (`_preview_message`) carry none. So a standalone tool pane stays clean.
  `_tool_label(cid)` derives the tool name from `td.tool_use_name_of`
  (one shared helper).
- **DRY.** `_preview_umbrella`'s direct-target tool branch keeps calling
  the rule-less `_render_tool_umbrella` directly; the composed path goes
  through `_node_own_body`, which wraps it. `_render_tool_umbrella` is the
  single shared block producer; the rule lives in exactly one place.

## Invariants to preserve

- **Streaming generator + batching.** Keep `_collect_umbrella_preview` /
  `_walk_umbrella` as generators threading the shared `_state` batch
  window; do not collapse to eager string building (matters for large
  sessions). The `finally:` force-flush and `_refresh_hidden_in_op` at
  flush stay as-is.
- **Eager child upserts.** Children discovered during descent are still
  upserted so later expansion is cheap. When a tool node renders its own
  unit (no descent), its leaf children simply aren't pre-pushed — they
  upsert on actual expansion. Acceptable (perf-only nuance, not
  correctness).
- **Body-only composition / no cache reuse.** Compose from body
  producers; never reuse a child's standalone/cached preview; chrome +
  scope card are top-level wrappers; the `───` rule is added at compose
  time only.

## Detail-level interaction

No special handling needed. `visible_children` already excludes `hidden`
rows via `_passes_filter`, so:

- A tool umbrella's min level is 2 (`tools`). At level 1 (`voice`) the
  tool row is hidden → not composed (correct).
- At levels 2–4 the tool row is visible → its rich block composes in.
- Nothing visible is lost at level 4 — every record is still its own
  inspectable row — so there is no need to descend to leaves there.

## Edge cases

- **`_render_tool_umbrella` returns `None`** (unregistered tool, in-flight
  part with no result, malformed shape): fall back to descending, exactly
  today's behavior. No information lost.
- **Collapsed vs expanded parent.** Composition is independent of
  expansion — a collapsed turn/tool still previews its whole subtree.
  Under A this preview is the rich block; expanding reveals the raw leaf
  rows for those who want them.
- **Boundary subtrees** (subagent group, `.md` file): still skipped in
  composition.
- **Mixed turn** (user voice + assistant text + tool block + …): the
  `prompt` recurses; voice/text leaves emit their units; the tool child
  emits its block — yielding exactly the desired interleaving.

## Testing

Prefer headless `Browser` / direct-call unit tests where possible; one
pty UI test for the end-to-end composed preview. Per repo convention, run
the **full** `./run-tests-parallel.sh` for preview/rendering changes.

- Composition uses the block: a `prompt`/`session` preview of a turn
  containing a registered tool shows the fused block and **not** the raw
  `tool_use` / `tool_result` leaf bodies.
- Fallback: when `_render_tool_umbrella` returns `None` (unregistered
  tool, in-flight result), the same preview descends to leaves.
- No chrome in composed output (between blocks); chrome still present on a
  standalone leaf preview.
- Section rule: a composed tool block carries a `─── tool: <Name> ───`
  separator (consistent with sibling leaves); a standalone tool pane does
  **not**.
- Level interaction: tool block composes at level ≥ 2; absent at level 1.
- Boundary not crossed: a session preview does not inline a subagent
  transcript.
- DRY: a directly-targeted tool row and the same tool composed inside its
  turn produce identical block text (modulo the composed separator).

## Rollout

Small, cohesive change in `recipes/browse-claude` (one commit per ticket;
review subagent for the non-trivial composition change):

1. **`_node_own_body` + walker refactor**: introduce the dispatch (body +
   compose-time rule), add `_tool_label`, collapse `_walk_umbrella`'s
   branches, and keep `_preview_umbrella`'s direct-target tool case on the
   rule-less `_render_tool_umbrella` — with composition tests.
2. **MANUAL docs** touch-up if preview behavior is documented anywhere
   user-facing (verify; may be none).

## Resolved decisions

- **A over B** — rich block at all levels; raw stays reachable via leaf
  rows + `V`/`E`; previews are a separate axis from row visibility.
- **No completeness gate** — the recipe owns both umbrella contents and
  renderer, so the block *is* the preview; `None` stays only as the rare
  can't-render fallback (unregistered / in-flight / malformed). Block
  content (incl. hooks) and any future level-adaptation are the renderer
  author's call.
- **Section rule = approach (b)** — the `─── … ───` separator is
  composition-time, added in `_node_own_body`; `_render_tool_umbrella`
  stays rule-less so standalone tool panes match standalone leaves.
- **No new `Item` property** — a `boundary`-style flag is more machinery
  for one consumer and can't capture the dynamic `None`-fallback anyway;
  the `_node_own_body` dispatch keeps the walker tool-agnostic without it.
  Revisit only if a second node type gains a rich renderer.

## Follow-on polish: prompt divider + Bash coloring

Two readability tweaks to composed previews, built on the same
`_render_record_with_rule` / tool-block paths (same branch, after the
composition change above).

### Turn-prompt divider — full-width dark-blue rule

Make each turn-opening user prompt instantly scannable in a multi-turn
composed preview by giving ONLY its `── user ──…` divider a dark-blue
(`48;5;17`, the recipe's existing dark blue) background spanning the full
preview width. All other rules keep the fixed ~60-wide, fg-only form.

- Extend `_rule(title, color=None, *, width=None, bg=None)`: `width`
  overrides the fixed 60 (size to `_BROWSER.preview_width`, fixed fallback
  when headless); `bg` paints a background SGR behind the whole bar.
- In `_render_record_with_rule`, when `_is_turn_root(obj)`, emit the
  full-width dark-blue rule instead of `_rule(kind, YELLOW)`. Everything
  else is unchanged.
- It's a single line, so the band is gap-free, and it appears only in
  composed previews (a standalone leaf carries no rule). The prompt body
  is rendered normally — only the divider is colored.

### Bash command — frame-less syntax coloring on the grey band

Syntax-color the `$ command` with the library's bash lexer (no code
fence / no `─── bash ───` chrome), keeping the existing grey `CMD_BG`
band gap-free.

- Add a `_bash_color(cmd)` helper mirroring `_json_color`:
  `_m2a_render(cmd, CMD_BG_PARAMS, M2A_CONTEXT_CODE_BASH, _M2A_DocumentState())`,
  falling back to a flat `WHITE` wrap when md2ansi_lib (or the bash
  context) isn't importable. `current_style = CMD_BG_PARAMS` (the grey
  params, e.g. `"48;5;236"`) so token resets land back on the band, not
  `\x1b[0m`. Add `M2A_CONTEXT_CODE_BASH` to the guarded md2ansi_lib import;
  split `CMD_BG`'s escape into a reusable `CMD_BG_PARAMS`.
- In `_fmt_tool_umbrella_bash`, colorize the whole command **once**
  (correct multi-line lexing), then split and emit each physical line on
  `CMD_BG` with the existing `$ ` / `  ` prefix. Re-asserting the band per
  line keeps it gap-free over leading indentation — verified that "issue
  the style once + reset" leaves the leading whitespace of indented
  continuation lines **unbanded**, which the current per-line band avoids.
- Single-line commands (the common case) are identical to "issue once".
