---
name: grc-build
description: 根据 Radio Specification 和 Radio Design 产物创建或修改 GRC Flowgraph，只负责生成 .grc 文件。
---

# GRC Flowgraph Build

根据 TaskCard 创建或修改当前 Flowgraph。

- 如果存在上游 Radio Design，直接使用 TaskCard 提供的 `waveform_path`。
- `blocks_file_source.file` 必须引用已经存在的宿主机文件。不得猜测文件名，不得使用 `/session/work/...` 虚拟路径。
- 优先复用 `references/recipe_index.md` 中匹配的配方。
- 需要自行建图时，依次使用 `init_flow_graph`、`add_block`、`set_param`、`connect` 和 `render_grc`。
- 修改已有 Flowgraph 时使用 `apply_grc_diff` 或 `apply_flowgraph_patch`。
- 保持块 ID 唯一，连接类型匹配。
- 返回生成的 `.grc` 路径和主要构建信息。

本 Stage 不调用 `validate_flowgraph`、`run_simulation` 或 `verify_claims`，也不声明 Flowgraph 已通过验证。
