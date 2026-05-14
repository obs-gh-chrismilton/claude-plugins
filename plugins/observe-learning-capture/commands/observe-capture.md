---
description: Force-capture the last turn into the pending queue (bypass prefilter)
allowed-tools: ["Bash"]
---

# /observe-capture

Force-capture the last assistant turn into the pending learning queue, bypassing the Stop-hook prefilter. Useful when you noticed a learning opportunity that the prefilter would have skipped.

## Workflow

1. **Snapshot the candidate count BEFORE invocation** so the post-run report can show the delta (not the cumulative total):
   ```bash
   PRE_COUNT=$(grep -c '^title:' ~/.claude/agents/.observeie-pending.md 2>/dev/null || echo 0)
   ```

2. **Run the classifier unconditionally on the last assistant turn.** The leading `unset ANTHROPIC_API_KEY` is critical: per the global CLAUDE.md "Anthropic API Key Policy", `claude -p` must use the parent session's MAX-subscription keychain credential, not an inherited API-key env var. The classifier ALSO strips these vars at the Python subprocess boundary (defense in depth), but unsetting here gives the user a visible single mitigation in case they audit this command:
   ```bash
   cd ~/repos/claude-plugins/plugins/observe-learning-capture
   unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
   python3 -m pipeline.runner \
     --mode stop \
     --transcript "${CLAUDE_TRANSCRIPT_PATH}" \
     --session-id "${CLAUDE_SESSION_ID}" \
     --cwd "$(pwd)"
   ```

3. **Compute and report the delta.** The pending file is append-only within a session, so post-count minus pre-count is the number of new candidates this invocation produced (including any `[FAILURE] classifier` markers — surface those clearly):
   ```bash
   POST_COUNT=$(grep -c '^title:' ~/.claude/agents/.observeie-pending.md 2>/dev/null || echo 0)
   NEW=$((POST_COUNT - PRE_COUNT))
   FAILURE_MARKERS=$(grep -c 'FAILURE\] classifier' ~/.claude/agents/.observeie-pending.md 2>/dev/null || echo 0)
   ```

4. Report:
   - `"Captured N candidate(s); surface at next session start or run /observe-review to review now."`
   - If `NEW == 0`: `"No new candidates captured (turn may not have contained surfaceable learnings; this is normal for routine acks/prose)."`
   - If any `[FAILURE] classifier` marker appears in the delta: `"⚠ Classifier produced a failure marker — run /observe-review to see the diagnostic. Likely cause: auth/credit issue or unexpected CLI output."`

## Use Cases

- You noticed a subtle pattern or insight that the automated prefilter deemed too low-value.
- The assistant provided a workaround, gotcha, or integration detail worth capturing.
- End-of-session "that was useful, capture it" moment.

## Troubleshooting

- **"Credit balance is too low" appearing in the marker fact** — means the env-strip protection in classifier.py is NOT active (downgrade or the file was reverted). Verify `_invoke_classifier` in `pipeline/classifier.py` still strips `ANTHROPIC_API_KEY` from the subprocess env.
- **Repeated `[FAILURE] classifier` markers with `claude -p exited 1: (no stderr) | (no stdout)`** — almost always the `claude` CLI not being on the PATH in the subshell. Check `which claude`.
- **No new candidates EVER captured** — confirm the Stop hook is firing by `tail -f ~/.claude/logs/observe-learning-capture.log` while the session runs. Look for `[stop-hook] prefilter passed — invoking classifier` lines.
