# browse-claude — Subagent wiring update + attachment record support

**Date:** 2026-05-22
**Status:** Draft (pre-implementation)
**Scope:** Recipe-level (`recipes/browse-claude`) only. No framework changes.

## Motivation

Claude Code's session schema evolved in three observable ways since the
recipe was last touched:

1. **Subagent transcripts moved fully out of the main `.jsonl`.**
   Previously `isSidechain: true` records lived inline; now they live
   exclusively under `<session>/subagents/agent-<agentId>.jsonl`. The
   main file has zero `isSidechain` records. Dispatch is recorded as
   an `Agent`/`Task` `tool_use` paired with a `tool_result` whose
   `toolUseResult.agentId` is the disk linkage key. The recipe already
   handles this via `_maybe_link_subagent`, but only when the linkage
   exists — **orphan subagent files (no matching dispatch in the
   main thread) are invisible in tree mode**.
2. **Top-level `attachment` records replaced inline system content.**
   13 sub-types observed in the wild (hooks, edits, perms, queues,
   plan-mode-exit, date-change, mcp deltas, deferred tools, skills,
   file refs, compact refs). Today they render as unknown shape.
3. **`toolUseResult` is gone from subagent jsonls.** Tool results
   inside a subagent live only in `message.content[].content`. The
   recipe's tool-result formatters key on `toolUseResult` and produce
   empty bodies for subagent records.

Plus polish items: orphan visibility, scope card on the synthetic top
row, cross-file header on the subagent umbrella's own preview, and
`attributionAgent` / `slug` surfacing.

## Goals

1. Orphan subagent files appear as bare top-level siblings in tree
   mode, mirroring flat-mode placement.
2. Every `attachment` sub-type renders with an explicit one-liner;
   high-signal sub-types (`hook_success`, `edited_text_file`,
   `plan_mode_exit`) get richer chrome. No sub-type renders as
   "unknown shape".
3. Tool-result rendering inside a subagent transcript falls back to
   the inline `tool_result.content` block when `toolUseResult` is
   absent. Pairing prefers `sourceToolAssistantUUID` when present.
4. Subagent umbrella row is classified as voice (uniform predicate);
   parent-preview composition does NOT descend into it (cross-file
   boundary), but the local dispatch pair (`Agent` `tool_use` +
   `tool_result`) is rendered from the parent file's own records.
5. Subagent umbrella's own preview (when navigated to) begins with a
   one-line file-name header.
6. The synthetic top row's preview is a scope card identifying the
   file: path, sessionId, entrypoint, cwd, gitBranch, slug,
   line/voice counts, mtime. Subagent file variant adds agentId,
   agentType, description, dispatching assistant timestamp.
7. `attributionAgent` surfaces as a row tag on subagent assistant
   rows; `slug` surfaces on the scope card.

## Non-goals

- No "running…" placeholder for in-flight dispatches. If the main
  file has no `tool_result` yet, the umbrella renders only what
  exists (the `tool_use` alone).
- No file-content expansion for `file` / `compact_file_reference` —
  filename only.
- No framework changes. The voice filter, `Item.hidden`, `mod` op,
  preview cache, etc. are all already in place.
- `attachment` records are never voice. Including `file` (per user
  preference — file content is contextual, not speech).
- No change to flat-mode listing of subagents (already correct).

---

## Design

### Orphan subagents in tree view

After `_scan_tree` finishes its main pass, diff the on-disk
subagent set (already enumerated by `_list_subagents_for_session`)
against `td.agent_link.values()` (`agent_id` field). The unmatched
files are orphans.

In tree mode, `_list_session_children(jsonl_path)` returns
`subs + msgs` where `subs` is the full list. Today that's correct
for flat view but wrong for tree view, where matched subagents are
rendered inline under their dispatching assistant turn. The fix:

- Tree-mode lister filters `subs` to **orphans only** (matched
  subagents continue to appear inline via the existing path).
- Orphan items get an additional `tag_style` hint (dim
  `orphan · agent · N msg`) so they're visually identifiable.
- Flat-mode lister is unchanged.

Implementation: split the two paths cleanly — `_list_session_tree`
(orphan-only subs + tree-mode messages) vs `_list_session_flat`
(all subs + flat messages). Routing in `get_children` picks one.

### Attachment classifier and renderers

New module-level helper:

```python
def _classify_attachment(obj):
    """Return (kind_label, oneline_text, body_text_or_None).

    ``obj`` is a top-level record with ``type == 'attachment'``.
    Dispatches on ``obj.attachment.type``.
    """
```

