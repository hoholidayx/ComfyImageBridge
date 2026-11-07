"""Minimal framework-side MCP integration, with an in-process offline demo."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from uuid import UUID, uuid4

from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent


def offline_server():
    """Use the shipped synthetic fixture and the real SDK, without network I/O."""
    server = MCPServer("offline-images-example")
    png = Path(__file__).with_name("robot.png").read_bytes()
    width, height = struct.unpack(">II", png[16:24])

    @server.tool(structured_output=True)
    def image_submit(request_id: str, prompt: str, images: list[str]) -> dict[str, str]:
        return {"task_id": request_id, "state": "pending"}

    @server.tool(structured_output=True)
    def image_status(task_id: str) -> dict[str, str]:
        return {"task_id": task_id, "state": "completed"}

    @server.tool()
    def image_result(task_id: str) -> CallToolResult:
        return CallToolResult(
            content=[ImageContent(type="image", data=base64.b64encode(png).decode(), mime_type="image/png")],
            structured_content={
                "task_id": task_id, "width": width, "height": height,
                "sha256": hashlib.sha256(png).hexdigest(), "seed_used": "0",
            },
        )

    return server


async def call(client, tool, arguments):
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        error = (result.structured_content or {}).get("error_code", "tool_failed")
        raise RuntimeError(f"{tool}: {error}")
    return result


def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        staging = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)


def record_completion(state_dir, conversation_id, task_id, result):
    metadata = result.structured_content
    (image,) = (block for block in result.content if isinstance(block, ImageContent))
    png = base64.b64decode(image.data, validate=True)
    if (image.mime_type != "image/png" or metadata["task_id"] != task_id
            or hashlib.sha256(png).hexdigest() != metadata["sha256"]):
        raise ValueError("Image result did not match its receipt")

    # The framework owns conversation scope. A stable task key replaces the same
    # record on retries, and other conversations resolve to separate directories.
    conversation = state_dir / hashlib.sha256(conversation_id.encode()).hexdigest()
    conversation.mkdir(parents=True, exist_ok=True)
    attachment_id = f"{UUID(task_id)}.png"
    receipt = {**metadata, "state": "completed", "attachment_id": attachment_id}
    atomic_write(conversation / attachment_id, png)
    atomic_write(conversation / f"{UUID(task_id)}.json", json.dumps(receipt).encode())
    return {"state": receipt["state"], "attachment_id": attachment_id}


async def run(args):
    endpoint = args.url or offline_server()
    task_id = args.task_id
    if task_id is None:
        request_id = args.request_id or str(uuid4())
        # A production framework persists this identifier and the conversation
        # before calling the tool. Pass the same --request-id to retry submission.
        print(f"request_id={request_id}", file=sys.stderr)
        images = [base64.b64encode(path.read_bytes()).decode() for path in args.image]
        async with Client(endpoint) as client:
            submitted = await call(client, "image_submit", {
                "request_id": request_id, "prompt": args.prompt, "images": images,
            })
            task_id = str(UUID(submitted.structured_content["task_id"]))
        print(f"task_id={task_id}", file=sys.stderr)

    # A new MCP connection can observe a task accepted on the first one.
    async with Client(endpoint) as client, asyncio.timeout(args.wait_seconds):
        while True:
            status = (await call(client, "image_status", {"task_id": task_id})).structured_content
            if status["state"] == "completed":
                break
            if status["state"] in {"failed", "expired"}:
                raise RuntimeError(status.get("error_code") or status["state"])
            await asyncio.sleep(2)
        result = await call(client, "image_result", {"task_id": task_id})
        projection = record_completion(args.state_dir, args.conversation, task_id, result)
    print(json.dumps(projection))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Explicit MCP URL. Submits real work when given.")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--conversation", default="synthetic-example")
    parser.add_argument("--prompt", default="A robot made of colorful geometric blocks")
    parser.add_argument("--image", type=Path, action="append", default=[])
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--request-id", type=lambda value: str(UUID(value)))
    identity.add_argument("--task-id", type=lambda value: str(UUID(value)))
    parser.add_argument("--wait-seconds", type=float, default=1200)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
