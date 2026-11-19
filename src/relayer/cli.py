"""Command-line entry point: ``relayer <command>``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ConfigError, load_personas, load_templates
from .reconcile import apply, plan
from .runtime import Runtime, check_environment, load_runtime
from .sessions import SessionError, TmuxBackend


def _common(parser: argparse.ArgumentParser, runtime_required: bool) -> None:
    parser.add_argument(
        "--personas", type=Path,
        default=Path(os.environ.get("RELAYER_PERSONAS", "personas.yaml")),
        help="personas file (env RELAYER_PERSONAS; default ./personas.yaml)")
    parser.add_argument(
        "--templates", type=Path,
        default=Path(os.environ.get("RELAYER_TEMPLATES", "templates")),
        help="templates directory (env RELAYER_TEMPLATES; default ./templates)")
    parser.add_argument(
        "--runtime", type=Path,
        default=(Path(os.environ["RELAYER_RUNTIME"]) if "RELAYER_RUNTIME" in os.environ
                 else Path("relayer.yaml") if runtime_required else None),
        help="runtime config (env RELAYER_RUNTIME; default ./relayer.yaml"
             + ("" if runtime_required else "; optional here") + ")")
    parser.add_argument(
        "--work-root", default=os.environ.get("RELAYER_WORK_ROOT"),
        help="value of {work} (env RELAYER_WORK_ROOT; default: the runtime "
             "config's work_root, else /work, the path inside the container)")


def _load(args: argparse.Namespace):
    runtime = load_runtime(args.runtime) if args.runtime else Runtime()
    work_root = args.work_root or runtime.work_root
    templates = load_templates(args.templates)
    personas = load_personas(args.personas, templates, work_root)
    return runtime, templates, personas


def _errors(lines: list[str]) -> None:
    for line in lines:
        print(f"error: {line}", file=sys.stderr)


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        _, templates, personas = _load(args)
    except ConfigError as exc:
        _errors(exc.errors)
        return 1
    for name, p in sorted(personas.items()):
        state = "enabled" if p.enabled else "disabled"
        print(f"{name}: template={p.template} tier={p.tier} {state}")
    print(f"ok: {len(personas)} persona(s), {len(templates)} template(s)")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    problems = check_environment(dict(os.environ))
    if problems:
        _errors(problems)
        return 2
    try:
        runtime, _, personas = _load(args)
    except ConfigError as exc:
        _errors(exc.errors)
        return 1
    backend = TmuxBackend(runtime.tmux_socket)
    try:
        actions = plan(personas, backend.list())
    except SessionError as exc:
        _errors([str(exc)])
        return 1
    for action in actions:
        print(action)
    if not actions:
        print("ok: nothing to do")
    if args.dry_run:
        return 0
    failures = apply(actions, personas, backend, runtime)
    _errors(failures)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relayer")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check personas.yaml and templates")
    _common(validate, runtime_required=False)
    validate.set_defaults(func=cmd_validate)

    reconcile = sub.add_parser(
        "reconcile", help="start, stop and restart sessions to match personas.yaml")
    _common(reconcile, runtime_required=True)
    reconcile.add_argument("--dry-run", action="store_true",
                           help="print the actions without carrying them out")
    reconcile.set_defaults(func=cmd_reconcile)

    args = parser.parse_args(argv)
    return args.func(args)
