"""Agent:ReAct 主循环,受 Planner 分层状态约束地调度 tools。

调度策略(架构文档第 6 节):
* **function-calling 为主**:若 LLM 已配置且接口支持,走 OpenAI tools 协议,
  让模型在"当前阶段允许的工具分组"内选工具、填参数。
* **ReAct 文本兜底**:否则用 Thought/Action/Action Input/Observation 文本协议,
  正则解析模型输出;若连 LLM 都没配,退化为**确定性骨架**——
  用 Planner 的启发式推进阶段、执行默认动作,保证无网络也能演示闭环。

Agent 不含领域逻辑,领域逻辑在 tools/skills;它只负责编排与协商节奏。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .. import env, llm
from ..tools import registry
from .context import AgentContext
from .planner import Planner, Stage
from .schema import AgentReply, ToolInvocation

logger = logging.getLogger(__name__)

#: ReAct 文本协议里解析 Action / Action Input 的正则
_ACTION_RE = re.compile(r"Action\s*:\s*(\w+)", re.IGNORECASE)
_ACTION_INPUT_RE = re.compile(
    r"Action\s*Input\s*:\s*(\{.*?\})", re.IGNORECASE | re.DOTALL)
_FINAL_RE = re.compile(r"Final\s*Answer\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)


class Agent:
    """DeepRadio-Agent 主控。

    典型用法::

        from grc.agent.core import Agent
        agent = Agent(platform=my_platform)     # 复用 GUI 的 platform
        reply = agent.step("用 BPSK 过 AWGN 看星座图")
        print(reply.text, reply.stage, reply.needs_confirmation)
    """

    def __init__(self, platform=None, ctx: Optional[AgentContext] = None,
                 max_tool_steps: int = 8):
        self.ctx = ctx or AgentContext()
        if platform is not None:
            self.ctx.tool_ctx.platform = platform
        self.planner = Planner()
        self.max_tool_steps = max_tool_steps
        registry.load_all()

    # -- 惰性初始化 platform(脱离 GUI 时) ---------------------------------
    def _ensure_platform(self):
        if self.ctx.tool_ctx.platform is None:
            logger.info("Agent 未持有 platform, 自建 env.make_platform()")
            self.ctx.tool_ctx.platform = env.make_platform()

    # -- 对外主入口 ---------------------------------------------------------
    def step(self, user_text: str) -> AgentReply:
        """处理一轮用户输入,推进协商状态机,返回一次回复。"""
        self._ensure_platform()
        self.ctx.add_user(user_text)

        # 创新 B:每轮先按用户话语自适应更新专业度画像(可被总开关关闭)。
        if self.ctx.adaptive:
            self.ctx.profile.observe(user_text)

        # 把画像注入 tool_ctx.extra,让 macro 宏工具(design_link/debug_by_metric)
        # 也能按档位渲染 narrative——创新 B 贯穿到 LLM function-calling 路径。
        self.ctx.tool_ctx.extra["profile"] = self.ctx.profile

        # 在 checkpoint 上:先按用户回应决定前进/回退/停留
        if self.planner.is_checkpoint():
            verdict = self.planner.classify_response(user_text)
            if verdict == "confirm":
                self.planner.advance()
            elif verdict == "reject":
                self.planner.back()
            # "other" 视为对当前阶段的补充,不迁移

        if self.planner.is_done():
            reply = AgentReply(
                text="已完成。需要的话可以继续调参或开启新任务。",
                stage=self.planner.stage.value, done=True)
            self.ctx.add_assistant(reply.text)
            return reply

        # 执行当前阶段的动作
        if llm.is_configured():
            reply = self._step_with_llm(user_text)
        else:
            reply = self._step_skeleton(user_text)

        reply.stage = self.planner.stage.value
        reply.needs_confirmation = self.planner.is_checkpoint()
        self.ctx.add_assistant(reply.text)
        return reply

    # -- 分支一:LLM 驱动(function-calling 为主, 文本兜底) -----------------
    def _step_with_llm(self, user_text: str) -> AgentReply:
        groups = self.planner.allowed_tool_groups()
        schemas = registry.openai_schemas(groups) if groups else []
        sys_prompt = self._system_prompt(groups)
        messages = self._build_messages(sys_prompt)

        invocations: List[ToolInvocation] = []
        artifacts: Dict[str, Any] = {}
        cfg = llm.get_config()

        # 意图/方案阶段是纯协商, 不需要工具; 直接一次性出自然语言回复,
        # 从机制上杜绝模型在只有检索工具时反复空转到步数上限。
        if self.planner.stage in (Stage.INTENT, Stage.PROPOSE):
            text = self._force_text_reply(messages, cfg)
            return AgentReply(text=text or "(无输出)",
                              tool_invocations=invocations, artifacts=artifacts)

        # function-calling 为主;失败/不支持则降级为文本 ReAct
        try:
            text = self._run_toolcall_loop(
                messages, schemas, cfg, invocations, artifacts)
        except llm.LLMError as exc:
            logger.warning("function-calling 失败, 转文本 ReAct: %s", exc)
            text = self._run_react_text(
                messages, groups, cfg, invocations, artifacts)

        return AgentReply(text=text or "(无输出)",
                          tool_invocations=invocations, artifacts=artifacts)

    def _run_toolcall_loop(self, messages, schemas, cfg,
                           invocations, artifacts) -> str:
        """OpenAI tools 协议循环:模型选工具->执行->回喂,直到给出文本回复。"""
        for _ in range(self.max_tool_steps):
            resp = self._chat_raw(messages, cfg, tools=schemas)
            msg = resp["choices"][0]["message"]
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return (msg.get("content") or "").strip()
            messages.append(msg)
            for tc in tool_calls:
                result = registry.call_from_llm_toolcall(tc, self.ctx.tool_ctx)
                self._record(tc, result, invocations, artifacts)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False),
                })
        # 步数耗尽:去掉 tools 再请求一次, 逼模型基于已有 observation 给一句
        # 自然语言总结, 而不是把生硬的"上限"文案抛给用户。
        return self._force_text_reply(messages, cfg)

    def _force_text_reply(self, messages, cfg) -> str:
        """不带 tools 请求一次, 强制模型给出自然语言回复。

        用于两种场景:(1) 意图/方案等纯协商阶段的一次性直出;
        (2) tools 循环耗尽后的兜底总结。不污染调用方的 messages。
        """
        msgs = list(messages) + [{
            "role": "system",
            "content": "现在请不要调用任何工具,直接用简洁自然的中文回复用户:"
                       "复述你对需求的理解或给出下一步建议,并邀请用户确认。",
        }]
        try:
            resp = self._chat_raw(msgs, cfg, tools=None)
            text = (resp["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text
        except llm.LLMError as exc:
            logger.warning("兜底强制出文本失败: %s", exc)
        return "我已理解你的需求,请确认后我继续下一步。"

    def _run_react_text(self, messages, groups, cfg,
                        invocations, artifacts) -> str:
        """文本 ReAct 兜底:解析 Action/Action Input,执行后把 Observation 回喂。"""
        tool_desc = registry.react_tool_descriptions(groups)
        messages[0]["content"] += (
            "\n\n可用工具(文本协议):\n" + tool_desc +
            "\n\n按如下格式输出:\nThought: ...\nAction: 工具名\n"
            "Action Input: {json}\n或给出 Final Answer: ...")
        for _ in range(self.max_tool_steps):
            content = llm.chat(messages, cfg)
            final = _FINAL_RE.search(content)
            if final:
                return final.group(1).strip()
            m = _ACTION_RE.search(content)
            if not m:
                return content.strip()
            name = m.group(1)
            mi = _ACTION_INPUT_RE.search(content)
            try:
                args = json.loads(mi.group(1)) if mi else {}
            except json.JSONDecodeError:
                args = {}
            result = registry.call(name, args, self.ctx.tool_ctx)
            inv = ToolInvocation(name=name, args=args, result=result,
                                 ok=result.get("ok", False))
            invocations.append(inv)
            self._merge_artifacts(result, artifacts)
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": "Observation: " +
                           json.dumps(result, ensure_ascii=False)})
        return self._force_text_reply(messages, cfg)

    def _chat_raw(self, messages, cfg, tools=None) -> dict:
        """直接调 chat/completions 拿原始 JSON(为了取 tool_calls)。"""
        import urllib.error
        import urllib.request

        url = f"{cfg['base_url']}/chat/completions"
        payload = {"model": cfg["model"], "messages": messages,
                   "temperature": 0.2, "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg['api_key']}"})
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise llm.LLMError(f"HTTP {e.code}: {body[:300]}") from e
        except urllib.error.URLError as e:
            raise llm.LLMError(f"网络错误: {e.reason}") from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise llm.LLMError(f"响应非 JSON: {raw[:200]}") from e

    # -- 分支二:无 LLM 的确定性骨架(保证离线可演示) ---------------------
    def _step_skeleton(self, user_text: str) -> AgentReply:
        """无 LLM 时,按当前阶段给出确定性的协商话术(不真正建图)。"""
        stage = self.planner.stage
        texts = {
            Stage.INTENT: (
                f"我理解你想做:「{user_text}」。"
                "我会把它拆成 调制→信道→采集 的链路。理解对吗?(确认后给方案)"),
            Stage.PROPOSE: (
                "候选方案:BPSK 调制 → AWGN 信道 → head 限长 → file_sink 落盘。"
                "采纳这个方案吗?"),
            Stage.BUILD: (
                "我将按方案增量建图(add_block/connect/validate)。"
                "(未配置 LLM,此为骨架演示)确认进入仿真吗?"),
            Stage.SIMULATE: (
                "将跑无头仿真并读回 IQ,算 EVM、画星座图。确认查看结果吗?"),
            Stage.TUNE: (
                "可以根据星座/EVM 调 noise_voltage、sps、excess_bw 等参数。"
                "还要继续调吗?"),
            Stage.DONE: "已完成。",
        }
        return AgentReply(text=texts.get(stage, "(骨架)"))

    # -- 辅助 ---------------------------------------------------------------
    def _system_prompt(self, groups: List[str]) -> str:
        level = self.ctx.profile.level
        stage = self.planner.stage.value
        # 直接复用 memory 的分档风格约束(创新 B 的表达来源, 三档共用一套)。
        style = self.ctx.profile.style_prompt()
        adapt_note = "" if self.ctx.adaptive else "(自适应表达已关闭, 固定当前档位)"
        # 分阶段职责:意图/方案阶段以自然语言协商为主, 工具只是可选辅助,
        # 千万不要为了调工具而反复空转——否则会耗尽调用步数拿不到回复。
        stage_note = {
            "intent": "本阶段只需复述并澄清用户意图,直接用自然语言回复并请其确认;"
                      "一般无需调用工具,如需可选检索一次即可,不要反复检索。",
            "propose": "本阶段给出候选链路方案(调制→信道→采集),用自然语言说明后请用户确认;"
                       "工具为可选,不要空转。",
            "build": "本阶段可调用建图/宏工具真正搭建流图,完成后用自然语言复述结果并请确认。",
            "simulate": "本阶段调用仿真工具跑无头仿真并读回指标,然后用自然语言汇报结果。",
            "tune": "本阶段按指标调参并重新仿真,汇报前后对比。",
        }.get(stage, "")
        return (
            "你是 GNU Radio 通信系统构建 Agent,采用分层协商方式与用户协作:"
            "意图→方案→建图→仿真→调参。"
            f"当前阶段是【{stage}】,只在本阶段职责内行动,不要越阶。"
            f"阶段要求:{stage_note} "
            "重要:每一轮最终都必须给出一句面向用户的自然语言回复,"
            "不要只调用工具而不产出文本。 "
            f"当前用户专业度档位【{level}】{adapt_note},表达要求:{style}"
            "完成本阶段后用简短自然语言向用户复述结果并请其确认。")

    def _build_messages(self, sys_prompt: str) -> List[dict]:
        cfg = llm.get_config()
        msgs = [{"role": "system", "content": sys_prompt}]
        for role, content in self.ctx.history[-cfg["max_messages"]:]:
            if role in ("user", "assistant") and content:
                msgs.append({"role": role, "content": content})
        return msgs

    def _record(self, tool_call, result, invocations, artifacts):
        fn = tool_call.get("function", {})
        name = fn.get("name", "")
        raw = fn.get("arguments", "{}")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            args = {}
        invocations.append(ToolInvocation(
            name=name, args=args, result=result,
            ok=result.get("ok", False)))
        self._merge_artifacts(result, artifacts)

    @staticmethod
    def _merge_artifacts(result: dict, artifacts: dict):
        # 原子工具的顶层产物键
        for k in ("path", "script_path", "out_dir", "summary"):
            if k in result:
                artifacts[k] = result[k]
        # 宏工具(design_link 等)把产物放在嵌套 artifacts 里(grc_path/
        # constellation_png/spectrum_png/eye_png), 一并收编到顶层, 供 GUI 展示。
        nested = result.get("artifacts")
        if isinstance(nested, dict):
            for k, v in nested.items():
                if v:
                    artifacts[k] = v
        # 宏工具的关键指标也上浮一层, 方便 GUI/埋点直接读。
        metrics = result.get("metrics")
        if isinstance(metrics, dict) and metrics:
            artifacts.setdefault("metrics", {}).update(metrics)


# ---------------------------------------------------------------------------
# 离线自检:不依赖 LLM,验证 registry 调度 + 增量建图工具链 + 仿真闭环
# ---------------------------------------------------------------------------
def _selftest() -> int:
    """无 LLM 骨架自检。

    1) 验证 Agent.step 的分层协商状态机在无 LLM 时按启发式推进;
    2) 用 registry 直接驱动 build_tools 增量建 BPSK+AWGN 图并 run_simulation,
       验证 tools 层真实可用(function-calling 时模型走的正是这条路)。

    运行::

        PYTHONPATH=$PWD python -m grc.agent.core.agent
    """
    import logging as _logging

    _logging.basicConfig(level=_logging.WARNING)

    # 本自检刻意验证"无 LLM 骨架 + tools 直调"路径, 强制走确定性分支,
    # 保证离线可复现、不受外部 LLM 配置与其可能触发的 Qt 行为影响。
    _saved = os.environ.get("GRC_AGENT_API_KEY")
    if _saved:
        os.environ["GRC_AGENT_API_KEY"] = ""
    try:
        return _selftest_impl()
    finally:
        if _saved:
            os.environ["GRC_AGENT_API_KEY"] = _saved


def _selftest_impl() -> int:
    # --- Part A: 分层协商状态机(无 LLM 骨架) ---
    print("=== Part A: 分层协商状态机 ===")
    agent = Agent()
    agent._ensure_platform()
    print(f"块库: {len(agent.ctx.platform.blocks)} 块")
    flow = ["用 BPSK 过 AWGN 看星座图", "对", "可以", "确认", "好的", "继续"]
    for msg in flow:
        reply = agent.step(msg)
        print(f"[{reply.stage:8}] 用户:{msg!r:20} -> {reply.text[:40]}...")
    assert agent.planner.is_done() or agent.planner.stage.value == "tune", \
        f"状态机未推进到末段: {agent.planner.stage}"
    print("Part A: PASS(状态机逐阶段推进)\n")

    # --- Part B: registry 驱动 tools 真实建图 + 仿真 ---
    print("=== Part B: tools 增量建图 + 仿真闭环 ===")
    import tempfile

    from ..tools.registry import ToolContext

    out_dir = tempfile.mkdtemp(prefix="agent_tools_")
    iq_file = os.path.join(out_dir, "rx.bin")
    ctx = ToolContext(platform=agent.ctx.platform, out_dir=out_dir)

    def c(name, **kw):
        r = registry.call(name, kw, ctx)
        assert r.get("ok"), f"{name} 失败: {r}"
        return r

    c("init_flow_graph", flowgraph_id="agent_bpsk", generate_options="no_gui")
    c("add_block", key="variable", id="samp_rate", params={"value": "1000000"})
    c("add_block", key="variable", id="sps", params={"value": "4"})
    c("add_block", key="variable_constellation", id="bpsk_const",
      params={"type": "bpsk"})
    c("add_block", key="analog_random_source_x", id="src",
      params={"type": "byte", "min": "0", "max": "2",
              "num_samps": "1000", "repeat": "True"})
    c("add_block", key="digital_constellation_modulator", id="mod",
      params={"constellation": "bpsk_const", "differential": "False",
              "samples_per_symbol": "sps", "excess_bw": "0.35"})
    c("add_block", key="channels_channel_model", id="chan",
      params={"noise_voltage": "0.05", "freq_offset": "0.0",
              "epsilon": "1.0", "taps": "1.0", "seed": "0"})
    c("add_block", key="blocks_head", id="head",
      params={"type": "complex", "num_items": "8192"})
    c("add_block", key="blocks_file_sink", id="sink",
      params={"type": "complex", "file": repr(iq_file)})

    c("connect", src_id="src", dst_id="mod")
    c("connect", src_id="mod", dst_id="chan")
    c("connect", src_id="chan", dst_id="head")
    c("connect", src_id="head", dst_id="sink")

    v = c("validate_flowgraph")
    print(f"校验: valid={v['valid']} blocks={v['num_blocks']}")
    assert v["valid"], f"流图无效: {v['errors']}"

    sim = c("run_simulation", probes={"rx": [iq_file, "complex64"]})
    print(f"仿真: {sim['summary']}")
    m = c("read_metric", kind="evm", probe_id="rx", modulation="bpsk", sps=4)
    print(f"EVM = {m['value']:.2f}%  (符号 {m['n_symbols']})")
    pic = c("plot_constellation", probe_id="rx", sps=4)
    print(f"星座图: {pic['path']}")

    ok = 0.1 < m["value"] < 40.0
    print("\nPart B:", "PASS" if ok else "FAIL",
          f"(EVM {m['value']:.2f}% 经 tools 链路算出)")
    print("\n总自检:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
