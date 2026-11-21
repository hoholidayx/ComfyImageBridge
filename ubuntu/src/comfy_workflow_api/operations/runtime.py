"""Bind generation to the Ubuntu admission gate, render evidence and inhibitor."""
from contextlib import contextmanager
import uuid

from .. import sleep_cycle, sleep_guard
from . import lifecycle, service_readiness
from ..backend import BackendUnavailable


class UbuntuGenerationRuntime:
    def __init__(self, backend):
        self.backend = backend

    def require_ready(self, check):
        sleep_cycle.assert_allowed(self.backend)
        if not (check() if check is not None else service_readiness.ready_instance(self.backend)):
            raise BackendUnavailable('ComfyUI is unavailable for generation')

    def admission_ready(self, instance, check):
        if check is not None:
            return bool(check())
        return instance is not None and service_readiness.ready_instance(self.backend) == instance

    @contextmanager
    def submitting(self, task_id, readiness):
        operation = str(uuid.uuid4())
        with lifecycle.submission(self.backend, task_id, operation, readiness) as instance:
            with sleep_guard.hold(self.backend, operation_id=operation, task_id=task_id,
                                  service_instance_id=instance, purpose='submit'):
                yield instance

    def fetching(self, task_id):
        return sleep_guard.hold(self.backend, task_id=task_id,
                                service_instance_id=lifecycle.instance_id(self.backend), purpose='fetch')

    def ticket(self, task_id, *, remove=False):
        sleep_guard.ticket(self.backend, task_id, remove=remove)

    def retired(self, instance):
        return lifecycle.retired(self.backend, instance)

    def invalidate(self, state, reason):
        return lifecycle.invalidate(self.backend, state, reason)
