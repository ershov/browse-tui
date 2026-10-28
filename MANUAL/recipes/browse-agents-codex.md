# recipes/browse-agents-codex

Codex CLI project / thread / record browser.

**One-line summary:** walks the rollouts under `$CODEX_HOME/sessions`
(default `~/.codex`), grouped by working directory, and folds each rollout
into turns with tool umbrellas and rendered previews.

**Parts build.** A generated file: `./build-agents.sh` concatenates
`recipes/browse-agents-src/common/` (the view code shared by every agent
flavour) and `recipes/browse-agents-src/codex/` (the Codex store, parser,
tail and extras) into `recipes/browse-agents-codex`. Edit the parts, rebuild
with `./build-agents.sh` (or `./build-all.sh`), and commit the generated
recipe together with the parts. See `recipes/browse-agents-src/DESIGN.md`.

**Hierarchy:**

- projects — threads grouped by their `cwd` (from `state_5.sqlite`, else the
  rollout's `session_meta`), most recently active first;
- threads — one `rollout-<ts>-<uuid>.jsonl` each, titled by the thread name
  from the index, else its first prompt, tagged with the model, record
  count, spawned-subthread count and age;
- records — the rollout lines folded into `<prompt>` turns (opened by the
  user prompt, closed by `task_complete` / `turn_aborted`) and `<system>`
  spans; a thread that spawned subthreads (`thread_spawn_edges`) lists them in
  a top block, expandable into their own tree.

**Usage:**

```bash
./recipes/browse-agents-codex                    # all projects
./recipes/browse-agents-codex /home/me/work      # one working directory
./recipes/browse-agents-codex --thread 01a119    # a thread by id (unique prefix)
./recipes/browse-agents-codex --file rollout.jsonl --detail tools
```

Flags: `--thread ID`, `--file PATH`, `--item N`, `--tree` / `--no-tree`,
`--detail LEVEL`, `-n NUMBER`. `$CODEX_HOME` overrides `~/.codex`.

**Detail levels** (`--detail`, or the `1`-`7` keys live):

- `1` `summary` — user prompts and the agent's final answers.
- `2` `voice` — adds commentary messages and readable reasoning summaries.
- `3` `edits` — adds `apply_patch` calls and their file changes.
- `4` `tools` — adds commands and other tool calls with their results, and
  the turn end rows (duration, token total).
- `5` `detailed` — adds `session_meta`, `turn_context`, settings changes
  and the injected developer instructions.
- `6` `all` — adds token counts, `world_state`, encrypted reasoning
  placeholders and the raw `response_item` duplicates of rendered items.
- `7` `unknown` — adds record kinds the recipe does not recognise.

**Format notes.** Codex writes most facts twice: the raw model exchange as
`response_item` lines and the rendered item as `event_msg/item_completed`
(0.153+). The recipe renders prompts from the `UserMessage` item (or the
0.104 `user_message` event), assistant text from `AgentMessage`, reasoning
from `Reasoning` (encrypted → a placeholder at level 6), and tool calls from
the `custom_tool_call` / `function_call` line paired with the
`CommandExecution` / `FileChange` item that follows it (stdout, exit code,
duration, unified diff). The `exec` custom tool's JavaScript is unwrapped to
`exec_command` (the `cmd`) or `apply_patch` (the patch and its file list).
Raw twins stay visible as `raw` rows at level 6.

Keys: `Enter` follow a `[↗]` link to a spawned subthread, `1`-`7` set the
detail level, `J` / `K` jump between voice rows, `t` toggle tree / flat,
`e` / `E` open the preview / the rollout in `$EDITOR`, `y` show the row id.

**Source:** [`recipes/browse-agents-src/`](../../recipes/browse-agents-src/) (generated: [`recipes/browse-agents-codex`](../../recipes/browse-agents-codex))

---

*[← All recipes](../recipes.md)*
