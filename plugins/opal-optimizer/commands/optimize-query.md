---
name: optimize-query
description: "Set up and launch autonomous OPAL query optimization"
argument-hint: "[optimization focus]"
model: opus
---

You are setting up an autonomous OPAL query optimization run.

## Step 1: Get the query to optimize

Ask the user for:
1. **The OPAL query** — either pasted directly, or a description of what they want optimized (in which case you'll use the Observe MCP tools or CLI to get the current query)
2. **Input dataset(s)** — dataset ID(s) or names for the query
3. **Time range** — how far back to query (e.g., `-r 1h`, `-r 4h`, `-r 24h`). Default to `-r 1h` for fast iterations.
4. **Optimization focus** (optional) — what to prioritize: execution speed, reducing data scanned, simplifying the query, or "all of the above". If the user provided an argument to this command, use that.

## Step 2: Verify the Observe CLI

```bash
~/go/bin/observe list dataset 2>&1 | head -3
```

If this fails, the CLI is not configured. Tell the user to run:
```bash
~/go/bin/observe --customerid <ID> --site observeinc.com login <EMAIL> --sso
```

## Step 3: Run the baseline

Save the original query to a file for reference:
```bash
mkdir -p /tmp/opal-optimizer
cat > /tmp/opal-optimizer/baseline.opal << 'OPAL'
<THE OPAL QUERY>
OPAL
```

Run the baseline and capture timing:
```bash
time ~/go/bin/observe query -f /tmp/opal-optimizer/baseline.opal -i '<DATASET>' -r <RANGE> --json > /tmp/opal-optimizer/baseline_results.json 2>/tmp/opal-optimizer/baseline_timing.txt
```

Extract metrics:
```bash
echo "=== Baseline Metrics ==="
echo "Rows: $(wc -l < /tmp/opal-optimizer/baseline_results.json)"
echo "Timing:" && cat /tmp/opal-optimizer/baseline_timing.txt
echo "Query lines: $(wc -l < /tmp/opal-optimizer/baseline.opal)"
```

## Step 4: Initialize results tracking

Create the results log:
```bash
cat > /tmp/opal-optimizer/results.tsv << 'TSV'
iteration	exec_time_s	rows	opal_lines	status	description
0	<baseline_time>	<baseline_rows>	<baseline_lines>	keep	baseline
TSV
```

## Step 5: Generate the dashboard

Create an HTML dashboard at `/tmp/opal-optimizer/dashboard.html` that:
- Auto-refreshes every 15 seconds by re-reading `/tmp/opal-optimizer/results.tsv`
- Shows a line chart of execution time over iterations (kept variants only)
- Shows summary: best time, total iterations, keeps/discards
- Shows the full iteration history table
- Shows the current best OPAL query (read from `/tmp/opal-optimizer/best.opal`)
- Uses a dark theme

Save the baseline as the current best:
```bash
cp /tmp/opal-optimizer/baseline.opal /tmp/opal-optimizer/best.opal
```

Open the dashboard:
```bash
open /tmp/opal-optimizer/dashboard.html
```

## Step 6: Dispatch the optimizer agent

Tell the user the baseline is captured and the dashboard is open. Then dispatch the `opal-optimizer` agent with this prompt:

"You are optimizing an OPAL query. Input dataset: `<DATASET>`. Time range: `<RANGE>`. <If focus provided: Optimization focus: <focus>.> The baseline query is at /tmp/opal-optimizer/baseline.opal and the current best is at /tmp/opal-optimizer/best.opal. Results are tracked in /tmp/opal-optimizer/results.tsv. Begin the optimization loop now."
