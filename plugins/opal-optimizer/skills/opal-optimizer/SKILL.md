---
name: opal-optimizer
description: "Optimize Observe monitors and OPAL queries. Trigger when the user says 'optimize monitor', 'clone monitor', 'improve monitor', 'optimize query', 'make this query faster', or mentions optimizing anything in an Observe environment. Also trigger on 'optimize monitor <ID>', 'clone monitor <ID>', or any request involving monitor IDs and optimization."
user-invocable: true
allowed-tools:
  - Bash
  - Edit
  - Read
  - Write
  - Grep
  - Glob
  - LS
model: opus
---

# OPAL Optimizer

You optimize Observe monitors and OPAL queries autonomously. When the user asks to optimize a monitor, you handle everything: fetch the config, analyze its purpose, run the baseline, iteratively optimize the OPAL, validate purpose is preserved, and offer to create a V2.

## Quick Reference

- **Observe CLI**: `~/go/bin/observe` (configured at `~/.config/observe.yaml`)
- **Observe CLI skill**: `~/.claude/skills/observe/SKILL.md` (OPAL syntax reference)
- **Observe API**: `https://<CUSTOMER_ID>.observeinc.com/v1/monitors/{id}`
- **Auth**: Read from `~/.config/observe.yaml` — `customerid` and `authtoken` under the active profile
- **Working directory**: `/tmp/opal-optimizer/`

## Dependency Check (run first, every time)

Before doing anything else, verify the dependencies are installed:

```bash
# Check Observe CLI
if [ -f ~/go/bin/observe ]; then
  echo "CLI: OK ($(~/go/bin/observe --help 2>&1 | head -2 | tail -1))"
else
  echo "CLI: NOT INSTALLED"
fi

# Check Observe CLI config
if [ -f ~/.config/observe.yaml ]; then
  echo "Config: OK"
else
  echo "Config: NOT FOUND"
fi

# Check Observe CLI skill
if [ -f ~/.claude/skills/observe/SKILL.md ]; then
  echo "Skill: OK"
else
  echo "Skill: NOT INSTALLED"
fi
```

If the **CLI is missing**, offer to install it:
"The Observe CLI isn't installed. I can install it for you — it requires Go. Want me to run `go install github.com/observeinc/observe@latest`?"

If **Go is also missing**, offer: "Go isn't installed either. I can install it via Homebrew: `brew install go`, then install the Observe CLI. Want me to proceed?"

If the **config is missing**, tell the user:
"The Observe CLI isn't configured. You'll need your customer ID and an auth token. Run: `~/go/bin/observe --customerid <ID> --site observeinc.com login <EMAIL> --sso`"

If the **CLI skill is missing**, offer to install it:
"The Observe CLI skill isn't installed. I can grab it from GitHub: `mkdir -p ~/.claude/skills/observe && curl -o ~/.claude/skills/observe/SKILL.md https://raw.githubusercontent.com/observeinc/observe-claude-code-skill-example/main/SKILL.md`"

Do not proceed until all three dependencies are satisfied.

## When the User Says "Optimize Monitor <ID>"

Follow these steps. Do not ask the user for information you can look up yourself — except for the two questions below.

### 1. Ask two follow-up questions (and only two)

After the user says "optimize monitor X", ask these two questions before doing anything else. Use a numbered list so the user can answer both at once if they want.

**Question 1 — Optimization goal:**
"What are we optimizing for?
1. Execution speed (faster queries)
2. Credit cost reduction (less data scanned)
3. Alert noise reduction (fewer false/noisy alerts)
4. All of the above (default)"

**Question 2 — Auto-create:**
"When optimization is done, should I:
1. Ask before creating the V2 monitor (default)
2. Go ahead and create it automatically (disabled, for your review)"

If the user doesn't answer or says "just go" or "defaults are fine", use option 4 for goal and option 1 for auto-create.

Do NOT ask any other questions. Everything else you can determine from the monitor config.

### 2. Determine the environment

If the user specifies an environment (e.g., "Tekion Prod", "NonProd"), use the corresponding profile from `~/.config/observe.yaml`. If they don't specify, use the default profile. Read the config to get the customer ID and auth token:

```bash
python3 -c "
import yaml
with open('$HOME/.config/observe.yaml') as f:
    c = yaml.safe_load(f)
p = c['profile']['default']
print(f'CUSTOMER_ID={p[\"customerid\"]}')
print(f'AUTH_TOKEN={p[\"authtoken\"]}')
print(f'SITE={p.get(\"site\", \"observeinc.com\")}')
"
```

