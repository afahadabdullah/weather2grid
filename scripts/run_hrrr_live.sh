#!/usr/bin/env bash
# Run one NOAA HRRR forecast end to end on THIS machine and publish it to the
# Weather2Grid dashboard.
#
# Local replacement for scripts/hpc/run_live_forecast.sh plus the
# scp-and-import handoff. Same stages, same frozen artifact, same shadow
# labelling; no Slurm, no transfer bundle.
#
#   artifact check
#     -> download HRRR gusts + MSLMA from AWS Open Data
#     -> build shadow inputs, detecting a surface low from the SAME cycle
#     -> run inference
#     -> live-status --strict
#     -> refresh NHC advisories
#     -> export to site/data
#     -> verify track/forecast pairing
#     -> publication gate
#     -> stop, unless --push
#
# HRRR needs no credentials - it is public on AWS Open Data - so unlike the
# WeatherNext path this one costs nothing to run and can be scheduled freely.
#
# Nothing is committed or pushed without --push.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=live_common.sh
source "${here}/live_common.sh"

init_argument="auto"
event_id=""
event_name="CONUS wind outlook"
lead_end="${SG_LIVE_LEAD_END:-18}"
do_push=0
want_track=1
force=0
message=""

usage() {
  cat <<'EOF'
Usage:
  ./scripts/run_hrrr_live.sh [options]

  --init INIT           HRRR initialization, or "auto" (default) to take the
                        newest complete cycle four to eight hours back
  --event-id NAME       default: hrrr-conus-YYYYMMDD
  --event-name TEXT     default: "CONUS wind outlook"
  --lead-end N          last forecast hour, 0-48   (default: 18)
  --no-storm-track      skip the MSLMA download and surface-low detection
  --force               re-run inference even if this initialization is already live
  --message TEXT        commit message override
  --push                commit and push after the gate passes
  -h, --help

Environment (all optional):
  SG_REPO_ROOT           stormgrid checkout   (default: ../stormgrid)
  SG_DATA_ROOT           StormGrid data root  (default: <stormgrid>/data)
  SG_LIVE_EVENT_TYPE     default: other_wind
  SG_LIVE_IMPACT_DRAWS   default: 256
  SG_LIVE_MAX_AGE_HOURS  default: 12
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --init) init_argument="${2:?--init needs a value}"; shift 2 ;;
    --event-id) event_id="${2:?--event-id needs a value}"; shift 2 ;;
    --event-name) event_name="${2:?--event-name needs a value}"; shift 2 ;;
    --lead-end) lead_end="${2:?--lead-end needs a value}"; shift 2 ;;
    --message) message="${2:?--message needs text}"; shift 2 ;;
    --no-storm-track) want_track=0; shift ;;
    --force) force=1; shift ;;
    --push) do_push=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument $1" 2 ;;
  esac
done

case "${lead_end}" in
  ''|*[!0-9]*) die "--lead-end must be an integer from 0 through 48" 2 ;;
esac
[ "${lead_end}" -le 48 ] || die "--lead-end must be an integer from 0 through 48" 2

resolve_pythons
preflight_inputs
say "Resolving the pinned model artifact"
artifact_dir="$(require_artifact)"
note "${artifact_dir}"

# ------------------------------------------------------------- download ---
# --with-storm-track also pulls MSLMA, which is what the surface-low detector
# reads. It is the same cycle's own pressure field, so the track it produces
# is paired to this forecast by construction rather than by a later lookup.
track_flag="--with-storm-track"
[ "${want_track}" -eq 1 ] || track_flag="--no-storm-track"

download_cycle() {
  sg download-live-hrrr --init "$1" --lead-start 0 --lead-end "${lead_end}" \
    "${track_flag}" --data-root "${SG_DATA_ROOT}"
}

