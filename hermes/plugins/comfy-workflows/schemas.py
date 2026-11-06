SIZE = {
    'type': 'object',
    'description': 'explicit: width/height multiples of 32; aspect_ratio: ratio + megapixels + multiple; edit also allows follow_image. Exact phone 9:16: explicit 864x1536 or 576x1024.',
    'properties': {
        'mode': {'type': 'string', 'enum': ['explicit', 'aspect_ratio', 'follow_image']},
        'width': {'type': 'integer', 'minimum': 64, 'maximum': 2048},
        'height': {'type': 'integer', 'minimum': 64, 'maximum': 2048},
        'aspect_ratio': {'type': 'string', 'enum': ['1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9', '21:9']},
        'megapixels': {'type': 'number', 'minimum': .1, 'maximum': 4},
        'multiple': {'type': 'integer', 'enum': [32, 64, 96, 128]},
    },
    'required': ['mode'], 'additionalProperties': False,
}
COMMON = {
    'prompt': {'type': 'string', 'maxLength': 8000, 'description': 'Image description/edit instruction; at most 8000 Unicode characters, never truncated. With 0 images use a text-to-image prompt without image tags; with 1 image refer naturally to the image, no tags. With 2..10 images use <image1>, <image2>, etc. (no underscore), matching the images array order. For each image state its role, which attributes to take and what to preserve. Name the base canvas when editing; for a new composition with no base canvas state each reference role and choose an explicit output size.'},
    'seed': {'type': 'integer', 'minimum': 0, 'description': 'Unsigned 64-bit integer, at most 18446744073709551615; seed+count-1 must also fit this range. Default FIXED 0, not random. Output i uses seed+i (i starts at 0). Actual seeds are returned as seed_used.'},
    'preset': {'type': 'string', 'enum': ['fast', 'quality'], 'description': 'Default fast: acceleration LoRA ON, exactly 6 steps, Euler with Pruna v0.2 custom sigmas and resolution-dependent shift (not the simple scheduler). Choose quality only when the user explicitly requests it: LoRA OFF, 25 steps for both text-to-image and editing, Euler/simple, denoise 1. CFG is always 1 and negative prompts are disabled. Other step/CFG/LoRA combinations produced poor or unstable results in user testing; do not try to override them.'},
    'count': {'type': 'integer', 'minimum': 1, 'maximum': 4, 'description': 'Number of images, default 1. Independent jobs execute serially; seed increments for each. Returns one parent task_id with child IDs and per-image seeds.'},
    'output_size': SIZE,
}


def schema(name, description, properties, required=()):
    return {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}


