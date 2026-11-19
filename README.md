# pessoa-relayer

A runtime that keeps several long-lived agent **personas** running on one
host, each in its own session, and relays messages to them over chat
channels. The name comes from Fernando Pessoa's heteronyms (one writer, many
distinct voices) and from a relayer that passes messages along.

## Where it fits

This repository is the **runtime layer** in the three-layer model of
[disciplined-project-seed](https://github.com/pdlourenco/disciplined-project-seed)
(its draft ADR-0019):

| Layer | Lives in | What it holds |
|---|---|---|
| Discipline | the seed | contracts, plans, review rules, ADRs |
| Agent bindings | the seed, optional profile | role definitions, generated agent files, forge mappings |
| Runtime | **this repository** | container image, session supervisor, persona config, channels |

The runtime reads the seed's files and never modifies them. It pins the
seed version it targets.

## Ground rules

- **Your own subscription, your own binary.** Each user signs in to their
  own unmodified Claude Code. This code never handles an OAuth token or an
  API key, and there is no hosted service.
- **Nothing host-specific.** Hosts, paths, user IDs, ports and network
  settings are configuration. The repository ships examples, never one
  machine's values.
- **Personas are config.** A `personas.yaml` file is the source of truth for
  which sessions run. The supervisor reconciles running sessions against it,
  so creating, changing or removing a persona is an edit with git history.
- **Templates cap permissions.** A persona can never get broader tools or
  folders than its template allows.

## Status

Early. Planned components, each landing as its own small PR:

1. `personas.yaml` and template format, with a validator (`relayer validate`)
2. reconciler: running sessions against `personas.yaml` (`relayer reconcile`)
3. session supervisor (`relayer supervise`, `relayer wake`)
4. container image and an example compose file (see Deploy)

Messaging channels (Telegram, then Signal) come after these.

## Deploy

Everything host-specific goes in `.env` and a config directory; the
repository holds only examples.

1. Copy `.env.example` to `.env`. Pin `CLAUDE_CODE_VERSION`, set the user
   ids to the owner of your host directories, and fill in the four host
   paths. Back up the Claude, work and state directories.
2. Put `personas.yaml` and `relayer.yaml` in the config directory, starting
   from `examples/`. Check them with `relayer validate`.
3. Build, then sign in once, interactively, with your own subscription:

   ```bash
   docker compose -f compose.example.yaml build
   ```

   ```bash
   docker compose -f compose.example.yaml run --rm relayer claude
   ```

   Complete the login, confirm `/status` shows your plan rather than an API
   key, and exit. The sign-in lives in the Claude directory you mounted.
4. Start the supervisor:

   ```bash
   docker compose -f compose.example.yaml up -d
   ```

Network settings (`network_mode`, `networks`) depend on the host: put them
in a compose override file, not in the example. The container gets no
Docker socket, no privileged mode and no extra capabilities. Updating Claude
Code means changing `CLAUDE_CODE_VERSION` and rebuilding; the image turns
off the updater so the version moves only then.

## Licence

[MIT](LICENSE).
