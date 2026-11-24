# vinyl

A pipeline for archiving a family record collection: an inventory web app that issues barcode
accession cards, a capture station that records each side to a 96 kHz / 24-bit FLAC master, and
a cluster QC job that judges every capture.

![Inventory home page: the next accession number, a "New record set" button, a record search box, and a task list filtered by Recapture, Review, Capture, Identify, Uploading and QC pending, with rows explaining why each side needs attention, such as a click rate or music ending earlier than expected](docs/screenshots/vinyl-inventory.webp)

**Status:** inventory, capture and QC are built and in use. Track splitting, derivative encodes and
import into Jellyfin are planned and not implemented yet. Latest release `v0.11.0`.

Developed on a self-hosted GitLab; this GitHub repo is a read-only mirror. Issues and pull
requests are not accepted here.

## How it works

```
Inventory  (web app, in the cluster)
  new record set -> accession id -> photograph label, jacket, runout
  -> bind a Discogs release (tracklist, expected side runtimes) -> print a 4x6 card
Capture    (station: a Linux box with a USB audio interface, kiosk UI)
  scan the card barcode -> arm a side -> record FLAC master + live analysis
  -> stop on runout, timer or operator -> integrity verdict: keep, or redo now
  -> upload to S3 in the background
Quality    (one-shot Kubernetes Job per uploaded capture)
  decode the master -> metrics -> pass / review / recapture -> task list in the web app
```

- **Inventory** (`server/`, FastAPI + SQLAlchemy + htmx). Each record set gets a sequential
  accession id. Discogs search and bind normalize the tracklist into per-side positions and
  expected runtimes; MusicBrainz is joined through its Discogs URL relationship when an entry
  exists. Photos go to S3. The printed card is a vector PDF with a Code 128 barcode of the
  accession id and only static facts, since printed status would go stale.
- **Capture** (`capture/`). One `ffmpeg` process reads the ALSA device and writes two outputs from
  the same stream: the FLAC master with no filters, and raw PCM teed to a live analyzer. The
  kiosk page is a thin client over a JSON control API on the station. When a side finishes, a
  Home Assistant call cuts the turntable's smart plug and sends a phone notification; both are
  best-effort and can never fail a capture.
- **QC** (`server/src/vinyl_server/qc/`). After upload the server spawns a Kubernetes Job with one
  POST to the API using its ServiceAccount token, with no CronJob and no Kubernetes client
  library. The job writes its verdict and measurements to the capture row, and non-pass results
  show up in the web app's task list with the reason.
- **Shared** (`shared/`, stdlib only): identifiers, lifecycle state machines and the locked-groove
  detector, imported by both the station and the cluster.

## QC approach

The design rests on one test, written at the top of
[shared/src/vinyl_shared/states.py](shared/src/vinyl_shared/states.py): *would redoing the
capture change this number?*

