# Testing browse-tui

## Running the tests

| Command | What it does |
|---------|--------------|
| `./run-tests.sh` | Canonical / CI runner. Rebuilds the `browse-tui` binary, then runs every suite via `python3 -m unittest discover -t . -s test/`. |
| `./run-tests-parallel.sh [-j N] [modules…]` | Same coverage, split into **per-module subprocesses** in a process pool (default 4 workers). Accepts specific modules, e.g. `./run-tests-parallel.sh test.unit.test_md_doc`. |
| `python3 -m unittest discover -t . -s test/unit` | Run one suite directory (`test/unit`, `test/async_`, or `test/ui`). |
| `python3 -m unittest test.unit.test_md_doc` | Run a single module. |

## Test layout

- **`test/unit`** — unit tests for the framework (`src-tui/*`) and the recipes. These **stub `browse_tui`** (see below), so they do **not** need the binary built.
- **`test/async_`** — async worker / post-queue tests. No build needed.
- **`test/ui`** — end-to-end tests that spawn the real `browse-tui` binary through tmux. These **require `./build-tui.sh` first** (the binary must be current); `run-tests.sh` always rebuilds before running. They may be skipped where the environment has no tmux.

## Gotcha: `browse_tui` import order when running recipe tests together

`browse_tui` (the framework) is not an installable module — it only exists at runtime, assembled from `src-tui/*` into the `browse-tui` binary. So the unit tests **fake it**: each recipe test file inserts a stub `browse_tui` into `sys.modules` via its own `_stub_browse_tui()`, exposing **only the symbols its recipe imports**. That helper returns early if `browse_tui` is already in `sys.modules` — it does not reconcile or top up the names:

```python
def _stub_browse_tui():
    if 'browse_tui' in sys.modules:
        return            # reuses whatever stub got installed first
    ...
```

Because the stub is process-wide, **whichever recipe test module loads first wins**, and its stub is reused by every recipe test loaded afterward in the same process.

### Symptom

Every test in a recipe module errors in `setUpClass` with, e.g.:

```
ImportError: cannot import name 'mod' from 'browse_tui' (unknown location)
```

### Cause

A *leaner* stub was installed first, and a later recipe needs a symbol it lacks:

- `recipes/browse-md` imports 4 names: `Action, Browser, BrowserConfig, Item`.
- `recipes/browse-claude` imports 8: those **plus `mod, set_preview_op, upsert, visible_items`**.

If `test_browse_md` loads first, it installs the 4-name stub; `test_browse_claude_render`'s later `from browse_tui import …, mod, …` then fails on `mod`.

This is a **test-harness artifact, not a product bug** — `mod` and the rest genuinely exist in the real `browse_tui` framework; only the per-file stubs are order-sensitive.

### How to deal with it

Any of these avoids the trap entirely:

- **Use `./run-tests.sh` or `unittest discover`.** `discover` loads modules alphabetically, so `test_browse_claude_render` (the richer stub) always precedes `test_browse_md`. Deterministic and safe — this is why CI is green.
- **Use `./run-tests-parallel.sh`.** Each module runs in its own subprocess with a fresh `sys.modules`, so it is immune regardless of order. Pass module names to run a safe subset, e.g.
  `./run-tests-parallel.sh test.unit.test_browse_md test.unit.test_browse_claude_render`.
- **Run a single recipe module alone**, e.g. `python3 -m unittest test.unit.test_browse_claude_render` — always safe.

The **only** way to trip it is hand-listing several recipe test modules in one `unittest` process, e.g.:

```bash
# DON'T — lean stub (browse-md) lands first, browse-claude then fails on `mod`
python3 -m unittest test.unit.test_browse_md test.unit.test_browse_claude_render
```

If you must do that, list the richer module first (`test_browse_claude_render` before `test_browse_md`) — but prefer `discover` or the parallel runner.

### Possible hardening (not yet done)

The footgun is the early `return` in `_stub_browse_tui()`. Making it idempotently ensure all required attributes exist on the stub (rather than bailing when one is already present), or sharing a single complete stub module across the recipe test files, would remove the order sensitivity. Low priority while the standard runners are deterministic.
