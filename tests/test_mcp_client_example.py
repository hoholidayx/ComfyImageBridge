"""Check framework delivery and model projection using only a synthetic PNG."""
import base64
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent
from PIL import Image


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


class AcceptanceEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_edit_replay_and_cleanup_with_a_backend_without_seed(self):
        with patch.object(sys, "path", [str(EXAMPLES), *sys.path]):
            verifier = importlib.import_module("verify_image_mcp")
        server = MCPServer("acceptance-fixture")
        png = (EXAMPLES / "robot.png").read_bytes()
        palette = (EXAMPLES / "palette.png").read_bytes()
        with Image.open(EXAMPLES / "robot.png") as image:
            width, height = image.size
        requests = {}
        sessions = []

        def conflict(code):
            return CallToolResult(content=[], structured_content={"error_code": code}, is_error=True)

        @server.tool()
        def image_submit(request_id: str, prompt: str, preset: str, images: list[str] | None = None):
            request = (prompt, preset, tuple(base64.b64decode(image) for image in (images or [])))
            if requests.setdefault(request_id, request) != request:
                return conflict("request_conflict")
            return CallToolResult(content=[], structured_content={"state": "pending", "task_id": request_id})

        @server.tool()
        def image_status(task_id: str):
            if task_id not in requests:
                return conflict("task_not_found")
            return CallToolResult(content=[], structured_content={"state": "completed", "task_id": task_id})

        @server.tool()
        def image_result(task_id: str):
            return CallToolResult(
                content=[ImageContent(type="image", mime_type="image/png", data=base64.b64encode(png).decode())],
                structured_content={"task_id": task_id, "sha256": hashlib.sha256(png).hexdigest(),
                                    "width": width, "height": height},
            )

        def connection():
            sessions.append(None)
            return Client(server)

        with tempfile.TemporaryDirectory() as sandbox:
            with patch.object(verifier, "emit"), patch.object(tempfile, "tempdir", sandbox):
                await verifier.run(connection, SimpleNamespace(generate=False, edit=False, size=None, wait_seconds=5))
                self.assertEqual(requests, {})
                await verifier.run(connection, SimpleNamespace(generate=True, edit=True, size=None, wait_seconds=5))
            self.assertEqual(list(Path(sandbox).iterdir()), [])

        self.assertEqual(len(sessions), 8)
        self.assertEqual(len(requests), 2)
        self.assertEqual([request[2] for request in requests.values()], [(), (png, palette)])
