# 建图 ResultEnvelope 输出契约

FlowgraphAgent 完成后返回紧凑 JSON：

- `task_id`：原样复制 TaskCard 的任务 ID。
- `ok`：仅当确定性建图和校验都成功时为 true。
- `produced_claims`：本轮创建或更新的结构 Claim。
- `proposed_changes`：被 PolicyGateway 暂停、等待确认的改动。
- `artifacts`：生成的 `.grc` 和相关产物路径。
- `note`：配方、块数、校验结论与剩余风险。

约束：

- 请求渲染时，没有有效 `.grc` 路径不得报告成功。
- 离线仿真使用 `generate_options=no_gui`。
- `blocks_file_sink.file` 的 `__PROBE__` 由运行时替换，不写死绝对路径。
- 中间产物写入 `/session/work/build/`；最终发布由 MainAgent 负责。
