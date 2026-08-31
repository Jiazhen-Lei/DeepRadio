"""Deterministic Plan Compiler / Policy Kernel.

Task labels are evaluation tags.  This module only enforces generic schema,
effect, and truncation rules.  It must not mention dataset utterances.
"""

from __future__ import annotations

import logging

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from .planning import (
    EffectLevel,
    highest_effect,
    normalize_effect,
    split_at_decision_boundary,
    stage_plan_item,
)
from .schema import Stage
from .completion import KNOWN_COMPLETIONS

logger = logging.getLogger(__name__)


@dataclass
class PlanNode:
    id: str
    objective: str = ""
    requires: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    effect_level: str = "READ"
    success_predicates: list[str] = field(default_factory=list)
    needs_user_decision: bool = False
    tools: list[str] = field(default_factory=list)
    stage_id: str = ""
    unbound_predicates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanNode":
        stage_id = str(data.get("stage_id") or data.get("id") or "")
        return cls(
            id=str(data.get("id") or stage_id),
            objective=str(data.get("objective") or stage_id),
            requires=list(data.get("requires") or []),
            produces=list(data.get("produces") or data.get("completion") or []),
            effect_level=str(data.get("effect_level") or data.get("effect") or "READ"),
            success_predicates=list(
                data.get("success_predicates") or data.get("completion") or []
            ),
            needs_user_decision="checkpoint" in str(
                data.get("interaction") or ""
            ) or bool(data.get("needs_user_decision")),
            tools=list(data.get("tools") or []),
            stage_id=stage_id,
            unbound_predicates=list(data.get("unbound_predicates") or []),
        )


def known_action_ids(catalog: Mapping[str, Any] | None = None) -> set[str]:
    """Action ids the compiler may bind. Catalog fragments plus tool names."""
    ids: set[str] = set()
    candidates = dict((catalog or {}).get("task_candidates") or {})
    for candidate in candidates.values():
        if not isinstance(candidate, Mapping):
            continue
        for key in ("stages", "runtime_stages", "deploy_stages"):
            for item in list(candidate.get(key) or []):
                if isinstance(item, Mapping) and item.get("id"):
                    ids.add(str(item["id"]))
    try:
        from ..tools import registry

        registry.load_all()
        ids.update(spec.name for spec in registry.all_specs())
    except Exception:  # noqa: BLE001
        pass
    return ids


def node_from_stage(stage: Stage) -> PlanNode:
    interaction = str(getattr(stage, "interaction", "") or "")
    produces = list(getattr(stage, "produces", None) or stage.completion or [])
    return PlanNode(
        id=stage.id,
        objective=str(getattr(stage, "objective", "") or stage.id),
        requires=list(getattr(stage, "requires", None) or stage.depends_on or []),
        produces=produces,
        effect_level=str(stage.effect_level or "READ"),
        success_predicates=list(
            getattr(stage, "success_predicates", None) or stage.completion or []
        ),
        needs_user_decision="checkpoint" in interaction,
        tools=list(getattr(stage, "recommended_agents", None) or []),
        stage_id=stage.id,
        unbound_predicates=list(getattr(stage, "unbound_predicates", None) or []),
    )


def attach_plan_metadata(stages: Iterable[Stage]) -> list[PlanNode]:
    """Fill generic PlanNode fields on executable Stages."""
    nodes = []
    for stage in stages:
        node = node_from_stage(stage)
        stage.objective = node.objective
        stage.requires = list(node.requires)
        stage.produces = list(node.produces)
        stage.success_predicates = list(node.success_predicates)
        stage.unbound_predicates = list(node.unbound_predicates)
        nodes.append(node)
    return nodes