SCHEMAS = {
    'comfy_image': schema('comfy_image', 'Generate or edit images in the background; automatically prepare Ubuntu and ComfyUI. Choose preset and ordered references. Empty images means text-to-image. Returns a task ID; progress, choices and images arrive directly in this chat. Do not poll or resubmit routinely. Titles describe generation intent, not verified visual content.', {
        'prompt': COMMON['prompt'],
        'preset': {'type': 'string', 'enum': ['fast', 'quality'], 'description': 'Default fast: LoRA/6 steps, Euler/Pruna custom sigmas. Choose quality only when the user explicitly requests it: no LoRA/25 steps, Euler/simple. CFG=1; no negative prompt or arbitrary steps.'},
        'images': {'type': 'array', 'minItems': 0, 'maxItems': 10, 'items': {'type': 'string'}, 'description': 'Optional ordered 0..10 image IDs (img_000001) or absolute local PNG paths. Omit or [] for text-to-image. Keep this order consistent with prompt references: images[0] is <image1>, images[1] is <image2>, etc. When editing a base canvas place it first; for a new composition describe each reference role and set an explicit output size.'},
        'size': {'type': 'string', 'description': 'Default auto: follow first image, or 1024x1024 for text-to-image. Ratios: 1:1,2:3,3:2,3:4,4:3,9:16,16:9,21:9 (~1MP,32 aligned); or WxH, 64..2048 multiples of32. Exact phone9:16: 864x1536 or576x1024.'},
        'title': {'type': 'string', 'maxLength': 80, 'description': 'Short title of intended image for later reference; no extra naming call needed.'},
        'seed': {'type': 'integer', 'minimum': 0, 'description': 'Unsigned 64-bit integer, at most 18446744073709551615; seed+count-1 must also fit this range. Default fixed0, not random. Output i uses seed+i.'},
        'count': {'type': 'integer', 'minimum': 1, 'maximum': 4, 'description': 'Default1; independent images with incrementing seeds.'},
    }, ['prompt']),
    'comfy_workflows': schema('comfy_workflows', 'Check ComfyUI connection, presets and backend/plugin versions, code hashes and stale_copy. No generation.', {}),
    'comfy_generate': schema('comfy_generate', 'First call comfy_service action=ensure_ready and require ready=true; HTTP alone is not readiness. Submit text-to-image using fast (default, LoRA/6 steps, Euler/Pruna custom sigmas) or quality (no LoRA/25 steps), fixed CFG=1, no negative prompt. Returns task_id quickly; then call comfy_task action=result wait_seconds=30 until completed. Never re-submit just because it is slow.', {**COMMON, 'preset': {**COMMON['preset'], 'description': 'Text-to-image preset: fast (default) enables LoRA and uses exactly 6 Euler steps with Pruna v0.2 custom sigmas and resolution-dependent shift. Choose quality only when the user explicitly requests it: no LoRA, 25 steps, Euler/simple, denoise 1. CFG fixed at 1; negative prompts and arbitrary step/LoRA overrides are disabled.'}}, ['prompt']),
    'comfy_edit': schema('comfy_edit', 'First call comfy_service action=ensure_ready and require ready=true; HTTP alone is not readiness. Submit image editing with fast (default, LoRA/6 steps, Euler/Pruna custom sigmas) or quality (no LoRA/25 steps, ComfyUI official template), fixed CFG=1, no negative prompt. Transfers 1..10 ordered PNGs; first image is target, others references. Returns task_id; retrieve with comfy_task. Supports size selection.', {
        **COMMON, 'images': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 10, 'description': 'Absolute paths to PNG files on the Hermes host, in reference order. No URLs or Ubuntu paths.'},
        'preset': {**COMMON['preset'], 'description': 'Image editing preset: fast (default) enables LoRA and uses exactly 6 Euler steps with Pruna v0.2 custom sigmas and resolution-dependent shift. Choose quality only when the user explicitly requests it: no LoRA, 25 steps, Euler/simple, denoise 1. CFG fixed at 1; negative prompts and arbitrary step/LoRA overrides are disabled.'},
        'reference_resolution': {'type': 'integer', 'minimum': 0, 'maximum': 4096, 'description': 'Reference square pixel budget, multiple of 32; default 1024; 0 preserves reference size.'},
    }, ['prompt', 'images']),
    'comfy_task': schema('comfy_task', 'Check an existing plugin task, cancel it, or download its result. Status includes elapsed_seconds, queued_seconds, running_seconds, last_progress_at and stalled (null when evidence is insufficient). stalled=true means suspected stagnation, not proof of dead GPU. Cancel only affects this task/batch; cancel_requested is cooperative and not proof the worker stopped. Use comfy_service recover for diagnostics if cancellation cannot finish. If queued/running, query this task ID; never submit it again. Never blindly poll forever when stalled or cancel_requested. Completed result supplies local image paths and MEDIA tags for display.', {
        'task_id': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['status', 'result', 'cancel']},
        'wait_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 30},
    }, ['task_id']),
    'comfy_tasks': schema('comfy_tasks', 'List local task history for this Hermes profile without SSH. Find unknown task IDs, preset, seeds, prompt summary, state and downloaded paths. Does not refresh server state; use comfy_task for that.', {
        'action': {'type': 'string', 'enum': ['list']},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
    }),
}

# Existing schemas remain callable for one compatibility release; only the unified
# entry is eager. Lifecycle and diagnosis stay discoverable rather than ambient.
for _name in ('comfy_generate', 'comfy_edit'):
    SCHEMAS[_name]['description'] = 'Legacy compatibility interface; prefer comfy_image. ' + SCHEMAS[_name]['description']
SCHEMAS['comfy_task']['parameters']['properties'].update({
    'action': {'type': 'string', 'enum': ['status', 'result', 'cancel', 'list']},
    'query': {'type': 'string', 'description': 'With list: find image titles or task IDs in this conversation.'},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
})
SCHEMAS['comfy_task']['parameters']['required'] = []
SCHEMAS['comfy_task']['description'] = 'Inspect or cancel an existing background task, or list this conversation\'s image titles and IDs. Not needed for routine polling; completion arrives directly. action defaults to status; task_id required except for list. Legacy task IDs remain supported. Cancellation is cooperative; never regenerate automatically.'
