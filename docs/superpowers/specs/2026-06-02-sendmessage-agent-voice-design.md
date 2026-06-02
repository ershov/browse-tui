# browse-claude: render & voice the SendMessage inter-agent loop

- **Date:** 2026-06-02
- **Status:** approved (brainstorm) — ready for implementation plan
- **Ticket:** #643
- **Recipe:** `recipes/browse-claude`

## Motivation

The newest Claude Code lets agents talk to each other. A leader dispatches
workers (the `Agent` tool) and then converses with them via the
`SendMessage` tool. `browse-claude` has no special handling for this, so
the exchange reads poorly — and one half is actively mis-attributed.

A single inter-agent exchange is **three** records in the leader's
transcript (sample: session `b94b131c-…`):

1. **Outbound** — an `assistant` record carrying a `tool_use` part with
   `name == "SendMessage"`. Input fields: `recipient` (a.k.a. `to`,
   the worker's agent id), `summary` (short label), `message` (a.k.a.
   `content`, **markdown** — the actual thing said). Fields are
   duplicated; prefer `recipient`/`message`, fall back to `to`/`content`.
2. **Ack** — the `tool_result` for that call. `toolUseResult` is a status
   receipt: `{success: bool, message: str}` (e.g. *"Agent … had no active
   task; resumed from transcript in the background; you'll be notified
   when it finishes. Output: /tmp/…/tasks/<id>.output"*). Not the reply.
3. **Inbound reply** — a later `user` record whose text content begins
   with `<task-notification>`. It wraps `<task-id>` (the worker agent id),
   `<tool-use-id>` (matches the originating SendMessage's tool_use id),
   `<output-file>`, `<status>` (e.g. `completed`), `<summary>`, and
   `<result>` (**markdown** — the worker's report).

### Current (wrong) behavior

- The **outbound** SendMessage is a `tool_use` with no text part, so
  `_is_voice` returns False → no stripe, hidden by the voice-only filter.
  It reads as undifferentiated machinery.
- The **inbound** `<task-notification>` is a `user` text record, so
  `_is_voice` returns True and it gets the **human** stripe (235). A
  worker's report is painted as if the human said it. In tree mode it also
  opens a new `<prompt>` turn, masquerading as a human prompt.

## Goals

Treat the whole leader↔worker exchange as first-class **agent voice**:
render the markdown, classify it as voice (so it survives the voice-only
filter and feeds latest-voice navigation), and give it its own colors —
distinct from the human and from the leader's own assistant turns, and
distinct per direction.

## Design

### 1. Detection helpers

- **Outbound:** an `assistant` record with a `tool_use` part where
  `name == "SendMessage"`. A small accessor returns `(recipient, summary,
  message)` with the `to`/`content` fallbacks.
- **Ack:** routed by the `toolUseResult` key-set being exactly
  `{success, message}` (skill acks carry `commandName`; nothing else
  matches just those two keys — see `_fmt_tool_use_result`).
- **Inbound:** a `user` record whose text content `lstrip()` starts with
  `<task-notification>`. A new `_parse_task_notification(text)` peels the
  tagged fields into a dict `{task_id, tool_use_id, output_file, status,
  summary, result}` (tolerant of missing tags; `result` kept as raw
  markdown). Detection is by the wrapper prefix, not a full XML parse.

### 2. Kinds, voice, and the two stripes

- New record kinds: **`agent-send`** (outbound) and **`agent-reply`**
  (inbound).
- `_kind_of` returns them:
  - an `assistant` record whose salient content is a `SendMessage`
    tool_use → `agent-send`;
  - a `user` record that is a `<task-notification>` → `agent-reply`.
  (Records that are plainly human prompts or other tools are unchanged.)
- `_is_voice` → True for both new kinds: mirror the existing
  `AskUserQuestion` branch for the outbound tool_use; add a
  task-notification check for the inbound user record.
- `_ROW_BG_FOR_KIND`: add two new distinct dark stripes in the same
  green family (so they read as one "agent voice" channel, split by
  direction): **`agent-send` → 22 (dark green)**, **`agent-reply` → 23
  (dark green-blue / teal)**; tunable. Both distinct from human (235) and
  assistant (17).
- `_TAG_STYLE_FOR_KIND`: add tag-text styles for both kinds.

### 3. Formatting (the custom rules)

- **Outbound** `_fmt_tool_use_send_message(inp)` (registered in
  `_FMT_TOOL_USE`): full preview = header line `→ <recipient> · <summary>`
  followed by the `message` rendered as markdown via `_md_voice`.
  One-liner (`_tool_use_one_line`): `→ <recipient>: <summary>`.
- **Ack** `_fmt_tur_send_message(tur)`: compact status, e.g.
  `✓ delivered · <message>` / `✗ <message>`, with the long
  `Output: /tmp/…` path trimmed. Dispatched from `_fmt_tool_use_result`
  via the `{success, message}` exact-key check.
- **Inbound** task-notification:
  - `_summarise_message` one-liner: `← <task_id> · <status> · <summary>`.
  - full preview: a small header (`status`, `summary`, `output_file`)
    then `<result>` rendered as markdown via `_md_voice`.
- Direction is always explicit via the `→` / `←` glyph plus the agent id,
  independent of color (so it survives no-color terminals).

### 4. Flat vs tree mode

- **Flat mode** (`_list_messages`): purely sequential — the three records
  are three chronological rows, each with its new kind → stripe → one-liner
  → drill-in preview. No grouping needed; falls out of §2–§3 directly.
- **Tree mode:**
  - The outbound `SendMessage` tool_use and its ack already group under a
    `<tool:SendMessage>` umbrella (standard tool_use + tool_result
    nesting). The tool umbrella row carries the `agent-send` stripe (same
    hook subagent umbrellas use for their stripe).
  - The inbound reply stays **inline at its real chronological position**
    as its own `agent-reply` voice row. We do **not** force it under the
    originating `<tool:SendMessage>` umbrella. Rationale: unlike a
    subagent transcript (a separate file with no place in the main
    timeline), the task-notification is a real timeline record;
    re-parenting it would distort chronology, hurt latest-voice
    prominence, and require fragile long-distance `tool_use_id` matching.
  - The `<task-notification>` must **not** open a human `<prompt>` turn
    root. It renders as an `agent-reply` row. (Implementation note: this
    is the one tree-scan behavior to adjust — a task-notification user
    record should not be treated as a turn-opening human prompt.)

## Out of scope (YAGNI for v1)

- **Cross-link navigation** between a reply and its originating
  SendMessage via `tool_use_id` (a "jump to the message this answers"
  affordance). Worth doing later; not now.
- **Reading the `/tmp/…/tasks/<id>.output` file.** The `<result>` markdown
  is already in the record; we render that.
- **Multi/edge notifications** (queued-vs-resumed wording variants,
  delayed/missing replies). We format whatever fields are present and
  degrade gracefully; we do not try to reconcile sends with replies.

## Testing

- **Unit:**
  - `_parse_task_notification` — field extraction, missing-tag tolerance.
  - `_fmt_tool_use_send_message` / `_tool_use_one_line` — header + markdown,
    `to`/`content` fallbacks.
  - `_fmt_tur_send_message` — success/failure, path trimming, `{success,
    message}` routing in `_fmt_tool_use_result`.
  - `_is_voice` — True for an assistant-with-SendMessage and for a
    task-notification user record; unchanged for ordinary tool calls.
  - `_kind_of` — `agent-send` / `agent-reply` classification.
  - `_summarise_message` — task-notification one-liner.
- **UI (tmux):** a fixture session containing a full round-trip (assistant
  `SendMessage` tool_use + `{success, message}` ack tool_result +
  `<task-notification>` user record). Assert:
  - the recipient/summary header and rendered markdown appear;
  - the outbound and inbound rows carry their two distinct stripes/kinds;
  - the voice-only filter keeps the outbound and inbound rows and hides the
    ack;
  - in tree mode the task-notification is an `agent-reply` row (not a new
    human prompt turn).

## Risks / notes

- The `{success, message}` ack discriminator is shape-based. It's
  specific enough today (no other `toolUseResult` is exactly those two
  keys), but if a future tool collides we may need to thread the owning
  tool name into `_fmt_tool_use_result`.
- Stripe color choices (22 dark green / 23 dark green-blue) are a first
  pass; adjust against the real palette during implementation if they
  clash with existing rows.
