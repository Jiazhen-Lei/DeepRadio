"""Bounded subprocess runtime for explicitly enabled hardware flowgraphs."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict


class HardwareRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: Dict[str, Dict[str, Any]] = {}
        self._last_results: Dict[str, Dict[str, Any]] = {}

    def start(
        self,
        session_id: str,
        program: str,
        duration: float,
        *,
        interpreter: str = "",
        startup_grace: float = 0.0,
    ) -> Dict[str, Any]:
        duration = max(1.0, min(float(duration), 60.0))
        with self._lock:
            existing = self._processes.get(session_id)
            if existing and existing["process"].poll() is not None:
                self._reap_locked(session_id)
            if session_id in self._processes:
                return {"ok": False, "error": "该 session 已有硬件 Flowgraph 在运行"}
            command = [interpreter, "-u", program] if interpreter else [program]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(Path(program).parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                return {
                    "ok": False,
                    "running": False,
                    "ready": False,
                    "error": f"硬件 Flowgraph 进程创建失败: {exc}",
                    "program": program,
                    "interpreter": interpreter,
                }
            record = {
                "process": process,
                "program": program,
                "interpreter": interpreter,
                "command": command,
                "run_id": f"run-{uuid.uuid4().hex[:12]}",
                "started_at": time.time(),
                "duration": duration,
                "output_chunks": [],
            }
            self._processes[session_id] = record
            self._last_results.pop(session_id, None)
            reader = threading.Thread(
                target=self._drain_output, args=(process, record), daemon=True
            )
            record["reader"] = reader
            reader.start()
            if startup_grace > 0:
                time.sleep(min(float(startup_grace), 2.0))
                if process.poll() is not None:
                    return self._reap_locked(session_id)
            timer = threading.Timer(duration, self.stop, args=(session_id,))
            timer.daemon = True
            record["timer"] = timer
            timer.start()
            return {
                "ok": True,
                "running": True,
                "pid": process.pid,
                "run_id": record["run_id"],
                "ready": True,
                "startup_health_passed": True,
                "started_at": record["started_at"],
                "deadline": record["started_at"] + duration,
                "duration_seconds": duration,
                "program": program,
                "interpreter": interpreter,
            }

    def status(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._processes.get(session_id)
            if not record:
                return dict(
                    self._last_results.get(session_id)
                    or {"ok": True, "running": False}
                )
            process = record["process"]
            if process.poll() is not None:
                return self._reap_locked(session_id)
            return {
                "ok": True,
                "running": True,
                "pid": process.pid,
                "run_id": record["run_id"],
                "ready": True,
                "elapsed_seconds": time.time() - record["started_at"],
                "duration_seconds": record["duration"],
                "max_duration_seconds": record["duration"],
                "started_at": record["started_at"],
                "deadline": record["started_at"] + record["duration"],
                "program": record.get("program"),
                "interpreter": record.get("interpreter"),
                "output": "".join(record.get("output_chunks") or [])[-8000:],
            }

    def stop(self, session_id: str, emergency: bool = False) -> Dict[str, Any]:
        with self._lock:
            record = self._processes.get(session_id)
            if not record:
                result = dict(
                    self._last_results.get(session_id)
                    or {"ok": True, "running": False}
                )
                result["already_stopped"] = True
                return result
            process = record["process"]
            timer = record.get("timer")
            if timer:
                timer.cancel()
            was_running = process.poll() is None
            if was_running:
                try:
                    os.killpg(process.pid, signal.SIGKILL if emergency else signal.SIGTERM)
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            return self._reap_locked(
                session_id,
                emergency=emergency,
                requested_stop=was_running,
            )

    @staticmethod
    def _drain_output(process: subprocess.Popen, record: Dict[str, Any]) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for chunk in iter(stream.readline, ""):
                record["output_chunks"].append(chunk)
                if sum(len(item) for item in record["output_chunks"]) > 65536:
                    record["output_chunks"] = record["output_chunks"][-128:]
        except (OSError, ValueError):
            return

    def _reap_locked(
        self,
        session_id: str,
        emergency: bool = False,
        requested_stop: bool = False,
    ) -> Dict[str, Any]:
        record = self._processes.get(session_id)
        if not record:
            return {"ok": True, "running": False}
        process = record["process"]
        if process.poll() is None:
            return {
                "ok": False,
                "running": True,
                "run_id": record.get("run_id"),
                "ready": True,
                "pid": process.pid,
                "error": "硬件 Flowgraph 未能进入终止状态",
            }
        self._processes.pop(session_id, None)
        reader = record.get("reader")
        if reader:
            reader.join(timeout=0.5)
        output = "".join(record.get("output_chunks") or [])[-65536:]
        if process.stdout:
            try:
                process.stdout.close()
            except OSError:
                pass
        return_code = process.poll()
        crashed = bool(
            not requested_stop
            and return_code is not None
            and return_code != 0
        )
        result = {
            "ok": bool(requested_stop or return_code == 0),
            "running": False,
            "run_id": record.get("run_id"),
            "ready": False,
            "return_code": return_code,
            "emergency": emergency,
            "crashed": crashed,
            "reason": (
                "emergency_stop"
                if emergency
                else "stopped"
                if requested_stop
                else "crashed"
                if crashed
                else "exited"
            ),
            "output": output,
            "program": record.get("program"),
            "interpreter": record.get("interpreter"),
            "started_at": record.get("started_at"),
            "deadline": record.get("started_at", 0) + record.get("duration", 0),
            "stopped_at": time.time(),
        }
        self._last_results[session_id] = dict(result)
        return result


RUNTIME = HardwareRuntime()
