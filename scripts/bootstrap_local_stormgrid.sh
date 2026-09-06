#!/usr/bin/env bash
# One-time setup so this machine can run StormGrid inference locally instead of
# on Prism.
#
# What actually has to come down is small. Training data does not: the model is
# already fitted, and a live run only applies it. What a live run cannot
# rebuild is the frozen artifact it applies, the pointer that pins which
# artifact is current, and the two static inputs the forecast adapters read.
# Everything else - the 200 GB of raw HRRR, ASOS, ERA5 and EAGLE-I under
# data/raw and data/interim - stays on Prism.
#
#   data/products/run_report.json                          which artifact is pinned
#   data/artifacts/<id>/                                   the frozen model
#   data/processed/calibration.json                        HRRR gust calibration
#   data/interim/eaglei/denominator/county_customers_*.parquet   customers per county
#   data/raw/census/cb_2024_us_county_500k.zip             county geometry
#   data/interim/{nlcd,eia861,elevation}/...               county covariates (optional)
#
# Re-running this is safe: existing files are refreshed, nothing is deleted.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
W2G_ROOT="$(cd "${here}/.." && pwd)"
SG_REPO_ROOT="${SG_REPO_ROOT:-$(cd "${W2G_ROOT}/../stormgrid" 2>/dev/null && pwd || true)}"
SG_DATA_ROOT="${SG_DATA_ROOT:-${SG_REPO_ROOT}/data}"
REMOTE=""
REMOTE_DATA="${SG_REMOTE_DATA_ROOT:-}"
do_env=1
do_copy=1

say()  { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
# $1 is the message, $2 the exit code. Using "$*" here would print the exit
# code as part of the message.
die()  { printf 'FATAL: %s\n' "$1" >&2; exit "${2:-1}"; }

usage() {
  cat <<'EOF'
Usage:
  ./scripts/bootstrap_local_stormgrid.sh --from USER@login.nccs.nasa.gov [options]

  --from HOST           ssh destination holding the StormGrid data root
  --remote-data DIR     data root on that host
                        (default: $SG_REMOTE_DATA_ROOT, else asks the host)
  --env-only            build the local environments, copy nothing
  --copy-only           copy the artifact and static inputs, build nothing
  -h, --help

Copying file by file needs an ssh round trip per file. Over a slow link,
pack one tarball on Prism instead and unpack it here:
  (on Prism)  ./scripts/pack_live_inputs.sh
  (here)      ./scripts/unpack_live_inputs.sh BUNDLE.tar.gz
              ./scripts/bootstrap_local_stormgrid.sh --env-only

After this finishes:
  ./scripts/run_hrrr_live.sh              (free, no credentials)
  ./scripts/run_weathernext3_live.sh --estimate
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --from) REMOTE="${2:?--from needs a host}"; shift 2 ;;
    --remote-data) REMOTE_DATA="${2:?--remote-data needs a directory}"; shift 2 ;;
    --env-only) do_copy=0; shift ;;
    --copy-only) do_env=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument $1" 2 ;;
  esac
done

if [ -z "${SG_REPO_ROOT}" ] || [ ! -d "${SG_REPO_ROOT}" ]; then
  die "cannot find the stormgrid checkout. Set SG_REPO_ROOT." 2
fi

# ---------------------------------------------------------- environments ---
if [ "${do_env}" -eq 1 ]; then
  say "Building the StormGrid environment"
  sg_python="${SG_REPO_ROOT}/.venv/bin/python"
  if [ ! -x "${sg_python}" ]; then
    python3 -m venv "${SG_REPO_ROOT}/.venv"
  fi
  "${sg_python}" -m pip install --quiet --upgrade pip
  # Editable, so a `git pull` in stormgrid takes effect without reinstalling.
  if ! "${sg_python}" -m pip install --quiet -e "${SG_REPO_ROOT}"; then
    die "installing stormgrid failed. If it needs system libraries (GDAL/PROJ
  for geopandas, eccodes for GRIB), install those first:
    brew install gdal proj eccodes" 3
  fi
  # The live paths need BigQuery and GRIB decoding; a training-only install
  # will import fine and then fail mid-forecast, which is the worst time.
  "${sg_python}" -m pip install --quiet \
    google-cloud-bigquery google-cloud-bigquery-storage db-dtypes \
    cfgrib xarray scipy || \
    note "some live extras failed to install; check the message above"
  note "$("${sg_python}" -c 'import stormgrid,sys;print(f"stormgrid ok on {sys.version.split()[0]}")')"

  say "Building the Weather2Grid export environment"
  w2g_python="${W2G_ROOT}/.venv/bin/python"
  if [ ! -x "${w2g_python}" ]; then
    python3 -m venv "${W2G_ROOT}/.venv"
  fi
  "${w2g_python}" -m pip install --quiet --upgrade pip
  "${w2g_python}" -m pip install --quiet -r "${W2G_ROOT}/requirements-export.txt" pytest
  note "export environment ready"
