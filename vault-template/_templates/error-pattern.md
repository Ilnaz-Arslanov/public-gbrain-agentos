---
type: error-pattern
created: 'YYYY-MM-DDTHH:MM:SSZ'
updated: 'YYYY-MM-DDTHH:MM:SSZ'
agent: your-agent
tags: []
related: []
weight: 3.0
severity: low | medium | high | critical
---

# Error pattern title

## Symptom

What did you observe? Be exact — error message text, log lines, user-facing behavior. Future-agent grep matches on this section first.

```
paste the actual error/log line here
```

## Root cause

What was actually wrong? Reference file paths and line numbers when applicable. Distinguish between «the proximate trigger» and «the underlying design flaw».

## Fix

What did you change? Commit hash if available:

```
commit abc123 — file.py: brief description
```

If the fix is multi-file or non-obvious, describe the shape, not just the diff.

## Detection

How would you notice this happening again? An alert? A log line? A metric threshold? A test?

## Prevention

Is there a test, lint, hook, or runbook step that would have caught this earlier? If yes, link it. If no, consider adding one.

## References

- PR / commit links
- Related decisions
- Related error patterns
- External writeups (provider status pages, GitHub issues, etc.)
