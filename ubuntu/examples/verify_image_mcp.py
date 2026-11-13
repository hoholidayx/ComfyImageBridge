"""Verify a deployed asynchronous image MCP with synthetic inputs."""

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

from httpx2 import AsyncClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from mcp_client import call, record_completion


TOOLS = {"image_submit", "image_status", "image_result"}
TERMINAL_ERRORS = {"failed", "expired", "submission_uncertain"}
PROMPT = "A red cube and a blue sphere on a clean white background, simple geometric study."
EDIT_PROMPT = "Keep the original composition and shapes from <image1>. Apply the color palette in <image2>."
CONVERSATION = "synthetic-acceptance"


@asynccontextmanager
async def connect(url, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with AsyncClient(headers=headers, trust_env=False) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, read_timeout_seconds=15) as client:
            yield client


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def emit(**fields):
    print(json.dumps(fields), flush=True)


async def expect_error(client, tool, arguments, code):
    result = await client.call_tool(tool, arguments)
    actual = (result.structured_content or {}).get("error_code")
    require(result.is_error and actual == code, f"{tool}: expected {code}, got {actual}")


async def inventory(connection):
    async with connection() as client:
        tools = {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}
        require(TOOLS <= tools.keys(), "The server does not expose all three image tools")
        properties = tools["image_submit"]["properties"]
        require("n" not in properties, "The submit tool exposes an image count")
        await expect_error(client, "image_status", {"task_id": str(uuid4())}, "task_not_found")
        emit(tools=sorted(TOOLS), unknown_task="task_not_found")
        return properties


async def verify_job(connection, directory, request, *, label, size, wait_seconds):
    task_id = request["request_id"]
    key = {"task_id": task_id}
    print(f"{label}: request_id={task_id}", file=sys.stderr, flush=True)
    started = time.monotonic()
    async with connection() as client:
        receipt = (await call(client, "image_submit", request)).structured_content
        require(receipt["task_id"] == task_id, "Submission returned a different task ID")

    async with connection() as client, asyncio.timeout(wait_seconds):
        replay = (await call(client, "image_submit", request)).structured_content
        require(replay["task_id"] == task_id, "Replay returned a different task ID")
        await expect_error(client, "image_submit",
                           {**request, "prompt": request["prompt"] + " Use a grid."}, "request_conflict")
        last_state = None
        while True:
            status = (await call(client, "image_status", key)).structured_content
            require(status["task_id"] == task_id, "Status returned a different task ID")
            state = status["state"]
            if state != last_state:
                emit(case=label, state=state)
                last_state = state
            if state == "completed":
                break
            if state in TERMINAL_ERRORS:
                raise RuntimeError(f"{label}: {state} ({status.get('error_code') or state})")
            await asyncio.sleep(1)
        result = await call(client, "image_result", key)
        projection = record_completion(directory, CONVERSATION, task_id, result)

    async with connection() as client:
        again = await call(client, "image_result", key)
        require(again.structured_content == result.structured_content, "Result metadata changed on reconnect")
        require(record_completion(directory, CONVERSATION, task_id, again) == projection,
                "Attachment reference changed on reconnect")

    conversation = directory / hashlib.sha256(CONVERSATION.encode()).hexdigest()
    files = {path.name for path in conversation.glob(f"{task_id}.*")}
    require(files == {f"{task_id}.json", f"{task_id}.png"}, "Expected one receipt and one attachment")
    png = (conversation / projection["attachment_id"]).read_bytes()
    metadata = result.structured_content
    dimensions = (metadata["width"], metadata["height"])
    with Image.open(BytesIO(png), formats=["PNG"]) as image:
        image.load()
        require(image.size == dimensions, "Decoded PNG dimensions differ from the receipt")
        require(size is None or image.size == size, "Decoded PNG dimensions differ from the requested size")
        require(any(low != high for low, high in image.convert("RGB").getextrema()), "Decoded PNG is a solid color")
    if "seed" in request:
        require(metadata.get("seed_used") not in (None, request["seed"]), "Edit reused the reference seed")
    emit(case=label, width=dimensions[0], height=dimensions[1], seconds=round(time.monotonic() - started, 2),
         reconnect=True, request_conflict=True, deduplicated_attachment=True)
    return png, metadata.get("seed_used")


async def run(connection, args):
    properties = await inventory(connection)
    if not args.generate:
        return
    require(not args.edit or "images" in properties, "This endpoint does not support reference images")
    options = {"preset": "fast"}
    size = None
    if args.size:
        options["size"] = args.size
        size = tuple(map(int, args.size.split("x")))
    with tempfile.TemporaryDirectory(prefix="comfy-mcp-acceptance-") as temporary:
        directory = Path(temporary)
        original, seed = await verify_job(connection, directory,
                                          {"request_id": str(uuid4()), "prompt": PROMPT, **options},
                                          label="text-to-image", size=size, wait_seconds=args.wait_seconds)
        if args.edit:
            palette = Path(__file__).with_name("palette.png").read_bytes()
            images = [base64.b64encode(png).decode("ascii") for png in (original, palette)]
            request = {"request_id": str(uuid4()), "prompt": EDIT_PROMPT, "images": images, **options}
            if seed is not None and "seed" in properties:
                request["seed"] = seed
            await verify_job(connection, directory, request, label="image-edit",
                             size=size, wait_seconds=args.wait_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Full Streamable HTTP MCP endpoint URL")
    parser.add_argument("--token-env", help="Read the MCP bearer token from this environment variable")
    parser.add_argument("--generate", action="store_true", help="Submit a real synthetic generation request")
    parser.add_argument("--edit", action="store_true", help="Additionally submit a real two-reference edit")
    parser.add_argument("--size", help="Optional explicit WIDTHxHEIGHT")
    parser.add_argument("--wait-seconds", type=int, default=240, help="Maximum polling time per job")
    args = parser.parse_args()
    if args.edit and not args.generate:
        parser.error("--edit requires --generate")
    if args.size and not re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", args.size):
        parser.error("--size must be WIDTHxHEIGHT")
    if args.wait_seconds < 1:
        parser.error("--wait-seconds must be positive")
    try:
        url = urlsplit(args.url)
        _ = url.port  # Parse and validate the optional port.
    except ValueError:
        parser.error("--url has an invalid address or port")
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.query or url.fragment):
        parser.error("--url must be an HTTP(S) endpoint without inline credentials, query or fragment")
    token = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and not token:
        parser.error("The MCP bearer token environment variable is unset or empty")
    if token and url.scheme == "http" and url.hostname not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("A bearer token requires HTTPS or a loopback HTTP endpoint")

    try:
        asyncio.run(run(lambda: connect(args.url, token), args))
    except (RuntimeError, ValueError, TimeoutError) as exc:
        parser.exit(1, f"Image MCP verification failed: {str(exc) or type(exc).__name__}\n")


if __name__ == "__main__":
    main()
