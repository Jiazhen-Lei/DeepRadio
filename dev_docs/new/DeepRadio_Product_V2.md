# DeepRadio 产品说明（V3 架构基线）

> 日期：2026-09-01
> 本文保留兼容文件名 `V2`，但不保留旧版本增量记录。

## 1. 产品定位

DeepRadio 是 GNU Radio Companion 内的 mixed-initiative 无线工程助手。用户用自然语言描述目标，系统与用户对齐 Radio Specification，动态组合 Workflow，并由 DeepAgent 协调协议、流图、验证、诊断和硬件领域 Agent。所有工程操作和 RF 动作受到确定性 Policy、Evidence 和可恢复 Checkpoint 约束。

产品承诺不是“LLM 自动做一切”，而是：开放任务能理解，固定步骤可复现，关键动作由用户控制，结果有证据，失败可以恢复。

## 2. 用户体验主流程

```text
描述目标
→ 查看系统理解的 Radio Specification
→ 补充缺失字段或确认
→ 实时查看 Workflow Monitor
→ 审阅流图/修改计划
→ 必要时确认设备配置或有限 RF 计划
→ 查看结果、证据和质量
→ 失败时 Retry / Revise / Cancel
```

Radio Specification 是只读 live card；修改继续通过对话或结构化选择进入同一个 Intent coordinator。Workflow Monitor 常驻显示当前 Stage、已完成步骤、关键 Claims、等待原因和可执行动作。

## 3. DeepAgent 在产品中的角色

- 需求含糊时，DeepAgent 提出最少、最相关的问题。
- 设计存在多个方案时，DeepAgent 组织领域 Agent 权衡。
- 验证失败时，DeepAgent 汇总跨层证据并提出最小修复。
- 固定校验和硬件操作在后台快速确定性执行，DeepAgent 解释结果。

用户感受到一个跨领域协作的 Agent，而不是多个互相冲突的机器人。系统不向普通用户显示工具名、JSON、内部 completion 字段或重复委派日志。

## 4. 用户可见状态语义

| 状态 | 含义 |
|---|---|
| Running | 当前 Stage 正在执行 |
| Waiting for input | 缺少执行所需信息 |
| Waiting for approval | 即将跨越明确决策边界 |
| Waiting for capability | 环境或设备条件不满足 |
| Recovery required | 当前步骤未通过，可重试或取消 |
| Passed | 当前目标的全部证据契约满足 |
| Completed with warning | 目标完成，但运行质量或证据等级有限 |
| Failed | 目标未满足且当前没有安全自动恢复路径 |

“文件已生成”“仿真成功”“设备已发现”“RF 已运行”“空口已观察”必须是不同事实。

## 5. 交互可完成性

每个 Waiting 状态至少提供一个有效操作：

- Input：回答、修改或取消。
- Approval：确认、拒绝或继续提问。
- Capability：修复环境后重试，或取消。
- Recovery：重试当前 Stage、修改方案或取消。

按钮发送结构化 command，并带 interaction/checkpoint id。用户在确认点提问不会被误判为批准；普通文本“确认”也不能越过错绑或过期的 checkpoint。

## 6. 安全与信任

- 读工程和离线验证不需要 RF 授权。
- 写工程前根据改动范围设置 checkpoint。
- 设备探测只读，不等于配置授权。
- RF grant 绑定设备 identity、频率、采样率、功率/衰减、artifact、时长和 Intent revision。
- 任意相关修改撤销旧 grant。
- Stop 与 Emergency Stop 在 Runtime 期间始终可用。
- Agent 不能把自己的叙述当成通过证据。

## 7. PlutoSDR BLE 用户流程

1. 用户提出 BLE/PlutoSDR 目标并完成规格对齐。
2. 系统快速生成 BLE PDU、1M PHY 波形和禁用 RF sink 的预览流图。
3. 系统离线验证 bits/CRC/whitening 和流图结构。
4. 用户审阅流图；确认只代表接受设计，不授权 RF。
5. 系统检查 IIO 环境、发现并精确探测 PlutoSDR。
6. 用户看到绑定参数的有限时长 RF 授权卡。
7. 授权后系统 arm、编译并启动；Monitor 显示 Runtime 和 Stop。
8. 用户或仪器提交独立空口证据。
9. 系统停止并给出 outcome、quality 和 evidence grade。

设备不在位时停在“Device not found”，提供 Retry/Cancel。不能退回无关仿真并声称任务完成。

## 8. 性能目标

- 固定 Stage 不等待 LLM。
- Hybrid 首轮使用 fast path；仅失败或歧义时启动 DeepAgent。
- 相同幂等调用不重复执行。
- 多个独立验证可并行。
- 进度按 Stage 推送，用户不面对长时间空白。
- Retry 不重跑已通过且版本仍有效的阶段。

产品评测同时报告总时延和 LLM 时间，避免把编译、仿真、设备等待混为一类。

## 9. 产品完成标准

- 开放文本、补充轮次、修改、诊断、观察和组合任务均能完成。
- 所有 waiting 状态都可操作，重启后仍可恢复。
- 回复只陈述实际发生且有证据的事实。
- GNU Radio 软件回归全绿；Pluto 流图可生成和编译。
- 无设备、未授权、参数错绑和离线验证失败均无法启动 RF。
- 真实 HIL 只在记录设备、run、stop 和 OTA evidence 后标记通过。
