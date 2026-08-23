"""Bounded subprocess runtime for explicitly enabled hardware flowgraphs."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict


class HardwareRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: Dict[str, Dict[str, Any]] = {}

    def start(self, session_id: str, program: str, duration: float) -> Dict[str, Any]:
        duration = max(1.0, min(float(duration), 60.0))
        with self._lock:
            self._reap_locked(session_id)
            if session_id in self._processes:
                return {"ok": False, "error": "该 session 已有硬件 Flowgraph 在运行"}
            process = subprocess.Popen(
                [program],
                cwd=str(Path(program).parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            record = {
                "process": process,
                "program": program,
                "started_at": time.time(),
                "duration": duration,
            }
            self._processes[session_id] = record
            timer = threading.Timer(duration, self.stop, args=(session_id,))
            timer.daemon = True
            record["timer"] = timer
            timer.start()
            return {
                "ok": True,
                "running": True,
                "pid": process.pid,
                "duration_seconds": duration,
                "program": program,
            }

    def status(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._processes.get(session_id)
            if not record:
                return {"ok": True, "running": False}
            process = record["process"]
            if process.poll() is not None:
                return self._reap_locked(session_id)
            return {
                "ok": True,
                "running": True,
                "pid": process.pid,
                "elapsed_seconds": time.time() - record["started_at"],
                "duration_seconds": record["duration"],
            }

    def stop(self, session_id: str, emergency: bool = False) -> Dict[str, Any]:
        with self._lock:
            record = self._processes.get(session_id)
            if not record:
                return {"ok": True, "running": False, "already_stopped": True}
            process = record["process"]
            timer = record.get("timer")
            if timer:
                timer.cancel()
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL if emergency else signal.SIGTERM)
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            return self._reap_locked(session_id, emergency=emergency)

    def _reap_locked(self, session_id: str, emergency: bool = False) -> Dict[str, Any]:
        record = self._processes.pop(session_id, None)
        if not record:
            return {"ok": True, "running": False}
        process = record["process"]
        output = ""
        if process.stdout:
            try:
                output = process.stdout.read(8192)
            except OSError:
                output = ""
        return {
            "ok": process.poll() is not None,
            "running": False,
            "return_code": process.poll(),
            "emergency": emergency,
            "output": output,
        }


RUNTIME = HardwareRuntime()
