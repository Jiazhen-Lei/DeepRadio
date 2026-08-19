"""memory:用户画像(创新 B 的名词料)。

创新 B 的数据核心在 :mod:`profile`——``UserProfile`` 贯穿所有 prompt 渲染,
实现"同一后端对小白/学生/专家三档自适应表达"。

子模块按需导入,避免在无 gnuradio 运行时的场景下过早加载依赖链。
"""

from __future__ import annotations


def __getattr__(name):
    if name in ("UserProfile", "infer_level_signals", "STYLE_GUIDE"):
        from . import profile
        return getattr(profile, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
