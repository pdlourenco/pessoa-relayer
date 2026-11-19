# Contracts

The files below cross a process boundary: people and tools write them, the
supervisor reads them. Implementations must match this text; when a contract
changes, this file changes first, in the same PR as the code.

## 1. `heteronyms.yaml`

The source of truth for which heteronym sessions run. One file per
deployment; `examples/heteronyms.yaml` is a starting point. In a deployment
it lives, with the templates, in a private definitions repository mounted
read-only at a configured path (`--heteronyms`, `--templates`); the runtime
never reads the copies in this repository. Heteronyms can read their
definitions and never write them, because definitions decide what each may
do.

### 1.1 Shape

```yaml
version: 1
heteronyms:
  <name>:
    class: <trust class>           # required (§1.5)
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

- `<name>` matches `^[a-z][a-z0-9-]{0,29}$`. It names the session and the
  heteronym's Unix user (`h-<name>`, within Linux's 32 characters, §6), and
  is substituted for `{name}` in folder patterns.
- `<tier>` is one of `fast`, `standard`, `strong`, `frontier`, the abstract
  tiers of disciplined-project-seed (draft ADR-0020). The runtime maps tiers
  to models; heteronyms never name a model.
- A `<folder pattern>` is an absolute path after substituting `{work}` (the
  work root, `/work` inside the container unless configured) and `{name}`.
  No other placeholders are allowed. The resolved path may only use
  `A–Z a–z 0–9 . _ @ + - /`: tmux expands formats such as `#(cmd)` in a
  session's start directory and treats a trailing `;` as a command
  separator, so neither may reach it.
- `<duration>` is `<n>m`, `<n>h`, `<n>d` (n ≥ 1) or `never`.
- Unknown keys anywhere are errors. Duplicate keys are errors.

Verified by: `tests/test_config.py` (`Rejections`, `ValidHeteronym`).

### 1.2 Resolution

A heteronym's effective settings are its own values where given, else its
template's. Folders are resolved and normalised (`..` collapsed) before any
check. The template's `deny` list always applies and cannot be narrowed.

Verified by: `tests/test_config.py::ValidHeteronym`.

### 1.3 Isolation

Within one trust class, no two heteronyms may share a folder, and no
heteronym's folder may sit inside another's. (Classes run in separate
containers with separate work roots.) Heteronyms exchange work through the
forge or notes, never through shared folders. No two heteronyms, in any
class, may use the same channel `kind` and `chat`, since both sessions would
answer every message there. A container loading only its own class cannot
see the others, so this cross-class check runs when the whole file is
validated (`relayer validate` with no `--class`), for example in the
definitions repository's own CI.

Paths are compared after collapsing repeated leading slashes (`//work` is
`/work`). No folder may resolve to `/`, and the work root must be an
absolute, normalised path below `/` without `{` or `}`.

Verified by: `tests/test_config.py::Rejections` (`test_overlapping_heteronyms`,
`test_same_folder_twice`, `test_nesting_in_the_other_direction`,
`test_double_slash_cannot_dodge_isolation`, `test_shared_chat`,
`test_work_root_slash_and_braces`).

### 1.4 Configuration hash

Each heteronym has a hash of its own settings: class, template, tier,
channel,
resolved folders, allow and deny lists. The reconciler combines it with the
launch command (§4) into the session hash that decides restarts. `enabled` and `idle_stop` are not in
the hash: they decide whether a session runs, not how.

Verified by: `tests/test_config.py::ValidHeteronym::test_hash_changes_with_shape_not_with_enabled`.

### 1.5 Trust classes

Heteronyms with the same tools and credentials form a **trust class**, and
each class runs in its own container from the same image. A container loads
only its class: `class` in `relayer.yaml` (§3), which `--class` or
`RELAYER_CLASS` override. Entries of other classes are skipped without being
validated: their errors belong to the container that runs them.

A heteronym's template must list its class in `classes` (§2), so a template
with forge write access cannot be used in a class that has none. New classes
are made by the owner, by hand: no heteronym creates a container, and a
heteronym created from chat is created only in the requester's own class.

Verified by: `tests/test_config.py::TrustClass`,
`tests/test_config.py::Rejections::test_template_not_for_this_class`.

## 2. Templates: `templates/<name>/template.yaml`

A template is a directory; its name is the template name. It caps what any
heteronym built from it may get. Later PRs add the files a template scaffolds
into a new heteronym's folder (CLAUDE.md, settings).

### 2.1 Shape

```yaml
version: 1                      # required
description: <one line>         # default ""
classes: [<trust class>, …]     # required, non-empty (§1.5)
max_tier: <tier>                # required
default_tier: <tier>            # required; not above max_tier
folders: [<folder pattern>, …]  # required, non-empty; roots heteronym folders sit within
permissions:                    # default: both lists empty
  allow: [<rule>, …]            # the most a heteronym may be granted
  deny: [<rule>, …]             # always applied
channel_kinds: [<kind>, …]      # default []: heteronyms get no channel
idle_stop: <duration>           # default never
chat_creatable: true | false    # default false
```

In `heteronyms.yaml`, an explicit `folders: []` is an error, `heteronyms:` must
be a mapping (empty or absent means no heteronyms), and `version` must be the
integer `1`.

`<rule>` is an opaque permission string passed to the agent tool (for
Claude Code, a permission rule such as `Bash(git:*)`). The runtime compares
rules as exact strings; it never interprets them.

### 2.2 Caps

A heteronym can never exceed its template:

