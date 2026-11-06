import json
from .schemas import SCHEMAS
from .transport import Transport
from .metadata import client_metadata


def validate_generation(args):
    if not isinstance(args.get('prompt'), str) or not args['prompt'].strip():
        raise ValueError('prompt must be a nonempty string')
    if len(args['prompt']) > 8000:
        raise ValueError('prompt exceeds 8000 characters; never truncated')
    if args.get('preset', 'fast') not in ('fast', 'quality'):
        raise ValueError('preset must be fast or quality')
    count, seed = args.get('count', 1), args.get('seed', 0)
    if type(count) is not int or not 1 <= count <= 4:
        raise ValueError('count must be 1..4')
    if type(seed) is not int or seed < 0 or seed+count-1 >= 2**64:
        raise ValueError('Invalid unsigned 64-bit seed range; default is fixed 0')


def public_result(value):
    """Keep reproduction internals in receipts, not in the model's tool context."""
    if isinstance(value, list):
        return [public_result(item) for item in value]
    if isinstance(value, dict):
        return {key: public_result(item) for key, item in value.items()
                if key not in ('model_config', 'original_request', 'input_images', 'submitted_metadata')}
    return value


def _handle_legacy(ctx, name, args):
    from plugins.plugin_storage import plugin_data_dir
    try:
        if not isinstance(args, dict) or set(args) - set(SCHEMAS[name]['parameters']['properties']):
            raise ValueError('Unknown/removed arguments. Use preset=fast (LoRA,6) or quality (no LoRA,25 steps for both generation and editing, following ComfyUI official templates). CFG is fixed at 1; steps/use_lora/cfg/negative_prompt overrides are disabled.')
        transport = Transport(ctx, plugin_data_dir('comfy-workflows'))
        if name == 'comfy_tasks':
            if args.get('action', 'list') != 'list':
                raise ValueError('Only action=list is supported')
            return json.dumps({'success': True, 'tasks': transport.history(args.get('limit', 20)),
                               'metadata': {**client_metadata(), 'stale_copy': None, 'offline': True}}, ensure_ascii=False)
        if name == 'comfy_workflows':
            response = transport.call({'operation': 'info'})
        elif name in ('comfy_generate', 'comfy_edit'):
            request = dict(args)
            validate_generation(request)
            count = request.pop('count', 1)
            blobs = transport.images(request.pop('images')) if name == 'comfy_edit' else []
            request.setdefault('output_size', {'mode': 'follow_image'} if name == 'comfy_edit' else {'mode': 'explicit', 'width': 1024, 'height': 1024})
            response = transport.call({'operation': 'submit', 'workflow': 'qwen-image-2.1-edit' if name == 'comfy_edit' else 'qwen-image-2.1-t2i', 'request': request, 'images': blobs, 'count': count})
            if response['success']:
                task = response['result']
                input_images = transport.save_inputs(task['task_id'], blobs)
                canonical = dict(task['request_used'])
                canonical['count'] = count
                if input_images:
                    canonical['images'] = [image['path'] for image in input_images]
                task.update(original_request=args, request_used=canonical, input_images=input_images,
                            submitted_metadata=response.get('metadata'))
                transport.record(task['task_id'], task)
                response = {'success': True, **task, 'metadata': response.get('metadata'), 'next': 'Call comfy_task action=result wait_seconds=30 with this task_id; never re-submit to poll.'}
            elif response.get('error', {}).get('task_id'):
                task_id = response['error']['task_id']
                input_images = transport.save_inputs(task_id, blobs)
                transport.record(task_id, {'original_request': args, 'input_images': input_images,
                                          'submitted_metadata': response.get('metadata'), 'state': 'submission_uncertain'})
        else:
            task_id = args['task_id']
            if not (transport.directory(task_id) / 'receipt.json').is_file():
                raise ValueError('Task is not registered in this Hermes profile')
            response = transport.call({'operation': 'task', 'action': args.get('action', 'status'),
                                       'task_id': task_id, 'wait_seconds': args.get('wait_seconds', 0)})
            if response['success']:
                result = response['result']
                from pathlib import Path
                prior = json.loads((transport.directory(task_id)/'receipt.json').read_text())
                if 'request_used' in prior:
                    result['request_used'] = prior['request_used']
                elif result.get('request_used') and prior.get('input_images'):
                    result['request_used']['images'] = [image['path'] for image in prior['input_images']]
                transport.record(task_id, {key: value for key, value in result.items() if key != 'outputs'})
                meta = response.get('metadata')
                response = transport.download(result) if result.get('outputs') else {'success': True, **result}
                response['metadata'] = meta
        if response.get('task_id'):
            response['receipt_path'] = str(transport.directory(response['task_id'])/'receipt.json')
        return json.dumps(public_result(response), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'success': False, 'error': {'type': type(exc).__name__, 'message': str(exc), 'retry_submission': False},
                           'metadata': {**client_metadata(), 'stale_copy': None}}, ensure_ascii=False)


