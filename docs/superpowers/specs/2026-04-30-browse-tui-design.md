# browse-tui — Design Specification

**Date:** 2026-04-30
**Status:** Draft (pre-implementation)

## Overview

`browse-tui` is a self-contained, dependency-free Python 3 program that turns a
hierarchical data source into an interactive terminal UI. It is modelled on
[`plan-tui`](../../../../plan-source/) but generalised: instead of being
hard-wired to one application's data model, it accepts data via a Python API or
shell-command-driven CLI, and lets users wire arbitrary actions to keys.

Use cases include filesystem browsing, Jira / GitHub ticket trees, USB device
hierarchies, Claude Code projects-and-sessions navigation, document outlines,
and any other tree-shaped corpus where a fast keyboard-driven UI with search,
preview, and per-item actions is valuable. `browse-tui` aims to be to
hierarchical data what `fzf` is to flat lists — a reusable selection and
browsing primitive.

## Goals

1. Single-file Python 3 executable; no dependencies outside the stdlib.
2. Layered design: a Python API as the foundation; a CLI shim that synthesises
   API calls from shell flags; eager and lazy data sources both supported.
3. All data callbacks are asynchronous; UI never blocks on slow data sources.
4. Background-thread updates supported (e.g. filesystem watchers).
5. Phased delivery: a working minimal core + filesystem recipe lands first;
   plan-tui parity follows; richer recipes round out v1.
6. Source organised as numbered modules concatenated into a single executable
   by a build script — same shape as `plan-tui`.

## Non-goals

- GUI / mouse-only modes (terminal-only; mouse is supplementary).
- `asyncio` integration (we use simple worker threads + a self-pipe wakeup).
- Persistent state between runs (each invocation is stateless).
- Built-in network or storage features beyond what shell `--children-cmd`
  invocations can provide.

## Reference: relationship to `plan-tui`

`plan-tui` (in `../plan-source/src-tui/`) is the architectural template.
What we **reuse essentially verbatim**:

- The terminal layer (`020-terminal.py`): VT100 raw mode, key parsing, signal
  handling (SIGTSTP/SIGCONT/SIGWINCH), self-pipe wakeup, mouse decoding.
- The visible-tree builder logic, scope/expand state, async preview worker
  pattern, mouse + scroll math, search-fragment highlighting, suspend/resume.
- The numbered-files-concatenated-by-build-script source layout.

What we **replace or generalise**:

- Plan-specific subprocess calls (`plan list`, `plan get`, …) → generic
  `get_children(parent_id)` and `get_preview(item_id)` callables.
- Plan-specific actions (status, edit, view, create, move, close) → generic
  `Action(key, label, handler)` machinery that any recipe can register.
- Hard-coded hierarchical data model (`id, parent, status, …`) → an `Item`
  dataclass with extensible fields and shorthand coercions.

## Architecture

### Module layout (`src-tui/`)

Source lives as numbered files concatenated by `build-tui.sh` into a single
`browse-tui` executable. The same single file is also importable as a Python
module (or `--python`-runnable, see CLI section).

```
src-tui/
  010-prelude.py    # shebang, stdlib imports, version
  020-terminal.py   # raw mode, key reader, signal handlers, mouse — REUSED ~verbatim from plan-tui
  030-data.py       # Item dataclass, coercion, _children/_preview caches, command log
  040-state.py      # visible-tree builder, cursor/scope/expanded/selected, async workers, post queue
  050-render.py     # 3-pane layout, item formatting, search highlight, status bar
  060-context.py    # Context affordances (run_external, refresh, error, pick, input, insert, …)
  070-actions.py    # default built-in actions (nav/search/scope/help/reload) + custom-action dispatch
  080-cli.py        # arg parsing, input-format parsers, --python loader, --install/--uninstall
  090-main.py       # main loop, key routing (normal/search/insert), entry point
```

Files whose stem ends in `+` and which are `chmod +x` are *executed* during the
build; their stdout becomes the section's content. This supports generated
sections (e.g. `075-help-text+` reading a structured help-text source and
emitting the Python literal for it).

**Layering rule** — strict, enforced by ordering:

```
prelude  ←  terminal  ←  data  ←  state  ←  render  ←  context  ←  actions  ←  cli  ←  main
```

Each layer imports only symbols defined in numerically-lower files.

### Public API

#### `Item`

