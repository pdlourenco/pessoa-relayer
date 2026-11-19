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

Each trust class runs as its own container from the same published image
(`ghcr.io/pdlourenco/pessoa-relayer:<tag>`, built and smoke-tested by the
release workflow). A deployment keeps its compose file, `.env` and
`relayer.yaml` files outside this repository; `compose.example.yaml` and
`.env.example` show the shape.

1. Copy `.env.example` to `.env` and pin `RELAYER_IMAGE_TAG` to an exact
   release. Fill in the host paths. The homes, work and state directories
   stay owned by root; the supervisor gives each heteronym its own Unix user
   and folders inside them (docs/SPEC.md §6). Back up those three.
2. Mount your private definitions repository (`heteronyms.yaml` and
   `templates/`) read-only at `/definitions`. Check it whole, including the
   cross-class checks, with `relayer validate` and no `--class`.
3. Write one `relayer.yaml` per container, starting from
   `examples/relayer.yaml`; its `class` picks the heteronyms that container
   runs. Keep `CLAUDE_CONFIG_DIR: "{home}/.claude"` in its `env` and the
   two update switches in its `pass_env`: sessions inherit nothing else.
4. Check the configuration inside the image and start the supervisor:

   ```bash
   docker compose run --rm assistants relayer validate
   ```

   ```bash
   docker compose up -d
   ```

5. For memory, clone your private memory repository into the host path
   given as `RELAYER_MEMORY_REPO_DIR`, and give root in the container a way
   to push to it (a read-only mounted key or credential file). Omit the
   `memory` section in `relayer.yaml` to run without it.
6. Sign each heteronym in once, interactively, with your own subscription:

   ```bash
   docker compose exec assistants relayer login pa
   ```

   Complete the login, confirm `/status` shows your plan rather than an API
   key, and exit. The sign-in lives in that heteronym's own home.

Network settings (`network_mode`, `networks`) depend on the host: put them
in a compose override file. The container gets no Docker socket and no
privileged mode; the supervisor keeps only `CHOWN`, `SETUID` and `SETGID`,
which it needs to create one user per heteronym (docs/SPEC.md §6.4).

Updating Claude Code means a new release: bump `.claude-code-version` in a
PR (CI tests that exact version), tag, and move the deployment's pinned tag.
The image turns off the updater so the version moves only then.

Optional tools are build arguments, all off by default: `INSTALL_GH=1`,
`TEA_VERSION`, `GLAB_VERSION`, `BUN_VERSION`, and `EXTRA_APT_PACKAGES` for
language toolchains. Build your own image with the ones a class needs.

## Licence

[MIT](LICENSE).
