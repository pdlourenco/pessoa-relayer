# pessoa-relayer

A runtime that keeps several long-lived agent **heteronyms** running on one
host, each in its own session, and relays messages to them over chat
channels. A heteronym is Fernando Pessoa's word for each of the distinct
writers he invented and wrote as; here it is one agent with its own role,
tools, folders and memory. The relayer passes messages along.

## Where it fits

This repository is the **runtime layer** in the three-layer model of
[disciplined-project-seed](https://github.com/pdlourenco/disciplined-project-seed)
(its draft ADR-0019):

| Layer | Lives in | What it holds |
|---|---|---|
| Discipline | the seed | contracts, plans, review rules, ADRs |
| Agent bindings | the seed, optional profile | role definitions, generated agent files, forge mappings |
| Runtime | **this repository** | container image, session supervisor, heteronym config, channels |

The runtime reads the seed's files and never modifies them. It pins the
seed version it targets.

## Ground rules

- **Your own subscription, your own binary.** Each user signs in to their
  own unmodified Claude Code. This code never handles an OAuth token or an
  API key, and there is no hosted service.
- **Nothing host-specific.** Hosts, paths, user IDs, ports and network
  settings are configuration. The repository ships examples, never one
  machine's values.
- **Heteronyms are config, kept elsewhere.** A `heteronyms.yaml` file is the
  source of truth for which sessions run. It lives in the deployment's own
  private definitions repository, mounted read-only at a configured path;
  this repository ships only examples. The supervisor reconciles running
  sessions against it, so creating, changing or removing a heteronym is an
  edit with git history.
- **Templates cap permissions.** A heteronym can never get broader tools or
  folders than its template allows. Memory grants nothing: permissions come
  only from definitions.
- **Isolation by operating-system user.** Each heteronym's session runs as
  its own Unix user, and its folders belong to that user alone.
- **One container per trust class.** Heteronyms with the same tools and
  credentials share a container; each container runs only its own class.
  No heteronym ever creates a container.
- **No private content here.** This repository is public. Host names, paths,
  the layout of any one deployment, and anything from the private
  repositories stay out of it.

## Status

Early. Planned components, each landing as its own small PR:

1. `heteronyms.yaml` and template format, with a validator (`relayer validate`)
2. reconciler: running sessions against `heteronyms.yaml` (`relayer reconcile`)
3. session supervisor (`relayer supervise`, `relayer wake`)
4. container image and an example compose file (see Deploy)

Messaging channels (Telegram, then Signal) come after these.

## Deploy

Everything host-specific goes in `.env` and a config directory; the
repository holds only examples.

1. Copy `.env.example` to `.env`. Pin `CLAUDE_CODE_VERSION`, set the user
   ids, and fill in the four host paths. Create the Claude, work and state
   directories yourself and give them to that user (`chown AGENT_UID:AGENT_GID`):
   if compose creates a missing one, it is owned by root and the container
   cannot write it. Back up those three.
2. Put `heteronyms.yaml` and `relayer.yaml` in the config directory, starting
   from `examples/`. Keep `CLAUDE_CONFIG_DIR`, `DISABLE_AUTOUPDATER` and
   `DISABLE_UPDATES` in `relayer.yaml`'s `pass_env`: sessions inherit nothing
   else, and without `CLAUDE_CONFIG_DIR` they would not find the sign-in.
3. Build, check the config inside the image, then sign in once,
   interactively, with your own subscription:

   ```bash
   docker compose -f compose.example.yaml build
   ```

   ```bash
   docker compose -f compose.example.yaml run --rm relayer relayer validate
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
