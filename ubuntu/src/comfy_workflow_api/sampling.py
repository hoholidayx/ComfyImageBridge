"""Separate the Pruna LoRA schedule from the official ComfyUI base schedule."""
import copy

FAST_RAW_SIGMAS = [1.0, 0.925, 0.85, 0.625, 0.375, 0.125]
PROFILES = {
    'fast': {'sampler_name': 'euler', 'scheduler': 'pruna-v0.2-custom',
             'raw_sigmas': FAST_RAW_SIGMAS, 'terminal_sigma': 0,
             'dynamic_shift': {'base_image_seq_len': 256, 'max_image_seq_len': 8192,
                               'base_shift': 0.5, 'max_shift': 0.9, 'shift_terminal': None},
             'source': 'https://huggingface.co/PrunaAI/P-Q-Image-2.1'},
    'quality': {'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1},
}


def configure(graph, request):
    if request['preset'] != 'fast':
        return graph
    old = graph['458']
    if old['class_type'] != 'KSampler':
        raise ValueError('Unsupported sampler structure; expected the verified KSampler adapter')
    if graph['479']['inputs']['lora_name'] != 'p_q_image_2.1_6_steps_v0.2_comfyui_bf16.safetensors':
        raise ValueError('Unsupported fast LoRA; the verified schedule requires Pruna 6-step v0.2')
    inputs = old['inputs']
    graph['pruna_sampler'] = {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': 'euler'}}
    # This already-installed node transforms arbitrary raw nodes using Qwen 2.1's
    # resolution-dependent shift. Explicitly supply Pruna nodes, never its Viggle defaults.
    graph['pruna_sigmas'] = {'class_type': 'ViggleTurboSigmas', 'inputs': {
        'latent': inputs['latent_image'], 'nodes': ', '.join(str(v) for v in FAST_RAW_SIGMAS)}}
    graph['458'] = {'class_type': 'SamplerCustom', 'inputs': {
        'model': inputs['model'], 'positive': inputs['positive'], 'negative': inputs['negative'],
        'latent_image': inputs['latent_image'], 'cfg': 1, 'add_noise': True,
        'noise_seed': request['seed'], 'sampler': ['pruna_sampler', 0], 'sigmas': ['pruna_sigmas', 0]}}
    return graph


def configuration(graph, request):
    sampler = graph['458']
    result = {'preset': request['preset'], 'steps': request['steps'], 'cfg': 1,
              'seed': request['seed'], 'sampler_class': sampler['class_type'],
              **copy.deepcopy(PROFILES[request['preset']])}
    if request['preset'] == 'fast':
        result.update(sigma_node=graph['pruna_sigmas'], latent_source=sampler['inputs']['latent_image'])
    else:
        result.update({key: sampler['inputs'][key] for key in ('sampler_name', 'scheduler', 'denoise')})
    return result
