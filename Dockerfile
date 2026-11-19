# syntax=docker/dockerfile:1
#
# pessoa-relayer runtime image: tmux, git, the relayer, and a pinned Claude
# Code. Nothing host-specific is baked in: paths inside the container are
# fixed, and the compose file maps them to host paths at deploy time.

FROM debian:bookworm-slim

# Pin Claude Code and update deliberately (rebuild with a new version).
ARG CLAUDE_CODE_VERSION
ARG AGENT_UID=1000
ARG AGENT_GID=1000

RUN test -n "$CLAUDE_CODE_VERSION" \
      || { echo "build arg CLAUDE_CODE_VERSION is required (pin a version)" >&2; exit 1; }

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git tmux tini python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

# The GID may already exist in Debian (e.g. 20, macOS's staff, is dialout
# here); reuse it rather than fail. -l keeps a large UID from bloating lastlog.
RUN (getent group "$AGENT_GID" >/dev/null || groupadd --gid "$AGENT_GID" agent) \
 && useradd -l --uid "$AGENT_UID" --gid "$AGENT_GID" --create-home --shell /bin/bash agent \
 && mkdir -p /work /state /config /home/agent/.claude \
 && chown "$AGENT_UID:$AGENT_GID" /work /state /home/agent/.claude

COPY pyproject.toml README.md /opt/relayer/src-pkg/
COPY src /opt/relayer/src-pkg/src
COPY templates /opt/relayer/templates
COPY examples /opt/relayer/examples
RUN python3 -m venv /opt/relayer/venv \
 && /opt/relayer/venv/bin/pip install --no-cache-dir /opt/relayer/src-pkg \
 && ln -s /opt/relayer/venv/bin/relayer /usr/local/bin/relayer

USER agent
WORKDIR /home/agent
# pipefail: under plain sh a failed curl feeds bash nothing, bash exits 0,
# and the image would build without claude.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Native installer, pinned (code.claude.com/docs/en/setup, "Install a specific
# version"). DISABLE_AUTOUPDATER stops the background update check;
# DISABLE_UPDATES also blocks a session running "claude update", so the
# version only moves when the image is rebuilt. CLAUDE_CONFIG_DIR keeps
# settings, credentials and .claude.json in the one mounted volume.
RUN curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
 && /home/agent/.local/bin/claude --version
# Sessions start with `env -i` and get HOME, PATH, LANG, USER and SHELL from
# here (docs/SPEC.md §4.2); Docker sets only HOME and PATH by itself.
ENV PATH=/home/agent/.local/bin:$PATH \
    LANG=C.UTF-8 \
    USER=agent \
    SHELL=/bin/bash \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    RELAYER_HETERONYMS=/config/heteronyms.yaml \
    RELAYER_RUNTIME=/config/relayer.yaml \
    RELAYER_TEMPLATES=/opt/relayer/templates \
    RELAYER_WORK_ROOT=/work \
    RELAYER_STATE=/state

VOLUME ["/home/agent/.claude", "/work", "/state"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["relayer", "supervise"]
