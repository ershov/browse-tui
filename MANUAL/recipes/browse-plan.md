# recipes/browse-plan

Drop-in replacement for `plan-tui` on the `browse-tui` core.

**One-line summary:** a full plan-tui port — same keybindings, same
behaviour, same on-disk format. Doubles as the parity validator for the
abstraction.

**Demonstrates:**

- Subprocess-driven `get_children` — shells out to the `plan` CLI and parses
  tab-separated output.
- `ctx.pick(label, options)` — fzf-style picker for status changes.
- `ctx.run_external` + `ctx.page` — edit ticket via `$EDITOR`, view via
  bat/less.
- `ctx.insert(label, on_confirm)` — full insert mode for create / move
  flows.
- Synthetic root rows — a non-expandable "Project" entry above the real
  tree (mirrors plan-tui's UX).
- Mixed-type ids (integers for tickets, `0` for the synthetic project).
- Multi-target actions with target filtering (`ctx.targets` minus the
  synthetic id).

**Usage:**

```bash
./recipes/browse-plan          # full project tree
./recipes/browse-plan 5        # drill into ticket 5 (initial-scope)
```

Keys: `s` status (picker), `e`/`E` edit (recursive), `v`/`V` view
(recursive), `c`/`C` create (bulk), `m` move, `x` close, `o` reopen, `~`
project log.

**Source:** [`recipes/browse-plan`](../../recipes/browse-plan)

---

*[← All recipes](../recipes.md)*