fi

[ "${do_copy}" -eq 1 ] || exit 0
[ -n "${REMOTE}" ] || die "--from is required to copy. Pass --env-only to skip copying." 2

# ------------------------------------------------------------- discovery ---
if [ -z "${REMOTE_DATA}" ]; then
  say "Locating the StormGrid data root on ${REMOTE}"
  REMOTE_DATA="$(ssh "${REMOTE}" 'bash -lc "
    for candidate in \$SG_DATA_ROOT \$HOME/nobackup/stormgrid/data \$HOME/stormgrid/data; do
      [ -f \"\$candidate/products/run_report.json\" ] && { echo \"\$candidate\"; exit 0; }
    done
    exit 1"' 2>/dev/null)" || die "could not find a data root on ${REMOTE}.
  Pass it explicitly:  --remote-data /path/to/stormgrid/data" 4
fi
note "${REMOTE}:${REMOTE_DATA}"

artifact_id="$(ssh "${REMOTE}" "python3 -c \"
import json
print(json.load(open('${REMOTE_DATA}/products/run_report.json'))['artifact']['model_artifact_id'])
\"" 2>/dev/null)" || die "could not read the pinned artifact id from ${REMOTE}" 4
note "pinned artifact: ${artifact_id}"

# ------------------------------------------------------------------ copy ---
mkdir -p "${SG_DATA_ROOT}/products" \
         "${SG_DATA_ROOT}/artifacts" \
         "${SG_DATA_ROOT}/processed" \
         "${SG_DATA_ROOT}/interim/eaglei/denominator" \
         "${SG_DATA_ROOT}/interim/nlcd" \
         "${SG_DATA_ROOT}/interim/eia861" \
         "${SG_DATA_ROOT}/interim/elevation" \
         "${SG_DATA_ROOT}/raw/census"

say "Copying the run report"
scp -q "${REMOTE}:${REMOTE_DATA}/products/run_report.json" \
       "${SG_DATA_ROOT}/products/run_report.json"

say "Copying the frozen artifact ${artifact_id}"
# -a preserves times so a re-run copies nothing it already has; the trailing
# slash keeps the artifact in its own id-named directory rather than flattening
# two artifacts together.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --info=progress2 \
    "${REMOTE}:${REMOTE_DATA}/artifacts/${artifact_id}/" \
    "${SG_DATA_ROOT}/artifacts/${artifact_id}/"
else
  scp -qr "${REMOTE}:${REMOTE_DATA}/artifacts/${artifact_id}" \
          "${SG_DATA_ROOT}/artifacts/"
fi