def validate_proposal(
    proposal: Iterable[Any],
    *,
    catalog: Mapping[str, Any] | None = None,
) -> tuple[list[PlanNode], list[str]]:
    """Drop unknown actions; never invent Registry capabilities."""
    known = known_action_ids(catalog)
    accepted: list[PlanNode] = []
    rejected: list[str] = []
    for item in proposal or []:
        node = item if isinstance(item, PlanNode) else PlanNode.from_dict(
            item if isinstance(item, Mapping) else {"id": str(item)}
        )
        action_id = node.stage_id or node.id
        tools = list(node.tools or [])
        unknown_tools = [name for name in tools if known and name not in known]
        if known and action_id not in known and not tools:
            rejected.append(action_id)
            continue
        if unknown_tools:
            rejected.extend(unknown_tools)
            continue
        accepted.append(node)
    return accepted, rejected


def ensure_rf_bounds(intent: Any) -> None:
    """Deploy/RF_RUN plans always carry a duration cap. Prepare does not."""
    slots = getattr(intent, "slots", None)
    if not isinstance(slots, dict):
        return
    capabilities = set(getattr(intent, "capabilities", None) or [])
    if slots.get("operation") != "deploy" and "deploy" not in capabilities:
        return
    slots.setdefault("duration_seconds", 30.0)
    slots.setdefault("max_duration_seconds", slots.get("duration_seconds") or 30.0)


class PlanCoverageError(ValueError):
    """Raised when a required planner action cannot be compiled at all."""


def _stage_tool_index() -> dict[str, set[str]]:
    """Map stage ids to their host tool allowlists."""
    try:
        from ..service.orchestrator import _STAGE_TOOLS

        return {
            str(stage_id): set(tools or ())
            for stage_id, tools in dict(_STAGE_TOOLS).items()
        }
    except Exception:  # noqa: BLE001
        return {}


def ensure_plan_coverage(
    stages: list[Stage],
    accepted: list[PlanNode],
    *,
    catalog: Mapping[str, Any] | None = None,
) -> tuple[list[Stage], list[str], list[str]]:
    """Bind planner actions the composed stages do not already cover.

    Returns ``(stages, bound_action_ids, unbound_action_ids)``.  Planner
    actions matching a catalog stage (directly or through the stage's tool
    allowlist) are inserted before the first checkpoint so they execute
    before the next decision boundary.  Actions that cannot be bound to any
    stage are returned as ``unbound`` — the caller decides whether to block.
    """
    if not accepted:
        return stages, [], []
    tool_index = _stage_tool_index()
    covered_stage_ids = {stage.id for stage in stages}
    covered_tools: set[str] = set()
    covered_predicates: set[str] = set()
    for stage in stages:
        covered_tools |= tool_index.get(stage.id, set())
        covered_tools |= set(stage.recommended_agents or [])
        covered_predicates |= set(stage.completion or [])
        covered_predicates |= set(stage.produces or [])
        covered_predicates |= set(stage.success_predicates or [])
    catalog_index = catalog_stage_index(catalog)
    base_effect_ceiling = max(
        (normalize_effect(stage.effect_level) for stage in stages),
        default=EffectLevel.READ,
    )
    bound: list[str] = []
    unbound: list[str] = []
    additions: list[Stage] = []
    for node in accepted:
        action_id = node.stage_id or node.id
        if not action_id:
            continue
        if action_id in covered_stage_ids or action_id in covered_tools:
            continue
        requested_predicates = {
            name for name in (
                list(node.produces or []) + list(node.success_predicates or [])
            )
            if name in KNOWN_COMPLETIONS
        }
        if requested_predicates and requested_predicates <= covered_predicates:
            # Bind by outcome, not merely by stage id.  A protocol-specific
            # producer plus its verifier already covers a generic "build and
            # validate" request; inserting another producer would overwrite
            # the authoritative artifact with an unrelated implementation.
            bound.append(action_id)
            continue
        # A planner action may name a tool; find the catalog stage that owns it.
        owner_ids = [
            stage_id for stage_id, tools in tool_index.items()
            if action_id in tools
        ]
        fragment_id = action_id if action_id in catalog_index else (
            owner_ids[0] if owner_ids else ""
        )
        fragment = catalog_index.get(fragment_id)
        if fragment is None:
            unbound.append(action_id)
            continue
        stage = Stage.from_dict(dict(fragment))
        _apply_tool_effect_floor(stage)
        if normalize_effect(stage.effect_level) > base_effect_ceiling:
            # An open planner may fill a coverage gap, but it cannot raise the
            # deterministic capability plan's side-effect authority.
            unbound.append(action_id)
            continue
        if stage.id in covered_stage_ids:
            covered_stage_ids.add(stage.id)
            covered_tools |= tool_index.get(stage.id, set())
            continue
        stage.objective = node.objective or stage.objective
        if node.success_predicates:
            bound_predicates = [
                name for name in node.success_predicates
                if name in KNOWN_COMPLETIONS
            ]
            if bound_predicates:
                stage.success_predicates = list(dict.fromkeys(
                    list(stage.success_predicates or stage.completion)
                    + bound_predicates
                ))
        additions.append(stage)
        covered_stage_ids.add(stage.id)
        covered_tools |= tool_index.get(stage.id, set())
        covered_tools |= set(stage.recommended_agents or [])
        covered_predicates |= set(stage.completion or [])
        covered_predicates |= set(stage.produces or [])
        covered_predicates |= set(stage.success_predicates or [])
        bound.append(action_id)
    if additions:
        insert_at = _dependency_aware_insert_index(stages, additions)
        successor_id = (
            stages[insert_at].id if insert_at < len(stages) else "completed"
        )
        stages = stages[:insert_at] + additions + stages[insert_at:]
        _rewire_inserted_chain(stages, additions, insert_at, successor_id)
    return stages, bound, unbound


