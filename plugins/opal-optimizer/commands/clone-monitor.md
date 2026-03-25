---
name: clone-monitor
description: "Clone an existing Observe monitor, optimize its OPAL, validate it still fulfills its purpose, and create a new version"
argument-hint: "<monitor-id>"
model: opus
---

You are cloning and optimizing an Observe monitor. This creates a new version (V+1) with an optimized OPAL query that still fulfills the original monitor's purpose.

## Step 1: Get the monitor ID

If the user provided an argument, use that as the monitor ID. Otherwise, ask them for it.

## Step 2: Fetch the original monitor config

Pull the full config via the Observe API. Read the auth credentials from `~/.config/observe.yaml` first:

```bash
CUSTOMER_ID=$(python3 -c "import yaml; c=yaml.safe_load(open('$HOME/.config/observe.yaml')); print(c['profile']['default']['customerid'])")
AUTH_TOKEN=$(python3 -c "import yaml; c=yaml.safe_load(open('$HOME/.config/observe.yaml')); print(c['profile']['default']['authtoken'])")
SITE=$(python3 -c "import yaml; c=yaml.safe_load(open('$HOME/.config/observe.yaml')); p=c['profile']['default']; print(p.get('site','observeinc.com'))")

curl -s -H "Authorization: Bearer $CUSTOMER_ID $AUTH_TOKEN" \
  "https://$CUSTOMER_ID.$SITE/v1/monitors/$MONITOR_ID" | python3 -m json.tool > /tmp/opal-optimizer/original_monitor.json
```

If the response is a 404, the monitor was deleted. Tell the user and ask if they have the OPAL or want to pick a different monitor.

## Step 3: Analyze the monitor's purpose

Read the full config and determine:

1. **What it monitors** — error rates, latency, throughput, anomalies
2. **What triggers an alert** — the threshold, promote condition, or count rule
3. **What it groups by** — the dimensions that create unique alert streams
4. **What it excludes** — any hardcoded endpoint/service exclusions and why they might be there
5. **Where alerts go** — webhook URLs, Slack channels, email addresses from actionRules
6. **How often it runs** — scheduling interval and freshness goal
7. **The severity level** — Critical, Error, Warning, Informational

Write a purpose statement summarizing all of this. Present it to the user for confirmation before proceeding.

Example: "This monitor watches for services where the 5xx error rate increased by 10% or more compared to last week. It groups by environment/service/endpoint, excludes 15 known-noisy endpoints, runs every 30 minutes at Critical severity, and sends webhooks to Slack channel [URL]. Its purpose is to detect error rate regressions relative to the previous week's baseline."

## Step 4: Determine the version number

Look at the monitor name. If it already contains a version indicator (V2, V3, v4, etc.), increment it. If not, the new version is V2.

Examples:
- "ARC Critical Alerts" → "ARC Critical Alerts V2"
- "ARC Critical Alerts V3" → "ARC Critical Alerts V4"
- "finetunning of ARC Critical Alerts" → "ARC Critical Alerts V2" (also clean up the name)

## Step 5: Extract and save the baseline OPAL

Extract the OPAL pipeline from the monitor config and save it:

```bash
python3 -c "
import json
with open('/tmp/opal-optimizer/original_monitor.json') as f:
    m = json.load(f)
stages = m['definition']['inputQuery']['stages']
for s in stages:
    print(s['pipeline'])
" > /tmp/opal-optimizer/baseline.opal
```

Also save the full monitor config for later use when creating the V2:
```bash
cp /tmp/opal-optimizer/original_monitor.json /tmp/opal-optimizer/original_config.json
```

## Step 6: Run the baseline and capture metrics

Identify the input dataset ID from the config, then run the baseline:

```bash
DATASET_ID=$(python3 -c "
import json
with open('/tmp/opal-optimizer/original_monitor.json') as f:
    m = json.load(f)
print(m['definition']['inputQuery']['stages'][0]['input'][0]['datasetId'])
")

TIMEFORMAT='%R'; { time ~/go/bin/observe query -f /tmp/opal-optimizer/baseline.opal -i "$DATASET_ID" -r 1h --json > /tmp/opal-optimizer/baseline_results.json 2>&1 ; } 2>/tmp/opal-optimizer/baseline_timing.txt
```

