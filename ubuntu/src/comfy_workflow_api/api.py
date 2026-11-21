"""Public SDK configured for the managed Ubuntu backend."""
from .backend import OutputUnavailable
from .generation import WorkflowRuns

__all__ = ["WorkflowAPI", "OutputUnavailable"]


class WorkflowAPI(WorkflowRuns):
    def __init__(self, root=None, base_url=None, client=None, *, runtime_factory=None):
        if runtime_factory is None:
            from .operations.runtime import UbuntuGenerationRuntime
            runtime_factory = UbuntuGenerationRuntime
        super().__init__(root, base_url, client, runtime_factory)

    def cleanup(self, dry_run=False):
        from .operations.maintenance import cleanup
        return cleanup(self, dry_run=dry_run)
