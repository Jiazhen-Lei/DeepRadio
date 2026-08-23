"""Deterministic single-workflow, serial-stage state machine."""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Callable, Dict, Optional

from .schema import Checkpoint, Stage, Workflow, WorkflowIntent


_TERMINALS = {"completed", "errored"}
_APPROVE = frozenset({"确认", "同意", "继续", "确认执行", "确认修改", "approve"})
_REJECT_HINTS = ("取消", "拒绝", "不同意", "不要执行", "cancel")
_TASK_LABELS = {
    "END_TO_END_SIM": "端到端仿真",
    "TX_BUILD": "构建发射链路",
    "RX_BUILD": "构建接收链路",
    "DIAGNOSE": "诊断工程",
    "MODIFY_PROJECT": "修改已有工程",
    "OBSERVE": "观测工程",
    "HARDWARE_CONFIGURE": "配置 SDR",
}
_STAGE_LABELS = {
    "spec_alignment": "规格对齐",
    "rx_spec_alignment": "接收规格对齐",
    "build_and_verify": "构建与验证",
    "tx_build_and_validate": "发射机构建与校验",
    "rx_build_and_verify": "接收机构建与验证",
    "inspect_and_diagnose": "检查与诊断",
    "repair_confirmation": "修复确认",
    "repair_and_verify": "修复与重验",
    "inspect_and_plan": "检查与规划",
    "change_confirmation": "变更确认",
    "apply_and_verify": "应用与重验",
    "inspect_and_measure": "检查与测量",
    "hardware_precheck": "硬件预检",
    "hardware_confirmation": "硬件确认",
    "configure_and_check": "配置与检查",
}


