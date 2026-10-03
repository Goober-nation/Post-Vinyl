#!/bin/sh
# Restart slskd-corpus when it stays off the Soulseek network for >2 min.
# Live 2026-10-03: after a server-side drop slskd sat in "Disconnecting"
# for an hour and never reconnected on its own. Exits when the harvester
# container stops. Run from ~/projects/postvinyl-corpus:
#   nohup ./watchdog.sh >> data/corpus/watchdog.log 2>&1 &
KEY=$(grep '^SLSKD_API_KEY=' .env | cut -d= -f2-)
down_since=""
sleep 60  # let the harvester container start
while [ "$(docker inspect -f '{{.State.Running}}' corpus-harvester 2>/dev/null)" = "true" ]; do
    state=$(curl -s -m 10 -H "X-API-Key: $KEY" http://127.0.0.1:8093/api/v0/server)
    if echo "$state" | grep -q '"isLoggedIn":true'; then
        down_since=""
    else
        now=$(date +%s)
        [ -z "$down_since" ] && down_since=$now && echo "$(date -Is) down: $state"
        if [ $((now - down_since)) -ge 120 ]; then
            echo "$(date -Is) restarting slskd-corpus after $((now - down_since))s down"
            docker restart slskd-corpus >/dev/null
            down_since=""
            sleep 60
        fi
    fi
    sleep 30
done
echo "$(date -Is) harvester stopped; watchdog exiting"
