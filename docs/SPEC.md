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
  No other placeholders are allowed.
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

Each persona has a hash of everything that shapes its running session:
template, tier, channel, resolved folders, allow and deny lists. A change of
hash means the session must restart. `enabled` and `idle_stop` are not in
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
  command: [<arg>, …]           # required; placeholders below
  env: {<NAME>: <value>, …}     # extra environment for every session
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

Sessions run on the signed-in subscription. An `ANTHROPIC_API_KEY` in the
environment outranks that sign-in, so sessions would bill the key instead.
`relayer reconcile` refuses to act (exit 2) while the variable is set and
non-empty, before touching any session, and `session.env` may not set it.

Verified by: `tests/test_runtime.py::ApiKeyGuard`,
`tests/test_runtime.py::RuntimeConfig::test_api_key_in_session_env`.

## 4. Reconciliation

`relayer reconcile` compares enabled personas with the sessions running on
the runtime's tmux socket, and acts:

| Session state | Persona | Action |
|---|---|---|
| running | absent from `personas.yaml` | stop |
| running | disabled | stop |
| running, hash differs or missing | enabled | restart |
| not running | enabled | start |

Stops run first, then restarts, then starts, each group in name order. One
failed action is reported and does not stop the others. Before a start, the
persona's first folder is created if missing. The runtime only manages
sessions on its own tmux socket. Each session carries its configuration
hash (§1.4) in the tmux user option `@relayer_hash`.

Verified by: `tests/test_reconcile.py`, `tests/test_tmux.py` (real tmux,
skipped where it is not installed).