def _dependency_aware_insert_index(
    stages: list[Stage],
    additions: list[Stage],
) -> int:
    """Place planner additions after their evidence and before higher effects."""
    checkpoint = next(
        (
            index for index, stage in enumerate(stages)
            if "checkpoint" in str(getattr(stage, "interaction", "") or "")
        ),
        len(stages),
    )
    produced_by_additions = {
        predicate
        for stage in additions
        for predicate in list(stage.completion or []) + list(stage.produces or [])
    }
    external_requirements = {
        predicate
        for stage in additions
        for predicate in list(stage.depends_on or []) + list(stage.requires or [])
        if predicate not in produced_by_additions
    }
    lower_bound = 0
    for index, stage in enumerate(stages[:checkpoint]):
        produced = set(stage.completion or []) | set(stage.produces or [])
        if produced & external_requirements:
            lower_bound = index + 1
    addition_effect = max(
        (normalize_effect(stage.effect_level) for stage in additions),
        default=EffectLevel.READ,
    )
    for index in range(lower_bound, checkpoint):
        if normalize_effect(stages[index].effect_level) > addition_effect:
            return index
    return checkpoint


#: Transition targets that are workflow control states, not stage ids.
#: Keep in sync with ``engine._NON_STAGE_TARGETS`` (plus wait park states).
_RESERVED_TRANSITION_TARGETS = frozenset({
    "completed", "errored", "cancelled", "stop", "waiting_user",
    "waiting", "not_required", "invalidated",
})


