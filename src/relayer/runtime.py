"""Runtime configuration: ``relayer.yaml`` (docs/SPEC.md §3).

Everything about how sessions are launched on a given host lives here, not
in code: the tier→model mapping of the agent tool, the session command, and
the work root. The repository ships an example, never one host's values.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (NAME_RE, SAFE_PATH_RE, ConfigError, Heteronym, _is_version,
                     _load_yaml, _unknown_keys)
from .tiers import TIERS

RUNTIME_KEYS = {"version", "class", "work_root", "models", "session", "tmux_socket",
                "isolation", "homes", "uid_base"}
ISOLATION = ("users", "none")
ENV_PLACEHOLDERS = {"home", "name", "user"}
SESSION_KEYS = {"command", "env", "pass_env"}
PLACEHOLDERS = {"model", "name", "workdir", "settings"}
SOCKET_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Each of these moves a Claude Code session off the signed-in subscription:
# an API key or auth token outranks the sign-in, and the provider switches
# route to a cloud account (docs/SPEC.md §3.2).
FORBIDDEN_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_TERM = "screen-256color"
# Always passed through when set: what any login session expects.
BASE_ENV = ("HOME", "PATH", "LANG", "USER", "SHELL")


@dataclass(frozen=True)
class Runtime:
    klass: str | None = None        # trust class this container runs (SPEC §1.5)
    work_root: str = "/work"
    models: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    pass_env: tuple[str, ...] = ()
    tmux_socket: str = "pessoa"
    isolation: str = "users"        # "users": one Unix user per heteronym (SPEC §6)
    homes: str = "/homes"
    uid_base: int = 20000

    def session_env(self, environ: dict[str, str],
                    identity: dict[str, str] | None = None) -> dict[str, str]:
        """The whole environment a session gets; nothing else is inherited.

        ``identity`` (HOME, USER, LOGNAME of the heteronym's own user)
        replaces the supervisor's; ``env`` values may use {home}, {name}
        and {user}.
        """
        env = {k: environ[k] for k in BASE_ENV if k in environ}
        env.update(identity or {})
        env["TERM"] = DEFAULT_TERM
        env.update({k: environ[k] for k in self.pass_env if k in environ})
        values = {"home": env.get("HOME", ""), "user": env.get("USER", ""),
                  "name": (identity or {}).get("USER", "").removeprefix("h-")}
        env.update({k: re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], v)
                    for k, v in self.env.items()})
        if not env.get("PATH"):
            # env -i would fall back to a libc default PATH, the agent binary
            # would not be found, and the session would die unreported.
            raise ValueError("PATH is not set; set it in the supervisor's "
                             "environment or in relayer.yaml session.env")
        return env

    def session_command(self, heteronym: Heteronym, environ: dict[str, str],
                        identity: dict[str, str] | None = None) -> str:
        """One shell-quoted string: ``env -i NAME=value … <argv>``.

        tmux hands a single string to ``sh -c``; shlex quoting means no
        argument can become shell syntax, and a quoted string never ends in
        the bare ";" tmux treats as a command separator.
        """
        assignments = [f"{k}={v}" for k, v in
                       sorted(self.session_env(environ, identity).items())]
        return shlex.join(["env", "-i", *assignments, *self.session_argv(heteronym)])

    def session_hash(self, heteronym: Heteronym) -> str:
        """Hash of the configuration that shapes the session (SPEC §4).

        It covers the heteronym hash, the model, the command template, the
        literal ``env`` and the *names* in ``pass_env``, never the values of
        inherited variables: a reconcile run from a shell whose PATH differs
        from the supervisor's must not restart every session.
        """
        shape = {"heteronym": heteronym.config_hash(), "model": self.models[heteronym.tier],
                 "command": list(self.command), "env": self.env,
                 "pass_env": list(self.pass_env), "base": list(BASE_ENV),
                 "term": DEFAULT_TERM, "isolation": self.isolation}
        blob = json.dumps(shape, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def session_argv(self, heteronym: Heteronym) -> list[str]:
        """The command that runs ``heteronym``'s session, placeholders filled."""
        settings = json.dumps(
            {"permissions": {"allow": list(heteronym.allow),
                             "deny": list(heteronym.deny)}},
            separators=(",", ":"))
        values = {"model": self.models[heteronym.tier], "name": heteronym.name,
                  "workdir": heteronym.folders[0], "settings": settings}
        return [re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], arg)
                for arg in self.command]


