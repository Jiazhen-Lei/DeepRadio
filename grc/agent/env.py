"""环境引导:让桌面源码树的 GRC 跑在 conda 安装的 GNU Radio 运行时上。

背景
----
开发姿势是"源码版 GRC(main 分支) + conda 版 C++ 运行时(3.10.12)"，
这样不必源码编译 GNU Radio，但会产生两处不兼容，本模块负责消除:

1. 包名差异
   源码树里 GRC 是顶层包 ``grc``；发行版里是 ``gnuradio.grc``。
   main 分支的 ``*.workflow.yml`` 用 ``generator_module:
   gnuradio.grc.workflows.python_qt_gui`` 指定代码生成器，
   在混搭环境下这个模块不存在 -> ModuleNotFoundError。
   解决:把源码树的 ``grc`` 包在 ``sys.modules`` 里同时注册为 ``gnuradio.grc``。

2. 块定义来源
   conda 的 ``share/gnuradio/grc/blocks`` 有 580 个块但没有 ``*.workflow.yml``
   (workflow 是 main 分支新机制)。需要把源码树的 ``grc/blocks`` 一起纳入
   block_paths 才能拿到 9 个 workflow。

用法
----
    from grc.agent import env
    platform = env.make_platform()

或作为脚本自检::

    PYTHONPATH=~/Desktop/gnuradio python -m grc.agent.env
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)

#: workflow yml 里会引用到的生成器模块(相对 grc 包)
_WORKFLOW_MODULES = (
    "python_qt_gui",
    "python_nogui",
    "python_hb_qt_gui",
    "python_hb_nogui",
    "python_bokeh_gui",
    "cpp_qt_gui",
    "cpp_nogui",
    "cpp_hb_qt_gui",
    "cpp_hb_nogui",
)


def source_root() -> str:
    """返回 GRC 源码树根目录(即含 grc/ 的那一层)。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def bridge_package_name() -> None:
    """把源码树的 ``grc`` 注册为 ``gnuradio.grc``，使 workflow 生成器可导入。

    幂等，可重复调用。
    """
    import grc

    try:
        import gnuradio
    except ImportError:  # 没有安装 GNU Radio 运行时，无需桥接
        logger.debug("gnuradio 运行时不存在，跳过包名桥接")
        return

    if sys.modules.get("gnuradio.grc") is grc:
        return  # 已桥接

    sys.modules["gnuradio.grc"] = grc
    gnuradio.grc = grc

    import grc.workflows

    sys.modules["gnuradio.grc.workflows"] = grc.workflows

    for name in _WORKFLOW_MODULES:
        try:
            mod = importlib.import_module(f"grc.workflows.{name}")
        except ImportError:
            continue
        sys.modules[f"gnuradio.grc.workflows.{name}"] = mod

    # core 子包也一并桥接，覆盖 Generator.py 注释里提到的另一种写法
    for sub in ("core", "core.generator"):
        try:
            mod = importlib.import_module(f"grc.{sub}")
        except ImportError:
            continue
        sys.modules[f"gnuradio.grc.{sub}"] = mod

    logger.info("已桥接 gnuradio.grc -> %s", grc.__file__)


def block_paths(extra: Optional[List[str]] = None) -> List[str]:
    """返回块搜索路径。

    顺序即优先级(后者覆盖前者)。先放 conda 的 gr-* 块定义，
    再放源码树的核心块，使 options/workflow 等核心定义以源码版为准。
    """
    paths: List[str] = []

    try:
        from gnuradio import gr

        conda_blocks = os.path.join(
            gr.prefix(), "share", "gnuradio", "grc", "blocks"
        )
        if os.path.isdir(conda_blocks):
            paths.append(conda_blocks)
    except ImportError:
        logger.warning("gnuradio 运行时不存在，块库将不完整")

    src_blocks = os.path.join(source_root(), "grc", "blocks")
    if os.path.isdir(src_blocks):
        paths.append(src_blocks)

    if extra:
        paths.extend(extra)
    return paths


