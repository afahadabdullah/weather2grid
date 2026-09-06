# What to copy from Prism to run StormGrid locally

No training happens on the Mac. A live run only *applies* an already-fitted
model, so almost nothing has to come down — the ~200 GB of raw HRRR, ASOS,
ERA5 and EAGLE-I under `data/raw/` and `data/interim/` on Prism is training
input and stays there.

Paths below are relative to the StormGrid data root on each machine
(`$SG_DATA_ROOT`, on Prism usually `~/nobackup/stormgrid/data`).

## The short way: one tarball

Rather than copying eight paths by hand, pack them on Prism and unpack them
here. Paths inside the tarball are relative to the data root, so extracting at
this machine's data root reproduces Prism's layout exactly:

```bash
# on Prism
cd /path/to/stormgrid
./scripts/pack_live_inputs.sh
#   -> data/products/transfers/stormgrid-live-inputs-<artifact id>.tar.gz  (+ .sha256)

# on this machine
scp USER@login.nccs.nasa.gov:'…/stormgrid-live-inputs-*.tar.gz*' ~/Downloads/
cd /path/to/weather2grid
./scripts/unpack_live_inputs.sh ~/Downloads/stormgrid-live-inputs-<id>.tar.gz
```

The packer refuses to build a bundle that is missing a required input, and
records every file's SHA-256 in a manifest inside the tarball. The unpacker
verifies the sidecar checksum, refuses absolute paths, `..`, symlinks and
device nodes, re-checks every extracted file against the manifest, asks before
overwriting, and finishes by loading the artifact — because files arriving
intact is not the same as a model that works.

`scripts/bootstrap_local_stormgrid.sh --from USER@login.nccs.nasa.gov` is the
alternative: it scp's the same files individually and builds the virtualenvs.
Use the tarball when the link is slow or you want one reviewable transfer; use
the bootstrap when you also need the environments built.

The rest of this document is the itemised list, so you can check the bundle or
copy pieces by hand.

---

## Required — nothing runs without these

| # | Path | Size | Why |
|---|------|------|-----|
| 1 | `products/run_report.json` | ~KB | Names which artifact is pinned. Every entry point reads this first to learn the `model_artifact_id`. |
| 2 | `artifacts/<model_artifact_id>/` | ~MB–100s MB | The frozen model. Copy the **whole directory** — see below. |
| 3 | `interim/eaglei/denominator/county_customers_2022.parquet` | ~200 KB | Customers per county. `_load_denominator` raises `FileNotFoundError` without it; there is no fallback, and without a denominator an outage *fraction* has no meaning. |

`<model_artifact_id>` is the value in `run_report.json` at
`["artifact"]["model_artifact_id"]` — currently **`bplus-d306fae20f`**, going by
what the published cycles cite. Read it from Prism rather than assuming:

```bash
ssh USER@login.nccs.nasa.gov \
  "python3 -c \"import json;print(json.load(open('\$SG_DATA_ROOT/products/run_report.json'))['artifact']['model_artifact_id'])\""
```

### Inside `artifacts/<id>/` — copy all four, they are hash-checked

`load_release_assessment` verifies each file's SHA-256 against `release.json`
and refuses the artifact if any hash disagrees. A partial copy fails loudly,
which is the behaviour you want, but it means there is no useful subset:

- `artifact.json` — metadata; its hash is pinned in `release.json`
- `model.pkl` — the fitted model bundle plus the frozen training envelope
- `release.json` — the machine-derived release decision
- `evaluation_report.json` — the pinned evaluation run (exact name comes from `release["source_run_report"]`)

---

## Required for the HRRR pipeline only

| # | Path | Size | Why |
|---|------|------|-----|
| 4 | `processed/calibration.json` | ~KB | Fitted HRRR-gust→ASOS calibration, per HRRR version. `prepare_hrrr_shadow_inputs` reads it unconditionally and raises if absent. |

The WeatherNext path does **not** use it — its 100 m wind proxy is explicitly
uncalibrated (`gust_calibration_status: unvalidated_100m_sustained_wind_proxy`).
So if you only want WeatherNext 3 running first, you can skip this one.

---

## Strongly recommended — it runs without them, but publishes different numbers