def _rewire_inserted_chain(
    stages: list[Stage],
    additions: list[Stage],
    insert_at: int,
    successor_id: str,
) -> None:
    """Rewire transitions around planner-materialized stages (in place).

    Catalog fragments carry the transitions of the template they were copied
    from (e.g. a runtime-chain ``passed → rf_plan_confirmation`` target that
    does not exist in this plan), and the predecessor still points at the
    original successor, which would bypass the whole inserted chain.  Both
    defects produced dead stages and dangling targets (V6: a BLE deploy plan
    where ``build_ble_advertiser`` was unreachable and the final transmitter
    would have been a 1 kHz diagnostic tone), so after insertion:

    * every earlier transition that pointed at the successor is redirected
      to the first inserted stage — nothing may bypass the planner's
      actions;
    * each inserted stage's transitions that point at a stage missing from
      this plan follow the chain: next addition, then the successor.
    """
    if not additions:
        return
    valid_ids = {stage.id for stage in stages}
    for stage in stages[:insert_at]:
        for outcome, target in list(stage.transitions.items()):
            if target == successor_id:
                stage.transitions[outcome] = additions[0].id
    success_outcomes = {"passed", "approved", "not_required", "completed"}
    for index, stage in enumerate(additions):
        next_id = (
            additions[index + 1].id
            if index + 1 < len(additions) else successor_id
        )
        for outcome, target in list(stage.transitions.items()):
            # A copied fragment's positive edge belongs to its source
            # template.  Rebuild it from compiled order even when the old
            # target happens to exist here; retaining such a backward edge
            # previously created a hardware/protocol execution cycle.
            if outcome in success_outcomes:
                stage.transitions[outcome] = next_id
                continue
            if target in _RESERVED_TRANSITION_TARGETS or target in valid_ids:
                continue
            stage.transitions[outcome] = next_id


def _repair_dangling_transitions(stages: list[Stage]) -> list[str]:
    """Defensive sweep: no transition may point outside this plan.

    Returns a description of each repair so callers can log/audit; success
    transitions are redirected to the next stage in plan order (or
    ``completed`` at the end), failure transitions to ``waiting_user``.
    """
    valid_ids = {stage.id for stage in stages}
    repairs: list[str] = []
    for index, stage in enumerate(stages):
        next_id = stages[index + 1].id if index + 1 < len(stages) else "completed"
        for outcome, target in list(stage.transitions.items()):
            if target in _RESERVED_TRANSITION_TARGETS or target in valid_ids:
                continue
            if outcome in ("passed", "approved", "not_required"):
                stage.transitions[outcome] = next_id
            else:
                stage.transitions[outcome] = "waiting_user"
            repairs.append(
                "{}.{}: {} -> {}".format(
                    stage.id, outcome, target, stage.transitions[outcome]
                )
            )
    return repairs


def compile_stages(
    intent: Any,
    stages: list[Stage],
    *,
    catalog: Mapping[str, Any] | None = None,
    proposal: Iterable[Any] | None = None,
) -> tuple[list[Stage], list[PlanNode], list[str], list[str]]:
    """Attach PlanNodes, apply RF bounds, and enforce plan coverage.

    Returns ``(stages, nodes, rejected, unbound)``.  ``unbound`` lists
    validated planner actions that no composed or catalog stage can serve.
    """
    ensure_rf_bounds(intent)
    for stage in stages:
        _apply_tool_effect_floor(stage)
    rejected: list[str] = []
    unbound: list[str] = []
    if proposal:
        accepted, rejected = validate_proposal(proposal, catalog=catalog)
        by_id = {stage.id: stage for stage in stages}
        for node in accepted:
            stage = by_id.get(node.stage_id or node.id)
            if stage is None:
                continue
            # Existing executable facts come from the catalog/Registry.  The
            # LLM may improve presentation metadata, but must not rewrite
            # dependencies, producers, or completion semantics.
            stage.objective = node.objective or stage.objective
            stage.unbound_predicates = list(dict.fromkeys(
                list(stage.unbound_predicates or [])
                + [
                    name for name in node.success_predicates
                    if name not in KNOWN_COMPLETIONS
                ]
            ))
        stages, _bound, unbound = ensure_plan_coverage(
            stages, accepted, catalog=catalog
        )
    for stage in stages:
        _apply_tool_effect_floor(stage)
    # Defensive invariant: every transition target is either a stage in this
    # plan or a reserved control state.  Broken wirings previously surfaced
    # only at runtime as a jump to a nonexistent stage.
    repairs = _repair_dangling_transitions(stages)
    if repairs:
        logger.warning("repaired dangling plan transitions: %s", repairs)
    _validate_success_flow_acyclic(stages)
    _validate_intent_effect_contract(intent, stages)
    _validate_evidence_contract(stages)
    nodes = attach_plan_metadata(stages)
    return stages, nodes, rejected, unbound