def make_platform(extra_block_paths: Optional[List[str]] = None,
                  quiet_overwrite: bool = True):
    """构造已载入块库的 Platform，供 Agent 与测试使用。

    Args:
        extra_block_paths: 追加的块搜索路径(如 OOT 模块)
        quiet_overwrite: 抑制"源码版块覆盖 conda 版块"的告警。
            混搭环境下这类覆盖是**预期行为**(见 block_paths 的顺序说明)，
            每次启动会产生数十条噪声，默认静音。
    """
    bridge_package_name()

    from grc.core.platform import Platform

    try:
        from gnuradio import gr

        version, prefix = gr.version(), gr.prefix()
    except ImportError:
        version, prefix = "3.10.0.0", "/usr/local"

    platform = Platform(
        name="GRC Agent", prefs=None, version=version, install_prefix=prefix
    )

    loader_log = logging.getLogger("grc.core.platform.block_loader")
    old_level = loader_log.level
    if quiet_overwrite:
        loader_log.setLevel(logging.ERROR)
    try:
        platform.build_library(block_paths(extra_block_paths))
    finally:
        loader_log.setLevel(old_level)
    return platform


def configure_options(flow_graph, output_language: str = "python",
                      generate_options: str = "no_gui",
                      flowgraph_id: Optional[str] = None) -> None:
    """安全地设置 options 块的输出语言、生成方式与流图 id。

    两个必须遵守的顺序约束(均在 main 分支实测踩到):

    1. ``output_language -> rewrite -> generate_options -> rewrite``
       ``rewrite()`` 会依据当前 ``output_language`` 重建 ``generate_options``
       的合法取值表。若在重建前写入 ``no_gui``，
       ``update_current_workflow()`` 找不到匹配项，会走"忽略
       generate_options"的回落分支，把值改回 ``qt_gui``。

    2. ``flowgraph_id`` 必须在上述 rewrite **之后**写入。
       ``options.rewrite()`` 切换 workflow 时会整体替换 ``self.params``，
       其 ``backup_params`` 只保留 title/author/copyright/description/
       output_language/generate_options 六项，**不含 id**。
       因此先写 id 会被 workflow 的默认 id 覆盖，
       导致生成的文件名变成 ``python_nogui_workflow.py``
       (文件名取自 ``get_option('id')``，见 grc/workflows/common.py:95)。

    Args:
        flow_graph: 目标 FlowGraph
        output_language: ``python`` 或 ``cpp``
        generate_options: ``no_gui`` / ``qt_gui`` / ``hb`` / ``bokeh_gui`` 等
        flowgraph_id: 流图 id，决定生成的文件名。None 表示不改。
    """
    ob = flow_graph.options_block
    if "output_language" in ob.params:
        ob.params["output_language"].set_value(output_language)
        ob.rewrite()
    ob.params["generate_options"].set_value(generate_options)
    ob.rewrite()

    actual = ob.params["generate_options"].get_value()
    if actual != generate_options:
        raise RuntimeError(
            f"generate_options 设置失败: 期望 {generate_options!r} 实际 {actual!r}。"
            f"可用 workflow: "
            f"{[w.id for w in getattr(ob, 'workflows', [])]}"
        )

    if flowgraph_id is not None:
        ob.params["id"].set_value(flowgraph_id)
        # 只让 Param 求值，不再触发 options.rewrite() 换 params 字典
        ob.params["id"].rewrite()
        if flow_graph.get_option("id") != flowgraph_id:
            raise RuntimeError(
                f"流图 id 设置失败: 期望 {flowgraph_id!r} "
                f"实际 {flow_graph.get_option('id')!r}"
            )


def selftest() -> int:
    """自检:验证混搭环境可用。返回 0 表示通过。"""
    logging.basicConfig(level=logging.WARNING)
    print(f"源码树       : {source_root()}")

    try:
        from gnuradio import gr

        print(f"运行时版本   : {gr.version()}")
        print(f"运行时前缀   : {gr.prefix()}")
    except ImportError:
        print("运行时       : 未安装(块库将不完整)")

    platform = make_platform()
    n_blocks = len(platform.blocks)
    n_wf = len(platform.workflow_manager.workflows)
    print(f"块库         : {n_blocks} 块 / {n_wf} workflow")

    ok = True
    if n_blocks < 300:
        print("  ! 块数偏少，检查 conda 环境的 share/gnuradio/grc/blocks")
        ok = False
    if n_wf == 0:
        print("  ! workflow 为 0，源码树的 grc/blocks 未被纳入")
        ok = False

    fg = platform.make_flow_graph()
    try:
        configure_options(fg, "python", "no_gui")
        print(f"options 配置 : OK -> {fg.options_block.current_workflow.id}")
    except Exception as exc:  # noqa: BLE001
        print(f"options 配置 : FAIL -> {exc}")
        ok = False

    print("自检结果     :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest())
