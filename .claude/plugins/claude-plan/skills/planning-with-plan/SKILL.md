---
name: planning-with-plan
description: Use when you need to plan, track, or execute a multi-step implementation task. Replaces ad-hoc markdown checklists and TodoWrite with the `plan` CLI ticket tracker. Use for any task with 3+ steps.
---

# Planning with Plan

Use the `plan` CLI to create, track, and execute implementation tasks in a structured ticket hierarchy stored in `.PLAN.md`.

**Announce at start:** "I'm using the planning-with-plan skill to track this work."

## When to Use

- Multi-step implementation tasks (3+ steps)
- Feature development requiring a breakdown
- Any work where you would normally create a TodoWrite checklist or an md plan file

## Quick Reference

```bash
plan create 'title="Step name"'              # Create top-level task
plan create PARENT 'title="Subtask"'          # Create subtask under PARENT
plan list                                     # List all tickets
plan list is_open                             # Filter to open tickets
plan 'status == "in-progress"' list           # Filter to in-progress tickets
plan -p is_open list                          # Open tickets with ancestor path
plan N                                        # View ticket content
plan N status in-progress                     # Mark as in-progress
plan N close                                  # Mark as done
plan N comment add "Note"                     # Add a note
plan list ready                               # Show actionable tickets
plan list order                               # Show execution order

```

## The Process

### Step 1: Break Down the Work

Create a ticket hierarchy. For multiple tickets, write them as markdown:

```bash
plan create [parent] - <<'EOF'
* ## Epic: Implement feature X

  Feature description and acceptance criteria.

  * ## Write failing tests

    Test the core behavior.

  * ## Implement core logic

    Build the feature.

  * ## Update documentation

    Update relevant docs.
EOF
```

Tickets are executed in creation order — list them in the sequence you want.
Use `plan move` to reorder after creation. For cross-branch dependencies,
use `{#placeholder}` IDs and `links: blocked:#placeholder` to cross-reference
between new tickets:

```bash
* ## Set up database {#db}
    ...
* ## Build API {#api}
        links: blocked:#db
    ...
```

For single tickets, use the expression syntax:

```bash
plan create 'title="Quick fix"'
plan create 1 'title="Subtask under #1"'
```

### Step 2: Execute Tasks

Work through tickets in order. For each:

1. **Start it:** `plan N status in-progress`
2. **Do the work** — follow the ticket description, write code, run tests.
3. **Add notes** as you go: `plan N comment add "Discovered edge case"`
4. **Close it:** `plan N close`
5. **Check what's next:** `plan list ready` or `plan list order`

### Step 3: Report Progress

Between batches of work, show the user where things stand:

```bash
plan list --format 'f"{indent}#{id} [{status}] {title}"'
```

### Step 4: Adapt

If new work surfaces: `plan create PARENT 'title="Handle newly discovered case"'`
If a task is unnecessary: `plan N close wontfix`
If you need to restructure a ticket body or subtree: `plan edit --start N`, edit the file, `plan edit --accept`.

## Replacing TodoWrite

| TodoWrite | plan equivalent |
|-----------|-----------------|
| `TaskCreate(subject="...")` | `plan create 'title="..."'` |
| `TaskUpdate(id, status="in_progress")` | `plan N status in-progress` |
| `TaskUpdate(id, status="completed")` | `plan N close` |
| `TaskList()` | `plan list --format 'f"{indent}#{id} [{status}] {title}"'` |

## Integration

- For subagent dispatch, use **claude-plan:dispatch-with-plan**
- For persistent named agents with assignees, use **claude-plan:team-with-plan**