candidate_init() {
  "${SG_PYTHON}" - "$1" <<'PY'
import sys
import pandas as pd
lag = int(sys.argv[1])
print((pd.Timestamp.now(tz="UTC").floor("h")
       - pd.Timedelta(hours=lag)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
}

forecast_init=""
if [ "${init_argument}" = auto ]; then
  say "Finding the newest complete HRRR cycle"
  # HRRR publishes a cycle over roughly an hour, so the newest initialization
  # is usually incomplete. Walking backwards is cheaper and more reliable than
  # guessing a single fixed lag.
  #
  # But a download can also fail because this machine is broken rather than
  # because the cycle is not ready, and retrying five times turns a missing
  # Python dependency into "no complete HRRR cycle" - a data diagnosis for a
  # setup problem. Keep the first attempt's output and show it if every lag
  # fails, so the real cause is visible.
  first_failure=""
  for lag in 4 5 6 7 8; do
    candidate="$(candidate_init "${lag}")"
    if [ "${force}" -eq 0 ] && [ -f "${W2G_ROOT}/site/data/cycles.json" ]; then
      is_live="$("${W2G_PYTHON}" - "${W2G_ROOT}/site/data/cycles.json" "${candidate}" <<'PY'
import json, sys
from pathlib import Path
try:
    cycles = json.loads(Path(sys.argv[1]).read_text())
    inits = {c.get("issued_utc") for c in cycles if "hrrr" in c.get("cycle_id", "") and c.get("is_latest_initialization")}
    print(1 if sys.argv[2] in inits else 0)
except Exception:
    print(0)
PY
      )"
      if [ "${is_live}" = "1" ]; then
        say "NOAA HRRR initialization ${candidate} is already published on the live dashboard."
        note "Forecast is up to date. Skipping redundant download, inference, and export (use --force to re-run)."
        exit 0
      fi
    fi
    note "trying ${candidate}"
    attempt_log="$(mktemp "${TMPDIR:-/tmp}/sg-hrrr-XXXXXX")"
    if download_cycle "${candidate}" >"${attempt_log}" 2>&1; then
      cat "${attempt_log}"
      rm -f "${attempt_log}"
      forecast_init="${candidate}"
      break
    fi
    [ -n "${first_failure}" ] || first_failure="${attempt_log}"
    [ "${first_failure}" = "${attempt_log}" ] || rm -f "${attempt_log}"
    note "${candidate} did not download; trying the prior cycle"
  done
  if [ -z "${forecast_init}" ]; then
    if [ -n "${first_failure}" ]; then
      printf '\nWhat the first attempt actually said:\n' >&2
      tail -15 "${first_failure}" | sed 's/^/  /' >&2
      rm -f "${first_failure}"
    fi
    die "no HRRR cycle four to eight hours back could be downloaded.
  If the output above is a Python error, this is a setup problem, not a
  forecast-availability one:
    ${W2G_ROOT}/scripts/bootstrap_local_stormgrid.sh --env-only
  Otherwise the cycles are genuinely not published yet - retry later, or pass
  an explicit --init." 5
  fi
  [ -n "${first_failure}" ] && rm -f "${first_failure}"
else
  forecast_init="$(normalise_init "${init_argument}")"
  if [ "${force}" -eq 0 ] && [ -f "${W2G_ROOT}/site/data/cycles.json" ]; then
    is_live="$("${W2G_PYTHON}" - "${W2G_ROOT}/site/data/cycles.json" "${forecast_init}" <<'PY'
import json, sys
from pathlib import Path
try:
    cycles = json.loads(Path(sys.argv[1]).read_text())
    inits = {c.get("issued_utc") for c in cycles if "hrrr" in c.get("cycle_id", "") and c.get("is_latest_initialization")}
    print(1 if sys.argv[2] in inits else 0)
except Exception:
    print(0)
PY
    )"
    if [ "${is_live}" = "1" ]; then
      say "NOAA HRRR initialization ${forecast_init} is already published on the live dashboard."
      note "Forecast is up to date. Skipping redundant download, inference, and export (use --force to re-run)."
      exit 0
    fi
  fi
  say "Downloading HRRR ${forecast_init}, forecast hours 0-${lead_end}"
  download_cycle "${forecast_init}" || die "the HRRR download failed for ${forecast_init}" 5
fi

