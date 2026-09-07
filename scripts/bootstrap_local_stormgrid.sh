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
#   data/interim/eaglei/denominator/county_customers_*.parquet   customers per county
#   data/raw/census/cb_2024_us_county_500k.zip             county geometry
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
# stormgrid requires Python >= 3.10. macOS ships 3.9.6 as /usr/bin/python3, so
# a plain `python3 -m venv` builds an interpreter the package refuses to
# install into - and the failure arrives at `pip install`, well after the venv
# looks fine. Find a qualifying interpreter up front instead.
find_python() {
  local minimum_minor=10 candidate version
  for candidate in python3.13 python3.12 python3.11 python3.10 \
      /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
      /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
      /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
      /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
      python3; do
    command -v "${candidate}" >/dev/null 2>&1 || continue
    version="$("${candidate}" -c 'import sys;print(sys.version_info[1])' 2>/dev/null || echo 0)"
    if [ "${version}" -ge "${minimum_minor}" ] 2>/dev/null; then
      command -v "${candidate}"
      return 0
    fi
  done
  return 1
}

# A venv built by the wrong interpreter cannot be repaired by installing into
# it; it has to be replaced.
venv_python_ok() {
  local venv_python=$1
  [ -x "${venv_python}" ] || return 1
  local minor
  minor="$("${venv_python}" -c 'import sys;print(sys.version_info[1])' 2>/dev/null || echo 0)"
  [ "${minor}" -ge 10 ] 2>/dev/null
}

build_venv() {
  local target=$1 base_python=$2
  if [ -d "${target}" ] && ! venv_python_ok "${target}/bin/python"; then
    note "replacing ${target} (built by a Python older than 3.10)"
    rm -rf "${target}"
  fi
  [ -d "${target}" ] || "${base_python}" -m venv "${target}"
}

if [ "${do_env}" -eq 1 ]; then
  say "Locating a Python 3.10 or newer"
  base_python="$(find_python)" || die "no Python 3.10+ found on this machine.
  macOS ships 3.9.6, which stormgrid does not support. Install a newer one:
    brew install python@3.12
  then rerun this script." 3
  note "$("${base_python}" -c 'import sys;print(f"{sys.executable}  ({sys.version.split()[0]})")')"

  say "Building the StormGrid environment"
  build_venv "${SG_REPO_ROOT}/.venv" "${base_python}"
  sg_python="${SG_REPO_ROOT}/.venv/bin/python"
  "${sg_python}" -m pip install --quiet --upgrade pip

  # Extras, not a bare install. `grib` decodes the HRRR GRIBs, `download`
  # carries BigQuery, and bokeh_sampledata supplies the county polygons that
  # build_counties falls back to when no Census shapefile is present - which
  # is the normal case, since Prism has none either. A bare install imports
  # fine and then fails at first use, which is the worst time to find out.
  say "Installing stormgrid with the live extras"
  if ! "${sg_python}" -m pip install -e "${SG_REPO_ROOT}[grib,download]" \
        bokeh bokeh_sampledata; then
    die "installing stormgrid failed. If a geospatial or GRIB wheel had to be
  built from source, install the system libraries first:
    brew install gdal proj eccodes
  then rerun this script." 3
  fi

  # Import the module the pipelines actually run. `import stormgrid` only
  # touches a light __init__ and passes even when a dependency the CLI needs
  # is absent.
  "${sg_python}" -c 'import stormgrid.cli' \
    || die "${sg_python} installed stormgrid but cannot import stormgrid.cli.
  The traceback above names the missing dependency." 3
  note "$("${sg_python}" -c 'import sys;print(f"stormgrid.cli imports on {sys.version.split()[0]}")')"

  say "Building the Weather2Grid export environment"
  build_venv "${W2G_ROOT}/.venv" "${base_python}"
  w2g_python="${W2G_ROOT}/.venv/bin/python"
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
         "${SG_DATA_ROOT}/interim/eaglei/denominator" \
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

say "Copying the EAGLE-I customer denominator"
scp -q "${REMOTE}:${REMOTE_DATA}/interim/eaglei/denominator/county_customers_*.parquet" \
       "${SG_DATA_ROOT}/interim/eaglei/denominator/" 2>/dev/null \
  || scp -q "${REMOTE}:${REMOTE_DATA}/interim/eaglei/denominator.parquet" \
            "${SG_DATA_ROOT}/interim/eaglei/denominator.parquet" \
  || die "no customer denominator found on ${REMOTE} under
  ${REMOTE_DATA}/interim/eaglei/. Without it every county's outage count has no
  denominator and the adapters refuse to run." 4

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
