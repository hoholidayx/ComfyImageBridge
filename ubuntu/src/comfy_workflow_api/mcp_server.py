"""The optional Streamable HTTP adapter for persistent single-image jobs."""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
from io import BytesIO
import json
import os
import signal
import struct
from threading import Event, Thread
from typing import Literal
from urllib.parse import urlsplit
import zlib

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
import uvicorn

from .api import WorkflowAPI
from .client import png_size
from .contract import MAX_PROMPT_LENGTH, VERSION
from .image_jobs import (
    ImageJobs, ImageJobError, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, MAX_IMAGE_SIDE, MAX_IMAGE_TOTAL_BYTES, MAX_IMAGES,
)
from .settings import load_settings, mcp_listen, module_root


SERVICE = 'comfy-image-mcp'
MAX_HTTP_BODY = 48 * 1024**2
UUID_PATTERN = r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$'


class _Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class _SubmitArguments(_Arguments):
    request_id: str = Field(pattern=UUID_PATTERN, description='Stable UUID assigned by the host.')
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)
    preset: Literal['fast', 'quality'] = 'fast'
    size: str = Field(default='auto', max_length=16, description='auto, WxH or a supported aspect ratio.')
    seed: str | None = Field(default=None, pattern=r'^[0-9]{1,20}$', description='Optional uint64 decimal text.')
    images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES, description='Ordered base64 PNGs supplied by the host.')


class _TaskArguments(_Arguments):
    task_id: str = Field(pattern=UUID_PATTERN)