Dispatch table keyed on `attachment.type`. Every sub-type returns a
non-None oneline. Body is None for low-signal types and present for
`hook_success` (stdout), `hook_additional_context` (additionalContext),
`edited_text_file` (name list + diff summary).

Sub-types and their oneliners:

| sub-type | oneline |
|---|---|
| `hook_success` | `hook: <hookName>` |
| `hook_additional_context` | `hook context: <hookName>` |
| `edited_text_file` | `edited: <n> file(s) (+<added>/-<removed>)` |
| `command_permissions` | `permission: <command>` |
| `queued_command` | `queued: <command>` |
| `plan_mode_exit` | `mode → exit plan` |
| `task_reminder` | `task reminder` |
| `date_change` | `date → <YYYY-MM-DD>` |
| `mcp_instructions_delta` | `mcp instructions changed (<n> server)` |
| `deferred_tools_delta` | `deferred tools changed` |
| `skill_listing` | `skills: <n>` |
| `file` | `file: <path>` |
| `compact_file_reference` | `compact ref → <path>` |

Unknown sub-types fall through to `attachment: <type>` so future
additions don't render as junk.

`plan_mode_exit` (and any future mode-switch sub-types) get reusable
"mode switch" chrome — a single rule line with arrow glyph.

#### Voice classification

`_is_voice(rec)` returns False for all `type == 'attachment'`
records. Already the case today (predicate checks `'user'` /
`'assistant'`), but make it explicit so future changes don't
accidentally elevate them.

`_passes_filter`: no change needed; non-voice leaf paths already
return False under filter.

#### Tree placement

Attachments enter via the existing `_resolve_tool_owner` path —
attachments with `attachment.toolUseID` matching a known tool_use
nest under that tool umbrella. Attachments without an owner fall
into the current `turn_direct` / span paths, unchanged.

The `kind` label table that the row chrome reads needs entries for
the new sub-types so the leading glyph isn't "unknown".

#### Item builder

`_tree_item` already routes on `type`; add an `attachment` branch
that pulls the kind label and oneline from `_classify_attachment`.
Title = oneline; tag = kind label. `hidden = not _passes_filter(...)`
under filter (machinery, so hidden).

### Subagent tool_result fallback

`_fmt_tur_subagent` and `_fmt_tur_*` siblings expect
`obj.toolUseResult`. Inside subagent files that key is absent.

Approach: introduce `_extract_tool_result(rec)` that returns a
synthesized `{tool_use_id, content, is_error}` dict, preferring
`rec.toolUseResult` when present, else walking
`rec.message.content[]` for a block with `type == 'tool_result'`.

Update the tool-result formatters to consume this synthesized
shape so the code path is single-source.

Pairing: `_resolve_tool_owner` already considers
`message.content[].tool_use_id` and `attachment.toolUseID` and
`rec.sourceToolUseID`. Add `rec.sourceToolAssistantUUID` as the
strongest signal (explicit back-pointer from the agent runtime),
preferred over the others when present.

### Subagent umbrella in parent preview

`_collect_umbrella_preview` currently has a `#agent:` early-return
that emits a compact stub. Replace with: skip descent (no children
loaded from the subagent jsonl), **but** the local dispatch pair —
the `Agent` `tool_use` row and its `tool_result` row — already
exists in the *parent* file and is rendered through the normal
leaf path because those records sit under the dispatching turn.
No change needed for that part.

The compact stub line that today represents the subagent in the
parent's composed preview can be dropped; the dispatch pair from
the parent file is enough.

`_passes_filter` for `#agent:` now branches into the voice path
(returning True via `_is_voice` rather than the special override).
Behaviorally identical to today — just semantically cleaner.

### Cross-file file-name header

When the user navigates *to* a subagent umbrella, its preview is
composed from its own jsonl. Prepend a single line:

```
── file: agent-<agentId>.jsonl ──
```

Implemented in the public entry-point of `_collect_umbrella_preview`
(or `_preview_umbrella`, whichever owns the top of the cascade):
detect that the umbrella's id contains `#agent:` and prepend the
header before invoking the recursive descent.

Generalises trivially if other cross-file umbrellas appear later
(e.g., compact references that link to prior session files) — the
prepend lives behind a `_resolve_file_for_umbrella(id) → path`
helper.

### Scope card on the synthetic top row

