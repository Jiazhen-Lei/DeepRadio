# 常见报错 -> 修复模式

| 报错特征 | 根因 | 修复 |
|----------|------|------|
| `type mismatch` / 端口颜色不一致 | 连接两端数据类型不同 | 统一链路类型;在需要处插入类型转换块 |
| `block key not found` / 未知块 | 块 key 拼错或发行版无此块 | 用 describe_block 核实 key;换标准块 |
| `param ... invalid` | 参数名/取值非法 | 对照 describe_block 的参数表改正 |
| `not connected` / 悬空端口 | 有输入/输出端口未连接 | 补齐连接或删除多余块 |
| `id duplicated` | 两个块 id 相同 | 改为唯一 id |
| `undefined variable` | 引用了不存在的变量 | 先加对应 variable 块(如 samp_rate/sps) |
| file_sink 路径错误 | file 参数为占位符或非法路径 | 由运行时填 `<session>/final/<id>_rx.bin` |

## 解读原则
- 只报**可执行**的修复(定位到块/连接/参数 + 目标值),不要泛泛而谈。
- 多个错误时按依赖顺序排列(先修变量/类型,再修连接)。
