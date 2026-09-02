import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from grc.agent.state import SharedIntent, SharedState
from grc.agent.tools.registry import ToolContext
from grc.agent.tools.state_tools import spec_commit, spec_update


class RadioSpecificationTest(unittest.TestCase):
    def setUp(self):
        self.state = SharedState()
        self.state.intent = SharedIntent.new("Build a BLE transmitter", "wf-test")
        self.ctx = ToolContext(extra={"state": self.state})

    def test_required_field_must_be_aligned_before_commit(self):
        updated = spec_update(self.ctx, fields=[
            {
                "key": "goal",
                "label": "Goal",
                "value": "Build a BLE transmitter",
                "group": "required",
                "source": "user",
                "status": "aligned",
            },
            {
                "key": "sample_rate",
                "label": "Sample rate",
                "value": None,
                "group": "required",
                "source": "unresolved",
                "status": "missing",
            },
        ])

        self.assertTrue(updated["ok"])
        self.assertEqual(updated["unresolved_fields"][0]["key"], "sample_rate")
        self.assertFalse(spec_commit(self.ctx)["ok"])

        spec_update(self.ctx, fields=[{
            "key": "sample_rate",
            "value": 2_000_000,
            "status": "aligned",
            "source": "protocol_default",
        }])
        self.assertTrue(spec_commit(self.ctx)["ok"])
        self.assertEqual(self.state.intent.status, "confirmed")

    def test_added_field_must_come_from_user(self):
        result = spec_update(self.ctx, fields=[{
            "key": "carrier_frequency",
            "label": "Carrier",
            "value": 2_402_000_000,
            "group": "added",
            "source": "protocol_default",
            "status": "aligned",
        }])

        self.assertFalse(result["ok"])
        self.assertIn("Added field must come from the user", result["errors"][0])

    def test_digest_uses_the_specification_as_its_only_field_source(self):
        spec_update(self.ctx, fields=[
            {
                "key": "goal", "label": "Goal", "value": "BLE advertising",
                "group": "required", "source": "user", "status": "aligned",
            },
            {
                "key": "protocol", "label": "Protocol", "value": "ble",
                "group": "required", "source": "extracted", "status": "aligned",
            },
            {
                "key": "local_name", "label": "Advertising name", "value": "DeepRadio",
                "group": "added", "source": "extracted", "status": "aligned",
            },
        ])

        digest = self.state.spec_digest()
        rows = {item["key"]: item for item in digest["radio_specification"]}
        self.assertEqual(digest["protocol"], "ble")
        self.assertEqual(rows["goal"]["group"], "required")
        self.assertEqual(rows["local_name"]["group"], "added")

    def test_specification_round_trip_keeps_field_state(self):
        spec_update(self.ctx, fields=[{
            "key": "goal", "label": "Goal", "value": "BLE advertising",
            "group": "required", "source": "user", "status": "aligned",
        }])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.state.save(str(path))
            loaded = SharedState.load(str(path))

        field = loaded.intent.specification.field("goal")
        self.assertIsNotNone(field)
        self.assertEqual(field.status, "aligned")
        self.assertEqual(loaded.intent.specification.revision, 1)


if __name__ == "__main__":
    unittest.main()
