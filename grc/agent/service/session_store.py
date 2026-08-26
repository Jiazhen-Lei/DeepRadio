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
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

#: 会话虚拟路径前缀约定(与 backend 一致)。
WORK_PREFIX = "/session/work/"
FINAL_PREFIX = "/session/final/"

_EVENT_LOCKS: Dict[str, threading.Lock] = {}
_EVENT_LOCKS_GUARD = threading.Lock()


def _event_lock(path: str) -> threading.Lock:
    with _EVENT_LOCKS_GUARD:
        return _EVENT_LOCKS.setdefault(path, threading.Lock())


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


def workflow_path(session_id: str) -> str:
    return os.path.join(session_root(session_id), "workflow.yaml")


def archive_session(session_id: str, destination: str) -> str:
    """Copy a session tree and rewrite leftover absolute session paths."""
    source = os.path.abspath(session_root(session_id))
    dest = os.path.abspath(destination)
    if os.path.isdir(dest) and os.path.isfile(os.path.join(dest, "state.json")):
        raise ValueError(f"归档目标已存在会话文件: {dest}")
    if os.path.isdir(dest):
        dest = os.path.join(dest, _safe_component(session_id))
    if os.path.exists(dest):
        raise ValueError(f"归档目标已存在: {dest}")
    shutil.copytree(source, dest)
    from ..state.shared_state import relativize_tree_paths, rewrite_root_prefix

    for current_root, dirnames, names in os.walk(dest):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for name in names:
            path = os.path.join(current_root, name)
            if name.endswith(".jsonl"):
                _rewrite_jsonl_session_paths(path, source, dest)
                continue
            if not name.endswith((".json", ".yaml", ".yml")):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            rewritten = relativize_tree_paths(
                dest, rewrite_root_prefix(payload, source, dest)
            )
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(rewritten, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    append_session_event(session_id, "session_archived", {"destination": dest})
    return dest


def _rewrite_jsonl_session_paths(path: str, old_root: str, new_root: str) -> None:
    from ..state.shared_state import relativize_tree_paths, rewrite_root_prefix

    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    rewritten_lines = []
    changed = False
    for line in lines:
        raw = line.strip()
        if not raw:
            rewritten_lines.append(line)
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            rewritten_lines.append(line)
            continue
        updated = relativize_tree_paths(
            new_root, rewrite_root_prefix(payload, old_root, new_root)
        )
        encoded = json.dumps(updated, ensure_ascii=False)
        rewritten_lines.append(encoded + "\n")
        changed = changed or encoded != raw
    if not changed:
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.writelines(rewritten_lines)
    os.replace(tmp, path)


def nested_export_dir(session_id: str, base: str) -> str:
    """Put each session's export under ``<base>/<session_id>/``."""
    if not base:
        return ""
    safe = _safe_component(session_id)
    abs_base = os.path.abspath(base)
    if os.path.basename(abs_base) == safe:
        path = abs_base
    else:
        path = os.path.join(abs_base, safe)
    os.makedirs(path, exist_ok=True)
    return path


def resolve_session_path(session_id: str, path: str) -> str:
    if not path:
        return ""
    from ..state.shared_state import to_abspath

    return to_abspath(session_root(session_id), path)


def archive_workflow(session_id: str) -> str:
    """Archive the active workflow without touching project facts/artifacts."""
    source = workflow_path(session_id)
    if not os.path.isfile(source):
        return ""
    destination = os.path.join(
        session_root(session_id), f"workflow.archived.{int(time.time())}.yaml"
    )
    os.replace(source, destination)
    append_session_event(session_id, "workflow_archived", {"path": destination})
    return destination


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


_EVENT_CONTROL_KEYS = (
    "workflow_id",
    "workflow_revision",
    "task_type",
    "stage_id",
    "attempt",
    "profile_level",
)


def append_session_event(session_id: str, event: str,
                         payload: Dict[str, Any] | None = None) -> None:
    """向会话事件流 ``events.jsonl`` 追加一条事件(JSONL)。"""
    payload_data = _jsonable(payload or {})
    path = os.path.join(session_root(session_id), "events.jsonl")
    with _event_lock(path):
        record = {
            "event_id": f"evt-{uuid.uuid4().hex}",
            "session_id": session_id,
            "ts": time.time(),
            "seq": _next_event_seq(path),
            "event": event,
            "payload": payload_data,
        }
        for key in _EVENT_CONTROL_KEYS:
            value = payload_data.get(key)
            if value is not None:
                record[key] = value
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("写会话事件失败: %s", exc)


def _next_event_seq(path: str) -> int:
    if not os.path.isfile(path):
        return 1
    last = ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = line
    except OSError:
        return 1
    if not last:
        return 1
    try:
        data = json.loads(last)
        return int(data.get("seq") or 0) + 1
    except (TypeError, ValueError, json.JSONDecodeError):
        return 1


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


def recent_events(session_id: str, limit: int = 40) -> List[Dict[str, Any]]:
    """Return the newest session events for GUI timeline rendering."""
    path = os.path.join(session_root(session_id), "events.jsonl")
    if not os.path.isfile(path) or limit <= 0:
        return []
    lines: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
    except OSError:
        return []
    items: List[Dict[str, Any]] = []
    for line in lines[-int(limit):]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        items.append(
            {
                "seq": record.get("seq"),
                "event": record.get("event"),
                "ts": record.get("ts"),
                "workflow_id": record.get("workflow_id") or payload.get("workflow_id"),
                "stage_id": record.get("stage_id") or payload.get("stage_id"),
                "attempt": record.get("attempt") if record.get("attempt") is not None else payload.get("attempt"),
                "actor": _event_actor(payload),
                "mode": payload.get("mode") or payload.get("executor") or "",
                "ok": (
                    (payload.get("result") or {}).get("ok")
                    if isinstance(payload.get("result"), dict)
                    else payload.get("ok")
                ),
            }
        )
    return items


def _event_actor(payload: Dict[str, Any]) -> str:
    target = str(
        payload.get("target_agent")
        or payload.get("tool")
        or payload.get("source")
        or ""
    )
    origin = str(payload.get("origin") or "")
    mode = str(payload.get("mode") or payload.get("executor") or "")
    return " · ".join(part for part in (target, origin, mode) if part)


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


def export_artifact(source: str, destination_dir: str) -> str:
    """Copy a canonical session artifact to an optional user export folder."""
    if not source or not os.path.isfile(source) or not destination_dir:
        return ""
    os.makedirs(destination_dir, exist_ok=True)
    normalized = os.path.abspath(source)
    marker = f"{os.sep}final{os.sep}"
    relative = (
        normalized.split(marker, 1)[1]
        if marker in normalized
        else os.path.basename(source)
    )
    destination = os.path.join(destination_dir, relative)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if os.path.abspath(source) != os.path.abspath(destination):
        shutil.copy2(source, destination)
    return destination


def attach_evidence(
    session_id: str, source: str, run_id: str = ""
) -> Dict[str, Any]:
    """Copy a user-supplied evidence file into ``final/evidence/<run_id>/``."""
    if not source or not os.path.isfile(source):
        return {}
    safe_run = _safe_component(run_id) or "unbound"
    evidence_dir = os.path.join(
        session_root(session_id), "final", "evidence", safe_run
    )
    os.makedirs(evidence_dir, exist_ok=True)
    base = _safe_component(os.path.basename(source)) or "evidence"
    stem, suffix = os.path.splitext(base)
    destination = os.path.join(
        evidence_dir, f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
    )
    shutil.copy2(source, destination)
    digest = ""
    size = 0
    try:
        with open(destination, "rb") as handle:
            payload = handle.read()
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
    except OSError:
        size = os.path.getsize(destination)
    relative = os.path.relpath(destination, session_root(session_id))
    meta = {
        "run_id": run_id,
        "source_name": os.path.basename(source),
        "path": relative,
        "artifact": destination,
        "sha256": digest,
        "size": size,
        "observed_at": time.time(),
    }
    meta_path = os.path.join(
        evidence_dir, f"{os.path.splitext(os.path.basename(destination))[0]}.meta.json"
    )
    try:
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return meta


def write_artifact_manifest(
    session_id: str, artifacts: Dict[str, Any] | None = None
) -> str:
    """Write relocatable artifact references and content hashes for a session."""
    root = session_root(session_id)
    final_dir = os.path.join(root, "final")
    os.makedirs(final_dir, exist_ok=True)
    listed = []
    seen = set()
    for value in (artifacts or {}).values():
        if not isinstance(value, str) or not os.path.isfile(value):
            continue
        abs_path = os.path.abspath(value)
        if abs_path in seen or _skip_manifest_name(os.path.basename(abs_path)):
            continue
        seen.add(abs_path)
        listed.append(abs_path)
    roles = {
        os.path.abspath(value): key
        for key, value in (artifacts or {}).items()
        if isinstance(value, str) and os.path.isfile(value)
    }
    entries = []
    for path in listed:
        try:
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            size = os.path.getsize(path)
        except OSError:
            continue
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = os.path.basename(path)
        entries.append({
            "role": roles.get(os.path.abspath(path), "artifact"),
            "path": rel,
            "size": size,
            "sha256": digest,
        })
    path = os.path.join(final_dir, "manifest.json")
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "path_base": "session_root",
        "artifacts": entries,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def rewrite_exported_grc_paths(session_id: str, destination_dir: str) -> None:
    """Make exported GRC companion-artifact references relocatable."""
    source_final = os.path.join(session_root(session_id), "final")
    for name in os.listdir(destination_dir) if os.path.isdir(destination_dir) else []:
        if not name.endswith(".grc"):
            continue
        path = os.path.join(destination_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            rewritten = text.replace(f"{source_final}{os.sep}", "")
            rewritten = rewritten.replace(
                f"{os.path.abspath(destination_dir)}{os.sep}", ""
            )
            if rewritten != text:
                tmp = f"{path}.tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(rewritten)
                os.replace(tmp, path)
        except OSError:
            continue


def write_export_manifest(
    session_id: str,
    destination_dir: str,
    exported_paths: List[str] | None = None,
) -> str:
    """Create a manifest for this round's exported files only."""
    os.makedirs(destination_dir, exist_ok=True)
    destination = os.path.abspath(destination_dir)
    if exported_paths is None:
        candidates = []
        for current_root, dirnames, names in os.walk(destination):
            dirnames[:] = [name for name in dirnames if not _skip_manifest_name(name)]
            for name in sorted(names):
                candidates.append(os.path.join(current_root, name))
    else:
        candidates = list(exported_paths)
    entries = []
    seen_rel = set()
    for path in candidates:
        name = os.path.basename(path)
        if _skip_manifest_name(name) or not os.path.isfile(path):
            continue
        abs_path = os.path.abspath(path)
        if os.path.commonpath([destination, abs_path]) != destination:
            continue
        rel = os.path.relpath(abs_path, destination)
        if rel in seen_rel:
            continue
        try:
            with open(abs_path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            size = os.path.getsize(abs_path)
        except OSError:
            continue
        entries.append({
            "role": "artifact",
            "path": rel,
            "size": size,
            "sha256": digest,
        })
        seen_rel.add(rel)
    path = os.path.join(destination, "manifest.json")
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "path_base": "export_root",
        "artifacts": entries,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _skip_manifest_name(name: str) -> bool:
    return (
        name in {"manifest.json", "__pycache__"}
        or name.endswith(".pyc")
        or name.endswith(".pyo")
    )


def _jsonable(value: Any) -> Any:
    """把任意值收敛为可 JSON 序列化的结构(尽力而为)。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)
