# Chris Milton's Personal Claude Code Plugins

Personal Claude Code plugin marketplace for Observe Integration Engineering work. Three plugins covering OPAL query optimization, Snowflake brand guidelines, and cross-customer Observe-platform knowledge capture.

## Plugins

### [opal-optimizer](plugins/opal-optimizer/)

Autonomous OPAL query optimization loop for Observe monitors and queries. Iteratively rewrites OPAL, measures execution performance via the Observe CLI, validates that optimized versions still fulfill their original purpose, and keeps improvements. Two slash commands: `/optimize-query` (raw OPAL) and `/clone-monitor` (full monitor config + V+1 promotion).

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) concept, applied to query optimization instead of ML training.

### [snowflake-brand](plugins/snowflake-brand/)

Snowflake brand guidelines for designs, code, presentations, and marketing materials. Used when writing or reviewing customer-facing content with Snowflake co-branding.

### [observe-learning-capture](plugins/observe-learning-capture/)

Auto-captures Observe-platform-general learnings from Claude Code session transcripts. Stages candidates for human review at next session start. Approved candidates merge into `~/.claude/agents/ObserveIE.md` so knowledge from one customer's session benefits every future session in any customer.

Three Claude Code hooks (Stop, SessionEnd, SessionStart) plus a Python pipeline. ~$0.10–$0.15 per session in Haiku tokens.

## Installation

Add this marketplace, then install plugins individually:

```
/plugin marketplace add obs-gh-chrismilton/claude-plugins
/plugin install <plugin-name>@chris-plugins
```

Each plugin's README has plugin-specific setup, prerequisites, and configuration details.

## Author

Chris Milton — Observe Integration Engineer.

## License

Personal Claude Code plugins. Use at your own risk; not officially supported by Observe Inc.
