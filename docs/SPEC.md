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
shared folders.

Verified by: `tests/test_config.py::Rejections::test_overlapping_personas`.

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
version: 1
description: <one line>
max_tier: <tier>
default_tier: <tier>            # not above max_tier
folders: [<folder pattern>, …]  # roots; persona folders must sit within one
permissions:
  allow: [<rule>, …]            # the most a persona may be granted
  deny: [<rule>, …]             # always applied
channel_kinds: [<kind>, …]      # channel kinds a persona may use
idle_stop: <duration>           # default for personas; default never
chat_creatable: true | false    # default false
```

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
