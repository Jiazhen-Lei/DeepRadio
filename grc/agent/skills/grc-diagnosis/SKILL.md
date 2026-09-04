---
name: grc-diagnosis
description: 根据可复现的 Evidence 诊断 GNU Radio、运行环境或硬件问题的原因，并输出诊断报告。
---

# DeepRadio Diagnosis

从 TaskCard 提供的 Evidence 开始诊断：

- 指标异常使用 `debug_by_metric`。
- Flowgraph 报错使用 `explain_error`。
- 环境、设备或运行状态问题使用 `run_diagnosis_checks`。
- 说明原因、依据和建议修改，并生成 `diagnosis_report`。

只诊断原因，不修改 Flowgraph，也不执行重新验证。用户接受修改后，由后续 Flowgraph Build Stage 处理。
