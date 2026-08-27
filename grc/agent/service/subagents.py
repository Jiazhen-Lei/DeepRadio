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
        "专职子代理，不与用户对话。产物只写 /session/work/<你的域>/，禁止写 /session/final/。\n"
        "按需读 SKILL.md。向主 Agent 回报：做了什么、artifacts 路径、风险。\n"
        "artifacts 在宿主机上，GUI 会展示；不要用 read_file / ls / glob 确认是否存在。\n"
    )


def build_orchestrator_prompt(subagent_names: Iterable[str],
                              style_prompt: str = "") -> str:
    names = ", ".join(subagent_names)
    style_section = f"\n【STYLE】{style_prompt}\n" if style_prompt else ""
    return (
        "你是 DeepRadio 主编排：只做路由、分派 TaskCard、汇总冲突、面向用户交付。"
        "领域工作委派给子代理，工程变更只能走已绑定工具。\n"
        f"可委派：{names}\n"
        "每次委派传完整 TaskCard JSON，不得改版本字段；按当前 Stage 的 "
        "recommended_agents 选能完成 completion 的最小集合。"
        "遇 DENY / CONFIRM、失败 Claim 或待确认项则停止并告知用户。\n"
        "停止并直接答复：design_flowgraph 已 ok+valid 且指标满足；"
        "或连续两次工具无新信息。同一目标不要重复 design_flowgraph / run_simulation"
        "（simulate=True 已含仿真和绘图）。artifacts 在宿主机，不要用文件工具确认。\n"
        "遵守 raw_text、IntentIR goals/constraints/stop_conditions、capabilities、"
        "forbidden_capabilities、slot_sources、completion；"
        "forbidden_capabilities 禁止调度对应硬件/部署工具。"
        "Task Type 仅是兼容/评测标签，不得用标签改写目标；"
        "按产物、证据和下一决策边界组织最短可执行计划。"
        "context 是背景，不得把硬件或实时观测改写成离线仿真。"
        "配方必须覆盖全部 capabilities，否则按块构建或报缺口，禁止近义顶替。\n"
        "换调制用 design_flowgraph 等确认，禁止 apply_grc_diff 改星座；"
        "用户说「确认」后运行时会重建，不必再选型。"
        "「只诊断 / 先不要改」禁止 design_flowgraph、apply_grc_diff。\n"
        + style_section
    )


def _domain_prompt(role: str, skill: str, duties: str) -> str:
    return (
        build_common_constraints()
        + f"角色：{role}。SKILL：{skill}。\n{duties}\n"
        "输入 TaskCard；返回 ResultEnvelope JSON"
        "（保留版本字段；outcome=passed|failed|inconclusive；"
        "completion 为 expected_results→bool）。不得绕过工具或 PolicyGateway。\n"
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
            "build_sdr_tx_flowgraph",
        ],
    ),
]


def build_grc_subagents(
    ctx: ToolContext,
    allowed_agents: List[str] | None = None,
    allowed_tools: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """按共享 ctx 生成 deepagents ``SubAgent`` 列表。

    Args:
        ctx: 共享运行上下文(携带 platform / out_dir),用于绑定业务工具。

    Returns:
        SubAgent(TypedDict) 列表,可直接传给 ``create_deep_agent``。
    """
    subagents: List[Dict[str, Any]] = []
    seen = set()
    allowed = set(allowed_agents or ())
    for name, desc, prompt_builder, skills, tool_names in _SUBAGENT_DEFS:
        if allowed and name not in allowed:
            continue
        if name in seen:
            raise ValueError(f"subagent 名称重复: {name}")
        seen.add(name)

        sub: Dict[str, Any] = {
            "name": name,
            "description": desc,
            "system_prompt": prompt_builder(),
            "skills": [f"/workspace/skills/{skill}/" for skill in skills],
        }
        stage_tools = set(allowed_tools or ())
        names = [name for name in tool_names if not stage_tools or name in stage_tools]
        bound = tools_lc.build_grc_tools(ctx, allowed=names) if names else []
        if bound:
            sub["tools"] = bound
        subagents.append(sub)

    return subagents


def subagent_names() -> List[str]:
    """返回已注册 subagent 名称。"""
    return [d[0] for d in _SUBAGENT_DEFS]


def tool_names_for_agents(agent_names: List[str]) -> List[str]:
    wanted = set(agent_names or [])
    return sorted({
        tool_name
        for name, _desc, _prompt, _skills, tool_names in _SUBAGENT_DEFS
        if not wanted or name in wanted
        for tool_name in tool_names
    })
