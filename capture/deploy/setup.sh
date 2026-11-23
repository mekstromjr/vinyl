#!/usr/bin/env bash
# Idempotent capture-station setup. Runs ON the station as root (deploy.sh
# invokes it over ssh). A station is reproducible from config: wipe it,
# reinstall, run deploy.sh, identical station.
#
# Runs on two different bases, so everything host-specific is probed, never
# assumed:
#   - a laptop       — Linux Mint 22.3, has PipeWire/WirePlumber + mintupdate
#   - a PVE host     — Debian/Proxmox VE, lean desktop, NO sound server at all
# The guards below are what let one script serve both. Do not replace them with
# unconditional calls: on Debian a missing mintupdate unit silently leaves a
# drop-in for a unit that will never exist, and an unconditional wireplumber
# rule creates a config tree no daemon reads.
set -euo pipefail

log() { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }

# --- packages ----------------------------------------------------------------
# --no-install-recommends matters on the lean Debian/PVE station: without it
# ffmpeg's recommends drag in a pile of desktop packages on a cluster node.
# alsa-utils is diagnostics only (arecord -l when a device goes missing).
apt-get install -y -qq --no-install-recommends ffmpeg flac curl alsa-utils >/dev/null
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
# Only meaningful where a sound server exists to be told. On a station with no
# PipeWire (<pve-host>), nothing can claim the card, so the recorder's exclusive
# ALSA open is already safe and the rule would be dead config.
if command -v wireplumber >/dev/null 2>&1; then
  install -D -m 0644 /opt/vinyl-capture/capture/deploy/51-vinyl-scarlett-ignore.conf \
    /etc/wireplumber/wireplumber.conf.d/51-vinyl-scarlett-ignore.conf
  log "wireplumber present — Scarlett ignore rule installed"
else
  log "no wireplumber — nothing contends for the card, skipping ignore rule"
fi

# --- retime update timers out of capture hours (approved 2026-08-13) ---------
# Only retime units this host actually has: mintupdate is Mint-only, and
# writing a drop-in for an absent unit leaves misleading config behind.
# Write every drop-in FIRST, then one daemon-reload, then restart — a
# try-restart before the reload would re-read the old timer.
RETIMED=()
retime() { # unit, calendar
  if ! systemctl list-unit-files "$1" >/dev/null 2>&1 ||
     [ -z "$(systemctl list-unit-files --no-legend "$1" 2>/dev/null)" ]; then
    log "no $1 on this host — skipping"
    return 0
  fi
  mkdir -p "/etc/systemd/system/$1.d"
  printf '[Timer]\nOnCalendar=\nOnCalendar=%s\nRandomizedDelaySec=15m\n' "$2" \
    > "/etc/systemd/system/$1.d/vinyl-retime.conf"
  RETIMED+=("$1")
  log "retimed $1 -> $2"
}
retime apt-daily.timer "*-*-* 04:00"
retime apt-daily-upgrade.timer "*-*-* 04:30"
retime mintupdate-automation-upgrade.timer "*-*-* 04:45"
systemctl daemon-reload
for t in ${RETIMED+"${RETIMED[@]}"}; do
  systemctl try-restart "$t" 2>/dev/null || true
done

# --- LAN-direct S3 ------------------------------------------------------------
# s3.meklab.net resolves to Traefik's TAILNET ingress IP by design; from this
# LAN-resident laptop that drags half-gigabyte masters through the tailscale
# overlay (observed: mid-transfer EOFs when the path degrades to DERP). Pin the
# hostname to the LAN VIP — same name, same cert, overlay out of the data path.
# VINYL_S3_LAN_IP comes from deploy.sh (deploy.local.env); unset skips the pin.
if [ -n "${VINYL_S3_LAN_IP:-}" ]; then
  grep -q "s3.meklab.net" /etc/hosts || echo "$VINYL_S3_LAN_IP s3.meklab.net  # vinyl-capture: LAN-direct upload path" >> /etc/hosts
fi

# --- app venv + service ------------------------------------------------------
cd /opt/vinyl-capture
# The checkout (and its .venv) belong to the deploy user so rsync stays
# simple; the vinyl service user only ever READS them at runtime.
OWNER=$(stat -c %U /opt/vinyl-capture)
sudo -u "$OWNER" /usr/local/bin/uv sync --package vinyl-capture --no-dev >/dev/null
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
