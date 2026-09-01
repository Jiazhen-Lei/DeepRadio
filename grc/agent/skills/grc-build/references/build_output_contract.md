# 建图 ResultEnvelope 输出契约

FlowgraphAgent 完成后返回紧凑 JSON：

- `workflow_id`、`revision`、`base_project_version`、`stage_id`：原样复制 TaskCard。
- `outcome`：`passed`、`failed` 或 `inconclusive`。
- `artifacts`：生成的 `.grc` 和相关产物路径。
- `evidence`：本轮成功工具结果对应的证据名称。
- `note`：配方、块数、校验结论与剩余风险。

约束：

- 请求渲染时，没有有效 `.grc` 路径不得报告成功。
- 离线仿真使用 `generate_options=no_gui`。
- `blocks_file_sink.file` 的 `__PROBE__` 由运行时替换，不写死绝对路径。
- 中间产物写入 `/session/work/build/`；最终发布由 MainAgent 负责。
