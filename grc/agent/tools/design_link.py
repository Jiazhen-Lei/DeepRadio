"""design_link:从一句通信意图端到端搭出一张可跑的流图。

编排:选配方(knowledge.recipes) -> 逐块 add_block/connect(build tools)
-> validate_flowgraph(critic tool) -> 可选 run_simulation + 取指标(sim tools)
-> 存 .grc(render_grc)。全程用 registry.call 走真实工具链,与 LLM
function-calling 时模型走的是同一条路,因此这条离线路径也是论文 baseline。

无 LLM 也能完整跑通;有 LLM 时 agent 可把它当"宏工具"一步到位。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..knowledge import recipes as _recipes
from . import registry
from .narrate import narrate_design


def design_link(ctx, profile=None, intent: str = "",
                recipe: str = "", simulate: bool = True,
                render: bool = True,
                flowgraph_id: str = "") -> Dict[str, Any]:
    """按意图/指定配方搭图并自检。

    Args:
        ctx: ToolContext(须已带 platform)。
        profile: UserProfile,用于按档位渲染 narrative。
        intent: 自然语言意图(用于离线选配方);给了 recipe 时可省。
        recipe: 显式指定配方名(优先于 intent 选型)。
        simulate: 建好后是否顺带跑一次仿真并取指标。
        render: 是否存 .grc。
        flowgraph_id: 流图 id;默认用配方名。

    Returns:
        dict:ok / recipe / blocks / connections / valid / metrics /
        artifacts(grc_path/const_png/...) / narrative。
    """
    if ctx.platform is None:
        return {"ok": False, "error": "缺少 platform,无法建图"}

    rc = _recipes.get_recipe(recipe) if recipe else None
    if rc is None:
        rc = _recipes.match_recipe(intent)
    fid = flowgraph_id or rc.name

    steps: List[dict] = []

    def _c(name, **kw):
        r = registry.call(name, kw, ctx)
        steps.append({"tool": name, "args": kw, "ok": bool(r.get("ok")),
                      "detail": r.get("error") or r.get("warning") or ""})
        return r

    # 1) 新建空流图(no_gui 便于无头仿真)
    r = _c("init_flow_graph", flowgraph_id=fid, generate_options="no_gui")
    if not r.get("ok"):
        return {"ok": False, "error": f"init 失败: {r.get('error')}",
                "steps": steps}

    # 2) probe 落盘路径(把配方里的占位符替换成真实路径)
    out_dir = ctx.out_dir or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    probe_path = os.path.join(out_dir, f"{fid}_rx.bin")

    # 3) 逐块添加
    for key, bid, params in rc.blocks:
        p = dict(params)
        for k, v in list(p.items()):
            if v == "__PROBE__":
                p[k] = repr(probe_path)
        _c("add_block", key=key, id=bid, params=p)

    # 4) 逐条连接(支持 (src,dst) 或 (src,dst,sp,dp))
    for conn in rc.connections:
        if len(conn) == 2:
            _c("connect", src_id=conn[0], dst_id=conn[1])
        else:
            _c("connect", src_id=conn[0], dst_id=conn[1],
               src_port=conn[2], dst_port=conn[3])

    # 5) critic 自检
    v = _c("validate_flowgraph")
    valid = bool(v.get("valid"))
    errors = v.get("errors", [])
    if not valid:
        exp = _c("explain_error", errors=errors)
        errors = exp.get("explanations", errors)

    artifacts: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}

    # 6) 存 .grc
    if render:
        rr = _c("render_grc")
        if rr.get("ok"):
            artifacts["grc_path"] = rr["path"]

    # 7) 可选仿真 + 取指标 + 画图
    if simulate and valid:
        probe_id = rc.probe_block_id or "sink"
        sim = _c("run_simulation",
                 probes={probe_id: [probe_path, "complex64"]})
        if sim.get("ok"):
            artifacts["out_dir"] = sim.get("out_dir")
            if "evm" in rc.metrics:
                mod = _guess_modulation(rc.name)
                m = _c("read_metric", kind="evm", probe_id=probe_id,
                       modulation=mod, sps=rc.sps)
                if m.get("ok"):
                    metrics["evm_pct"] = m["value"]
                    metrics["n_symbols"] = m.get("n_symbols")
            if "constellation" in rc.metrics:
                pc = _c("plot_constellation", probe_id=probe_id, sps=rc.sps)
                if pc.get("ok"):
                    artifacts["constellation_png"] = pc["path"]
            if "spectrum" in rc.metrics:
                ps = _c("plot_spectrum", probe_id=probe_id, samp_rate=1e6)
                if ps.get("ok"):
                    artifacts["spectrum_png"] = ps["path"]
            if "eye" in rc.metrics:
                pe = _c("plot_eye", probe_id=probe_id, sps=rc.sps)
                if pe.get("ok"):
                    artifacts["eye_png"] = pe["path"]

    out = {
        "ok": valid,
        "recipe": rc.name,
        "recipe_title": rc.title,
        "difficulty": rc.difficulty,
        "num_blocks": v.get("num_blocks", len(rc.blocks)),
        "valid": valid,
        "errors": errors,
        "knobs": rc.knobs,
        "metrics": metrics,
        "artifacts": artifacts,
        "steps": steps,
    }
    out["narrative"] = narrate_design(rc, out, profile)
    return out


def _guess_modulation(recipe_name: str) -> str:
    n = recipe_name.lower()
    if "qpsk" in n:
        return "qpsk"
    if "bpsk" in n:
        return "bpsk"
    return "bpsk"
