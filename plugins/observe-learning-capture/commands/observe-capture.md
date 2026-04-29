---
description: Force-capture the last turn into the pending queue (bypass prefilter)
allowed-tools: ["Bash"]
---

# /observe-capture

Force-capture the last assistant turn into the pending learning queue, bypassing the Stop-hook prefilter. Useful when you noticed a learning opportunity that the prefilter would have skipped.

## Workflow

1. Run the classifier unconditionally on the last assistant turn:
   ```bash
   cd ~/repos/claude-plugins/plugins/observe-learning-capture
   python3 -m pipeline.runner \
     --mode stop \
     --transcript "${CLAUDE_TRANSCRIPT_PATH}" \
     --session-id "${CLAUDE_SESSION_ID}" \
     --cwd "$(pwd)"
   ```

2. Read `~/.claude/agents/.observeie-pending.md` and report how many candidates were added.

3. Report: "Captured N candidate(s); surface at next session start or run `/observe-review` to review now."

## Use Cases

- You noticed a subtle pattern or insight that the automated prefilter deemed too low-value.
- The assistant provided a workaround, gotcha, or integration detail worth capturing.
- End-of-session "that was useful, capture it" moment.
