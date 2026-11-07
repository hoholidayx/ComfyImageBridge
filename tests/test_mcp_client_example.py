"""Check framework delivery and model projection using only a synthetic PNG."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "ubuntu" / "examples"


class ClientExampleTests(unittest.TestCase):
    def test_reconnect_and_replay_store_one_attachment_and_one_record(self):
        identifier = str(uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            command = [sys.executable, str(EXAMPLES / "mcp_client.py"), "--state-dir", temporary]
            for identity in ("--request-id", "--task-id"):
                result = subprocess.run(
                    [*command, identity, identifier], capture_output=True, text=True, check=True, timeout=10,
                )
                self.assertEqual(json.loads(result.stdout), {
                    "state": "completed", "attachment_id": identifier + ".png",
                })

            (attachment,) = directory.rglob("*.png")
            (record,) = directory.rglob("*.json")
            self.assertEqual(attachment.read_bytes(), (EXAMPLES / "robot.png").read_bytes())
            self.assertEqual(json.loads(record.read_text())["attachment_id"], attachment.name)
