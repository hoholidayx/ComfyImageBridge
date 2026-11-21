"""ComfyUI connection settings and the host services used by generation."""
from collections.abc import Callable
from contextlib import AbstractContextManager
import os
from pathlib import Path
from typing import Protocol

from .client import Client
from .settings import load_settings, module_root


class BackendContext:
    """Connection and checkout paths shared by the SDK and operations commands."""

    def __init__(self, root=None, base_url=None, client=None):
        self.root = module_root(root)
        self.settings = load_settings(self.root)
        self.data = Path(self.settings['data_dir'])
        self.client = client or Client(base_url or os.environ.get('COMFYUI_URL') or self.settings['base_url'],
                                       self.settings['request_timeout'])


class BackendUnavailable(RuntimeError):
    """The host cannot accept a generation request."""


class OutputUnavailable(RuntimeError):
    """A completed workflow produced no saved image to fetch."""


class GenerationRuntime(Protocol):
    """Host services a workflow may use during submission, observation and result fetch."""

    def require_ready(self, check: Callable[[], bool] | None) -> None: ...
    def admission_ready(self, instance: str | None, check: Callable[[], bool] | None) -> bool: ...
    def submitting(self, task_id: str,
                   readiness: Callable[[str | None], bool] | None) -> AbstractContextManager[str | None]: ...
    def fetching(self, task_id: str) -> AbstractContextManager: ...
    def ticket(self, task_id: str, *, remove: bool = False) -> None: ...
    def retired(self, instance: str | None) -> dict | None: ...
    def invalidate(self, state: dict, reason: str) -> dict | None: ...