- its tier is at most `max_tier`;
- each of its permissions appears verbatim in `permissions.allow`;
- each of its folders resolves to a template root or a path inside one;
- its channel kind is in `channel_kinds`.

`chat_creatable: false` marks a template (for example one with repository
write access or a forge token) whose heteronyms may only be added by editing
`heteronyms.yaml`, never from a chat request.

Verified by: `tests/test_config.py::Rejections` (one case per cap).

### 2.3 Caps reach the session, not the workspace

The runtime passes a heteronym's allow and deny lists to the agent tool at
launch (a later PR). The tool also reads the workspace's own settings, which
the heteronym can edit, and allow lists merge across levels. A template should
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
class: <trust class>            # required: the class this container runs (§1.5)
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
isolation: users | none         # default users (§6)
homes: <absolute path>          # default /homes: each heteronym's home (§6)
uid_base: <integer>             # default 20000: first uid handed out (§6)
```

`command` arguments may contain these placeholders, and no others:

| Placeholder | Becomes |
|---|---|
| `{model}` | `models[<heteronym tier>]` |
| `{name}` | the heteronym name |
| `{workdir}` | the heteronym's first folder |
| `{settings}` | `{"permissions":{"allow":[…],"deny":[…]}}`, the heteronym's resolved lists as compact JSON |

Values in `env` may use `{home}`, `{name}` and `{user}`, the heteronym's own
home, name and Unix user; for Claude Code, `CLAUDE_CONFIG_DIR: "{home}/.claude"`
keeps each heteronym's settings, history and sign-in in its own home.

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

`relayer reconcile` compares enabled heteronyms with the sessions running on
the runtime's tmux socket, and acts:

| Session state | Heteronym | Action |
|---|---|---|
| running | absent from `heteronyms.yaml` | stop |
| running | disabled | stop |
| running, session hash differs or missing | enabled | restart |
| not running | enabled | start |

Stops run first, then restarts, then starts, each group in name order. One
failed action is reported and does not stop the others. Before a start, the
heteronym's first folder is created if missing. The runtime only manages
sessions on its own tmux socket.

The **session hash** covers the heteronym hash (§1.4), the heteronym's model,
the command template, the literal `env` and the names in `pass_env`, so
editing `relayer.yaml` restarts the sessions it affects. It never covers the
values of inherited variables: a reconcile run from a shell with a different
`PATH` must not restart every session.

A session name on the socket that is not a valid heteronym name makes the
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
  in `heteronyms.yaml` must not stop every session.
- **Idle stop.** A running, enabled heteronym with an `idle_stop` is stopped
  once that long has passed since its session's last activity (tmux
  `window_activity`, the last output in the pane: a heuristic for "in use";
  tmux's `session_activity` only moves with client input). It is then
  **parked**: a marker file `DIR/parked/<name>` keeps it stopped,
  across supervisor restarts, until `relayer wake <name>` removes the
  marker. The marker is written before the stop; if it cannot be written,
  the session is left running and the failure logged. Parks of heteronyms
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

## 6. Isolation by operating-system user

Each heteronym runs as its own Unix user, so no session can read another's
folders, home or memory, whatever its permission rules say. This is the
default (`isolation: users`).

### 6.1 Users

- Heteronym `<name>` runs as user and group `h-<name>`.
- Its uid comes from `users.json` in the state directory, handed out from
  `uid_base` in order and never reused for another name. A recreated
  container, whose `/etc/passwd` starts empty, recreates each user with its
  old uid, so its folders stay its own.
- The user is created the first time the heteronym starts. Only heteronyms
  of the container's own class (§1.5) ever get a user.

### 6.2 Folders

The heteronym's home (`<homes>/<name>`) and every folder in its definition
are owned by its user, mode 700. A missing folder under a root-owned parent
is made by the supervisor and handed over; one under the heteronym's own
folders is made as the heteronym. A folder owned by anyone else, or a
symlink, is refused and the session is not started.

### 6.3 Sessions and sign-in

- Each heteronym has its own tmux server, run as its user with no
  capabilities (`setpriv --reuid --regid --clear-groups`). Its session's
  `HOME`, `USER` and `LOGNAME` are its own (§4.2).
- Each heteronym signs in once, on its own: `relayer login <name>` runs the
  agent program interactively as that user, in its workspace, with the
  session's environment. Its credentials then live in its own home.

### 6.4 Capabilities

The supervisor runs as root inside its container with exactly `CAP_CHOWN`,
`CAP_SETUID` and `CAP_SETGID`; the container drops every other capability
and keeps `no-new-privileges` (setpriv is not a setuid program, so that
flag does not stop it).

Two shapes were possible: (a) an entrypoint that creates every user as
root and then drops all capabilities before starting the supervisor, or
(b) a supervisor that keeps these three. This is (b). Under (a) a heteronym
created at run time, from chat, needs the container restarted to get its
user, and a restart ends every session in the class, losing each one's
context, every time one is created. Under (b) the supervisor holds no
`DAC_OVERRIDE` or `DAC_READ_SEARCH`, so even it cannot read inside a
heteronym's folders; what it must do there, it does as that heteronym.

### 6.5 Without isolation

`isolation: none` runs every session as the supervisor's own user, with no
users, no ownership changes and no isolation between heteronyms. It is for
running outside a container: tests and single-user development.

Verified by: `tests/test_users.py` (`RealUsers` creates real users and runs
one case with only the three capabilities; CI runs it as root), and
`scripts/image-smoke.sh` (the image under compose's exact capabilities: the
session's process is `h-<name>` with no capabilities, its folders are 700,
and root in the container cannot read them).
