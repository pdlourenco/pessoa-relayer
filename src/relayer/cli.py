"""Command-line entry point: ``relayer <command>``."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .config import ConfigError, load_heteronyms, load_templates
from .reconcile import apply, plan
from .runtime import Runtime, check_environment, load_runtime
from .sessions import SessionError, TmuxBackend, UserTmuxBackend
from .users import Accounts, UidMap
from .supervise import ParkStore, Supervisor, check_state_dir, run


def _state_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state", type=Path, default=Path(os.environ.get("RELAYER_STATE", "state")),
        help="supervisor state directory (env RELAYER_STATE; default ./state)")


def _common(parser: argparse.ArgumentParser, runtime_required: bool) -> None:
    parser.add_argument(
        "--heteronyms", type=Path,
        default=Path(os.environ.get("RELAYER_HETERONYMS", "heteronyms.yaml")),
        help="heteronyms file (env RELAYER_HETERONYMS; default ./heteronyms.yaml)")
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
    parser.add_argument(
        "--class", dest="klass", default=os.environ.get("RELAYER_CLASS"),
        help="trust class to load (env RELAYER_CLASS; default: the runtime "
             "config's class, else every class)")


def _load(args: argparse.Namespace):
    runtime = load_runtime(args.runtime) if args.runtime else Runtime()
    work_root = args.work_root or runtime.work_root
    templates = load_templates(args.templates)
    heteronyms = load_heteronyms(args.heteronyms, templates, work_root,
                                 args.klass or runtime.klass)
    return runtime, templates, heteronyms


def _errors(lines: list[str]) -> None:
    for line in lines:
        print(f"error: {line}", file=sys.stderr)


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        _, templates, heteronyms = _load(args)
    except ConfigError as exc:
        _errors(exc.errors)
        return 1
    for name, p in sorted(heteronyms.items()):
        state = "enabled" if p.enabled else "disabled"
        print(f"{name}: class={p.klass} template={p.template} tier={p.tier} {state}")
    print(f"ok: {len(heteronyms)} heteronym(s), {len(templates)} template(s)")
    return 0


def _backend(runtime: Runtime, state: Path):
    """The session backend for this runtime, or an error line."""
    if runtime.isolation == "none":
        return TmuxBackend(runtime.tmux_socket), None
    if os.geteuid() != 0:
        return None, ("isolation: users needs the supervisor to run as root inside "
                      "its container (with only CHOWN, SETUID and SETGID); set "
                      "isolation: none to run everything as the current user")
    if not shutil.which("setpriv"):
        return None, "isolation: users needs setpriv (util-linux)"
    accounts = Accounts(UidMap(state / "users.json", runtime.uid_base), runtime.homes,
                        runtime.work_root)
    return UserTmuxBackend(runtime.tmux_socket, accounts), None


def cmd_reconcile(args: argparse.Namespace) -> int:
    problems = check_environment(dict(os.environ))
    if problems:
        _errors(problems)
        return 2
    try:
        runtime, _, heteronyms = _load(args)
    except ConfigError as exc:
        _errors(exc.errors)
        return 1
    environ = dict(os.environ)
    backend, problem = _backend(runtime, args.state)
    if problem:
        _errors([problem])
        return 1
    try:
        actions = plan(heteronyms, backend.list(),
                       runtime.session_hash)
        _errors(getattr(backend, "errors", []))
    except SessionError as exc:
        _errors([str(exc)])
        return 1
    for action in actions:
        print(action)
    if not actions:
        print("ok: nothing to do")
    if args.dry_run:
        return 0
    failures = apply(actions, heteronyms, backend, runtime, environ)
    _errors(failures)
    return 1 if failures else 0


def cmd_supervise(args: argparse.Namespace) -> int:
    problems = check_environment(dict(os.environ))
    if problems:
        _errors(problems)
        return 2
    if args.interval <= 0:
        _errors(["--interval must be greater than 0"])
        return 1
    problem = check_state_dir(args.state)
    if problem:
        _errors([problem])
        return 1
    try:
        runtime, _, _ = _load(args)
    except ConfigError as exc:
        # The first config must be valid; later bad edits are survived.
        _errors(exc.errors)
        return 1
    backend, problem = _backend(runtime, args.state)
    if problem:
        _errors([problem])
        return 1
    supervisor = Supervisor(backend, lambda: _load(args)[::2],
                            ParkStore(args.state / "parked"), args.interval,
                            dict(os.environ))
    return run(supervisor, args.interval)


def cmd_wake(args: argparse.Namespace) -> int:
    try:
        woke = ParkStore(args.state / "parked").wake(args.name)
    except ValueError as exc:
        _errors([str(exc)])
        return 1
    print(f"{args.name}: {'woken; starts on the next tick' if woke else 'was not parked'}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Run the heteronym's own session command interactively, as its user, in
    its workspace, for the one-time sign-in and first-run prompts (docs/SPEC.md
    §6.3, §6.6). Replaces this process."""
    try:
        runtime, _, heteronyms = _load(args)
    except ConfigError as exc:
        _errors(exc.errors)
        return 1
    if args.name not in heteronyms:
        _errors([f"{args.name}: not a heteronym of this container's class"])
        return 1
    backend, problem = _backend(runtime, args.state)
    if problem:
        _errors([problem])
        return 1
    heteronym = heteronyms[args.name]
    try:
        running = args.name in backend.list()
    except SessionError as exc:
        _errors([str(exc)])
        return 1
    if running:
        # Two agents in one workspace, one of them unseen by the supervisor,
        # would fight over the same files and settings.
        _errors([f"{args.name}: its session is running; set enabled: false for it, "
                 "let the supervisor stop it, then run login again"])
        return 1
    identity = None
    prefix: list[str] = []
    if isinstance(backend, UserTmuxBackend):
        backend.prepare(heteronym)
        ident = backend.accounts.identity(args.name)
        identity, prefix = ident.env(), ident.as_user()
    env = runtime.session_env(dict(os.environ), identity)
    # The exact command the supervisor will run, flags included, so the
    # sign-in, the workspace-trust prompt and the Remote Control prompt are
    # answered once, in the configuration that will use them. env -C changes
    # directory after setpriv has switched user: root here has no
    # DAC_OVERRIDE and cannot enter the heteronym's 700 folder itself.
    argv = [*prefix, "env", "-i", "-C", heteronym.folders[0],
            *(f"{k}={v}" for k, v in sorted(env.items())),
            *runtime.session_argv(heteronym)]
    os.execvp(argv[0], argv)
    return 0  # not reached


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relayer")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check heteronyms.yaml and templates")
    _common(validate, runtime_required=False)
    validate.set_defaults(func=cmd_validate)

    reconcile = sub.add_parser(
        "reconcile", help="start, stop and restart sessions to match heteronyms.yaml")
    _common(reconcile, runtime_required=True)
    _state_dir(reconcile)
    reconcile.add_argument("--dry-run", action="store_true",
                           help="print the actions without carrying them out")
    reconcile.set_defaults(func=cmd_reconcile)

    supervise = sub.add_parser(
        "supervise", help="reconcile forever; stop idle sessions; back off crash loops")
    _common(supervise, runtime_required=True)
    _state_dir(supervise)
    supervise.add_argument("--interval", type=float, default=30.0,
                           help="seconds between passes (default 30)")
    supervise.set_defaults(func=cmd_supervise)

    wake = sub.add_parser("wake", help="let an idle-stopped heteronym start again")
    _state_dir(wake)
    wake.add_argument("name")
    wake.set_defaults(func=cmd_wake)

    login = sub.add_parser("login", help="sign a heteronym's own user in, interactively")
    _common(login, runtime_required=True)
    _state_dir(login)
    login.add_argument("name")
    login.set_defaults(func=cmd_login)

    args = parser.parse_args(argv)
    return args.func(args)
