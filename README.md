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
4. container image and an example compose file

Messaging channels (Telegram, then Signal) come after these.

## Licence

[MIT](LICENSE).
