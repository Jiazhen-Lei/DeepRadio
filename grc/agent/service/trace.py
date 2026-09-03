"""Optional terminal trace for DeepAgents lifecycle events."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict

from langchain_core.callbacks import BaseCallbackHandler


_LOGGER = logging.getLogger("deepradio.trace")
if not _LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False


def trace_enabled() -> bool:
    return (os.environ.get("GRC_AGENT_TRACE") or "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _summary(value: Any) -> tuple[str, str]:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            content = {}
    if not isinstance(content, dict):
        return "finished", ""
    if str(content.get("policy") or "").upper() == "DENY":
        return "denied", str(content.get("error") or "")
    if content.get("ok") is False:
        return "failed", str(content.get("error") or "")
    return "finished", ""


def _safe_text(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]?\s*\S+",
        r"\1=<redacted>",
        text,
    )
    return text[:limit]


class AgentTraceCallback(BaseCallbackHandler):
    """Observe one Agent turn without changing graph state or results."""

    run_inline = True
    raise_error = False

    def __init__(
        self,
        *,
        session_id: str,
        context: Callable[[], Dict[str, Any]],
        emit: Callable[[str], None] | None = None,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        self.session_id = session_id
        self.turn_id = f"turn-{os.urandom(4).hex()}"
        self._context = context
        self._emit = emit or _LOGGER.info
        self._heartbeat_seconds = max(0.1, float(heartbeat_seconds))
        self._started = time.monotonic()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._finished = False

    def start(self) -> None:
        self._write("MainAgent", "TURN", "started")
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"deepradio-trace-{self.turn_id}",
            daemon=True,
        )
        self._thread.start()

    def finish(self, error: BaseException | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop.set()
        status = "failed" if error else "finished"
        self._write(
            "MainAgent",
            "TURN",
            status,
            _safe_text(error) if error else "",
            time.monotonic() - self._started,
        )

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], *,
        run_id: Any, parent_run_id: Any = None, tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        del serialized, messages, parent_run_id, tags, kwargs
        self._begin(run_id, self._component(metadata), "MODEL", "waiting")

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        del response, kwargs
        self._end(run_id, "finished")

    def on_llm_error(
        self, error: BaseException, *, run_id: Any, **kwargs: Any
    ) -> None:
        del kwargs
        self._end(run_id, "failed", _safe_text(error))

    def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, *, run_id: Any,
        parent_run_id: Any = None, tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        del input_str, parent_run_id, tags, kwargs
        name = str(serialized.get("name") or "tool")
        del inputs
        self._begin(run_id, self._component(metadata), "TOOL", name)

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        status, detail = _summary(output)
        self._end(run_id, status, _safe_text(detail))

    def on_tool_error(
        self, error: BaseException, *, run_id: Any, **kwargs: Any
    ) -> None:
        del kwargs
        self._end(run_id, "failed", _safe_text(error))

    def heartbeat(self) -> None:
        with self._lock:
            active = max(
                self._active.values(),
                key=lambda item: item["started"],
                default=None,
            )
        if active:
            self._write(
                active["component"], active["kind"], "running",
                active["name"], time.monotonic() - active["started"],
            )

    def _begin(self, run_id: Any, component: str, kind: str, name: str) -> None:
        item = {
            "component": component,
            "kind": kind,
            "name": name,
            "started": time.monotonic(),
        }
        with self._lock:
            self._active[str(run_id)] = item
        self._write(component, kind, "started", name)

    def _end(self, run_id: Any, status: str, detail: str = "") -> None:
        with self._lock:
            item = self._active.pop(str(run_id), None)
        if not item:
            return
        self._write(
            item["component"], item["kind"], status,
            detail or item["name"], time.monotonic() - item["started"],
        )

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            self.heartbeat()

    def _write(
        self, component: str, event: str, status: str,
        detail: str = "", elapsed: float | None = None,
    ) -> None:
        try:
            context = dict(self._context() or {})
            stage = str(context.get("stage_id") or "planning")
            duration = f" · {elapsed:.1f}s" if elapsed is not None else ""
            suffix = f" · {_safe_text(detail)}" if detail else ""
            timestamp = time.strftime("%H:%M:%S")
            self._emit(
                f"{timestamp} [DR {self.turn_id[5:]}] "
                f"{component} {event} {status}{suffix}{duration} · stage={stage}"
            )
        except Exception:  # Trace must never affect Agent execution.
            return

    @staticmethod
    def _component(metadata: dict[str, Any] | None) -> str:
        data = metadata or {}
        name = str(
            data.get("lc_agent_name")
            or data.get("agent_name")
            or data.get("langgraph_node")
            or "MainAgent"
        )
        return "MainAgent" if name in {"model", "tools"} else name


def build_trace_callback(
    *, session_id: str, context: Callable[[], Dict[str, Any]]
) -> AgentTraceCallback | None:
    return AgentTraceCallback(session_id=session_id, context=context) if trace_enabled() else None