The synthetic top row id is the bare `.jsonl` path. Its preview is
currently the umbrella cascade over all root-level items.

Prepend a scope card:

```
── browse-claude
   path:        <abs path>
   sessionId:   <uuid>
   entrypoint:  cli
   cwd:         /…
   gitBranch:   main
   slug:        <slug>            (omitted if absent)
   lines:       1591
   voice:       62
   mtime:       2026-05-21 23:22
──
```

For subagent files (when navigated to as a top synthetic row), the
card also includes:

```
   agentId:     a6d68f7…
   agentType:   general-purpose
   description: <from meta.json>
```

Source data: read first record of the file for sessionId / cwd /
gitBranch / entrypoint / slug (all are stamped on every record);
`_TreeData` already has line counts and span tallies; mtime from
the file stat; voice count is a `sum(_is_voice(r) for r in
td.records)` computed lazily.

For non-Claude jsonls (defensive — currently unreachable), card
omits Claude-specific fields and shows path + lines + mtime only.

The cascade body comes after the card, separated by a rule.

### `attributionAgent` row tag

Subagent assistant rows carry `attributionAgent: "general-purpose"`
(or other subagent_type). Surface as a row tag with magenta style,
consistent with the existing subagent umbrella tag.

### `slug` on scope card

Already covered above. Show only when present.

---

## Implementation outline

All changes in `recipes/browse-claude` and its tests.

1. **Attachment foundation** — `_classify_attachment` + dispatch
   table. Update `_tree_item` to route `attachment` records. Update
   the kind-glyph table. Tests for every sub-type returning a
   non-junk oneline.

2. **Orphan subagents** — split `_list_session_children` into
   `_list_session_tree` and `_list_session_flat`. Compute orphan
   set after scan. Route in `get_children`.

3. **Tool-result fallback** — `_extract_tool_result(rec)` helper.
   Update `_fmt_tur_*` formatters. Add
   `sourceToolAssistantUUID` to `_resolve_tool_owner`.

4. **Scope card** — `_fmt_scope_card(path, td)`. Prepend in
   `_preview_umbrella` for the synthetic top row.

5. **Cross-file header** — prepend file-name line when umbrella id
   contains `#agent:` (in `_preview_umbrella`).

6. **Subagent umbrella as voice** — `_passes_filter` `#agent:`
   branch routes through `_is_voice` instead of returning True
   directly. `_collect_umbrella_preview` drops the `#agent:`
   compact stub (dispatch pair from parent file is enough).

7. **Polish** — `attributionAgent` row tag; `slug` on scope card;
   richer chrome for `hook_success`, `edited_text_file`,
   `plan_mode_exit`.

## Test plan

Unit tests (`test/unit/test_browse_claude_render.py`):

- `_classify_attachment` returns non-None oneline for all 13 known
  sub-types and falls through gracefully for `attachment: ?`.
- Attachment records are not voice; pass through `_passes_filter`
  as hidden under filter.
- `_extract_tool_result` returns the inline block when
  `toolUseResult` is missing; returns the top-level when present.
- Orphan computation: a fixture with 3 subagent files on disk
  where 2 have matching dispatches → tree-mode returns 1 orphan;
  flat-mode returns all 3.
- Scope card content: every field present when source records
  carry them; missing `slug` omitted gracefully.
- Cross-file header prepends on subagent umbrella preview entry.
- `_passes_filter` for `#agent:` ids returns True via the voice
  path (regression).

UI tests (`test/ui/test_recipe_browse_claude.py`):

- Open a session with an orphan subagent in tree mode; assert it
  appears at the top of the list with the `orphan` tag style.
- Open a session with `attachment` records; assert each one
  renders with a recognizable header (no "unknown shape").
- Top synthetic row preview begins with the scope-card block.
- Navigate into a subagent umbrella; preview begins with the
  file-name header line.

## Sequence (one ticket per item)

1. Attachment foundation (classifier, dispatcher, tree placement,
   one-liners for all sub-types). One ticket.
2. Orphan subagents in tree view. One ticket.
3. Tool-result fallback + `sourceToolAssistantUUID`. One ticket.
4. Scope card. One ticket.
5. Cross-file header + subagent-umbrella-as-voice cleanup. One
   ticket.
6. Polish: `attributionAgent` row tag, `slug` on scope card,
   enriched chrome for `hook_success` / `edited_text_file` /
   `plan_mode_exit`. One ticket (or split if any sub-item grows).

## Open questions

None.
