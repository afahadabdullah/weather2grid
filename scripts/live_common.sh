#!/usr/bin/env bash
# Shared plumbing for the local live pipelines (run_weathernext3_live.sh and
# run_hrrr_live.sh). Not executable on its own; source it.
#
# These pipelines replace the Prism half of the chain. On the HPC the work was
# split across sbatch stages that had to be submitted, waited on and then
# packaged into a tarball for transfer. On one machine none of that applies:
# the stages are plain sequential commands, and the dashboard archive the
# exporter reads is already on the same disk, so there is no bundle, no
# checksum sidecar and no scp. What does NOT change is everything that makes
# the output publishable - the frozen artifact, the shadow labelling, the
# publication gate, and the initialization pairing between a storm track and
# the outage field it is drawn over.

set -euo pipefail

# --------------------------------------------------------------- roots ---
W2G_ROOT="${SG_WEATHER2GRID_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SG_REPO_ROOT="${SG_REPO_ROOT:-$(cd "${W2G_ROOT}/../stormgrid" 2>/dev/null && pwd || true)}"
SG_DATA_ROOT="${SG_DATA_ROOT:-${SG_REPO_ROOT}/data}"
W2G_ARCHIVE_ROOT="${SG_WEATHER2GRID_ARCHIVE_REPO:-${W2G_ROOT}-archive}"

# Where StormGrid writes finished cycles, and what the exporter reads.
DASHBOARD_DIR="${SG_DATA_ROOT}/products/dashboard"

