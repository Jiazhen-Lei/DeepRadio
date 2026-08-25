"""BPSK @ 2.4GHz 手工回归:验证 grc.agent.env 支撑的完整链路。

不是 live 架构层。产品路径请走 ServiceAgent + Workflow。

运行::

    conda activate gnuradio
    PYTHONPATH=$PWD python dev_docs/regression/bpsk_2g4_regression.py

验证链路: 建图 -> validate -> 存 .grc -> 生成 .py -> 执行 -> 星座校验。
"""

from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from grc.agent import env  # noqa: E402

OUT_DIR = tempfile.mkdtemp(prefix="bpsk_reg_")
IQ_FILE = os.path.join(OUT_DIR, "bpsk_iq.bin")

SAMP_RATE = 1_000_000
SPS = 4
CENTER_FREQ = 2.4e9
N_SAMPLES = 8192


def build(platform):
    """构建 BPSK 基带发射链(不含硬件，便于无 SDR 回归)。"""
    fg = platform.make_flow_graph()
    env.configure_options(fg, "python", "no_gui",
                          flowgraph_id="bpsk_2g4_regression")

    def nb(key, bid, **kw):
        block = fg.new_block(key)
        if block is None:
            raise AssertionError(f"块不存在: {key}")
        block.params["id"].set_value(bid)
        for name, value in kw.items():
            if name not in block.params:
                raise AssertionError(
                    f"{key} 无参数 {name!r}; 实际参数: {list(block.params)}"
                )
            block.params[name].set_value(value)
        return block

    nb("variable", "samp_rate", value=str(SAMP_RATE))
    nb("variable", "sps", value=str(SPS))
    # center_freq 仅作记录:2.4GHz 必须由 SDR 硬件完成上变频，
    # 数字混频需 >=4.8 GS/s(38.4 GB/s)，物理不可行。
    nb("variable", "center_freq", value=repr(CENTER_FREQ))
    nb("variable_constellation", "bpsk_const", type="bpsk")

    # random_source 默认 dtype 是 int，而调制器要 byte -> 必须显式指定
    src = nb("analog_random_source_x", "src", type="byte",
             min="0", max="2", num_samps="1000", repeat="True")
    mod = nb("digital_constellation_modulator", "mod",
             constellation="bpsk_const", differential="False",
             samples_per_symbol="sps", excess_bw="0.35")
    head = nb("blocks_head", "head", type="complex", num_items=str(N_SAMPLES))
    sink = nb("blocks_file_sink", "sink", type="complex", file=repr(IQ_FILE))

    for i, block in enumerate(fg.blocks):
        block.states["coordinate"] = (120 + (i % 4) * 230, 140 + (i // 4) * 170)

    # connect 之前必须 rewrite():端口数受 nports/type 参数控制，
    # 未 rewrite 时 block.sources/sinks 可能为空或数量不对。
    fg.rewrite()
    fg.connect(src.sources[0], mod.sinks[0])
    fg.connect(mod.sources[0], head.sinks[0])
    fg.connect(head.sources[0], sink.sinks[0])
    fg.rewrite()
    fg.validate()
    return fg


def check_constellation(path: str) -> bool:
    """BPSK 判据:符号点应落在实轴 ±A，虚部能量可忽略。"""
    import numpy as np

    data = np.fromfile(path, dtype=np.complex64)
    if len(data) == 0:
        print("    ! 输出文件为空")
        return False
    symbols = data[SPS * 4::SPS]  # 跳过成形滤波瞬态后按符号率抽取
    re = float(np.mean(np.abs(symbols.real)))
    im = float(np.mean(np.abs(symbols.imag)))
    ratio = im / max(re, 1e-9)
    print(f"    样本 {len(data)}  实部均值 {re:.3f}  虚部均值 {im:.3f}  "
          f"虚/实 {ratio:.3f}")
    return ratio < 0.35


def main() -> int:
    platform = env.make_platform()
    print(f"[1] 块库: {len(platform.blocks)} 块 / "
          f"{len(platform.workflow_manager.workflows)} workflow")

    fg = build(platform)
    print(f"[2] is_valid(): {fg.is_valid()}")
    if not fg.is_valid():
        for msg in fg.get_error_messages():
            print("    !", msg.strip().splitlines()[-1].strip())
        return 1

    grc_path = os.path.join(OUT_DIR, "bpsk_2g4_regression.grc")
    platform.save_flow_graph(grc_path, fg)
    print(f"[3] 已存 {grc_path} ({os.path.getsize(grc_path)} bytes)")

    from grc.core.generator import Generator

    generator = Generator(fg, OUT_DIR)
    generator.write()
    py_path = generator.file_path
    print(f"[4] 已生成 {py_path} ({os.path.getsize(py_path)} bytes)")

    import importlib.util

    spec = importlib.util.spec_from_file_location("bpsk_gen", py_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    top_cls = next(
        obj for name, obj in vars(module).items()
        if isinstance(obj, type) and hasattr(obj, "start")
        and obj.__module__ == module.__name__
    )
    tb = top_cls()
    tb.start()
    tb.wait()
    print("[5] 流图执行完成")

    ok = check_constellation(IQ_FILE)
    print("[6] BPSK 星座:", "PASS" if ok else "FAIL")
    print("\n回归结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
