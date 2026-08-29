"""Pure view-model projection for the DeepRadio workflow inspector.

The GTK widgets intentionally do not interpret Workflow/SharedState JSON.
Keeping that interpretation here makes the paper-facing UI testable without a
display server and prevents raw control-plane fields from leaking into the
default user view.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


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
        })
    if not rows:
        summary = str(spec.get("summary") or "尚未提取")
        rows = [{
            "key": "summary", "label": "Summary", "value": summary,
            "source": "System", "unresolved": summary == "尚未提取",
        }]
    unresolved = [
        item["key"] for item in rows
        if item["unresolved"] or item.get("needs_confirmation")
    ]
    intent_status = str(
        (workflow.get("shared_intent") or {}).get("status")
        or spec.get("intent_status") or ""
    )
    return {
        "title": "Radio Specification",
        "rows": rows,
        "aligned": not unresolved and intent_status == "confirmed",
        "unresolved": unresolved,
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
    workflow: Dict[str, Any], spec: Dict[str, Any]
) -> Dict[str, Any]:
    """Project execution state without exposing control-plane bookkeeping."""
    current = str(workflow.get("current_stage") or "")
    stages = []
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
        stages.append({
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or item.get("id") or "Stage"),
            "status": status,
            "current": str(item.get("id") or "") == current,
            # Count means independent acceptance predicates, never executions.
            "acceptance_count": len(item.get("completion") or []),
        })
    return {
        "visible": bool(stages),
        "title": str(
            workflow.get("task_label") or workflow.get("task_type") or "Workflow"
        ),
        "stages": stages,
        "success_conditions": list(spec.get("success_conditions") or []),
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
            "status": _STATUS_LABELS.get(status.lower(), status),
            "version": int(claim.get("project_version") or project_version),
            "evidence_grade": str(latest.get("evidence_grade") or ""),
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
    return {
        "phase": phase_view(workflow),
        "specification": specification_view(spec, workflow),
        "workflow": workflow_view(workflow, spec),
        "diagnosis": diagnosis_view(workflow),
        "claims": claims_view(claims, workflow),
        "runtime": runtime_view(workflow),
    }
