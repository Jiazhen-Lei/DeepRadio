---
name: grc-block-rag
description: 检索 GNU Radio Companion(GRC)块、解释端口与关键参数、查找可参考的示例链路。当需要"用哪个块、这个块的端口/参数是什么、有没有类似示例"时使用。
---

# grc-block-rag:块知识检索

## 何时使用
- 当前 Stage 需要知道**用哪个块**实现某功能(如"BPSK 调制用什么块")。
- 需要解释某个块的**端口类型与关键参数**(避免类型不匹配导致连接失败)。
- 需要检索**可参考的示例链路**作为建图起点。

## 使用协议
1. 先读本目录 `references/block_catalog.md` 了解常用块清单与用途。
2. 用工具 `search_blocks(query)` 语义检索候选块;用 `describe_block(key)` 查端口/参数。
3. 用 `list_examples()` 找相近示例链路。
4. 将推荐块、端口、参数和参考示例用于当前 Stage。

## 硬性护栏(见 references/naming_guardrails.md)
- 整条链路的数据类型(complex/float/byte)必须一致,否则连接非法。
- 块实例 id 必须唯一、小写字母数字下划线。
- 只使用 GNU Radio 标准发行版中真实存在的块 key 与参数名,不要臆造。

## 不要做
- 在 `radio_design` 中不建图、不改图；建图只属于 `flowgraph_build`。
- 不写 `/session/final/`。
