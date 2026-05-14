# observe-learning-capture — moved

This plugin moved to its own repository on 2026-05-14:

**New home:** https://github.com/obs-gh-chrismilton/observe-learning

Why: the plugin grew its own dedicated test suite, eval set, autoresearch
workspace, and design docs — enough infrastructure to warrant a standalone
repo. Keeping it inside claude-plugins/ was making both repos harder to
navigate.

The new repo contains:
- The same `/observe-capture` and `/observe-review` slash commands
- The same Stop / SessionEnd / SessionStart hooks (same auto-fire behavior)
- The same `pipeline.classifier` + runner, with the 2026-05-14 fix for
  the `ANTHROPIC_API_KEY` subprocess-auth leak and the missing-file
  hook race
- The autoresearch run from 2026-05-14 that took eval pass rate from
  83.3% baseline to a stable ~95.8%

To install the plugin from its new home: see the new repo's README.
