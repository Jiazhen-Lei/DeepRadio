# 建图输出契约

flowgraph_builder_agent 完成后必须满足:

## 产物路径
- 主产物:`/session/work/build/flowgraph.grc`(YAML 文本)。
- 建图结论摘要:`/session/work/build/summary.md`(配方名/难度/块数/是否合法/可调旋钮)。

## 合法性
- 产物必须能被 `validate_flowgraph` 判定为 valid;否则回报 critic 并给出可疑块/连接。

## 参数占位符
- `blocks_file_sink` 的 `file` 参数在配方里是占位符 `__PROBE__`,由运行时替换为
  `<session>/final/<id>_rx.bin`。builder 不要写死绝对路径。

## generate_options
- 用于离线仿真时必须是 `no_gui`;仅供 GUI 打开展示时可用 `qt_gui`。
- DeepRadio 主链路走 no_gui(可无头仿真),GUI 打开时由适配层/GUI 侧处理显示。

## 回报格式(给主 Agent)
一段话说明:选了哪个配方、为何、关键参数、块数、是否已通过校验、产物路径。
