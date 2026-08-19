"""GRC Agent —— 自动流图设计能力。

本包在 GRC 之上增加一层"意图 -> 流图"的能力，不修改 grc/core。
子模块规划:
    env        环境引导(混搭 conda 运行时时的桥接)
    llm        LLM 后端(function-calling / 文本, 配置来自 GRC_AGENT_*)
    schema     GUI 契约(AgentReply / ToolInvocation, service 层回填给 GUI 渲染)
    tools      动词壳: 原子工具层(可被 LLM function-calling 调度) +
               design_link / debug_by_metric / narrate 领域动作
    skills     喂给 deepagents 的 SKILL markdown 目录(渐进式披露)
    knowledge  名词料: 领域知识层(通信任务配方库 recipes)
    runtime    名词料: 无头仿真执行体(simulate)
    memory     名词料: 用户画像(创新 B, profile)
    service    ★ deepagents 装配层(create_deep_agent: 主 Agent + subagents + SKILL)

对外高层入口:
    UserProfile    三档用户画像(创新 B 数据核心, 见 grc.agent.memory)
    design_link / debug_by_metric   领域动作(见 grc.agent.tools)
    ServiceAgent   主路径编排器(见 grc.agent.service);
                   step(text) 返回 AgentReply, GUI 侧渲染逻辑零改动。
    build_flow_graph_from_text(text, platform=None, out_dir=None) -> str(.grc 路径)
        供 GUI(旧版 GTK 的 AgentPanel)调用: 文本意图 -> 建图 -> 存 .grc。
        (保留为兼容旧链路的薄包装, 内部仍走一句话直出 YAML, 作为论文 baseline。)
"""

from __future__ import annotations

import logging
import os
import tempfile

__all__ = [
    "env", "llm", "UserProfile",
    "design_link", "debug_by_metric",
    "build_flow_graph_from_text",
    "ServiceAgent", "build_service_agent",
]

logger = logging.getLogger(__name__)

#: 顶层惰性入口名 -> (子模块, 属性名)。避免无 gnuradio 时过早导入依赖链。
_LAZY = {
    "UserProfile": ("memory", "UserProfile"),
    "design_link": ("tools.design_link", "design_link"),
    "debug_by_metric": ("tools.debug_by_metric", "debug_by_metric"),
    "ServiceAgent": ("service", "ServiceAgent"),
    "build_service_agent": ("service", "build_service_agent"),
}


def __getattr__(name):
    """惰性暴露高层入口, 避免在无 gnuradio 运行时的场景下过早导入。"""
    target = _LAZY.get(name)
    if target:
        mod = __import__(f"{__name__}.{target[0]}", fromlist=[target[1]])
        return getattr(mod, target[1])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def build_flow_graph_from_text(text, platform=None, out_dir=None, history=None):
    """意图文本 -> 建图 -> 存 .grc, 返回生成的 .grc 文件路径。

    优先调用 LLM(见 ``grc.agent.llm``, 配置来自 GRC_AGENT_* 环境变量)让模型
    直接产出 .grc(YAML) 文本, 经校验后保存; 若未配置 LLM 则回落到内置 demo,
    保证 "输入 -> 生成 .grc -> 载入画布" 的主链路始终可跑通。

    Args:
        text: 用户自然语言需求, 如 "生成 QPSK 的 BLE 波形, 信息 xxx"。
        platform: GUI 现成的 Platform(复用其块库)。为 None 时才 fallback
            到 ``env.make_platform()`` 自建一套(较慢, 且仅用于脱离 GUI 的测试)。
        out_dir: 输出目录。为 None 时用临时目录。
        history: 可选的历史对话 [(role, content), ...], 供多轮上下文。

    Returns:
        生成的 .grc 文件绝对路径。

    Raises:
        任何建图/存盘异常都会向上抛出, 由调用方(AgentPanel)捕获并回显。
    """
    from . import env

    if platform is None:
        logger.info("未传入 platform, fallback 到 env.make_platform()")
        platform = env.make_platform()

    # 意图解析 + 建图: 优先 LLM 直出 .grc 文本; 未配置/失败时回落内置 demo。
    fg = _plan_and_build(text, platform, env, history)

    fg.rewrite()
    fg.validate()
    if not fg.is_valid():
        errors = "; ".join(
            m.strip().splitlines()[-1].strip()
            for m in fg.get_error_messages()
        )
        raise RuntimeError(f"生成的流图未通过校验: {errors}")

    flowgraph_id = fg.get_option("id") or "agent_generated"
    out_dir = out_dir or tempfile.mkdtemp(prefix="agent_grc_")
    grc_path = os.path.join(out_dir, f"{flowgraph_id}.grc")
    platform.save_flow_graph(grc_path, fg)
    logger.info("已生成流图: %s", grc_path)
    return grc_path


