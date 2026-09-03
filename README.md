# Weather2Grid

**Probabilistic weather-to-power-grid risk intelligence.**

Weather2Grid is the public, static presentation layer for versioned grid-risk
products. It animates forecast cycles, maps county outage distributions,
separates weather and impact-model uncertainty, and lets users apply their own
cost/loss decision threshold.

The published site runs entirely in the browser. GitHub Pages serves HTML,
CSS, JavaScript, JSON, and GeoJSON; it does not run the StormGrid models or a
Python API.

## Repository relationship

The two repositories have deliberately separate responsibilities:

```text
stormgrid                                  weather2grid
weather/model inputs                      public presentation
        │                                         ▲
        ▼                                         │
pipeline → versioned product archive ── export_products.py
              risk.parquet                 JSON + GeoJSON snapshot
              cycle.json
              track.json
              counties.geojson
```

- **StormGrid** remains the modeling and product-generation repository.
- **Weather2Grid** reads only StormGrid's published product contract.
- Raw GFS, GEFS, WeatherNext, utility, training, and credential data do not
  belong in this repository.

This boundary means the dashboard can move from synthetic to real products
without changing its user interface.

## Run the public demo locally

No JavaScript build step is required:

```bash
python -m http.server 8081 --directory site
```

Open <http://127.0.0.1:8081/>. Do not open `site/index.html` directly because
browsers restrict JSON requests from `file://` pages.

## Forecast history and the initialization picker

The site keeps more than one forecast initialization. `export_products.py`
retains the newest `--keep-initializations` runs per `hazard_source` (default
4); the newest is flagged `is_latest_initialization` and shown by default, and
the rest are offered by the initialization picker in the masthead, beside the
theme toggle. `data/initializations.json` indexes them. The picker only appears
when the active hazard source actually has more than one run.

Selecting an archived run must never look like current guidance, so it turns the
picker amber, retitles the headline `ARCHIVED RUN — …`, sets the freshness pill
to `archived`, and labels the source line `archived <stamp>` instead of
`latest init`. Switching hazard source drops an initialization pin the new
source does not have, rather than silently falling back to its latest while the
control still says archived. The banner in `status.json` is computed from the
latest initialization only, so a retained old run cannot change what today's
product claims.

Retention is per initialization, never per cycle: evicting some windows of a run
would leave the frame slider animating across gaps.

### Where archived payloads live

Older runs' `cycles/<id>/` payloads are written to a second checkout
(`--archive-output`) that is published to Pages under the same account, so they
sit at `https://<user>.github.io/<repo>/` — the **same origin** as the
dashboard, since scheme, host and port all match and only the path differs. No
CORS configuration is needed anywhere. Archived summaries carry `data_base`, an
absolute URL prefix, and `cycleRoot()` in `app.js` resolves each cycle through
it; cycles without it load from `data/` exactly as before, so the split is
opt-in and the exporter works unchanged without it.

Only cycle payloads move. `cycles.json`, `initializations.json`, `status.json`
and the content-addressed `geometries/` stay with the site — the index is small,
and geometry is deduplicated across every run, so one copy beside the site beats
one per archive.

**Measured sizes.** One cycle's `counties.json` is 0.88 MB, 0.226 MB packed. An
extended 25-window run is 22 MB in the working tree, 5.6 MB of git objects per
publish.

By default the split does *not* reduce the site repository's per-publish growth:
the current run's data is new every time. What it does is keep *historical* data
out of this repository entirely and put it somewhere disposable — nothing links
to the archive's old commits, so its history can be collapsed to one commit at
any time. `--offload-current` moves the current run as well, leaving this
repository with only code, indexes and geometry and reducing its churn to a few
hundred kilobytes per publish; the cost is that the site then renders nothing if
the archive is unreachable. The exporter prints both trees' sizes on each run
and warns above 250 MB.

## Export products from the current StormGrid repository

The repositories can live beside one another:

```text
Projects/
├── stormgrid/
└── weather2grid/
```

Create a small export environment once:

```bash
cd weather2grid
python -m venv .venv
.venv/bin/pip install -r requirements-export.txt
```

Publish the synthetic archive currently committed with StormGrid:

```bash
.venv/bin/python scripts/export_products.py \
  --archive ../stormgrid/examples/dashboard-archive
```

Publish the latest generated dashboard products instead:

```bash
.venv/bin/python scripts/export_products.py \
  --archive ../stormgrid/data/products/dashboard
```

The exporter replaces `site/data/` with one complete, self-contained snapshot.
Review the change, preview it locally, and then commit it:

```bash
git add site/data
git commit -m "Publish forecast products"
git push
```

A push to `main` triggers the GitHub Pages workflow. The live site is expected
at <https://afahadabdullah.github.io/weather2grid/>.

## Product archive contract

The exporter expects this StormGrid output layout:

```text
<archive>/
├── basemap.geojson                 optional CONUS state context
├── counties.geojson                shared county geometry
└── <cycle_id>/
    ├── risk.parquet                county probability distributions
    ├── cycle.json                  issue time, provenance, release status
    ├── track.json                  optional cyclone track and wind radii
    └── counties.geojson            optional cycle-specific geometry
```

`nhc-active-tracks.json`, when present at the archive root, is a separate
official NHC advisory overlay. It contains active ocean-storm forecast centers
and asymmetric 34/50/64-kt wind swaths; it is not a county-outage forecast.
The `Refresh NHC ocean-storm overlay` GitHub Action refreshes this one file
every three hours and deploys the static site, so ocean tracks do not depend
on the much shorter HRRR CONUS horizon.

Weather2Grid writes compact browser-ready files under
`site/data/cycles/<cycle_id>/`. County attributes use the static
`w2g-columnar-v1` JSON layout (a column-name array plus row-value arrays), and
identical county GeoJSON is stored once under `site/data/geometries/` using a
content hash. Both are ordinary files supported by GitHub Pages; no API,
database, decompression library, or build-time JavaScript is required. The
adjustable threshold and cost/loss curve are calculated in the browser from
`prob_outage_fraction_gt_05`.

## Moving to real forecast data

1. Run the ingestion, hazard, impact, calibration, and product phases in
   StormGrid.
2. Confirm `cycle.json` truthfully records the provider, data cutoff,
   synthetic flag, degraded mode, and release-gate result.
3. Export only the resulting dashboard archive into Weather2Grid.
4. Preview and validate the snapshot.
5. Commit and push `site/data/`.

The page derives its safety banner from product metadata. Synthetic products
always display **SYNTHETIC DATA — NOT A FORECAST**. Ungated products driven by
current real weather display **REAL-TIME FORECAST — EXPERIMENTAL** together
with an explicit statement that the model has not passed its release gate and
must not be used as operational guidance. Do not edit the frontend to hide
either state.

Before publishing real products, verify that county-level customer data and
utility information are approved for public release. Never commit secrets,
private infrastructure locations, credentials, raw proprietary forecasts, or
restricted utility data.

## GitHub Pages setup

After the first push, open **Settings → Pages** in this repository and select
**GitHub Actions** as the source. The workflow in
`.github/workflows/pages.yml` validates and deploys the `site/` directory.

## Validation

```bash
.venv/bin/pip install pytest
.venv/bin/pytest -q
```

Weather2Grid is experimental research software. It is not an official
forecast, warning, evacuation recommendation, or emergency-management
directive.
