import json
from .metadata import metadata
from .runtime import Runtime
from .schemas import SCHEMAS


def public_result(result):
    """Keep process/inhibitor internals in receipts instead of every model turn."""
    result = dict(result)
    if 'sleep_guard' in result:
        guard = result['sleep_guard']
        result['sleep_guard'] = {key: guard[key] for key in (
            'phase', 'running', 'inhibited', 'leases', 'queued_or_running', 'queue_error', 'log', 'observed_at', 'stale', 'lease_details', 'warnings') if key in guard}
    if 'service' in result:
        service = dict(result['service'])
        process = service.pop('process', {})
        listeners = service.pop('listeners', [])
        service['pid'] = process.get('pid')
        service['listener_pids'] = sorted({owner['pid'] for item in listeners for owner in item.get('owners',[item]) if owner.get('pid') is not None})
        service['unknown_owner_listener_count'] = sum(not item.get('owners') and item.get('pid') is None for item in listeners)
        result['service'] = service
    return result


def handle(ctx, name, args):
    try:
        if name not in SCHEMAS or not isinstance(args, dict):
            raise ValueError('Unknown runtime tool or invalid arguments')
        allowed = SCHEMAS[name]['parameters']['properties']
        if set(args)-set(allowed):
            raise ValueError('Unknown arguments; host, key, script paths and shell commands are not tool arguments')
        if name == 'comfy_service' and args.get('action') not in ('status', 'ensure_ready', 'stop', 'restart', 'recover'):
            raise ValueError('action must be status, ensure_ready, stop, restart or recover')
        from plugins.plugin_storage import plugin_data_dir
        result = Runtime(ctx, plugin_data_dir('ubuntu-runtime')).run('status' if name == 'ubuntu_status' else args['action'], operation_id=args.get('operation_id'), wait_seconds=args.get('wait_seconds',30))
    except Exception as exc:
        result = {'success': False, 'error': {'type': type(exc).__name__, 'message': str(exc),
                                           'retry_automatically': False}, 'metadata': metadata()}
    return json.dumps(public_result(result), ensure_ascii=False)


def register(ctx):
    for name, schema in SCHEMAS.items():
        def handler(args, _name=name, **kwargs):
            return handle(ctx, _name, args)
        ctx.register_tool(name=name, toolset='ubuntu_runtime', schema=schema, handler=handler,
                          description=schema['description'], emoji='🖥️')

    def setup(parser):
        parser.add_argument('tool', choices=list(SCHEMAS))
        parser.add_argument('--args', default='{}', help='JSON tool arguments')

    def command(args):
        result = ctx.dispatch_tool(args.tool, json.loads(args.args))
        print(result)
        return 0 if json.loads(result).get('success') else 1

    ctx.register_cli_command('ubuntu-runtime', 'Ubuntu power and ComfyUI lifecycle tools', setup, command)
