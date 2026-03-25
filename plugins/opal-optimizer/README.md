# OPAL Optimizer — Claude Code Plugin

An autonomous OPAL query optimization plugin for Claude Code. Give it a query or an existing monitor ID, and it iteratively rewrites the OPAL, measures execution performance via the Observe CLI, validates that the optimized version still fulfills its original purpose, and keeps improvements. Inspired by Karpathy's [autoresearch](https://github.com/karpathy/autoresearch) concept, applied to query optimization instead of ML training.

## What It Does

Two workflows:

**Optimize a raw query** (`/optimize-query`) — paste an OPAL query, the optimizer measures it, rewrites it, and iterates until interrupted.

**Clone and optimize a monitor** (`/clone-monitor <id>`) — fetches a real monitor's full config from the Observe API (OPAL stages, input datasets, thresholds, notification channels, groupings, scheduling), analyzes its purpose, optimizes the OPAL while validating purpose is preserved, and creates a new V+1 monitor via the API.

Both workflows:
1. Capture baseline metrics (execution time, row count, query complexity)
2. Analyze the query using Observe platform knowledge — acceleration, indexed columns, cardinality, join cost
3. Rewrite the OPAL with targeted optimizations
4. Validate the rewrite still surfaces the same issues the original was built to detect
5. Keep improvements, discard regressions
6. Repeat autonomously until interrupted

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
- [Observe CLI](https://github.com/observeinc/observe) — install with:
  ```bash
  go install github.com/observeinc/observe@latest
  ```
- Observe CLI configured with a valid profile at `~/.config/observe.yaml`:
  ```yaml
  profile:
    default:
      customerid: "<YOUR_CUSTOMER_ID>"
      site: "observeinc.com"
      authtoken: "<YOUR_AUTH_TOKEN>"
  ```
- (Optional) Observe MCP servers configured in Claude Code for richer dataset exploration

## Installation

Add the plugin to your Claude Code settings. Open `~/.claude/settings.json` and merge:

```json
{
  "enabledPlugins": {
    "opal-optimizer@obs-gh-chrismilton": true
  },
  "extraKnownMarketplaces": {
    "obs-gh-chrismilton": {
      "source": {
        "source": "github",
        "repo": "obs-gh-chrismilton/opal-optimizer"
      }
    }
  }
}
```

Restart Claude Code or run `/reload-plugins`.

### Verify Installation

Run `/optimize-query` or `/clone-monitor`. If the commands are recognized, you're set.

## Usage

### Optimize a Raw Query

```
/optimize-query
```

The command walks you through:
1. Paste your OPAL query (or describe what you want optimized)
2. Specify the input dataset(s)
3. Set a time range (defaults to `-r 1h` for fast iterations)
4. Optionally provide an optimization focus (e.g., "reduce groupBy cardinality" or "find an accelerated dataset")

It runs the baseline, generates an HTML dashboard, and dispatches the autonomous optimizer agent.

### Clone and Optimize a Monitor

```
/clone-monitor 42812526
```

The command:
1. Fetches the full monitor config via `GET /v1/monitors/{id}` (the MonitorV2 API)
2. Extracts the OPAL pipeline, input datasets, thresholds, notification actions, groupings, scheduling, and severity
3. Writes a purpose statement and confirms it with you
4. Determines the version number (V+1 — if the name already has V3, the new one is V4)
5. Runs the baseline and generates a dashboard
6. Dispatches the optimizer with the purpose statement as a constraint
7. When you're satisfied, creates the V2 monitor via `POST /v1/monitors` — **disabled by default** so you can review before enabling

The new monitor preserves everything from the original: notification channels, severity, groupings, scheduling. Only the OPAL is optimized.

### Check Progress

```
/optimization-status
```

Shows the current best execution time, recent iterations, keep/discard counts, and the current best query.

### Monitor in the Browser

The HTML dashboard (generated during setup) auto-refreshes every 15 seconds and shows:
- Execution time chart over iterations
- Summary stats (best time, total iterations, keeps/discards)
- Full iteration history table
- Current best OPAL query

### Stop the Optimizer

The optimizer stops automatically after 20 iterations or 5 consecutive discards. You can also press `Esc` or `Ctrl+C` to interrupt early. Either way, the current best query is saved at `/tmp/opal-optimizer/best.opal` and the full history is in `/tmp/opal-optimizer/results.tsv`.

## How the Optimization Loop Works

The plugin uses an autonomous test-measure-rewrite cycle. Here's what happens under the hood once the loop starts:

```
┌─────────────────────────────────────────────────────────┐
│  1. ANALYZE                                             │
│     Read the current best query and results history.    │
│     Inspect the input dataset schema, check for         │
│     accelerated variants, identify indexed columns.     │
│     Diagnose WHY the query is slow.                     │
└────────────────────┬────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│  2. REWRITE                                             │
│     Apply a single targeted optimization to the OPAL.   │
│     Save the variant to disk (variant_N.opal).          │
└────────────────────┬────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│  3. EXECUTE                                             │
│     Run the rewritten query against the live Observe    │
│     environment via the CLI with timing measurement.    │
└────────────────────┬────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│  4. MEASURE                                             │
│     Capture metrics:                                    │
│     • Execution time (wall clock seconds)               │
│     • Result row count (semantic equivalence check)     │
│     • Query complexity (OPAL line count)                │
│     • Alert volume (monitor clones: uncapped row count) │
└────────────────────┬────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│  5. VALIDATE                                            │
│     Compare to the current best:                        │
│     • Is it faster?                                     │
│     • Does it return equivalent results?                │
│     • (Monitor clones) Does it still fulfill the        │
│       original monitor's purpose?                       │
└────────────────────┬────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│  6. KEEP or DISCARD                                     │
│     Keep: variant becomes the new best, saved to        │
│           best.opal, logged as "keep" in results.tsv    │
│     Discard: best.opal unchanged, logged as "discard"   │
│              with reason (slower, broke semantics,       │
│              broke purpose)                              │
│     Error: query failed to run, logged with stack trace │
└────────────────────┬────────────────────────────────────┘
                     ▼
               Loop back to 1.
               Repeat until a stopping condition is met.
```

Every iteration is logged to `results.tsv` with full metrics regardless of outcome. The dashboard reads this file and updates in real time so you can watch the optimizer work from a browser.

The agent doesn't try random changes. It uses Observe platform knowledge to make informed decisions about what to try first — switching to an accelerated dataset, adding early filters on indexed columns, reducing groupBy cardinality — and falls back to less impactful techniques as the easy wins are exhausted.

### Stopping Conditions

The loop stops automatically when either condition is met:

- **Iteration cap**: 20 total iterations. Beyond this, meaningful improvements are rare and server-side execution variability dominates.
- **Diminishing returns**: 5 consecutive discards with no improvement. If the last 5 attempts all failed to beat the current best, the low-hanging fruit is gone.

When the loop stops, the agent presents a final summary with the best result, total improvement, and offers to create the V2 monitor (if cloning) or save the optimized query. You can also interrupt manually at any time with `Esc` or `Ctrl+C`.

## Purpose Validation

When cloning a monitor, every optimization is validated against the monitor's original purpose. The optimizer checks:

- **Coverage** — would every issue that triggered the original monitor also trigger the optimized version?
- **GroupBy preservation** — are the dimensions that create unique alert streams still present?
- **Exclusion carryforward** — hardcoded endpoint exclusions were added for a reason and are preserved
- **Threshold equivalence** — the promote/threshold/count condition must produce equivalent alerting behavior
- **Comparison windows** — if the original compares current vs. 7-day-ago data, the V2 must preserve that

Optimizations that improve speed but change what the monitor detects are automatically discarded with a `breaks purpose` note in the results log.

## How It Optimizes

The optimizer combines Observe Integration Engineer expertise with iterative measurement. Before making changes, it analyzes the query and input datasets to understand why a query is slow.

### What It Looks For

- **Unaccelerated datasets** — checks if an accelerated or optimized variant exists
- **Missing early filters** — filters should appear before aggregations to reduce data volume
- **High-cardinality groupBys** — unnecessary dimensions that explode the number of unique groups
- **Expensive joins** — `leftjoin` scans the entire right-side dataset; narrowing it helps
- **Inefficient patterns** — `sort + limit` instead of `topk`, complex regex where simple matches suffice, redundant stages

### Optimization Techniques (by typical impact)

| Impact | Technique |
|--------|-----------|
| Highest | Switch to an accelerated/optimized dataset |
| Highest | Add early filters on indexed/enum columns |
| High | Reduce groupBy cardinality |
| High | Use `topk` instead of `sort + limit` |
| High | Pre-filter before histogram operations |
| Medium | Combine adjacent filters |
| Medium | Remove unused `make_col` statements |
| Medium | Simplify regex patterns |
| Low | Remove dead code and unused variables |
| Low | Inline single-use stage variables |

## Tool Access

The optimizer uses two tools for interacting with Observe. They serve different purposes — neither is a fallback for the other.

**Observe CLI** (`~/go/bin/observe`):
- Runs OPAL queries directly with timing measurement
- Lists and inspects datasets for schema information
- Samples data to understand column structure

**Observe MCP servers** (if configured):
- Explores the knowledge graph for correlation tags, metrics, and dataset relationships
- Discovers indexed/enum columns
- Finds related or alternative datasets

MCP servers are not required. The optimizer works with the CLI alone. If MCP is available for the target environment, it uses it for richer dataset discovery alongside the CLI for execution.

## Observe API

The `/clone-monitor` command uses the [Observe REST API](https://developer.observeinc.com/#tag/monitor) to read and create monitors:

| Endpoint | Use |
|----------|-----|
| `GET /v1/monitors/{id}` | Fetch full monitor config (OPAL, rules, actions, groupings) |
| `POST /v1/monitors` | Create the optimized V+1 monitor |
| `GET /v1/monitors` | List monitors (optional, for discovery) |
| `PATCH /v1/monitors/{id}` | Update an existing monitor |
| `DELETE /v1/monitors/{id}` | Delete a monitor |

Authentication uses the same credentials as the CLI: `Authorization: Bearer <customerid> <authtoken>`.

## Plugin Structure

```
opal-optimizer/
├── plugin.json                          # Plugin manifest
├── README.md                            # This file
├── skills/
│   └── autoresearch/
│       └── SKILL.md                     # Context skill
├── agents/
│   └── autoresearch-loop.md             # Autonomous optimizer agent
└── commands/
    ├── optimize-query.md                # /optimize-query setup command
    ├── clone-monitor.md                 # /clone-monitor full workflow
    └── optimization-status.md           # /optimization-status quick check
```

## Companion Skill: Observe CLI

This plugin works well alongside the [Observe CLI skill](https://github.com/observeinc/observe-claude-code-skill-example), which teaches Claude Code how to use the Observe CLI for general querying, investigation, and object management. Install it at `~/.claude/skills/observe/SKILL.md` for the optimizer to reference OPAL syntax and patterns.

## Example: First Run Results

Optimizing a p95 latency monitor query on the Tekion Prod environment:

| Iteration | Time | Status | Description |
|-----------|------|--------|-------------|
| 0 | 47.2s | keep | Baseline on unoptimized dataset (42530686) |
| 1 | 5.7s | keep | Switched to V4 Optimized dataset (42870350) |
| 5 | 4.5s | keep | Filter request_count >= 50 before histogram_quantile |
| 8 | 3.9s | keep | limit 50 instead of topk (later reverted to topk for sorted output) |

**Result: 47.2s → 4.5s (90.5% improvement)** in 13 iterations, with purpose preserved — the V2 surfaces high-traffic endpoints with genuine latency issues instead of single-request noise.

## License

MIT
