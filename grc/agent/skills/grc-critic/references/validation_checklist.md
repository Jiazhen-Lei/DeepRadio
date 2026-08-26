# 校验清单

按顺序过一遍:

1. **结构**:含 options / blocks / connections;有 id=samp_rate 的 variable 块。
2. **块 id 唯一性**:无重复 id;命名为小写字母数字下划线。
3. **参数合法性**:每个块的参数名与取值都在 describe_block 允许范围内。
4. **变量引用**:参数里引用的变量(samp_rate/sps/星座名)都有对应块定义。
5. **类型一致**:每条连接两端类型相同;转换只在调制/转换块处发生。
6. **端口完备**:无悬空必需端口。已校验后再 disarm 的硬件 sink 不算失败。
7. **仿真可行**:若需读指标,末端有 head + file_sink,file 用占位符。
8. **generate_options**:离线仿真为 no_gui。

全部通过 -> valid;否则逐条列出未过项与修复建议。
