#!/usr/bin/env bash
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
repo_dir=$(cd -- "$source_dir/../.." && pwd -P)
out_dir=${1:-${XDG_CACHE_HOME:-/tmp}/personal-codex-router/fast-mode-policy}
mkdir -p -- "$out_dir"
out_dir=$(cd -- "$out_dir" && pwd -P)
case "$out_dir/" in "$repo_dir/"*) echo 'Build output must be outside the repository.' >&2; exit 1;; esac
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo 'This build targets Linux amd64.' >&2; exit 1
fi
go_bin=$(python3 "$source_dir/toolchain.py")
version=0.2.0
export CGO_ENABLED=1 GOOS=linux GOARCH=amd64 GOTOOLCHAIN=local
stage=$(mktemp -d "$out_dir/.build-XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
cd -- "$source_dir"
runner=()
if command -v heavy >/dev/null 2>&1; then runner=(heavy); fi
"${runner[@]}" "$go_bin" build -mod=readonly -trimpath -buildvcs=false -buildmode=c-shared -o "$stage/fast-mode-policy-v$version.so" .
target="$out_dir/fast-mode-policy-v$version.so"
# Never overwrite a potentially loaded library, even in the build cache.
if [[ -e "$target" ]]; then
  cmp -s -- "$target" "$stage/fast-mode-policy-v$version.so" || { echo 'Versioned library exists with different bytes; use another output directory.' >&2; exit 1; }
else
  ln -- "$stage/fast-mode-policy-v$version.so" "$target"
fi
printf '%s\n' "$target"
