"""Assemble DeepRadio domain subagents.

The main agent delegates TaskCards through deepagents' built-in task tool.
Each domain agent receives a system prompt, a bound LangChain tool subset,
and SKILL paths for progressive disclosure.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from ..tools.registry import ToolContext
from . import tools_lc

logger = logging.getLogger(__name__)


def build_common_constraints() -> str:
    return (
        "你是领域执行 Agent，不与用户交互。"
        "执行 TaskCard 并保留其中的 Workflow、Stage 和工程版本，遵循指定 Skill，调用已绑定工具。"
        "返回简短的结构化结果和 Evidence，不修改 Workflow，不扩大任务范围。"
        "宿主机 artifact 由 GUI 展示，不用文件工具重复确认。\n"
    )


def build_orchestrator_prompt(subagent_names: Iterable[str],
                              style_prompt: str = "") -> str:
    names = ", ".join(subagent_names)
    style_section = f"\n【STYLE】{style_prompt}\n" if style_prompt else ""
    prompt = """你是 DeepRadio 的 MainAgent，也是系统中唯一与用户交互的 Agent。

DeepRadio 由 MainAgent、动态 Workflow 和多个 SubAgent 组成。你负责理解用户意图、维护 Workflow、委派当前 Stage，并向用户反馈结果。

SubAgent 只执行收到的 Task，返回结果、Artifact、Measurement 和 Evidence。SubAgent 不与用户交互，不规划或修改 Workflow，也不扩大任务范围。

处理涉及 Workflow 的请求时，必须读取并遵循 grc-orchestration Skill 及其引用的 Stage 候选库。Workflow 的规划、执行、推进和调整均以该 Skill 为准。

不要直接执行 SubAgent 负责的领域任务。