cycle_stamp="$(cycle_stamp_for "${forecast_init}")"
[ -n "${event_id}" ] || event_id="hrrr-conus-${cycle_stamp:0:8}"
case "${event_id}" in
  ''|*[!A-Za-z0-9_.-]*)
    die "--event-id may contain only letters, digits, dot, underscore and dash" 2 ;;
esac

manifest="${SG_DATA_ROOT}/raw/hrrr/live-manifests/${cycle_stamp}.json"
[ -f "${manifest}" ] || die "the download wrote no manifest at ${manifest}" 5
cycle_id="${cycle_stamp}_${event_id}"
note "initialization ${forecast_init}"
note "cycle          ${cycle_id}"

# ---------------------------------------------------------------- inputs ---
# prepare-hrrr-shadow runs the surface-low detector over this cycle's own
# MSLMA and gust grids and writes track.json beside the members, then points
# the cycle spec at it. Most cycles have no organized low and publish without
# a track; that is the normal case, not a failure.
say "Building shadow inputs"
sg prepare-hrrr-shadow \
  --init "${forecast_init}" \
  --event-id "${event_id}" \
  --event-name "${event_name}" \
  --event-type "${SG_LIVE_EVENT_TYPE:-other_wind}" \
  --download-manifest "${manifest}" \
  --data-root "${SG_DATA_ROOT}"

cycle_dir="${SG_DATA_ROOT}/interim/live/${cycle_id}"
[ -f "${cycle_dir}/cycle-spec.json" ] \
  || die "the prepare step did not produce ${cycle_dir}/cycle-spec.json" 6

# The detector is experimental and its output is optional, but if it DID
# produce a track it must carry this cycle's initialization. Stamping here
# rather than trusting the writer means an older StormGrid that predates the
# pairing contract still publishes a correct, checkable track.
if [ -f "${cycle_dir}/track.json" ]; then
  "${SG_PYTHON}" - "${cycle_dir}/track.json" "${forecast_init}" <<'PY'
import json
import sys
from pathlib import Path

import pandas as pd

path, init = Path(sys.argv[1]), sys.argv[2]
init = pd.Timestamp(init)
init = init.tz_localize("UTC") if init.tzinfo is None else init.tz_convert("UTC")
payload = json.loads(path.read_text())
if payload.get("available") is not False and payload.get("points"):
    existing = payload.get("forecast_init_time_utc") or payload.get("init_time_utc")
    if existing and pd.Timestamp(existing) != init:
        raise SystemExit(
            f"FATAL: {path} carries init {existing}, not {init.isoformat()}. "
            "Refusing to relabel a track from another cycle.")
    payload["forecast_init_time_utc"] = init.isoformat()
    payload.setdefault("init_time_utc", init.isoformat())
    payload["pairing_key"] = "forecast_init_time_utc"
    payload.setdefault("pairing_basis",
                       "detected from this cycle's own HRRR MSLMA and gust grids")
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"   surface low: {len(payload['points'])} fixes, init {init.isoformat()}")
else:
    print("   no organized surface low in this cycle")
PY
else
  note "no track produced for this cycle"
fi

# ------------------------------------------------------------- inference ---
say "Running inference"
sg run-live-cycle \
  --artifact "${artifact_dir}" \
  --cycle-spec "${cycle_dir}/cycle-spec.json" \
  --members "${cycle_dir}/members.parquet" \
  --data-root "${SG_DATA_ROOT}" \
  --mode "${SG_LIVE_MODE:-shadow}" \
  --impact-draws "${SG_LIVE_IMPACT_DRAWS:-256}" \
  --max-age-hours "${SG_LIVE_MAX_AGE_HOURS:-12}"

say "Live status"
sg live-status --data-root "${SG_DATA_ROOT}" --strict

fetch_nhc_tracks

# --------------------------------------------------------------- publish ---
export_only
assert_track_pairing

[ -n "${message}" ] || message="Publish HRRR shadow cycle ${cycle_id}"
publish_cycles "${do_push}" "${message}"

print_footer "${do_push}" "HRRR RUN COMPLETE (SHADOW ONLY)" \
  "initialization: ${forecast_init}" \
  "cycle:          ${cycle_id}" \
  "artifact:       ${artifact_dir##*/}"
