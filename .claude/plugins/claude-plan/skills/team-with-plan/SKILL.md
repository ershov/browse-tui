---
name: team-with-plan
description: Use when coordinating multiple subagents or a team of agents on implementation tasks. Replaces TaskCreate/TaskUpdate/TaskList with the `plan` CLI for richer coordination via tickets, comments, queries, and hierarchy.
---

# Team Coordination with Plan

Coordinate a team of subagents using the `plan` CLI ticket tracker instead of the built-in task system.

**Announce at start:** "I'm using the team-with-plan skill to coordinate this work."

## When to Use

- Dispatching 2+ subagents on implementation work
- Complex multi-task plans requiring coordination

## Leader Role

When coordinating subagents, you are the coordinator — do not implement tickets yourself.
Delegate all implementation work to subagents. Your job: create tickets, dispatch workers, monitor progress, review results.
If a worker fails, dispatch a new agent to fix it — do not fix it yourself.

## Quick Reference — Leader

```bash
plan create 'title="Task name", assignee="agent-name"'   # Create and assign
plan list --format 'f"{indent}#{id} [{status}] {assignee}: {title}"'     # Dashboard
plan list ready                                           # Actionable items
plan list order                                           # Execution order
plan N comment add "Feedback note"                        # Add feedback
```

## Quick Reference — Agent

```bash
plan 'assignee == "my-name" and is_open' list                  # My work
plan N status in-progress                                      # Start
plan N -r ls                                                   # Structured view of subtasks
plan N -r ls order                                             # Subtasks in execution order
plan N comment add "Found issue, fixing"                       # Note
plan N close                                                   # Done
plan list ready                                                # What's next
plan list order                                                # Execution order
```

## Agent Prompt Template

Include this in subagent dispatch prompts:

```
## Task Tracking

Use the `plan` CLI to manage your assigned work:

- Find your tasks: plan 'assignee == "YOUR-NAME" and is_open' list
- View a task: plan N
- Structured view of subtasks: plan N -r ls
- Subtasks in execution order: plan N -r ls order
- Start work: plan N status in-progress
- Add notes: plan N comment add "Description of what you did or found"
- Complete: plan N close
- Check for more: plan list ready (or plan list order)
- Create subtasks if needed: plan create PARENT 'title="New subtask", assignee="YOUR-NAME"'
- If blocked: plan N comment add "Blocked: reason"
- For body restructuring: plan edit --start N, edit the file, plan edit --accept
```

## Integration

- Use **claude-plan:planning-with-plan** to create the initial work breakdown
- Compatible with TeamCreate for agent lifecycle management
