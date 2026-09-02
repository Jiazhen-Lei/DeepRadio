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
    if ctx.extra.get("mutation_forbidden"):
        return {
            "ok": False,
            "error": "Flowgraph changes are forbidden in this turn because the user requested diagnosis only or asked not to modify it yet",
            "policy": "DENY",
        }

    intent = (intent or "").strip() or str(ctx.extra.get("user_text") or "")
    rc = _recipes.resolve_recipe(intent=intent, recipe=recipe)
    fid = flowgraph_id or rc.name
    state = ctx.extra.get("state")
    approved_pending = None
    if state is not None:
        from ..state import ALLOW, gate

        new_modulation = _recipes.guess_modulation(rc.name)
        old_modulation = str(state.project.config.get("modulation") or "")
        if (
            "modulation" in state.coordination.locked_constraints
            and old_modulation
            and old_modulation != new_modulation
        ):
            return {
                "ok": False,
                "recipe": rc.name,
                "valid": False,
                "error": "PolicyGateway rejected a change to the locked modulation",
                "policy": "DENY",
            }
        current_recipe = str(state.project.config.get("recipe") or "")
        canvas_dirty = bool(state.project.config.get("canvas_dirty"))
        has_existing = bool(current_recipe or state.project.grc_path)
        recipe_changed = bool(current_recipe) and current_recipe != rc.name
        scope = (
            "multi_block_change"
            if has_existing and (recipe_changed or canvas_dirty or not current_recipe)
            else "new_flowgraph"
        )
        decision = gate(
            {"target": "flowgraph", "scope": scope, "domain": "dsp"},
            state.coordination,
        )
        pending = next(
            (
                item
                for item in reversed(state.coordination.pending_confirmations)
                if item.get("action") == "design_link"
                and item.get("recipe") == rc.name
            ),
            None,
        )
        if pending and pending.get("approved"):
            if ctx.platform is None:
                return {"ok": False, "error": "Platform is missing; the flowgraph cannot be built"}
            approved_pending = dict(pending)
            state.coordination.pending_confirmations.remove(pending)
            decision = ALLOW
        workflow = ctx.extra.get("workflow") or {}
        workflow_approved = any(
            (stage.get("checkpoint") or {}).get("decision_status") == "approved"
            for stage in workflow.get("stages") or []
            if isinstance(stage, dict)
            and stage.get("id") in ("change_confirmation", "repair_confirmation")
        )
        if decision != ALLOW and workflow_approved:
            proposed = list(ctx.extra.get("proposed_decisions") or [])
            if new_modulation and not any(
                item.get("key") == "modulation" for item in proposed
            ):
                proposed.append(
                    {"key": "modulation", "value": new_modulation, "source": "user"}
                )
            approved_pending = {
                "proposed_decisions": proposed
            }
            decision = ALLOW
        if decision != ALLOW:
            if decision in ("PROPOSE", "CONFIRM") and pending is None:
                proposed = list(ctx.extra.get("proposed_decisions") or [])
                if new_modulation and not any(
                    item.get("key") == "modulation" for item in proposed
                ):
                    proposed.append(
                        {
                            "key": "modulation",
                            "value": new_modulation,
                            "source": "user",
                        }
                    )
                state.coordination.pending_confirmations.append(
                    {
                        "action": "design_link",
                        "recipe": rc.name,
                        "from_recipe": current_recipe,
                        "policy": decision,
                        "approved": False,
                        "proposed_decisions": proposed,
                    }
                )
            return {
                "ok": False,
                "error": f"PolicyGateway rejected flowgraph construction: {decision}",
                "policy": decision,
                "recipe": rc.name,
                "from_recipe": current_recipe,
                "valid": False,
                "requires_confirmation": decision in ("PROPOSE", "CONFIRM"),
            }
    if ctx.platform is None:
        return {"ok": False, "error": "Platform is missing; the flowgraph cannot be built"}

    steps: List[dict] = []

    def _c(name, **kw):
        # ``design_flowgraph`` is the Stage-authorized macro.  Its primitive
        # build/validate/sim calls remain Gateway checked for effect and
        # requirements, but do not each need to be duplicated in every Stage
        # profile.
        marker = object()
        previous = ctx.extra.get("_gateway_parent_tool", marker)
        ctx.extra["_gateway_parent_tool"] = "design_flowgraph"
        try:
            r = registry.call(name, kw, ctx)
        finally:
            if previous is marker:
                ctx.extra.pop("_gateway_parent_tool", None)
            else:
                ctx.extra["_gateway_parent_tool"] = previous
        steps.append({"tool": name, "args": kw, "ok": bool(r.get("ok")),
                      "detail": r.get("error") or r.get("warning") or ""})
        return r

    # 1) 新建空流图(no_gui 便于无头仿真)
    r = _c("init_flow_graph", flowgraph_id=fid, generate_options="no_gui")
    if not r.get("ok"):
        return {"ok": False, "error": f"Initialization failed: {r.get('error')}",
                "steps": steps}

    # 2) probe 落盘路径：.grc 里写相对文件名，仿真读回仍用绝对路径。
    # TX-only recipes capture the transmitted waveform, so the file must not
    # be labelled as an RX probe.
    out_dir = ctx.out_dir or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    dtype = str(getattr(rc, "probe_dtype", "complex64") or "complex64")
    if str(rc.name).endswith("_tx"):
        probe_name = f"{fid}_tx.bin"
    elif dtype in {"uint8", "int8"}:
        probe_name = f"{fid}_bits.bin"
    else:
        probe_name = f"{fid}_iq.bin"
    tx_probe_name = f"{fid}_tx.bin"
    probe_path = os.path.join(out_dir, probe_name)
    tx_probe_path = os.path.join(out_dir, tx_probe_name)

    # 3) 逐块添加
    for key, bid, params in rc.blocks:
        p = dict(params)
        for k, v in list(p.items()):
            if v == "__PROBE__":
                p[k] = repr(probe_name)
            elif v == "__TX_PROBE__":
                p[k] = repr(tx_probe_name)
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
    render_ok = not render
    render_error = ""

    # 6) 存 .grc
    if render:
        rr = _c("render_grc")
        if rr.get("ok"):
            artifacts["grc_path"] = rr["path"]
            render_ok = True
        else:
            render_error = str(rr.get("error") or "render_grc failed")

    # 7) 可选仿真 + 取指标 + 画图
    if simulate and valid:
        probe_id = rc.probe_block_id or "sink"
        probes = {probe_id: [probe_path, rc.probe_dtype]}
        if rc.tx_probe_block_id:
            probes[rc.tx_probe_block_id] = [tx_probe_path, "uint8"]
        sim = _c("run_simulation", probes=probes)
        if sim.get("ok"):
            artifacts["out_dir"] = sim.get("out_dir")
            if sim.get("probes"):
                artifacts["probes"] = sim["probes"]
            if sim.get("probe_sizes"):
                artifacts["probe_sizes"] = sim["probe_sizes"]
            mod = _recipes.guess_modulation(rc.name)
            samp_rate = 1e6
            for _key, bid, params in rc.blocks:
                if bid == "samp_rate":
                    try:
                        samp_rate = float(str(params.get("value") or "1000000"))
                    except ValueError:
                        samp_rate = 1e6
                    break
            if "evm" in rc.metrics:
                m = _c("read_metric", kind="evm", probe_id=probe_id,
                       modulation=mod, sps=rc.sps)
                if m.get("ok"):
                    metrics["evm_pct"] = m["value"]
                    metrics["n_symbols"] = m.get("n_symbols")
                    metrics["evm_report"] = {
                        key: value for key, value in m.items()
                        if key not in {"ok", "kind"}
                    }
            if "ber" in rc.metrics:
                ber_sps = 1 if rc.probe_dtype in {"uint8", "int8"} else rc.sps
                m = _c("read_metric", kind="ber", probe_id=probe_id,
                       modulation=mod, sps=ber_sps,
                       tx_bits_probe=rc.tx_probe_block_id or "")
                if m.get("ok"):
                    metrics["ber"] = m["value"]
                    metrics["ber_report"] = {
                        key: value for key, value in m.items()
                        if key not in {"ok", "kind"}
                    }
            if "constellation" in rc.metrics:
                pc = _c("plot_constellation", probe_id=probe_id, sps=rc.sps,
                        modulation=mod)
                if pc.get("ok"):
                    artifacts["constellation_png"] = pc["path"]
            if "spectrum" in rc.metrics:
                ps = _c("plot_spectrum", probe_id=probe_id, samp_rate=samp_rate)
                if ps.get("ok"):
                    artifacts["spectrum_png"] = ps["path"]
            if "eye" in rc.metrics:
                pe = _c("plot_eye", probe_id=probe_id, sps=rc.sps)
                if pe.get("ok"):
                    artifacts["eye_png"] = pe["path"]

    out = {
        "ok": valid and render_ok,
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
    if render_error:
        out["error"] = render_error
    if state is not None and valid and render_ok:
        from ..state import ClaimStore
        from .state_tools import verify_state_claims

        if artifacts.get("grc_path"):
            state.project.grc_path = artifacts["grc_path"]
        state.project.config["recipe"] = rc.name
        state.project.config["modulation"] = _recipes.guess_modulation(rc.name)
        state.project.config["canvas_dirty"] = False
        from .state_tools import apply_proposed_decisions

        apply_proposed_decisions(
            state, (approved_pending or {}).get("proposed_decisions") or []
        )
        state.project.flowgraph_version += 1
        ClaimStore(state).invalidate_by_version(
            state.project.flowgraph_version
        )
        ctx.extra.setdefault("artifacts", {}).update(artifacts)
        ctx.extra.setdefault("metrics", {}).update(metrics)
        out["claim_updates"] = verify_state_claims(ctx, metrics)
    out["narrative"] = narrate_design(rc, out, profile)
    return out


def _profile_of(ctx):
    try:
        return ctx.extra.get("profile")
    except AttributeError:
        return None


@registry.tool(
    name="design_link",
    description=(
        "宏工具:按一句通信意图端到端搭出一张可跑流图(选配方→逐块建图→"
        "critic 自检→可选仿真取指标→存 .grc)。适合 BUILD 阶段一步到位;"
        "给 intent(自然语言)或 recipe(配方名,见 knowledge.recipes)之一。"),
    parameters={
        "type": "object",
        "properties": {
            "intent": {"type": "string",
                       "description": "自然语言意图,用于离线选配方"},
            "recipe": {"type": "string",
                       "description": "显式配方名,优先于 intent"},
            "simulate": {"type": "boolean",
                         "description": "建好后是否顺带仿真取指标,默认 true"},
            "render": {"type": "boolean",
                       "description": "是否存 .grc,默认 true"},
        },
    },
    group="macro",
    origin="deepradio_macro",
    runtime="deepradio",
    permission="project.write",
)
def design_link_tool(ctx, intent: str = "", recipe: str = "",
                     simulate: bool = True, render: bool = True) -> Dict[str, Any]:
    return design_link(ctx, _profile_of(ctx), intent=intent, recipe=recipe,
                       simulate=simulate, render=render)
