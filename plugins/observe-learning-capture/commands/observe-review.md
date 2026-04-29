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

When the user replies, resolve their position references (#1, #2) to the
actual 8-char hash `id` field from the YAML record (e.g. `a3f7e1c2`).
**Never pass the table position number to merge_cli — it expects the hash.**

For each approved candidate:
- `merge N` → `python3 -m pipeline.merge_cli --merge {hash_id}` from
  `~/repos/claude-plugins/plugins/observe-learning-capture/`
- `merge all` → loop over all pending records, run `--merge` for each
- `discard N` → `python3 -m pipeline.merge_cli --discard {hash_id}`
- `edit N` → open the YAML record from the pending file for the user to
  edit, save, then `--merge` with the hash id.
- `defer` → no action; candidates remain in queue for next session.

4. After all actions complete, confirm with: "Merged N, discarded M, deferred K."

## Customer-Specific Filtering

If a candidate looks customer-specific (mentions a tenant ID, a customer-named dataset, a customer contact), recommend `discard` or `edit` to redirect — those facts belong in `~/Work/<Customer>/CLAUDE.md`, not in the shared ObserveIE knowledge base.
