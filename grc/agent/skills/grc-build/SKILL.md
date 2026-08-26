---
name: grc-build
description: 按确定性配方选型并搭建 GRC 流图(init/add/set/connect/render),产出可校验、可仿真的 .grc。当需要"把意图变成一张具体流图"时使用。
---

# grc-build:流图建图

## 何时使用
- 主 Agent 在 BUILD 阶段委派:把用户意图落成一张具体、合法的 .grc 流图。

## 使用协议(推荐顺序)
1. 读 `references/recipe_index.md`,按意图关键词选最匹配的**确定性配方**作骨架。
2. 读 `references/connect_rules.md` 复习连接与类型护栏。
3. 建图工具链(严格顺序):
   - `init_flow_graph(flowgraph_id, generate_options="no_gui")`
   - 逐块 `add_block(key, id, params)`(顺序照配方)
   - 必要时 `set_param(id, param, value)` 调整旋钮
   - 逐条 `connect(src_id, dst_id[, src_port, dst_port])`
   - `render_grc()` 存 .grc
4. 产物写入 `/session/work/build/`,并回报所用配方名、关键参数、块数。

## 输出契约
见 `references/build_output_contract.md`。核心:产物 .grc 必须能被
`validate_flowgraph` 通过;file_sink 路径由运行时填充(配方占位符 `__PROBE__`)。

## 与确定性宏的关系
无 LLM 场景下,建图由 `grc.agent.tools.design_link.design_link` 一步完成(同一套工具链)。
有 LLM 时,你可逐步调用工具以做更细的意图适配,但**默认应优先复用配方骨架**,
只在配方不覆盖时才偏离,并说明理由。
