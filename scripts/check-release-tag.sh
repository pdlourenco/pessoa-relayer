#!/usr/bin/env bash
# check-release-tag.sh — a release tag must be "v" + the version in
# pyproject.toml, so the published image tag and the package version never
# disagree. The release workflow runs it before building anything.
#
#   scripts/check-release-tag.sh v0.1.0
set -euo pipefail
cd "$(dirname "$0")/.."
tag="${1:?usage: $0 <tag>}"
version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
if [ "$tag" != "v$version" ]; then
  echo "release tag $tag does not match pyproject.toml version $version (expected v$version)" >&2
  exit 1
fi
echo "release tag $tag matches pyproject.toml"
