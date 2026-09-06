#!/usr/bin/env bash
# Unpack a live-inputs bundle made on Prism by stormgrid's
# scripts/pack_live_inputs.sh, into this machine's StormGrid data root.
#
# Every path inside the bundle is relative to the data root it was packed
# from, so extracting at this machine's data root reproduces Prism's layout
# exactly. Nothing is rewritten or relocated.
#
# The bundle arrives from another machine, so it is checked before it is
# trusted: the sha256 sidecar must match, every member must be a plain
# relative path to a regular file or directory, and every file's own digest is
# verified against the manifest after extraction. Absolute paths, "..",
# symlinks and device nodes are refused rather than extracted.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
W2G_ROOT="$(cd "${here}/.." && pwd)"
SG_REPO_ROOT="${SG_REPO_ROOT:-$(cd "${W2G_ROOT}/../stormgrid" 2>/dev/null && pwd || true)}"
DATA_ROOT="${SG_DATA_ROOT:-}"
BUNDLE=""
force=0

say()  { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die()  { printf 'FATAL: %s\n' "$1" >&2; exit "${2:-1}"; }

usage() {
  cat <<'EOF'
Usage:
  ./scripts/unpack_live_inputs.sh BUNDLE.tar.gz [--data-root DIR] [--force]

  BUNDLE.tar.gz     made on Prism by stormgrid/scripts/pack_live_inputs.sh.
                    Its .sha256 sidecar must sit beside it.
  --data-root DIR   where to extract (default: $SG_DATA_ROOT, else
                    <stormgrid>/data)
  --force           overwrite existing files without asking
  -h, --help
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  '') usage >&2; exit 2 ;;
esac
BUNDLE="$1"; shift

while [ $# -gt 0 ]; do
  case "$1" in
    --data-root) DATA_ROOT="${2:?--data-root needs a directory}"; shift 2 ;;
    --force) force=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument $1" 2 ;;
  esac
done

[ -f "${BUNDLE}" ] || die "no such bundle: ${BUNDLE}" 2
BUNDLE="$(cd "$(dirname "${BUNDLE}")" && pwd)/$(basename "${BUNDLE}")"

if [ -z "${DATA_ROOT}" ]; then
  [ -n "${SG_REPO_ROOT}" ] && [ -d "${SG_REPO_ROOT}" ] \
    || die "cannot find the stormgrid checkout. Pass --data-root, or set
  SG_REPO_ROOT." 2
  DATA_ROOT="${SG_REPO_ROOT}/data"
fi
mkdir -p "${DATA_ROOT}"
DATA_ROOT="$(cd "${DATA_ROOT}" && pwd)"

# ------------------------------------------------------------- checksum ---
say "Verifying the bundle checksum"
sidecar="${BUNDLE}.sha256"
[ -f "${sidecar}" ] || die "missing checksum sidecar: ${sidecar}
  Copy it down alongside the bundle; an unverified transfer is not worth
  extracting over your model inputs." 2
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$(dirname "${BUNDLE}")" && sha256sum -c "$(basename "${sidecar}")")
elif command -v shasum >/dev/null 2>&1; then
  (cd "$(dirname "${BUNDLE}")" && shasum -a 256 -c "$(basename "${sidecar}")")
else
  die "neither sha256sum nor shasum is available" 2
fi

