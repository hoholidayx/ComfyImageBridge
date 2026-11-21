"""Conservative progress diagnostics. Missing telemetry is never zero utilization."""
from datetime import datetime
from .storage import now, read_json

TERMINAL = frozenset(('completed', 'failed', 'cancelled', 'interrupted_by_restart'))


def seconds(start, end=None):
    if not start:
        return 0.0
    try:
        return max(0.0, (datetime.fromisoformat(end or now()) - datetime.fromisoformat(start)).total_seconds())
    except (ValueError, TypeError):
        return 0.0


def decorate(api, state):
    observed = now()
    end = state.get('completed_at') or state.get('ended_at') or observed
    evidence_path = api.data/'task-observations'/f"{state['task_id']}.json"
    evidence = read_json(evidence_path) if evidence_path.exists() else {}
    same = (evidence.get('prompt_id') == state.get('prompt_id') and
            evidence.get('service_instance_id') == state.get('service_instance_id'))
    if same and evidence.get('running_since'):
        if not state.get('running_since') or evidence['running_since'] < state['running_since']:
            state['running_since'] = evidence['running_since']
    started = state.get('running_since')
    state.update(observed_at=observed,
                 elapsed_seconds=round(seconds(state.get('created_at'), end), 3),
                 queued_seconds=round(seconds(state.get('submitted_at'), started or end), 3),
                 running_seconds=round(seconds(started, end), 3),
                 last_progress_at=state.get('last_progress_at'), stalled=None)
    if state.get('state') not in ('running', 'cancel_requested'):
        state['stalled'] = False
    elif seconds(started, observed) < 300:
        state['stalled'] = False
    elif (evidence.get('prompt_id') == state.get('prompt_id') and
          evidence.get('service_instance_id') == state.get('service_instance_id') and
          seconds(evidence.get('observed_at'), observed) <= 15):
        state['stall_evidence'] = evidence
        state['last_progress_at'] = evidence.get('last_progress_at') or state.get('last_progress_at')
        if evidence.get('gpu_utilization_percent') is not None and evidence.get('log_available'):
            state['stalled'] = bool(evidence.get('low_gpu_samples', 0) >= 3 and
                seconds(evidence.get('low_gpu_since'), observed) >= 120 and
                seconds(evidence.get('last_progress_at'), observed) >= 120)
    state['stall_reason'] = ('suspected_stall; not proof of a dead GPU and never authorizes termination'
                             if state['stalled'] else 'insufficient_evidence' if state['stalled'] is None else None)
    return state
