"""DeepRadio-Agent 运行时执行底座。

包含:
- ``simulate``: 无头仿真闭环(生成->跑->读数据->算指标)。

参见 ``docs/agent_architecture.md`` 第 5 节。

子模块按需导入(``from grc.agent.runtime import simulate``),
此处不做即时导入,以免 ``python -m grc.agent.runtime.simulate`` 触发 runpy 警告。
"""
