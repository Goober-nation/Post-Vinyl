#!/bin/sh
# Creates Post-Vinyl's folders, writes the settings file from the example on
# first start, hands both to PUID:PGID, then runs the app as that user.
set -eu
config_dir=$(dirname "$PV_CONFIG_PATH")
data_dir=$(dirname "$PV_DB_PATH")
mkdir -p "$config_dir" "$data_dir"
if [ ! -e "$PV_CONFIG_PATH" ]; then
  cp /app/post-vinyl.example.yaml "$PV_CONFIG_PATH"
  echo "Created $PV_CONFIG_PATH from the example; edit it on the host and restart."
fi
if [ "$(id -u)" = 0 ]; then
  chown -R "$PUID:$PGID" "$config_dir" "$data_dir"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
