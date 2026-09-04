"""Claim/evidence update rules."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Iterable, List, Optional

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
        same_claim = (
            current.statement == claim.statement
            and current.layer == claim.layer
        )
        current.statement = claim.statement
        current.layer = claim.layer
        if claim.producer:
            current.producer = claim.producer
        if claim.measurement_id:
            current.measurement_id = claim.measurement_id
        current.intent_id = claim.intent_id
        current.intent_revision = claim.intent_revision
        if claim.evidence:
            current.evidence = claim.evidence
        elif not same_claim:
            current.project_version = claim.project_version
            current.status = "Untested"
            current.freshness = "Current"
            current.stale_reason = ""
            current.evidence = []
        return current

    def ensure_for_workflow(self, workflow: Any) -> List[str]:
        """Create the small Claim roster declared by the selected Stages."""
        from ..workflow.catalog import load_stage_catalog

        catalog = load_stage_catalog()
        created = []
        for stage in getattr(workflow, "stages", None) or []:
            for definition in catalog.get(stage.id, {}).get("claims") or []:
                claim = self.upsert(Claim(
                    id=str(definition.get("id") or ""),
                    statement=str(definition.get("statement") or ""),
                    layer=str(definition.get("scope") or ""),
                    project_version=self.state.project.flowgraph_version,
                    producer=stage.id,
                ))
                if claim.id:
                    created.append(claim.id)
        return created

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
            claim.status = "Supported"
        elif passed is False:
            claim.status = "Contradicted"
        else:
            claim.status = "Unresolved"
        claim.freshness = "Current"
        return claim

    def invalidate_scopes(
        self, scopes: Iterable[str], reason: str
    ) -> List[str]:
        """Invalidate only conclusions that depend on the changed scope."""
        affected = {str(item) for item in scopes if item}
        if "task" in affected:
            self.state.project.config.pop("task_observation", None)
        invalidated = []
        for claim in self.state.claims:
            if claim.layer in affected and claim.freshness != "Stale":
                if claim.status == "Untested" and not claim.evidence:
                    claim.project_version = self.state.project.flowgraph_version
                    continue
                claim.freshness = "Stale"
                claim.stale_reason = str(reason or "Relevant project state changed")
                invalidated.append(claim.id)
        return invalidated

    def summary(self, *, active_intent_only: bool = False) -> List[dict]:
        claims = list(self.state.claims)
        intent_id = str(self.state.intent.intent_id or "")
        if active_intent_only and intent_id:
            claims = [claim for claim in claims if claim.intent_id == intent_id]
        return [asdict(claim) for claim in claims]
