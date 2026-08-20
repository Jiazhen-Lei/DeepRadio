---
name: grc-spec
description: Extract and maintain traceable radio goals, constraints, decisions, and success claims.
---

# GRC specification

Use `spec_clarify` before committing an ambiguous request. Use `spec_commit` to
record only facts present in the TaskCard. Mark inferred choices as assumptions;
never silently convert an assumption into a user decision. Return a
ResultEnvelope and list any open questions.
