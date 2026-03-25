---
name: optimization-status
description: "Quick status check on the current OPAL query optimization run"
---

Check the current OPAL optimization status:

```bash
echo "=== Optimization Status ===" && \
echo "--- Best Result ---" && \
tail -n +2 /tmp/opal-optimizer/results.tsv | grep 'keep' | sort -t$'\t' -k2 -n | head -1 && \
echo "--- Last 5 Iterations ---" && \
tail -5 /tmp/opal-optimizer/results.tsv && \
echo "--- Totals ---" && \
echo "Keeps: $(grep -c 'keep' /tmp/opal-optimizer/results.tsv)" && \
echo "Discards: $(grep -c 'discard' /tmp/opal-optimizer/results.tsv)" && \
echo "Errors: $(grep -c 'error' /tmp/opal-optimizer/results.tsv)" && \
echo "Total: $(tail -n +2 /tmp/opal-optimizer/results.tsv | wc -l | tr -d ' ')" && \
echo "--- Current Best Query ---" && \
cat /tmp/opal-optimizer/best.opal
```

Format the output as a clean summary. Highlight the best execution time and how it compares to the baseline.