# ---------------------------------------------------------------- paths ---
say "Inspecting archive members"
members="$(tar -tzf "${BUNDLE}")"
[ -n "${members}" ] || die "the bundle is empty" 3
while IFS= read -r entry; do
  [ -n "${entry}" ] || continue
  case "${entry}" in
    /*|*..*) die "refusing unsafe path in bundle: ${entry}" 3 ;;
  esac
  # Only the trees this bundle is defined to carry. A member outside them is
  # either a different kind of archive or something that should not be
  # landing in a data root.
  # These patterns also cover the bare directory entries ("products/"),
  # because * matches the empty string.
  case "${entry}" in
    products/*|artifacts/*|processed/*|interim/*|raw/*) ;;
    config/*|live-inputs-manifest.json) ;;
    *) die "unexpected entry in bundle: ${entry}
  This does not look like a stormgrid-live-inputs bundle." 3 ;;
  esac
done <<< "${members}"

# Reject anything that is not a regular file or a directory.
if tar -tvzf "${BUNDLE}" | awk '{print substr($1,1,1)}' | grep -qv '^[d-]$'; then
  die "the bundle contains a symlink or special file; refusing to extract" 3
fi
note "$(printf '%s\n' "${members}" | grep -c . ) members, all relative and plain"

# ------------------------------------------------------------- manifest ---
staging="$(mktemp -d "${TMPDIR:-/tmp}/sg-unpack-XXXXXX")"
trap 'rm -rf "${staging}"' EXIT
tar -C "${staging}" -xzf "${BUNDLE}" live-inputs-manifest.json 2>/dev/null || true
manifest="${staging}/live-inputs-manifest.json"

if [ -f "${manifest}" ]; then
  say "Bundle contents"
  python3 - "${manifest}" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
if manifest.get("kind") != "stormgrid-live-inputs":
    raise SystemExit(f"FATAL: unexpected bundle kind {manifest.get('kind')!r}")
print(f"   artifact:  {manifest['model_artifact_id']}")
print(f"   packed:    {manifest['packed_at_utc']} on {manifest['packed_on']}")
print(f"   from:      {manifest['packed_from_data_root']}")
print(f"   contents:  {manifest['file_count']} files, "
      f"{manifest['total_bytes'] / 1024 / 1024:.1f} MiB")
PY
else
  note "no manifest in this bundle; per-file digests cannot be verified"
fi

# ------------------------------------------------------------ overwrite ---
# A live data root is not scratch space. Say what is about to be replaced
# before replacing it, so a re-copy of an older bundle cannot silently roll
# the pinned artifact backwards.
existing=()
while IFS= read -r entry; do
  case "${entry}" in
    */|config/*|live-inputs-manifest.json) continue ;;
  esac
  [ -f "${DATA_ROOT}/${entry}" ] && existing+=("${entry}")
done <<< "${members}"

if [ "${#existing[@]}" -gt 0 ] && [ "${force}" -eq 0 ]; then
  say "${#existing[@]} file(s) already exist and will be overwritten"
  printf '   %s\n' "${existing[@]:0:10}"
  [ "${#existing[@]}" -gt 10 ] && note "... and $(( ${#existing[@]} - 10 )) more"
  if [ -t 0 ]; then
    printf '\n   Overwrite them? [y/N] '
    read -r answer
    case "${answer}" in
      [Yy]*) ;;
      *) die "nothing extracted." 0 ;;
    esac
  else
    die "refusing to overwrite without confirmation. Rerun with --force." 3
  fi
fi

# -------------------------------------------------------------- extract ---
say "Extracting into ${DATA_ROOT}"
# config/ and the manifest are not data-root content; everything else is.
tar -C "${DATA_ROOT}" -xzf "${BUNDLE}" \
  --exclude 'config/*' --exclude 'live-inputs-manifest.json'
note "done"

# ---------------------------------------------------------------- verify ---
if [ -f "${manifest}" ]; then
  say "Verifying every extracted file against the manifest"
  python3 - "${manifest}" "${DATA_ROOT}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])

def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

problems = []
for relative, expected in sorted(manifest["files"].items()):
    path = root / relative
    if not path.is_file():
        problems.append(f"{relative}: did not land")
        continue
    if path.stat().st_size != expected["bytes"]:
        problems.append(f"{relative}: size differs")
        continue
    if digest(path) != expected["sha256"]:
        problems.append(f"{relative}: content differs from the packed original")

if problems:
    print(f"   {len(problems)} problem(s):")
    for problem in problems:
        print(f"     - {problem}")
    raise SystemExit(1)
print(f"   all {len(manifest['files'])} files match their packed digests")
PY
fi

# ------------------------------------------------------------- site.env ---
# Config, not data, and it may hold local edits. Never overwrite it silently.
if tar -tzf "${BUNDLE}" | grep -q '^config/site\.env$'; then
  tar -C "${staging}" -xzf "${BUNDLE}" config/site.env
  target="${SG_REPO_ROOT}/scripts/hpc/site.env"
  if [ -n "${SG_REPO_ROOT}" ] && [ ! -f "${target}" ]; then
    mkdir -p "$(dirname "${target}")"
    cp "${staging}/config/site.env" "${target}"
    say "Installed scripts/hpc/site.env"
  else
    cp "${staging}/config/site.env" "${DATA_ROOT}/site.env.from-prism"
    say "site.env already exists here, so Prism's copy was left at"
    note "${DATA_ROOT}/site.env.from-prism"
    note "Compare them yourself rather than letting a transfer overwrite config."
  fi
fi

# -------------------------------------------------------- does it load? ---
# Files landing intact is not the same as a usable model. The artifact's four
# files are hash-checked against each other at load time, so this is the check
# that actually says "you can run a forecast now".
say "Loading the artifact"
sg_python="${SG_PYTHON:-${SG_REPO_ROOT}/.venv/bin/python}"
if [ -x "${sg_python}" ] && "${sg_python}" -c 'import stormgrid' 2>/dev/null; then
  "${sg_python}" - "${DATA_ROOT}" <<'PY'
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
print(f"   artifact {artifact_id}: loads and is release-assessed")

for candidate in (root / "interim/eaglei/denominator/county_customers_2022.parquet",
                  root / "interim/eaglei/denominator.parquet"):
    if candidate.exists():
        import pandas as pd
        print(f"   denominator: {len(pd.read_parquet(candidate))} counties")
        break
else:
    print("   denominator: ABSENT - no forecast can run")

calibration = root / "processed" / "calibration.json"
if calibration.is_file():
    from stormgrid.real_data import _load_calibrations
    print(f"   calibration: {len(_load_calibrations(calibration))} HRRR version(s)")
else:
    print("   calibration: ABSENT - the HRRR pipeline will not run")
PY
else
  note "no StormGrid environment yet, so the artifact was not loaded."
  note "Build one, then rerun this or the bootstrap to verify:"
  note "  ${W2G_ROOT}/scripts/bootstrap_local_stormgrid.sh --env-only"
fi

cat <<EOF

UNPACKED
  data root: ${DATA_ROOT}

Next:
  ${W2G_ROOT}/scripts/run_hrrr_live.sh
      Free and credential-free. If this publishes, the local chain works.

  ${W2G_ROOT}/scripts/run_weathernext3_live.sh --estimate
      Prices both BigQuery reads without billing anything.
EOF
