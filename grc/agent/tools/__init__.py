"""原子工具层(Tools)—— DeepAgent 的 Capability 底层。

每个 tool 是一个**纯函数 + JSON-Schema 描述**,通过 :mod:`registry` 注册,
再由 :mod:`service.tools_lc` 桥接为 LangChain ``StructuredTool``,
供主 Agent / Subagent 以 function-calling 协议调度。

分组:
    knowledge_tools  search_blocks / describe_block / list_examples
    build_tools      add_block / connect / set_param / render_grc
    critic_tools     validate_flowgraph / explain_error
    sim_tools        run_simulation / read_metric / plot_constellation
    design_link / debug_by_metric   宏工具（registry 注册）

用法::

    from grc.agent.tools import registry
    from grc.agent.tools import knowledge_tools, build_tools, critic_tools, sim_tools

    # 触发各模块的 @tool 注册
    registry.load_all()
    result = registry.call("search_blocks", {"query": "bpsk"}, ctx)
"""

from __future__ import annotations

from . import registry  # noqa: F401
