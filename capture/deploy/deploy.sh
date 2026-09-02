#!/usr/bin/env bash
# Deploy the capture station from this repo checkout to a station.
# Run on the Mac. Mirrors ripper's deploy-brick.sh: rsync + remote setup.
#
#   capture/deploy/deploy.sh              # full deploy to the default station
#   WRITE_ENV=0 capture/deploy/deploy.sh  # skip secret provisioning
#
# VINYL_BRICK selects the station. setup.sh probes the host, so the same deploy
# works against either base:
#   VINYL_BRICK=mekadmin@<tailnet-ip>  capture/deploy/deploy.sh  # laptop (Mint)
#   VINYL_BRICK=root@<station-host>       capture/deploy/deploy.sh  # <pve-host> (Debian/PVE)
#
# Secrets: pulled from the CLUSTER (k8s secret vinyl-secrets, same values the
# server uses) and written to /etc/vinyl-capture/env on the laptop. The laptop
# never talks to Vault.
set -euo pipefail

BRICK="${VINYL_BRICK:-mekadmin@<tailnet-ip>}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SSH="ssh -o ConnectTimeout=10"
WRITE_ENV="${WRITE_ENV:-1}"

say() { printf '\033[1;35m[deploy]\033[0m %s\n' "$*"; }

# HARD GUARD: never restart the station out from under a capture. A deploy
# raced an arm once (2026-08-15, killed GM-0010-D1-A mid-recording): the
# caller's idle check and the restart were ~90s apart. The check now lives
# HERE, immediately before anything destructive, and aborts loudly.
say "idle guard: checking the station"
PHASE=$($SSH "$BRICK" 'curl -s -m 3 http://127.0.0.1:8081/status 2>/dev/null' | python3 -c "import json,sys
try: print(json.load(sys.stdin).get(\"phase\",\"unknown\"))
except Exception: print(\"unreachable\")")
if [ "$PHASE" != "idle" ] && [ "$PHASE" != "unreachable" ]; then
  say "ABORT: station is $PHASE — a capture or unacked review is in progress"
  exit 1
fi

say "rsync repo -> $BRICK:/opt/vinyl-capture"
# Owner = whoever we connect as, so rsync (which writes as that user) and
# setup.sh's `sudo -u "$OWNER" uv sync` agree. Hardcoding a user breaks the
# moment a station is reached as root rather than mekadmin.
$SSH "$BRICK" 'sudo install -d -o "$(id -un)" /opt/vinyl-capture'
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude '.ruff_cache' \
  "$REPO_ROOT/" "$BRICK:/opt/vinyl-capture/"

if [ "$WRITE_ENV" = "1" ]; then
  say "provisioning /etc/vinyl-capture/env from cluster secrets"
  # setup.sh also creates this, but env provisioning must not depend on
  # setup having run first (fresh-wipe ordering).
  $SSH "$BRICK" 'sudo install -d -m 0750 /etc/vinyl-capture'
  API_KEY=$(kubectl -n prod get secret vinyl-secrets -o jsonpath='{.data.api-key}' | base64 -d)
  HA_TOKEN=$(kubectl -n prod get secret vinyl-secrets -o jsonpath='{.data.ha-token}' | base64 -d)
  if [ -z "$HA_TOKEN" ]; then
    say "WARNING: no ha-token in vinyl-secrets yet — HA integration will be dormant"
  fi
  S3_ACCESS=$(kubectl -n prod get secret vinyl-secrets -o jsonpath='{.data.s3-access-key}' | base64 -d)
  S3_SECRET=$(kubectl -n prod get secret vinyl-secrets -o jsonpath='{.data.s3-secret-key}' | base64 -d)
  # HA plug config lands with ticket #6; placeholders keep the flag off.
  $SSH "$BRICK" "sudo tee /etc/vinyl-capture/env >/dev/null" <<EOF
VINYL_API_URL=https://vinyl-api.meklab.net
VINYL_API_KEY=$API_KEY
VINYL_S3_ENDPOINT=https://s3.meklab.net
VINYL_S3_ACCESS_KEY=$S3_ACCESS
VINYL_S3_SECRET_KEY=$S3_SECRET
VINYL_HA_URL=https://home.meklab.net
VINYL_HA_TOKEN=${HA_TOKEN}
VINYL_HA_ENTITY=switch.<turntable-plug>
VINYL_HA_NOTIFY_SERVICE=mobile_app_<phone>
EOF
fi

# Stamp the deployed version. The station holds a plain rsync with no .git, so
# without this there is no way to ask a running station which code it has — and
# a deploy does not necessarily match any tag.
STAMP=$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)
say "stamping deployed version: $STAMP"
$SSH "$BRICK" "printf '%s' '$STAMP' | sudo tee /opt/vinyl-capture/.deployed-version >/dev/null"

say "running setup.sh on the station"
$SSH "$BRICK" 'sudo bash /opt/vinyl-capture/capture/deploy/setup.sh'

say "status:"
$SSH "$BRICK" 'systemctl is-active vinyl-capture.service; curl -fsS -m 3 http://127.0.0.1:8081/healthz || true'
