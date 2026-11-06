import http.client
import io
import json
import struct
import time
import urllib.parse
import uuid
from pathlib import Path


class _DeadlineRaw(io.RawIOBase):
    def __init__(self, raw, sock, deadline):
        self.raw, self._sock, self.deadline = raw, sock, deadline
    def readable(self):
        return True
    def readinto(self, buffer):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('HTTP headers/body exceeded total request deadline')
        self._sock.settimeout(remaining)
        return self.raw.readinto(buffer)
    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
    def __getattr__(self, name):
        return getattr(self.sock, name)
    def sendall(self, data, flags=0):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('HTTP request exceeded total request deadline')
        self.sock.settimeout(remaining)
        return self.sock.sendall(data, flags)
    def makefile(self, mode, *args, **kwargs):
        if mode != 'rb':
            raise ValueError('Only binary HTTP response reading is supported')
        return io.BufferedReader(_DeadlineRaw(self.sock.makefile('rb', buffering=0), self.sock, self.deadline))


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, deadline, **kwargs):
        self.deadline = deadline
        super().__init__(*args, **kwargs)
    def connect(self):
        self.timeout = max(.001,self.deadline-time.monotonic())
        super().connect()
        self.sock = _DeadlineSocket(self.sock,self.deadline)


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, deadline, **kwargs):
        self.deadline = deadline
        super().__init__(*args, **kwargs)
    def connect(self):
        self.timeout = max(.001,self.deadline-time.monotonic())
        super().connect()
        self.sock = _DeadlineSocket(self.sock,self.deadline)


class APIError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"ComfyUI HTTP {status}: {body}")


class Client:
    def __init__(self, base_url="http://127.0.0.1:8188", timeout=60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, path, method="GET", body=None, content_type=None, binary=False):
        headers = {}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        target = urllib.parse.urlsplit(self.base_url + path)
        if target.scheme not in ('http', 'https') or not target.hostname or target.username:
            raise ValueError('ComfyUI URL must be an HTTP(S) URL without credentials')
        deadline = time.monotonic() + self.timeout
        connection_type = _DeadlineHTTPSConnection if target.scheme == 'https' else _DeadlineHTTPConnection
        connection = connection_type(target.hostname, target.port, timeout=self.timeout, deadline=deadline)
        try:
            connection.request(method, urllib.parse.urlunsplit(('', '', target.path or '/', target.query, '')),
                               body=body, headers=headers)
            response = connection.getresponse()
            data = self._read(response, deadline)
            if response.status >= 300:
                raise APIError(response.status, data.decode(errors='replace'))
        finally:
            connection.close()
        return data if binary else json.loads(data)

    @staticmethod
    def _read(response, deadline):
        chunks = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('HTTP response exceeded total request deadline')
            # urllib's timeout normally bounds inactivity only. read1 performs a
            # single underlying read, allowing us to enforce the absolute budget
            # even if a peer trickles bytes indefinitely.
            try:
                response.fp.raw._sock.settimeout(remaining)
            except AttributeError:
                pass  # file-like test responses have no socket
            chunk = response.read1(65536)
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)

    def workflow(self, filename):
        return self.request("/userdata/" + urllib.parse.quote("workflows/" + filename, safe=""))

    def upload(self, path, task_id, index):
        data = Path(path).read_bytes()
        png_size(data)
        boundary = "comfy-workflow-" + uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{index}.png"\r\nContent-Type: image/png\r\n\r\n'.encode(), data, b"\r\n"]
        for key, value in (("type", "input"), ("overwrite", "false"), ("subfolder", "comfy-workflow-api/" + task_id)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f"--{boundary}--\r\n".encode())
        result = self.request("/upload/image", "POST", b"".join(parts), f"multipart/form-data; boundary={boundary}")
        return "/".join(x for x in (result.get("subfolder"), result["name"]) if x)


def png_size(data):
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("Only PNG images are supported")
    return struct.unpack(">II", data[16:24])