```python
@dataclass
class Item:
    id: Any                      # opaque hashable; what get_children/preview receive
    title: str = ''              # falls back to str(id) in __post_init__
    tag: str = ''                # rendered as [tag] after id/title
    tag_style: str = ''          # 'green'|'red'|'yellow'|'gray'|'cyan'|'blue'|'magenta'|'dim'
    has_children: bool = False   # controls ▼/▶ marker

    # arbitrary extra attrs (e.g., item.size, item.path) survive — non-slotted
```

Coercion: when a `get_children` callback returns an iterable, each element may
be `Item | str | tuple | dict`:

- `Item(...)` — passed through.
- `str` — `Item(id=s, title=s)` (leaf).
- `tuple` — positional dataclass init: `Item(*t)`. So `(id, title)`, `(id, title, tag)`, etc.
- `dict` — `Item(**d)`.

Mixed lists are valid.

#### `Action`

```python
@dataclass
class Action:
    key: str                    # 'e', 'ctrl-r', 'alt-down', 'space', 's', …
    label: str = ''             # shown in help and (when concise) info-bar hints
    handler: Callable[[Context], None] = None
    requires: str = 'none'      # 'none'|'cursor'|'selection'|'targets'   (gating)
```

#### `Context` (passed to action handlers; main-thread-only)

```python
class Context:
    cursor:   Item | None        # property
    selected: list[Item]         # property
    targets:  list[Item]         # property — `selected if selected else [cursor]`

    def select(ids, replace=False): ...
    def cursor_to(id, on_complete=None) -> Pending: ...   # expands ancestors automatically
    def refresh(id=None, on_complete=None) -> Pending: ...
    def expand(id, on_complete=None) -> Pending: ...

    def run_external(cmd, env=None) -> int:                # suspend → run → resume
    def page(text, lang=''):                               # text → bat/less

    # sub-flows (block on key reading; main-thread-only)
    def pick(label, options) -> str | None:                # fzf-style filterable picker
    def input(prompt, default='') -> str | None:
    def confirm(prompt) -> bool:                           # y/n
    def insert(label, on_confirm):                         # enter insert-mode; cb(relation, dest_id)

    # feedback
    def error(msg)
    def message(msg)
    def quit(code=0, output='')
```

#### `Browser` (engine; thread-safe public surface)

```python
class Browser:
    def __init__(self, *,
        title='browse-tui',
        get_children=lambda _: [],         # (parent_id) -> Iterable[Item|str|tuple|dict]
        get_preview=None,                  # (item_id) -> str  (optional)
        actions=None,                      # list[Action]
        on_enter=None,                     # default action handler; if None → print+exit
        format_item=None,                  # (item, ctx) -> [(text, fg, bold), …]  (optional)
        root_id=None,
        initial_scope=None,
        show_preview=True,
        show_children_pane=True,
        multi_select=True,
        print_format='{id}',               # used when on_enter=None
    ): ...

    def add_action(action): ...
    def run() -> int: ...                  # blocks; returns exit code

    @classmethod
    def from_flat_tree(cls, rows, **kw) -> 'Browser': ...  # eager adapter

    # Thread-safe public ops (callable from any thread)
    def refresh(id=None, on_complete=None) -> Pending: ...
    def cursor_to(id, on_complete=None) -> Pending: ...
    def expand(id, on_complete=None) -> Pending: ...
    def select(ids, replace=False): ...
    def message(text): ...
    def error(text): ...
    def quit(code=0, output=''): ...
    def post(callable_): ...               # schedule callable on main thread
    def watch(callback, interval=None) -> threading.Thread: ...
```

`Context` wraps `Browser` and adds the main-thread-only sub-flows (`pick`,
`input`, `confirm`, `insert`, `run_external`, `page`).

#### `Pending` (chained completion handles)

```python
class Pending:
    def then(self, callback) -> 'Pending':   # chain; returns self
    @property
    def done(self) -> bool: ...
```

Async-returning ops (`refresh`, `expand`, `cursor_to`) return a `Pending`. Use
fire-and-forget, `on_complete=cb`, or chained `.then(cb)`. Callbacks always run
on the main thread, after the worker's result has been applied.

#### One-liner

```python
def browse(get_children, **kwargs) -> int:
    return Browser(get_children=get_children, **kwargs).run()
```

### Data flow & threading model

**Three caches, all in `030-data.py`:**

