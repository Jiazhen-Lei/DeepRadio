# 连接规则与建图顺序

## 建图工具调用顺序(不可乱序)
1. `init_flow_graph` —— 必须最先,建空图并设 generate_options(仿真用 no_gui)。
2. `add_block` —— 先加所有块(含 variable),再连接。变量块要在被引用前存在。
3. `set_param` —— 在 add_block 之后微调参数(可选)。
4. `connect` —— 全部块就位后再连线。
5. `render_grc` —— 最后存盘。

## 连接规则
- `connect(src_id, dst_id)`:单入单出,端口默认 0→0。
- `connect(src_id, dst_id, src_port, dst_port)`:多端口块显式指定(如 add 的 in0/in1)。
- 两端数据类型必须一致(见 grc-block-rag/references/naming_guardrails.md)。

## 典型链路骨架(BPSK/QPSK over AWGN)
```
random_source(byte) -> constellation_modulator(byte->complex)
   -> channel_model(complex) -> head(complex) -> file_sink(complex)
```

## 典型链路骨架(单音+噪声)
```
sig_source(complex) --\
                       add(complex) -> head -> file_sink
noise_source(complex) -/
```
注意 add 的两路分别接 in0/in1(显式端口)。
