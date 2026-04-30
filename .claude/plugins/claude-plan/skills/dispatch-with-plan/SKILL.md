---
name: dispatch-with-plan
description: Use when dispatching subagents (via the Agent tool) to work on tickets from the plan. You are the leader — you pick tickets, dispatch workers one at a time, and track progress.
---

# Dispatch with Plan

Dispatch subagents via the Agent tool to execute tickets from `.PLAN.md`. You are the leader — you pick tickets in order, send them to workers, and track progress.

**Announce at start:** "I'm using the dispatch-with-plan skill to coordinate subagents."

## When to Use

- You have tickets in `.PLAN.md` and want subagents to execute them
- Single leader dispatching workers via the Agent tool

Getting tickets overview/list:
- `plan N -r ls` gives structured view of subtasks.
- `plan N -r ls order` gives subtasks in order of execution.

## Leader Role

You are the coordinator — do not implement tickets yourself.
Delegate all implementation work to subagents. Your job: pick tickets, dispatch workers, review results.
If a worker fails, dispatch a new agent to fix it — do not fix it yourself.

## The Dispatch Loop

### 1. Get the next ticket

```bash
plan list order -n 1
```

### 2. Read the ticket

```bash
plan N
```

### 3. Dispatch a worker subagent

Use the Agent tool. Paste the full ticket content into the prompt — do not make the worker read the plan file. Use the worker prompt template below.

### 4. Process the return

When the worker returns, review the result. Then get the next ticket and repeat.

## Worker Prompt Template

Include this in the Agent tool prompt when dispatching:

```
You are implementing ticket #N: [TITLE]

## Ticket Content

[PASTE FULL TICKET CONTENT HERE]

## Your Workflow

1. Start the ticket: plan N status in-progress
2. Do the work — follow the ticket description.
3. Add notes as you go: plan N comment add "What you did or found"
4. If you discover additional work, create a subticket of your current ticket:
   plan create N 'title="Discovered subtask"'
   If the issue is out of scope of this ticket, create it elsewhere.
5. When done: plan N close
6. Report back: what you implemented, files changed, any issues.
7. For body restructuring: plan edit --start N, edit the file, plan edit --accept.
```

## Integration

- Use **claude-plan:planning-with-plan** to create the initial ticket breakdown
- Use **claude-plan:team-with-plan** instead for persistent named agents with assignees
