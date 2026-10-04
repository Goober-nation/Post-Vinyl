#!/bin/sh
# One harvest, start to finish, with no SSH session needed:
#   setsid sh run_suite.sh <plan.json> <out-dir> <response-limit> > suite.log 2>&1 < /dev/null &
# Stops the main slskd (the corpus instance reuses its Soulseek port), runs
# the harvest with the watchdog, then ALWAYS tears the corpus stack down and
# starts the main slskd again — on success, failure, or kill (TERM/INT).
set -u
cd "$(dirname "$0")"
PLAN=$1 OUT=$2 RESPONSE_LIMIT=${3:-300} PER_MINUTE=${PER_MINUTE:-4}
export PLAN OUT RESPONSE_LIMIT PER_MINUTE

restore() {
    echo "$(date -Is) teardown: corpus stack down, main slskd up"
    docker compose --profile run down
    docker start slskd
}
trap restore EXIT
trap 'exit 1' INT TERM

echo "$(date -Is) suite: plan=$PLAN out=$OUT cap=$RESPONSE_LIMIT pace=$PER_MINUTE/min"
mkdir -p "$OUT"
docker stop slskd || exit 1
docker compose up -d slskd-corpus || exit 1
for _ in $(seq 1 36); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' slskd-corpus)" = healthy ] && break
    sleep 5
done
docker compose --profile run up -d --force-recreate harvester || exit 1
sh watchdog.sh &
while [ "$(docker inspect -f '{{.State.Running}}' corpus-harvester 2>/dev/null)" = true ]; do
    sleep 30
done
wait
docker logs corpus-harvester 2>&1 | grep -E "queries to run|GAVE|^\[" | tail -n 3
echo "$(date -Is) harvest finished"
