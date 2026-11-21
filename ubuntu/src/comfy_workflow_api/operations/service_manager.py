"""Load the deployed Ubuntu service control entry point."""
import importlib.util


def manager_for(root):
    spec = importlib.util.spec_from_file_location('managed_comfyui_service', root/'scripts/comfyui-service.py')
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    return manager
