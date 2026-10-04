#!/bin/bash
# Restart (or just stop) the worldserver without losing character progress.
#   scripts/safe-restart.sh [seconds-warning] [--stop]
# Warns players in game, saves everyone, then lets the server log all characters out
# and save before it exits, and starts it again (recreating it if the image changed).
set -euo pipefail
cd "$(dirname "$0")/.."
WARN=${1:-60}
MODE=${2:-restart}

# Real players according to the running server (the characters.online flag lags for alts).
online=$(./acmd.sh "dash who" 2>/dev/null | grep -c "^P" || true)

if docker ps --format '{{.Names}}' | grep -q '^ac-worldserver$'; then
    if [ "$online" -gt 0 ] && [ "$WARN" -gt 0 ]; then
        ./acmd.sh "announce Server restarting in $WARN seconds. Your progress is being saved." >/dev/null
        sleep "$WARN"
    fi
    ./acmd.sh "saveall" >/dev/null || true
    sleep 3
    echo "Stopping worldserver (saving all characters)..."
    start=$(date +%s)
    docker stop -t 300 ac-worldserver >/dev/null
    echo "Stopped cleanly in $(( $(date +%s) - start ))s (exit code $(docker inspect -f '{{.State.ExitCode}}' ac-worldserver))."
fi

if [ "$MODE" != "--stop" ]; then
    since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    docker compose up -d --no-build ac-worldserver
    for _ in $(seq 1 60); do
        # Capture first: "grep -q" closing the pipe early would fail the check under pipefail.
        logs=$(docker logs --since "$since" ac-worldserver 2>&1 || true)
        if grep -q "ready\.\.\." <<<"$logs"; then
            echo "Worldserver ready."
            exit 0
        fi
        sleep 5
    done
    echo "Worldserver did not report ready within 5 minutes." >&2
    exit 1
fi
