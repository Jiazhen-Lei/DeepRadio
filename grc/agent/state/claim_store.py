"""Claim/evidence update rules."""

from __future__ import annotations

from dataclasses import asdict
from typing import List, Optional

from .shared_state import Claim, Evidence, SharedState


class ClaimStore:
    def __init__(self, state: SharedState):
        self.state = state

    def get(self, claim_id: str) -> Optional[Claim]:
        return next((c for c in self.state.claims if c.id == claim_id), None)

    def upsert(self, claim: Claim) -> Claim:
        current = self.get(claim.id)
        if current is None:
            self.state.claims.append(claim)
            return claim
        unchanged = (
            current.statement == claim.statement
            and current.layer == claim.layer
            and current.project_version == claim.project_version
        )
        current.statement = claim.statement
        current.layer = claim.layer
        current.project_version = claim.project_version
        if claim.evidence:
            current.evidence = claim.evidence
        elif not unchanged:
            current.status = claim.status
            current.evidence = []
        return current

    def add_evidence(
        self, claim_id: str, evidence: Evidence, passed: Optional[bool] = None
    ) -> Claim:
        claim = self.get(claim_id)
        if claim is None:
            raise KeyError(f"未知 claim: {claim_id}")
        duplicate = any(
            item.test == evidence.test
            and item.observation == evidence.observation
            and item.project_version == evidence.project_version
            and item.artifact == evidence.artifact
            for item in claim.evidence
        )
        if not duplicate:
            claim.evidence.append(evidence)
        claim.project_version = evidence.project_version
        if passed is True:
            claim.status = "Passed"
        elif passed is False:
            claim.status = "Failed"
        else:
            claim.status = "Inconclusive"
        return claim

    def invalidate_by_version(self, new_version: int) -> List[str]:
        invalidated = []
        for claim in self.state.claims:
            if claim.project_version < new_version:
                claim.status = "NotTested"
                invalidated.append(claim.id)
        return invalidated

    def summary(self) -> List[dict]:
        return [asdict(claim) for claim in self.state.claims]
