
## Task tracking

Use the `plan` CLI for ALL task tracking.

### Before starting work
- Break the task into tickets: `plan create 'title="Step name"'`
- For subtasks: `plan create PARENT 'title="Subtask"'`
- Create tickets in preferred execution order (or reorder with `plan move`)
- Put details in each subtask body (`plan N add "what to do"`), not as a TODO list in the parent
- Review the breakdown: `plan list`

### While working
- Before starting a ticket: `plan N status in-progress`
- After completing a ticket: `plan N close`
- Add notes when useful: `plan N comment add "What happened"`
- If new work surfaces: `plan create PARENT 'title="New task"'`
- If a task is unnecessary: `plan N close wontfix`
- Check what's next: `plan list ready` or `plan list order`

### Reporting progress
- Show status: `plan list --format 'f"{indent}#{id} [{status}] {title}"'`

### For subagents / team workers
When dispatching subagents, you are the coordinator — do not implement tickets yourself.

Include these instructions in the subagent prompt:
- Find your tasks: `plan 'assignee == "YOUR-NAME" and is_open' list`
- View a task: `plan N`
- Structured view of subtasks: `plan N -r ls`
- Subtasks in execution order: `plan N -r ls order`
- Start work: `plan N status in-progress`
- Add notes: `plan N comment add "Description of what you did"`
- Complete: `plan N close`
- Check for more: `plan list ready` or `plan list order`
- Create subtasks if needed: `plan create PARENT 'title="Subtask", assignee="YOUR-NAME"'`
