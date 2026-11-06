"""Stable generation presets shared by the SDK and Hermes bridge."""
import copy

PRESETS = {
    'qwen-image-2.1-t2i': {'fast': {'use_lora': True, 'steps': 6}, 'quality': {'use_lora': False, 'steps': 25}},
    'qwen-image-2.1-edit': {'fast': {'use_lora': True, 'steps': 6}, 'quality': {'use_lora': False, 'steps': 25}},
}
CONTRACT_VERSION = 7
VERSION = '0.6.1'
MAX_PROMPT_LENGTH = 8000


def normalize(request, workflow=None):
    request = copy.deepcopy(request)
    if workflow is None:
        workflow = 'qwen-image-2.1-edit' if request.get('images') else 'qwen-image-2.1-t2i'
    if workflow not in PRESETS:
        raise ValueError(f'Unknown workflow: {workflow}')
    presets = PRESETS[workflow]
    prompt = request.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('prompt must be a nonempty string')
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise ValueError('prompt exceeds 8000 characters; never truncated')
    preset = request.get('preset', 'quality' if request.get('use_lora') is False else 'fast')
    if preset not in presets:
        raise ValueError('preset must be fast or quality')
    if request.get('negative_prompt', '') != '' or request.get('cfg', 1) != 1:
        raise ValueError('CFG is fixed at 1 and negative prompts are disabled: non-1 CFG with this pipeline/LoRA was unstable in user tests')
    for key, value in presets[preset].items():
        if key in request and (type(request[key]) is not type(value) or request[key] != value):
            raise ValueError(f'For {workflow}, use fast (LoRA, 6 steps) or quality (no LoRA, {presets["quality"]["steps"]} steps); arbitrary combinations are disabled due to poor tested results')
    seed = request.get('seed', 0)
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError('seed must be an unsigned 64-bit integer; default is fixed 0, not random')
    request.update(preset=preset, **presets[preset], seed=seed, cfg=1, negative_prompt='')
    return request
