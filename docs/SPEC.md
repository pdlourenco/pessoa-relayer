# Contracts

The files below cross a process boundary: people and tools write them, the
supervisor reads them. Implementations must match this text; when a contract
changes, this file changes first, in the same PR as the code.

## 1. `personas.yaml`

The source of truth for which persona sessions run. One file per deployment;
`examples/personas.yaml` is a starting point. It lives outside this
repository's tree in a real deployment, because it holds that deployment's
chat identifiers.

### 1.1 Shape

```yaml
version: 1
personas:
  <name>:
    template: <template name>      # required
    tier: <tier>                   # default: the template's default_tier
    channel:                       # optional; omit for no chat channel
      kind: <channel kind>
      chat: <chat or group identifier, as the channel names it>
    folders: [<folder pattern>, …] # default: the template's folders
    permissions: [<rule>, …]       # default: the template's allow list
    idle_stop: <duration>          # default: the template's idle_stop
    enabled: true | false          # default: true
```

- `<name>` matches `^[a-z][a-z0-9-]{0,31}$`. It names the session and is
  substituted for `{name}` in folder patterns.
- `<tier>` is one of `fast`, `standard`, `strong`, `frontier`, the abstract
  tiers of disciplined-project-seed (draft ADR-0020). The runtime maps tiers
  to models; personas never name a model.
- A `<folder pattern>` is an absolute path after substituting `{work}` (the
  work root, `/work` inside the container unless configured) and `{name}`.
  No other placeholders are allowed. The resolved path may only use
  `A–Z a–z 0–9 . _ @ + - /`: tmux expands formats such as `#(cmd)` in a
  session's start directory and treats a trailing `;` as a command
  separator, so neither may reach it.
- `<duration>` is `<n>m`, `<n>h`, `<n>d` (n ≥ 1) or `never`.
- Unknown keys anywhere are errors. Duplicate keys are errors.

Verified by: `tests/test_config.py` (`Rejections`, `ValidPersona`).

### 1.2 Resolution

A persona's effective settings are its own values where given, else its
template's. Folders are resolved and normalised (`..` collapsed) before any
check. The template's `deny` list always applies and cannot be narrowed.

Verified by: `tests/test_config.py::ValidPersona`.

### 1.3 Isolation

No two personas may share a folder, and no persona's folder may sit inside
another's. Personas exchange work through the forge or notes, never through
shared folders. No two personas may use the same channel `kind` and `chat`,
since both sessions would answer every message there.

Paths are compared after collapsing repeated leading slashes (`//work` is
`/work`). No folder may resolve to `/`, and the work root must be an
absolute, normalised path below `/` without `{` or `}`.

Verified by: `tests/test_config.py::Rejections` (`test_overlapping_personas`,
`test_same_folder_twice`, `test_nesting_in_the_other_direction`,
`test_double_slash_cannot_dodge_isolation`, `test_shared_chat`,
`test_work_root_slash_and_braces`).

### 1.4 Configuration hash

Each persona has a hash of its own settings: template, tier, channel,
resolved folders, allow and deny lists. The reconciler combines it with the
launch command (§4) into the session hash that decides restarts. `enabled` and `idle_stop` are not in
the hash: they decide whether a session runs, not how.

Verified by: `tests/test_config.py::ValidPersona::test_hash_changes_with_shape_not_with_enabled`.

## 2. Templates: `templates/<name>/template.yaml`

A template is a directory; its name is the template name. It caps what any
persona built from it may get. Later PRs add the files a template scaffolds
into a new persona's folder (CLAUDE.md, settings).

### 2.1 Shape

```yaml
version: 1                      # required
description: <one line>         # default ""
max_tier: <tier>                # required
default_tier: <tier>            # required; not above max_tier
folders: [<folder pattern>, …]  # required, non-empty; roots persona folders sit within
permissions:                    # default: both lists empty
  allow: [<rule>, …]            # the most a persona may be granted
  deny: [<rule>, …]             # always applied
channel_kinds: [<kind>, …]      # default []: personas get no channel
idle_stop: <duration>           # default never
chat_creatable: true | false    # default false
```

In `personas.yaml`, an explicit `folders: []` is an error, `personas:` must
be a mapping (empty or absent means no personas), and `version` must be the
integer `1`.

`<rule>` is an opaque permission string passed to the agent tool (for
Claude Code, a permission rule such as `Bash(git:*)`). The runtime compares
rules as exact strings; it never interprets them.

### 2.2 Caps

A persona can never exceed its template:

- its tier is at most `max_tier`;
- each of its permissions appears verbatim in `permissions.allow`;
- each of its folders resolves to a template root or a path inside one;
- its channel kind is in `channel_kinds`.

`chat_creatable: false` marks a template (for example one with repository
write access or a forge token) whose personas may only be added by editing
`personas.yaml`, never from a chat request.

Verified by: `tests/test_config.py::Rejections` (one case per cap).

### 2.3 Caps reach the session, not the workspace

