"""explain_block:对某个块产出\"它是什么 / 为什么这么配\"的分档解说。

编排:describe_block(knowledge tool 取真实参数/端口)+ 内置块语义表
(给出角色定位与关键参数直觉)-> 按 profile 档位 narrate。

内置语义表覆盖配方里用到的核心块;未收录的块回落到 describe_block
的原始信息,保证任何块都能解释(只是深度不同)。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..tools import registry
from .narrate import narrate_block

#: 块角色 + 关键参数直觉(key -> {role, params:{name:meaning}})
_BLOCK_SEMANTICS: Dict[str, dict] = {
    "digital_constellation_modulator": {
        "role": "把比特流映射成星座点并做脉冲成形,是数字发射机的核心。",
        "params": {
            "constellation": "选用的星座(bpsk/qpsk/...),决定每符号承载几 bit",
            "samples_per_symbol": "每符号样本数(过采样率),影响带宽与眼图",
            "excess_bw": "RRC 滚降系数,权衡带宽占用与码间串扰",
            "differential": "是否差分编码,影响对相位模糊的鲁棒性",
        },
    },
    "channels_channel_model": {
        "role": "模拟无线信道:叠加高斯噪声、频偏、定时偏差与多径,用于压测接收性能。",
        "params": {
            "noise_voltage": "噪声电压;越大等效 SNR 越低",
            "freq_offset": "归一化载波频偏;非零会让星座旋转",
            "epsilon": "定时偏差(重采样比),模拟采样时钟不准",
            "taps": "多径抽头;非平凡值引入频率选择性衰落",
        },
    },
    "analog_random_source_x": {
        "role": "产生随机比特/字节作为信源,用于仿真数据业务。",
        "params": {
            "min": "取值下界", "max": "取值上界(开区间)",
            "num_samps": "预生成样本数", "repeat": "是否循环重复",
        },
    },
    "blocks_head": {
        "role": "只放行前 N 个样本后停止,给无头仿真一个明确的结束条件。",
        "params": {"num_items": "放行的样本数,决定仿真时长/数据量"},
    },
    "blocks_file_sink": {
        "role": "把数据流落盘成二进制文件,供仿真后回读分析(探针)。",
        "params": {"file": "输出文件路径", "type": "数据类型(complex/float/...)"},
    },
    "variable_constellation": {
        "role": "定义一个可复用的星座对象,供调制/解调块引用。",
        "params": {"type": "星座类型 bpsk/qpsk/qam16 等"},
    },
    "analog_sig_source_x": {
        "role": "产生正弦/方波等模拟信号源,常用于生成单音载波。",
        "params": {"freq": "频率(Hz)", "amplitude": "幅度",
                   "waveform": "波形(余弦/方波/锯齿)"},
    },
    "analog_noise_source_x": {
        "role": "产生高斯/均匀噪声,用于叠加到信号上做加噪演示。",
        "params": {"amplitude": "噪声幅度", "noise_type": "噪声分布类型"},
    },
    "blocks_add_xx": {
        "role": "把多路同类型数据流逐样本相加,常用于\"信号 + 噪声\"。",
        "params": {"num_inputs": "输入路数"},
    },
    "digital_ofdm_tx": {
        "role": "OFDM 发射机:分组->调制子载波->IFFT->加循环前缀,抗多径。",
        "params": {"fft_len": "子载波数", "cp_len": "循环前缀长度",
                   "bps_payload": "载荷每符号比特数"},
    },
}


def explain_block(ctx, profile=None, key: str = "",
                  block_id: str = "") -> Dict[str, Any]:
    """解释一个块。

    Args:
        key: 块类型 key(优先)。
        block_id: 若给的是已在流图里的实例 id,则从 ctx.blocks 反查其 key。
    """
    if not key and block_id and block_id in ctx.blocks:
        key = getattr(ctx.blocks[block_id], "key", "")
    if not key:
        return {"ok": False, "error": "需提供块 key 或已存在的 block_id"}

    desc = registry.call("describe_block", {"key": key}, ctx)
    sem = _BLOCK_SEMANTICS.get(key, {})
    role = sem.get("role", "")
    if not role and desc.get("ok"):
        role = f"{desc.get('label', key)}(通用块,详见其参数)。"

    # 组织关键参数:优先用语义表的直觉,补上真实默认值
    real_params = {p["name"]: p for p in desc.get("params", [])} \
        if desc.get("ok") else {}
    key_params: List[dict] = []
    for pname, meaning in sem.get("params", {}).items():
        entry = {"name": pname, "meaning": meaning}
        if pname in real_params:
            entry["default"] = real_params[pname].get("default", "")
        key_params.append(entry)
    # 若语义表没覆盖,退回真实参数前几个
    if not key_params and desc.get("ok"):
        for p in desc["params"][:5]:
            key_params.append({"name": p["name"], "meaning": "见块文档",
                               "default": p.get("default", "")})

    info = {"ok": True, "key": key,
            "label": desc.get("label", key) if desc.get("ok") else key,
            "role": role, "key_params": key_params}
    info["narrative"] = narrate_block(info, profile)
    return info