### 3. Fetch the monitor config

```bash
curl -s -H "Authorization: Bearer $CUSTOMER_ID $AUTH_TOKEN" \
  "https://$CUSTOMER_ID.$SITE/v1/monitors/$MONITOR_ID" | python3 -m json.tool
```

If 404, tell the user the monitor was deleted and ask if they have the OPAL or want a different monitor.

### 4. Analyze the monitor's purpose

From the config, determine:
- **What it detects** (error rates, latency, throughput anomalies)
- **Severity level** (Critical, Error, Warning)
- **GroupBy dimensions** (what creates unique alert streams)
- **Exclusions** (hardcoded endpoint filters and why they might exist)
- **Notification channels** (actionRules — webhooks, Slack, email)
- **Scheduling** (freshnessGoal, evaluation interval)
- **Input dataset** (datasetId from the stages)
- **The full OPAL pipeline** (from stages[].pipeline)

Write a one-paragraph purpose statement and present it to the user. Wait for confirmation before proceeding.

### 5. Determine version number

Check the monitor name:
- No version → V2
- Has V2 → V3
- Has V3 → V4
- Also clean up typos in the name (e.g., "finetunning" → proper name)

### 6. Set up the optimization workspace

```bash
mkdir -p /tmp/opal-optimizer
```

Save the original config, extract the OPAL, identify the input dataset ID.

### 7. Run the baseline

Run the original OPAL via the CLI and capture timing:

```bash
TIMEFORMAT='%R'; { time ~/go/bin/observe query -f /tmp/opal-optimizer/baseline.opal -i '$DATASET_ID' -r 1h --json > /tmp/opal-optimizer/baseline_results.json 2>&1 ; } 2>/tmp/opal-optimizer/baseline_timing.txt
```

Initialize `results.tsv` with the baseline, save `best.opal`, generate the HTML dashboard, and open it.

### 8. Run the optimization loop

Iteratively optimize the OPAL. For each iteration:

1. **Analyze** the current best query and diagnose bottlenecks
2. **Rewrite** with a targeted optimization (save as `variant_N.opal`)
3. **Execute** the variant with timing via the CLI
4. **Measure** execution time, row count, query complexity, and alert volume (uncapped row count)
5. **Validate purpose** — does this still detect the same issues?
6. **Keep or discard** — copy to `best.opal` if improved, log the result either way

**Stop after 20 iterations or 5 consecutive discards.**

Refer to the optimizer agent at `${CLAUDE_PLUGIN_ROOT}/agents/autoresearch-loop.md` for the full optimization technique list, purpose validation rules, and measurement commands.

### 9. Present results

When the loop finishes, present a summary:
- Execution time: baseline → best (% improvement)
- Alert volume: baseline → optimized (% reduction, monthly extrapolation)
- Total iterations, keeps, discards, errors
- The optimized OPAL query
- What was changed and what was preserved

### 10. Offer to create V2

**Respect the user's answer from Question 2:**

- If they chose **"Ask first" (default)**: Ask "The optimized query is ready. Want me to create [MONITOR NAME V2] as a disabled monitor in Observe? It will preserve the original's severity, groupings, scheduling, and notification channels. You can review it in the UI before enabling." Only proceed if they confirm.
- If they chose **"Go ahead"**: Create it automatically, but still set `disabled: true` so they can review before enabling. Tell them it was created and give them the monitor ID.

## When the User Says "Optimize This Query" (no monitor ID)

Simpler flow — no monitor config to fetch, no purpose validation, no V2 creation:

1. Ask for the OPAL, input dataset, and time range
2. Run baseline, set up workspace and dashboard
3. Run the optimization loop (same techniques, same stopping conditions)
4. Present the optimized query

## Optimization Techniques (ordered by impact)

1. Switch to an accelerated/optimized dataset variant
2. Add early filters on indexed/enum columns
3. Reduce groupBy cardinality
4. Add throughput/count filters to eliminate noise
5. Pre-filter before histogram operations
6. Use `topk` instead of `sort + limit`
7. Combine adjacent filters
8. Remove unused `make_col` statements
9. Simplify regex patterns
10. Remove dead code and inline single-use variables

## Key Rules

- **Do not ask for information you can look up.** The CLI and API have everything you need.
- **Do not create monitors without asking.** Always confirm with the user first.
- **Preserve purpose.** Every optimization must still detect the same issues the original was built for.
- **Log everything.** Every iteration goes in results.tsv.
- **Stop at 20 iterations or 5 consecutive discards.** Present results when done.
