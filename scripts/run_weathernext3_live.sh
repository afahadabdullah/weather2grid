#!/usr/bin/env bash
# Run one WeatherNext 3 extended-range forecast end to end on THIS machine and
# publish it to the Weather2Grid dashboard.
#
# This is the local replacement for scripts/hpc/run_live_weathernext3_extended.sh
# plus the scp-and-import handoff. Same stages, same frozen artifact, same
# shadow labelling; no Slurm, no transfer bundle.
#
#   artifact check
#     -> BigQuery fetch (billed, estimated first)
#     -> build rolling 24 h windows
#     -> detect the cyclone track from the SAME initialization
#     -> run inference per window
#     -> live-status --strict
#     -> refresh NHC advisories
#     -> export to site/data
#     -> verify track/forecast pairing
#     -> publication gate
#     -> stop, unless --push
#
# Nothing is committed or pushed without --push.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=live_common.sh
source "${here}/live_common.sh"

init_argument="latest"
event_prefix=""
event_name="CONUS wind outlook"
do_push=0
estimate_only=0
want_track=1
force=0
message=""

usage() {
  cat <<'EOF'
Usage:
  ./scripts/run_weathernext3_live.sh [options]

  --init INIT           WeatherNext 3 initialization (default: latest complete)
  --event-prefix NAME   default: wn3x-conus-YYYYMMDD
  --event-name TEXT     default: "CONUS wind outlook"
  --no-cyclone-track    skip the MSLP storm-centre read (saves query bytes;
                        cycles publish with no WeatherNext track at all)
  --estimate            free BigQuery dry run for both reads, then stop
  --force               re-run even if initialization is already published on dashboard
  --message TEXT        commit message override
  --push                commit and push after the gate passes
  -h, --help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --init) init_argument="${2:?--init needs a value}"; shift 2 ;;
    --event-prefix) event_prefix="${2:?--event-prefix needs a value}"; shift 2 ;;
    --event-name) event_name="${2:?--event-name needs a value}"; shift 2 ;;
    --message) message="${2:?--message needs text}"; shift 2 ;;
    --no-cyclone-track) want_track=0; shift ;;
    --estimate) estimate_only=1; shift ;;
    --force) force=1; shift ;;
    --push) do_push=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument $1" 2 ;;
  esac
done

resolve_pythons

: "${WN3_GCP_PROJECT:=${WN_GCP_PROJECT:-project-be5066ea-d26a-467b-893}}"
: "${WN3_BQ_DATASET:=weathernext_3}"
: "${WN3_BQ_TABLE:=weathernext_3_0_0_0p1deg}"
export WN3_GCP_PROJECT WN3_BQ_DATASET WN3_BQ_TABLE

county_fetch_args=(
  --project "${WN3_GCP_PROJECT}"
  --dataset "${WN3_BQ_DATASET}"
  --table "${WN3_BQ_TABLE}"
  --lead-start "${WNX3_LEAD_START:-6}"
  --lead-end "${WNX3_LEAD_END:-168}"
  --window-hours "${WNX3_WINDOW_HOURS:-24}"
  --step-hours "${WNX3_STEP_HOURS:-12}"
  --members "${WNX3_PRODUCT_MEMBERS:-1}"
  --max-query-bytes "${WN3_MAX_QUERY_BYTES:-1500000000000}"
  --expected-members "${WN3_EXPECTED_MEMBERS:-1}"
  --bbox "${WN3_BBOX:-}"
  --states "${WN3_STATES:-}"
  --data-root "${SG_DATA_ROOT}"
)

track_args=(
  --bbox "${WN3_TRACK_BBOX:--130,15,-55,55}"
  --grid-step "${WN3_TRACK_GRID_STEP:-0.5}"
  --max-query-bytes "${WN3_TRACK_MAX_QUERY_BYTES:-600000000000}"
  --lead-start "${WNX3_LEAD_START:-6}"
  --lead-end "${WNX3_LEAD_END:-168}"
  --data-root "${SG_DATA_ROOT}"
)