def _decode_pngs(images):
    decoded = []
    remaining = MAX_IMAGE_TOTAL_BYTES
    for encoded in images:
        limit = min(MAX_IMAGE_BYTES, remaining)
        if len(encoded) > 4 * ((limit + 2) // 3):
            raise ImageJobError('invalid_request', 'PNG payload exceeds the input size limit')
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ImageJobError('invalid_request', 'Each reference image must be base64 PNG') from exc
        if len(data) > limit:
            raise ImageJobError('invalid_request', 'PNG payload exceeds the input size limit')
        try:
            width, height = png_size(data)
            if not (0 < width <= MAX_IMAGE_SIDE and 0 < height <= MAX_IMAGE_SIDE
                    and width * height <= MAX_IMAGE_PIXELS):
                raise ValueError('PNG dimensions exceed the input size limit')
            with Image.open(BytesIO(data), formats=['PNG']) as image:
                if image.is_animated:
                    raise ValueError('Animated PNG is unsupported')
                image.verify()
            with Image.open(BytesIO(data), formats=['PNG']) as image:
                image.load()
        except (OSError, ValueError, SyntaxError, EOFError, struct.error, zlib.error, Image.DecompressionBombError) as exc:
            raise ImageJobError('invalid_request', 'Reference image must be a valid, bounded PNG') from exc
        decoded.append(data)
        remaining -= len(data)
    return tuple(decoded)


def _structured(data, *, error=False):
    return types.CallToolResult(
        content=[types.TextContent(type='text', text=json.dumps(data, ensure_ascii=False))],
        structured_content=data, is_error=error,
    )


def _error(code, message):
    return _structured({'error_code': code, 'message': message}, error=True)


class _CheckHTTPHeaders:
    """Apply exact Host and Origin validation to both health and MCP requests."""

    def __init__(self, app, *, hosts, origins):
        self.app = app
        self.hosts = frozenset(value.encode('ascii') for value in hosts)
        self.origins = frozenset(value.encode('ascii') for value in origins)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = scope['headers']
            hosts = [value for name, value in headers if name == b'host']
            origins = [value for name, value in headers if name == b'origin']
            if len(hosts) != 1 or hosts[0] not in self.hosts:
                return await PlainTextResponse('Invalid Host header', status_code=421)(scope, receive, send)
            if len(origins) > 1 or (origins and origins[0] not in self.origins):
                return await PlainTextResponse('Invalid Origin header', status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


def create_app(jobs, *, host, port):
    """Compose an MCP application and one job worker for the HTTP process."""
    hosts = [f'{host}:{port}']
    if port == 80:
        hosts.append(host)
    origins = [f'http://{authority}' for authority in hosts]
    worker = None
    stop = Event()
    upload_slots = asyncio.Semaphore(2)

    def submit(args):
        request = _SubmitArguments.model_validate(args)
        images = _decode_pngs(request.images)
        return _structured(jobs.submit(request.request_id, request.prompt, request.preset,
                                       request.size, request.seed, images))

    def status(args):
        request = _TaskArguments.model_validate(args)
        return _structured(jobs.status(request.task_id))

    def result(args):
        request = _TaskArguments.model_validate(args)
        metadata, png = jobs.result(request.task_id)
        return types.CallToolResult(
            structured_content=metadata,
            content=[types.ImageContent(type='image', mime_type='image/png', data=base64.b64encode(png).decode('ascii'))],
        )

    tools = {
        'image_submit': (submit, 'Submit one generated image. Use ordered reference PNGs to edit.',
                         _SubmitArguments.model_json_schema()),
        'image_status': (status, 'Read the status of a submitted image task.', _TaskArguments.model_json_schema()),
        'image_result': (result, 'Retrieve the verified PNG for a completed image task.',
                         _TaskArguments.model_json_schema()),
    }

    async def list_tools(_, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=name, description=description, input_schema=schema)
            for name, (_, description, schema) in tools.items()
        ])

    def input_schema(name):
        return tools[name][2] if name in tools else None

    async def call_tool(_, params):
        if params.name not in tools:
            raise MCPError(code=types.INVALID_PARAMS, message='Unknown image tool')
        handler = tools[params.name][0]
        try:
            if handler is submit:
                if worker is None or not worker.is_alive() or stop.is_set():
                    return _error('worker_unavailable', 'Image job worker is unavailable')
                async with upload_slots:
                    return await asyncio.to_thread(handler, params.arguments or {})
            return await asyncio.to_thread(handler, params.arguments or {})
        except ImageJobError as exc:
            return _error(exc.code, str(exc))
        except ValidationError:
            return _error('invalid_request', 'Arguments do not match the image tool schema')

    async def health(request: Request):
        healthy = worker is not None and worker.is_alive() and not stop.is_set()
        return JSONResponse({'status': 'ok' if healthy else 'unavailable', 'pid': os.getpid(), 'service': SERVICE},
                            status_code=200 if healthy else 503)

    server = Server(SERVICE, version=VERSION, on_list_tools=list_tools, on_call_tool=call_tool,
                    get_tool_input_schema=input_schema)
    app = server.streamable_http_app(
        streamable_http_path='/mcp', json_response=True, stateless_http=True, max_request_body_size=MAX_HTTP_BODY,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins),
        custom_starlette_routes=[Route('/healthz', health, methods=['GET'])],
    )
    http_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        nonlocal worker
        async with http_lifespan(application):
            stop.clear()
            worker = Thread(target=jobs.run, args=(stop,), name='image-jobs')
            worker.start()
            try:
                yield
            finally:
                stop.set()
                await asyncio.to_thread(worker.join)

    app.router.lifespan_context = lifespan
    app.add_middleware(_CheckHTTPHeaders, hosts=hosts, origins=origins)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='Ubuntu deployment checkout with config and workflow snapshots')
    parser.add_argument('--host', help='Override the configured explicit IPv4 address')
    parser.add_argument('--port', type=int, help='Override the configured port')
    args = parser.parse_args(argv)
    root = module_root(args.root)
    try:
        settings = load_settings(root)
        base_url = settings['base_url']
        if not isinstance(base_url, str):
            raise ValueError('The MCP service requires a configured loopback ComfyUI HTTP URL')
        target = urlsplit(base_url)
        if (target.scheme != 'http' or target.hostname not in ('127.0.0.1', 'localhost', '::1')
                or target.username is not None or target.query or target.fragment or target.port == 0):
            raise ValueError('The MCP service requires a configured loopback ComfyUI HTTP URL')
        config = settings.get('mcp', {})
        if isinstance(config, dict):
            overrides = {name: value for name, value in (('host', args.host), ('port', args.port)) if value is not None}
            config = {**config, **overrides}
        host, port = mcp_listen({'mcp': config})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    api = WorkflowAPI(root=root, base_url=base_url)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    jobs = ImageJobs(api)
    uvicorn.run(create_app(jobs, host=host, port=port), host=host, port=port, workers=1,
                lifespan='on', access_log=False, proxy_headers=False, limit_concurrency=8)


if __name__ == '__main__':
    main()
