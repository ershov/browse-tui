#!/bin/bash
set -euo pipefail

# Locate the plan file using the same discovery logic as `plan`:
# 1. PLAN_MD env var
# 2. .PLAN.md at git root
plan_file="${PLAN_MD:-}"

if [ -z "$plan_file" ]; then
    git_root=$(git rev-parse --show-toplevel 2>/dev/null || true)
    if [ -n "$git_root" ] && [ -f "$git_root/.PLAN.md" ]; then
        plan_file="$git_root/.PLAN.md"
    fi
fi

if [ -z "$plan_file" ] || [ ! -f "$plan_file" ]; then
    # No plan file found — nothing to inject
    exit 0
fi

# Show current ticket status
status=$(plan -f "$plan_file" list 2>/dev/null || true)

if [ -n "$status" ]; then
    cat <<EOF
Plan file: $plan_file

Current tickets:
$status
EOF
fi