# ------------------------------------------------------------- estimate ---
# Both reads are billed by bytes scanned, so both get a free dry run. The
# track read is priced separately because it is the one you would turn off.
if [ "${estimate_only}" -eq 1 ]; then
  say "County read (dry run)"
  sg prepare-weathernext3-extended --dry-run --init "${init_argument}" \
    "${county_fetch_args[@]}"
  if [ "${want_track}" -eq 1 ]; then
    say "Cyclone-track read (dry run)"
    if [ "${init_argument}" = latest ]; then
      note "--estimate cannot price the track read against 'latest': the track"
      note "must be pinned to the initialization the county extract resolves to."
      note "Rerun with an explicit --init to price it."
    else
      sg fetch-weathernext3-track --dry-run --init "$(normalise_init "${init_argument}")" \
        "${track_args[@]}"
    fi
  fi
  exit 0
fi

preflight_inputs
say "Resolving the pinned model artifact"
artifact_dir="$(require_artifact)"
note "${artifact_dir}"

# ---------------------------------------------------------------- fetch ---
say "Fetching the WeatherNext 3 extract"
sg prepare-weathernext3-extended --fetch-only --init "${init_argument}" \
  "${county_fetch_args[@]}" \
  || die "the WeatherNext 3 fetch failed.
  A 'Dataset ... was not found' error is configuration, not credentials: the
  request authenticated and BigQuery answered. Check WN3_BQ_DATASET.
  Re-authenticate only if the error mentions credentials or permission:
    gcloud auth application-default login
    ${SG_PYTHON} -m stormgrid.cli describe-weathernext3" 5

pointer="${SG_DATA_ROOT}/interim/weathernext3/pending-init.json"
[ -f "${pointer}" ] || die "${pointer} was not written by the fetch" 5
forecast_init="$("${SG_PYTHON}" - "${pointer}" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["forecast_init_utc"])
PY
)"
cycle_stamp="$(cycle_stamp_for "${forecast_init}")"
[ -n "${event_prefix}" ] || event_prefix="wn3x-conus-${cycle_stamp:0:8}"
note "initialization ${forecast_init}"
note "event prefix   ${event_prefix}"

if [ "${force}" -eq 0 ] && [ -f "${W2G_ROOT}/site/data/cycles.json" ]; then
  is_live="$("${W2G_PYTHON}" - "${W2G_ROOT}/site/data/cycles.json" "${forecast_init}" <<'PY'
import json, sys
from pathlib import Path
import pandas as pd
try:
    cycles = json.loads(Path(sys.argv[1]).read_text())
    inits = {c.get("issued_utc") for c in cycles if "wn3" in c.get("cycle_id", "") and c.get("is_latest_initialization")}
    cand = pd.Timestamp(sys.argv[2])
    cand_iso = cand.tz_localize("UTC") if cand.tzinfo is None else cand.tz_convert("UTC")
    match = any(pd.Timestamp(i) == cand_iso for i in inits if i)
    print(1 if match else 0)
except Exception:
    print(0)
PY
  )"
  if [ "${is_live}" = "1" ]; then
    say "WeatherNext 3 initialization ${forecast_init} is already published on the live dashboard."
    note "Forecast is up to date. Skipping redundant window processing and inference (use --force to re-run)."
    exit 0
  fi
fi

# --------------------------------------------------------------- windows ---
# Every window below is built from this one extract, so they all share an
# initialization by construction. That is what makes a single track legitimate
# across all of them.
say "Building rolling windows"
sg prepare-weathernext3-extended --init "${forecast_init}" \
  --event-prefix "${event_prefix}" --event-name "${event_name}" \
  "${county_fetch_args[@]}"

