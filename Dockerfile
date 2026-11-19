# syntax=docker/dockerfile:1
#
# pessoa-relayer runtime image: tmux, git, the relayer, and a pinned Claude
# Code. Nothing host-specific is baked in: paths inside the container are
# fixed, and the compose file maps them to host paths at deploy time.

FROM debian:bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/pdlourenco/pessoa-relayer" \
      org.opencontainers.image.licenses="MIT"

# Pin Claude Code and update deliberately (rebuild with a new version).
ARG CLAUDE_CODE_VERSION

LABEL io.github.pessoa-relayer.claude-code-version="$CLAUDE_CODE_VERSION"
RUN test -n "$CLAUDE_CODE_VERSION" \
      || { echo "build arg CLAUDE_CODE_VERSION is required (pin a version)" >&2; exit 1; }

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git procps tmux tini python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

# Optional extras: all off by default, each pinned when turned on. CI's
# "image extras" job builds with every one of them, so a broken download
# fails there rather than in a deployment.
ARG EXTRA_APT_PACKAGES=""
ARG INSTALL_GH=""
ARG TEA_VERSION=""
ARG GLAB_VERSION=""
ARG BUN_VERSION=""
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN arch="$(dpkg --print-architecture)"; \
    apt_extra="$EXTRA_APT_PACKAGES${INSTALL_GH:+ gh}${BUN_VERSION:+ unzip}"; \
    if [ -n "${apt_extra// /}" ]; then \
      apt-get update && apt-get install -y --no-install-recommends $apt_extra \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
 && if [ -n "$TEA_VERSION" ]; then \
      curl -fsSL -o /usr/local/bin/tea \
        "https://dl.gitea.com/tea/${TEA_VERSION}/tea-${TEA_VERSION}-linux-${arch}" \
      && chmod 755 /usr/local/bin/tea && tea --version; \
    fi \
 && if [ -n "$GLAB_VERSION" ]; then \
      curl -fsSL -o /tmp/glab.deb \
        "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/${GLAB_VERSION}/glab_${GLAB_VERSION}_linux_${arch}.deb" \
      && dpkg -i /tmp/glab.deb && rm /tmp/glab.deb && glab --version; \
    fi \
 && if [ -n "$BUN_VERSION" ]; then \
      curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash -s "bun-v${BUN_VERSION}" \
      && bun --version; \
    fi

# Root-owned mount points. The supervisor runs as root with only CAP_CHOWN,
# CAP_SETUID and CAP_SETGID (compose drops the rest) and gives each heteronym
# its own Unix user, home and folders at run time (docs/SPEC.md §6).
RUN mkdir -p /work /state /config /definitions /homes \
 && chmod 755 /work /homes && chmod 700 /state

COPY pyproject.toml README.md /opt/relayer/src-pkg/
COPY src /opt/relayer/src-pkg/src
# Examples only: a deployment mounts its own definitions at /definitions.
COPY examples /opt/relayer/examples
COPY templates /opt/relayer/examples/templates
RUN python3 -m venv /opt/relayer/venv \
 && /opt/relayer/venv/bin/pip install --no-cache-dir /opt/relayer/src-pkg \
 && ln -s /opt/relayer/venv/bin/relayer /usr/local/bin/relayer

# Native installer, pinned (code.claude.com/docs/en/setup, "Install a specific
# version"), into a shared, read-only location every heteronym user can run.
# DISABLE_AUTOUPDATER stops the background update check; DISABLE_UPDATES
# also blocks a session running "claude update", so the version only moves
# when the image is rebuilt. Each session's CLAUDE_CONFIG_DIR is its own
# home (relayer.yaml), so settings, history and sign-in are per heteronym.
RUN curl -fsSL https://claude.ai/install.sh | HOME=/opt/claude bash -s "$CLAUDE_CODE_VERSION" \
 && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude \
 && chmod -R a+rX,go-w /opt/claude \
 && claude --version
# Sessions start with `env -i`: HOME, USER and LOGNAME are the heteronym's
# own; PATH, LANG and SHELL come from here (docs/SPEC.md §4.2).
ENV LANG=C.UTF-8 \
    SHELL=/bin/bash \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    RELAYER_HETERONYMS=/definitions/heteronyms.yaml \
    RELAYER_RUNTIME=/config/relayer.yaml \
    RELAYER_TEMPLATES=/definitions/templates \
    RELAYER_WORK_ROOT=/work \
    RELAYER_STATE=/state

VOLUME ["/homes", "/work", "/state"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["relayer", "supervise"]
