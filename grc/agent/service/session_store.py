"""session_store:会话落盘镜像 + 事件流。

对齐 ``local/radiomaster_agents`` 的 ``storage/session_store`` 思路,但只用标准库:

* **落盘镜像** ``mirror_session_files``:把虚拟文件系统里的 ``path -> content``
  镜像到磁盘 ``local/agent_sessions/<session_id>/{work,final}/``,供回归、
  断点续跑与 GUI 读取产物。
* **会话事件流** ``append_session_event``:把主 Agent / subagent 的关键事件
  (委派、工具调用、产物发布)以 JSONL 追加到 ``events.jsonl``,作为 CHI 埋点
  与可复现实验的数据源。

会话根目录 ``local/agent_sessions/`` 应加入 .gitignore(运行期产物)。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

#: 会话虚拟路径前缀约定(与 backend 一致)。
WORK_PREFIX = "/session/work/"
FINAL_PREFIX = "/session/final/"


def sessions_root() -> str:
    """返回会话根目录 ``<project_root>/local/agent_sessions``(自动创建)。

    project_root 由本文件位置推算:grc/agent/service/session_store.py -> 上四级。
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    path = os.path.join(root, "local", "agent_sessions")
    os.makedirs(path, exist_ok=True)
    return path


def session_root(session_id: str) -> str:
    """某个会话的落盘根目录(自动创建)。"""
    safe = _safe_component(session_id) or "default"
    path = os.path.join(sessions_root(), safe)
    os.makedirs(path, exist_ok=True)
    return path


def state_path(session_id: str) -> str:
    return os.path.join(session_root(session_id), "state.json")


def snapshots_dir(session_id: str) -> str:
    path = os.path.join(session_root(session_id), "snapshots")
    os.makedirs(path, exist_ok=True)
    return path


def export_spec(session_id: str, destination: str) -> str:
    """Export only RadioSpec for explicit cross-session reuse."""
    from ..state import SharedState

    state = SharedState.load(state_path(session_id), session_id=session_id)
    payload = {"schema_version": 1, "spec": state.spec_digest()}
    parent = os.path.dirname(os.path.abspath(destination))
    os.makedirs(parent, exist_ok=True)
    tmp = f"{destination}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, destination)
    append_session_event(session_id, "spec_export", {"path": destination})
    return destination


def import_spec(session_id: str, source: str) -> None:
    """Import a validated RadioSpec without carrying project or claim state."""
    from ..state import Decision, RadioSpec, SharedState

    with open(source, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != 1 or not isinstance(
        payload.get("spec"), dict
    ):
        raise ValueError("不支持的 Spec 文件格式")
    data = payload["spec"]
    state = SharedState.load(state_path(session_id), session_id=session_id)
    state.spec = RadioSpec(
        goals=list(data.get("goals") or []),
        success_conditions=list(data.get("success_conditions") or []),
        constraints=dict(data.get("constraints") or {}),
        decisions=[Decision(**item) for item in data.get("decisions") or []],
        open_questions=list(data.get("open_questions") or []),
    )
    state.save(state_path(session_id))
    append_session_event(session_id, "spec_import", {"path": source})


def _safe_component(name: str) -> str:
    """把 session_id 收敛为安全的单层目录名。"""
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in str(name)]
    return "".join(keep)[:120]


def _virtual_to_disk(session_id: str, virtual_path: str) -> str | None:
    """把 ``/session/work/...`` 或 ``/session/final/...`` 映射到磁盘绝对路径。

    非受管前缀返回 None(不落盘,避免越权写任意路径)。
    """
    root = session_root(session_id)
    for prefix, sub in ((WORK_PREFIX, "work"), (FINAL_PREFIX, "final")):
        if virtual_path.startswith(prefix):
            rel = virtual_path[len(prefix):].lstrip("/")
            # 归一化,禁止 .. 逃逸
            rel = os.path.normpath(rel)
            if rel.startswith("..") or os.path.isabs(rel):
                return None
            return os.path.join(root, sub, rel)
    return None


def mirror_session_files(session_id: str,
                         files: Dict[str, Any]) -> List[str]:
    """把虚拟文件系统 ``{path: content|{"content":...}}`` 镜像到磁盘。

    Args:
        session_id: 会话 id。
        files: 虚拟文件字典;值可以是 str,也可以是 ``{"content": str, ...}``。

    Returns:
        实际写盘的磁盘路径列表。
    """
    written: List[str] = []
    for vpath, value in (files or {}).items():
        if not isinstance(vpath, str):
            continue
        disk = _virtual_to_disk(session_id, vpath)
        if disk is None:
            continue
        content = _extract_content(value)
        try:
            os.makedirs(os.path.dirname(disk), exist_ok=True)
            if isinstance(content, bytes):
                with open(disk, "wb") as fh:
                    fh.write(content)
            else:
                with open(disk, "w", encoding="utf-8") as fh:
                    fh.write(content if isinstance(content, str)
                             else str(content))
            written.append(disk)
        except OSError as exc:
            logger.warning("镜像文件 %s 失败: %s", vpath, exc)
    return written


def _extract_content(value: Any):
    """从虚拟文件值里取内容(兼容 str 与 {"content": ...} 两种形态)。"""
    if isinstance(value, dict):
        return value.get("content", "")
    return value


def append_session_event(session_id: str, event: str,
                         payload: Dict[str, Any] | None = None) -> None:
    """向会话事件流 ``events.jsonl`` 追加一条事件(JSONL)。"""
    record = {
        "ts": time.time(),
        "event": event,
        "payload": _jsonable(payload or {}),
    }
    path = os.path.join(session_root(session_id), "events.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("写会话事件失败: %s", exc)


def scan_final_artifacts(session_id: str,
                         files: Dict[str, Any] | None = None) -> Dict[str, str]:
    """扫描 ``final/`` 下的产物,返回 ``{文件名: 磁盘路径}``。

    优先扫描虚拟文件字典里 ``/session/final/`` 前缀的项(镜像前);若未提供
    files,则直接扫描磁盘 ``<session>/final/``(镜像后)。
    """
    out: Dict[str, str] = {}
    if files:
        for vpath in files:
            if isinstance(vpath, str) and vpath.startswith(FINAL_PREFIX):
                disk = _virtual_to_disk(session_id, vpath)
                if disk:
                    out[os.path.basename(disk)] = disk
        if out:
            return out
    final_dir = os.path.join(session_root(session_id), "final")
    if os.path.isdir(final_dir):
        for name in sorted(os.listdir(final_dir)):
            full = os.path.join(final_dir, name)
            if os.path.isfile(full):
                out[name] = full
    return out


def publish_artifact(session_id: str, source: str) -> str:
    """Mirror a user-visible artifact into the session final directory."""
    if not source or not os.path.isfile(source):
        return source
    final_dir = os.path.join(session_root(session_id), "final")
    os.makedirs(final_dir, exist_ok=True)
    destination = os.path.join(final_dir, os.path.basename(source))
    if os.path.abspath(source) != os.path.abspath(destination):
        shutil.copy2(source, destination)
    return destination


def _jsonable(value: Any) -> Any:
    """把任意值收敛为可 JSON 序列化的结构(尽力而为)。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)
