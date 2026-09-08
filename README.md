# vinyl

> **Read-only mirror.** This project is developed on a self-hosted GitLab instance and
> mirrored to GitHub for visibility. Issues and pull requests are not accepted here;
> the mirror is overwritten on every push. Contact: see the profile of [@mekstromjr](https://github.com/mekstromjr).

Digitization pipeline for the inherited record collection: inventory, capture,
QC, and Jellyfin import. Tracking: [meklab/vinyl issues](https://gitlab.meklab.net/meklab/vinyl/-/issues),
milestone *meklab: Vinyl Digitization*.

```
Part A  INVENTORY   iPad -> vinyl.meklab.net (k3s): assign accession, photograph
                    label + runout, bind Discogs release, print 4x6 barcode card
Part B  CAPTURE     ripper laptop kiosk: scan card, record 96/24 FLAC per side,
                    integrity check, upload master to Garage; HA kills the
                    turntable plug and notifies
Part C  IMPORT      k3s: quality scorecard, track split (Discogs priors),
                    derivatives to the Jellyfin prefix (12h scan picks them up)
```

## Layout

uv workspace, sibling of `recipe-pipeline`:

| Package | Runs | Contents |
|---|---|---|
| `shared/` | everywhere | IDs (`GM-0142-D1-A`) + lifecycle state machines. Stdlib only. |
| `server/` | k3s | FastAPI control plane, SQLAlchemy models, Alembic migrations. |
| `worker/` | ripper laptop | (ticket #5) capture daemon + kiosk UI. Never a server dep. |

## Identifiers

```
GM-0142-D1-A.flac
│   │    │  └─ side (A/B)
│   │    └──── disc within the set (ALWAYS emitted, even for single-disc sets)
│   └───────── accession number, 4-digit zero-padded
└───────────── collection prefix
```

The accession number identifies a **set** (one jacket), is sequential and
deliberately meaningless, and is never processed "in order" — dedup and status
checks replace ordering everywhere. The same string is the Postgres primary
key, the Code 128 barcode on the printed card, and the FLAC filename stem.

## Invariants

- Masters are 96 kHz / 24-bit FLAC, one file per side, zero DSP, never modified
  in place. Everything else is derived.
- **Recording integrity** (laptop, gates the retry-or-flip prompt) vs
  **quality** (cluster, judges the disc): the test is *"would redoing the
  capture change this number?"*
- Upload trouble is a retry loop, never a capture failure — a network problem
  must not prompt a re-record.
- 78s are inventoried (`speed_rpm = 78`) but excluded from phase-one capture:
  different stylus, non-RIAA EQ.
- Scarlett 2i2 **3rd Gen** on the capture station: line inputs, phantom off,
  Air off, gain knobs matched and physically marked. Clip Safe / Auto Gain do
  not exist on this unit (4th-gen features) — nothing to disable.

## Development

```bash
uv sync --all-packages
uv run pytest
uv run ruff check .
cd server && uv run alembic upgrade head --sql   # render DDL, no DB needed
```

Database: `vinyl` on the shared `infra/postgres` (see `meklab-k8s/apps/postgres/`).
Connection URL comes from `VINYL_DB_URL`; migrations run with
`cd server && VINYL_DB_URL=... uv run alembic upgrade head`.
