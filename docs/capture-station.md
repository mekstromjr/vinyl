# Capture station

The capture station is a Linux machine with a USB audio interface, a turntable, a barcode scanner
and a browser showing the kiosk page. It holds only the in-flight capture; everything durable
lives in the cluster. If the process dies mid-capture, the staged FLAC stays on disk for manual
salvage and the cluster row is demoted the next time that side is armed.

## Audio chain

- Interface: Focusrite Scarlett 2i2 (3rd Gen) on line inputs, phantom power off, Air off, gain
  knobs matched and physically marked. Clip Safe and Auto Gain are 4th Gen features and do not
  exist on this unit, so there is nothing to disable.
- The recorder opens the ALSA hardware device directly (default `hw:USB`) and needs exclusive
  access. On hosts running PipeWire, `setup.sh` installs
  [51-vinyl-scarlett-ignore.conf](../capture/deploy/51-vinyl-scarlett-ignore.conf) so WirePlumber
  leaves the card alone; otherwise the recorder gets `EBUSY`. Reboot once after the first install.
- Recording command shape (see `capture/src/vinyl_capture/recorder.py`): one `ffmpeg` ALSA input
  at 96 kHz, written to FLAC with `-bits_per_raw_sample 24` and, in parallel, to raw `s32le` on
  stdout for the live analyzer. No filters on either path.
- The systemd unit runs at `Nice=-10` with a raised I/O priority to bias the scheduler toward the
  recorder. Any xrun still fails the capture.

## Stopping a side

A capture stops on whichever comes first:

1. **Runout**: the locked-groove detector fires at or after the expected runtime minus 60 s.
2. **Locked groove**: it fires earlier than that, which is a skip; the capture fails.
3. **Timer**: expected runtime plus `VINYL_STOP_BUFFER_SEC` (default 360 s), or the hard cap
   when no runtime is known (`VINYL_HARD_CAP_33`, default 35 min; `VINYL_HARD_CAP_45`, 15 min).
4. **Operator** or an xrun.

If the timer fires with music still playing and an expected runtime was set, the runtime was
wrong; the capture fails integrity and the retry runs open-ended.

## Deploying a station

Run from a checkout on the maintainer's machine:

```bash
cp capture/deploy/deploy.local.env.example capture/deploy/deploy.local.env   # then fill it in
capture/deploy/deploy.sh                 # full deploy to the station in deploy.local.env
WRITE_ENV=0 capture/deploy/deploy.sh     # skip secret provisioning
```

`deploy.local.env` is gitignored and holds the site-specific values: the SSH target
(`VINYL_BRICK`), the Home Assistant plug entity and notify service, and an optional LAN address
for the S3 ingress.

`deploy.sh`:

- aborts if the station reports a capture or an unacknowledged review in progress (the idle check
  runs immediately before anything destructive);
- rsyncs the repo to `/opt/vinyl-capture`;
- writes `/etc/vinyl-capture/env` from the cluster's `vinyl-secrets` Kubernetes secret, so the
  station never talks to the secret store directly;
- stamps the deployed version, since the station has no `.git`;
- runs `setup.sh` on the station.

`setup.sh` is idempotent and probes the host instead of assuming it, so the same script serves a
desktop distribution with PipeWire and a lean Debian host with no sound server. It installs
`ffmpeg`, `flac`, `curl` and `alsa-utils`, creates the `vinyl` system user in the `audio` group,
installs the WirePlumber rule only where WirePlumber exists, moves automatic update timers out of
capture hours where they exist, syncs the virtualenv and installs `vinyl-capture.service`. The
kiosk is then served at `http://127.0.0.1:8081`.

## Configuration

The service reads `/etc/vinyl-capture/env` (see `capture/src/vinyl_capture/config.py`). Required:
`VINYL_API_KEY`, `VINYL_S3_ACCESS_KEY`, `VINYL_S3_SECRET_KEY`. Notable optional settings:
`VINYL_API_URL`, `VINYL_S3_ENDPOINT`, `VINYL_ALSA_DEVICE`, `VINYL_SAMPLE_RATE`, the stop timer
values above, `VINYL_HA_URL` / `VINYL_HA_TOKEN` (Home Assistant integration stays dormant without
them), and `VINYL_SERVE_KIOSK=0` to drop the local kiosk page while keeping the JSON control API.