使用用户当前使用的语言回复。保持简洁明确，不展示内部 JSON、TaskCard、工具日志或状态字段。""".strip()
    return prompt + f"\n\n可委派：{names}。\n" + style_section


def _domain_prompt(role: str, skill: str, duties: str) -> str:
    return (
        build_common_constraints()
        + f"角色：{role}。SKILL：{skill}。\n{duties}\n"
        "输入 TaskCard；返回包含 outcome、artifacts 和 evidence 的 JSON。\n"
    )


def build_spec_prompt() -> str:
    return _domain_prompt(
        "SpecAgent",
        "grc-spec",
        "你是 Radio Specification 的唯一维护者。根据 TaskCard、当前 Specification "
        "和 Skill reference 判断 Required 与 Added，调用 spec_update 保存变更。"
        "将未对齐字段返回 MainAgent；只有全部 Required 已对齐时才调用 spec_commit。",
    )


def build_radio_design_prompt() -> str:
    return _domain_prompt(
        "RadioDesignAgent",
        "grc-block-rag, grc-ble-advertising",
        "根据 Radio Specification 生成发送数据和基带波形。"
        "只使用与协议匹配的确定性工具；不构建、修改或运行 Flowgraph。"
        "当前能力无法覆盖协议时，明确返回能力缺口。",
    )


def build_flowgraph_prompt() -> str:
    return _domain_prompt(
        "FlowgraphAgent",
        "grc-build",
        "根据 TaskCard 创建或修改 Flowgraph，并返回 .grc 产物。"
        "File Source 必须使用 TaskCard 中已存在的宿主机文件路径，不得猜测或改写路径。"
        "只负责 Build，不执行 Verification 或 Simulation。",
    )


def build_verification_prompt() -> str:
    return _domain_prompt(
        "VerificationAgent",
        "grc-critic, grc-sim",
        "根据 stage_id 执行对应任务：flowgraph_verification 只校验，"
        "simulation_and_measurement 只仿真并读取所需指标。"
        "每次委派只执行一次对应任务并立即返回；失败或缺少输入时不得自行重试或提问。"
        "不修改 Flowgraph，结论必须绑定 Evidence。",
    )


def build_diagnosis_prompt() -> str:
    return _domain_prompt(
        "DiagnosisAgent",
        "grc-diagnosis",
        "根据已有 Evidence 诊断原因并输出报告。可以提出修改建议，"
        "但不修改 Flowgraph，也不重新验证。",
    )


def build_hardware_prompt() -> str:
    return _domain_prompt(
        "HardwareAgent",
        "grc-hardware",
        "根据 stage_id 执行对应任务：hardware_preparation 只配置、发现、"
        "探测和准备 Flowgraph，不启动 RF；physical_rf_execution 只在"
        " MainAgent 已记录本次确认后启动有限时长运行，并记录停止结果。",
    )


#: subagent 名 -> (描述, prompt 构造器, SKILL 列表, 工具名子集)
_SUBAGENT_DEFS = [
    (
        "spec_agent",
        "维护可追溯 RadioSpec 与成功条件。",
        build_spec_prompt,
        ["grc-spec"],
        ["spec_update", "spec_commit"],
    ),
    (
        "radio_design_agent",
        "根据 Radio Specification 生成发送数据和基带波形。",
        build_radio_design_prompt,
        ["grc-block-rag", "grc-ble-advertising"],
        [
            "select_recipe", "search_blocks", "describe_block", "list_examples",
            "build_ble_advertising_pdu", "generate_ble_1m_waveform",
            "verify_ble_packet_bits",
        ],
    ),
    (
        "flowgraph_agent",
        "构建或增量修改 GNU Radio 流图。",
        build_flowgraph_prompt,
        ["grc-build", "grc-block-rag"],
        [
            "select_recipe", "search_blocks", "describe_block", "list_examples",
            "init_flow_graph", "add_block", "set_param", "connect", "render_grc",
            "inspect_flowgraph", "apply_grc_diff", "apply_flowgraph_patch",
            "build_sdr_tx_flowgraph", "build_ble_uhd_tx_flowgraph",
            "build_ble_pluto_tx_flowgraph",
        ],
    ),
    (
        "verification_agent",
        "校验、仿真、绘图并将证据绑定到 Claim。",
        build_verification_prompt,
        ["grc-critic", "grc-sim"],
        [
            "validate_flowgraph",
            "run_simulation",
            "read_metric",
            "plot_spectrum",
            "plot_constellation",
            "plot_eye",
            "explain_error",
            "verify_claims",
        ],
    ),
    (
        "diagnosis_agent",
        "根据指标诊断并提出最小修复。",
        build_diagnosis_prompt,
        ["grc-diagnosis", "grc-critic"],
        ["debug_by_metric", "explain_error", "run_diagnosis_checks"],
    ),
    (
        "hardware_agent",
        "准备 SDR 硬件，并执行用户已确认的有限时长 RF 任务。",
        build_hardware_prompt,
        ["grc-hardware"],
        [
            "hardware_preflight", "configure_sdr",
            "discover_devices", "probe_device", "start_flowgraph",
            "query_runtime_status", "stop_flowgraph", "emergency_stop",
            "arm_hardware_flowgraph",
        ],
    ),
]


def build_grc_subagents(
    ctx: ToolContext,
) -> List[Dict[str, Any]]:
    """按共享 ctx 生成 deepagents ``SubAgent`` 列表。

    Args:
        ctx: 共享运行上下文(携带 platform / out_dir),用于绑定业务工具。

    Returns:
        SubAgent(TypedDict) 列表,可直接传给 ``create_deep_agent``。
    """
    subagents: List[Dict[str, Any]] = []
    seen = set()
    for name, desc, prompt_builder, skills, tool_names in _SUBAGENT_DEFS:
        if name in seen:
            raise ValueError(f"subagent 名称重复: {name}")
        seen.add(name)

        sub: Dict[str, Any] = {
            "name": name,
            "description": desc,
            "system_prompt": prompt_builder(),
            "skills": [f"/workspace/skills/{skill}/" for skill in skills],
        }
        bound = tools_lc.build_grc_tools(ctx, allowed=tool_names) if tool_names else []
        if bound:
            sub["tools"] = bound
        subagents.append(sub)

    return subagents


def subagent_names() -> List[str]:
    """返回已注册 subagent 名称。"""
    return [d[0] for d in _SUBAGENT_DEFS]
