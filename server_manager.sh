#!/bin/bash
# server_manager.sh
# Monitors AzerothCore status and manages Worldserver uptime based on player activity.

# --- Configuration ---
# Time in minutes before stopping the server if empty
IDLE_TIMEOUT_MINUTES=60

# Check interval in seconds
CHECK_INTERVAL_SECONDS=5

# Docker Container Names (from docker-compose.yml)
DB_CONTAINER="ac-database"
WORLD_CONTAINER="ac-worldserver"

# Database Credentials
DB_USER="root"
DB_PASS="password" # Change this if you set a custom DOCKER_DB_ROOT_PASSWORD
AUTH_DB="acore_auth"
CHAR_DB="acore_characters"
# ---------------------

IDLE_COUNTER=0

# Ensure we are in the correct directory for docker compose
cd "$(dirname "$0")"

# Load .env if it exists
if [ -f .env ]; then
  set -o allexport
  source .env
  set +o allexport
fi

echo "Starting AzerothCore Server Manager..."
echo "Configuration: Timeout=${IDLE_TIMEOUT_MINUTES}m, Interval=${CHECK_INTERVAL_SECONDS}s"

while true; do
    # Update DB_PASS from env if available
    DB_PASS="${DOCKER_DB_ROOT_PASSWORD:-password}"

    # Check if DB is up first
    if ! docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
        echo "Database container '${DB_CONTAINER}' is not running. Waiting..."
        sleep 60
        continue
    fi

    # Check if Worldserver is running
    if docker ps --format '{{.Names}}' | grep -q "^${WORLD_CONTAINER}$"; then
        # Worldserver is running. Check for players.
        # We use docker exec to run the query inside the db container
        # We filter by checking if the account associated with the online character has a known OS (real players usually send OS info)
        # Ask the running server who is really playing (".dash who" prints one "P" line per real
        # player). The characters.online flag is not reliable: one alt bot logging out clears it
        # for every character on that account, so the player would look gone.
        WHO=$(./acmd.sh "dash who" 2>/dev/null)
        if echo "$WHO" | grep -q "^T"; then
            PLAYER_COUNT=$(echo "$WHO" | grep -c "^P")
        else
            PLAYER_COUNT=$(docker exec "${DB_CONTAINER}" mysql -u"${DB_USER}" -p"${DB_PASS}" -N -B -e "SELECT count(*) FROM ${CHAR_DB}.characters c JOIN ${AUTH_DB}.account a ON c.account = a.id WHERE c.online=1 AND a.os != ''" 2>/dev/null)
        fi

        # Handle case where both checks fail (e.g. during startup)
        if [ -z "$PLAYER_COUNT" ]; then
            echo "Failed to query database. Retrying..."
            sleep 10
            continue
        fi

        if [ "$PLAYER_COUNT" -eq 0 ]; then
            IDLE_COUNTER=$((IDLE_COUNTER + 1))
            CURRENT_IDLE_TIME=$((IDLE_COUNTER * CHECK_INTERVAL_SECONDS / 60))
            
            # Only print every few checks to reduce log spam
            if [ $((IDLE_COUNTER % 4)) -eq 0 ]; then
                 echo "[$(date)] Worldserver idle ($PLAYER_COUNT players). Idle time: $CURRENT_IDLE_TIME / $IDLE_TIMEOUT_MINUTES min."
            fi
            
            if [ "$CURRENT_IDLE_TIME" -ge "$IDLE_TIMEOUT_MINUTES" ]; then
                echo "[$(date)] Idle timeout reached. Stopping Worldserver..."
                docker compose stop "${WORLD_CONTAINER}"
                IDLE_COUNTER=0
            fi
        else
            if [ "$IDLE_COUNTER" -gt 0 ]; then
                 echo "[$(date)] Players detected ($PLAYER_COUNT). Resetting idle timer."
            fi
            IDLE_COUNTER=0
        fi
    else
        # Worldserver is stopped. Check for recent Auth Logins to trigger startup.
        # We look for logins in the last (CHECK_INTERVAL + buffer) seconds.
        LOGIN_CHECK_INTERVAL=$((CHECK_INTERVAL_SECONDS + 15))
        
        RECENT_LOGINS=$(docker exec "${DB_CONTAINER}" mysql -u"${DB_USER}" -p"${DB_PASS}" -N -B -e "SELECT count(*) FROM ${AUTH_DB}.account WHERE last_login > NOW() - INTERVAL ${LOGIN_CHECK_INTERVAL} SECOND" 2>/dev/null)

        if [ $? -eq 0 ] && [ "$RECENT_LOGINS" -gt 0 ]; then
            echo "[$(date)] Detected $RECENT_LOGINS recent login(s). Starting Worldserver..."
            docker compose up -d "${WORLD_CONTAINER}"
            IDLE_COUNTER=0 
            
            # Wait for it to initialize a bit
            echo "Waiting 60s for startup..."
            sleep 60
        fi
    fi
    
    sleep "$CHECK_INTERVAL_SECONDS"
done
