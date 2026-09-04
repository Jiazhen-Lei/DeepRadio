---
name: grc-spec
description: 根据用户意图和当前 Workflow 对齐并维护唯一的 Radio Specification。
---

# Radio Specification Alignment

执行 `radio_specification_alignment` 时，只维护 Radio Specification，不执行其他领域工作。

执行前读取 `references/radio-specification.yaml`。它提供字段和无线电领域参考，
最终字段由你根据用户意图、完整 Workflow 和当前 Specification 判断。

1. 从 TaskCard 中提取用户明确表达的事实。
2. 更新现有 Specification，不重新生成未变化的字段。
3. Goal 和后续 Stage 必需的字段归入 `required`。
4. 用户主动增加的非必需字段归入 `added`。默认值、派生值和假设不能归入 Added。
5. 字段状态只能是 `aligned`、`needs_confirmation` 或 `missing`。
6. 使用 `spec_update` 保存字段、约束和假设。
7. 存在 `unresolved_fields` 时，通过 Workflow 的输入检查点向用户提问。
8. 只有所有 Required 字段均为 `aligned` 时才调用 `spec_commit`。

协议唯一确定的标准值和由已对齐字段直接计算的值可以标记为 `aligned`。
存在可替代选择的建议值标记为 `needs_confirmation`。没有值时标记为 `missing`。
字段来源使用 `user`、`extracted`、`protocol_default`、`safety_default`、
`derived` 或 `unresolved`。`group`、`source` 和 `status` 分别表达字段作用、来源和对齐状态。

用户明确选择仅仿真或不使用硬件时，将其写入 constraints，不要求硬件参数。
推断写入 assumptions，不能作为用户决定。新的协议或参数可以直接加入 Specification，
不受 reference 中示例范围限制。

多轮补充始终属于同一个 Radio Specification Alignment Stage。完成后更新当前 Stage，并向用户说明对齐结果。
