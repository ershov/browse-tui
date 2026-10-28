# browse-agents — design

Parts-built transcript browsers for Claude Code, Codex CLI and OpenCode.
Source lives here; `build-agents.sh` generates one single-file recipe per
agent into `recipes/`. The frozen `recipes/browse-claude` is the ancestor
and stays untouched.

## Goals and non-goals

Goals (variant 1b, this epic):

* One generated recipe per agent: `recipes/browse-agents-claude`,
  `recipes/browse-agents-codex`, `recipes/browse-agents-opencode`. Each is a
  self-contained `#!/usr/bin/env -S browse-tui --run-py` script, committed
  like `browse-tui` itself.
* All view code (tree fold, detail levels, summaries, tool previews,
  cross-links, voice jumps, markdown subtree, dir actions, menus, Browser
  wiring, live tail worker) written once in `common/`.
* Per-agent code limited to a store adapter, a parser to the normalized
  record model, a tail poller and optional extras.
* No behaviour regression in the Claude flavour relative to `browse-claude`.
* Python 3.10+, stdlib only, no runtime import of sibling modules beyond the
  optional `md2ansi_lib` / `md_doc` plugins browse-claude already uses.

Non-goals now:

* Variant 3b (one merged `recipes/browse-agents` grouping all agents by cwd
  with `--agent`) is a later epic (#1272). The contract below is written so
  that 3b only needs a registry keyed by agent name in front of the same
  functions.
* Editing or deleting sessions; inotify watching; a shared runtime library.

## Directory layout

```
recipes/browse-agents-src/
  DESIGN.md
  common/      015-imports 020-helpers 030-model 040-tree 050-preview-core 050-render 070-links 080-actions 085-menus 090-main
  claude/      010-prelude 025-store 035-parse 045-tail 055-extras
  codex/       010-prelude 025-store 035-parse 045-tail 055-extras
  opencode/    010-prelude 025-store 035-parse 045-tail 055-extras
build-agents.sh          common/ + <agent>/ → recipes/browse-agents-<agent>
build-all.sh             ./build-tui.sh then ./build-agents.sh
```

Numbering convention:

* Common parts use multiples of 10 (`020-helpers`, `030-model`, `040-tree`,
  `050-preview-core`, `050-render`, `070-links`, `080-actions`, `090-main`)
  plus `015-imports` and `085-menus`; two common parts may share a number
  when they sort before the agent part that extends them. `050-preview-core`
  sorts before `050-render` by name and `050-render` depends on it (helpers
  such as `_rule` / `_md_voice` / `_record_at`), so neither may be renamed
  in a way that flips that order; `050-render` holds the registries
  `055-extras` fills.
* Agent parts use the 5s and sort right after the common part they extend:
  `025-store` after `020-helpers`, `035-parse` after `030-model`, `045-tail`
  after `040-tree`, `055-extras` after `050-render`.
* Every agent owns `010-prelude` (shebang and module docstring); it sorts
  first and nothing in `common/` uses number 010.
* Counterpart files carry the same number in every agent dir.
* Only Python module-level evaluation order matters: registries, constants,
  class bases, and the final `main()` call in `090-main`. A function body may
  reference a name defined in a later part.

## Build mechanism

`build-agents.sh` is `build-tui.sh` applied per agent dir:

1. For each `<agent>/` under `recipes/browse-agents-src/` other than
   `common/`, list `common/[0-9]*` and `<agent>/[0-9]*` and sort with
   `LC_ALL=C sort -s -t/ -k2,2` (by basename; stable, so on an equal basename
   the common part precedes the agent part).
2. Concatenate the parts with `# SOURCE START: <dir>/<file> {{{` /
   `# }}} # SOURCE END: <dir>/<file>` markers. The first part's shebang passes
   through untouched and a `# THIS IS A GENERATED FILE - DO NOT EDIT!` banner
   follows it.
3. A part named `NNN-name+.ext` that is executable is run and its stdout is
   the part body (same `+` convention as `src-tui/`).
4. Output is written to `recipes/browse-agents-<agent>.generated`, then moved
   over `recipes/browse-agents-<agent>` and set to mode 755.
5. An agent dir with no numbered parts is skipped with a note on stderr.

`run-tests.sh` calls `./build-all.sh` so both the binary and the recipes are
current before any suite runs. Generated recipes are committed in the same
commit as the parts that changed them.

## Store contract

`common/030-model` lists the contract as plain module-level function names;
each agent's `025-store`, `035-parse` and `045-tail` define them and common
code calls them by name. `common/` never touches agent paths, line numbers,
SQL or raw record shapes: records reach it as `Record` instances (next
section). The parser that builds them (Claude: `_parse_record(raw, key)`)
is internal to the agent's `035-parse`, not a contract function; common
only sees its output through `store_tail_open` / `store_tail_poll`.

Keys: `project_key`, `session_key` and `child_key` are opaque to common
code (hashable, orderable within their kind, stable across runs; for Claude
the project dir, the transcript path and the agent id). `record_key` is
**not** opaque: it is a dense 0-based ordinal in transcript order, which
common code uses to index its cached `_TreeData.records` list, to walk
records in order and to form `('msg', session_key, record_key)` ids. The
store maps it back to its native locator (Claude and Codex: the line
number; OpenCode: `(message_id, index)`) and may yield `None` placeholders
to keep the ordinals dense (Claude: blank or unparsable lines).

Listings return ready `Item`s (title, tag, `boundary`, per-row attrs such
as `agent_id` / `mtime`), not plain dicts: the session and child rows are
presentation the store owns. Common only marks a child row it could not
link to a dispatch site (`is_orphan`, `tag_style='dim'`) and asks the
store to re-tag it (`store_mark_orphan`).

| Function | Returns |
|---|---|
| `store_name()` | `'claude' \| 'codex' \| 'opencode'`; `browse-agents-<name>` is the program name in the title, error prefixes and the tail thread name |
| `store_help_intro()` / `store_help_usage()` | the in-app `?` intro and the `--help` usage block (the agent's flags and key hints) |
| `store_init()` | startup indexes (Claude: live pids, team rosters); called once before `store_pop_args` |
| `store_pop_args()` | pops the agent's target flags from the argv (Claude `--session`, `--pid`, `--file`, `--running`); `{'target': session_key or None, 'error': message or None}` |
| `store_resolve_positional(arg)` | `('session', key)` or `('project', key)` for the positional argument |
| `store_reset()` | drops discovery caches on an explicit root refresh |
| `store_list_projects()` | project `Item`s with ids `('project', project_key)` |
| `store_list_sessions(project_key)` | top-level session `Item`s with ids `('session', session_key)`; `[]` for an unknown project |
| `store_project_path(project_key)` | filesystem path for the external source actions |
| `store_project_of_session(session_key)` | the project key a session lists under |
| `store_session_exists(session_key)` | `bool` |
| `store_session_item(session_key)` | the session `Item` (same shape as the listing) or `None`; the project preview is built in common from `store_project_path` and the listing's titles |
| `store_session_info(session_key)` | `{'path', 'label', 'is_child', 'meta': [(key, value), …], 'mtime'}` for the scope / metadata cards and link labels; never a full read |
| `store_session_dirs(session_key)` | `(cwd, project_dir)`: the session's recorded working directory (or `None`) and its real project directory; common derives the git root for the markdown reference resolver |
| `store_context_dirs(project_key, session_key)` | `[(role, path)]` with roles `project` / `cwd` / `workspace` for the directory actions; `session_key` may be `None` |
| `store_child_sessions(session_key)` | child (subagent / worker) `Item`s with ids `('agent', session_key, child_key)`; never returned by `store_list_sessions` |
| `store_mark_orphan(item)` | re-tags a child `Item` the fold found no dispatch site for (common has already set `is_orphan` and `tag_style`) |
| `store_child_session(session_key, child_key)` | the child's `session_key`, or `None` when it has no transcript (the source actions then report "no transcript for child") |
| `store_parent_of_child(session_key)` | `(parent_session_key, child_key)` when the session is a child transcript, else `None` |
| `store_resolve_spawned_child(session_key, call, result)` | `(child_key, child_session_key)` for a canon `spawn` tool dict `call` and the `Record` of its result, or `None`; used by the tree fold to link `('agent', …)` rows to their dispatch site |
| `store_source_path(session_key)` | filesystem path holding the whole transcript (may be a temp file the store wrote) |
| `store_source_locator(session_key, record_key)` | `(path, line)`; `line` 1-based or `None` |

Tail (`045-tail`):

| Function | Returns |
|---|---|
| `store_tail_open(session_key)` | `(records, state)`: every record so far as `[(record_key, Record_or_None), …]` plus an opaque state whose watermark sits at the end of that read; `([], None)` when the session cannot be read or followed (the worker then leaves it alone). This is the one bulk read of a session |
| `store_tail_poll(session_key, state)` | `(new, changed)`: `new` is a list of `(record_key, Record)` appended since the watermark, `changed` a list of records whose body changed in place (same key); updates the watermark |
| `store_tail_interval()` | poll period in seconds (Claude and Codex 5.0, OpenCode 1.0) |
| `store_source_excerpt({session_key: [record_key, …]})` | a temp file holding exactly those records in order (caller unlinks), or `None`; lives next to the reader because it reuses its per-record locators |

Common code keeps the state in `_TAIL_STATE[session_key]` and stores its
fold cursor on `state.cursor`; the store must leave that attribute alone.

Identity tuples in the tree are `('project', project_key)`,
`('session', session_key)`, `('msg', session_key, record_key)`,
`('agent', session_key, child_key)` and the synthetic umbrellas
`('prompt'|'tool'|'span', session_key, record_key)` plus the markdown ids
`('md', anchor, chain, line)` / `('refs', anchor, chain)`.

## Normalized record model

A record is a `Record` (`common/030-model`, a `__slots__` class) built by
the agent's `_parse_record(raw, key)` in `035-parse`, one per raw record.
Common code reads only its fields; `raw` is for the agent's own preview
renderers (#1287 keys them on `tool.canon`) and the source view.

| Field | Type | Meaning |
|---|---|---|
| `key` | int | dense 0-based ordinal into the session's cached record list; the store maps it to its native locator (see Store contract). Ordinals stay stable only while in-place growth happens at the tail of the session (the streaming assistant row is the last message); a new content element in an earlier message would shift every later ordinal (#1270) |
| `kind` | str | one of the kinds below |
| `raw` | any | the source object |
| `id` | str or `None` | the agent's record id (Claude `uuid`); resolves `caller_id` back-pointers |
| `ts` | float or `None` | epoch seconds |
| `turn_id` | str or `None` | explicit turn id when the store has one |
| `tag` | str | row label; the agent's own type name (Claude `user`, `attachment`, `agent-send`, `last-prompt`, …), defaults to the kind |
| `tag_style`, `row_bg` | str or `None`, int or `None` | row presentation: tag colour name and the 256-colour voice stripe. `None` means the per-kind default (`_DEFAULT_TAG_STYLE` / `_DEFAULT_ROW_BG`, the stripe for voice records only); Claude sets both from its own label maps |
| `text` | str | plain body: prompt, assistant text, thinking, system text, inbound message body; markdown allowed |
| `summary` | str or `None` | row title the parser supplies when the generic one (below) would lose the salient fact; `None` = derive from kind, text and tool |
| `tools` | list of dict | tool calls (a record that calls tools) or results (a `tool_result` record), see below; `rec.tool` is the first or `None` |
| `call_id` | str or `None` | for a record that is neither a call nor a result but belongs to one (Claude hook output): that call's id |
| `caller_id` | str or `None` | `id` of the record that issued the call this record answers, when the agent stamps it (Claude `sourceToolAssistantUUID`) |
| `agent_voice` | str or `None` | inbound inter-agent voice: `reply` (a spawned worker's task result), `peer` (another session's message), `idle` (a worker's automatic idle notice) |
| `peer` | str or `None` | sender of inbound agent voice |
| `is_voice` | bool | carries speech (stripe, voice jumps, latest-voice landing); not derivable from kind (Claude counts AskUserQuestion / SendMessage calls as voice) |
| `is_meta` | bool | injected context, never typed or spoken (Claude `isMeta`, Codex `developer`, OpenCode `synthetic`) |
| `end_turn` | bool | an assistant record that concluded its turn |
| `min_level` | int or `None` | lowest detail level that shows the record; `None` means the per-kind default |
| `meta` | dict | small facts: `agent` (attribution), `title` + `title_by` (`user`/`agent`: a session title stated by this record), `duration_ms`, `encrypted`, `thinking` (the readable thinking text, also when the record has spoken text → `· thinking` row tag and the thinking rule in the generic body), `span_label` (finer label for `<system>` span titles, Claude `att:<type>` / `sys:<subtype>`), and the generic chrome keys `model`, `tokens`, `cost` |

The fold does not consume `turn_id` yet (it derives turns from
`user_prompt` / `turn_end` records); the generic chrome reads `ts`. Both
are filled so Codex, which has explicit turns and timestamps, maps onto
the same shape.

Tool dicts come from `new_tool(**fields)` with every key present:
`canon`, `name`, `call_id`, `input` (dict or str), `output` (str or `None`),
`glance` (short result text for the row title, `None` = `output`; Claude
derives it from `toolUseResult`), `diff` (unified diff of an edit result or
`None`), `is_error`, plus `peer` (canon `message`: recipient; canon `spawn`:
teammate name) and `text` (canon `message`: body). An agent may add keys
of its own for its extras (Claude: `tur`, `content` on results). The generic one-liner reads `input` keys
`command`, `file_path`, `pattern`, `subagent_type`, `skill`, `summary` by
canon; a parser whose native shape differs normalises `input` or sets
`summary`.

Kinds and default detail level (1 summary, 2 voice, 3 edits, 4 tools,
5 detailed, 6 all, 7 unknown); the Claude parser sets `min_level`
explicitly on every record (its tiers are finer than the defaults):

| kind | default level | notes |
|---|---|---|
| `user_prompt` | 1 | a fresh human prompt (`is_voice`, not `is_meta`, no `agent_voice`) opens a turn; inbound agent voice re-opens the last human turn |
| `assistant_text` | 2 | spoken assistant text; may also carry `tools` (mixed text + call), then it is wrapped in a `<tool:>` umbrella like a `tool_call` |
| `thinking` | 2 | readable thinking in `text`; empty `text` plus `meta.encrypted` renders a placeholder (level 6) |
| `tool_call` | 3 for canon `edit`/`write`, else 4 | paired with its results by `call_id` |
| `tool_result` | 4 | folded under the call's umbrella |
| `turn_start` | 5 | explicit turn boundary |
| `turn_end` | 5 (Claude 4) | closes the open turn; carries `meta.duration_ms` |
| `compaction` | 5 (Claude 2) | context compaction summary in `text` |
| `system` | 5 | agent/system text not spoken by either side; Claude `system/*` and `attachment` records |
| `meta` | 6 | bookkeeping rows; `tag` carries the agent's own type name; Claude `queue-operation` is a voice `meta` |
| `unknown` | 7 | anything the parser did not recognise; `raw` kept |

Common classification on records (`030-model`): `_record_min_level`,
`_is_turn_root`, `_is_turn_close`, `_is_continuation_activity`,
`_is_inbound_agent_voice`, `_issues_tool_call`; row text:
`_summarise_message`, `_row_tag`, `_tool_use_one_line`,
`_tool_result_one_line`. The fold (`040-tree`) keys every index by record
key (`turn_direct`, `tool_children`, `agent_link[call_id] = {child_key,
child_session, assistant_key}`), so agents without record ids work; the
cross-link map (`070-links`) joins canon `message` / `spawn` tools with
`agent_voice == 'peer'` records by `peer` and `text`.

Tool canon names and the per-agent name map:

| canon | Claude | Codex | OpenCode |
|---|---|---|---|
| `bash` | Bash | CommandExecution, `exec` custom tool | bash (1.x), shell (2.x) |
| `read` | Read | | read |
| `edit` | Edit, MultiEdit, NotebookEdit | FileChange | edit, patch |
| `write` | Write | | write |
| `grep` | Grep | | grep |
| `glob` | Glob | | glob, list |
| `spawn` | Agent, Task | thread spawn (via `thread_spawn_edges`) | task |
| `message` | SendMessage | | |
| `question` | AskUserQuestion | | question |
| `webfetch` | WebFetch | | webfetch |
| `websearch` | WebSearch | | websearch |
| `skill` | Skill | | skill |
| `mcp` | `mcp__*` | | names with a server prefix |
| `other` | everything else | everything else | everything else (todowrite, …) |

## Extras hook contract

`055-extras` is optional. Common code looks the hooks up by name at call
time (`globals().get(name)`) and falls back to its generic rendering when
a hook is absent or returns `None`; an agent with only `010-prelude`,
`025-store`, `035-parse` and `045-tail` gets a complete browser.

| Hook | Called | Returns |
|---|---|---|
| `extras_render(rec, bg)` | before the generic body of any record preview (`_render_record`); `bg` is the md2ansi reset baseline of a banded turn-root prompt | ANSI str, or `None` for the generic body |
| `extras_chrome(rec, root)` | before the generic metadata footer (`_record_chrome`); `root` is the turn-root `Record` or `None` | ANSI str (may be `''`), or `None` for the generic footer |
| `_TOOL_FORMATTERS[key]` | registry in `050-render`: `key` is a canon (common entries) or the agent's own tool name (agent entries, tried first); value `{'call': fn, 'result': fn, 'umbrella': fn}`, each `fn(call, result) -> str \| None` over tool dicts (`result` is `None` while pending) | `call`: the request body under the `🔧 Name` header; `result`: the body under `↳ tool_result`; `umbrella`: the fused block of a `<tool:>` row (`None` cascades to the leaves) |

Generic rendering (`050-render`): `user_prompt` → `▶ user` (or `← reply` /
`← peer from X` for inbound agent voice) + the markdown body; assistant
kinds → `⏺ assistant model [end_turn]`, the thinking under a rule, the
spoken text, each call via `_fmt_tool_call`; `tool_result` → `↳
tool_result [error] id=…` + the result body; `turn_end` / `compaction`
one-liners; `system` / `meta` / `unknown` → the label, text or summary,
and the raw object as JSON (always for `unknown`, at detail 6+ for the
others — the one place common reads `raw`). The generic chrome shows
`id`, `timestamp` (from `ts`), `offset` (from the turn root's `ts`) and
the meta keys `model`, `agent`, `tokens`, `cost`, `duration_ms`. Generic
tool formatters exist for canon bash, read, edit, write, grep, glob,
skill, message, webfetch, websearch, mcp (umbrellas), spawn / question /
message (call bodies), spawn / message / webfetch (markdown results); the
default result body is the diff, else the output (JSON coloured).

Claude extras: `extras_render` renders every record from `raw`
(`_classify` → `_render_user` / `_render_assistant` / `_render_attachment`
/ `_render_system` / `_render_metadata` / `_render_unknown`), because the
raw shapes carry permission mode, structured `toolUseResult`s,
attachments and bookkeeping detail the model does not; `extras_chrome`
emits the uuid / parentUuid / sessionId / cwd / version / flags / usage
rows; `_TOOL_UMBRELLA_FORMATTERS` (tool name → `fn(inp, tur, raw_content,
is_error)`) is adapted into `_TOOL_FORMATTERS` for Bash, Read, Edit,
Write, NotebookEdit, Grep, Glob, WebFetch, WebSearch, ToolSearch, Skill,
SendMessage, TodoWrite, ExitPlanMode, Task* and `mcp`; result tool dicts
carry the agent-private keys `tur` and `content` for them. Codex extras
(planned): `parsed_cmd`, token counts and rate limits. OpenCode extras
(planned): step cost and tokens, snapshots, agent and model selection
events.

## What the common view expects from records

* `store_tail_open` / `store_tail_poll` yield records in display order;
  `key` is a dense ordinal, unique within a session.
* `ts` is non-decreasing except for records the store marks `changed`.
* Each call in a record's `tools` is followed by at most one result with
  the same `call_id`; an unmatched call renders as pending.
* Voice records (`is_voice`) have `text` filled; the voice jumps and the
  markdown subtree operate on `text` (plus result `output`).
* `turn_end` is optional; the fold opens a turn at each fresh
  `user_prompt` and closes it at the next `turn_end` or prompt.
* `raw` is read only by the agent's own extras, the generic JSON dump of
  `unknown` / high-detail bookkeeping records and `store_source_locator`;
  the markdown `.md` reference scan reads `text` and the tool inputs /
  outputs (`_record_strings`), so a reference that only appears in an
  agent-private field (a Claude attachment path) is not detected.

## Per-agent format notes

### Claude Code

* Store: `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`; subagents at
  `<encoded-cwd>/<sid>/subagents/agent-<id>.jsonl` plus `agent-<id>.meta.json`
  (`agentType`, `description`); the encoded cwd of a worktree differs from
  the main project dir. Teams under `~/.claude/teams/`, live pids under
  `~/.claude/sessions/`.
* Record: one JSON object per line with `type`, `uuid`, `parentUuid`,
  `timestamp` (ISO), `sessionId`, `cwd`, `gitBranch`, `version`, `isMeta`,
  `isSidechain`, `agentId`. `type ∈ user, assistant, system, attachment` plus
  bookkeeping types (`summary`, `ai-title`, `custom-title`, `last-prompt`,
  `permission-mode`, `queue-operation`, `file-history-snapshot`, …) → `meta`.
* `message.content[]` blocks: `text`, `thinking`, `tool_use{id,name,input}`,
  `tool_result{tool_use_id,content,is_error}`; `toolUseResult` on the user
  record carries structured results (`structuredPatch` for edits → `diff`,
  `agentId` for spawns, `stdout` for Bash → the row glance).
* Thinking is plaintext. Turn boundaries derive from user records;
  `system/turn_duration` → `turn_end`, `system/compact_boundary` →
  `compaction`, other `system/*` and `attachment` → `system`, bookkeeping
  types → `meta`. Inter-agent voice: `SendMessage` → canon `message`
  (tag `agent-send`), a `<task-notification>` user record → `agent_voice
  'reply'`, a `<teammate-message>` / `<agent-message>` / `origin.kind ==
  peer` user record → `'peer'` (`'idle'` for an idle-notification body).
* Tail: file append; byte offset per file, partial last line kept.

### Codex CLI

* Store: `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`
  (`$CODEX_HOME` override). Index `~/.codex/state_5.sqlite`: `threads(id,
  rollout_path, created_at, updated_at, cwd, title, model_provider, source,
  tokens_used, archived, git_sha, git_branch)`,
  `thread_spawn_edges(parent_thread_id, child_thread_id, status)`. Projects
  group threads by `cwd`; when the DB is absent fall back to line 1
  `session_meta.payload.cwd`.
* Line: `{timestamp, ordinal?, type, payload, metadata?}`. Versions seen
  locally: 0.104.0 (no `ordinal`, `event_msg.user_message`), 0.153.4, 0.161.0
  (`ordinal`, `item_completed`).
* `type ∈ session_meta, turn_context, response_item, event_msg,
  token_usage_record, world_state`.
  `response_item.payload.type ∈ message{role ∈ user, developer, assistant},
  reasoning{summary, encrypted_content}, custom_tool_call{name, input, call_id},
  custom_tool_call_output{call_id, output}, function_call{name, arguments,
  call_id}, function_call_output{call_id, output}, …`; not a closed set,
  others → `unknown`.
  `event_msg.payload.type ∈ task_started, task_complete, turn_aborted,
  token_count, thread_settings_applied, user_message (0.104),
  item_completed{turn_id, item{type ∈ UserMessage, AgentMessage, Reasoning,
  CommandExecution{command, cwd, parsed_cmd, stdout, stderr, exit_code,
  duration}, FileChange{changes, status}}}`.
* Assistant, tool and reasoning items usually appear twice (raw
  `response_item` and `item_completed`); developer/user `response_item`
  messages and 0.104 rollouts have no `item_completed` twin. Render from
  `item_completed`, fall back to `response_item` when no item event exists,
  keep the duplicate as `meta` at level 6.
* `custom_tool_call` named `exec` wraps JS `tools.exec_command({...})`; the
  `cmd` string is extracted for the one-line summary.
* Thinking is encrypted; `summary_text` is usually empty → placeholder with
  `meta.encrypted`.
* Turns: `turn_id` explicit on every item; `task_started`/`task_complete`/
  `turn_aborted` → `turn_start`/`turn_end`.
* Tail: file append, same mechanism as Claude.

### OpenCode

* Store: `$XDG_DATA_HOME/opencode/opencode.db` (default
  `~/.local/share/opencode/`), or `opencode-<channel>.db`; newest mtime wins,
  `--db PATH` overrides. WAL mode, written concurrently by a running opencode;
  open with `sqlite3.connect('file:…?mode=ro', uri=True)`. Installed
  version 2.2.0 holds both generations in one DB.
* v2 (default when `session_v2` exists): `project(id, worktree, name, vcs,
  time_*)` with `project_directory` and `worktree` rows for display →
  `session_v2(id, project_id, parent_id, fork_session_id, slug, directory,
  title, agent, model, cost, tokens_*, time_created, time_updated,
  time_archived, time_suspended)` → `session_message(id, session_id, type,
  seq, time_created, time_updated, data)` ordered by `seq`.
  `type ∈ user{text, files, agents}, assistant{agent, model{providerID, id,
  variant}, content:[text{text, time} | reasoning{text} | tool{id, name,
  state{status, input, content:[{type:text, text}], metadata}, time}],
  snapshot{start, end}, finish, cost, tokens{input, output, reasoning,
  cache{read, write}}, time}, system{text}, synthetic, skill, shell,
  compaction, idle{outcome}, agent_selected, model_selected,
  location_switched`.
  Mapping: user → `user_prompt` (also opens the turn); each assistant
  content element → `assistant_text` / `thinking` / `tool_call`+`tool_result`;
  idle → `turn_end`; system, synthetic, skill, shell → `system`; compaction →
  `compaction`; selection events → `meta`. Tool name is `shell` in 2.x.
* v1 (fallback, opencode 1.x): `session(id, project_id, parent_id,
  directory, title, …)` → `message(id, session_id, time_created, data{role,
  agent, model})` → `part(id, message_id, session_id, time_created,
  time_updated, data{type, …})`; records are parts ordered by
  `message.time_created, message.id, part.id`. Part types `text`,
  `reasoning`, `tool{tool, callID, state{status, input, output, metadata,
  title, time}}`, `step-start{snapshot}` → `turn_start`,
  `step-finish{reason, cost, tokens}` → `turn_end`, `compaction`, `retry`,
  `snapshot`/`patch`/`file` → `meta`. An upgraded 2.x install has migrated v1
  rows into v2 (kv key `migration.v1-v2`), so v1 is read only when
  `session_v2` is absent.
* Child sessions via `parent_id`; thinking is plaintext.
* Source locator: dump the `session_message` row (v2) or message plus parts
  (v1) as pretty JSON to a temp file.

## Live-tail model

The common tail worker (`common/040-tree` + `090-main`) is poll-based: a
daemon thread resolves the cursor's session, makes sure `_scan_tree` has
opened it (`store_tail_open`), then every `store_tail_interval()` seconds
calls `store_tail_poll`, folds `new` records into the cached tree data,
replaces `changed` ones in place, and pushes `upsert` patches for
umbrellas whose visible children changed. A stop event ends the thread
before `sys.exit`.

* Claude, Codex (`045-tail`): stat size and mtime, read from the saved byte
  offset, parse complete lines, keep a partial trailing line for the next
  poll. `changed` is always empty. The per-line byte offsets recorded
  while reading serve `store_source_excerpt`.
* OpenCode (`045-tail`): watermark `(max(seq), max(time_updated))` of
  `session_message` for the session (v1: `part.time_updated`). On change,
  rows with `seq` above the watermark become `new`; rows with
  `time_updated` above it and `seq` at or below it become `changed` (an
  assistant row grows while streaming). `database is locked` is retried on
  the next tick.

## Test strategy

* Unit suites `test/unit/test_browse_agents_<agent>*.py` load the generated
  `recipes/browse-agents-<agent>` with `importlib` and a stubbed `browse_tui`,
  exactly as `test/unit/test_browse_claude_render.py` does. The stub trap in
  `TESTING.md` applies: `discover` loads `test_browse_agents_*` before
  `test_browse_claude_*`, so the new stubs must expose every name the frozen
  `browse-claude` imports too.
* UI suites `test/ui/test_recipe_browse_agents_<agent>.py` run
  `./browse-tui --run-py recipes/browse-agents-<agent>` under tmux against a
  temp `$HOME` (Claude), `$CODEX_HOME` (Codex) or `--db` (OpenCode), like
  `test/ui/test_recipe_browse_claude.py`.
* The Claude suites are forks of the existing browse-claude suites; a test
  that asserted on a moved internal updates its import, not its expectation.
* Fixtures are synthetic: small hand-written JSONL lines and SQLite DBs built
  in-test with the shapes above. No copy of a real user session, DB or
  rollout is ever committed.
* `run-tests.sh` rebuilds through `build-all.sh`, so suites always see the
  recipes generated from the current parts.

## Adding a new agent

1. `mkdir recipes/browse-agents-src/<agent>` and add `010-prelude`
   (shebang, docstring), `025-store`, `035-parse`, `045-tail`,
   `055-extras` (may be empty).
2. Implement the store and tail contracts; map raw records to the normalized
   model with the canon table above.
3. `./build-agents.sh` produces `recipes/browse-agents-<agent>`; commit it
   with the parts.
4. Add unit and UI suites with synthetic fixtures.
5. Add the recipe to `vars.sh` `FILES` (the `b-agents-<agent>` alias derives
   automatically), to the README recipe list and to `MANUAL/recipes/`.
