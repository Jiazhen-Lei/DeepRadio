"""LLM 接入层:调用兼容 OpenAI 的自定义 URL 接口, 让模型直接产出 .grc(YAML) 文本。

配置全部来自环境变量(不写死任何 key):

    GRC_AGENT_BASE_URL     接口根地址, 如 https://open.bigmodel.cn/api/paas/v4
                           实际请求 {BASE_URL}/chat/completions
    GRC_AGENT_API_KEY      鉴权 key, 作为 Authorization: Bearer <key>
    GRC_AGENT_MODEL        模型名, 如 glm-4.6
    GRC_AGENT_TIMEOUT      单次请求超时秒数(可选, 默认 120)
    GRC_AGENT_MAX_MESSAGES 送入的历史消息条数上限(可选, 默认 20)

接口约定为 OpenAI Chat Completions 兼容格式:
    POST {BASE_URL}/chat/completions
    body: {"model": ..., "messages": [...], ...}
    resp: {"choices": [{"message": {"content": "..."}}], ...}

只用标准库 urllib, 不引入额外依赖。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class LLMNotConfigured(Exception):
    """未配置 LLM(缺 BASE_URL / API_KEY / MODEL), 调用方应回落到 demo。"""


class LLMError(Exception):
    """LLM 请求或响应异常(网络错误 / HTTP 非 2xx / 响应格式不符)。"""


# ---------------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def get_config() -> dict:
    """从环境变量读取 LLM 配置。

    Raises:
        LLMNotConfigured: 缺少 BASE_URL / API_KEY / MODEL 任一项时。
    """
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
    try:
        max_messages = int(_env("GRC_AGENT_MAX_MESSAGES", "20"))
    except ValueError:
        max_messages = 20

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "timeout": timeout,
        "max_messages": max(1, max_messages),
    }


def is_configured() -> bool:
    """是否已配置好 LLM(不抛异常的探测)。"""
    try:
        get_config()
        return True
    except LLMNotConfigured:
        return False


# ---------------------------------------------------------------------------- #
# 提示词
# ---------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
你是 GNU Radio Companion (GRC) 的流图生成助手。用户会用自然语言描述想要的\
无线波形 / 信号处理流图, 你必须只输出一个合法的 GRC 流图文件内容 (YAML 文本, \
即 .grc 文件), 不要输出任何解释、Markdown 说明或多余文字。

.grc 文件是 YAML, 顶层包含 options / blocks / connections / metadata 四部分。\
严格遵循以下结构与约束:

options:
  parameters:
    id: '<流图id, 小写字母数字下划线>'
    title: '<标题>'
    author: ''
    copyright: ''
    description: ''
    output_language: python
    generate_options: qt_gui
  states:
    coordinate: [8, 8]
    rotation: 0
    state: enabled

blocks:
- name: <块实例名, 唯一>
  id: <块类型key, 如 variable / analog_sig_source_x / blocks_throttle / blocks_null_sink / qtgui_time_sink_x>
  parameters:
    <参数名>: <值>          # 字符串值统一用单引号包裹, 例如 '32000'
  states:
    coordinate: [<x>, <y>]  # 合理排布, 避免重叠, 横向间距>=200
    rotation: 0
    state: enabled

connections:
- [<源块name>, <源端口序号>, <目标块name>, <目标端口序号>]   # 端口序号从 '0' 开始, 用字符串

metadata:
  file_format: 1
  grc_version: 3.8.0

硬性要求:
1. 必须包含一个 id 为 variable、name 为 samp_rate 的采样率变量块。
2. 需要显示波形时使用 qtgui_time_sink_x 或 qtgui_freq_sink_x, 并保持 generate_options: qt_gui。
3. 只使用 GNU Radio 标准发行版中真实存在的块 key 与参数名, 不要臆造。
4. 每个块的 type 参数(复数/浮点等)要在整条链路上保持一致, 否则连接会失败。
5. 采样率、频率等引用 samp_rate 的地方, 直接写 'samp_rate' 字符串。
6. 输出必须是可被 yaml.safe_load 解析的纯 YAML, 不要用 ```yaml 代码块包裹。
"""


def build_messages(user_text: str, history=None, max_messages: int = 20):
    """组装 chat messages: system + (裁剪后的历史) + 当前用户输入。

    Args:
        user_text: 本次用户自然语言需求。
        history: 可选, [(role, content), ...] 形式的历史对话。
        max_messages: 送入模型的历史消息条数上限(不含 system 与本次输入)。
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        trimmed = history[-max_messages:]
        for role, content in trimmed:
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


# ---------------------------------------------------------------------------- #
# 请求
# ---------------------------------------------------------------------------- #
def chat(messages, config: dict = None) -> str:
    """调用 OpenAI 兼容的 chat/completions 接口, 返回 content 文本。

    Raises:
        LLMNotConfigured: 未配置。
        LLMError: 网络 / HTTP / 响应格式异常。
    """
    cfg = config or get_config()

    url = f"{cfg['base_url']}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
    )

    logger.info("LLM 请求: %s model=%s messages=%d",
                url, cfg["model"], len(messages))
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise LLMError(f"HTTP {e.code}: {body[:500]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"网络错误: {e.reason}") from e

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"响应不是合法 JSON: {raw[:300]}") from e

    try:
        content = obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"响应缺少 choices[0].message.content: {raw[:300]}") from e

    if not content or not content.strip():
        raise LLMError("LLM 返回空内容")

    return content


# ---------------------------------------------------------------------------- #
# 后处理: 提取纯 .grc 文本
# ---------------------------------------------------------------------------- #
_FENCE_RE = re.compile(
    r"```(?:ya?ml|grc)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def extract_grc_text(content: str) -> str:
    """从模型输出里提取纯 .grc(YAML) 文本。

    尽管 system prompt 要求不要用代码块包裹, 但模型有时仍会加 ```yaml 围栏,
    这里做一次容错剥离。若没有围栏则原样返回(去掉首尾空白)。
    """
    m = _FENCE_RE.search(content)
    if m:
        return m.group(1).strip()
    return content.strip()


def generate_grc_text(user_text: str, history=None, config: dict = None) -> str:
    """高层入口: 自然语言 -> LLM -> 纯 .grc(YAML) 文本。

    Raises:
        LLMNotConfigured / LLMError。
    """
    cfg = config or get_config()
    messages = build_messages(user_text, history, cfg["max_messages"])
    content = chat(messages, cfg)
    return extract_grc_text(content)
