#!/usr/bin/env bash
# image-smoke.sh — build the image and check what it promises (README
# §"Deploy", docs/SPEC.md §3.2 and §6). Needs a running Docker daemon. CI and
# the release workflow run it; run it locally before changing the Dockerfile.
#
#   scripts/image-smoke.sh "$(cat .claude-code-version)"
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
# The capabilities compose.example.yaml grants, and nothing else.
caps=(--cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID
      --security-opt no-new-privileges:true)

echo "== claude is installed, on PATH, and runnable by any user"
run --entrypoint claude "$tag" --version
run --user 4242:4242 --entrypoint claude "$tag" --version

echo "== sessions get a locale and shell; the updater is off"
# shellcheck disable=SC2016  # expanded inside the container, on purpose
run --entrypoint sh "$tag" -c 'test "$LANG" = C.UTF-8 && test "$SHELL" = /bin/bash \
  && test "$DISABLE_AUTOUPDATER" = 1 && test "$DISABLE_UPDATES" = 1' \
  || fail "LANG, SHELL or the update switches not set"

echo "== optional extras are off by default"
# shellcheck disable=SC2016
run --entrypoint sh "$tag" -c 'for t in gh tea glab bun; do ! command -v "$t" >/dev/null || exit 1; done' \
  || fail "an optional extra is installed in the default image"

echo "== shipped examples validate inside the image"
run --entrypoint relayer "$tag" validate --heteronyms /opt/relayer/examples/heteronyms.yaml \
  --templates /opt/relayer/examples/templates --runtime /opt/relayer/examples/relayer.yaml

echo "== supervise refuses to start with an API key in the environment"
set +e
run -e ANTHROPIC_API_KEY=sk-smoke "$tag"
code=$?
set -e
[ "$code" = 2 ] || fail "expected exit 2 from the API-key guard, got $code"

echo "== one Unix user per heteronym, with only CHOWN, SETUID and SETGID"
work=$(mktemp -d)
trap 'docker rm -f relayer-smoke >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
mkdir -p "$work/definitions/templates/assistant" "$work/config"
cp templates/assistant/template.yaml "$work/definitions/templates/assistant/"
cat > "$work/definitions/heteronyms.yaml" <<'YAML'
version: 1
heteronyms:
  smoke: {class: assistants, template: assistant}
YAML
# A stand-in for the agent: no sign-in, no network.
cat > "$work/config/relayer.yaml" <<'YAML'
version: 1
class: assistants
isolation: users
models: {fast: x, standard: x, strong: x, frontier: x}
session:
  command: ["sleep", "3600"]
YAML
docker run -d --name relayer-smoke "${caps[@]}" \
  -v "$work/definitions:/definitions:ro" -v "$work/config:/config:ro" \
  "$tag" relayer supervise --interval 1 >/dev/null
for _ in $(seq 1 30); do
  user=$(docker exec relayer-smoke ps -o user= -C sleep 2>/dev/null | head -1 || true)
  [ -n "$user" ] && break
  sleep 1
done
[ "$user" = "h-smoke" ] || { docker logs relayer-smoke >&2; fail "session runs as '$user', not h-smoke"; }
for d in /work/smoke /homes/smoke; do
  got=$(docker exec relayer-smoke stat -c '%U %a' "$d")
  [ "$got" = "h-smoke 700" ] || fail "$d is '$got', expected 'h-smoke 700'"
done
docker exec relayer-smoke sh -c 'grep -q "^CapEff:.*0000000000000000" /proc/$(pgrep -x sleep)/status' \
  || fail "the session process holds capabilities"
if docker exec relayer-smoke ls /work/smoke >/dev/null 2>&1; then
  fail "root in the container can read a heteronym's folder"
fi

echo "image smoke: all checks passed"