def _validate_success_flow_acyclic(stages: list[Stage]) -> None:
    """Reject cycles on ordinary successful execution edges."""
    success_outcomes = {"passed", "approved", "not_required", "completed"}
    edges = {
        stage.id: {
            target
            for outcome, target in stage.transitions.items()
            if outcome in success_outcomes
            and target not in _RESERVED_TRANSITION_TARGETS
        }
        for stage in stages
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(stage_id: str) -> None:
        if stage_id in visiting:
            raise PlanCoverageError(
                f"The compiled plan contains a successful execution cycle at {stage_id}."
            )
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for target in edges.get(stage_id, set()):
            visit(target)
        visiting.remove(stage_id)
        visited.add(stage_id)

    for stage in stages:
        visit(stage.id)


def _validate_intent_effect_contract(intent: Any, stages: list[Stage]) -> None:
    """Ensure the compiled plan can realize the LLM's requested effect."""
    if not stages:
        return
    slots = dict(getattr(intent, "slots", None) or {})
    context = dict(getattr(intent, "context", None) or {})
    execution_mode = str(
        context.get("execution_mode") or slots.get("operation") or ""
    ).lower()
    if execution_mode != "deploy":
        return
    if not any(
        normalize_effect(stage.effect_level) >= EffectLevel.RF_RUN
        and not stage.safety_finalizer
        for stage in stages
    ):
        raise PlanCoverageError(
            "The request requires RF execution, but the compiled plan has no "
            "non-finalizer RF_RUN stage."
        )


def _validate_evidence_contract(stages: list[Stage]) -> None:
    """Reject plans that request effects without prerequisite evidence.

    The invariant is expressed only in effects and evidence predicates; it is
    independent of protocol, hardware model, language, or task wording.
    """
    first_mutation = next(
        (
            index for index, stage in enumerate(stages)
            if normalize_effect(stage.effect_level) >= EffectLevel.DEVICE_CONFIG
        ),
        None,
    )
    if first_mutation is None:
        return
    prior_evidence = {
        predicate
        for stage in stages[:first_mutation]
        for predicate in list(stage.completion or []) + list(stage.produces or [])
    }
    required = {
        "hardware_precheck_completed",
        "device_discovered",
        "device_probed",
    }
    missing = sorted(required - prior_evidence)
    if not prior_evidence.intersection(
        {"rf_plan_approved", "hardware_decision_recorded"}
    ):
        missing.append("explicit_device_grant")
    if missing:
        raise PlanCoverageError(
            "Device mutation requires host readiness, physical discovery, "
            "exact probing, and an explicit grant; missing evidence: {}"
            .format(", ".join(missing))
        )
    rf_indices = [
        index for index, stage in enumerate(stages)
        if normalize_effect(stage.effect_level) >= EffectLevel.RF_RUN
        and not stage.safety_finalizer
    ]
    if not rf_indices:
        return
    if not any(
        stage.safety_finalizer
        and normalize_effect(stage.effect_level) >= EffectLevel.RF_RUN
        for stage in stages[rf_indices[-1] + 1:]
    ):
        raise PlanCoverageError(
            "An RF_RUN stage requires a later RF_RUN safety finalizer."
        )


def plan_needs_proposal(intent: Any, stages: Iterable[Stage]) -> bool:
    """Return whether typed, compiler-bindable intent lacks stage coverage.

    Free-form artifact/evidence prose is valuable for presentation and final
    evaluation, but it is not an executable action id.  Treating arbitrary
    LLM wording as an exact catalog key caused a planner call on every
    otherwise-complete workflow and a 120-second V4 timeout.
    """
    sequence = list(stages)
    covered = {stage.id for stage in sequence}
    for stage in sequence:
        covered.update(stage.completion or [])
        covered.update(stage.produces or [])
        covered.update(stage.success_predicates or [])
    abstract = set(getattr(intent, "capabilities", None) or [])
    requested = {
        str(item)
        for item in (
            list(getattr(intent, "requested_operations", None) or [])
            + list(getattr(intent, "desired_artifacts", None) or [])
            + list(getattr(intent, "evidence_requirements", None) or [])
        )
        if str(item)
    }
    bindable = known_action_ids() | set(KNOWN_COMPLETIONS)
    concrete = {item for item in requested if item in bindable} - abstract
    return bool(concrete - covered)


def _apply_tool_effect_floor(stage: Stage) -> None:
    """A Stage can never claim less effect than its executable tool allowlist."""
    try:
        from ..service.orchestrator import stage_tool_names
        from ..tools import registry

        registry.load_all()
        effects = [normalize_effect(stage.effect_level)]
        for name in stage_tool_names(stage.id):
            spec = registry.get(name)
            if spec is not None:
                effects.append(normalize_effect(spec.effect_level))
        stage.effect_level = max(effects).name
    except Exception:  # noqa: BLE001
        stage.effect_level = normalize_effect(stage.effect_level).name


def proposal_fingerprint(nodes: Iterable[PlanNode | Mapping[str, Any]]) -> tuple[str, ...]:
    names = []
    for item in nodes:
        if isinstance(item, PlanNode):
            names.append(item.stage_id or item.id)
        elif isinstance(item, Mapping):
            names.append(str(item.get("stage_id") or item.get("id") or ""))
        else:
            names.append(str(item))
    return tuple(name for name in names if name)


_PROTECTED_TAIL_IDS = frozenset({
    "configure_device",
    "transmit_bounded",
    "run_bounded",
    "over_air_verification",
    "runtime_observation",
    "stop_and_finalize",
    "stop_runtime",
})
_PROTECTED_EFFECTS = frozenset({"DEVICE_CONFIG", "RF_RUN"})


def protected_tail_ids(deferred: Iterable[Any]) -> set[str]:
    """Stage ids a replan must not drop: granted RF work and safety stop."""
    ids: set[str] = set()
    for item in deferred or []:
        if isinstance(item, Mapping):
            stage_id = str(item.get("id") or item.get("stage_id") or "")
            effect = str(item.get("effect_level") or item.get("effect") or "")
            finalizer = bool(item.get("safety_finalizer"))
        else:
            stage_id = str(getattr(item, "id", "") or getattr(item, "stage_id", "") or "")
            effect = str(getattr(item, "effect_level", "") or "")
            finalizer = bool(getattr(item, "safety_finalizer", False))
        if not stage_id:
            continue
        if (
            stage_id in _PROTECTED_TAIL_IDS
            or effect in _PROTECTED_EFFECTS
            or finalizer
        ):
            ids.add(stage_id)
    return ids


def tail_needs_replan_proposal(deferred: Iterable[Any]) -> bool:
    """True when an LLM tail rewrite could still change upcoming work.

    An empty tail, or a tail that is only the safety stop, is already bound
    by the grant.  Calling the model here only adds a blocking round-trip.
    """
    items = list(deferred or [])
    if not items:
        return False
    stop_ids = {"stop_and_finalize", "stop_runtime"}
    for item in items:
        if isinstance(item, Mapping):
            stage_id = str(item.get("id") or "")
            finalizer = bool(item.get("safety_finalizer"))
        else:
            stage_id = str(getattr(item, "id", "") or "")
            finalizer = bool(getattr(item, "safety_finalizer", False))
        if finalizer or stage_id in stop_ids:
            continue
        return True
    return False


def replan_tail(
    deferred: list[dict[str, Any]],
    *,
    proposal: Iterable[Any] | None = None,
    catalog: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rebuild an unexecuted tail from a compiler-checked proposal.

    Invalid or empty proposals keep the existing deferred plan.  A proposal
    that matches the deferred fingerprint is treated as no-op.  A proposal
    that drops already-authorized device/RF or safety-finalizer stages is
    also treated as invalid.
    """
    if not proposal:
        return list(deferred or [])
    accepted, rejected = validate_proposal(proposal, catalog=catalog)
    if rejected or not accepted:
        return list(deferred or [])
    proposed_ids = proposal_fingerprint(accepted)
    current_ids = tuple(
        str(item.get("id") or "") for item in deferred or [] if isinstance(item, Mapping)
    )
    if proposed_ids == current_ids:
        return list(deferred or [])
    protected = protected_tail_ids(deferred)
    if protected and not protected.issubset(set(proposed_ids)):
        return list(deferred or [])
    rebuilt = []
    deferred_by_id = {
        str(item.get("id") or ""): item
        for item in deferred or []
        if isinstance(item, Mapping)
    }
    catalog_by_id = catalog_stage_index(catalog)
    for node in accepted:
        action_id = node.stage_id or node.id
        existing = deferred_by_id.get(action_id)
        if existing is not None:
            rebuilt.append(dict(existing))
            continue
        fragment = catalog_by_id.get(action_id)
        if fragment is not None:
            rebuilt.append(dict(fragment))
    if protected and not protected.issubset(protected_tail_ids(rebuilt)):
        return list(deferred or [])
    return rebuilt or list(deferred or [])


def catalog_stage_index(
    catalog: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    candidates = dict((catalog or {}).get("task_candidates") or {})
    for candidate in candidates.values():
        if not isinstance(candidate, Mapping):
            continue
        for key in ("stages", "runtime_stages", "deploy_stages"):
            for item in list(candidate.get(key) or []):
                if isinstance(item, Mapping) and item.get("id"):
                    index.setdefault(str(item["id"]), dict(item))
    return index


def compact_workflow_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep control-plane facts; drop bulky tool stdout from persisted Workflow."""
    data = dict(payload or {})
    for stage in list(data.get("stages") or []):
        if not isinstance(stage, dict):
            continue
        result = dict(stage.get("result") or {})
        if "invocations" in result:
            result["invocations"] = compact_invocations(result.get("invocations"))
        stage["result"] = result
        history = []
        for item in list(stage.get("result_history") or [])[-5:]:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            nested = dict(entry.get("result") or {})
            if "invocations" in nested:
                nested["invocations"] = compact_invocations(nested.get("invocations"))
                entry["result"] = nested
            history.append(entry)
        stage["result_history"] = history
    return data


def compact_invocations(items: Any) -> list[dict[str, Any]]:
    """Keep control-plane facts; raw tool stdout stays in Evidence artifacts."""
    compact = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        result = dict(item.get("result") or {})
        tools = []
        for tool in list(item.get("tools") or []):
            if not isinstance(tool, dict):
                continue
            payload = dict(tool.get("result") or {})
            tools.append({
                "name": str(tool.get("name") or ""),
                "ok": bool(tool.get("ok", payload.get("ok", True))),
                "report_path": str(
                    tool.get("report_path")
                    or payload.get("report_path")
                    or payload.get("raw_output_artifact")
                    or ""
                ),
                "error": str(
                    tool.get("error") or payload.get("error") or ""
                )[:500],
            })
        compact.append({
            "task_id": str(item.get("task_id") or ""),
            "target_agent": str(item.get("target_agent") or ""),
            "protocol_valid": bool(item.get("protocol_valid")),
            "result": {
                "ok": bool(result.get("ok")),
                "outcome": str(result.get("outcome") or ""),
                "completion": dict(result.get("completion") or {}),
                "artifacts": dict(result.get("artifacts") or {}),
            },
            "tools": tools,
        })
    return compact


def compiled_plan_summary(
    stages: Iterable[Stage], deferred: Iterable[Mapping[str, Any]] | None = None
) -> list[dict[str, Any]]:
    summary = [node_from_stage(stage).to_dict() for stage in stages]
    for item in deferred or []:
        if isinstance(item, Mapping):
            summary.append(PlanNode.from_dict(item).to_dict())
    return summary