index_path="${SG_DATA_ROOT}/interim/live/weathernext3-extended-latest.json"
[ -f "${index_path}" ] || die "the prepare step wrote no window index at ${index_path}" 6
# Read into an array without mapfile: macOS ships bash 3.2, which has neither
# mapfile nor readarray.
window_labels=()
while IFS= read -r label; do
  [ -n "${label}" ] && window_labels+=("${label}")
done < <("${SG_PYTHON}" - "${index_path}" <<'PY'
import json, sys
from pathlib import Path
for label in json.loads(Path(sys.argv[1]).read_text())["window_labels"]:
    print(label)
PY
)
[ "${#window_labels[@]}" -gt 0 ] || die "no windows in ${index_path}" 6
note "${#window_labels[@]} windows: ${window_labels[0]} .. ${window_labels[@]: -1}"

# ----------------------------------------------------------------- track ---
# Before inference, not after: fetch-weathernext3-track attaches itself to
# this initialization's cycle specs, and run-live-cycle then copies it into
# each published cycle and folds its checksum into that cycle's provenance.
# A track bolted on afterwards would be a file sitting beside the forecast
# rather than part of the same product.
if [ "${want_track}" -eq 1 ]; then
  say "Detecting the WeatherNext 3 surface low for ${forecast_init}"
  if ! sg fetch-weathernext3-track --init "${forecast_init}" "${track_args[@]}"; then
    note "the cyclone-track read failed; continuing without a track."
    note "The cycles will publish with no WeatherNext track, and the dashboard"
    note "will say so. Rerun later with --no-cyclone-track to skip it entirely."
  fi
else
  note "cyclone track skipped (--no-cyclone-track)"
fi

# ------------------------------------------------------------- inference ---
say "Running inference for ${#window_labels[@]} windows"
cycle_ids=()
for label in "${window_labels[@]}"; do
  cycle_id="${cycle_stamp}_${event_prefix}-${label}"
  cycle_dir="${SG_DATA_ROOT}/interim/live/${cycle_id}"
  [ -f "${cycle_dir}/cycle-spec.json" ] \
    || die "the prepare step did not produce ${cycle_dir}/cycle-spec.json" 6
  note "${cycle_id}"
  sg run-live-cycle \
    --artifact "${artifact_dir}" \
    --cycle-spec "${cycle_dir}/cycle-spec.json" \
    --members "${cycle_dir}/members.parquet" \
    --data-root "${SG_DATA_ROOT}" \
    --mode "${SG_LIVE_MODE:-shadow}" \
    --impact-draws "${SG_LIVE_IMPACT_DRAWS:-256}" \
    --max-age-hours "${SG_LIVE_MAX_AGE_HOURS:-84}"
  cycle_ids+=("${cycle_id}")
done

say "Live status"
sg live-status --data-root "${SG_DATA_ROOT}" --strict

fetch_nhc_tracks

# --------------------------------------------------------------- publish ---
export_only

if [ "${want_track}" -eq 1 ]; then
  say "Ensuring WeatherNext cyclone tracks are populated for ${forecast_init}"
  "${W2G_PYTHON}" "${here}/fetch_weathernext_tracks.py" \
    --version 3 \
    --init "${forecast_init}" \
    --output "${W2G_ROOT}/site/data/weathernext-active-tracks.json" \
    --populate-cycles \
    --allow-synthetic \
    || note "fetch_weathernext_tracks.py encountered an issue; continuing to pairing check."
fi

assert_track_pairing

[ -n "${message}" ] || message="Publish ${#cycle_ids[@]} WeatherNext 3 shadow cycles from ${forecast_init}"
publish_cycles "${do_push}" "${message}"

print_footer "${do_push}" "WEATHERNEXT 3 RUN COMPLETE (SHADOW ONLY)" \
  "initialization: ${forecast_init}" \
  "windows:        ${#cycle_ids[@]}" \
  "artifact:       ${artifact_dir##*/}"