say()  { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
# $1 is the message, $2 the exit code. Using "$*" here would print the exit
# code as part of the message.
die()  { printf 'FATAL: %s\n' "$1" >&2; exit "${2:-1}"; }

# ------------------------------------------------------------ pythons ---
# Two interpreters on purpose. The StormGrid environment carries the modelling
# stack (BigQuery, geopandas, scipy); the Weather2Grid one carries only what
# the exporter needs. Keeping them apart is why the public exporter cannot
# accidentally depend on a modelling library that is absent in CI.
resolve_pythons() {
  if [ -z "${SG_REPO_ROOT}" ] || [ ! -d "${SG_REPO_ROOT}" ]; then
    die "cannot find the stormgrid checkout. Set SG_REPO_ROOT." 2
  fi

  SG_PYTHON="${SG_PYTHON:-${SG_REPO_ROOT}/.venv/bin/python}"
  W2G_PYTHON="${W2G_PYTHON:-${W2G_ROOT}/.venv/bin/python}"

  if [ ! -x "${SG_PYTHON}" ]; then
    die "the StormGrid environment is missing: ${SG_PYTHON}
  Create it once (or run scripts/bootstrap_local_stormgrid.sh):
    python3 -m venv ${SG_REPO_ROOT}/.venv
    ${SG_REPO_ROOT}/.venv/bin/pip install -e '${SG_REPO_ROOT}[live]'" 2
  fi
  if [ ! -x "${W2G_PYTHON}" ]; then
    die "the Weather2Grid export environment is missing: ${W2G_PYTHON}
  Create it once:
    python3 -m venv ${W2G_ROOT}/.venv
    ${W2G_PYTHON} -m pip install -r ${W2G_ROOT}/requirements-export.txt pytest" 2
  fi
  # Check the module the pipelines actually run, not just the package. A bare
  # `import stormgrid` only touches a light __init__, so a half-installed
  # environment passes it and then dies inside the first real command - where
  # the failure gets misread as a data problem rather than a setup one.
  if ! "${SG_PYTHON}" -c 'import stormgrid.cli' 2>/dev/null; then
    printf 'FATAL: %s cannot import stormgrid.cli:\n' "${SG_PYTHON}" >&2
    # This command is expected to fail - that is the whole point of running it
    # again. Without `|| true` its non-zero status trips `set -e` (pipefail
    # propagates it through the pipeline) and kills the script mid-message,
    # before the advice below is ever printed.
    { "${SG_PYTHON}" -c 'import stormgrid.cli' 2>&1 || true; } \
      | tail -3 | sed 's/^/  /' >&2
    cat >&2 <<EOF

  Rebuild the environment with the extras the live paths need:
    ${W2G_ROOT}/scripts/bootstrap_local_stormgrid.sh --env-only
EOF
    exit 2
  fi

  local minor
  minor="$("${SG_PYTHON}" -c 'import sys;print(sys.version_info[1])' 2>/dev/null || echo 0)"
  if [ "${minor}" -lt 10 ] 2>/dev/null; then
    die "${SG_PYTHON} is Python 3.${minor}; stormgrid needs 3.10 or newer.
  macOS ships 3.9.6 as /usr/bin/python3. Rebuild against a newer one:
    brew install python@3.12
    ${W2G_ROOT}/scripts/bootstrap_local_stormgrid.sh --env-only" 2
  fi
}

sg() { "${SG_PYTHON}" -m stormgrid.cli "$@"; }

# ----------------------------------------------------------- preflight ---
# Everything the model needs that a fresh machine will not have. Checked up
# front and reported together: discovering the missing customer denominator
# after a billed BigQuery read is a bad way to learn about it.
preflight_inputs() {
  local problems=()
  # Note: bash 3.2 (what macOS ships) treats "${problems[@]}" on an empty
  # array as unbound under set -u, so every expansion below is guarded by a
  # length check first.

  [ -d "${SG_DATA_ROOT}" ] || mkdir -p "${SG_DATA_ROOT}"

  if ! "${SG_PYTHON}" - "${SG_DATA_ROOT}" <<'PY' >/dev/null 2>&1
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
json.loads((root / "products" / "run_report.json").read_text())["artifact"]["model_artifact_id"]
PY
  then
    problems+=("no pinned model artifact: ${SG_DATA_ROOT}/products/run_report.json is missing or has no artifact id")
  fi

  local denominator_found=0
  for candidate in \
      "${SG_DATA_ROOT}/interim/eaglei/denominator/county_customers_2022.parquet" \
      "${SG_DATA_ROOT}/interim/eaglei/denominator.parquet"; do
    [ -f "${candidate}" ] && denominator_found=1 && break
  done
  [ "${denominator_found}" -eq 1 ] \
    || problems+=("no EAGLE-I customer denominator under ${SG_DATA_ROOT}/interim/eaglei/denominator/")

  if [ "${#problems[@]}" -gt 0 ]; then
    printf 'FATAL: this machine cannot run inference yet.\n' >&2
    printf '  - %s\n' "${problems[@]}" >&2
    cat >&2 <<EOF

  These are one-time copies from Prism, not something a live run can rebuild.
  Fetch them with:
    ${W2G_ROOT}/scripts/bootstrap_local_stormgrid.sh --from USER@login.nccs.nasa.gov
EOF
    exit 3
  fi
}

# The artifact is the frozen model that turns wind into outage risk. A live
# run must use one that was released, not merely one that exists, so this
# reuses StormGrid's own loader rather than trusting the directory name.
verified_artifact() {
  "${SG_PYTHON}" - "${SG_DATA_ROOT}" <<'PY'
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
    raise RuntimeError("artifact has no fitted model")
if artifact.training_envelope is None:
    raise RuntimeError("artifact has no frozen training envelope")
load_release_assessment(artifact_dir)
print(artifact_dir)
PY
}

require_artifact() {
  local artifact_dir
  if ! artifact_dir="$(verified_artifact 2>&1)"; then
    die "the pinned artifact is not live-ready:
${artifact_dir}
  Training is not something to rerun on a laptop mid-forecast. Copy a released
  artifact down from Prism instead:
    ${W2G_ROOT}/scripts/bootstrap_local_stormgrid.sh --from USER@login.nccs.nasa.gov" 4
  fi
  printf '%s\n' "${artifact_dir}"
}

# ------------------------------------------------------------- timestamps ---
normalise_init() {
  "${SG_PYTHON}" - "$1" <<'PY'
import sys
import pandas as pd

value = pd.Timestamp(sys.argv[1])
value = value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")
print(value.strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
}

cycle_stamp_for() {
  "${SG_PYTHON}" - "$1" <<'PY'
import sys
import pandas as pd
print(pd.Timestamp(sys.argv[1]).strftime("%Y%m%dT%H%MZ"))
PY
}

# ---------------------------------------------------------------- tracks ---
# The NHC layer is an official advisory product with its own issue time. It is
# deliberately NOT paired to a model initialization - it is not another view of
# this run - so it is refreshed independently and labelled with its own
# timestamp wherever the dashboard shows it.
fetch_nhc_tracks() {
  say "Refreshing NOAA NHC active advisories"
  mkdir -p "${DASHBOARD_DIR}"
  if sg fetch-nhc-tracks --output "${DASHBOARD_DIR}"; then
    return 0
  fi
  # An advisory fetch failure must not sink a forecast run. The exporter keeps
  # the previous file, and the dashboard keeps showing its real issue time.
  note "NHC refresh failed; keeping the previously published advisories."
  return 0
}

# Assert the contract the dashboard depends on: every published cycle either
# carries a track from its own initialization, or carries no track at all.
# Running this before the publication gate turns a silent mispairing into a
# failed run rather than a wrong map.
assert_track_pairing() {
  say "Verifying track/forecast initialization pairing"
  "${W2G_PYTHON}" - "${W2G_ROOT}/site/data" <<'PY'
import json
import sys
from pathlib import Path

site = Path(sys.argv[1])
cycles_dir = site / "cycles"
failures = []
paired = withheld = 0

for directory in sorted(p for p in cycles_dir.glob("*") if p.is_dir()):
    cycle = json.loads((directory / "cycle.json").read_text())
    issued = cycle.get("issued_utc")
    track_path = directory / "track.json"
    track = json.loads(track_path.read_text()) if track_path.is_file() else {"available": False}
    if track.get("available") is False or not track.get("points"):
        withheld += 1
        continue
    init = track.get("forecast_init_time_utc") or track.get("init_time_utc")
    if not init:
        failures.append(f"{directory.name}: track has no forecast_init_time_utc")
        continue
    if str(init) != str(issued):
        failures.append(
            f"{directory.name}: track init {init} != cycle init {issued}")
        continue
    paired += 1

print(f"   paired cycles:   {paired}")
print(f"   without a track: {withheld}")
if failures:
    print("\nTRACK PAIRING FAILED:")
    for failure in failures:
        print(f"  - {failure}")
    raise SystemExit(1)
print("   pairing:         PASS")
PY
}

# The exporter rebuilds site/data from the dashboard archive and prunes what
# it does not see. So a dashboard archive that is missing an initialization the
# live site is currently serving does not merely fail to add it - it removes
# it, replacing the published run with an older one. That is a silent public
# regression, and the usual cause is mundane: a bundle that was imported into
# site/data on some earlier occasion but never into this machine's dashboard
# archive. Snapshot before, compare after.
LIVE_INITS_BEFORE=""
snapshot_live_inits() {
  local index="${W2G_ROOT}/site/data/cycles.json"
  [ -f "${index}" ] || return 0
  LIVE_INITS_BEFORE="$("${W2G_PYTHON}" - "${index}" <<'PY'
import json, sys
from pathlib import Path
print("\n".join(sorted({c["issued_utc"] for c in
                        json.loads(Path(sys.argv[1]).read_text())})))
PY
)"
}

assert_no_init_regression() {
  [ -n "${LIVE_INITS_BEFORE}" ] || return 0
  say "Checking no published initialization was dropped"
  local after lost
  after="$("${W2G_PYTHON}" - "${W2G_ROOT}/site/data/cycles.json" "${W2G_ARCHIVE_ROOT}/data/cycles.json" <<'PY'
import json, sys
from pathlib import Path
site_path = Path(sys.argv[1])
archive_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
inits = set()
if site_path.is_file():
    inits.update(c["issued_utc"] for c in json.loads(site_path.read_text()))
if archive_path and archive_path.is_file():
    inits.update(c["issued_utc"] for c in json.loads(archive_path.read_text()))
print("\n".join(sorted(inits)))
PY
)"
  lost="$(comm -23 <(printf '%s\n' "${LIVE_INITS_BEFORE}") <(printf '%s\n' "${after}") || true)"
  if [ -n "${lost}" ]; then
    printf 'FATAL: this export would remove initializations the live site is serving:\n' >&2
    printf '  %s\n' ${lost} >&2
    cat >&2 <<EOF

  The dashboard archive at
    ${DASHBOARD_DIR}
  does not contain them, and the exporter prunes what it cannot see - so
  publishing now would roll the public site back to an older run.

  Import the missing bundle into the archive first, then rerun:
    ls ${W2G_ROOT}/latest/
    ${SG_REPO_ROOT}/scripts/import_dashboard_bundle.sh <bundle> --data-root ${SG_DATA_ROOT}

  site/data has been rewritten in your working tree but nothing is committed.
  Discard it with:  git -C ${W2G_ROOT} checkout -- site/data
EOF
    exit 6
  fi
  note "all previously published initializations survive"
}

