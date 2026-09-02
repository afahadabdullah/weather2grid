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