| # | Path | Size | Fallback if missing |
|---|------|------|---------------------|
| 5 | `raw/census/cb_2024_us_county_500k.zip` | ~10 MB | `build_counties` uses a coarser bundled outline. The map geometry then differs from what Prism published. |
| 6 | `interim/nlcd/county_cover.parquet` | ~200 KB | tree cover → national median 0.45 |
| 7 | `interim/eia861/county_saidi.parquet` | ~100 KB | baseline SAIDI → national median 180 min |
| 8 | `interim/elevation/county_elevation.parquet` | ~100 KB | elevation → national median 200 m |

Items 6–8 fall back to national medians, and the cycle is then stamped
`covariate_quality: national_defaults`. Nothing breaks — but the same weather
will produce different county risk than Prism did for the same run, which is
worth avoiding if you plan to compare the two.

---

## Configuration, not data

| # | Path | Why |
|---|------|-----|
| 9 | `<stormgrid repo>/scripts/hpc/site.env` | Gitignored, so it does not arrive with a clone. Holds `WN3_GCP_PROJECT`, `WN3_BQ_DATASET`, query budgets. Copy it or set those variables yourself. |

---

## What you do NOT need

- `raw/hrrr/` — the live pipeline downloads the cycles it needs from AWS Open
  Data itself, fresh each run.
- `raw/asos/`, `interim/asos_1min/`, `interim/hrrr_asos_pairs/` — calibration
  fitting inputs. You are copying the fitted result (item 4) instead.
- `interim/eaglei/outages/` — training target. Large, and never read by a
  forecast.
- `processed/county_hazard/` — training features.
- `products/dashboard/` — you already have these locally; they are the
  imported bundles from previous Prism runs.

---

## Rough total

Everything above is on the order of **tens of megabytes plus the artifact**.
The artifact is the only item whose size I could not check from here — run this
on Prism before you start:

```bash
du -sh $SG_DATA_ROOT/artifacts/<model_artifact_id> \
       $SG_DATA_ROOT/processed/calibration.json \
       $SG_DATA_ROOT/interim/eaglei/denominator \
       $SG_DATA_ROOT/raw/census
```

---

## Copy it by hand, if you would rather

```bash
REMOTE=USER@login.nccs.nasa.gov
RDATA=/path/to/stormgrid/data          # $SG_DATA_ROOT on Prism
LDATA=~/…/Projects/stormgrid/data      # $SG_DATA_ROOT here
ART=bplus-d306fae20f                   # read it from run_report.json first

mkdir -p "$LDATA"/{products,artifacts,processed} \
         "$LDATA"/interim/{eaglei/denominator,nlcd,eia861,elevation} \
         "$LDATA"/raw/census

scp    "$REMOTE:$RDATA/products/run_report.json"                          "$LDATA/products/"
rsync -a "$REMOTE:$RDATA/artifacts/$ART/"                                 "$LDATA/artifacts/$ART/"
scp    "$REMOTE:$RDATA/processed/calibration.json"                        "$LDATA/processed/"
scp    "$REMOTE:$RDATA/interim/eaglei/denominator/county_customers_*.parquet" "$LDATA/interim/eaglei/denominator/"
scp    "$REMOTE:$RDATA/raw/census/cb_2024_us_county_500k.zip"             "$LDATA/raw/census/"
scp    "$REMOTE:$RDATA/interim/nlcd/county_cover.parquet"                 "$LDATA/interim/nlcd/"
scp    "$REMOTE:$RDATA/interim/eia861/county_saidi.parquet"               "$LDATA/interim/eia861/"
scp    "$REMOTE:$RDATA/interim/elevation/county_elevation.parquet"        "$LDATA/interim/elevation/"
```

Then verify the artifact actually loads, rather than trusting that the files
arrived:

```bash
./scripts/bootstrap_local_stormgrid.sh --copy-only --from "$REMOTE"   # re-verifies
# or directly:
../stormgrid/.venv/bin/python -c "
from stormgrid.phase6_train.artifact import load_artifact, load_release_assessment
from pathlib import Path
d = Path('$LDATA/artifacts/$ART')
a = load_artifact(d); load_release_assessment(d)
print('artifact loads:', a.model is not None, 'envelope:', a.training_envelope is not None)"
```
