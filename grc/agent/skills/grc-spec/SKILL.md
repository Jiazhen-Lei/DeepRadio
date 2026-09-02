---
name: grc-spec
description: 对齐并维护可追溯的 Radio Specification，包括目标、Required 参数、Added 参数和成功条件。
---

# Radio Specification Alignment

根据 TaskCard 对齐用户目标、参数和成功条件。

- 先使用 `spec_clarify` 检查缺少的 Required 参数。
- Required 参数没有确定时，返回 `open_questions`，不要提交完整结果。
- Added 参数只记录用户主动提供或确认的额外参数。
- 使用 `spec_commit` 记录用户已经表达的事实。
- 推断只能标记为 assumption，不能写成用户决定。
- 用户明确选择仅仿真或不使用硬件时，将其记录为约束，不再询问硬件参数。

多轮补充参数始终属于同一个 Radio Specification Alignment Stage。
返回简短结果、当前参数和仍需用户回答的问题。
