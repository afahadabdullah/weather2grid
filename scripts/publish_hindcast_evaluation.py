#!/usr/bin/env python3
"""Publish a StormGrid hindcast evaluation bundle into the Weather2Grid archive.

A hindcast is a measurement of past performance, not a forecast, so it never
belongs in the live dashboard's initialization list: it goes straight to the
archive checkout, which is where the dashboard's hindcast picker looks. The
live site is not touched by this script at all.

The bundle this reads is what a StormGrid evaluation run stages:

    <evaluation-run>/
        evaluation_manifest.json
        dashboard/
            counties.geojson                 shared county layer for the run
            <cycle_id>/cycle.json            product metadata, product_kind=hindcast
            <cycle_id>/risk.parquet          county rows incl. observed_* columns

Everything written here goes through the same contract functions the live
exporter uses (`export_products`), so a hindcast payload is byte-compatible
with a forecast payload and the dashboard needs no special case to read it.

Two things differ from a plain `export_products` run, both deliberate:

* It is ADDITIVE. `export_products --archive-output` rebuilds the archive
  indexes from the runs in one product archive and prunes everything else; an
  evaluation bundle contains no live forecast runs, so that would delete the
  archived forecasts. This merges instead, replacing only cycles it republishes.

* Geometry is subset PER CYCLE rather than shared across the run. The map fits
  its view to the county layer it is handed, so a shared layer spanning every
  scored storm would zoom Harvey's 66 counties out to the whole evaluation
  domain and paint the other ~1,700 counties as no-data. Layers are
  content-addressed, so identical footprints still publish only once.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_products import (  # noqa: E402
    DASHBOARD_COLUMNS,
    _discard_unreferenced_geometries,
    _issued_value,
    columnar_counties,
    cycle_summary,
    initialization_index,
    publish_geometry,
    records,
    write_compact_json,
    write_json,
)

DEFAULT_ARCHIVE_BASE_URL = "https://afahadabdullah.github.io/weather2grid-archive"

REQUIRED_FIELDS = {
    "county_fips", "county_name", "state", "expected_customers_out",
    "p90_customers_out", "prob_outage_fraction_gt_05", "peak_gust_ms",
}

# What makes a payload a verification product rather than a forecast. Without
# these the dashboard's observed and error layers have nothing to draw, and
# publishing the run as a hindcast would be a claim the data cannot support.
VERIFICATION_FIELDS = {"observed_outage_fraction", "observed_customers_out"}


def load_manifest(run: Path) -> dict[str, Any]:
    path = run / "evaluation_manifest.json"
    if not path.exists():
        raise SystemExit(f"Not an evaluation run: {path} is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def subset_geometry(layer: dict[str, Any], fips: set[str]) -> dict[str, Any]:
    """The shared county layer restricted to the counties this cycle scored."""
    features = [
        feature for feature in layer.get("features", [])
        if str((feature.get("properties") or {}).get("county_fips", "")).zfill(5)
        in fips
    ]
    return {"type": "FeatureCollection", "features": features}


def publish_cycle(source: Path, archive: Path, layer: dict[str, Any],
                  base_url: str, now: datetime,
                  workspace: Path) -> dict[str, Any]:
    meta = json.loads((source / "cycle.json").read_text(encoding="utf-8"))
    meta.setdefault("cycle_id", source.name)
    if str(meta["cycle_id"]) != source.name:
        raise SystemExit(
            f"Cycle id {meta['cycle_id']!r} does not match directory {source.name!r}")
    if str(meta.get("product_kind")) != "hindcast":
        raise SystemExit(
            f"{source.name}: product_kind is {meta.get('product_kind')!r}, not "
            "'hindcast'. Publish forecast runs with export_products.py.")

    frame = pd.read_parquet(source / "risk.parquet")
    missing = sorted(REQUIRED_FIELDS - set(frame.columns))
    if missing:
        raise SystemExit(f"{source.name}: missing dashboard fields {missing}")
    missing = sorted(VERIFICATION_FIELDS - set(frame.columns))
    if missing:
        raise SystemExit(
            f"{source.name}: declared a hindcast but carries no observed "
            f"outcome ({missing}); it cannot be verified against anything.")

    summary = cycle_summary(meta, now)
    # Payload files are overwritten in place rather than removed and rewritten.
    # A cycle directory holds exactly counties.json, cycle.json and track.json,
    # so there is nothing to leave stale -- and some hosts mount the checkout
    # without delete permission, where a rmtree-first publish simply fails.
    target = archive / "cycles" / source.name
    target.mkdir(parents=True, exist_ok=True)

    dashboard_columns = [c for c in DASHBOARD_COLUMNS if c in frame]
    county_rows = records(frame[dashboard_columns])
    write_compact_json(target / "counties.json", columnar_counties(county_rows))

    fips = {str(value).zfill(5) for value in frame["county_fips"]}
    geometry = source / "counties.geojson"
    if geometry.exists():
        published_layer = json.loads(geometry.read_bytes())
    else:
        published_layer = subset_geometry(layer, fips)
    drawn = {
        str((f.get("properties") or {}).get("county_fips", "")).zfill(5)
        for f in published_layer.get("features", [])
    }
    if not drawn:
        raise SystemExit(
            f"{source.name}: none of its {len(fips)} counties are in the "
            "shared county layer; the map would render empty.")
    if fips - drawn:
        print(f"  warning: {len(fips - drawn)} scored counties have no geometry "
              f"and will not be drawn ({sorted(fips - drawn)[:5]}…)")
    layer_path = workspace / f"{source.name}.geojson"
    layer_path.write_text(
        json.dumps(published_layer, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8")
    summary["geometry_path"] = publish_geometry(layer_path, archive / "data")

    track = source / "track.json"
    if track.exists():
        track_data = json.loads(track.read_text(encoding="utf-8"))
        summary["track_available"] = bool(
            track_data.get("available", True) is not False
            and track_data.get("points"))
        shutil.copyfile(track, target / "track.json")
    else:
        summary["track_available"] = False
        write_json(target / "track.json",
                   {"available": False, "reason": "no track in this product"})

    fields = {
        "required": sorted(REQUIRED_FIELDS),
        "optional_present": sorted(set(frame.columns) - REQUIRED_FIELDS),
        "optional_absent": [],
        "cdf_quantiles": sorted(c for c in frame
                                if c.startswith("q") and c.endswith("_outage_fraction")),
    }
    write_json(target / "cycle.json", {**summary, "fields": fields, "meta": meta})

    # A hindcast is never the current run of anything: it is a fixed
    # measurement of a storm that has already happened. Claiming the flag would
    # let a 2016 storm arrive in the picker labelled "latest".
    summary["is_latest_initialization"] = False
    summary["data_base"] = base_url.rstrip("/")
    return summary


def refresh_shell(site: Path, archive: Path) -> None:
    """Re-copy the dashboard shell so the archive cannot drift from the site.

    The live site and the archive deliberately serve the same HTML, CSS and JS
    so they cannot become two applications; only their data indexes differ.
    Files are copied over, never deleted first, so this works on a checkout
    mounted without delete permission.
    """
    for name in ("index.html", "evaluation.html", "og.png"):
        source = site / name
        if source.exists():
            shutil.copyfile(source, archive / name)
    assets = archive / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for source in sorted((site / "assets").iterdir()):
        if source.is_file():
            shutil.copyfile(source, assets / source.name)
    (archive / ".nojekyll").touch()


def merge_indexes(archive: Path, published: list[dict[str, Any]],
                  now: datetime) -> list[dict[str, Any]]:
    data = archive / "data"
    index_path = data / "cycles.json"
    existing = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    replaced = {str(item["cycle_id"]) for item in published}
    summaries = [item for item in existing
                 if str(item.get("cycle_id")) not in replaced] + published
    for item in summaries:
        item["is_latest_initialization"] = False
    summaries.sort(key=lambda item: (-_issued_value(item),
                                     str(item.get("valid_start_utc") or ""),
                                     str(item["cycle_id"])))

    initializations = initialization_index(summaries)
    for item in initializations:
        item["is_latest_initialization"] = False

    status_path = data / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    status.update({
        "version": status.get("version", "0.1.0"),
        "generated_at_utc": now.isoformat(),
        "cycles": len(summaries),
        "initializations": len(initializations),
        "archive_view": True,
        "operational": False,
        "shadow_mode": True,
        "any_synthetic": any(item.get("synthetic") for item in summaries),
        "any_ungated": any(not item.get("release_gate_passed") for item in summaries),
        "banner": status.get("banner", {
            "level": "warning",
            "title": "ARCHIVED FORECAST — NOT CURRENT GUIDANCE",
            "detail": "You are viewing an older forecast initialization. Use "
                      "Live forecast for the current outlook.",
        }),
        "latest": summaries[0],
    })

    write_json(index_path, summaries)
    write_json(data / "initializations.json", initializations)
    write_json(status_path, status)
    try:
        _discard_unreferenced_geometries(summaries, data)
    except OSError as error:
        # Nothing references them, so leaving them costs disk and not
        # correctness. Worth saying out loud rather than failing the publish.
        print(f"  warning: could not remove unreferenced geometry ({error})")
    return summaries


def publish(run: Path, archive: Path, site: Path, base_url: str,
            allow_incomplete: bool = False,
            refresh_dashboard_shell: bool = True) -> list[dict[str, Any]]:
    manifest = load_manifest(run)
    if not manifest.get("evaluation_complete") and not allow_incomplete:
        raise SystemExit(
            f"{run.name} is not a complete evaluation "
            f"(missing {manifest.get('missing_storms')}, "
            f"skipped {manifest.get('skipped_storms')}). "
            "Publishing it would put a partial scorecard on a public page; "
            "pass --allow-incomplete only if that is what you mean.")

    staging = run / str(manifest.get("dashboard", {}).get("staging_root", "dashboard"))
    cycle_paths = sorted(staging.glob("*/risk.parquet"))
    if not cycle_paths:
        raise SystemExit(f"No */risk.parquet hindcasts found under {staging}")
    shared = staging / "counties.geojson"
    if not shared.exists():
        raise SystemExit(f"Missing shared county geometry: {shared}")
    layer = json.loads(shared.read_bytes())

    now = datetime.now(timezone.utc)
    published: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        for risk in cycle_paths:
            summary = publish_cycle(risk.parent, archive, layer, base_url, now,
                                    workspace)
            published.append(summary)
            print(f"  published {summary['cycle_id']} "
                  f"({summary.get('verification', {}).get('counties', '?')} counties)")

    if refresh_dashboard_shell:
        refresh_shell(site, archive)
    summaries = merge_indexes(archive, published, now)
    return summaries


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evaluation", type=Path, required=True,
                        help="Evaluation run directory containing "
                             "evaluation_manifest.json and dashboard/")
    parser.add_argument("--archive", type=Path, required=True,
                        help="weather2grid-archive checkout to publish into")
    parser.add_argument("--site", type=Path, default=root / "site",
                        help="Dashboard shell source (default: this repo's site/)")
    parser.add_argument("--archive-base-url", default=DEFAULT_ARCHIVE_BASE_URL,
                        help="Public URL the archive checkout is served from")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="Publish even when the evaluation manifest reports "
                             "missing or skipped storms")
    parser.add_argument("--no-shell-refresh", action="store_true",
                        help="Leave the archive's index.html/assets as they are")
    args = parser.parse_args()

    summaries = publish(
        args.evaluation.resolve(), args.archive.resolve(), args.site.resolve(),
        args.archive_base_url, allow_incomplete=args.allow_incomplete,
        refresh_dashboard_shell=not args.no_shell_refresh)
    hindcasts = [item for item in summaries
                 if item.get("product_kind") == "hindcast"]
    print(f"Archive now indexes {len(summaries)} cycles "
          f"({len(hindcasts)} hindcast, {len(summaries) - len(hindcasts)} forecast).")


if __name__ == "__main__":
    main()
