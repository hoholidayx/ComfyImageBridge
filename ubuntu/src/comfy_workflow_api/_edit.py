import copy
import math
from pathlib import Path
from .sampling import configure

def parameters(workflow):
    definitions = {s['id']: s for s in workflow['definitions']['subgraphs']}
    parent = next((n for n in workflow['nodes'] if n['type'] in definitions and definitions[n['type']]['name'] == 'Image Edit (Qwen Image 2.1)'))
    names = [i['name'] for i in parent['inputs'] if 'widget' in i]
    defaults = dict(zip(names, parent['widgets_values'], strict=True))
    return (parent, definitions[parent['type']], defaults)

def validate_request(request):
    allowed = {'images', 'prompt', 'reference_resolution', 'output_size', 'seed', 'steps', 'cfg', 'negative_prompt', 'use_lora', 'preset'}
    if set(request) - allowed:
        raise ValueError(f'Unknown request parameters: {set(request) - allowed}')
    images = request.get('images')
    if not isinstance(images, list) or not 0 <= len(images) <= 10:
        raise ValueError('images must contain 0..10 PNG paths, in reference order')
    if type(request.get('use_lora', True)) is not bool:
        raise ValueError('use_lora must be a boolean')
    if not isinstance(request.get('negative_prompt', ''), str):
        raise ValueError('negative_prompt must be a string')
    if not isinstance(request.get('prompt'), str) or not request['prompt'].strip():
        raise ValueError('A nonempty prompt is required')
    for path in images:
        if not Path(path).is_file():
            raise ValueError(f'Input file does not exist: {path}')
    resolution = request.get('reference_resolution', 1024)
    if type(resolution) is not int or not 0 <= resolution <= 4096 or resolution % 32:
        raise ValueError('reference_resolution must be 0..4096, divisible by 32')
    if type(request.get('steps', 6)) is not int or not 1 <= request.get('steps', 6) <= 100:
        raise ValueError('steps must be an integer from 1 to 100')
    cfg = request.get('cfg', 1)
    if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or (not math.isfinite(cfg)) or (not 0 <= cfg <= 20):
        raise ValueError('cfg must be a finite number from 0 to 20')
    seed = request.get('seed', 0)
    if type(seed) is not int or not 0 <= seed < 2 ** 64:
        raise ValueError('seed must be an unsigned 64-bit integer')
    size = request.get('output_size', {'mode': 'follow_image'})
    if not isinstance(size, dict):
        raise ValueError('output_size must be an object')
    mode = size.get('mode', 'follow_image')
    if not images and mode == 'follow_image':
        raise ValueError('Text-to-image requires an explicit or aspect-ratio output size')
    fields = {'follow_image': {'mode'}, 'aspect_ratio': {'mode', 'aspect_ratio', 'megapixels', 'multiple'}, 'explicit': {'mode', 'width', 'height'}}
    if mode not in fields or set(size) - fields[mode]:
        raise ValueError('Invalid output size mode or fields')
    if mode == 'explicit':
        for axis in ('width', 'height'):
            value = size.get(axis)
            if type(value) is not int or not 64 <= value <= 2048 or value % 32:
                raise ValueError(f'{axis} must be 64..2048, divisible by 32')
    if mode == 'aspect_ratio':
        ratios = ('1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9', '21:9')
        if size.get('aspect_ratio') not in ratios:
            raise ValueError(f'aspect_ratio must be one of {ratios}')
        mp = size.get('megapixels', 1)
        if isinstance(mp, bool) or not isinstance(mp, (int, float)) or (not math.isfinite(mp)) or (not 0.1 <= mp <= 4):
            raise ValueError('MVP megapixels must be 0.1..4')
        multiple = size.get('multiple', 32)
        if type(multiple) is not int or multiple not in (32, 64, 96, 128):
            raise ValueError('multiple must be 32, 64, 96 or 128')

def build_prompt(workflow, uploaded_images, request, prefix):
    """Flatten this workflow's subgraph using named widgets and graph links."""
    _, subgraph, values = parameters(workflow)
    values.update(prompt=request['prompt'], negative_prompt=request.get('negative_prompt', ''), use_lora=request.get('use_lora', True), resolution=request.get('reference_resolution', 1024), seed=request.get('seed', 0), steps=request.get('steps', values['steps']), cfg=request.get('cfg', values['cfg']))
    size = request.get('output_size', {'mode': 'follow_image'})
    mode = size.get('mode', 'follow_image')
    values['switch'] = mode != 'follow_image'
    graph = {}
    image_refs = {}
    for index, image in enumerate(uploaded_images, 1):
        node_id = f'input_{index}'
        graph[node_id] = {'class_type': 'LoadImage', 'inputs': {'image': image}}
        image_refs[f'image_{index}'] = [node_id, 0]
    if mode == 'aspect_ratio':
        labels = {'1:1': 'Square', '2:3': 'Portrait Photo', '3:2': 'Photo', '3:4': 'Portrait Standard', '4:3': 'Standard', '9:16': 'Portrait Widescreen', '16:9': 'Widescreen', '21:9': 'Ultrawide'}
        ratio = size['aspect_ratio']
        graph['resolution'] = {'class_type': 'ResolutionSelector', 'inputs': {'aspect_ratio': f'{ratio} ({labels[ratio]})', 'megapixels': size.get('megapixels', 1), 'multiple': size.get('multiple', 32)}}
        values['width'], values['height'] = (['resolution', 0], ['resolution', 1])
    else:
        values['width'], values['height'] = (size.get('width', 1024), size.get('height', 1024))
    internal = {n['id']: n for n in subgraph['nodes']}
    for node in internal.values():
        inputs = copy.deepcopy(node.get('widgets_values_named', {}))
        inputs.pop('control_after_generate', None)
        graph[str(node['id'])] = {'class_type': node['type'], 'inputs': inputs}
    output_ref = None
    for link in subgraph['links']:
        origin, target = (link['origin_id'], link['target_id'])
        if target == -20:
            output_ref = [str(origin), link['origin_slot']]
            continue
        target_input = internal[target]['inputs'][link['target_slot']]['name']
        if origin == -10:
            outer_input = subgraph['inputs'][link['origin_slot']]
            name = outer_input['name']
            if name.startswith('images.'):
                continue
            value = values[name]
        else:
            value = [str(origin), link['origin_slot']]
        graph[str(target)]['inputs'][target_input] = copy.deepcopy(value)
    encoder = next((str(n['id']) for n in internal.values() if n['type'] == 'TextEncodeQwenImage21'))
    for name, ref in image_refs.items():
        graph[encoder]['inputs']['images.' + name] = ref
    if output_ref is None:
        raise ValueError('Subgraph output not found')
    graph['save'] = {'class_type': 'SaveImageAdvanced', 'inputs': {'images': output_ref, 'filename_prefix': prefix, 'format': 'png', 'format.bit_depth': '8-bit', 'format.input_color_space': 'sRGB'}}
    return configure(graph, request)