def load_runtime(path: Path) -> Runtime:
    data = _load_yaml(path)
    errors: list[str] = []
    where = str(path)
    if not isinstance(data, dict):
        raise ConfigError([f"{where}: must be a mapping"])
    _unknown_keys(data, RUNTIME_KEYS, where, errors)
    if not _is_version(data.get("version")):
        errors.append(f"{where}: version must be 1")

    klass = data.get("class")
    if not isinstance(klass, str) or not NAME_RE.match(klass):
        errors.append(f"{where}.class: required; the trust class this container "
                      f"runs, matching {NAME_RE.pattern}")
        klass = None

    work_root = data.get("work_root", "/work")
    if not isinstance(work_root, str):
        errors.append(f"{where}.work_root: must be a string")
        work_root = "/work"

    models = data.get("models")
    if not isinstance(models, dict):
        errors.append(f"{where}.models: must map each tier to a model")
        models = {}
    for tier in TIERS:
        if not isinstance(models.get(tier), str) or not models.get(tier):
            errors.append(f"{where}.models.{tier}: missing model name")
    _unknown_keys(models, set(TIERS), f"{where}.models", errors)

    session = data.get("session")
    command: list[str] = []
    env: dict[str, str] = {}
    pass_env: list[str] = []
    if not isinstance(session, dict):
        errors.append(f"{where}.session: must be a mapping with a command")
    else:
        _unknown_keys(session, SESSION_KEYS, f"{where}.session", errors)
        command = session.get("command")
        if (not isinstance(command, list) or not command
                or not all(isinstance(a, str) for a in command)):
            errors.append(f"{where}.session.command: must be a non-empty list of strings")
            command = []
        for arg in command:
            for name in re.findall(r"\{(\w*)\}", arg):
                if name not in PLACEHOLDERS:
                    errors.append(f"{where}.session.command: unknown placeholder "
                                  f"{{{name}}} (known: {', '.join(sorted(PLACEHOLDERS))})")
        env = session.get("env", {})
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            errors.append(f"{where}.session.env: must map names to strings")
            env = {}
        pass_env = session.get("pass_env", [])
        if not isinstance(pass_env, list) or not all(isinstance(n, str) for n in pass_env):
            errors.append(f"{where}.session.pass_env: must be a list of names")
            pass_env = []
        for name in [*env, *pass_env]:
            if not ENV_NAME_RE.match(name):
                errors.append(f"{where}.session: {name!r} is not a valid variable name")
            if name in FORBIDDEN_ENV:
                errors.append(f"{where}.session: {name} not allowed; sessions "
                              "run on the signed-in subscription (docs/SPEC.md §3.2)")
        if command and "=" in command[0]:
            errors.append(f"{where}.session.command: the program name may not contain '='")
        for key, value in env.items():
            for name in re.findall(r"\{(\w*)\}", value):
                if name not in ENV_PLACEHOLDERS:
                    errors.append(f"{where}.session.env.{key}: unknown placeholder "
                                  f"{{{name}}} (known: {', '.join(sorted(ENV_PLACEHOLDERS))})")

    socket = data.get("tmux_socket", "pessoa")
    if not isinstance(socket, str) or not SOCKET_RE.match(socket):
        errors.append(f"{where}.tmux_socket: must match {SOCKET_RE.pattern}")

    isolation = data.get("isolation", "users")
    if isolation not in ISOLATION:
        errors.append(f"{where}.isolation: must be one of {', '.join(ISOLATION)}")
    homes = data.get("homes", "/homes")
    if not isinstance(homes, str) or not SAFE_PATH_RE.match(homes) or homes == "/":
        errors.append(f"{where}.homes: must be an absolute path using [A-Za-z0-9._@+-/]")
    uid_base = data.get("uid_base", 20000)
    if type(uid_base) is not int or not 1000 <= uid_base <= 2**31 - 100000:
        errors.append(f"{where}.uid_base: must be an integer from 1000")

    if errors:
        raise ConfigError(errors)
    return Runtime(klass=klass, work_root=work_root, models=dict(models), command=tuple(command),
                   env=dict(env), pass_env=tuple(pass_env), tmux_socket=socket,
                   isolation=isolation, homes=homes, uid_base=uid_base)


def check_environment(environ: dict[str, str]) -> list[str]:
    """Problems with the supervisor's own environment (docs/SPEC.md §3.2).

    An API key in the environment outranks the subscription sign-in, so
    sessions would silently bill the key instead of the plan.
    """
    return [f"{name} is set in the environment; unset it so sessions use the "
            "signed-in subscription" for name in FORBIDDEN_ENV if environ.get(name)]
