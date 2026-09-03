---
name: grc-critic
description: 校验当前 GRC 流图，并把错误整理为可执行的修复建议。
---

# GRC Flowgraph Verification

用于 `flowgraph_verification` Stage。只校验当前宿主机已经加载的 Flowgraph，不修改 Flowgraph。

## 使用协议
1. 调用一次 `validate_flowgraph()`。包含未启用的 RF 端点时使用 `arm_disabled_rf=true`，该参数只用于结构校验。
2. 如果 `valid=true`，立即返回 `passed`。
3. 如果 `valid=false`，可调用一次 `explain_error(errors)`，随后立即返回 `failed`。
4. 如果没有当前 Flowgraph 或缺少引用文件，立即返回 `inconclusive`。

## 输出
- 返回 `outcome`、`evidence`、`errors` 和简短建议。
- 不自行重试、不向用户提问、不进入 Build 或 Diagnosis。
- 不使用固定的 `/session/work/...` 路径，也不额外写虚拟报告文件。

## 校验清单
见 `references/validation_checklist.md`。
