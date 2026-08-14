#!/usr/bin/env bash
# Deploy the capture station from this repo checkout to the ripper laptop.
# Run on the Mac. Mirrors ripper's deploy-brick.sh: rsync + remote setup.
#
#   capture/deploy/deploy.sh              # full deploy
#   WRITE_ENV=0 capture/deploy/deploy.sh  # skip secret provisioning
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

say "rsync repo -> $BRICK:/opt/vinyl-capture"
$SSH "$BRICK" 'sudo install -d -o mekadmin /opt/vinyl-capture'
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

say "running setup.sh on the laptop"
$SSH "$BRICK" 'sudo bash /opt/vinyl-capture/capture/deploy/setup.sh'

say "status:"
$SSH "$BRICK" 'systemctl is-active vinyl-capture.service; curl -fsS -m 3 http://127.0.0.1:8081/healthz || true'
