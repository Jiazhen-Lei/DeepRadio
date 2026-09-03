"""MainAgent 的 LLM 环境配置。

配置全部来自环境变量(不写死任何 key):

    GRC_AGENT_BASE_URL     接口根地址, 如 https://open.bigmodel.cn/api/paas/v4
                           实际请求 {BASE_URL}/chat/completions
    GRC_AGENT_API_KEY      鉴权 key, 作为 Authorization: Bearer <key>
    GRC_AGENT_MODEL        模型名, 如 glm-4.6
    GRC_AGENT_TIMEOUT      单次请求超时秒数(可选, 默认 120)
    GRC_AGENT_THINKING     思考模式: disabled|enabled|auto (可选, 默认 disabled)。
                           GLM-5.x 默认深度思考,常规建图轮次通常不需要;
                           实测关闭后单轮延迟约降 45%。疑难诊断可设 enabled。
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- #
# .env 加载 (纯标准库, 不引入额外依赖)
# ---------------------------------------------------------------------------- #
def _load_env_file(path: str | None = None) -> None:
    """从 .env 文件把变量注入 os.environ。

    查找顺序: 显式传入的 path -> 当前工作目录 ./env -> 本文件所在的
    grc/agent 向上三级(即项目根目录)的 .env。

    覆盖策略: 仅当 shell 中该变量**缺失或为空串**时, 才用 .env 的值填充;
    shell 中已存在的**非空**值优先级更高。这样即便 shell 里残留了
    ``export GRC_AGENT_API_KEY=`` 之类的空导出, .env 里的真实 key 仍能生效,
    避免误判为"未配置"而降级到 demo。

    幂等, 可重复调用。
    """
    candidates = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(os.getcwd(), ".env"))
    # 本项目根目录: grc/agent/llm.py -> 上三级
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    candidates.append(os.path.join(root, ".env"))

    target = next((c for c in candidates if os.path.isfile(c)), None)
    if target is None:
        return

    try:
        with open(target, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as e:
        logger.warning("读取 .env 失败: %s", e)
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        # 去掉成对引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        # 仅当 shell 中缺失或为空时才填充, 非空 shell 值优先 (避免空导出压过真值)。
        if not os.environ.get(key):
            os.environ[key] = val

    logger.info("已从 %s 载入 .env 配置", target)


class LLMNotConfigured(Exception):
    """未配置 LLM(缺 BASE_URL / API_KEY / MODEL), 调用方应回落到 demo。"""


# ---------------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def get_config() -> dict:
    """从环境变量读取 LLM 配置。

    调用前会先尝试从项目根目录的 .env 载入(不覆盖已 export 的变量)。

    Raises:
        LLMNotConfigured: 缺少 BASE_URL / API_KEY / MODEL 任一项时。
    """
    _load_env_file()

    base_url = _env("GRC_AGENT_BASE_URL").rstrip("/")
    api_key = _env("GRC_AGENT_API_KEY")
    model = _env("GRC_AGENT_MODEL")

    missing = [
        n for n, v in (
            ("GRC_AGENT_BASE_URL", base_url),
            ("GRC_AGENT_API_KEY", api_key),
            ("GRC_AGENT_MODEL", model),
        ) if not v
    ]
    if missing:
        raise LLMNotConfigured(
            "缺少环境变量: " + ", ".join(missing) +
            " (请设置后重启 GRC)"
        )

    try:
        timeout = float(_env("GRC_AGENT_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    thinking = _env("GRC_AGENT_THINKING", "disabled").lower()
    if thinking not in ("disabled", "enabled", "auto", "low", "high", "max"):
        thinking = "disabled"

    try:
        max_output_tokens = max(0, int(
            _env("GRC_AGENT_MAX_OUTPUT_TOKENS", "1200")
        ))
    except ValueError:
        max_output_tokens = 1200

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "timeout": timeout,
        "thinking": thinking,
        "max_output_tokens": max_output_tokens,
    }


def _unittest_runner_active() -> bool:
    """True for ``python -m unittest`` / pytest, not the GRC GUI."""
    if os.environ.get("GRC_AGENT_FORCE_INTENT_LLM", "").strip().lower() in {
        "1", "true", "yes",
    }:
        return False
    if os.environ.get("GRC_AGENT_NO_INTENT_LLM", "").strip().lower() in {
        "1", "true", "yes",
    }:
        return True
    argv0 = os.path.basename(sys.argv[0] if sys.argv else "")
    joined = " ".join(sys.argv)
    return (
        "unittest" in argv0
        or "pytest" in argv0
        or "unittest" in joined
        or "pytest" in joined
    )


def is_configured() -> bool:
    """是否已配置好 LLM(不抛异常的探测)。

    自动回归（``python -m unittest``）默认不打线上 Intent LLM，避免本机
    ``.env`` 里的 key 把 Gate 1 变成不稳定的网络调用。GUI 不受影响。
    """
    if _unittest_runner_active():
        return False
    try:
        get_config()
        return True
    except LLMNotConfigured:
        return False
