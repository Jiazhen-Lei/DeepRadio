# 命名与连接护栏

违反以下任一条,流图校验(validate_flowgraph)通常会失败。

## 1. 类型一致性
- 一条信号通路上的数据类型必须端到端一致(complex↔complex, float↔float, byte↔byte)。
- 类型转换只发生在明确的转换块处(如 `digital_constellation_modulator`:byte→complex)。
- 常见错误:把 complex 源直接连到期望 float 的块;或忘了信道块两端都是 complex。

## 2. 块 id 命名
- id 必须**唯一**,且为小写字母/数字/下划线(如 `src`、`bpsk_const`、`chan`)。
- 变量块(`variable`)的 id 就是它在其它块参数里被引用的名字(如 `samp_rate`、`sps`)。
- 引用变量时直接写变量名字符串(如 `samples_per_symbol: 'sps'`)。

## 3. 端口序号
- 端口序号从 0 开始,用于多输入/多输出块(如 `blocks_add_xx` 有 in0/in1)。
- 单入单出块连接可省略端口号(默认 0→0)。

## 4. 采样率变量
- 必须存在 id 为 `samp_rate` 的 `variable` 块;需要采样率的块直接引用 `'samp_rate'`。

## 5. 仿真友好
- 需要离线读指标时,链路末端接 `blocks_head`(截断样本数)+ `blocks_file_sink`(落盘)。
- file_sink 的 `file` 参数由运行时填真实路径(配方里用占位符 `__PROBE__`)。
