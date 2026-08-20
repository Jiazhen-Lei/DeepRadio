"""model:把 DeepRadio 现有 LLM 配置封装为 LangChain chat model。

``create_deep_agent`` 需要一个 LangChain ``BaseChatModel``。DeepRadio 的 LLM
配置统一来自 ``grc.agent.llm.get_config()``(读 ``GRC_AGENT_*`` 环境变量,
OpenAI 兼容接口)。本模块把它转成 :class:`~langchain_openai.ChatOpenAI`,
**不引入** RadioMaster 的第二套 ``models/loader``——单一事实源仍是 ``llm.py``。

无 LLM 配置或未安装 langchain_openai 时抛出异常,由 orchestrator 决定降级到
确定性骨架(见 ``local/docs/agent_architecture_deepagents.md`` 红线 4)。
"""

from __future__ import annotations

import logging

from .. import llm

logger = logging.getLogger(__name__)


def build_chat_model(temperature: float = 0.2):
    """按 ``llm.get_config()`` 构造一个 LangChain ``ChatOpenAI``。

    Args:
        temperature: 采样温度(编排/建图偏确定,默认 0.2)。

    Returns:
        已配置 base_url / api_key / model 的 ``ChatOpenAI`` 实例。

    Raises:
        llm.LLMNotConfigured: 未配置 ``GRC_AGENT_*``。
        ImportError: 未安装 ``langchain_openai``。
    """
    cfg = llm.get_config()  # 未配置时抛 LLMNotConfigured

    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=cfg["model"],
        base_url=f"{cfg['base_url']}/",
        api_key=cfg["api_key"],
        temperature=temperature,
        timeout=cfg["timeout"],
        max_retries=1,
    )
    logger.info("已构造 ChatOpenAI: model=%s base_url=%s",
                cfg["model"], cfg["base_url"])
    return model


def is_available() -> bool:
    """探测:是否既配置了 LLM 又装了 langchain_openai(不抛异常)。"""
    if not llm.is_configured():
        return False
    try:
        import langchain_openai  # noqa: F401
        return True
    except ImportError:
        return False