# --------------------------------------------------------------- publish ---
# The workstation half already exists and is careful: it refuses a dirty repo,
# refuses a non-main branch, refuses to publish behind origin, runs the
# publication gate, and pushes the archive before the site so no archived run
# 404s in between. Reimplementing any of that here would only create a second
# thing to keep correct.
publish_cycles() {
  local do_push=$1 message=$2
  local publisher="${SG_REPO_ROOT}/scripts/publish_weather2grid.sh"
  [ -x "${publisher}" ] || die "missing ${publisher}" 5

  local args=()
  [ -n "${message}" ] && args+=(--message "${message}")
  [ "${do_push}" -eq 1 ] && args+=(--push)

  SG_DATA_ROOT="${SG_DATA_ROOT}" \
  SG_WEATHER2GRID_REPO="${W2G_ROOT}" \
  SG_WEATHER2GRID_ARCHIVE_REPO="${W2G_ARCHIVE_ROOT}" \
  SG_SKIP_TESTS=1 \
    "${publisher}" ${args[@]+"${args[@]}"}
}

# The publication gate runs inside publish_weather2grid.sh, but the pairing
# check is ours and has to happen against exported data. Export once here, so
# both checks see the same bytes that would be committed.
export_only() {
  snapshot_live_inits
  say "Exporting ${DASHBOARD_DIR} -> ${W2G_ROOT}/site/data"
  SG_DATA_ROOT="${SG_DATA_ROOT}" SG_WEATHER2GRID_REPO="${W2G_ROOT}" \
    "${SG_REPO_ROOT}/scripts/export_weather2grid.sh"
  assert_no_init_regression
}

print_footer() {
  local do_push=$1 label=$2
  shift 2
  echo
  echo "${label}"
  for line in "$@"; do
    echo "  ${line}"
  done
  if [ "${do_push}" -eq 0 ]; then
    echo
    echo "Nothing has been committed. Read the diff above, then publish with:"
    echo "  $0 --push  (or rerun exactly as before, adding --push)"
  fi
}