class WorkflowEngine:
    def __init__(
        self,
        path: str,
        *,
        catalog_path: str = "",
        event_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.path = path
        self.catalog_path = catalog_path or os.path.join(
            os.path.dirname(__file__), "task_catalog.yaml"
        )
        self._event_sink = event_sink
        self.catalog = self._load_catalog()
        self.workflow = self._load()
        if self.workflow:
            self._recover_interrupted()

    def _load_catalog(self) -> Dict[str, Any]:
        with open(self.catalog_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        candidates = data.get("task_candidates")
        if data.get("schema_version") != 1 or not isinstance(candidates, dict):
            raise ValueError("不支持的 Task Catalog")
        return data

    def _load(self) -> Optional[Workflow]:
        if not os.path.isfile(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return Workflow.from_dict(data)

    def _recover_interrupted(self) -> None:
        changed = False
        for stage in self.workflow.stages:
            if stage.execution_status == "running":
                stage.execution_status = "pending"
                changed = True
        if self.workflow.execution_status == "running":
            self.workflow.execution_status = "pending"
            changed = True
        if changed:
            self._event("workflow_recovered", self.digest())
            self.save()

    def consume_turn(self, user_text: str, shared_state: Any) -> Workflow:
        text = (user_text or "").strip()
        current = self.current_stage()
        if self.workflow and self.workflow.execution_status not in _TERMINALS:
            if any(hint in text.lower() for hint in ("终止任务", "取消任务", "cancel task")):
                self.workflow.execution_status = "completed"
                self.workflow.outcome = "cancelled"
                if current:
                    current.execution_status = "completed"
                    current.outcome = "cancelled"
                self._event("workflow_cancelled", {"workflow_id": self.workflow.workflow_id})
                self.save()
                return self.workflow
            if current and current.execution_status == "waiting" and current.checkpoint:
                decision = self._decision(text)
                if decision:
                    self.resolve_checkpoint(decision)
                    return self.workflow
                if current.id in ("spec_alignment", "rx_spec_alignment"):
                    update = self.classify(text, shared_state)
                    self.workflow.intent.slots.update(
                        {key: value for key, value in update.slots.items() if value not in (None, "", [])}
                    )
                    self.workflow.intent.missing_slots = self._missing_slots(
                        self.workflow.task_type, self.workflow.intent.slots, shared_state
                    )
                    if not self.workflow.intent.missing_slots:
                        self.resolve_checkpoint("approved")
                    else:
                        self.save()
                    return self.workflow
                return self.workflow
            if current and current.execution_status == "waiting" and not current.checkpoint:
                update = self.classify(text, shared_state)
                self.workflow.intent.slots.update({
                    key: value
                    for key, value in update.slots.items()
                    if value not in (None, "", [])
                })
                self.workflow.intent.missing_slots = self._missing_slots(
                    self.workflow.task_type,
                    self.workflow.intent.slots,
                    shared_state,
                )
                current.execution_status = "pending"
                self.workflow.execution_status = "pending"
                self.workflow.revision += 1
                self.workflow.intent.raw_text = text
                self.save()
                return self.workflow

        intent = self.classify(text, shared_state)
        return self.instantiate(intent, shared_state)

    def classify(self, text: str, shared_state: Any) -> WorkflowIntent:
        low = (text or "").lower()
        has_project = bool(
            getattr(getattr(shared_state, "project", None), "grc_path", "")
            or getattr(getattr(shared_state, "project", None), "config", {}).get("recipe")
        )
        if any(word in low for word in ("硬件", "usrp", "b210", "hackrf", "pluto", "sdr")):
            task_type = "HARDWARE_CONFIGURE"
        elif any(word in low for word in ("诊断", "排查", "故障", "为什么", "diagnos")):
            task_type = "DIAGNOSE"
        elif any(word in low for word in ("观察", "查看", "频谱", "星座图", "眼图", "measure")) and has_project and not any(word in low for word in ("构建", "生成", "做一个")):
            task_type = "OBSERVE"
        elif has_project and any(word in low for word in ("修改", "改成", "改为", "换成", "调参", "设为")):
            task_type = "MODIFY_PROJECT"
        elif any(word in low for word in ("接收机", "receiver", "解调", "rx")):
            task_type = "RX_BUILD"
        elif any(word in low for word in ("发射机", "transmitter", "发射链", "tx")):
            task_type = "TX_BUILD"
        else:
            task_type = "END_TO_END_SIM"
        slots = self._parse_slots(text)
        project_config = getattr(getattr(shared_state, "project", None), "config", {})
        for key in ("modulation", "channel"):
            if not slots.get(key) and project_config.get(key):
                slots[key] = project_config[key]
        missing = self._missing_slots(task_type, slots, shared_state)
        return WorkflowIntent(
            raw_text=text,
            task_type=task_type,
            confidence=0.95 if any(slots.values()) or task_type != "END_TO_END_SIM" else 0.65,
            slots=slots,
            missing_slots=missing,
        )

    @staticmethod
    def _parse_slots(text: str) -> Dict[str, Any]:
        low = (text or "").lower()
        modulation = next((name for name in ("ofdm", "qpsk", "bpsk") if name in low), "")
        channel = "awgn" if "awgn" in low or "高斯" in text or "噪声" in text else ""
        metrics = [
            name for name, hints in (
                ("evm", ("evm",)), ("ber", ("ber", "误码率")),
                ("spectrum", ("频谱", "spectrum")),
                ("constellation", ("星座", "constellation")),
                ("eye", ("眼图", "eye diagram")),
            ) if any(hint in low for hint in hints)
        ]
        slots: Dict[str, Any] = {
            "modulation": modulation,
            "channel": channel,
            "requested_metrics": metrics,
        }
        units = {"g": 1e9, "m": 1e6, "k": 1e3, "": 1.0}
        patterns = {
            "carrier_frequency": r"(?:中心频率|载频|carrier(?: frequency)?)\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*([gmk]?)hz",
            "sample_rate": r"(?:采样率|sample rate)\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*([gmk]?)s?(?:ps|hz)",
            "bandwidth": r"(?:带宽|bandwidth)\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*([gmk]?)hz",
            "symbol_rate": r"(?:符号率|symbol rate)\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*([gmk]?)(?:baud|sym/s)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, low, flags=re.IGNORECASE)
            if match:
                slots[key] = float(match.group(1)) * units[match.group(2).lower()]
        device = next((name for name in ("b210", "usrp", "hackrf", "pluto", "limesdr") if name in low), "")
        if device:
            slots["hardware"] = device
        success = re.findall(r"(?:evm|ber)\s*(?:小于|低于|<|≤)\s*\d+(?:\.\d+)?\s*%?", low)
        if success:
            slots["success_conditions"] = success
        return slots

    @staticmethod
    def _missing_slots(task_type: str, slots: Dict[str, Any], shared_state: Any) -> list[str]:
        missing = []
        if task_type in {"END_TO_END_SIM", "TX_BUILD", "RX_BUILD"} and not slots.get("modulation"):
            missing.append("modulation")
        if task_type in {"DIAGNOSE", "MODIFY_PROJECT", "OBSERVE"}:
            project = getattr(shared_state, "project", None)
            if not (getattr(project, "grc_path", "") or getattr(project, "config", {}).get("recipe")):
                missing.append("current_project")
        if task_type == "HARDWARE_CONFIGURE" and not slots.get("hardware"):
            missing.append("hardware")
        return missing

    def instantiate(self, intent: WorkflowIntent, shared_state: Any) -> Workflow:
        candidate = self.catalog["task_candidates"].get(intent.task_type)
        if not candidate:
            raise ValueError(f"未知 Task Type: {intent.task_type}")
        stages = []
        for raw in candidate.get("stages") or []:
            stage_id = str(raw.get("id") or "")
            if stage_id in ("spec_alignment", "rx_spec_alignment") and not intent.missing_slots:
                continue
            stages.append(Stage.from_dict(raw))
        if not stages:
            raise ValueError(f"Task {intent.task_type} 没有可执行 Stage")
        version = int(getattr(getattr(shared_state, "project", None), "flowgraph_version", 0))
        self.workflow = Workflow(
            workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
            task_type=intent.task_type,
            intent=intent,
            stages=stages,
            base_project_version=version,
            current_stage=stages[0].id,
            catalog_version=int(self.catalog.get("schema_version", 1)),
        )
        self._activate_current()
        self._event("intent_classified", {"task_type": intent.task_type, "slots": intent.slots, "missing_slots": intent.missing_slots})
        self._event("workflow_created", self.digest())
        self.save()
        return self.workflow

    def current_stage(self) -> Optional[Stage]:
        return self.workflow.stage() if self.workflow else None

    def start_stage(self) -> Optional[Stage]:
        stage = self.current_stage()
        if not stage or stage.execution_status == "waiting":
            return stage
        if stage.execution_status not in ("pending", "invalidated"):
            return stage
        stage.execution_status = "running"
        stage.attempt += 1
        self.workflow.execution_status = "running"
        self._event("stage_started", {"workflow_id": self.workflow.workflow_id, "stage_id": stage.id, "attempt": stage.attempt})
        self.save()
        return stage

    def accept_result(self, result: Any) -> bool:
        if not self.workflow:
            raise ValueError("没有活动 Workflow")
        data = result if isinstance(result, dict) else vars(result)
        stage = self.current_stage()
        if not stage:
            raise ValueError("没有 current_stage")
        stale = (
            str(data.get("workflow_id") or self.workflow.workflow_id) != self.workflow.workflow_id
            or str(data.get("stage_id") or stage.id) != stage.id
            or int(data.get("workflow_revision", self.workflow.revision)) != self.workflow.revision
            or int(data.get("base_project_version", self.workflow.base_project_version)) != self.workflow.base_project_version
        )
        if stale:
            self._event("stale_result_discarded", {"stage_id": data.get("stage_id"), "workflow_revision": data.get("workflow_revision")})
            return False
        ok = bool(data.get("ok"))
        outcome = str(data.get("outcome") or ("passed" if ok else "failed"))
        stage.result = {
            key: data.get(key) for key in ("note", "artifacts", "produced_claims", "proposed_changes") if data.get(key)
        }
        if data.get("errored"):
            stage.execution_status = "errored"
            stage.outcome = "inconclusive"
            transition_key = "errored"
            self._event("stage_errored", {"stage_id": stage.id, "attempt": stage.attempt})
        else:
            stage.execution_status = "completed"
            stage.outcome = outcome if outcome in ("passed", "failed", "inconclusive") else ("passed" if ok else "failed")
            improvement = bool(data.get("improvement_available"))
            if stage.outcome == "passed":
                transition_key = "passed"
            elif improvement and stage.attempt < stage.max_attempts:
                transition_key = "failed_with_improvement"
            elif stage.outcome == "failed":
                transition_key = "failed_without_improvement"
            else:
                transition_key = "errored"
            self._event("stage_completed", {"stage_id": stage.id, "outcome": stage.outcome, "attempt": stage.attempt})
        target = stage.transitions.get(transition_key)
        if target is None and transition_key == "passed":
            target = stage.transitions.get("completed")
        if target is None and stage.outcome == "failed":
            target = stage.transitions.get("failed")
        self._transition(target or ("completed" if ok else "waiting_user"))
        self.save()
        return True

    def resolve_checkpoint(self, decision: str) -> None:
        stage = self.current_stage()
        if not stage or not stage.checkpoint or stage.execution_status != "waiting":
            raise ValueError("当前没有待决 Checkpoint")
        normalized = "approved" if decision == "approved" else "rejected"
        stage.checkpoint.decision_status = normalized
        stage.execution_status = "completed"
        stage.outcome = "passed" if normalized == "approved" else "cancelled"
        self._event("checkpoint_resolved", {"stage_id": stage.id, "decision": normalized})
        self._transition(stage.transitions.get(normalized, "completed"))
        self.save()

    def invalidate(self, cause: str, project_version: int) -> None:
        if not self.workflow:
            return
        affected = [
            stage for stage in self.workflow.stages
            if stage.execution_status == "completed" and any(
                token in stage.id for token in ("verify", "measure", "diagnose")
            )
        ]
        if not affected:
            return
        for stage in affected:
            stage.execution_status = "invalidated"
            stage.outcome = ""
            stage.attempt = 0
            stage.result = {}
        self.workflow.revision += 1
        self.workflow.base_project_version = int(project_version)
        self.workflow.current_stage = affected[0].id
        self.workflow.execution_status = "pending"
        self.workflow.outcome = ""
        self._event("stage_invalidated", {"cause": cause, "stages": [stage.id for stage in affected], "project_version": project_version})
        self.save()

    def finish(self, outcome: str = "passed") -> None:
        """Finish an idempotent/no-op workflow without forcing another stage."""
        if not self.workflow:
            return
        stage = self.current_stage()
        if stage and stage.execution_status not in ("completed", "errored"):
            stage.execution_status = "completed"
            stage.outcome = "cancelled" if outcome == "cancelled" else "passed"
        self.workflow.execution_status = "completed"
        self.workflow.outcome = outcome
        self._event(
            "workflow_cancelled" if outcome == "cancelled" else "workflow_completed",
            {"workflow_id": self.workflow.workflow_id, "outcome": outcome},
        )
        self.save()

    def save(self) -> str:
        if not self.workflow:
            return self.path
        self.workflow.validate()
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.workflow.to_dict(), handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return self.path

    def digest(self) -> Dict[str, Any]:
        if not self.workflow:
            return {}
        stage = self.current_stage()
        index = next((i for i, item in enumerate(self.workflow.stages, 1) if item.id == self.workflow.current_stage), 0)
        waiting_reason = ""
        if stage and stage.checkpoint:
            waiting_reason = stage.checkpoint.reason
        return {
            "workflow_id": self.workflow.workflow_id,
            "task_type": self.workflow.task_type,
            "task_label": _TASK_LABELS.get(self.workflow.task_type, self.workflow.task_type),
            "execution_status": self.workflow.execution_status,
            "outcome": self.workflow.outcome,
            "current_stage": self.workflow.current_stage,
            "stage_label": _STAGE_LABELS.get(
                self.workflow.current_stage, self.workflow.current_stage
            ),
            "stage_index": index,
            "stage_total": len(self.workflow.stages),
            "waiting_reason": waiting_reason,
            "revision": self.workflow.revision,
        }

    @staticmethod
    def _decision(text: str) -> str:
        normalized = (text or "").strip().lower()
        if normalized in _APPROVE or any(word in normalized for word in ("确认执行", "同意修改", "继续执行")):
            return "approved"
        if any(word in normalized for word in _REJECT_HINTS):
            return "rejected"
        return ""

    def _activate_current(self) -> None:
        stage = self.current_stage()
        if not stage:
            return
        if "checkpoint" in stage.interaction:
            reason = ", ".join(self.workflow.intent.missing_slots) or _STAGE_LABELS.get(
                stage.id, stage.id
            )
            stage.execution_status = "waiting"
            stage.checkpoint = Checkpoint(id=f"cp-{uuid.uuid4().hex[:8]}", reason=reason)
            self.workflow.execution_status = "waiting"
            self._event("checkpoint_opened", {"stage_id": stage.id, "reason": reason})

    def _transition(self, target: str) -> None:
        if target == "completed":
            self.workflow.execution_status = "completed"
            self.workflow.outcome = "passed"
            self._event("workflow_completed", {"workflow_id": self.workflow.workflow_id})
            return
        if target == "cancelled":
            self.workflow.execution_status = "completed"
            self.workflow.outcome = "cancelled"
            self._event("workflow_cancelled", {"workflow_id": self.workflow.workflow_id})
            return
        if target == "stop":
            self.workflow.execution_status = "errored"
            self.workflow.outcome = "inconclusive"
            return
        if target == "waiting_user":
            self.workflow.execution_status = "waiting"
            return
        if not self.workflow.stage(target):
            raise ValueError(f"Stage 转移目标不存在: {target}")
        self.workflow.current_stage = target
        next_stage = self.current_stage()
        if next_stage.execution_status in ("completed", "errored"):
            next_stage.execution_status = "pending"
            next_stage.outcome = ""
        self.workflow.execution_status = "pending"
        self._activate_current()

    def _event(self, event: str, payload: Dict[str, Any]) -> None:
        if self._event_sink:
            self._event_sink(event, payload)