say "Copying the HRRR gust calibration"
# Mandatory for the HRRR path: prepare-hrrr-shadow reads it unconditionally and
# raises if it is absent. The WeatherNext path does not use it - its 100 m wind
# proxy is explicitly uncalibrated.
scp -q "${REMOTE}:${REMOTE_DATA}/processed/calibration.json" \
       "${SG_DATA_ROOT}/processed/calibration.json" \
  || die "no ${REMOTE_DATA}/processed/calibration.json on ${REMOTE}. The HRRR
  pipeline cannot run without it (fitted by \`stormgrid fit-calibration\`).
  The WeatherNext pipeline can: rerun with --copy-only after removing HRRR from
  your plans, or copy the file by hand." 4

say "Copying the EAGLE-I customer denominator"
scp -q "${REMOTE}:${REMOTE_DATA}/interim/eaglei/denominator/county_customers_*.parquet" \
       "${SG_DATA_ROOT}/interim/eaglei/denominator/" 2>/dev/null \
  || scp -q "${REMOTE}:${REMOTE_DATA}/interim/eaglei/denominator.parquet" \
            "${SG_DATA_ROOT}/interim/eaglei/denominator.parquet" \
  || die "no customer denominator found on ${REMOTE} under
  ${REMOTE_DATA}/interim/eaglei/. Without it every county's outage count has no
  denominator and the adapters refuse to run." 4

say "Copying county covariates (optional)"
# Each of these has a national-median fallback, so a miss degrades resolution
# rather than stopping the run - but the published risk then differs from what
# Prism produced for the same weather, which is worth knowing about.
for relative in "interim/nlcd/county_cover.parquet" \
                "interim/eia861/county_saidi.parquet" \
                "interim/elevation/county_elevation.parquet"; do
  if scp -q "${REMOTE}:${REMOTE_DATA}/${relative}" \
            "${SG_DATA_ROOT}/${relative}" 2>/dev/null; then
    note "${relative}"
  else
    note "${relative} not on ${REMOTE}; national defaults will be used"
  fi
done

say "Copying the site configuration (optional)"
# scripts/hpc/site.env is gitignored, so it does not arrive with a clone, but
# it is where WN3_GCP_PROJECT and WN3_BQ_DATASET are pinned.
scp -q "${REMOTE}:${REMOTE_DATA%/data}/scripts/hpc/site.env" \
       "${SG_REPO_ROOT}/scripts/hpc/site.env" 2>/dev/null \
  && note "scripts/hpc/site.env" \
  || note "no site.env on ${REMOTE}; set WN3_GCP_PROJECT and WN3_BQ_DATASET yourself"

say "Copying the Census county shapefile"
# Optional: build_counties falls back to a bundled outline. The fallback is
# coarser than the published map, so copy it when it exists rather than
# quietly shipping different geometry than Prism did.
scp -q "${REMOTE}:${REMOTE_DATA}/raw/census/cb_2024_us_county_500k.zip" \
       "${SG_DATA_ROOT}/raw/census/" 2>/dev/null \
  || note "not present on ${REMOTE}; build_counties will use its fallback outline"

# --------------------------------------------------------------- verify ---
say "Verifying"
sg_python="${SG_REPO_ROOT}/.venv/bin/python"
if [ -x "${sg_python}" ]; then
  "${sg_python}" - "${SG_DATA_ROOT}" <<'PY'
import json
import sys
from pathlib import Path

from stormgrid.phase6_train.artifact import load_artifact, load_release_assessment

root = Path(sys.argv[1])
report = json.loads((root / "products" / "run_report.json").read_text())
artifact_id = report["artifact"]["model_artifact_id"]
artifact_dir = root / "artifacts" / artifact_id
artifact = load_artifact(artifact_dir)
if artifact.model is None:
    raise SystemExit(f"FATAL: {artifact_dir} has no fitted model")
if artifact.training_envelope is None:
    raise SystemExit(f"FATAL: {artifact_dir} has no frozen training envelope")
load_release_assessment(artifact_dir)
print(f"   artifact:    {artifact_id} loads and is release-assessed")

for candidate in (root / "interim/eaglei/denominator/county_customers_2022.parquet",
                  root / "interim/eaglei/denominator.parquet"):
    if candidate.exists():
        import pandas as pd
        print(f"   denominator: {len(pd.read_parquet(candidate))} counties")
        break
else:
    raise SystemExit("FATAL: no customer denominator landed")

calibration = root / "processed" / "calibration.json"
if calibration.is_file():
    from stormgrid.real_data import _load_calibrations
    print(f"   calibration: {len(_load_calibrations(calibration))} HRRR version(s)")
else:
    print("   calibration: ABSENT - the HRRR pipeline will not run")
PY
else
  note "skipping (no StormGrid environment; rerun without --copy-only)"
fi

cat <<EOF

BOOTSTRAP COMPLETE
  data root: ${SG_DATA_ROOT}
  artifact:  ${artifact_id}

Next, in order:
  ${W2G_ROOT}/scripts/run_hrrr_live.sh
      Free and credential-free. Run this first - if it publishes, the local
      chain works end to end.

  gcloud auth application-default login
  ${W2G_ROOT}/scripts/run_weathernext3_live.sh --estimate
      Prices both BigQuery reads without billing anything. Read the estimate
      before running the real thing.
EOF
