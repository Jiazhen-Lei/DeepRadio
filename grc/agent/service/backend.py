"""backend:基于 deepagents 原生后端的会话文件系统装配。

对齐 ``local/docs/agent_architecture_deepagents.md`` 的设计:主 Agent 与各
subagent 共享一个 :class:`CompositeBackend`:

* ``default`` = :class:`StateBackend` —— 会话产物区(``/session/work``、
  ``/session/final`` 等)存于 LangGraph State 的 ``files`` 键,随 checkpointer
  持久化,支持断点续跑。
* ``routes["/workspace/skills/"]`` = :class:`FilesystemBackend` —— 只读挂载
  ``grc/agent/skills`` 下的 SKILL 目录,subagent 用内置文件工具 (``read_file``
  / ``glob`` / ``grep``) 按需读取 references,实现"渐进式披露"。

本模块不再自研虚拟文件系统 —— 全部交给 deepagents 现成后端;仅提供
``build_backend()`` 工厂与 ``skills_root()`` / ``list_skills()`` 辅助。
"""

from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger(__name__)

#: SKILL 目录在 backend 中的只读挂载前缀。
SKILLS_MOUNT = "/workspace/skills/"


def skills_root() -> str:
    """返回 SKILL 包根目录 ``grc/agent/skills`` 的绝对路径。

    本文件位于 ``grc/agent/service/backend.py``,上溯一级到 ``grc/agent``。
    """
    agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(agent_dir, "skills")


def list_skills() -> List[str]:
    """列出 ``skills`` 下所有含 ``SKILL.md`` 的技能目录名。"""
    root = skills_root()
    out: List[str] = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "SKILL.md")):
            out.append(name)
    return out


def build_backend():
    """组装 deepagents 的 :class:`CompositeBackend`。

    Returns:
        CompositeBackend 实例;会话产物走 State,``/workspace/skills/`` 只读
        映射到磁盘 ``skills``。

    Raises:
        ImportError: 未安装 deepagents 时向上抛出,由调用方决定降级。
    """
    from deepagents.backends import CompositeBackend, StateBackend
    from deepagents.backends.filesystem import FilesystemBackend

    routes = {}
    root = skills_root()
    if os.path.isdir(root):
        # virtual_mode=True:把 root 作为该路由的虚拟根,只读披露 references。
        routes[SKILLS_MOUNT] = FilesystemBackend(
            root_dir=root, virtual_mode=True)
    else:
        logger.warning("skills 目录不存在: %s(SKILL 只读挂载被跳过)", root)

    return CompositeBackend(default=StateBackend(), routes=routes)
