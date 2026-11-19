"""Command-line entry point: ``relayer <command>``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ConfigError, load_heteronyms, load_templates


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--heteronyms", type=Path,
        default=Path(os.environ.get("RELAYER_HETERONYMS", "heteronyms.yaml")),
        help="heteronyms file (env RELAYER_HETERONYMS; default ./heteronyms.yaml)")
    parser.add_argument(
        "--templates", type=Path,
        default=Path(os.environ.get("RELAYER_TEMPLATES", "templates")),
        help="templates directory (env RELAYER_TEMPLATES; default ./templates)")
    parser.add_argument(
        "--work-root", default=os.environ.get("RELAYER_WORK_ROOT", "/work"),
        help="value of {work} in folder patterns (env RELAYER_WORK_ROOT; "
             "default /work, the path inside the container)")
    parser.add_argument(
        "--class", dest="klass", default=os.environ.get("RELAYER_CLASS"),
        help="trust class to load (env RELAYER_CLASS; default: every class)")


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        templates = load_templates(args.templates)
        heteronyms = load_heteronyms(args.heteronyms, templates, args.work_root,
                                     args.klass)
    except ConfigError as exc:
        for line in exc.errors:
            print(f"error: {line}", file=sys.stderr)
        return 1
    for name, p in sorted(heteronyms.items()):
        state = "enabled" if p.enabled else "disabled"
        print(f"{name}: class={p.klass} template={p.template} tier={p.tier} {state}")
    print(f"ok: {len(heteronyms)} heteronym(s), {len(templates)} template(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relayer")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="check heteronyms.yaml and templates")
    _common(validate)
    validate.set_defaults(func=cmd_validate)
    args = parser.parse_args(argv)
    return args.func(args)
