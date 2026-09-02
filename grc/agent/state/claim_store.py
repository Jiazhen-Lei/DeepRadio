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
        active = self.state.intent
        if active.intent_id:
            claim.intent_id = active.intent_id
            claim.intent_revision = active.revision
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
        if claim.producer:
            current.producer = claim.producer
        if claim.measurement_id:
            current.measurement_id = claim.measurement_id
        current.stale_reason = claim.stale_reason
        current.intent_id = claim.intent_id
        current.intent_revision = claim.intent_revision
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
            and item.measurement_id == evidence.measurement_id
            and item.evidence_grade == evidence.evidence_grade
            for item in claim.evidence
        )
        if not duplicate:
            claim.evidence.append(evidence)
        claim.project_version = evidence.project_version
        claim.stale_reason = ""
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
                claim.status = "Stale"
                claim.stale_reason = (
                    f"project_version {claim.project_version} < {new_version}"
                )
                invalidated.append(claim.id)
        return invalidated

    def invalidate_by_intent_revision(self, new_revision: int) -> List[str]:
        """Mark prior conclusions stale after a user-directed Workflow revision."""
        invalidated = []
        for claim in self.state.claims:
            if claim.intent_revision < new_revision and claim.status != "Stale":
                claim.status = "Stale"
                claim.stale_reason = (
                    f"intent_revision {claim.intent_revision} < {new_revision}"
                )
                invalidated.append(claim.id)
        return invalidated

    def summary(self, *, active_intent_only: bool = False) -> List[dict]:
        claims = list(self.state.claims)
        intent_id = str(self.state.intent.intent_id or "")
        if active_intent_only and intent_id:
            claims = [claim for claim in claims if claim.intent_id == intent_id]
        return [asdict(claim) for claim in claims]
