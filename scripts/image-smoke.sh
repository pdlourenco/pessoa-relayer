#!/usr/bin/env bash
# image-smoke.sh — build the image and check what it promises (README
# §"Deploy", docs/SPEC.md §3.2). Needs a running Docker daemon. CI runs it;
# run it locally before changing the Dockerfile.
#
#   scripts/image-smoke.sh <claude-code-version>
set -euo pipefail
cd "$(dirname "$0")/.."
version="${1:?usage: $0 <claude-code-version>}"
tag="pessoa-relayer:smoke"
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== the version pin is required: a build without it must fail, for that reason"
if out=$(docker build -t pessoa-relayer:nopin . 2>&1); then
  fail "image built without CLAUDE_CODE_VERSION"
fi
grep -q "CLAUDE_CODE_VERSION is required" <<<"$out" \
  || { echo "$out" | tail -20 >&2; fail "no-pin build failed for another reason"; }

echo "== build with the pin"
docker build -t "$tag" --build-arg CLAUDE_CODE_VERSION="$version" .

run() { docker run --rm "$@"; }

echo "== runs as a non-root user"
uid=$(run --entrypoint id "$tag" -u)
[ -n "$uid" ] && [ "$uid" != 0 ] || fail "container runs as root (uid '$uid')"

echo "== sessions get a locale, user and shell"
# shellcheck disable=SC2016  # expanded inside the container, on purpose
run --entrypoint sh "$tag" -c 'test "$LANG" = C.UTF-8 && test "$USER" = agent && test "$SHELL" = /bin/bash' \
  || fail "LANG, USER or SHELL not set"

echo "== claude is installed and on PATH"
run --entrypoint claude "$tag" --version

echo "== update switches are set"
# shellcheck disable=SC2016  # expanded inside the container, on purpose
run --entrypoint sh "$tag" -c 'test "$DISABLE_AUTOUPDATER" = 1 && test "$DISABLE_UPDATES" = 1' \
  || fail "auto-update not disabled"

echo "== shipped examples validate inside the image"
run --entrypoint relayer "$tag" validate --heteronyms /opt/relayer/examples/heteronyms.yaml \
  --runtime /opt/relayer/examples/relayer.yaml

echo "== supervise refuses to start with an API key in the environment"
set +e
run -e ANTHROPIC_API_KEY=sk-smoke -v "$PWD/examples:/config:ro" \
  -e RELAYER_HETERONYMS=/config/heteronyms.yaml -e RELAYER_RUNTIME=/config/relayer.yaml "$tag"
code=$?
set -e
[ "$code" = 2 ] || fail "expected exit 2 from the API-key guard, got $code"

echo "image smoke: all checks passed"
