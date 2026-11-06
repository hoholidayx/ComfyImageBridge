def schema(name, description, properties=None, required=()):
    return {'name':name,'description':description,'parameters':{'type':'object','properties':properties or {},'required':list(required),'additionalProperties':False}}


SCHEMAS = {
    'ubuntu_status': schema('ubuntu_status',
        'Read-only Ubuntu/ComfyUI, power/inhibitor and GPU observations with timestamps. '
        'Never wakes, starts/stops, generates or holds a sleep inhibitor. Offline_unknown cannot distinguish sleep, shutdown or network failure. '
        'Unknown GPU telemetry is null, never assumed zero.'),
    'comfy_service': schema('comfy_service',
        'Explicit ComfyUI administration; ordinary comfy_image prepares the service internally. '
        'status is read-only. ensure_ready may wake Ubuntu and verifies real rendering; HTTP alone is insufficient. '
        'Use operation_id/status for in_progress work, never repeat restart to poll. '
        'stop/restart require an explicit user service request and protect active jobs. '
        'recover diagnoses a suspect owned instance and may interrupt it; never use for a healthy busy queue. '
        'Uses original run_comfyui.sh, identity-checked shutdown and automatic timeout escalation; no manual recovery confirmation.',
        {'action':{'type':'string','enum':['status','ensure_ready','stop','restart','recover']},
         'operation_id':{'type':'string','description':'With status only: query a previously returned operation ID without starting new work.'},
         'wait_seconds':{'type':'integer','minimum':0,'maximum':30,'description':'Initial wait budget, default30; unfinished work continues independently and returns ready=false.'}}, ['action']),
}
