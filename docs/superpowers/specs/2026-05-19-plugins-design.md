# browse-tui — Plugin System Design

**Date:** 2026-05-19
**Status:** Draft (pre-implementation)

A small plugin system that lets auxiliary Python files extend a main
recipe — or extend the framework itself — without modifying either.
Two channels: recipes `import` plugins they want to consume; users
load extra plugins from the CLI to alter Browser behavior regardless
of what the main recipe does.

## Motivation

Today, every browse-tui feature must live inside the main recipe.
Reusing behavior across recipes means copy-paste. Personal
customizations (e.g., "always pretty-print Markdown in my preview
window") require forking each recipe a user runs.

A plugin system fixes both: shared functionality (formatters,
launchers, decorators) becomes a normal Python module, recipes
import what they want, and users layer personal plugins on top via
the CLI.

## Goals

1. Recipes can `import` plugin modules and use whatever they expose.
2. Users can load plugins on the CLI (`--plugin SPEC`, repeatable;
   SPEC is a module name or filesystem path) regardless of what the
   main recipe expects.
3. Plugins can run setup at four lifecycle points: before/after
   `Browser.__init__`, before/after `Browser.run`.
4. Plugins can read and modify Browser construction parameters
   before the Browser is built.
5. No plugin loader, registry walker, or discovery API to maintain
   beyond `register_plugin(cfg)` and four optional hook fields.
6. Plugins work in three of the four `browse-tui` invocation
   modes:
   - **CLI mode** — recipe-less, all behavior on flags
     (`browse-tui -c CMD -p CMD --plugin foo`).
   - **Python recipe** — `--run-py SCRIPT` or `--run SCRIPT`
     auto-detected as Python.
   - **(unsupported)** External CLI recipe — `--run-cli SCRIPT` or
     `--run SCRIPT` auto-detected as `cli`. Combining `--plugin`
     with this mode is a hard startup error; silent drops would
     be more confusing than an explicit refusal.

## Non-goals

- A capability/permission model. Plugins run with full Python
  access — same trust level as the main recipe.
- Inter-plugin dependency resolution beyond normal Python `import`.
- Plugin versioning, manifests, or remote installation.
- Hot reload or unload during a session.
- A general event bus. Only the four lifecycle hooks fire.

---

## API

### `BrowserConfig` (replaces `Browser(**kwargs)`)

The current `Browser(**kwargs)` surface becomes an explicit dataclass:

```python
@dataclass
class BrowserConfig:
    title: str = 'browse-tui'
    children_cmd: str | None = None
    preview_cmd: str | None = None
    # ... every existing kwarg becomes a field ...

class Browser:
    def __init__(self, config: BrowserConfig):
        ...
```

Recipes construct a `BrowserConfig`, pass it to `Browser(...)`, and
get a Browser. The `**kwargs` form goes away. Existing recipes are
updated as part of this change (no compatibility shim).

The dataclass is the public contract: adding a new construction
parameter means adding a field to `BrowserConfig`. Plugins that
mutate `config` in `on_before_init` get exactly the same surface
the recipe would see.

### `PluginConfig`

```python
@dataclass
class PluginConfig:
    name: str | None = None
    on_before_init: Callable[[Browser, BrowserConfig], None] | None = None
    on_after_init:  Callable[[Browser], None] | None = None
    on_before_run:  Callable[[Browser], None] | None = None
    on_after_run:   Callable[[Browser], None] | None = None
```

All fields are optional. A plugin that only populates a shared
registry at module-body time (e.g., `preview_formatters['markdown']
= my_format`) needs no hooks at all — it just imports cleanly.

If `name` is `None` at registration, the framework fills it from
the calling module's `__name__` (walked back one frame). Authors
who want a stable display name override it.

### `register_plugin(cfg: PluginConfig) -> None`

Called at module-body time by plugins that want lifecycle hooks.

```python
# In some_plugin.py
from browse_tui import register_plugin, PluginConfig

def _setup(browser):
    browser.bind('M', show_markdown_help)

register_plugin(PluginConfig(name='markdown-preview', on_before_run=_setup))
```

`register_plugin` appends `cfg` to `browse_tui.registered_plugins`
(the public list — see below). Multiple calls from the same module
are allowed and each appends a separate entry (use case: a single
file registering several loosely related hook sets).

Registration order = call order = actual load order. Transitive
imports register in the order Python executes their bodies, which
is the only order under which `on_before_init` can read a complete
view of what earlier-loaded plugins have set up.

The main recipe may also call `register_plugin`. If it does, the
recipe's hooks fire when the recipe's own `main()` constructs a
Browser — same pipeline as any other plugin, no special-case skip.
A file used both as a main recipe and as a `--plugin` will see its
hooks fire in either mode (mid-`main()` in main mode, before any
`main()` in plugin mode).

### `Context.register_plugin(cfg)`

Pass-through for symmetry with other `Context` shortcuts. Equivalent
to calling `browse_tui.register_plugin(cfg)`.

### `browse_tui.registered_plugins: list[PluginConfig]`

Public read-only view of the registration list, in registration
order. Useful for:

- **Introspection** — plugins can check "is `markdown-preview`
  already registered?" by scanning for a matching `name`.
- **Composition** — a plugin that wraps another's hooks can find
  the target by name and replace/decorate the callable fields.
- **Debugging / tests** — assert which plugins loaded and in what
  order.

The list is fully mutable. Plugins may reorder, remove, replace,
or insert entries as needed. Same trust model as the rest of the
plugin surface — plugins are unprivileged Python and can do
anything Python lets them do.

---

## Hooking patterns

Plugins are unprivileged Python — they have whatever access the
recipe has. Several patterns cover most extension needs:

1. **Populate a shared registry at module-body time.** Cheapest for
   simple "add a thing" plugins (formatters, file-type handlers,
   action factories):

   ```python
   from browse_tui import preview_formatters
   preview_formatters['markdown'] = render_markdown
   ```

   No hooks, no `register_plugin` call. The recipe (or another
   plugin) reads from the registry when it needs to.

2. **Override `BrowserConfig` defaults in `on_before_init`.** Use
   when the plugin wants to influence what the Browser becomes
   regardless of how the recipe configured it:

   ```python
   def _set_defaults(browser, config):
       if config.preview_formatter is None:
           config.preview_formatter = render_markdown
   register_plugin(PluginConfig(on_before_init=_set_defaults))
   ```

3. **Wrap callable fields on `BrowserConfig`.** Compose with what
   the recipe set instead of replacing it:

   ```python
   def _wrap(browser, config):
       prev = config.preview_formatter
       def chained(item):
           text = prev(item) if prev else item.body
           return decorate_with_markdown(text)
       config.preview_formatter = chained
   ```

4. **Monkey-patch the Browser instance in `on_after_init`.** Use
   when the override needs the live Browser (e.g., to access state
   that doesn't exist before construction). Works equally well for
   replacing methods, adding new methods, or attaching plain
   attribute data:

   ```python
   def _patch(browser):
       # Replace a method
       orig = browser.set_preview
       def wrapped(id_, text):
           return orig(id_, render_markdown(text))
       browser.set_preview = wrapped

       # Or just add a new attribute the recipe / other plugins
       # can read off the browser
       browser.markdown = MarkdownState()
   ```

5. **Monkey-patch the `Browser` class at module-body time.** Use
   when the override should apply to *every* Browser, including
   ones constructed before the plugin's hooks run (rare; mostly
   useful for testing and global rewrites). Class-level attributes
   are a convenient namespace for cross-Browser registries:

   ```python
   # Now every Browser instance reads/writes the same registry.
   Browser.preview_formatters = {}
   ```

   Compared to a free-standing module-level dict, this scopes the
   registry to `Browser` (easier to find, easier to reset in tests,
   no extra import path for consumers).

6. **Compose with another plugin via `registered_plugins`.** Find
   the target by name and wrap its hooks:

   ```python
   for cfg in registered_plugins:
       if cfg.name == 'syntax-highlight':
           orig = cfg.on_after_init
           def wrapped(browser):
               if orig: orig(browser)
               attach_extra_lexer(browser)
           cfg.on_after_init = wrapped
   ```

These patterns are not exhaustive — plugins can do anything Python
allows. The framework's contract is just: `BrowserConfig` is read
during `__init__`, the four hooks fire at known points, and
`registered_plugins` is a stable list in registration order.

---

## CLI

### New flag: `--plugin SPEC`

```
browse-tui --plugin foo --plugin bar --run-py recipe.py
browse-tui --plugin /opt/tools/markdown_helper.py --run recipe.py
browse-tui -c cmd -p cmd --plugin foo            # CLI mode (no recipe file)
```

- Repeatable. Order is preserved.
- `SPEC` is either a module name or a filesystem path:
  - **Module name** (no `/`, no `.py` suffix): resolved via
    standard `importlib.import_module(SPEC)`. Must be on `sys.path`
    — see "Module discovery" below.
  - **Path** (contains `/` or ends in `.py`): loaded via
    `importlib.util.spec_from_file_location`. The module is named
    after its basename without `.py` (so `--plugin /a/b/foo.py`
    becomes module `foo`). The file's parent directory is also
    prepended to `sys.path` so the plugin can import its own
    sibling files.
- A failed import propagates as a normal `ImportError` and aborts
  startup — no silent skip.

### Interaction with recipe-runner flags

Recipe runners (`--run`, `--run-py`, `--run-cli`) and the bare
positional consume `argv` after the recipe path. `--plugin` is a
launcher flag, so it must appear before the recipe runner:

```
browse-tui --plugin foo --run recipe.py arg1 arg2     # OK
browse-tui --run recipe.py --plugin foo arg1 arg2     # NOT OK: --plugin goes to recipe
```

This is consistent with how recipe runners already consume `argv`.

### Load order

```
for each --plugin NAME in CLI order:
    importlib.import_module(NAME)     # module body runs, may register_plugin
run the main recipe                   # --run-py: runpy; --run-cli: execvpe; etc.
```

Plugins are fully loaded (module bodies executed, registrations
recorded) before the main recipe is touched. Recipes that `import`
plugins themselves work normally; if a recipe imports a plugin that
was also passed via `--plugin`, the second import is a no-op
(Python's normal import caching).

### `--plugin` with external CLI recipes: hard error

`--plugin` is supported in:

- CLI mode (`browse-tui [flags] --plugin foo`, no recipe file),
- `--run-py SCRIPT` (Python recipe),
- `--run SCRIPT` when auto-detection resolves to Python.

`--plugin` is rejected when:

- `--run-cli SCRIPT`,
- `--run SCRIPT` when auto-detection resolves to `cli`.

The reason is that an external CLI recipe replaces the Python
process via `execvpe`; whatever plugins the outer launcher
imported are thrown away with it. The launcher exits with a clear
error before doing anything:

```
browse-tui: --plugin requires an in-process recipe host (CLI mode
            or a Python recipe).
            --run-cli (or --run resolved as 'cli') replaces the
            Python process with the recipe, so plugins imported by
            the launcher would be discarded.
            To use plugins with an external CLI recipe, pass
            --plugin to the inner 'browse-tui' invocation inside
            the recipe script.
```

We don't auto-propagate via env vars (a hostile env var would
otherwise cause arbitrary Python to load on every invocation), and
we don't silently drop the request — silent drops are exactly the
class of behavior the "propagate exceptions, don't catch" rule
exists to prevent. A confused user staring at a non-working
plugin is worse than an explicit "this combination is not
supported."

### Module discovery (`sys.path`)

At process start, the launcher prepends three locations to
`sys.path`, in this order:

1. The directory containing the running `browse-tui` binary.
2. The directory of the main recipe (for `--run-py` / `--run` /
   bare positional). Skipped for non-Python main types.
3. For each path-form `--plugin SPEC`: the parent directory of
   that file.

This lets the natural "drop files in a directory" distribution
work three ways:

- Plugins shipped with browse-tui (alongside the binary) are
  always importable by short name.
- A recipe and its companion plugins/helpers can live in the same
  directory; the recipe can `import` them and `--plugin SHORTNAME`
  resolves automatically.
- Standalone plugins live wherever the user wants — point at them
  with a path, and they can still `import` their own helpers from
  the same directory.

No special plugin directory or `BROWSE_TUI_PLUGIN_PATH` env var.
Recipes live in `$PATH` (executable scripts); plugins can live in
the same directories and resolve by name once `--plugin` is given
a path or once a sibling recipe pulls them onto `sys.path`.

---

## Semantics

### Hook firing

```python
class Browser:
    def __init__(self, config: BrowserConfig):
        for cfg in registered_plugins:
            if cfg.on_before_init:
                cfg.on_before_init(self, config)
        # ... construction reads from config, not from kwargs ...
        for cfg in registered_plugins:
            if cfg.on_after_init:
                cfg.on_after_init(self)

    def run(self):
        for cfg in registered_plugins:
            if cfg.on_before_run:
                cfg.on_before_run(self)
        try:
            ... event loop ...
        finally:
            for cfg in registered_plugins:
                if cfg.on_after_run:
                    cfg.on_after_run(self)
```

Order is registration order on each pass — no reverse for teardown.
`on_after_run` runs in a `finally` so cleanup hooks fire even when
the event loop exits via exception.

### `on_before_init`: `self` is empty

The Browser instance passed to `on_before_init` is at the top of
`__init__`; almost no attributes are set yet. Plugins should treat
the `browser` argument as an identity (e.g., for indexing per-Browser
state) and use `config` to influence what the Browser becomes.
Reading Browser attributes in `on_before_init` is undefined.

### `config` mutation

`config` is the live `BrowserConfig` instance the Browser will read
from. Plugins set fields to override defaults:

```python
def on_before_init(browser, config):
    if config.preview_formatter is None:
        config.preview_formatter = markdown_format
```

Order matters when plugins conflict — last writer wins, which
matches CLI load order (last-loaded plugin gets last word).

### Exception propagation

A plugin's module body, `register_plugin` call, or any of its four
hooks may raise. Exceptions propagate unchanged. The framework does
not catch, log, and continue — a broken plugin produces a clear
traceback at startup or at the hook site. This is consistent with
how a broken recipe behaves today.

### Module identity and double registration

`register_plugin` does not deduplicate. Calling it twice from the
same module appends two entries, both fire. If a plugin is
imported twice (e.g., via `--plugin foo` and recipe `import foo`),
Python's import system runs the module body once, so
`register_plugin` is called once.

### `--plugin` and recipe `import` overlap

Same module loaded by both channels: imported once, registered
however many times the module body explicitly called
`register_plugin`. No deduplication of registrations beyond
Python's own import caching of module bodies.

### Hook firing under external CLI recipes

`--run-cli` recipes execute an external script that typically
invokes `browse-tui` again (in CLI mode) for the actual interface.
The inner `browse-tui` process constructs a `Browser`, which fires
all four hooks normally for whatever plugins were passed to that
inner call. Combining `--run-cli` with an outer `--plugin` is
rejected at launcher startup (see the CLI section).

### `Context.register_plugin`

Calling `ctx.register_plugin(cfg)` *during* a Browser's lifetime
appends to the global list but is essentially useless for the
current Browser — `__init__` hooks have already fired, and
registrations only affect future Browser constructions. The
pass-through exists for symmetry, not for runtime use.

---

## Test plan

Tests live in `test/unit/test_plugins.py` (new file). UI test
optional (`test/ui/test_plugins.py`).

### `BrowserConfig` plumbing

- `BrowserConfig` defaults match the prior `Browser(**kwargs)`
  defaults for every existing keyword.
- `Browser(BrowserConfig(title='X'))` produces a Browser with the
  same observable state as the old `Browser(title='X')`.
- All existing recipes updated to the new shape continue to work.

### `register_plugin` mechanics

- `register_plugin(PluginConfig())` appends one entry.
- Two calls from the same module append two entries.
- `name=None` resolves to the caller's `__name__` after
  registration; an explicit `name='foo'` survives unchanged.
- Calling `register_plugin` outside a module body (e.g., from a
  REPL with no module frame) still works and uses a placeholder
  name.
- `browse_tui.registered_plugins` returns the same list, in
  registration order; appended entries appear immediately.

### CLI plumbing

- `browse-tui --plugin foo --run-py recipe.py` imports `foo`
  before running `recipe.py`.
- `--plugin foo --plugin bar` imports in order.
- Repeated `--plugin foo --plugin foo` imports once (Python cache).
- Missing plugin: `--plugin nonexistent` produces a plain
  `ImportError` and exits non-zero.
- Path form: `--plugin /tmp/X.py` loads from path, registers under
  module name `X`, adds `/tmp/` to `sys.path`.
- `sys.path` at process start contains, in order: browse-tui
  binary dir, main recipe dir (when Python), each path-plugin's
  parent dir.
- `--run-cli script.sh --plugin foo` (and `--run` resolving to
  `cli`) exits with a clear error before running anything.
- CLI mode (recipe-less): `browse-tui -c CMD -p CMD --plugin foo`
  loads `foo` and runs normally.
- The inner `browse-tui --plugin foo` inside an external CLI
  recipe script still works — it's a normal CLI-mode invocation.

### Hook firing

- All four hooks fire when defined; missing hooks (None) are skipped
  silently.
- Hook firing order matches registration order.
- `on_before_init`: receives the partially-built Browser and the
  `BrowserConfig`; mutations to `config` are visible to subsequent
  construction.
- `on_after_init`: receives the constructed Browser; reads of
  Browser attributes work.
- `on_before_run` / `on_after_run`: fire at the start and end of
  `Browser.run()`; `on_after_run` fires even if the event loop
  raises.

### Exception propagation

- Module body raising: launcher exits with the traceback.
- `register_plugin` raising: ditto.
- `on_before_init` raising: `Browser.__init__` does not return; the
  exception surfaces in the recipe's `main()`.
- `on_after_run` raising during event-loop exception: the original
  exception is the one surfaced (Python's standard `finally`
  semantics).

### Main-recipe-as-plugin

- A recipe that calls `register_plugin` during its module body sees
  its hooks fire when its own `main()` constructs a Browser.
- The same file passed via `--plugin` instead of `--run-py` has its
  hooks fire but its `main()` is not invoked.

### Introspection and composition

- `browse_tui.registered_plugins` contains exactly the entries
  appended by `register_plugin` calls, in order.
- A plugin loaded second can find a plugin loaded first by name and
  wrap its `on_after_init` field; the wrapped function fires in the
  expected order.
- The list is mutable: removing or reordering entries from plugin
  code affects subsequent Browser constructions as expected (no
  framework-side immutability guard).

### Plugin-to-plugin import order

- Plugin `a` imports plugin `b` at module body. CLI:
  `--plugin a --plugin b`. Registration order is `b, a` (b's body
  completes inside a's body, registers first).
- Hooks fire in `b, a` order.

---

## Open questions

None.

---

## Implementation outline (informational)

1. **`BrowserConfig` extraction** (`src-tui/040-state.py` or new
   file)
   - Define `BrowserConfig` dataclass with every existing kwarg.
   - `Browser.__init__(config: BrowserConfig)` reads fields from
     `config` instead of `kwargs`.
   - Update all in-tree recipes to construct `BrowserConfig`.

2. **Plugin registration primitive** (`src-tui/010-prelude.py` or
   new file)
   - `PluginConfig` dataclass.
   - Module-level `registered_plugins: list[PluginConfig]`.
   - `register_plugin(cfg)` — fills `name` from caller frame if
     unset, appends.
   - Export `PluginConfig`, `register_plugin`, `registered_plugins`
     from the public `browse_tui` API.

3. **Hook firing** (`src-tui/040-state.py`)
   - Wrap `Browser.__init__` body with the two construction hooks.
   - Wrap `Browser.run` body with the two run hooks (`finally` for
     `on_after_run`).

4. **CLI flag** (`src-tui/080-cli.py`)
   - Add `--plugin` (repeatable) to the argparse parser.
   - In recipe-mode early dispatch (the `_RECIPE_FLAGS` path), pull
     `--plugin` from `argv` before handing off to the runner so
     plugins specified before the recipe path are extracted
     regardless of dispatch mode.
   - Before invoking the runner: for each SPEC, classify as
     name-or-path and import accordingly (`importlib.import_module`
     or `importlib.util.spec_from_file_location`).
   - If `--plugin` is present and the dispatched mode is external
     CLI recipe (`--run-cli`, or `--run` auto-detected as `cli`):
     print the error message described in the CLI section and exit
     non-zero before any import or `execvpe`.

5. **`sys.path` setup** (`src-tui/080-cli.py`)
   - At process start, prepend (1) the directory of the running
     `browse-tui` binary, (2) the main recipe directory (when
     Python), (3) each path-form `--plugin`'s parent directory.

6. **Context pass-through** (`src-tui/060-context.py`)
   - `Context.register_plugin(cfg)` calls the module function.

7. **Docs** (`docs/api.md`)
   - Public-API section for `BrowserConfig`, `PluginConfig`,
     `register_plugin`, `registered_plugins`.
   - Lifecycle-hook table with where each fires.
   - Hooking-patterns section (mirrors the spec).
   - Note on `--plugin` CLI flag and `sys.path` setup.

8. **Tests** (`test/unit/test_plugins.py`, optional UI test).
