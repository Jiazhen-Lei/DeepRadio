"""Pure view-model projection for the DeepRadio workflow inspector.

The GTK widgets intentionally do not interpret Workflow/SharedState JSON.
Keeping that interpretation here makes the paper-facing UI testable without a
display server and prevents raw control-plane fields from leaking into the
default user view.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


_LAYER_LABELS = {
    "sim": "Simulation",
    "structure": "Flowgraph check",
    "hardware": "Hardware",
    "protocol": "Protocol",
    "waveform": "Waveform",
    "radio": "Radio design",
    "link": "Link",
    "measurement": "Measurement",
    "ota": "Over-the-air",
    "": "General",
}


def layer_label(layer: Any) -> str:
    """Map internal claim layer ids to user-facing category names."""
    raw = str(layer or "").strip().lower()
    if raw in _LAYER_LABELS:
        return _LAYER_LABELS[raw]
    return raw.replace("_", " ").title() or "General"


_SOURCE_LABELS = {
    "user": "User",
    "user_choice": "User",
    "user_text": "User",
    "user_revision": "User",
    "protocol_default": "Protocol Default",
    "safety_default": "Safety Default",
    "safe_preview_default": "Safety Default",
    "derived": "Derived",
    "current_project": "Canvas",
    "canvas": "Canvas",
    "rules": "System",
    "default": "Default",
}

_STATUS_LABELS = {
    "pass": "Passed",
    "passed": "Passed",
    "fail": "Failed",
    "failed": "Failed",
    "stale": "Stale",
    "unknown": "Unknown",
    "nottested": "Not tested",
    "not_tested": "Not tested",
    "inconclusive": "Inconclusive",
    "running": "Running",
    "pending": "Pending",
}

_CHECK_LABELS = {
    "intent.requested_device_supported": "Requested device",
    "environment.vendor_cli_available": "Driver / vendor CLI",
    "device.discovery": "Device discovery",
    "device.requested_observed_identity_match": "Device identity",
    "device.exact_probe": "Exact probe",
    "parameters.frequency_in_device_range": "Radio parameters",
    "project.flowgraph_present": "Flowgraph",
    "runtime.process_status": "Runtime",
    "rf_path.physical_connection": "Physical RF path",
}


def phase_view(workflow: Dict[str, Any]) -> Dict[str, str]:
    """Return the paper-facing phase independently of catalog task labels."""
    current = str(workflow.get("current_stage") or "")
    wait = str(workflow.get("wait_kind") or "")
    if current in {"intent_alignment", "protocol_spec_alignment"} or wait == "intent":
        return {"id": "align_intent", "label": "ALIGN INTENT"}
    if current == "intent_alignment" or (
        str(workflow.get("task_type") or "") == "INTENT_ALIGNMENT"
    ):
        return {"id": "align_intent", "label": "ALIGN INTENT"}
    effect = str(workflow.get("requested_effect") or "")
    purpose = str(workflow.get("checkpoint_purpose") or "")
    if purpose in {"proposal", "flowgraph_proposal"} or (
        wait == "approval" and effect in {"ARTIFACT_WRITE", "PROJECT_WRITE"}
    ):
        return {"id": "co_construct", "label": "CO-CONSTRUCT"}
    if any(token in current for token in ("build", "proposal", "apply")):
        return {"id": "co_construct", "label": "CO-CONSTRUCT"}
    return {"id": "verify_operate", "label": "VERIFY AND OPERATE"}


def specification_view(
    spec: Dict[str, Any], workflow: Dict[str, Any]
) -> Dict[str, Any]:
    """Normalize Radio Specification rows with explicit provenance."""
    rows = []
    for item in spec.get("radio_specification") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("display_value", item.get("value"))
        unresolved = bool(item.get("unresolved")) or value in (None, "", [])
        needs_confirmation = bool(item.get("needs_confirmation"))
        source = str(item.get("source") or "")
        rows.append({
            "key": str(item.get("key") or ""),
            "label": str(item.get("label") or item.get("key") or ""),
            "value": "?" if unresolved else str(value),
            "source": "Unresolved" if unresolved else _SOURCE_LABELS.get(
                source, source.replace("_", " ").title() if source else "System"
            ),
            "unresolved": unresolved,
            "needs_confirmation": needs_confirmation,
            "editable": bool(item.get("editable")),
            "choices": list(item.get("choices") or []),
            "allow_custom": bool(item.get("allow_custom", True)),
            "raw_value": item.get("value"),
            "requirement": str(item.get("requirement") or "mentioned"),
            "group": (
                "Required"
                if str(item.get("requirement") or "") == "required"
                else "Added"
            ),
            "locked": bool(item.get("locked", True)),
            "confirmed": bool(item.get("confirmed")),
        })
    if not rows:
        summary = str(spec.get("summary") or "Not extracted")
        rows = [{
            "key": "summary", "label": "Summary", "value": summary,
            "source": "System", "unresolved": summary == "Not extracted",
        }]
    unresolved = [
        item["key"] for item in rows
        if item["unresolved"] or item.get("needs_confirmation")
    ]
    intent_status = str(
        (workflow.get("shared_intent") or {}).get("status")
        or spec.get("intent_status") or ""
    )
    blocking_questions = [
        str(item.get("prompt") or item.get("field") or "").strip()
        for item in spec.get("blocking_questions") or []
        if isinstance(item, dict)
        and str(item.get("prompt") or item.get("field") or "").strip()
    ]
    if len(blocking_questions) == 1:
        open_question = "Open question: " + blocking_questions[0]
    elif blocking_questions:
        open_question = "Open questions: " + " · ".join(blocking_questions)
    else:
        open_question = ""
    return {
        "title": "Radio Specification",
        "rows": rows,
        "aligned": not unresolved and intent_status == "confirmed",
        "unresolved": unresolved,
        "status": intent_status or "draft",
        "revision": int(spec.get("intent_revision") or 0),
        "profiles": list(spec.get("specification_profiles") or []),
        "blocking_questions": list(spec.get("blocking_questions") or []),
        "open_question": open_question,
        "optional_prompts": list(spec.get("optional_prompts") or []),
    }


def diagnosis_view(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact requested-check sequence from a scoped diagnosis."""
    diagnosis = dict(workflow.get("diagnosis") or {})
    findings = []
    for item in diagnosis.get("findings") or []:
        if not isinstance(item, dict):
            continue
        raw_status = str(item.get("status") or "unknown").lower()
        observation = item.get("observation")
        if isinstance(observation, dict):
            useful = [
                "{}={}".format(key, value)
                for key, value in observation.items()
                if value not in (None, "", [], {})
            ]
            observation = ", ".join(useful[:3])
        check_id = str(item.get("check_id") or "")
        findings.append({
            "id": check_id,
            "label": str(
                item.get("label") or _CHECK_LABELS.get(check_id)
                or item.get("dimension") or "Check"
            ),
            "status": _STATUS_LABELS.get(raw_status, raw_status.title()),
            "status_id": raw_status,
            "observation": str(observation or ""),
            "remediation": str(item.get("remediation") or ""),
            "requires_human": bool(item.get("requires_human")),
        })
    return {
        "visible": bool(findings),
        "title": "Diagnosis",
        "findings": findings,
        "summary": dict(diagnosis.get("summary") or {}),
        "report_path": str(diagnosis.get("report_path") or ""),
    }