def handle(ctx, name, args):
    """One model-facing entry; actual orchestration runs outside the agent loop."""
    from plugins.plugin_storage import plugin_data_dir
    try:
        if name not in SCHEMAS or not isinstance(args, dict):
            raise ValueError('Unknown tool or invalid arguments')
        if set(args) - set(SCHEMAS[name]['parameters']['properties']):
            raise ValueError('Unknown arguments; use prompt, preset, images, size, title, seed and count')
        if name == 'comfy_image' or name == 'comfy_task':
            from .jobs import JobService
            from .delivery import capture_origin
            service = JobService(ctx, plugin_data_dir('comfy-workflows'))
            origin = capture_origin(ctx)
            if name == 'comfy_image':
                if hasattr(ctx, 'has_plugin') and not ctx.has_plugin('ubuntu-runtime'):
                    raise ValueError('Enable the existing ubuntu-runtime plugin before generating')
                result = service.create(args, origin)
            elif args.get('action', 'status') == 'list':
                result = service.list(query=args.get('query', ''), limit=args.get('limit', 20), origin=origin)
            else:
                task_id = args.get('task_id')
                if not task_id:
                    raise ValueError('task_id is required except for action=list')
                if service.store.get_job(task_id) is None:
                    return _handle_legacy(ctx, name, args)
                result = (service.cancel(task_id, origin=origin) if args.get('action') == 'cancel'
                          else service.status(task_id, origin=origin))
            return json.dumps({**_public_job(result), 'metadata': {**client_metadata(), 'stale_copy': None}}, ensure_ascii=False)
        return _handle_legacy(ctx, name, args)
    except Exception as exc:
        return json.dumps({'success': False, 'error': {'type': type(exc).__name__, 'message': str(exc),
                           'retry_submission': False}, 'metadata': {**client_metadata(), 'stale_copy': None}}, ensure_ascii=False)


def _public_job(value):
    """Keep short image references in context; reproduction and routing stay in receipts."""
    if isinstance(value, list):
        return [_public_job(item) for item in value]
    if not isinstance(value, dict):
        return value
    allowed = {'success', 'task_id', 'image_id', 'title', 'state', 'stage', 'preset', 'seed_used',
               'count', 'width', 'height', 'path', 'expired', 'reference_ids', 'artifacts', 'tasks',
               'worker_running', 'elapsed_seconds', 'queued_seconds', 'running_seconds', 'stalled',
               'last_progress_at', 'stall_reason', 'cancel_requested', 'error', 'message', 'receipt_path', 'created_at',
               'type', 'retry_submission', 'retry_allowed', 'event_id'}
    return {key: _public_job(item) for key, item in value.items() if key in allowed}


def register(ctx):
    for name, schema in SCHEMAS.items():
        def handler(args, _name=name, **kwargs):
            return handle(ctx, _name, args)
        ctx.register_tool(name=name, toolset='comfy_workflows', schema=schema, handler=handler,
                          description=schema['description'], emoji='🎨')

    def setup(parser):
        parser.add_argument('tool', choices=list(SCHEMAS))
        parser.add_argument('--args', default='{}', help='JSON object of tool arguments')
        parser.add_argument('--args-file', help='JSON argument file')

    def command(args):
        from pathlib import Path
        payload = json.loads(Path(args.args_file).read_text()) if args.args_file else json.loads(args.args)
        # Use the same scoped dispatch and handlers as the agent.
        raw = ctx.dispatch_tool(args.tool, payload)
        print(raw)
        return 0 if json.loads(raw).get('success') else 1

    ctx.register_cli_command('comfy-workflows', 'ComfyUI workflow tools', setup, command)

    from plugins.plugin_storage import plugin_data_dir
    from . import delivery, context_cards
    data_dir = plugin_data_dir('comfy-workflows')
    delivery.install(ctx, data_dir)
    context_cards.install(ctx, data_dir)
