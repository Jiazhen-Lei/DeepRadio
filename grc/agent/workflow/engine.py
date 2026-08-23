"""Deterministic single-workflow, serial-stage state machine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any, Callable, Dict, Optional

from .completion import KNOWN_COMPLETIONS
from .schema import Checkpoint, Stage, Workflow, WorkflowIntent


_TERMINALS = {"completed", "errored"}
_APPROVE = frozenset({"确认", "同意", "继续", "确认执行", "确认修改", "approve"})
_REJECT_HINTS = ("取消", "拒绝", "不同意", "不要执行", "cancel")
_KNOWN_AGENTS = frozenset(
    {
        "spec_agent",
        "radio_design_agent",
        "flowgraph_agent",
        "verification_agent",
        "diagnosis_agent",
        "hardware_agent",
        "protocol_agent",
    }
)
_TERMINAL_TARGETS = frozenset({"completed", "cancelled", "stop", "waiting_user"})
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
    "protocol_spec_alignment": "BLE 规格对齐",
    "build_ble_advertiser": "构建 BLE 广播",
    "offline_protocol_verify": "BLE 离线协议校验",
    "discover_and_probe_device": "发现并探测 B210",
    "rf_plan_confirmation": "RF 计划确认",
    "configure_device": "配置设备参数",
    "transmit_bounded": "有限时长发射",
    "over_air_verification": "LightBlue 空口验收",
    "stop_and_finalize": "停止并完成审计",
    "discover_and_probe_hardware": "发现并探测硬件",
    "run_bounded": "有限时长运行",
    "runtime_observation": "运行结果确认",
    "stop_runtime": "停止硬件运行",
}

_ALIGNMENT_STAGES = frozenset(
    {"spec_alignment", "rx_spec_alignment", "protocol_spec_alignment"}
)
_BUILD_HINTS = ("构建", "生成", "创建", "做一个", "build", "create")
_MODIFY_HINTS = ("修改", "改成", "改为", "换成", "调参", "设为", "modify", "change")
_OBSERVE_HINTS = ("观察", "查看", "频谱", "星座图", "眼图", "measure", "spectrum")
_HARDWARE_HINTS = ("硬件", "usrp", "b210", "hackrf", "pluto", "limesdr", "sdr")
_CAUSE_DEPENDENCIES = {
    "flowgraph_changed": {"project.flowgraph"},
    "snapshot_restored": {"project.flowgraph"},
    "project_version_mismatch": {"project.flowgraph"},
    "spec_changed": {"spec.architecture"},
    "architecture_changed": {"spec.architecture"},
    "recipe_changed": {"spec.architecture", "project.flowgraph"},
    "success_conditions_changed": {"success_conditions"},
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
        for task_type, candidate in candidates.items():
            stage_sets = [list(candidate.get("stages") or [])]
            if candidate.get("deploy_stages"):
                stage_sets.append(list(candidate.get("deploy_stages") or []))
            if candidate.get("runtime_stages"):
                stage_sets.append(list(candidate.get("runtime_stages") or []))
            for stages in stage_sets:
                self._validate_catalog_stages(task_type, stages)
        return data

    @staticmethod
    def _validate_catalog_stages(task_type: str, stages: list[Dict[str, Any]]) -> None:
        ids = [str(stage.get("id") or "") for stage in stages]
        if not task_type or not stages or any(not stage_id for stage_id in ids):
            raise ValueError(f"Task {task_type!r} 缺少有效 Stage")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Task {task_type} Stage id 重复")
        for stage in stages:
            unknown_agents = set(stage.get("recommended_agents") or []) - _KNOWN_AGENTS
            unknown_completion = set(stage.get("completion") or []) - KNOWN_COMPLETIONS
            if unknown_agents:
                raise ValueError(f"Task {task_type} 使用未知 Subagent: {sorted(unknown_agents)}")
            if unknown_completion:
                raise ValueError(
                    f"Task {task_type} 使用未知 completion: {sorted(unknown_completion)}"
                )
            for target in (stage.get("on") or {}).values():
                if target not in ids and target not in _TERMINAL_TARGETS:
                    raise ValueError(
                        f"Task {task_type} Stage {stage.get('id')} 转移目标不存在: {target}"
                    )

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

    def reconcile_project_version(self, project_version: int) -> None:
        """Invalidate stale verification facts after loading state/workflow."""
        if not self.workflow:
            return
        current = int(project_version)
        if current != self.workflow.base_project_version:
            self.invalidate("project_version_mismatch", current)

    def consume_turn(self, user_text: str, shared_state: Any) -> Workflow:
        text = (user_text or "").strip()
        current = self.current_stage()
        if self.workflow and self.workflow.execution_status not in _TERMINALS:
            relation = self._turn_relation(text, shared_state)
            self.workflow.intent.turn_relation = relation
            self._event("turn_relation_classified", {"relation": relation, "text": text})
            if relation == "cancel":
                self.workflow.execution_status = "completed"
                self.workflow.outcome = "cancelled"
                if current:
                    current.execution_status = "completed"
                    current.outcome = "cancelled"
                self._event("workflow_cancelled", {"workflow_id": self.workflow.workflow_id})
                self.save()
                return self.workflow
            if relation in ("approval", "rejection"):
                if current and current.id in (
                    "over_air_verification", "runtime_observation"
                ):
                    key = (
                        "over_air_observed"
                        if current.id == "over_air_verification"
                        else "runtime_observed"
                    )
                    self.workflow.intent.slots[key] = relation == "approval"
                self.resolve_checkpoint(
                    "approved" if relation == "approval" else "rejected"
                )
                return self.workflow
            if current and current.execution_status == "waiting" and current.checkpoint:
                if current.id in _ALIGNMENT_STAGES:
                    self._merge_turn_slots(text, shared_state)
                    if not self.workflow.intent.missing_slots and not self.workflow.intent.validation_errors:
                        self.resolve_checkpoint("approved")
                    else:
                        self.save()
                    return self.workflow
                if relation == "adjustment":
                    self._merge_turn_slots(text, shared_state)
                    current.checkpoint.reason = self._checkpoint_reason(current)
                    self.workflow.revision += 1
                    self.save()
                return self.workflow
            if relation in ("answer", "feedback", "adjustment"):
                self._merge_turn_slots(text, shared_state)
                if current and self.workflow.execution_status == "waiting":
                    self._remember_result(current, "user_feedback")
                    current.execution_status = "pending"
                    current.outcome = ""
                    current.result = {}
                self.workflow.execution_status = "pending"
                self.workflow.revision += 1
                self.workflow.intent.raw_text = text
                self.save()
                return self.workflow
            if relation != "new_task":
                self.save()
                return self.workflow
            self._event(
                "workflow_superseded",
                {"workflow_id": self.workflow.workflow_id, "new_text": text},
            )

        intent = self.classify(text, shared_state)
        return self.instantiate(intent, shared_state)

    def _turn_relation(self, text: str, shared_state: Any) -> str:
        low = (text or "").lower().strip()
        current = self.current_stage()
        if any(hint in low for hint in ("终止任务", "取消任务", "cancel task")):
            return "cancel"
        if current and current.execution_status == "waiting" and current.checkpoint:
            decision = self._decision(text)
            if decision:
                return "approval" if decision == "approved" else "rejection"
        if self._is_explicit_new_task(low):
            return "new_task"
        classified = self.classify(text, shared_state)
        if self._is_strong_task_switch(classified):
            return "new_task"
        if current and current.execution_status == "waiting" and current.checkpoint:
            if current.id in _ALIGNMENT_STAGES:
                return "answer"
            return "adjustment"
        if self.workflow and self.workflow.execution_status == "waiting":
            return "feedback"
        if any(hint in low for hint in ("继续", "重试", "再试", "resume", "retry")):
            return "feedback"
        if current and current.execution_status in ("pending", "invalidated"):
            if any(hint in low for hint in ("调整", "改一下方案", "补充", "其余条件")):
                return "adjustment"
        if self.workflow.execution_status == "waiting":
            return "feedback"
        return "adjustment"

    @staticmethod
    def _is_explicit_new_task(low: str) -> bool:
        return any(
            hint in low
            for hint in ("新任务", "另外一个任务", "开始新的", "new task")
        )

    def _is_strong_task_switch(self, intent: WorkflowIntent) -> bool:
        if not self.workflow or intent.task_type == self.workflow.task_type:
            return False
        strong = {
            "diagnose",
            "modify_project",
            "build_rx",
            "build_tx",
            "hardware_configure",
            "observe",
            "protocol",
            "deploy",
        }
        if not set(intent.capabilities or ()) & strong:
            return False
        return intent.confidence >= 0.9 or bool(
            intent.slots.get("modulation") or intent.slots.get("hardware")
        )

    def _merge_turn_slots(self, text: str, shared_state: Any) -> None:
        update = self.classify(text, shared_state)
        updates = {
            key: value
            for key, value in update.slots.items()
            if value not in (None, "", [])
        }
        self.workflow.intent.slots.update(updates)
        self.workflow.intent.slot_sources.update(
            {key: "user" for key in updates}
        )
        for capability in update.capabilities:
            if capability not in self.workflow.intent.capabilities:
                self.workflow.intent.capabilities.append(capability)
        self.workflow.intent.missing_slots = self._missing_slots(
            self.workflow.task_type,
            self.workflow.intent.slots,
            shared_state,
            self.workflow.intent.capabilities,
        )
        self.workflow.intent.validation_errors = self._validate_slots(
            self.workflow.intent.slots
        )

    def classify(self, text: str, shared_state: Any) -> WorkflowIntent:
        low = (text or "").lower()
        has_project = bool(
            getattr(getattr(shared_state, "project", None), "grc_path", "")
            or getattr(getattr(shared_state, "project", None), "config", {}).get("recipe")
        )
        slots = self._parse_slots(text)
        capabilities = self._detect_capabilities(text, slots, has_project)
        if "diagnose" in capabilities:
            task_type = "DIAGNOSE"
        elif "modify_project" in capabilities:
            task_type = "MODIFY_PROJECT"
        elif "build_rx" in capabilities:
            task_type = "RX_BUILD"
        elif "build_tx" in capabilities:
            task_type = "TX_BUILD"
        elif "observe" in capabilities and has_project:
            task_type = "OBSERVE"
        elif "hardware_configure" in capabilities:
            task_type = "HARDWARE_CONFIGURE"
        else:
            task_type = "END_TO_END_SIM"
        project_config = getattr(getattr(shared_state, "project", None), "config", {})
        slot_sources = {key: "user" for key, value in slots.items() if value not in (None, "", [])}
        context = {
            "current_project": {
                "grc_path": str(getattr(getattr(shared_state, "project", None), "grc_path", "") or ""),
                "recipe": str(project_config.get("recipe") or ""),
                "modulation": str(project_config.get("modulation") or ""),
                "channel": str(project_config.get("channel") or ""),
            }
        } if has_project else {}
        # Only inherit parameters that are requirements of a signal-generation
        # task.  Existing project facts remain context for modify/observe/hardware
        # workflows and are never represented as a fresh user decision.
        if task_type in {"END_TO_END_SIM", "TX_BUILD", "RX_BUILD"} and "signal_agnostic_observe" not in capabilities:
            for key in ("modulation", "channel"):
                if not slots.get(key) and project_config.get(key):
                    slots[key] = project_config[key]
                    slot_sources[key] = "current_project"
        if (
            "signal_agnostic_observe" in capabilities
            and slots.get("hardware")
            and slots.get("carrier_frequency")
            and not slots.get("sample_rate")
        ):
            slots["sample_rate"] = 2_000_000.0
            slot_sources["sample_rate"] = "default"
        missing = self._missing_slots(task_type, slots, shared_state, capabilities)
        validation_errors = self._validate_slots(slots)
        intent = WorkflowIntent(
            raw_text=text,
            turn_relation="new_task",
            task_type=task_type,
            confidence=0.95 if any(slots.values()) or task_type != "END_TO_END_SIM" else 0.65,
            slots=slots,
            missing_slots=missing,
            capabilities=capabilities,
            slot_sources=slot_sources,
            context=context,
            validation_errors=validation_errors,
        )
        if intent.confidence < 0.9:
            from .intent_llm import complete_intent

            intent = complete_intent(intent, text, shared_state)
            intent.missing_slots = self._missing_slots(
                intent.task_type, intent.slots, shared_state, intent.capabilities
            )
            intent.validation_errors = self._validate_slots(intent.slots)
        return intent

    @staticmethod
    def _detect_capabilities(
        text: str, slots: Dict[str, Any], has_project: bool
    ) -> list[str]:
        low = (text or "").lower()
        capabilities: list[str] = []

        def add(name: str, condition: bool) -> None:
            if condition and name not in capabilities:
                capabilities.append(name)

        diagnose = any(word in low for word in ("诊断", "排查", "故障", "为什么", "diagnos"))
        modify = any(word in low for word in _MODIFY_HINTS)
        build = any(word in low for word in _BUILD_HINTS)
        rx = slots.get("direction") == "rx"
        tx = slots.get("direction") == "tx"
        hardware = bool(slots.get("hardware")) or any(word in low for word in _HARDWARE_HINTS)
        observe = bool(slots.get("requested_metrics")) or any(word in low for word in _OBSERVE_HINTS)
        realtime = any(word in low for word in ("实时", "live", "real-time", "realtime"))

        add("diagnose", diagnose)
        add("modify_project", modify and (has_project or bool(slots.get("target_project"))))
        add("build_rx", build and rx)
        add("build_tx", build and tx)
        add("build_signal", build and not rx and not tx)
        add("hardware_configure", hardware)
        add("observe", observe)
        add("realtime_observe", observe and realtime)
        add("signal_agnostic_observe", hardware and observe and not any(
            word in low for word in ("解调", "decode", "demod", "判决")
        ))
        add("protocol", bool(slots.get("protocol")))
        add("deploy", slots.get("operation") == "deploy")
        add("hardware_runtime", hardware and (
            realtime
            or slots.get("operation") == "deploy"
            or any(word in low for word in ("启动", "运行", "run", "start"))
        ))
        return capabilities

    @staticmethod
    def _parse_slots(text: str) -> Dict[str, Any]:
        low = (text or "").lower()
        modulation = next((
            name for name in ("ofdm", "qpsk", "bpsk", "gfsk")
            if re.search(
                rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])", low
            )
        ), "")
        channel = "awgn" if (
            re.search(r"(?<![A-Za-z0-9_])awgn(?![A-Za-z0-9_])", low)
            or "高斯" in text or "噪声" in text
        ) else ""
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
        if any(word in low for word in ("直接部署", "部署", "deploy")):
            slots["operation"] = "deploy"
        duration = re.search(
            r"(?:运行|持续|时长|duration)\s*[:=为]?\s*"
            r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec(?:onds?)?)",
            low,
        )
        if duration:
            slots["duration_seconds"] = float(duration.group(1))
        if any(word in low for word in ("实时", "live", "real-time", "realtime")):
            slots["observation_mode"] = "realtime"
        if "ble" in low or "bluetooth low energy" in low or "低功耗蓝牙" in text:
            slots.update(
                {
                    "protocol": "ble",
                    "ble_mode": "advertising",
                    "operation": "deploy" if any(
                        word in low for word in ("发射", "部署", "transmit", "deploy")
                    ) else "configure",
                    "modulation": "gfsk",
                    "advertising_channels": [37, 38, 39],
                    "carrier_frequency": 2_402_000_000.0,
                    "sample_rate": 2_000_000.0,
                    "duration_seconds": slots.get("duration_seconds", 30.0),
                    "tx_gain": 0.0,
                }
            )
        name = re.search(
            r"(?:local\s*name|localname|本地名称|设备名称)\s*(?:为|=|:)?\s*"
            r"([A-Za-z0-9_-]{1,26})",
            text,
            flags=re.IGNORECASE,
        )
        if name:
            slots["local_name"] = name.group(1)
        if any(word in low for word in ("接收机", "receiver", "解调", " rx")):
            slots["direction"] = "rx"
        elif any(word in low for word in ("发射机", "transmitter", "发射链", " tx")):
            slots["direction"] = "tx"
        elif modulation:
            slots["direction"] = "transceiver"
        target_project = re.search(
            r"(?:直接)?(?:修改|打开|基于|modify)\s*([A-Za-z_]\w*)",
            text,
            flags=re.IGNORECASE,
        )
        if target_project:
            slots["target_project"] = target_project.group(1)
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
        if not slots.get("carrier_frequency") and any(
            marker in low for marker in ("硬件", "usrp", "sdr", "信号", "carrier")
        ):
            match = re.search(
                r"(?<![0-9.])\d+(?:\.\d+)?\s*[gmk]?hz(?![A-Za-z0-9_])", low
            )
            if match:
                parsed = re.fullmatch(
                    r"(\d+(?:\.\d+)?)\s*([gmk]?)hz", match.group(0).strip()
                )
                if parsed:
                    slots["carrier_frequency"] = (
                        float(parsed.group(1)) * units[parsed.group(2).lower()]
                    )
        device = next((name for name in ("b210", "usrp", "hackrf", "pluto", "limesdr") if name in low), "")
        if device:
            slots["hardware"] = device
        switch = re.search(
            r"(?:改成|换成|改为|change\s+(?:it\s+)?to|switch\s+(?:it\s+)?to)\s*"
            r"([a-z0-9_]+)",
            low,
        )
        if switch:
            target = switch.group(1)
            mapping = {"bpsk": "bpsk_awgn", "qpsk": "qpsk_awgn", "ofdm": "ofdm_awgn"}
            slots["target_recipe"] = mapping.get(target, target)
            slots["change_type"] = "modulation_change" if target in mapping else "recipe_change"
        elif re.search(r"[A-Za-z_]\w*\.[A-Za-z_]\w*\s*(?:改为|设为|改成|=)", text):
            slots["change_type"] = "single_parameter"
        if device and any(word in low for word in _BUILD_HINTS):
            slots["requires_build"] = True
        success = re.findall(r"(?:evm|ber)\s*(?:小于|低于|<|≤)\s*\d+(?:\.\d+)?\s*%?", low)
        if success:
            slots["success_conditions"] = success
        return slots

    @staticmethod
    def _missing_slots(
        task_type: str,
        slots: Dict[str, Any],
        shared_state: Any,
        capabilities: Optional[list[str]] = None,
    ) -> list[str]:
        missing = []
        capabilities = list(capabilities or [])
        signal_agnostic = "signal_agnostic_observe" in capabilities
        if task_type in {"END_TO_END_SIM", "TX_BUILD", "RX_BUILD"} and not signal_agnostic and not slots.get("modulation"):
            missing.append("modulation")
        if task_type in {"DIAGNOSE", "MODIFY_PROJECT", "OBSERVE"}:
            project = getattr(shared_state, "project", None)
            if not (getattr(project, "grc_path", "") or getattr(project, "config", {}).get("recipe")):
                missing.append("current_project")
        if task_type == "HARDWARE_CONFIGURE" or "hardware_configure" in capabilities:
            for key in ("hardware", "carrier_frequency", "sample_rate"):
                if not slots.get(key):
                    missing.append(key)
            if slots.get("operation") == "deploy" and slots.get("protocol") == "ble":
                if not slots.get("local_name"):
                    missing.append("local_name")
        return missing

    @staticmethod
    def _validate_slots(slots: Dict[str, Any]) -> list[str]:
        errors: list[str] = []
        frequency = slots.get("carrier_frequency")
        if frequency is not None:
            try:
                value = float(frequency)
            except (TypeError, ValueError):
                errors.append("carrier_frequency_invalid")
            else:
                if value <= 0:
                    errors.append("carrier_frequency_invalid")
                # This is a capability guard, not a task classifier. Known
                # radios can add ranges without changing workflow semantics.
                hardware = str(slots.get("hardware") or "").lower()
                ranges = {"b210": (70e6, 6e9)}
                if hardware in ranges:
                    low, high = ranges[hardware]
                    if not low <= value <= high:
                        errors.append("carrier_frequency_out_of_device_range")
        for key in ("sample_rate", "bandwidth", "symbol_rate"):
            if slots.get(key) is not None:
                try:
                    if float(slots[key]) <= 0:
                        errors.append(f"{key}_invalid")
                except (TypeError, ValueError):
                    errors.append(f"{key}_invalid")
        return errors

    def instantiate(self, intent: WorkflowIntent, shared_state: Any) -> Workflow:
        candidate = self.catalog["task_candidates"].get(intent.task_type)
        if not candidate:
            raise ValueError(f"未知 Task Type: {intent.task_type}")
        stages = self._compose_stages(intent, candidate)
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
        self._event("intent_classified", {
            "task_type": intent.task_type,
            "capabilities": intent.capabilities,
            "slots": intent.slots,
            "slot_sources": intent.slot_sources,
            "missing_slots": intent.missing_slots,
            "validation_errors": intent.validation_errors,
        })
        self._event("workflow_created", self.digest())
        self.save()
        return self.workflow

    def _compose_stages(
        self, intent: WorkflowIntent, candidate: Dict[str, Any]
    ) -> list[Stage]:
        """Compose capability groups while preserving each group's failure graph.

        Task candidates remain stable policy/display entry points.  Independent
        capabilities may extend their success path; no capability may replace
        the user's raw text or silently terminate a later group.
        """
        deploying_protocol = (
            intent.slots.get("operation") == "deploy"
            and "protocol" in intent.capabilities
        )
        if deploying_protocol:
            selected = candidate.get("deploy_stages") or []
        elif (
            intent.task_type == "HARDWARE_CONFIGURE"
            and "hardware_runtime" in intent.capabilities
        ):
            selected = candidate.get("runtime_stages") or candidate.get("stages") or []
        else:
            selected = candidate.get("stages") or []
        groups: list[list[Stage]] = []

        def make_group(raw_stages: list[Dict[str, Any]]) -> list[Stage]:
            return [
                Stage.from_dict(raw)
                for raw in raw_stages
                if str(raw.get("id") or "") not in _ALIGNMENT_STAGES
                or bool(intent.missing_slots or intent.validation_errors)
            ]

        base_group = make_group(list(selected))
        if base_group:
            groups.append(base_group)

        if not deploying_protocol:
            capabilities = set(intent.capabilities)
            # A hardware Task may also carry a signal-build capability. Choose
            # the build family from direction, never from a device keyword.
            if intent.task_type == "HARDWARE_CONFIGURE" and capabilities.intersection(
                {"build_rx", "build_tx", "build_signal"}
            ):
                build_type = (
                    "RX_BUILD" if "build_rx" in capabilities
                    else "TX_BUILD" if "build_tx" in capabilities
                    else "END_TO_END_SIM"
                )
                build_group = make_group(list(
                    self.catalog["task_candidates"][build_type].get("stages") or []
                ))
                groups.insert(0, build_group)

            composable = intent.task_type in {
                "END_TO_END_SIM", "TX_BUILD", "RX_BUILD", "MODIFY_PROJECT",
                "HARDWARE_CONFIGURE",
            }
            if (
                "hardware_configure" in capabilities
                and intent.task_type != "HARDWARE_CONFIGURE"
                and composable
            ):
                hardware_candidate = self.catalog["task_candidates"]["HARDWARE_CONFIGURE"]
                hardware_stages = (
                    hardware_candidate.get("runtime_stages")
                    if "hardware_runtime" in capabilities
                    else hardware_candidate.get("stages")
                ) or []
                groups.append(make_group(list(
                    hardware_stages
                )))

        # Missing or invalid execution parameters are a global gate.  Task
        # templates that do not define alignment receive the generic alignment
        # stage without changing their business classification.
        flattened_ids = {stage.id for group in groups for stage in group}
        if (
            (intent.missing_slots or intent.validation_errors)
            and not flattened_ids.intersection(_ALIGNMENT_STAGES)
        ):
            alignment = Stage.from_dict(
                self.catalog["task_candidates"]["END_TO_END_SIM"]["stages"][0]
            )
            alignment.transitions["approved"] = "completed"
            groups.insert(0, [alignment])

        groups = [group for group in groups if group]
        for index, group in enumerate(groups[:-1]):
            next_stage = groups[index + 1][0].id
            for stage in group:
                for outcome, target in list(stage.transitions.items()):
                    if target == "completed":
                        stage.transitions[outcome] = next_stage

        stages = [stage for group in groups for stage in group]
        if len({stage.id for stage in stages}) != len(stages):
            raise ValueError("能力组合产生重复 Stage id")

        # Hardware observation needs structural evidence from the build/change
        # stage.  This is capability-driven and applies to any compatible Task.
        capabilities = set(intent.capabilities)
        if "hardware_configure" in capabilities:
            for stage in stages:
                if stage.id in {"build_and_verify", "tx_build_and_validate", "rx_build_and_verify", "apply_and_verify"}:
                    if "signal_agnostic_observe" in capabilities:
                        stage.completion = [
                            name for name in stage.completion
                            if name not in {"receive_quality_evaluated", "measurement_completed"}
                        ]
                    self._add_completion(stage, "hardware_endpoint_present")
                    self._add_completion(stage, "radio_parameters_match")
                    if "realtime_observe" in capabilities:
                        self._add_completion(stage, "realtime_sink_present")
        return stages

    @staticmethod
    def _add_completion(stage: Stage, name: str) -> None:
        if name not in stage.completion:
            stage.completion.append(name)

    def current_stage(self) -> Optional[Stage]:
        return self.workflow.stage() if self.workflow else None

    def start_stage(self) -> Optional[Stage]:
        stage = self.current_stage()
        if not stage or stage.execution_status == "waiting":
            return stage
        if stage.execution_status not in ("pending", "invalidated"):
            return stage
        if not stage.resume_pending and stage.attempt >= stage.max_attempts:
            stage.execution_status = "waiting"
            self.workflow.execution_status = "waiting"
            self._event("attempt_limit_reached", {
                "stage_id": stage.id,
                "attempt": stage.attempt,
                "max_attempts": stage.max_attempts,
            })
            self.save()
            return stage
        stage.execution_status = "running"
        if stage.resume_pending:
            stage.resume_pending = False
        else:
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
            if stage.execution_status == "running":
                stage.execution_status = "pending"
                self.workflow.execution_status = "pending"
                self.save()
            return False
        completion = dict(data.get("completion") or {})
        missing_completion = [
            name for name in stage.completion if completion.get(name) is not True
        ]
        ok = bool(data.get("ok")) and not missing_completion
        outcome = str(data.get("outcome") or ("passed" if ok else "failed"))
        if missing_completion:
            outcome = "failed"
        stage.result = {
            key: data.get(key)
            for key in (
                "note",
                "artifacts",
                "produced_claims",
                "proposed_changes",
                "completion",
                "invocations",
            )
            if data.get(key)
        }
        fingerprint = _result_fingerprint(stage.result, ok, outcome)
        stage.result["fingerprint"] = fingerprint
        if missing_completion:
            stage.result["missing_completion"] = missing_completion
        if data.get("errored"):
            self._event("stage_errored", {"stage_id": stage.id, "attempt": stage.attempt})
            if stage.attempt < stage.max_attempts:
                self._remember_result(stage, "errored_retry")
                stage.execution_status = "pending"
                stage.outcome = ""
                stage.result = {}
                self.workflow.execution_status = "pending"
                self.save()
                return True
            stage.execution_status = "errored"
            stage.outcome = "inconclusive"
            transition_key = "errored"
        else:
            stage.execution_status = "completed"
            stage.outcome = outcome if outcome in ("passed", "failed", "inconclusive") else ("passed" if ok else "failed")
            improvement = bool(data.get("improvement_available"))
            prior_prints = [
                (item.get("result") or {}).get("fingerprint")
                for item in stage.result_history
            ]
            if fingerprint and fingerprint in prior_prints:
                improvement = False
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

    def wait_for_checkpoint(
        self,
        reason: str,
        *,
        action: str = "",
        payload_ref: str = "",
    ) -> Checkpoint:
        """Pause the current autonomous Stage for a Policy/user decision."""
        stage = self.current_stage()
        if not stage:
            raise ValueError("没有 current_stage")
        checkpoint = Checkpoint(
            id=f"cp-{uuid.uuid4().hex[:8]}",
            reason=reason or _STAGE_LABELS.get(stage.id, stage.id),
            action=action,
            payload_ref=payload_ref,
            resume_stage=True,
        )
        stage.execution_status = "waiting"
        stage.checkpoint = checkpoint
        self.workflow.execution_status = "waiting"
        self._event(
            "checkpoint_opened",
            {"stage_id": stage.id, "reason": checkpoint.reason, "checkpoint_id": checkpoint.id},
        )
        self.save()
        return checkpoint

    def resolve_checkpoint(self, decision: str) -> None:
        stage = self.current_stage()
        if not stage or not stage.checkpoint or stage.execution_status != "waiting":
            raise ValueError("当前没有待决 Checkpoint")
        normalized = "approved" if decision == "approved" else "rejected"
        stage.checkpoint.decision_status = normalized
        if stage.checkpoint.resume_stage and normalized == "approved":
            checkpoint_id = stage.checkpoint.id
            stage.execution_status = "pending"
            stage.outcome = ""
            stage.checkpoint = None
            stage.resume_pending = True
            self.workflow.execution_status = "pending"
            self._event(
                "checkpoint_resolved",
                {"stage_id": stage.id, "decision": normalized, "checkpoint_id": checkpoint_id},
            )
            self.save()
            return
        if stage.checkpoint.resume_stage and normalized == "rejected":
            checkpoint_id = stage.checkpoint.id
            stage.execution_status = "completed"
            stage.outcome = "cancelled"
            self.workflow.execution_status = "completed"
            self.workflow.outcome = "cancelled"
            self._event(
                "checkpoint_resolved",
                {"stage_id": stage.id, "decision": normalized, "checkpoint_id": checkpoint_id},
            )
            self.save()
            return
        stage.execution_status = "completed"
        stage.outcome = "passed" if normalized == "approved" else "cancelled"
        self._event("checkpoint_resolved", {"stage_id": stage.id, "decision": normalized})
        self._transition(stage.transitions.get(normalized, "completed"))
        self.save()

    def invalidate(self, cause: str, project_version: int) -> None:
        if not self.workflow:
            return
        wanted_deps = _CAUSE_DEPENDENCIES.get(cause, {"project.flowgraph"})
        fallback_ids = {
            "build_and_verify",
            "tx_build_and_validate",
            "rx_build_and_verify",
            "inspect_and_diagnose",
            "repair_and_verify",
            "apply_and_verify",
            "inspect_and_measure",
            "hardware_precheck",
            "configure_and_check",
        }
        if cause in ("spec_changed", "architecture_changed", "recipe_changed"):
            fallback_ids = {
                stage.id for stage in self.workflow.stages if "alignment" not in stage.id
            }
        elif cause == "success_conditions_changed":
            fallback_ids = fallback_ids - {"hardware_precheck", "configure_and_check"}
        affected = [
            stage
            for stage in self.workflow.stages
            if stage.execution_status == "completed"
            and (
                (set(stage.depends_on) & wanted_deps)
                if stage.depends_on
                else stage.id in fallback_ids
            )
        ]
        if not affected:
            # Even when no completed Stage needs rerunning, future envelopes must
            # be compared with the current project version.
            if self.workflow.base_project_version != int(project_version):
                self.workflow.revision += 1
                self.workflow.base_project_version = int(project_version)
                self._event(
                    "workflow_rebased",
                    {"cause": cause, "project_version": project_version},
                )
                self.save()
            return
        for stage in affected:
            self._remember_result(stage, cause)
            stage.execution_status = "invalidated"
            stage.outcome = ""
            stage.attempt = 0
            stage.resume_pending = False
            stage.result = {}
        self.workflow.revision += 1
        self.workflow.base_project_version = int(project_version)
        self.workflow.current_stage = affected[0].id
        self.workflow.execution_status = "pending"
        self.workflow.outcome = ""
        self._event("stage_invalidated", {"cause": cause, "stages": [stage.id for stage in affected], "project_version": project_version})
        self.save()

    def _remember_result(self, stage: Stage, cause: str) -> None:
        if not stage.result and not stage.outcome:
            return
        stage.result_history.append(
            {
                "revision": self.workflow.revision,
                "outcome": stage.outcome,
                "cause": cause,
                "result": dict(stage.result),
            }
        )

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
            "checkpoint_id": (
                stage.checkpoint.id if stage and stage.checkpoint else ""
            ),
            "revision": self.workflow.revision,
            "base_project_version": self.workflow.base_project_version,
            "capabilities": list(self.workflow.intent.capabilities),
            "missing_slots": list(self.workflow.intent.missing_slots),
            "validation_errors": list(self.workflow.intent.validation_errors),
            "stages": [
                {
                    "id": item.id,
                    "label": _STAGE_LABELS.get(item.id, item.id),
                    "execution_status": item.execution_status,
                    "outcome": item.outcome,
                    "attempt": item.attempt,
                    "max_attempts": item.max_attempts,
                    "completion": list(item.completion),
                    "completion_result": dict(item.result.get("completion") or {}),
                }
                for item in self.workflow.stages
            ],
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
            if stage.interaction == "conditional_checkpoint" and not self._checkpoint_required(stage):
                stage.execution_status = "completed"
                stage.outcome = "passed"
                self._event("stage_skipped", {"stage_id": stage.id, "reason": "not_required"})
                self._transition(stage.transitions.get("not_required", "completed"))
                return
            reason = self._checkpoint_reason(stage)
            stage.execution_status = "waiting"
            stage.checkpoint = Checkpoint(id=f"cp-{uuid.uuid4().hex[:8]}", reason=reason)
            self.workflow.execution_status = "waiting"
            self._event(
                "checkpoint_opened",
                {"stage_id": stage.id, "reason": reason, "checkpoint_id": stage.checkpoint.id},
            )

    def _checkpoint_required(self, stage: Stage) -> bool:
        if stage.id in _ALIGNMENT_STAGES:
            return bool(
                self.workflow.intent.missing_slots
                or self.workflow.intent.validation_errors
            )
        if stage.id == "change_confirmation":
            return self.workflow.intent.slots.get("change_type") != "single_parameter"
        return True

    def _checkpoint_reason(self, stage: Stage) -> str:
        return ", ".join(
            self.workflow.intent.missing_slots
            + self.workflow.intent.validation_errors
        ) or str(
            self.workflow.intent.slots.get("change_type")
            or _STAGE_LABELS.get(stage.id, stage.id)
        )

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
            self._remember_result(next_stage, "retry")
            next_stage.execution_status = "pending"
            next_stage.outcome = ""
            next_stage.result = {}
        self.workflow.execution_status = "pending"
        self._activate_current()

    def _event(self, event: str, payload: Dict[str, Any]) -> None:
        if self._event_sink:
            data = dict(payload or {})
            if self.workflow:
                stage = self.current_stage()
                data.setdefault("workflow_id", self.workflow.workflow_id)
                data.setdefault("workflow_revision", self.workflow.revision)
                data.setdefault("task_type", self.workflow.task_type)
                data.setdefault("stage_id", self.workflow.current_stage)
                data.setdefault("attempt", stage.attempt if stage else 0)
            self._event_sink(event, data)


def _result_fingerprint(result: Dict[str, Any], ok: bool, outcome: str) -> str:
    payload = {
        "ok": ok,
        "outcome": outcome,
        "completion": result.get("completion") or {},
        "produced_claims": result.get("produced_claims") or [],
        "proposed_changes": result.get("proposed_changes") or [],
        "note": result.get("note") or "",
        "artifacts": result.get("artifacts") or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
