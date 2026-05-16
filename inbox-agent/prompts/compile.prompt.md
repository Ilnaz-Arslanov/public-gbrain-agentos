You are processing an inbox item forwarded to an AI agent.
Below is the raw material. Produce a plain-markdown summary in this exact shape:

```
**TL;DR:** <1-2 sentences>

**Category:** article | reference | learning | tool

**Tags:** tag1, tag2, tag3

## Takeaways
- ...
- ...
- ...
```

Do NOT emit YAML frontmatter (no leading `---` block) — the wrapper script
owns frontmatter. Do not add a preamble, apology, or any prose outside the
structure above.
