from __future__ import annotations

import os
import unittest
from unittest import mock
from uuid import uuid4

from grc.agent.service.trace import AgentTraceCallback, build_trace_callback


class AgentTraceTest(unittest.TestCase):
    def test_lifecycle_is_compact_and_does_not_expose_inputs(self):
        lines = []
        trace = AgentTraceCallback(
            session_id="trace-test",
            context=lambda: {"stage_id": "design"},
            emit=lines.append,
            heartbeat_seconds=60,
        )
        model_run = uuid4()
        tool_run = uuid4()
        trace.start()
        trace.on_chat_model_start(
            {}, [[object()]], run_id=model_run, metadata={"lc_agent_name": "flowgraph_agent"}
        )
        trace.heartbeat()
        trace.on_llm_end(object(), run_id=model_run)
        trace.on_tool_start(
            {"name": "apply_flowgraph_patch"},
            "api_key=secret",
            run_id=tool_run,
            inputs={"api_key": "secret"},
        )
        trace.on_tool_end(
            '{"ok": false, "error": "invalid operation"}', run_id=tool_run
        )
        trace.finish()

        output = "\n".join(lines)
        self.assertIn("flowgraph_agent MODEL running", output)
        self.assertIn("apply_flowgraph_patch", output)
        self.assertIn("failed · invalid operation", output)
        self.assertIn("stage=design", output)
        self.assertNotIn("secret", output)

    def test_trace_can_be_disabled_without_affecting_the_caller(self):
        with mock.patch.dict(os.environ, {"GRC_AGENT_TRACE": "0"}):
            self.assertIsNone(
                build_trace_callback(session_id="off", context=lambda: {})
            )


if __name__ == "__main__":
    unittest.main()