Initialize results.tsv and best.opal, generate the dashboard, and open it.

## Step 7: Dispatch the optimizer agent

Dispatch the `opal-optimizer` agent with the full context:

"You are optimizing monitor [NAME] (ID: [ID]).

MONITOR PURPOSE: [purpose statement from Step 3]

ORIGINAL CONFIG: The full monitor config is at /tmp/opal-optimizer/original_config.json. The OPAL is at /tmp/opal-optimizer/baseline.opal.

Input dataset: [DATASET_ID]. Time range: -r 1h.

CRITICAL RULE — PURPOSE VALIDATION: After each optimization, you MUST verify the optimized query still fulfills the monitor's original purpose. Specifically:
1. The same types of issues that would trigger the original monitor must also trigger the optimized version
2. GroupBy dimensions that are essential to the alerting logic must be preserved
3. Hardcoded exclusions should be carried forward unless they are clearly unnecessary
4. The threshold/promote condition logic must produce equivalent alerting behavior
5. If an optimization changes what the query surfaces (e.g., adding a throughput filter), explicitly note what is no longer covered and whether that coverage gap matters

If an optimization improves performance but compromises purpose, mark it as DISCARD with the reason 'breaks purpose: [explanation]'.

Begin the optimization loop now."

## Step 8: Measure alert volume reduction

Before creating the V2, compare the estimated alert volume between baseline and optimized:

Run both queries with no topk/limit cap to count total violating combinations:
```bash
# Baseline alert volume (uncapped)
sed '/topk\|limit/d' /tmp/opal-optimizer/baseline.opal > /tmp/opal-optimizer/baseline_uncapped.opal
BASELINE_ALERTS=$(~/go/bin/observe query -f /tmp/opal-optimizer/baseline_uncapped.opal -i "$DATASET_ID" -r 1h --json 2>/dev/null | wc -l | tr -d ' ')

# Optimized alert volume (uncapped)
sed '/topk\|limit/d' /tmp/opal-optimizer/best.opal > /tmp/opal-optimizer/best_uncapped.opal
OPTIMIZED_ALERTS=$(~/go/bin/observe query -f /tmp/opal-optimizer/best_uncapped.opal -i "$OPTIMIZED_DATASET_ID" -r 1h --json 2>/dev/null | wc -l | tr -d ' ')

echo "Baseline alert volume: $BASELINE_ALERTS violating combinations per evaluation"
echo "Optimized alert volume: $OPTIMIZED_ALERTS violating combinations per evaluation"
```

Present a summary to the user:
- Execution time improvement (baseline → best, % reduction)
- Alert volume reduction (baseline combinations → optimized combinations, % reduction)
- Estimated monthly alert reduction (extrapolate: combinations × evaluations per hour × 720 hours)
- What was filtered out and why (e.g., "removed 8,000 single-request endpoints that were generating noise")

## Step 9: Create the V2 monitor

When the user is satisfied with the optimized query and the alert reduction numbers, offer to create the V2 monitor via the API. Build the new monitor config by:

1. Copying the original config
2. Replacing the OPAL pipeline with the optimized version from best.opal
3. Updating the name to the V+1 version
4. Setting `disabled: true` initially (so it can be reviewed before going live)
5. Preserving all other settings: actionRules, groupings, scheduling, severity, description
6. Adding a note to the description with the optimization summary (e.g., "V2: 90% faster, 95% fewer alerts. Optimized from [original name] on [date].")

```bash
curl -s -X POST \
  -H "Authorization: Bearer $CUSTOMER_ID $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d @/tmp/opal-optimizer/v2_monitor.json \
  "https://$CUSTOMER_ID.$SITE/v1/monitors"
```

Report the new monitor ID and remind the user it's created as disabled — they should review it in the Observe UI before enabling.
