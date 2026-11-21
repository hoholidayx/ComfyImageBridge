from .client import APIError, Client

__all__ = ["WorkflowAPI", "Client", "APIError"]


def __getattr__(name):
    if name == "WorkflowAPI":
        from .api import WorkflowAPI
        return WorkflowAPI
    raise AttributeError(name)
