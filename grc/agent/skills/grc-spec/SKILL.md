---
name: grc-spec
description: Extract and maintain traceable radio goals, constraints, decisions, and success claims.
---

# GRC specification

Use `spec_clarify` before committing an ambiguous request. Use `spec_commit` to
record only facts present in the TaskCard. A new Workflow replaces `goals` and
`success_conditions`. Hardware-only specs summarize device, rate, and arming,
not `调制 → 信道 → recipe`. Explicit constraints (simulation-only,
no hardware/RF) are constraints, not missing hardware slots. Mark inferred
choices as assumptions; never silently convert an assumption into a user
decision. Return a ResultEnvelope and list any open questions.
