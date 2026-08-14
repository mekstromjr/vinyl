#!/usr/bin/env bash
# Idempotent capture-station setup. Runs ON the laptop as root (deploy.sh
# invokes it over ssh). The laptop is reproducible from config: wipe it,
# reinstall Mint, run deploy.sh, identical station.
set -euo pipefail

log() { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }

# --- packages ----------------------------------------------------------------
apt-get install -y -qq ffmpeg flac curl >/dev/null
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | \
  UV_INSTALL_DIR=/usr/local/bin sh >/dev/null

# --- user + dirs -------------------------------------------------------------
if ! id vinyl >/dev/null 2>&1; then
  log "creating system user vinyl (audio group)"
  useradd --system --home-dir /var/lib/vinyl-capture --create-home \
          --groups audio --shell /usr/sbin/nologin vinyl
else
  usermod -aG audio vinyl
fi
install -d -o vinyl -g vinyl /var/lib/vinyl-capture/staging
install -d -m 0750 /etc/vinyl-capture
chown root:vinyl /etc/vinyl-capture

# --- wireplumber: hands off the Scarlett -------------------------------------
install -D -m 0644 /opt/vinyl-capture/capture/deploy/51-vinyl-scarlett-ignore.conf \
  /etc/wireplumber/wireplumber.conf.d/51-vinyl-scarlett-ignore.conf

# --- retime update timers out of capture hours (approved 2026-08-13) ---------
retime() { # unit, calendar
  mkdir -p "/etc/systemd/system/$1.d"
  printf '[Timer]\nOnCalendar=\nOnCalendar=%s\nRandomizedDelaySec=15m\n' "$2" \
    > "/etc/systemd/system/$1.d/vinyl-retime.conf"
  log "retimed $1 -> $2"
}
retime apt-daily.timer "*-*-* 04:00"
retime apt-daily-upgrade.timer "*-*-* 04:30"
retime mintupdate-automation-upgrade.timer "*-*-* 04:45"
systemctl daemon-reload
for t in apt-daily.timer apt-daily-upgrade.timer mintupdate-automation-upgrade.timer; do
  systemctl try-restart "$t" 2>/dev/null || true
done

# --- app venv + service ------------------------------------------------------
cd /opt/vinyl-capture
sudo -u vinyl /usr/local/bin/uv sync --package vinyl-capture --no-dev >/dev/null
install -m 0644 capture/deploy/vinyl-capture.service /etc/systemd/system/vinyl-capture.service
systemctl daemon-reload
systemctl enable vinyl-capture.service >/dev/null

if [ ! -s /etc/vinyl-capture/env ]; then
  log "WARNING: /etc/vinyl-capture/env missing — deploy.sh writes it; service will fail until then"
else
  chgrp vinyl /etc/vinyl-capture/env; chmod 0640 /etc/vinyl-capture/env
  systemctl restart vinyl-capture.service
fi

log "done. Kiosk UI: http://127.0.0.1:8081 (open a tab in the kiosk browser)."
log "NOTE: if the Scarlett was grabbed by the desktop session, reboot once so"
log "      the wireplumber ignore rule takes effect before first capture."