- **Yes: recording integrity, checked on the station in seconds.** ALSA xruns (counted from
  ffmpeg's stderr; lost samples make the file unrepairable), clipped samples, DC offset, a capture
  that is too short or short of the expected runtime, music still playing when the timer fired,
  and a mid-record locked groove. A failure means "redo it now, while the record is on the
  platter", and the capture never uploads.
- **No: quality, measured in the cluster afterwards.** Surface noise and click rate are faults of
  the disc; recapturing gives the same numbers. These drive pass / review, and a human decides.
- **Upload trouble is neither.** It is a retry loop, not a capture state, so a network problem
  never prompts a re-record. Staged files carry `<side_id>__<capture_id>` in the name, get a deep
  `flac -t` check off the hot path, and are deleted only after the server confirms the upload.

**Locked groove vs runout.** [loopdetect.py](shared/src/vinyl_shared/loopdetect.py) watches a
100 ms peak envelope for self-similarity at one platter revolution (1.8 s at 33 1/3 rpm), checking
lags of one revolution plus or minus one bucket because real platters drift off nominal speed.
It fires when half of the buckets across the last ten revolutions correlate above 0.97, and only
arms after music has been heard, so a long lead-in groove cannot trigger it. The thresholds are
field-tuned: recorded runout correlated at 0.982 to 0.996, while music has a median near 0.2 and
never sustains 0.95 across ten revolutions. The detector cannot tell a scratch from the end of a
side, so the station uses position: a loop more than 60 s before the Discogs expected runtime is
a locked groove (fail, clean the record, retry), and anything later is the runout and stops the
capture as a success. The timer stays as a safety net at expected runtime plus 360 s, or a hard
cap of 35 min (33 rpm) / 15 min (45 rpm) when no runtime is known.

**Cluster metrics** ([qc/metrics.py](server/src/vinyl_server/qc/metrics.py)) stream two ffmpeg
decodes through numpy accumulators in 1 s chunks, since a 96/24 side is about 1.2 GB as float32.
Pass 1 (full-band stereo) collects peaks, clipping, DC, per-channel RMS, L/R correlation, music
onset and end, lead-in noise floor, and dropouts (at least 0.5 s below -60 dBFS inside the music).
Pass 2 (3 kHz high-passed mono) estimates click rate from short-window crest factor. The QC job
re-runs the same locked-groove detector over the full file to catch loops the live pass missed.
Thresholds ([qc/verdict.py](server/src/vinyl_server/qc/verdict.py)) are recorded next to the
measurements in each report, so retuning is a diff against evidence; they have already been
loosened once after listening tests showed ordinary vintage surface noise was being flagged.

## Engineering decisions

- **One identifier everywhere.** `GM-0142-D1-A` is the Postgres key, the barcode payload (as
  `GM-0142`, one per set) and the FLAC filename stem. Accession numbers are deliberately
  meaningless and never processed in order; dedup and status checks replace ordering.
- **Masters are never touched.** 96 kHz / 24-bit FLAC, one file per side, zero DSP, never modified
  in place. `-bits_per_raw_sample 24` makes the FLAC a true 24-bit file rather than 32-bit with
  padding. Everything else will be derived from it.
- **Live analysis instead of a second read.** The integrity numbers accumulate while recording, in
  pure Python sized for an old dual-core laptop (about 770 KB/s of PCM in 100 ms chunks), so the
  retry-or-flip prompt is immediate when the side ends.
- **Two auth paths, kept apart.** Browser routes sit behind an SSO forward-auth ingress. `/api`
  routes require a static `X-API-Key` and ignore forward-auth headers, because that ingress has no
  forward-auth middleware and the headers would be caller-controlled.
- **78 rpm records are inventoried but not captured yet** (different stylus, non-RIAA EQ).
  Speed is a per-disc quantity, not a flag.
- **Reproducible station.** `capture/deploy/setup.sh` is idempotent and probes the host, so the
  same deploy works on a desktop distribution with PipeWire or a lean Debian box with no sound
  server. `deploy.sh` refuses to restart the station while a capture is in progress. Site-specific
  values (target host, Home Assistant entities) live in a gitignored `deploy.local.env`; see
  [capture/deploy/deploy.local.env.example](capture/deploy/deploy.local.env.example) and
  [docs/capture-station.md](docs/capture-station.md).

## Layout

uv workspace with three packages:

| Package | Runs | Contents |
|---|---|---|
| `shared/` | everywhere | IDs, lifecycle state machines, locked-groove detector. Stdlib only. |
| `server/` | cluster | FastAPI app and htmx UI, SQLAlchemy models, Alembic migrations, QC job. |
| `capture/` | capture station | Recorder, live analysis, uploader, kiosk UI, deploy scripts. Never a server dependency. |

## Run it locally

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and `ffmpeg` on the `PATH` (the QC tests
decode real audio).

```bash
git clone https://github.com/mekstromjr/vinyl.git
cd vinyl
uv sync --all-packages
uv run pytest -q          # database-backed tests skip without VINYL_TEST_DB_URL
uv run ruff check .
(cd server && uv run alembic upgrade head --sql)   # render the migration DDL, no database needed
```

To run the database-backed tests too, point them at a throwaway Postgres:

```bash
docker run -d --rm --name vinyl-test-pg -p 55432:5432 \
  -e POSTGRES_DB=vinyl_test -e POSTGRES_USER=vinyl -e POSTGRES_PASSWORD=test postgres:16.9
VINYL_TEST_DB_URL=postgresql+psycopg://vinyl:test@localhost:55432/vinyl_test uv run pytest -q
docker stop vinyl-test-pg
```

Running the server itself needs real services: it fails at startup unless `VINYL_DB_URL`,
`VINYL_API_KEY`, `VINYL_DISCOGS_TOKEN`, `VINYL_S3_ACCESS_KEY` and `VINYL_S3_SECRET_KEY` are set
(see [server/src/vinyl_server/config.py](server/src/vinyl_server/config.py)). Apply migrations
with `cd server && VINYL_DB_URL=... uv run alembic upgrade head`, then start
`uvicorn --factory vinyl_server.app:create_app`. In production the image runs migrations in an
init container.

## Tests and CI

183 tests across the three packages. Without a database, 118 run and 65 skip. The GitLab
pipeline ([.gitlab-ci.yml](.gitlab-ci.yml)) installs ffmpeg, runs `ruff` and the full suite against
a `postgres:16.9` service, renders the migrations offline, then builds the server image with
Kaniko on `main` and semver tags. Production manifests pin the release tag.

## License

[MIT](LICENSE)