The runtime passes a persona's allow and deny lists to the agent tool at
launch (a later PR). The tool also reads the workspace's own settings, which
the persona can edit, and allow lists merge across levels. A template should
therefore deny edits to the workspace's settings directory; for Claude Code,
`Edit(./.claude/**)`. Deny wins at every level in Claude Code
(<https://code.claude.com/docs/en/permissions>, retrieved 2026-11-19). Two
limits remain: an `Edit` rule does not cover writes made through a shell,
and a permission-skipping mode ignores it. Which mechanism enforces the caps
in practice (permission rules or a pre-tool hook) is a deployment choice
this runtime does not make.

## 3. Runtime config: `relayer.yaml`

How sessions are launched on one host. `examples/relayer.yaml` is a Claude
Code starting point; nothing in it names a host.

### 3.1 Shape

```yaml
version: 1                      # required
work_root: <absolute path>      # default /work; --work-root or RELAYER_WORK_ROOT override it
models:                         # required: one model for every tier
  fast: <model>
  standard: <model>
  strong: <model>
  frontier: <model>
session:
  command: [<arg>, …]           # required; placeholders below; argv[0] has no "="
  pass_env: [<NAME>, …]         # variables copied from the supervisor's environment
  env: {<NAME>: <value>, …}     # literal variables for every session
tmux_socket: <name>             # default pessoa; [A-Za-z0-9_-]{1,64}
```

`command` arguments may contain these placeholders, and no others:

| Placeholder | Becomes |
|---|---|
| `{model}` | `models[<persona tier>]` |
| `{name}` | the persona name |
| `{workdir}` | the persona's first folder |
| `{settings}` | `{"permissions":{"allow":[…],"deny":[…]}}`, the persona's resolved lists as compact JSON |

For Claude Code, `--settings {settings}` passes the caps at launch; the tool
merges them with the workspace's own settings (§2.3).

Verified by: `tests/test_runtime.py::RuntimeConfig`.

### 3.2 No API key

Sessions run on the signed-in subscription. These variables move a Claude
Code session off it: `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` outrank
the sign-in, and `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` route
to a cloud account. `relayer reconcile` refuses to act (exit 2) while any is
set and non-empty in its environment, before touching any session, and
neither `session.env` nor `session.pass_env` may name one. Sessions inherit
nothing else either (§4.2), so a variable reaches a session only if this
file lists it.

Values in `env` and `pass_env` appear in the session's command line, which
other processes of the same user can read, as they can its environment. Do
not put secrets there.

Verified by: `tests/test_runtime.py::ApiKeyGuard`,
`tests/test_runtime.py::RuntimeConfig::test_api_key_in_session_env`.

## 4. Reconciliation

`relayer reconcile` compares enabled personas with the sessions running on
the runtime's tmux socket, and acts:

| Session state | Persona | Action |
|---|---|---|
| running | absent from `personas.yaml` | stop |
| running | disabled | stop |
| running, session hash differs or missing | enabled | restart |
| not running | enabled | start |

Stops run first, then restarts, then starts, each group in name order. One
failed action is reported and does not stop the others. Before a start, the
persona's first folder is created if missing. The runtime only manages
sessions on its own tmux socket.

The **session hash** covers the persona hash (§1.4), the persona's model,
the command template, the literal `env` and the names in `pass_env`, so
editing `relayer.yaml` restarts the sessions it affects. It never covers the
values of inherited variables: a reconcile run from a shell with a different
`PATH` must not restart every session.

A session name on the socket that is not a valid persona name makes the
reconcile stop with an error rather than guess (fail closed). Each session carries
its hash in the tmux user option `@relayer_hash`.

Verified by: `tests/test_reconcile.py`, `tests/test_runtime.py`
(`test_session_hash_tracks_runtime_changes`), `tests/test_tmux.py` (real
tmux; CI installs it).

### 4.2 Session isolation

- The session command is one shell-quoted string,
  `env -i <NAME=value …> <argv …>`. A session's environment is exactly
  `HOME`, `PATH`, `LANG`, `USER` and `SHELL` from the supervisor (when set),
  `TERM=screen-256color`, `pass_env`, then `env`; it inherits nothing from
  the tmux server or whichever process started it. `PATH` must end up set,
  or the start is reported as failed: `env -i` would otherwise fall back to
  a libc default and the agent binary would not be found.
- tmux runs with `-f /dev/null` (no user config) and a minimal environment.
- The start directory has `#` doubled, as a second guard behind §1.1's
  character set.
- A command ending in `;`, an unsafe session name or a non-hex hash is
  refused.

Verified by: `tests/test_tmux.py` (`test_session_inherits_nothing`,
`test_hash_in_workdir_is_literal`,
`test_trailing_semicolon_argument_cannot_chain`), `tests/test_config.py`
(`test_folder_characters_tmux_would_interpret`).

## 5. Supervisor

`relayer supervise [--interval 30] [--state DIR]` reconciles (§4) every
interval, until SIGTERM or SIGINT. It refuses to start (exit 2) under §3.2,
and exits 1 if the first configuration is invalid, the interval is not
positive, or the state directory cannot be created and written. An
exception in one pass is logged and the loop goes on.

- **Bad edits are survived.** If a later reload fails validation, the
  errors are logged and the last valid configuration stays in force: a typo
  in `personas.yaml` must not stop every session.
- **Idle stop.** A running, enabled persona with an `idle_stop` is stopped
  once that long has passed since its session's last activity (tmux
  `window_activity`, the last output in the pane: a heuristic for "in use";
  tmux's `session_activity` only moves with client input). It is then
  **parked**: a marker file `DIR/parked/<name>` keeps it stopped,
  across supervisor restarts, until `relayer wake <name>` removes the
  marker. The marker is written before the stop; if it cannot be written,
  the session is left running and the failure logged. Parks of personas
  that are removed or disabled are cleared.
- **Crash back-off.** A session gone within 60 s of being started counts as
  a crash. Its next start waits `interval × 2^min(crashes, 10)`, at most
  900 s, and
  each held start is logged. A session that stays up 300 s clears its count.
  An idle stop or a deliberate stop is never a crash.
- **The supervisor does not stop sessions on exit.** Outside a container,
  sessions survive a supervisor restart. In the image, the supervisor runs
  under tini as the container's main process, so its exit ends the
  container and every session with it; the container's restart policy then
  brings everything back.
- Log lines go to stdout, one per action, with a timestamp.

Verified by: `tests/test_supervise.py`.
