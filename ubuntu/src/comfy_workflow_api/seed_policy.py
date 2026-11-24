"""Choose a seed once at public preview/admission, never inside graph compilation."""
import secrets

from .contract import normalize

EDIT_WORKFLOW = 'qwen-image-2.1-edit'


def seed_policy(workflow):
    return 'random_per_edit' if workflow == EDIT_WORKFLOW else 'fixed'


def prepare_request(request, workflow, *, exclude_seeds=()):
    """Keep validation deterministic, then replace every valid caller seed for edit.

    The returned plain request is compiled deterministically and persisted by the
    caller. Exclusions can rule out reference-image seeds; they cannot fix a seed
    or bypass random selection. Preview does not reserve the next dispatch seed.
    """
    effective = normalize(request, workflow)
    if workflow == EDIT_WORKFLOW:
        forbidden = set(exclude_seeds)
        forbidden.add(effective['seed'])
        while True:
            seed = secrets.randbits(64)
            if seed not in forbidden:
                effective['seed'] = seed
                break
    return effective
