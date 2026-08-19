---
name: grc-critic
description: 校验 GRC 流图合法性,把校验/运行报错整理为可执行的修复建议。当需要"这张图对不对、报错怎么修"时使用。
---

# grc-critic:流图校验与报错解读

## 何时使用
- 主 Agent 在 CRITIC 阶段委派:判定 build 产物是否合法,报错时给修复建议。

## 使用协议
1. 用 `validate_flowgraph()` 校验当前流图(或指定 /session/work/build/flowgraph.grc)。
2. 若报错,用 `explain_error(errors)` 结合 `references/error_fix_patterns.md`
   把底层报错翻译成"原因 + 具体改法"。
3. 结论写入 `/session/work/critic/report.md`:通过 or 需修复(逐条给建议)。

## 输出
- 你**不直接改图**;只产出结论供主 Agent 决定是否回退给 builder 修复。
- 结论要具体到"哪个块/哪条连接/哪个参数"以及"改成什么"。

## 校验清单
见 `references/validation_checklist.md`。
