import asyncio
import base64
import hashlib
from importlib.util import find_spec
from io import BytesIO
import unittest
from unittest.mock import patch
from uuid import uuid4


MCP_INSTALLED = find_spec('mcp') is not None
if MCP_INSTALLED:
    from httpx2 import ASGITransport, AsyncClient
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from PIL import Image
    from comfy_workflow_api.image_jobs import ImageJobError
    from comfy_workflow_api.mcp_server import MAX_HTTP_BODY, create_app


def png_bytes(color):
    output = BytesIO()
    with Image.new('RGB', (4, 4), color) as image:
        image.save(output, format='PNG')
    return output.getvalue()


class FakeImageJobs:
    def __init__(self):
        self.received = []
        self.error = None
        self.png = png_bytes('orange')

    def run(self, stop):
        stop.wait()

    def submit(self, request_id, prompt, preset, size, seed, images):
        if self.error:
            raise self.error
        self.received.append((request_id, prompt, preset, size, seed, images))
        return {'task_id': request_id, 'state': 'queued'}

    def status(self, task_id):
        return {'task_id': task_id, 'state': 'completed'}

    def result(self, task_id):
        return ({'task_id': task_id, 'width': 4, 'height': 4, 'sha256': hashlib.sha256(self.png).hexdigest(),
                 'seed_used': '18446744073709551615'}, self.png)


@unittest.skipUnless(MCP_INSTALLED, 'requires the optional mcp extra')
class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.jobs = FakeImageJobs()
        self.base = 'http://127.0.0.1:8787'
        self.app = create_app(self.jobs, host='127.0.0.1', port=8787)
        started, stopped = asyncio.Event(), asyncio.Event()

        async def lifespan():
            async with self.app.router.lifespan_context(self.app):
                started.set()
                await stopped.wait()

        task = asyncio.create_task(lifespan())

        async def close():
            stopped.set()
            await task

        self.addAsyncCleanup(close)
        await asyncio.wait_for(started.wait(), 5)
        self.http = await self.enterAsyncContext(
            AsyncClient(transport=ASGITransport(self.app), base_url=self.base, trust_env=False),
        )

    def client(self, mode='auto'):
        return Client(streamable_http_client(f'{self.base}/mcp', http_client=self.http), mode=mode)

    async def test_tool_discovery_ordered_references_and_reconnect(self):
        request_id = str(uuid4())
        references = (png_bytes('red'), png_bytes('blue'))
        async with self.client() as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual(set(tools), {'image_submit', 'image_status', 'image_result'})
            submit_schema = tools['image_submit'].input_schema
            self.assertNotIn('n', submit_schema['properties'])
            self.assertFalse(submit_schema['additionalProperties'])
            response = await client.call_tool('image_submit', {
                'request_id': request_id, 'prompt': 'Change the colors', 'preset': 'quality', 'size': 'auto',
                'images': [base64.b64encode(png).decode('ascii') for png in references],
            })
            self.assertFalse(response.is_error)
            self.assertEqual(response.structured_content, {'task_id': request_id, 'state': 'queued'})
        self.assertEqual(self.jobs.received, [(request_id, 'Change the colors', 'quality', 'auto', None, references)])
        async with self.client() as client:
            status = await client.call_tool('image_status', {'task_id': request_id})
            self.assertEqual(status.structured_content, {'task_id': request_id, 'state': 'completed'})
            result = await client.call_tool('image_result', {'task_id': request_id})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content['seed_used'], '18446744073709551615')
            self.assertEqual(len(result.content), 1)
            self.assertEqual(result.content[0].mime_type, 'image/png')
            self.assertEqual(base64.b64decode(result.content[0].data), self.jobs.png)

    async def test_invalid_arguments_and_backend_error_are_structured(self):
        async with self.client() as client:
            arguments = {'request_id': str(uuid4()), 'prompt': 'Geometric shapes'}
            for bad in ({'n': 4}, {'images': ['%%%']}, {'seed': 42},
                        {'images': [base64.b64encode(b'not a PNG').decode('ascii')]},
                        {'images': [base64.b64encode(png_bytes('black')[:32]).decode('ascii')]}):
                result = await client.call_tool('image_submit', arguments | bad)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error_code'], 'invalid_request')
            self.jobs.error = ImageJobError('backend_unavailable', 'ComfyUI is unavailable')
            result = await client.call_tool('image_submit', arguments)
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content['error_code'], 'backend_unavailable')
        self.assertEqual(self.jobs.received, [])

    async def test_health_origin_host_and_body_limits(self):
        healthy = await self.http.get('/healthz')
        self.assertEqual(healthy.status_code, 200)
        self.assertEqual(healthy.json()['service'], 'comfy-image-mcp')
        self.assertEqual((await self.http.get('/healthz', headers={'Host': 'untrusted.test'})).status_code, 421)
        self.assertEqual((await self.http.get('/healthz', headers={'Origin': 'null'})).status_code, 403)
        self.assertEqual((await self.http.post('/mcp', headers={'Origin': 'http://untrusted.test'})).status_code, 403)
        self.assertEqual((await self.http.post('/mcp', headers={'Content-Length': str(MAX_HTTP_BODY + 1)})).status_code,
                         413)

    async def test_body_limit_also_applies_without_content_length(self):
        with patch('comfy_workflow_api.mcp_server.MAX_HTTP_BODY', 128):
            app = create_app(self.jobs, host='127.0.0.1', port=8787)

        async def body():
            yield b' ' * 100
            yield b' ' * 100

        async with AsyncClient(transport=ASGITransport(app), base_url=self.base, trust_env=False) as client:
            response = await client.post('/mcp', content=body(), headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 413)

    async def test_legacy_client_can_discover_tools(self):
        async with self.client(mode='legacy') as client:
            self.assertEqual(len((await client.list_tools()).tools), 3)
