import argparse
import json
import sys
from pathlib import Path
from .api import WorkflowAPI


def main():
    parser = argparse.ArgumentParser(description="ComfyUI 工作流 SDK/CLI")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--base-url")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("health", "list-workflows", "sync-workflows"):
        commands.add_parser(command)
    cleanup = commands.add_parser('cleanup')
    cleanup.add_argument('--dry-run', action='store_true')
    guard = commands.add_parser('sleep-guard')
    guard.add_argument('action', choices=['start', 'stop', 'restart', 'status'])
    guard.add_argument('--force', action='store_true')
    for command in ("compile", "submit", "run"):
        p = commands.add_parser(command)
        p.add_argument("workflow")
        p.add_argument("--request", type=Path, required=True)
        if command == "run":
            p.add_argument("--timeout", type=float)
    for command in ("status", "wait", "fetch", "cancel"):
        p = commands.add_parser(command)
        p.add_argument("task_id")
        if command == "wait":
            p.add_argument("--timeout", type=float)
    args = parser.parse_args()
    try:
        api = WorkflowAPI(args.root, args.base_url)
        if args.command == 'sleep-guard':
            from . import sleep_guard
            if args.action == 'restart':
                sleep_guard.stop(api, force=args.force)
                result = sleep_guard.start(api)
            elif args.action == 'stop':
                result = sleep_guard.stop(api, force=args.force)
            else:
                result = getattr(sleep_guard, args.action)(api)
        elif args.command == 'cleanup':
            result = api.cleanup(dry_run=args.dry_run)
        elif args.command in ("compile", "submit", "run"):
            path = args.request.resolve()
            kwargs = {"request_dir": path.parent}
            if args.command == "run":
                kwargs["timeout"] = args.timeout
            result = getattr(api, args.command)(args.workflow, json.loads(path.read_text()), **kwargs)
        elif args.command in ("status", "wait", "fetch", "cancel"):
            kwargs = {"timeout": args.timeout} if args.command == "wait" else {}
            result = getattr(api, args.command)(args.task_id, **kwargs)
        else:
            result = getattr(api, args.command.replace("-", "_"))()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
