#!/bin/bash
# Clone (or update) every module listed in modules.lock at its pinned commit and apply the
# local patches from patches/. Safe to re-run: patches already applied are skipped.
set -euo pipefail
cd "$(dirname "$0")/.."

grep -vE '^\s*(#|$)' modules.lock | while read -r name url commit; do
    dir="modules/$name"
    if [ ! -d "$dir/.git" ]; then
        echo "== cloning $name"
        git clone -q "$url" "$dir"
    fi
    git -C "$dir" fetch -q origin
    git -C "$dir" -c advice.detachedHead=false checkout -q "$commit"
    patch="patches/$name.patch"
    if [ -f "$patch" ]; then
        if git -C "$dir" apply --check "../../$patch" 2>/dev/null; then
            git -C "$dir" apply "../../$patch"
            echo "== $name: applied $patch"
        elif git -C "$dir" apply --reverse --check "../../$patch" 2>/dev/null; then
            echo "== $name: $patch already applied"
        else
            echo "!! $name: $patch does not apply cleanly; merge it by hand" >&2
        fi
    fi
done
echo "Modules ready. modules/mod-dashboard-tools is part of this repository."
