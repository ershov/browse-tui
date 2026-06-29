# browse-claude — six detail levels (add "summary" + "edits")

## Status & context

Extends the 4-level detail filter (#1151) — keys `1`–`4`, `--detail`,
`_record_min_level` classifier — and sits alongside the composed-preview
work (#1184) and the turn-divider banding (`d2d50fd`, which paints the
turn-root prompt on a dark-blue band via `_band_block`). Built on current
`main` (`1d0cc9e`). The classifier and the rendering are **orthogonal**:
this change is entirely in the *classifier* (which records are visible at
a level); it touches no rendering/banding code.

## Motivation

Two workflows the current 4 levels don't serve well:

- **Skim** — "what did I ask, and what did the agent conclude" — without
  the intermediate assistant chatter. (Real session: 62 `end_turn` vs 451
  `tool_use` records, so this is a large signal-to-noise win.) The user
  wants this as the **default**.
- **Review changes** — voice plus *just the edits/writes*, without Read /
  Bash / Grep noise. The edit umbrellas already render rich diffs.

## The six levels

A record's MINIMUM visible level. Monotonic nesting (1⊂2⊂3⊂4⊂5⊂6), so the
single-min-level classifier still applies.

| # | name | adds (vs the level below) | maps from |
|---|------|---------------------------|-----------|
| 1 | **summary** | turn-root user prompts + assistant `end_turn` responses | NEW (default) |
| 2 | voice | all remaining `_is_voice` (intermediate assistant text, AskUserQuestion, inter-agent voice) | was 1 |
| 3 | **edits** | Edit / Write / NotebookEdit / MultiEdit tool umbrellas | NEW |
| 4 | tools | all other tool machinery + `_LIVE_EXTRAS` (turn_duration, api_error) + thinking | was 2 |
| 5 | detailed | `_L3_USEFUL` curated metadata | was 3 |
| 6 | all | everything (isMeta, empty-thinking, unknown, raw leaves) | was 4 |

Default stays the literal `_DETAIL_LEVEL = 1` — only its *meaning* changes
(voice → summary).

## Classifier changes (`_record_min_level`)

Stays **per-record / stateless** — no turn scanning (per the user's "keep
it simple"). New helpers:

- `_is_end_turn(rec)` — `rec['type']=='assistant'` and
  `(rec.get('message') or {}).get('stop_reason') == 'end_turn'`.
- `_is_edit_tool_record(rec)` — an assistant record carrying a `tool_use`
  part whose `name` is in `_EDIT_TOOLS = {'Edit','Write','NotebookEdit','MultiEdit'}`.

Rewritten body (min level):

```
not a dict                                  → 6
isMeta                                      → 6
_is_voice(rec):
    _is_turn_root(rec) or _is_end_turn(rec) → 1   # summary
    otherwise                               → 2   # voice
assistant & _is_empty_thinking_only(rec)    → 6
type in {user, assistant}:
    _is_edit_tool_record(rec)               → 3   # edits
    otherwise                               → 4   # tools
(system, turn_duration|api_error)           → 4   # _LIVE_EXTRAS
(type,subtype) in _L3_USEFUL                → 5   # detailed
otherwise                                   → 6   # all
```

So the only renumber of existing tiers: `_LIVE_EXTRAS` 2→4, `_L3_USEFUL`
3→5, isMeta/empty-thinking/unknown 4→6, plain voice 1→2.

### `_passes_filter` touch-ups

- **`mode 1` / summary** falls out for free: a turn's `prompt` umbrella
  always contains its turn-root (level 1) so it stays visible; its
  composed body shows the prompt (1) + the `end_turn` leaf (1) and hides
  intermediate voice (2) / tools. A turn with no `end_turn` shows just the
  prompt; a node that is neither a prompt nor an `end_turn` isn't shown at
  level 1. No fallback computed.
- **`mode 3` / edits**: a `('tool',…)` umbrella is gated by its assistant
  record's `_record_min_level` (edit-set → 3, else → 4). The paired
  `tool_result` / hook **leaves stay at level 4** (no per-result pairing
  in the stateless classifier) — the edit umbrella's rich block already
  fuses the diff+result, so a mode-3 *composed* turn shows the complete
  change; only the raw result leaf row waits for level 6.
- **subagent-wrap promotion**: the existing tool-branch rule
  (`_tool_umbrella_wraps_subagent` → visible) meant "voice-equivalent";
  under renumbering that becomes **level 2** (was the old voice=1), so a
  Task-with-subagent umbrella shows from `voice`, not `summary`. (Pure
  renumber of the existing semantic — no new behavior.)

## Keys / CLI / flash

- `_DETAIL_LEVEL_NAMES = {1:'summary', 2:'voice', 3:'edits', 4:'tools', 5:'detailed', 6:'all'}`; aliases auto-derive (unchanged mechanism).
- Add `Action('5'…)` and `Action('6'…)`; relabel `1`–`4`. `_set_detail_level` and the flash are already level-generic.
- `_parse_detail_level` accepts `1`–`6` + the six aliases; default 1. Update the usage/help block and `_DETAIL_LEVEL` comment.
- MANUAL docs (`MANUAL/recipes/browse-claude.md`, `cli.md` / `recipes.md` if they enumerate the levels): describe all six + the new default.

## Rendering interaction (no changes needed)

The banding work (`d2d50fd`) renders the turn-root prompt on a blue band;
`end_turn` responses are non-turn-root assistants → the normal YELLOW
`── assistant ──` rule (unbanded). At summary level a composed turn reads
as: banded prompt + final response. This is purely a consequence of the
classifier; `_render_record_with_rule` / `_band_block` / `_md_voice` are
untouched.

## Non-goals

- No turn-aware "last voice if no end_turn" fallback for summary (the user
  explicitly chose the simple per-record rule).
- No `tool_result`↔tool pairing for the edits level.
- No rendering / banding / divider changes — classifier only.
- No new detail-level *behavior* for subagent promotion beyond renumber.

## Testing

- **Renumber** every existing test that asserts a specific level number or
  name (the 4-level suite) to the new mapping. This is the bulk of the
  churn — `grep` for `_DETAIL_LEVEL`, `_record_min_level`, the level names,
  `--detail`, and `Action('1'..'4'`.
- **Summary (1):** a turn-root user prompt → 1; an assistant `end_turn`
  → 1; intermediate assistant voice / AskUserQuestion / inter-agent voice
  → 2. A composed turn preview at level 1 contains the prompt + the
  `end_turn` response and NOT intermediate voice or tools. A turn lacking
  `end_turn` shows only the prompt.
- **Edits (3):** an Edit/Write/NotebookEdit umbrella → 3; a Bash/Read/Grep
  umbrella → 4; a composed turn at level 3 shows voice + edit blocks and
  hides other tools.
- **Renumbered tiers:** `_LIVE_EXTRAS`→4, `_L3_USEFUL`→5, isMeta/
  empty-thinking/unknown→6.
- **Keys / CLI:** `1`–`6` set the level (UI test driving each); `--detail`
  maps `1`–`6` and each alias, rejects bad input; default is summary.
- Run the FULL `./run-tests-parallel.sh` (preview/rendering + the pty UI
  suite).

## Migration risk / co-development note

This area is **actively co-developed** on `main` (recent concurrent
commits `227320d` "Clean up details mode 2", `d2d50fd` divider banding).
The classifier and level constants may shift under us; rebase / re-verify
against `main` before merging, and expect the test renumber to be the
largest, most conflict-prone part.

## Rollout (one commit per ticket; review subagent)

1. **Classifier + helpers**: `_is_end_turn`, `_is_edit_tool_record`,
   `_EDIT_TOOLS`; rewrite `_record_min_level` to the 6-tier mapping;
   renumber the `_passes_filter` subagent-wrap promotion; unit tests for
   every tier (incl. summary + edits) and the composed-preview filtering.
2. **Keys, CLI, names, docs**: `_DETAIL_LEVEL_NAMES` (6), bindings `1`–`6`,
   `_parse_detail_level` (1–6 + aliases), default-as-summary, help text,
   MANUAL; context-menu + UI + arg-parse tests.