def workflow_view(
    workflow: Dict[str, Any], spec: Dict[str, Any], claim_rows: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """Project execution state without exposing control-plane bookkeeping."""
    current = str(workflow.get("current_stage") or "")
    stages = []
    unmatched_claims = [dict(item) for item in claim_rows or []]
    for item in workflow.get("stages") or []:
        if not isinstance(item, dict):
            continue
        outcome = str(item.get("outcome") or "")
        execution = str(item.get("execution_status") or "pending")
        status = outcome or execution
        if outcome == "failed":
            status = "failed"
        elif outcome == "passed":
            status = "passed"
        elif outcome == "inconclusive" or execution == "deferred":
            # Parked on an external precondition / not yet scheduled.
            status = "waiting" if outcome == "inconclusive" else "deferred"
        stage_id = str(item.get("id") or "")
        stage_claims = [
            claim for claim in unmatched_claims
            if str(claim.get("producer") or "") == stage_id
        ]
        unmatched_claims = [claim for claim in unmatched_claims if claim not in stage_claims]
        stages.append({
            "id": stage_id,
            "label": str(item.get("label") or item.get("id") or "Stage"),
            "status": status,
            "current": str(item.get("id") or "") == current,
            # Count means independent acceptance predicates, never executions.
            "acceptance_count": len(item.get("completion") or []),
            "claims": stage_claims,
        })
    transition = _latest_transition(workflow, stages)
    previous_workflows = []
    for item in workflow.get("previous_attempts") or []:
        if not isinstance(item, dict):
            continue
        previous_workflows.append({
            "task_label": str(item.get("task_label") or item.get("task_type") or ""),
            "outcome": str(item.get("outcome") or ""),
            "status": str(item.get("execution_status") or ""),
            "stage_label": str(item.get("stage_label") or ""),
            "stage_count": len(item.get("stages") or []),
        })
    return {
        "visible": bool(stages),
        "title": str(
            workflow.get("task_label") or workflow.get("task_type") or "Workflow"
        ),
        "stages": stages,
        "previous_workflows": previous_workflows,
        "success_conditions": list(spec.get("success_conditions") or []),
        "transition": transition,
        "task_claims": unmatched_claims,
        "execution_status": str(workflow.get("execution_status") or "idle"),
        "outcome": str(workflow.get("outcome") or ""),
        "stage_index": int(workflow.get("stage_index") or 0),
        "stage_total": int(workflow.get("stage_total") or len(stages)),
    }


def _latest_transition(
    workflow: Dict[str, Any], stages: List[Dict[str, Any]]
) -> Dict[str, str]:
    index = {item["id"]: position for position, item in enumerate(stages)}
    activations = [
        item for item in workflow.get("timeline") or []
        if item.get("event") in {"stage_started", "stage_invalidated"}
        and item.get("stage_id")
    ]
    if len(activations) < 2:
        return {}
    previous = str(activations[-2].get("stage_id") or "")
    current = str(activations[-1].get("stage_id") or "")
    if not previous or not current or previous == current:
        return {}
    backward = index.get(current, 0) <= index.get(previous, 0)
    return {
        "from": previous,
        "to": current,
        "kind": "back" if backward else "forward",
        "reason": str(
            activations[-1].get("cause")
            or ("Revalidation or recovery" if backward else "Previous stage completed")
        ),
    }


def claims_view(
    claims: Iterable[Dict[str, Any]], workflow: Dict[str, Any]
) -> Dict[str, Any]:
    """Keep user-facing claim truth concise; details remain inspectable."""
    project_version = int(workflow.get("project_version") or 0)
    active_intent_id = str(
        (workflow.get("shared_intent") or {}).get("intent_id") or ""
    )
    task_type = str(workflow.get("task_type") or "")
    diagnosis_dimensions = set(
        (workflow.get("diagnosis") or {}).get("requested_dimensions") or []
    )
    carries_project_evidence = task_type == "MODIFY_PROJECT" or bool(
        diagnosis_dimensions.intersection({"project", "signal", "waveform", "metrics"})
    )
    rows: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []
    for claim in claims or []:
        claim_intent_id = str(claim.get("intent_id") or "")
        if (
            active_intent_id
            and claim_intent_id != active_intent_id
            and not carries_project_evidence
        ):
            continue
        details.append(dict(claim))
        status = str(claim.get("status") or "NotTested")
        evidence = list(claim.get("evidence") or [])
        latest = evidence[-1] if evidence else {}
        rows.append({
            "statement": str(claim.get("statement") or ""),
            "layer": str(claim.get("layer") or ""),
            "layer_label": layer_label(claim.get("layer")),
            "status": _STATUS_LABELS.get(status.lower(), status),
            "version": int(claim.get("project_version") or project_version),
            "evidence_grade": str(latest.get("evidence_grade") or ""),
            "producer": str(claim.get("producer") or ""),
        })
    priority = {"Failed": 0, "Inconclusive": 1, "Stale": 2, "Not tested": 3, "Passed": 4}
    rows.sort(key=lambda item: priority.get(item["status"], 5))
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"rows": rows, "counts": counts, "details": details}


def runtime_view(workflow: Dict[str, Any]) -> Dict[str, Any]:
    runtime = dict(workflow.get("runtime") or {})
    return {
        "visible": bool(runtime),
        "status": str(runtime.get("status") or ""),
        "running": bool(runtime.get("running")),
        "run_id": str(runtime.get("run_id") or ""),
        "remaining_seconds": float(runtime.get("remaining_seconds") or 0.0),
        "max_duration_seconds": runtime.get("max_duration_seconds")
        or runtime.get("duration_seconds"),
        "quality": str(workflow.get("quality") or runtime.get("quality") or "clean"),
    }


def present(
    *, spec: Dict[str, Any], workflow: Dict[str, Any], claims: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """Create the complete default inspector view model."""
    claim_projection = claims_view(claims, workflow)
    return {
        "phase": phase_view(workflow),
        "specification": specification_view(spec, workflow),
        "workflow": workflow_view(workflow, spec, claim_projection.get("rows") or []),
        "diagnosis": diagnosis_view(workflow),
        "claims": claim_projection,
        "runtime": runtime_view(workflow),
    }
