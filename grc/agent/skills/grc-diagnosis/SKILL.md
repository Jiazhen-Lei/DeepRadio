---
name: grc-diagnosis
description: Diagnose GNU Radio flowgraphs from reproducible metrics and propose minimal fixes.
---

# GRC diagnosis

Start from VerificationAgent evidence. Use `debug_by_metric`, then propose the
smallest parameter change that explains the symptom. Do not mutate the
flowgraph directly; route an accepted change through FlowgraphAgent and require
VerificationAgent to re-test invalidated claims.
