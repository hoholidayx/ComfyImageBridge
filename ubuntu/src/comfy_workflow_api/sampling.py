"""Capture the effective settings from the submitted uni graph."""

NATIVE_SAMPLER = {'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1}


def configuration(graph, request):
    sampler = graph['458']
    inputs = sampler['inputs']
    return {'preset': request['preset'], 'sampler_class': sampler['class_type'],
            'use_lora': graph['485']['inputs']['switch'],
            **{key: inputs[key] for key in ('steps', 'cfg', 'seed', 'sampler_name', 'scheduler', 'denoise')}}
