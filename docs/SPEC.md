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

- `<name>` matches `^[a-z][a-z0-9-]{0,31}$`. It names the session and is
  substituted for `{name}` in folder patterns.
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
only its class (`--class` or `RELAYER_CLASS`; the runtime config's `class`
once it exists). Entries of other classes are skipped without being
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
| `{model}` | `models[<heteronym tier>]` |
| `{name}` | the heteronym name |
| `{workdir}` | the heteronym's first folder |
| `{settings}` | `{"permissions":{"allow":[…],"deny":[…]}}`, the heteronym's resolved lists as compact JSON |

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
