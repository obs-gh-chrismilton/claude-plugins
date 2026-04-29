---
description: Review pending Observe learning candidates from prior sessions
allowed-tools: ["Bash", "Read", "Edit", "Write"]
---

# /observe-review

Review and merge, discard, or edit pending learning candidates captured in prior sessions.

## Workflow

1. Read `~/.claude/agents/.observeie-pending.md`. If empty or missing, report "No pending candidates" and stop.

2. Render the candidate table in this format:

```
📋 N learning candidates pending review:

  #1 [high]  OPAL Gotchas
             "OPAL rejects '7d'; use '168h'"  (EchoNet, 2026-04-29)
  #2 [med]   Object Management
             "Cascade-ordering deadlock..."   (EchoNet, 2026-04-29)
```

3. Prompt the user: "Reply: `merge all` / `merge 1, 3` / `discard 2` / `edit 1` / `defer`."

## Actions

When the user replies:

- **`merge N` or `merge all`** — Invoke the merge pipeline for each approved candidate:
  ```bash
  cd ~/repos/claude-plugins/plugins/observe-learning-capture
  python3 -m pipeline.merge_cli --merge ID
  ```
  Look up the candidate ID from the table position (ID = position number or explicit ID from the YAML record).

- **`discard N`** — Invoke the discard pipeline:
  ```bash
  cd ~/repos/claude-plugins/plugins/observe-learning-capture
  python3 -m pipeline.merge_cli --discard ID
  ```

- **`edit N`** — Open the YAML record from `~/.claude/agents/.observeie-pending.md` for the user to edit, save the file, then run the merge pipeline on that candidate.

- **`defer`** — No action. Candidates remain in queue for next session.

4. After all actions complete, confirm with: "Merged N, discarded M, deferred K."

## Customer-Specific Filtering

If a candidate looks customer-specific (mentions a tenant ID, a customer-named dataset, a customer contact), recommend `discard` or `edit` to redirect — those facts belong in `~/Work/<Customer>/CLAUDE.md`, not in the shared ObserveIE knowledge base.
