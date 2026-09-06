# Running the live pipelines locally

Both forecasts now run end to end on one machine. There is no Slurm submission,
no transfer bundle, and no scp: StormGrid writes its dashboard cycles to the
same disk the exporter reads, so the chain is a single command per model.

What did **not** change is everything that makes the output publishable — the
frozen model artifact, the shadow labelling, the publication gate, and the
initialization pairing between a storm track and the outage field drawn over
it. Local execution removes the transport, not the safeguards.

```
                    ┌──────────────────── on this machine ────────────────────┐
  BigQuery / AWS ──▶│  fetch → prepare → track → infer → export → gate → push │──▶ GitHub Pages
                    └────────────────────────────────────────────────────────┘
```

## One-time setup

Two routes; both move the same files, and training data comes down in neither.
See [PRISM_DOWNLOAD_LIST.md](PRISM_DOWNLOAD_LIST.md) for exactly what moves and
why.

**One tarball** — best over a slow link, or when you want a single reviewable,
checksummed transfer:

```bash
# on Prism
./scripts/pack_live_inputs.sh          # in the stormgrid checkout

# here
scp USER@login.nccs.nasa.gov:'…/stormgrid-live-inputs-*.tar.gz*' ~/Downloads/
./scripts/unpack_live_inputs.sh ~/Downloads/stormgrid-live-inputs-<id>.tar.gz
./scripts/bootstrap_local_stormgrid.sh --env-only     # virtualenvs
```

**Or file by file**, which also builds the environments:

```bash
./scripts/bootstrap_local_stormgrid.sh --from USER@login.nccs.nasa.gov
```

For WeatherNext only, also authenticate to Google:

```bash
gcloud auth application-default login
```

## Daily use

### NOAA HRRR — free, no credentials

```bash
./scripts/run_hrrr_live.sh                 # review the diff, publish nothing
./scripts/run_hrrr_live.sh --push          # publish
```

Takes the newest complete HRRR cycle four to eight hours back. HRRR is public
on AWS Open Data, so this costs nothing and is the right thing to run first:
if it publishes, the whole local chain works.

### WeatherNext 3 — billed by bytes scanned

```bash
./scripts/run_weathernext3_live.sh --estimate            # free dry run, prices both reads
./scripts/run_weathernext3_live.sh                       # run, stop at the gate
./scripts/run_weathernext3_live.sh --push                # publish
./scripts/run_weathernext3_live.sh --no-cyclone-track    # skip the storm-centre read
```

Two BigQuery reads happen per run: the county extract that drives the outage
forecast, and a coarser one over pressure and wind that locates the storm
centre. Both are dry-run estimated and refused if over budget. `--estimate`
prices them before you commit to anything; run it whenever you change
`WN3_TRACK_GRID_STEP` or `WN3_TRACK_BBOX`, since track cost scales with
1/step².

## Nothing publishes without `--push`

Both scripts stop after the publication gate and print the diff. Adding
`--push` commits and pushes — archive first, then the site, because the site
links to archived payloads the moment it deploys.

The push itself is the publish: it triggers `.github/workflows/pages.yml`,
which reruns the exporter suite and deploys. There is no undo other than
another commit.

## Where the storm track comes from

| Layer | Source | Paired to the forecast init? |
|---|---|---|
| WeatherNext track | `stormgrid fetch-weathernext3-track` — closed-low detection over WN3 pressure from the **same** BigQuery partition as the county extract | Yes, by construction |
| HRRR surface low | `prepare-hrrr-shadow` — the same detector over this cycle's own MSLMA and gust grids | Yes, by construction |
| NOAA NHC | `stormgrid fetch-nhc-tracks` — official advisories | **No, by design.** A separate forecaster-issued product with its own issue time, labelled as such |

Both model tracks are stamped with `forecast_init_time_utc`. The exporter
refuses to publish a track whose stamp differs from its cycle's init, the
dashboard refuses to draw one, and each pipeline runs `assert_track_pairing`
against the exported data before the gate. A mismatch fails the run rather than
producing a wrong map.

Neither detector can tell a warm-core tropical cyclone from an ordinary
extratropical low, so both label their output a surface low, never a hurricane
or an advisory.

> `scripts/fetch_weathernext_tracks.py` is a **demonstration** generator — it
> emits an invented Hurricane Marie track for exercising the storm overlay. It
> now refuses to run without `--allow-synthetic`, and marks what it writes as
> synthetic. It is not part of either pipeline.

## When something goes wrong

| Symptom | What it means |
|---|---|
| `this machine cannot run inference yet` | Missing artifact, denominator, or (HRRR only) calibration. Run the bootstrap. |
| `the pinned artifact is not live-ready` | The artifact copied down but fails its hash or release check. Recopy the whole `artifacts/<id>/` directory — the files are checksummed together. |
| `Dataset ... was not found` | Configuration, not credentials: the request authenticated and BigQuery answered. Check `WN3_BQ_DATASET`. |
| `no mean-sea-level-pressure field found` | The WN3 table names its fields differently than expected. The error lists what the table does expose; set `WN3_TRACK_MSLP_FIELD` / `WN3_TRACK_WIND_FIELDS`, or run `--no-cyclone-track`. |
| `TRACK PAIRING FAILED` | A track and its cycle disagree on initialization. Nothing was published. This should be unreachable; if you see it, the cause is worth finding rather than working around. |
| `has uncommitted changes outside site/data` | Site code and forecast data must not ride into the same commit — the data may depend on exporter changes that are not deployed yet. Commit and push the code first, on its own. |
| `no complete HRRR cycle four to eight hours back` | HRRR publishes over roughly an hour. Retry later or pass an explicit `--init`. |

## Scheduling it

HRRR is free and needs no credentials, so it is the safe one to schedule.
Prove a few runs by hand first, then:

```cron
17 */6 * * *  cd /path/to/weather2grid && ./scripts/run_hrrr_live.sh --push >> ~/logs/hrrr.log 2>&1
```

Do not schedule WeatherNext without a budget you have checked with
`--estimate`: it bills per run, and a scheduled job that silently doubles its
scan is an expensive way to find that out. Application default credentials also
expire, so an unattended WeatherNext job will eventually stop until you
re-authenticate.
