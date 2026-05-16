---
type: runbook
created: 'YYYY-MM-DDTHH:MM:SSZ'
updated: 'YYYY-MM-DDTHH:MM:SSZ'
agent: your-agent
tags: []
related: []
last_executed: null
---

# Runbook title

## Purpose

One sentence: what does this procedure accomplish?

## Preconditions

- Required access (credentials, SSH keys, tokens)
- Required state (no active deploy in flight, off-peak, etc.)
- Required tools installed locally

## Steps

### 1. Step name

```bash
# Exact command, copy-pasteable
echo "do the thing"
```

**Verify:**

```bash
# Command that proves step worked
echo "check the thing"
```

Expected output:

```
some-string-or-pattern
```

### 2. Step name

```bash
# ...
```

**Verify:** ...

## Rollback

If something goes wrong at step N, do this:

```bash
# rollback commands
```

## Notes

Non-obvious gotchas the author learned the hard way. Provider quirks. Why a step is ordered the way it is.

## References

- Link to related decisions
- Link to error patterns this procedure prevents
- Link to source docs
