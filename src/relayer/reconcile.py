"""Reconcile running sessions against personas.yaml (docs/SPEC.md §4).

``plan`` is pure: desired personas and running sessions in, actions out.
``apply`` carries the actions out on a backend. Keeping them apart lets the
supervisor log a plan before acting and lets tests cover every case without
tmux.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from .config import Persona
from .runtime import Runtime
from .sessions import Backend, Running


@dataclass(frozen=True)
class Action:
    kind: Literal["start", "stop", "restart"]
    name: str
    reason: str

    def __str__(self) -> str:
        return f"{self.kind} {self.name}: {self.reason}"


_ORDER = {"stop": 0, "restart": 1, "start": 2}


def plan(desired: dict[str, Persona], running: dict[str, Running]) -> list[Action]:
    actions = []
    for name, session in running.items():
        persona = desired.get(name)
        if persona is None:
            actions.append(Action("stop", name, "not in personas.yaml"))
        elif not persona.enabled:
            actions.append(Action("stop", name, "disabled"))
        elif session.config_hash != persona.config_hash():
            actions.append(Action("restart", name, "configuration changed"))
    for name, persona in desired.items():
        if persona.enabled and name not in running:
            actions.append(Action("start", name, "not running"))
    # Stops first, so a restart or start never races a session being removed.
    return sorted(actions, key=lambda a: (_ORDER[a.kind], a.name))


def apply(actions: list[Action], desired: dict[str, Persona], backend: Backend,
          runtime: Runtime) -> list[str]:
    """Carry out ``actions``; returns one error line per action that failed.

    A failing action does not stop the others: one broken persona must not
    keep the rest down.
    """
    errors = []
    for action in actions:
        try:
            if action.kind in ("stop", "restart"):
                backend.stop(action.name)
            if action.kind in ("start", "restart"):
                persona = desired[action.name]
                workdir = persona.folders[0]
                os.makedirs(workdir, exist_ok=True)
                backend.start(action.name, runtime.session_argv(persona), workdir,
                              runtime.env, persona.config_hash())
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(f"{action}: failed: {exc}")
    return errors