| Cache             | Key       | Populated by                     | Invalidated by                |
|-------------------|-----------|----------------------------------|-------------------------------|
| `_children`       | parent_id | `_children_worker` (async)       | `ctx.refresh(id)` or full     |
| `_preview`        | item_id   | `_preview_worker` (async)        | same                          |
| `_visible_tree`   | —         | DFS over `_children` + `expanded`| any state change              |

**Two worker threads, one pattern (lifted from plan-tui's preview worker):**

| Worker             | Request shape         | Coalescing                  | Concurrency             |
|--------------------|-----------------------|-----------------------------|-------------------------|
| `_children_worker` | FIFO queue of pids    | none in v1 (phase 2 dedupe) | one in flight at a time |
| `_preview_worker`  | single latest-id slot | latest-wins                 | one in flight at a time |

The main thread owns all state; everything else either *posts* (background
threads → main queue) or *delivers a result* (workers → cache + `notify_wake()`
on the self-pipe).

**Three load states per parent node** during visible-tree build:

1. **Cached, populated** — render its children normally.
2. **Cached, empty** — render nothing under it.
3. **Pending** — show single `⧗ loading…` placeholder; trigger fetch if not
   already pending.

**Preview pane during async fetch:** while loading, show previous result with a
small `loading…` indicator on the separator label. Avoids flicker-to-blank.

**Cancellation: none.** In-flight requests run to completion; results that
arrive after the user has navigated past simply land in cache for next visit.
Saves complexity (no thread killing); the cost is some wasted work on rapid
scrolling.

**Errors:**
- `get_children(pid)` raises → cache `[]` for pid (prevent retry storm),
  surface error in preview pane.
- `get_preview(id)` raises → cache the error string as preview, prefixed
  `[error] …`.

**Thread safety:** all shared state mutated via simple dict/set ops; the GIL
provides single-op atomicity. No explicit locks. The dirty-flag race (worker
sets dirty after main has rebuilt) is benign — next tick rebuilds again.

### Pending registry & chained completions

```python
# In Browser:
_children_cache:     dict[Any, list[Item]]
_children_in_flight: dict[Any, list[Pending]]
_children_queue:     deque[Any]
_main_queue:         queue.Queue          # background-thread → main-thread ops
```

Lifecycle of a `ctx.refresh(id).then(cb)` chain:

1. `ctx.refresh(id)` posts `_do_refresh(id, pending)` to main_queue → returns
   Pending P1 immediately.
2. Action handler returns; main loop drains queue → P1's id enqueued for worker.
3. Worker fetches, delivers `(id, items)` to `_children_results`, calls
   `notify_wake()`.
4. Main loop's notify path: drain `_children_results`, fill cache, mark visible
   dirty, resolve all Pendings in `_children_in_flight[id]`.
5. P1 resolves → `cb` runs on main thread.

**Background updates** — same plumbing:

```python
# Recipe:
def watch_files(browser):
    while True:
        time.sleep(1.0)
        for d in changed_dirs():
            browser.refresh(d)         # posts to main queue, kicks worker

browser.watch(watch_files)             # daemon thread
browser.run()
```

`browser.refresh()` is identical whether called from main or from a watcher
thread — the post queue funnels everything.

**`browser.watch(callback, interval=None)`:** convenience that spawns a daemon
thread invoking `callback(browser)`. Watchers run in their own threads;
`threading.excepthook` is shimmed to surface exceptions via `browser.error()`.

### Coarse-only mutation in v1

Background updates always go through `refresh(id)` — invalidating a subtree
and re-fetching. Fine-grained `add_child / remove / update_in_place` are NOT in
v1. The cache-consistency obligations they would impose on watcher authors
outweigh the perf savings for any realistic recipe. Add later (phase 3+) if
streaming inserts genuinely need it.

## CLI surface

### Flag table

```
USAGE
  browse-tui [OPTIONS]

DATA SOURCE (one mode required)
  -c, --children-cmd CMD       Lazy. Bash command listing children of $TUI_ID.
      --root-id ID             Initial id passed (default empty string).
  -p, --preview-cmd CMD        Bash command for the preview pane.
      --root-cmd CMD           Eager. Emits the entire flat tree on stdout
                               (parsed per --input). Mutually exclusive with
                               --children-cmd. Pre-populates the children cache.
      --python SCRIPT [args…]  Run a Python recipe; the binary self-injects
                               into sys.modules['browse_tui'] before exec.
                               Mutually exclusive with the data flags above.

INPUT FORMAT
  -i, --input FORMAT           Default: tsv. Values:
                                 tsv | csv | json | json-array
                                 ifs:CHARS         (e.g. ifs::, ifs:" \t")
                                 split:REGEX       (split each record on regex)
                                 match:REGEX       (named groups (?P<name>…) → fields)
      --fields LIST            Comma-separated field names for positional formats
                               (default: id,title). Extra columns become
                               arbitrary attrs on the Item.
      --record-sep SEP         nl (default) | null | LITERAL_STRING

ACTIONS
  -a, --action 'KEY:LABEL:CMD' Register a custom action; repeatable.
                               CMD runs via /bin/bash with these env vars:
                                 TUI_ID, TUI_TITLE, TUI_TAG, TUI_TAG_STYLE,
                                 TUI_HAS_CHILDREN          — primary target
                                 TUI_<CUSTOM_ATTR>         — any item attribute
                                 TUI_IDS_FILE              — NUL-sep targets file
                                 TUI_IDS_COUNT             — number of targets
                                 TUI_TARGETS               — 'cursor'|'selection'
                                 TUI_BIN                   — path to running binary
                               Inherited env (EDITOR, PAGER, PATH, …) is unchanged.
                               After CMD exits, the affected subtree refreshes.
      --action-timeout SECS    Default 600. SIGTERM after timeout, SIGKILL +5s.
      --on-enter MODE          What Enter does:
                                 print-exit (default)  print id + exit 0
                                 action:KEY            invoke a registered action
                                 noop                  long-running mode
      --print-format FMT       Format for print-exit. Default '{id}'.

LAYOUT / DISPLAY
      --no-preview             Start with preview pane hidden (toggle: C-p).
      --no-children-pane       Hide the children grid (phase 2).
      --no-multi-select        Disable selection.
      --title TITLE            Window title in info bar.
      --initial-scope ID       Start scoped to this id.

INSTALL / UNINSTALL (mutually exclusive with TUI mode)
      --install   {local|user|system|env}
      --uninstall {local|user|system|env}
                               Targets:
                                 local    ./browse-tui
                                 user     ~/.local/bin/browse-tui
                                 system   /usr/local/bin/browse-tui (may need sudo)
                                 env      $VIRTUAL_ENV/bin/browse-tui

DEBUG / OPS
      --command-log            Show command log on quit.
      --version
  -h, --help

ENVIRONMENT
  EDITOR                       Used by recipes that template $EDITOR.
  PAGER                        Falls back to bat/batcat/less in that order.
```

### Action env-var conventions

- All exported variables are prefixed `TUI_`. No collisions with parent env.
- Item attribute names → uppercased: `size` → `TUI_SIZE`, `tag_style` →
  `TUI_TAG_STYLE`.
- Non-identifier attribute names skipped silently.
- All commands run via `/bin/bash -c CMD`. Bash features (arrays, `[[ ]]`,
  `$'...'`, process substitution) are available. Probed at startup; explicit
  error if bash is unavailable.

### `--python` mode mechanics

```python
# in 080-cli.py / 090-main.py
import sys, runpy
sys.modules['browse_tui'] = sys.modules[__name__]   # self-inject
runpy.run_path(script_path, run_name='__main__')
```

Recipes can `from browse_tui import Browser, Item, Action` and resolve from the
in-memory cache — no install of any kind required. Recipes can use a shebang:

```python
#!/usr/bin/env -S browse-tui --python
from browse_tui import Browser, Item, Action
…
```

### `--install` / `--uninstall` behaviour

- `--install local`     copies the running binary to `./browse-tui`.
- `--install user`      copies to `~/.local/bin/browse-tui`.
- `--install system`    copies to `/usr/local/bin/browse-tui`. If non-root,
  prints the `sudo cp …` invocation rather than re-execing under sudo.
- `--install env`       copies to `$VIRTUAL_ENV/bin/browse-tui`. Errors if
  `$VIRTUAL_ENV` is unset.
- `--uninstall <target>` removes the file from the corresponding path.
- Identical contents → no-op (silent). Different contents → prompt to
  overwrite (or `--force`).

When `--install` / `--uninstall` is on the command line, browse-tui never
enters TUI mode; the action runs and the process exits.

### Worked CLI examples

```bash
# 1. fzf-style flat selector
ls | browse-tui --children-cmd 'cat' --input tsv | xargs cat

# 2. Filesystem tree (lazy)
browse-tui \
  --children-cmd 'find "$TUI_ID" -mindepth 1 -maxdepth 1 -printf "%p\t%f\t%y\n"' \
  --fields id,title,kind \
  --root-id "$PWD" \
  --preview-cmd '[[ -d "$TUI_ID" ]] && ls -lA "$TUI_ID" || head -200 "$TUI_ID"' \
  --action 'e:Edit:$EDITOR "$TUI_ID"' \
  --action 'd:Delete:rm -ri "$TUI_ID"'

# 3. /etc/passwd browser (eager, IFS-split)
browse-tui --root-cmd 'cat /etc/passwd' \
           --input ifs:: \
           --fields user,_,uid,gid,gecos,home,shell

# 4. find with NUL safety
find . -maxdepth 3 -print0 \
  | browse-tui --root-cmd 'cat' --record-sep null --fields id

# 5. ls -l with named-regex
browse-tui --root-cmd 'ls -lA' \
           --input 'match:^(?P<mode>\S+)\s+\d+\s+(?P<owner>\S+)\s+\S+\s+(?P<size>\d+)\s+(?P<date>\S+\s+\S+\s+\S+)\s+(?P<id>.+)$'

# 6. Run a Python recipe directly
browse-tui --python recipes/browse-fs.py ~

# 7. Install to ~/.local/bin
browse-tui --install user
```

## Phasing

### Phase 1 — Minimal core + `browse-fs`

**Lands:**

- All modules in skeleton; single-file `browse-tui` builds and runs.
- Full async data layer + `Pending` chaining + `browser.watch()`.
- `Item` dataclass + str/tuple/dict coercion. `Browser.from_flat_tree()`.
- Navigation, search, custom actions, default Enter (print+exit fzf-mode).
- `Context` affordances: `refresh / cursor_to / expand / select / message /
  error / input / confirm / run_external / page / quit` (no `pick` / `insert`
  in this phase).
- Mouse (click + scroll). Preview pane (toggleable). Suspend/resume. Help.
  Reload (C-r). Redraw (C-l).
- CLI: `--children-cmd`, `--preview-cmd`, `--root-id`, `--root-cmd`,
  `--action`, `--on-enter`, `--print-format`, `--no-preview`, `--input` (tsv +
  json only), `--fields`, `--record-sep`, `--python`, `--install`,
  `--uninstall`, `--help`, `--version`.
- Recipe: **`browse-fs`** — filesystem browser with lazy `os.scandir`, preview,
  edit/open/delete actions, and a background mtime watcher.

**Module skeleton sizing target:** ~1,800 lines in single-file output.

**Exit criterion:** `browse-fs` is usable as a daily-driver replacement for
ranger-style file browsing. The Python API is feature-complete enough that any
reasonable hierarchy can be wired up.

### Phase 2 — plan-tui parity + `browse-plan`

**Lands:**

- Multi-select: space toggle, alt-space (toggle + up), C-a select all, C-n
  deselect all, `*` marker, `[N]` selection count.
- `Action.requires` semantics enforced.
- Scoping: alt-↓/alt-↑, `_expanded_by_scope` per-scope memoization, scope crumb
  in info bar.
- Children grid pane (multi-column flowed layout, `_sub_layout` math from
  plan-tui).
- `ctx.pick` (fzf-style sub-picker), `ctx.insert` (full insert mode).
- Search-fragment highlighting in list (yellow/bold; reverse+underline on
  cursor row).
- CLI input formats: csv, json-array, ifs:CHARS, split:REGEX, match:REGEX.
- `--record-sep null` for `find -print0`.
- Recipe: **`browse-plan`** — full plan-tui port. Side-by-side comparison test.

**Exit criterion:** `browse-plan` replaces `plan-tui` with no behavioural
regressions.

### Phase 3 — Rich recipes + polish

**Lands:**

- Recipe: **`browse-claude`** — Claude Code projects/sessions/messages.
- Performance: coalesce duplicate ids in `_children_queue`; `_children_in_flight`
  collects all waiters per id so a single fetch resolves all Pendings.
- `Pending.cancel()` non-strict cancellation.
- Polish: docstrings, type hints across the public API, `--help` includes the
  `?` help screen body, recipe tutorials in `docs/`.
- Bonus recipes if time: `browse-jira`, `browse-procs`, `browse-git`.

**Exit criterion:** documented, polished, three production-quality recipes.

### Cross-cutting

Each phase ends with the test suite green and one PR's worth of changes.

## Testing strategy

Three layers.

### Layer 1 — Unit tests (`unittest`, fast, pure)

- Item coercion: every input shape, mixed lists.
- Input parsers: csv, tsv, json, json-array, ifs, split, match (named groups);
  edge cases (quoted fields, empty values, embedded delimiters, NUL records).
- Field coercion: `has_children='1'` etc.
- Visible-tree builder: scope + expanded sets → ordered list.
- Scope-stack mechanics.
- `_resolve_insert(pos, depth, vis)` — every depth/position combo.
- `Pending`: registration, resolution order, chains, late `.then()`.

**Target: ~150 tests, suite in <1s.**

### Layer 2 — Async / threading tests (no terminal)

`Browser._headless = True` + `start_workers / stop_workers / run_until_idle`
test affordances let us drive workers + post queue deterministically.

- Worker queue ordering (FIFO).
- Two threads refreshing same id → both Pendings resolve.
- Chain resolution order with mid-chain fetches.
- Worker error → cache `[]`, error surfaced, Pending still resolves.
- Background `browser.refresh(id)` from a watcher → main queue → worker.
- `.then()` on already-resolved Pending fires immediately.
- Cancellation (phase 3).

**Target: ~50 tests, suite in <2s.**

### Layer 3 — UI / end-to-end tests (tmux)

Tmux-driven. Each test uses a private tmux server (`-L browse-tui-test-…`),
session dimensions explicit, polling-based readiness.

```python
class TmuxFixture:
    def __init__(self, cols=150, rows=60, env=None):
        self.socket = f'browse-tui-test-{os.getpid()}-{secrets.token_hex(4)}'
        self.cols, self.rows = cols, rows
        self.env = env

    def __enter__(self):
        self.tmux('new-session', '-d', '-s', 'main',
                  '-x', str(self.cols), '-y', str(self.rows),
                  'bash', '--norc', '--noprofile', '-i')
        self.send_line(r"PS1=$ ; unset HISTFILE")
        return self
    def __exit__(self, *_): self.tmux('kill-server', check=False)

    def tmux(self, *a, check=True): ...
    def send_line(self, line): ...                   # type cmd + Enter
    def launch(self, *argv): ...                     # send_line of shlex.join
    def send(self, *keys): ...                       # tmux key names
    def send_bytes(self, raw): ...                   # raw bytes (Ctrl-codes)
    def ctrl_c(self): self.send_bytes('\x03')
    def ctrl_z(self): self.send_bytes('\x1a')
    def fg(self): self.send_line('fg')

    def pane_pid(self) -> int: ...                   # bash pid
    def fg_pid(self) -> int: ...                     # walk children of pane_pid
    def signal(self, sig): os.kill(self.fg_pid(), sig)

    def capture(self, colors=False) -> str: ...
    def wait_for(self, pattern, timeout=3.0, interval=0.03): ...
    def wait_stable(self, dwell=0.05, timeout=3.0): ...
    def resize(self, cols, rows): ...
```

Skip-if-no-tmux at the top of `test_ui.py`.

Two suspend tests (phase 1 / phase 2):

```python
def test_ctrl_z_then_fg_round_trip(self):
    # Tests SIGTSTP → leave_raw → re-raise → SIGCONT → enter_raw → redraw
    with TmuxFixture() as t:
        t.launch('./browse-tui', '--children-cmd', 'printf "a\\nb\\n"')
        before = t.wait_for('a') and t.wait_stable()
        t.ctrl_z()
        t.wait_for(r'Stopped|Suspended')
        t.fg()
        self.assertEqual(t.wait_stable(), before)

def test_sigstop_then_sigcont(self):  # phase 2
    with TmuxFixture() as t:
        t.launch('./browse-tui', '--children-cmd', 'printf "a\\n"')
        before = t.wait_for('a') and t.wait_stable()
        t.signal(signal.SIGSTOP); time.sleep(0.1)
        t.signal(signal.SIGCONT)
        self.assertEqual(t.wait_stable(), before)
```

Coverage: nav, search, expand/collapse, custom actions, async loader (`⧗`)
visibility, background updates, resize handling, multi-select (phase 2),
scoping (phase 2), sub-picker (phase 2), insert mode (phase 2). A handful of
full-screen snapshot tests (phase 3, ~5 total, regenerated when intentionally
changing rendering).

**Target: ~30 tests, suite in <30s.**

### Test layout

```
test/
  unit/
    test_item.py
    test_parsers.py
    test_visible_tree.py
    test_scope_stack.py
    test_insert_resolution.py
    test_pending.py
  async_/
    test_workers.py
    test_post_queue.py
    test_chains.py
    test_background.py
  ui/
    fixtures/
      tmux.py
      recipes.py                # tiny test recipes
    test_navigation.py
    test_search.py
    test_actions.py
    test_async_ui.py
    test_resize.py
    test_suspend.py
    test_snapshots.py
    snapshots/
```

## Recipes

### `browse-fs` (phase 1)

Filesystem browser, target ~80 lines. Demonstrates lazy `os.scandir` children,
preview (head of file or `os.listdir` for directories), custom actions
(`e:Edit`, `o:Open`, `d:Delete` with `ctx.confirm`), and a background mtime
watcher that polls cached directories and calls `browser.refresh(d)` on change.
Items carry custom attributes (`item.size`, `item.mode`, `item.mtime`) which
become `TUI_SIZE`, `TUI_MODE`, `TUI_MTIME` env vars when CLI-style actions are
also wired up.

### `browse-plan` (phase 2)

Full plan-tui replacement. Status picker → `ctx.pick`; edit/view →
`ctx.run_external`; create/move → `ctx.insert`. Validates the abstraction —
side-by-side parity against `plan-tui` is the exit criterion.

### `browse-claude` (phase 3)

Three-level hierarchy (project → session → message), each level has its own
`get_children` branch keyed on item depth/shape. Demonstrates lazy multi-level
+ JSON parsing + mixed item types.

## Build & distribution

### `build-tui.sh`

Copy of `plan-tui`'s build script with `plan-tui` → `browse-tui`. Preserves
the executable-section feature: files in `src-tui/` whose stem ends in `+` and
which are `chmod +x` are *executed*; their stdout becomes the section's
content.

### Single-file artifact

`browse-tui` is both:

1. An executable (`#!/usr/bin/env python3` shebang, `chmod +x`).
2. A Python module — when loaded via `--python`, `sys.modules['browse_tui']`
   points at the running interpreter's code, so recipe `from browse_tui import
   …` statements resolve from the in-memory cache without filesystem lookup or
   `site-packages` install.

### Install / uninstall

`browse-tui --install {local|user|system|env}` and `--uninstall …` move the
binary in/out of standard paths (`./`, `~/.local/bin/`, `/usr/local/bin/`,
`$VIRTUAL_ENV/bin/`). System target prints the `sudo cp …` for the user to run
rather than re-execing under sudo.

No Python library install required — the binary self-injects on `--python`.

### Documentation deliverables (phase 3)

- `README.md` — one-screen quickstart (CLI + API + recipe pointer).
- `docs/cli.md` — full flag reference + worked examples.
- `docs/api.md` — `Browser` / `Item` / `Action` / `Context` / `Pending`
  surface, with cross-references to recipes.
- `docs/recipes.md` — index of shipped recipes.
- `docs/internals.md` — module layout, threading model, async/Pending — for
  contributors.

## Open questions / future work

- **Async cancellation** — phase 3+ — `Pending.cancel()` semantics, whether
  workers can be told to skip in-flight requests.
- **Coalescing of duplicate `_children_queue` entries** — phase 2/3 — single
  fetch satisfies all Pendings.
- **Backpressure on watcher updates** — if a watcher fires faster than the
  worker can refresh, do we drop intermediate updates or queue them up?
- **Streaming inserts** — for `tail -f`-like recipes, full-subtree refetch is
  wasteful. Add `browser.append_child(parent, item)` /
  `browser.remove_item(id)` if a recipe genuinely needs it.
- **`asyncio` integration** — currently we use threads + self-pipe. If a
  recipe author has an existing asyncio loop, an `async_get_children=async def
  …` adapter that the worker awaits could land later.
- **Persistent state** — caching of preview/children across runs would
  accelerate restart on slow data sources. Out of scope for v1.
- **CSV/TSV parser quirks** — e.g. RFC 4180 vs Excel-flavoured CSV; document
  which we follow.

## Implementation references

- `plan-tui` source: `../plan-source/src-tui/{010-prelude,020-terminal,…,070-main}.py`
- `plan-tui` build script: `../plan-source/build-tui.sh`
- `plan-tui` README: `../plan-source/README.md`