def _plan_and_build(text, platform, env, history=None):
    """把文本意图变成 FlowGraph。

    顺序:
        1. 若配置了 LLM(GRC_AGENT_* 环境变量齐全), 让模型直接产出 .grc 文本,
           解析并 import 成 FlowGraph。任一步失败都会抛出可读错误。
        2. 未配置 LLM 时, 回落到内置最小 demo 流图。
    """
    from . import llm

    if llm.is_configured():
        logger.info("已配置 LLM, 走模型生成 .grc 文本")
        grc_text = llm.generate_grc_text(text, history=history)
        return _flow_graph_from_grc_text(grc_text, platform)

    logger.warning("未配置 LLM(GRC_AGENT_*), 使用内置 demo 流图 (未真正解析意图)")
    return _demo_flow_graph(text, platform, env)


def _flow_graph_from_grc_text(grc_text, platform):
    """把 LLM 产出的 .grc(YAML) 文本解析成 FlowGraph。

    先做 YAML 解析与基本结构检查, 再 import_data 做真正的语义装配;
    合法性由外层的 fg.rewrite()/validate()/is_valid() 统一判定。
    (注: grc.core 的 schema_checker 对 states 里的 coordinate/rotation
    并不接受 list/int, 官方 parse_flow_graph 也忽略其返回值, 故此处不把
    schema 校验当作硬性失败判据。)
    任一步失败抛出 RuntimeError, 错误信息会回显到 Agent 面板。
    """
    from grc.core.io import yaml

    try:
        data = yaml.safe_load(grc_text)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"LLM 生成的内容不是合法 YAML: {e}") from e

    if not isinstance(data, dict) or "options" not in data:
        raise RuntimeError(
            "LLM 生成的内容不是有效的 GRC 流图(缺少 options 顶层字段)")

    data.setdefault("metadata", {}).setdefault("file_format", 1)

    fg = platform.make_flow_graph()
    try:
        fg.import_data(data)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"装配 LLM 生成的流图失败: {e}") from e
    return fg


def _demo_flow_graph(text, platform, env):
    """内置最小可运行流图: 信号源 -> 节流 -> 空 sink。

    仅用于验证 GUI 主链路(输入->生成->载入画布), 不代表真实意图解析。
    使用 qt_gui 生成方式, 便于生成的 .grc 能在 GUI 中正常打开显示。
    """
    fg = platform.make_flow_graph()
    env.configure_options(fg, "python", "qt_gui",
                          flowgraph_id="agent_demo")

    def nb(key, bid, **kw):
        block = fg.new_block(key)
        if block is None:
            raise RuntimeError(f"块不存在: {key}")
        block.params["id"].set_value(bid)
        for name, value in kw.items():
            if name in block.params:
                block.params[name].set_value(value)
        return block

    nb("variable", "samp_rate", value="32000")
    src = nb("analog_sig_source_x", "src", type="complex",
             samp_rate="samp_rate", frequency="1000", amplitude="1")
    throttle = nb("blocks_throttle", "throttle", type="complex",
                  samp_rate="samp_rate")
    sink = nb("blocks_null_sink", "sink", type="complex")

    for i, block in enumerate(fg.blocks):
        block.states["coordinate"] = (120 + (i % 4) * 230, 140 + (i // 4) * 170)

    fg.rewrite()
    fg.connect(src.sources[0], throttle.sinks[0])
    fg.connect(throttle.sources[0], sink.sinks[0])
    return fg
