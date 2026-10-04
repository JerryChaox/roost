#!/usr/bin/env bash
# Builds what goes into the sandbox template and stages it in dist/sandbox
# (or $1): roost-driver for linux/amd64 and agent-pi's package files and
# build output. roost serve --sandbox-dir reads this directory; the template's
# name is a digest of it, so a changed driver or agent host is a new template.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$root/dist/sandbox}"

rm -rf "$out"
mkdir -p "$out/agent-pi"

echo "build-sandbox: roost-driver (linux/amd64)" >&2
(cd "$root" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -buildvcs=false -o "$out/roost-driver" ./cmd/roost-driver)

# agent-pi is built in a copy of its sources, so its own tree (node_modules,
# dist) is left as it is.
echo "build-sandbox: agent-pi" >&2
build="$(mktemp -d "${TMPDIR:-/tmp}/roost-agent-pi.XXXXXX")"
trap 'rm -rf "$build"' EXIT
rsync -a --exclude node_modules --exclude dist "$root/agent-pi/" "$build/"
(cd "$build" && npm ci --no-audit --no-fund >&2 && npm run build >&2)
cp "$build/package.json" "$build/package-lock.json" "$out/agent-pi/"
cp -R "$build/dist" "$out/agent-pi/dist"
test -f "$out/agent-pi/dist/main.js"

echo "build-sandbox: staged in $out" >&2
