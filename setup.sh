#!/usr/bin/env bash
# Post-Vinyl first-run setup.
#
# Handles the one thing that has to happen BEFORE `docker compose up`:
# generating slskd's API key. Docker Compose substitutes ${VARS} from .env
# once, at `docker compose up` parse time, before any container starts — so
# generating this key from inside a running container (an init container, or
# musica's own entrypoint) is always one run too late. This script exists
# only because of that; everything else (Navidrome account, Soulseek login,
# ListenBrainz, tutorial) happens in the web setup wizard after the stack is
# already up, since none of that has the same before-boot constraint.
#
# Does not start the stack itself — prints the command to run instead, so
# you can see its output/attach to it/run it detached, your call.
#
# Usage: ./setup.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — fill in your Navidrome/Soulseek/ListenBrainz values."
else
  echo ".env already exists — leaving it as-is."
fi

# MUSIC_HOST_DIR is never generated — it's your real music library path, and
# guessing one would risk `docker compose up` silently creating and mounting
# an empty directory (Docker auto-creates missing bind-mount paths). Stop
# here rather than let that happen quietly.
music_host_dir=$(grep -E '^MUSIC_HOST_DIR=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"'' || true)
if [ -z "${music_host_dir:-}" ] || [ "$music_host_dir" = "/path/to/host/music" ]; then
  echo
  echo "MUSIC_HOST_DIR in .env is not set to a real path yet."
  echo "Edit .env and point MUSIC_HOST_DIR at your actual music library directory"
  echo "on this host before running 'docker compose up' — otherwise Docker will"
  echo "silently create and mount an empty directory there."
  echo
  echo "Re-run ./setup.sh once that's set."
  exit 1
fi

# Generate SLSKD_API_KEY only if it's missing or blank. Idempotent: re-running
# this script never rotates an existing key.
current_key=$(grep -E '^SLSKD_API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"'' || true)
if [ -z "${current_key:-}" ]; then
  new_key=$(openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  if grep -qE '^SLSKD_API_KEY=' .env; then
    # BSD sed (macOS) and GNU sed (Linux) need different -i syntax.
    if sed --version >/dev/null 2>&1; then
      sed -i "s|^SLSKD_API_KEY=.*|SLSKD_API_KEY=${new_key}|" .env
    else
      sed -i '' "s|^SLSKD_API_KEY=.*|SLSKD_API_KEY=${new_key}|" .env
    fi
  else
    echo "SLSKD_API_KEY=${new_key}" >> .env
  fi
  echo "Generated a new SLSKD_API_KEY."
else
  echo "SLSKD_API_KEY already set — leaving it as-is."
fi

# SLSKD_DOWNLOADS_DIR tells slskd where to write completed downloads, inside
# the /music volume postvinyl also mounts — must match
# Config.paths.slskd_downloads_path (music_dir/download_dir/complete/soulseek).
# Derived from config/config.toml's [paths] download_dir if set, else the
# app's own default ("downloads"). Only auto-filled if missing — if you've
# customized download_dir, re-run setup.sh after updating config.toml, or
# just edit SLSKD_DOWNLOADS_DIR in .env directly and restart.
current_downloads_dir=$(grep -E '^SLSKD_DOWNLOADS_DIR=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"'' || true)
if [ -z "${current_downloads_dir:-}" ]; then
  download_dir=$(grep -E '^[[:space:]]*download_dir[[:space:]]*=' config/config.toml 2>/dev/null | head -1 | sed -E 's/.*=[[:space:]]*"([^"]*)".*/\1/' || true)
  download_dir="${download_dir:-downloads}"
  new_downloads_dir="/music/${download_dir}/complete/soulseek"
  if grep -qE '^SLSKD_DOWNLOADS_DIR=' .env; then
    if sed --version >/dev/null 2>&1; then
      sed -i "s|^SLSKD_DOWNLOADS_DIR=.*|SLSKD_DOWNLOADS_DIR=${new_downloads_dir}|" .env
    else
      sed -i '' "s|^SLSKD_DOWNLOADS_DIR=.*|SLSKD_DOWNLOADS_DIR=${new_downloads_dir}|" .env
    fi
  else
    echo "SLSKD_DOWNLOADS_DIR=${new_downloads_dir}" >> .env
  fi
  echo "Set SLSKD_DOWNLOADS_DIR=${new_downloads_dir}."
else
  echo "SLSKD_DOWNLOADS_DIR already set — leaving it as-is."
fi

# slskd auto-loads slskd.yml from its own app directory (/app inside the
# container, bind-mounted from ./slskd_config) — most of what's in this file
# duplicates docker-compose.yml's env vars for reference. The destination
# template key (transfers.download.destination.subdirectory) has no env var
# equivalent and is required for completed downloads to be grouped in a way
# postvinyl can find, so this copy is NOT purely cosmetic. The download
# directory itself is intentionally NOT set in this file — see the comment
# in slskd.yml.example — it comes from SLSKD_DOWNLOADS_DIR above instead, so
# no substitution is needed here; a plain copy is correct.
mkdir -p slskd_config
if [ ! -f slskd_config/slskd.yml ]; then
  cp slskd.yml.example slskd_config/slskd.yml
  echo "Copied slskd.yml.example -> slskd_config/slskd.yml (required — sets the destination-template layout postvinyl expects; most other keys mirror docker-compose.yml's env vars and can be tuned here or via slskd's web UI once remote_configuration is on)."
else
  echo "slskd_config/slskd.yml already exists — leaving it as-is."
fi

# postvinyl runs as a fixed UID (1000) inside the container, which usually
# doesn't match the host user creating these directories. Without this, the
# bind mounts end up owned by the host user with no write access for UID
# 1000, and postvinyl fails at startup with "unable to open database file".
mkdir -p app_data config
chmod -R o+rwX app_data config
echo "Ensured app_data/ and config/ are writable by the container (UID 1000)."

echo
echo "Development is ongoing — expect rough edges. Post-Vinyl shares your"
echo "library back to the Soulseek network by default (see the README FAQ)."
echo
echo "Setup done. Start the stack with:"
echo
echo "    docker compose up -d"
echo
echo "Then open http://localhost:8092 to finish setup (Navidrome account,"
echo "Soulseek login, ListenBrainz) in the setup wizard."
