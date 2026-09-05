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
    "flowgraph": "Flowgraph",
    "signal": "Signal",
    "hardware": "Hardware",
    "runtime": "Runtime",
    "rf": "RF",
    "task": "Task",
    "protocol": "Protocol",
    "waveform": "Waveform",
    "radio": "Radio design",
    "link": "Link",
    "measurement": "Measurement",
    "ota": "Over-the-air",
    "": "General",
}

_CLAIM_ASSESSMENTS = {
    "passed": "Supported",
    "supported": "Supported",
    "failed": "Contradicted",
    "contradicted": "Contradicted",
    "inconclusive": "Unresolved",
    "unresolved": "Unresolved",
    "nottested": "Untested",
    "not_tested": "Untested",
    "untested": "Untested",
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
    "llm": "Extracted",
    "extracted": "Extracted",
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

_TASK_LABELS = {
    "END_TO_END_SIM": "End-to-End Link",
    "TX_BUILD": "Transmit Flowgraph",
    "RX_BUILD": "Receive Flowgraph",
    "DIAGNOSE": "Diagnosis",
    "MODIFY_PROJECT": "Flowgraph Update",
    "OBSERVE": "Signal Observation",
    "HARDWARE_CONFIGURE": "Hardware Operation",
}

_STAGE_LABELS = {
    "hardware_precheck": "Host Readiness",
    "discover_and_probe_hardware": "Device Discovery and Probe",
    "flowgraph_confirmation": "Flowgraph Review",
    "rf_plan_confirmation": "Operation Confirmation",
    "configure_device": "Configure Device",
    "transmit_bounded": "Bounded Transmission",
    "run_bounded": "Bounded Runtime",
    "over_air_verification": "Over-the-Air Verification",
    "stop_and_finalize": "Stop and Finalize",
    "stop_runtime": "Stop Runtime",
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
    if purpose in {"proposal", "flowgraph_proposal", "flowgraph_review"} or (
        wait == "approval" and effect in {
            "ARTIFACT_WRITE", "PROJECT_WRITE", "project.write"
        }
    ):
        return {"id": "co_construct", "label": "CO-CONSTRUCT"}
    if any(token in current for token in ("build", "proposal", "apply", "flowgraph")):
        return {"id": "co_construct", "label": "CO-CONSTRUCT"}
    return {"id": "verify_operate", "label": "VERIFY AND OPERATE"}


def specification_view(
    spec: Dict[str, Any], workflow: Dict[str, Any]
) -> Dict[str, Any]:
    """Normalize Radio Specification rows with explicit provenance."""
    rows = []
    raw_items = [
        item for item in spec.get("radio_specification") or []
        if isinstance(item, dict)
    ]
    visible_keys = {str(item.get("key") or "") for item in raw_items}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        # duration_seconds is already presented as "Maximum duration".
        # max_duration_seconds is the internal safety ceiling for the same
        # concept and must not appear as a duplicate public field.
        if key == "max_duration_seconds" and "duration_seconds" in visible_keys:
            continue
        value = item.get("display_value", item.get("value"))
        group = str(item.get("group") or "").lower()
        if group not in {"required", "added"}:
            group = (
                "required"
                if str(item.get("requirement") or "") == "required"
                else "added"
            )
        status = str(item.get("status") or "").lower()
        if status not in {"aligned", "needs_confirmation", "missing"}:
            if bool(item.get("unresolved")) or value in (None, "", []):
                status = "missing"
            elif bool(item.get("needs_confirmation")):
                status = "needs_confirmation"
            else:
                status = "aligned"
        unresolved = status == "missing"
        needs_confirmation = status == "needs_confirmation"
        source = str(item.get("source") or "")
        rows.append({
            "key": key,
            "label": str(item.get("label") or item.get("key") or ""),
            "value": "?" if unresolved else str(value),
            "source": "Unresolved" if unresolved else _SOURCE_LABELS.get(
                source, source.replace("_", " ").title() if source else "System"
            ),
            "unresolved": unresolved,
            "needs_confirmation": needs_confirmation,
            "status": status,
            "status_label": (
                "Needs confirmation" if needs_confirmation
                else ""
            ),
            "group": "Required" if group == "required" else "Added",
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
    return {
        "title": "Radio Specification",
        "rows": rows,
        "aligned": not unresolved and intent_status == "confirmed",
        "unresolved": unresolved,
        "status": intent_status or "draft",
        "revision": int(
            spec.get("specification_revision") or spec.get("intent_revision") or 0
        ),
        "blocking_questions": list(spec.get("blocking_questions") or []),
        "open_question": "",
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
        outcome = str(item.get("outcome") or "").lower()
        execution = str(
            item.get("status") or item.get("execution_status") or "pending"
        ).lower()
        outcome = {"pass": "passed", "complete": "completed"}.get(
            outcome, outcome
        )
        execution = {
            "pass": "passed", "complete": "passed", "completed": "passed"
        }.get(
            execution, execution
        )
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
            "label": str(
                _STAGE_LABELS.get(stage_id)
                or stage_id.replace("_", " ").title()
                or "Stage"
            ),
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
            "task_label": str(
                _TASK_LABELS.get(str(item.get("task_type") or ""))
                or "Previous workflow"
            ),
            "outcome": str(item.get("outcome") or ""),
            "status": str(item.get("execution_status") or ""),
            "stage_label": str(item.get("stage_label") or ""),
            "stage_count": len(item.get("stages") or []),
        })
    intent_status = str(
        spec.get("intent_status")
        or (workflow.get("shared_intent") or {}).get("status")
        or ""
    )
    alignment_labels = {
        "draft": ("Specification Draft", "running"),
        "awaiting_input": ("Awaiting Specification Details", "waiting"),
        "awaiting_confirmation": ("Awaiting Specification Confirmation", "waiting"),
    }
    alignment_only = not stages and intent_status in alignment_labels
    if alignment_only:
        label, status = alignment_labels[intent_status]
        stages = [{
            "id": "intent_alignment",
            "label": label,
            "status": status,
            "current": True,
            "acceptance_count": 0,
            "claims": [],
        }]
    completed = [
        item for item in stages
        if str(item.get("status") or "") in {"passed", "completed"}
    ]
    current_rows = [
        item for item in stages
        if item not in completed and (
            item.get("current")
            or str(item.get("status") or "") in {
                "running", "waiting", "failed", "errored"
            }
        )
    ]
    pending = [
        item for item in stages
        if item not in completed and item not in current_rows
    ]
    return {
        "visible": bool(stages),
        "title": str(
            "Radio Specification"
            if alignment_only else
            _TASK_LABELS.get(str(workflow.get("task_type") or ""))
            or "Workflow"
        ),
        "stages": stages,
        "completed": completed,
        "current": current_rows,
        "pending": pending,
        "previous_workflows": previous_workflows,
        "success_conditions": list(spec.get("success_conditions") or []),
        "transition": transition,
        "task_claims": unmatched_claims,
        "execution_status": (
            "waiting" if alignment_only else
            str(workflow.get("execution_status") or "idle")
        ),
        "state_label": {
            "completed": "Finished",
            "pending": "Planned",
            "running": "In progress",
            "waiting": "Waiting",
            "errored": "Needs attention",
            "invalidated": "Needs revalidation",
        }.get(
            str(workflow.get("execution_status") or ""),
            "Not started",
        ),
        "outcome": str(workflow.get("outcome") or ""),
        "stage_index": int(
            workflow.get("stage_index") or (1 if alignment_only else 0)
        ),
        "stage_total": int(workflow.get("stage_total") or len(stages)),
    }


def _latest_transition(
    workflow: Dict[str, Any], stages: List[Dict[str, Any]]
) -> Dict[str, str]:
    index = {item["id"]: position for position, item in enumerate(stages)}
    labels = {item["id"]: item["label"] for item in stages}
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
        "from": labels.get(previous, "Previous step"),
        "to": labels.get(current, "Current step"),
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
        status = _CLAIM_ASSESSMENTS.get(
            str(claim.get("status") or "Untested").lower(),
            str(claim.get("status") or "Untested"),
        )
        freshness = str(claim.get("freshness") or "Current")
        evidence = list(claim.get("evidence") or [])
        latest = evidence[-1] if evidence else {}
        observation = latest.get("observation") or {}
        if isinstance(observation, dict):
            evidence_summary = ", ".join(
                "{}={}".format(key, value)
                for key, value in list(observation.items())[:4]
                if value not in (None, "", [], {})
            )
        else:
            evidence_summary = str(observation or "")
        rows.append({
            "statement": str(claim.get("statement") or ""),
            "layer": str(claim.get("layer") or ""),
            "layer_label": layer_label(claim.get("layer")),
            "status": _STATUS_LABELS.get(status.lower(), status),
            "freshness": freshness,
            "version": int(claim.get("project_version") or project_version),
            "evidence_grade": str(latest.get("evidence_grade") or ""),
            "evidence_test": str(latest.get("test") or ""),
            "evidence_summary": evidence_summary,
            "artifact": str(latest.get("artifact") or ""),
            "stale_reason": str(claim.get("stale_reason") or ""),
            "producer": str(claim.get("producer") or ""),
        })
    priority = {"Contradicted": 0, "Unresolved": 1, "Untested": 2, "Supported": 3}
    rows.sort(key=lambda item: (
        str(item.get("freshness") or "").lower() != "stale",
        priority.get(item["status"], 4),
    ))
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "rows": rows,
        "counts": counts,
        "stale_count": sum(
            str(item.get("freshness") or "").lower() == "stale" for item in rows
        ),
        "details": details,
    }


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


def _format_si(value: Any, unit: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "not set"
    for scale, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(number) >= scale:
            return f"{number / scale:g} {suffix}{unit}"
    return f"{number:g} {unit}"


def interaction_view(
    workflow: Dict[str, Any], pending: Dict[str, Any] | None
) -> Dict[str, Any]:
    """Allowlisted user-facing checkpoint projection.

    Command identifiers are retained only for button dispatch; all visible
    strings are generated here and never fall back to raw stage/task fields.
    """
    item = dict(pending or {})
    wait = str(workflow.get("wait_kind") or "")
    if item.get("action") == "intent_alignment" or wait == "intent":
        return {"visible": False}
    if not item and wait == "approval":
        item = {
            "action": str(workflow.get("current_stage") or "workflow_checkpoint"),
            "checkpoint_id": workflow.get("checkpoint_id"),
            "requested_effect": workflow.get("requested_effect"),
            "purpose": workflow.get("checkpoint_purpose"),
        }
    elif not item and wait in {"recovery", "capability"}:
        item = {
            "action": (
                "stage_recovery" if wait == "recovery" else "capability_blocker"
            ),
            "blocker": dict(workflow.get("blocker") or {}),
            "can_retry": bool(
                wait == "recovery"
                or dict(workflow.get("blocker") or {}).get("retryable")
            ),
        }
    action = str(item.get("action") or "")
    if not action or item.get("approved"):
        return {"visible": False}
    purpose = str(item.get("purpose") or "")
    effect = str(item.get("requested_effect") or "")
    direction = str(
        item.get("direction")
        or dict((workflow.get("intent_ir") or {}).get("slots") or {}).get("direction")
        or ""
    ).lower()
    can_confirm = bool(item.get("can_confirm", True))
    can_retry = bool(item.get("can_retry"))
    confirm_label = "Confirm"
    cancel_label = "Cancel"
    message = "Confirm the next workflow step."
    message_cn = "确认后继续下一工作流步骤。"
    if action == "flowgraph_confirmation" or purpose == "flowgraph_review":
        message = (
            "Review the current flowgraph. Confirm it to continue, ask a "
            "question, or describe a change. Confirmation does not start RF."
        )
        message_cn = (
            "请检查当前流图。确认后继续，也可以提问或说明需要修改的内容。"
            "确认不会启动射频。"
        )
        confirm_label = "Confirm Flowgraph"
    elif action == "over_air_verification":
        message = (
            "Confirm whether an independent receiver observed the target "
            "signal over the air. Human confirmation is required; you may "
            "attach a screenshot."
        )
        message_cn = (
            "请确认独立接收设备是否通过空口观测到目标信号。"
            "此处需要人工确认，也可以添加截图。"
        )
        confirm_label, cancel_label = "Target Signal Observed", "Not Observed"
    elif (
        action == "rf_plan_confirmation"
        or purpose == "rf_authorization"
        or effect in {"RF_RUN", "rf.start"}
    ):
        device = dict(item.get("device") or {})
        name = str(device.get("type") or "SDR")
        identity = str(device.get("identity") or "not detected")
        duration = item.get("max_duration_seconds") or 30
        frequency = _format_si(item.get("center_frequency"), "Hz")
        sample_rate = _format_si(item.get("sample_rate"), "sps")
        bandwidth = _format_si(item.get("bandwidth"), "Hz")
        gain = item.get("rx_gain", item.get("gain"))
        level = (
            f"Gain {gain} dB" if direction == "rx" and gain is not None
            else "gain not set" if direction == "rx"
            else f"Attenuation {item.get('tx_attenuation')} dB"
            if item.get("tx_attenuation") is not None
            else f"Gain {item.get('tx_gain')} dB"
            if item.get("tx_gain") is not None
            else "power not set"
        )
        if purpose == "rf_authorization" or effect in {"RF_RUN", "rf.start"}:
            if direction == "rx":
                message = (
                    f"Authorize bounded reception on {name} [{identity}] at "
                    f"{frequency}, {sample_rate}, bandwidth {bandwidth}, {level}, "
                    f"for up to {duration} seconds. The controlled stop remains active."
                )
                message_cn = (
                    f"批准在 {name} [{identity}] 上执行限时接收：频率 {frequency}，"
                    f"采样率 {sample_rate}，带宽 {bandwidth}，{level}，最长 {duration} 秒。"
                    "受控停止保持有效。"
                )
                confirm_label = "Approve Bounded Reception"
            else:
                message = (
                    f"Authorize a bounded RF run on {name} [{identity}] at "
                    f"{frequency}, {sample_rate}, bandwidth {bandwidth}, {level}, "
                    f"for up to {duration} seconds. The controlled stop remains active."
                )
                message_cn = (
                    f"批准在 {name} [{identity}] 上执行限时射频运行：频率 {frequency}，"
                    f"采样率 {sample_rate}，带宽 {bandwidth}，{level}，最长 {duration} 秒。"
                    "受控停止保持有效。"
                )
                confirm_label = "Approve Bounded Transmission"
        else:
            message = (
                f"Confirm saved configuration for {name} [{identity}] at "
                f"{frequency}, {sample_rate}, bandwidth {bandwidth}, {level}, "
                "without starting RF."
            )
            message_cn = (
                f"确认已保存的 {name} [{identity}] 配置：频率 {frequency}，"
                f"采样率 {sample_rate}，带宽 {bandwidth}，{level}。不会启动射频。"
            )
            confirm_label = (
                "Confirm Saved Configuration"
                if purpose == "config_handoff"
                else "Confirm Configuration"
            )
    elif action == "stage_recovery" or wait == "recovery":
        message = (
            "The current step did not pass. Retry after correcting the "
            "reported condition, or cancel the workflow."
        )
        message_cn = "当前步骤未通过。修正所报告的问题后重试，或取消工作流。"
        confirm_label, cancel_label = "Retry This Step", "Cancel Workflow"
        can_retry = True
    elif action == "capability_blocker" or wait == "capability":
        blocker = dict(item.get("blocker") or workflow.get("blocker") or {})
        message = str(
            blocker.get("message")
            or "A required system capability is not available."
        )
        message_cn = str(
            blocker.get("message_cn")
            or blocker.get("message")
            or "所需的系统能力不可用。"
        )
        remediation = str(blocker.get("remediation") or "")
        if remediation:
            message += " " + remediation
            message_cn += " " + str(
                blocker.get("remediation_cn") or remediation
            )
        can_confirm = False
        can_retry = bool(blocker.get("retryable", can_retry))
        cancel_label = "Cancel Workflow"
    return {
        "visible": True,
        "message": message,
        "message_cn": message_cn,
        "confirm_label": confirm_label,
        "cancel_label": cancel_label,
        "can_confirm": can_confirm,
        "can_retry": can_retry,
        "show_evidence": action == "over_air_verification",
        "action": action,
        "purpose": purpose,
        "checkpoint_id": item.get("checkpoint_id"),
        "allowed_actions": [
            name
            for name, enabled in (
                ("confirm", can_confirm),
                ("retry_stage", can_retry),
                ("cancel_workflow", True),
            )
            if enabled
        ],
    }


_HARDWARE_STATE_LABELS = {
    "not_started": "not_started",
    "detected": "detected",
    "not_found": "not_found",
    "failed": "failed",
    "not_applicable": "not_applicable",
}


def hardware_detection_view(
    workflow: Dict[str, Any], spec: Dict[str, Any]
) -> Dict[str, Any]:
    """Project hardware presence into one field with five exclusive states."""
    capabilities = {
        str(item) for item in (workflow.get("capabilities") or []) if item
    }
    stage_ids = {
        str(item.get("id") or "")
        for item in (workflow.get("stages") or [])
        if isinstance(item, dict)
    }
    hardware_needed = bool(
        capabilities.intersection({"hardware_configure", "deploy", "hardware_runtime"})
        or str(workflow.get("task_type") or "") == "HARDWARE_CONFIGURE"
        or stage_ids.intersection({"hardware_preparation", "physical_rf_execution"})
        or spec.get("hardware")
    )
    detection = dict(workflow.get("hardware_detection") or {})
    observed = dict(workflow.get("observed_device") or {})
    state = str(detection.get("state") or "")
    if not hardware_needed:
        state = "not_applicable"
    elif state not in _HARDWARE_STATE_LABELS:
        state = "detected" if observed else "not_started"
    error = str(detection.get("error") or "") \
        if state in {"failed", "not_found"} else ""
    return {
        "visible": True,
        "title": "Hardware Detection",
        "label": "Hardware",
        "applicable": hardware_needed,
        "state": state,
        "state_label": _HARDWARE_STATE_LABELS.get(state, state),
        "error": error,
        "rows": [{
            "id": "hardware",
            "label": "Hardware",
            "state": state,
            "state_label": _HARDWARE_STATE_LABELS.get(state, state),
            "detail": error,
        }],
    }


def present(
    *, spec: Dict[str, Any], workflow: Dict[str, Any],
    claims: Iterable[Dict[str, Any]], pending: Dict[str, Any] | None = None
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
        "interaction": interaction_view(workflow, pending),
        "hardware_detection": hardware_detection_view(workflow, spec),
    }
