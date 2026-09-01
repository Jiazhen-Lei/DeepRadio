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
    return (
        "你是 DeepRadio 的唯一用户接口和 Workflow 负责人。"
        "读取 grc-orchestration Skill，理解用户目标并维护最短 Workflow。"
        "领域任务必须通过 task 委派给 SubAgent，不直接调用领域工具。"
        "根据宿主验证的 Evidence 决定继续、重试、重新编排或结束。"
        "缺少信息或涉及物理 RF 执行时，用 request_user_decision 询问用户。"
        "最终使用用户当前语言简洁回复，不输出内部字段、JSON 或工具日志。"
        f"可委派：{names}。\n"
        + style_section
    )


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
        "只记录 TaskCard 已有事实；不足则 open_questions，不把假设写成用户决定。",
    )


def build_radio_design_prompt() -> str:
    return _domain_prompt(
        "RadioDesignAgent",
        "grc-block-rag",
        "只选型、不改图。recipe 必须覆盖全部 capabilities，否则报缺口，禁止近义顶替。",
    )


def build_flowgraph_prompt() -> str:
    return _domain_prompt(
        "FlowgraphAgent",
        "grc-build",
        "优先 design_flowgraph。换调制禁止 apply_grc_diff 改星座点；"
        "改参后必须重仿真并 verify_claims。",
    )


def build_verification_prompt() -> str:
    return _domain_prompt(
        "VerificationAgent",
        "grc-critic, grc-sim",
        "先校验再仿真；失败则 explain_error。结论绑定 Evidence。"
        "byte sink 只读 BER，勿对 uint8 算 EVM。",
    )


def build_diagnosis_prompt() -> str:
    return _domain_prompt(
        "DiagnosisAgent",
        "grc-diagnosis",
        "根据指标给最小可恢复的修复建议，不直接改图。",
    )


def build_hardware_prompt() -> str:
    return _domain_prompt(
        "HardwareAgent",
        "grc-hardware",
        "configure / discover / probe 只读。RF 默认关闭；"
        "仅确认且 feature flag 开启后可有限时长启动，必须 stop。"
        "建图不等于已发射。",
    )


def build_protocol_prompt() -> str:
    return _domain_prompt(
        "ProtocolAgent",
        "grc-ble-advertising, grc-build, grc-critic",
        "PDU / CRC / 白化 / GFSK 只用确定性工具；禁止口头声称协议或空口通过。",
    )


#: subagent 名 -> (描述, prompt 构造器, SKILL 列表, 工具名子集)
_SUBAGENT_DEFS = [
    (
        "spec_agent",
        "维护可追溯 RadioSpec 与成功条件。",
        build_spec_prompt,
        ["grc-spec"],
        ["spec_clarify", "spec_commit"],
    ),
    (
        "radio_design_agent",
        "检索块知识并选择确定性通信配方。",
        build_radio_design_prompt,
        ["grc-block-rag"],
        ["select_recipe", "search_blocks", "describe_block", "list_examples"],
    ),
    (
        "flowgraph_agent",
        "构建或增量修改 GNU Radio 流图。",
        build_flowgraph_prompt,
        ["grc-build", "grc-block-rag"],
        ["design_flowgraph", "inspect_flowgraph", "apply_grc_diff", "apply_flowgraph_patch", "build_sdr_tx_flowgraph"],
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
        ["debug_by_metric", "explain_error"],
    ),
    (
        "protocol_agent",
        "构建并离线验证 BLE Advertising PDU、PHY 波形和 TX 流图（B210 或 PlutoSDR）。",
        build_protocol_prompt,
        ["grc-ble-advertising", "grc-build", "grc-critic"],
        [
            "build_ble_advertising_pdu",
            "generate_ble_1m_waveform",
            "verify_ble_packet_bits",
            "build_ble_uhd_tx_flowgraph",
            "build_ble_pluto_tx_flowgraph",
            "validate_flowgraph",
        ],
    ),
    (
        "hardware_agent",
        "管理 SDR 配置、只读发现/探测，以及受控有限时长 RF。",
        build_hardware_prompt,
        ["grc-hardware", "grc-build"],
        [
            "hardware_preflight", "configure_sdr",
            "discover_devices", "probe_device", "start_flowgraph",
            "query_runtime_status", "stop_flowgraph", "emergency_stop",
            "arm_hardware_flowgraph",
            "inspect_flowgraph", "build_usrp_rx_spectrum_flowgraph",
            "build_sdr_rx_spectrum_flowgraph",
            "build_sdr_tx_flowgraph",
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
