#!/usr/bin/env python3
"""Export a StormGrid product archive as static Weather2Grid JSON.

The public dashboard never imports StormGrid and never reads model inputs. It
consumes only the versioned product contract written by the modeling pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

DASHBOARD_COLUMNS = [
    "county_fips", "county_name", "state", "customers_total",
    "expected_customers_out", "p90_customers_out", "expected_outage_fraction",
    "prob_outage_fraction_gt_05", "weather_spread_pp", "impact_spread_pp",
    "peak_gust_ms", "duration_hr", "training_envelope_flag",
    "hazard_reference_quality", "data_quality_flag", "product_confidence",
    "q05_outage_fraction", "q10_outage_fraction", "q25_outage_fraction",
    "q50_outage_fraction", "q75_outage_fraction", "q90_outage_fraction",
    "q95_outage_fraction", "q99_outage_fraction",
    # Verification only. A forecast cycle cannot have these -- the outcome has
    # not happened yet -- so their presence is what distinguishes a hindcast
    # payload, and the map layer that uses them only exists for one.
    "observed_outage_fraction", "observed_customers_out",
    "residual_outage_fraction", "residual_customers_out",
]


def clean(value: Any) -> Any:
    """Convert pandas/numpy values to strict JSON values."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        converted = value.tolist()
        if isinstance(converted, list):
            return [clean(item) for item in converted]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return clean(value.item())
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )

def write_compact_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def age_hours(issued: str, now: datetime) -> float | None:
    try:
        timestamp = pd.Timestamp(issued)
    except (TypeError, ValueError):
        return None
    timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
    return round((pd.Timestamp(now) - timestamp).total_seconds() / 3600, 2)


def cycle_summary(meta: dict[str, Any], now: datetime) -> dict[str, Any]:
    issued = str(meta.get("forecast_init_time_utc", ""))
    age = age_hours(issued, now)
    freshness = "degraded" if meta.get("degraded_mode") else "unknown" if age is None else "stale" if age > 12 else "current"
    valid_start = meta.get("valid_start_utc")
    valid_end = meta.get("valid_end_utc")
    window_hours = None
    horizon_hours = None
    try:
        start = pd.Timestamp(valid_start)
        end = pd.Timestamp(valid_end)
        init = pd.Timestamp(issued)
        if not (pd.isna(start) or pd.isna(end)):
            window_hours = round((end - start).total_seconds() / 3600, 2)
        if not (pd.isna(init) or pd.isna(end)):
            horizon_hours = round((end - init).total_seconds() / 3600, 2)
    except (TypeError, ValueError):
        pass
    input_leads = meta.get("input_lead_hours")
    lead_step = meta.get("lead_step_hours")
    if not isinstance(input_leads, list):
        source = str(meta.get("hazard_source", "")).lower()
        if source.startswith("hrrr"):
            lead_step = 1.0
        elif source.startswith("weathernext"):
            lead_step = 6.0
        if lead_step and issued and valid_start and valid_end:
            try:
                init = pd.Timestamp(issued)
                start = pd.Timestamp(valid_start)
                end = pd.Timestamp(valid_end)
                first = round((start - init).total_seconds() / 3600)
                last = round((end - init).total_seconds() / 3600)
                input_leads = list(range(first, last + 1, round(float(lead_step))))
            except (TypeError, ValueError):
                input_leads = []
    input_leads = input_leads if isinstance(input_leads, list) else []
    return {
        "cycle_id": str(meta["cycle_id"]),
        "event_id": str(meta.get("event_id", "unknown")),
        "event_name": str(meta.get("event_name", meta.get("event_id", "Unknown event"))),
        "issued_utc": issued,
        "lead_hours": meta.get("lead_hours"),
        "age_hours": age,
        "freshness": freshness,
        "synthetic": bool(meta.get("synthetic", False)),
        "release_gate_passed": bool(meta.get("release_gate_passed", False)),
        "degraded_mode": bool(meta.get("degraded_mode", False)),
        "provider_status": meta.get("provider_status", "unknown"),
        "model_artifact_id": meta.get("model_artifact_id"),
        "hazard_source": meta.get("hazard_source"),
        "forecast_provider": meta.get("forecast_provider"),
        "valid_start_utc": valid_start,
        "valid_end_utc": valid_end,
        "forecast_window_hours": window_hours,
        "forecast_horizon_hours": horizon_hours,
        "input_lead_hours": clean(input_leads),
        "lead_step_hours": clean(lead_step),
        # Product window geometry, when the archive states it. An adapter that
        # slices one initialization into several windows says here how long
        # each window is and how far apart they start; when the step is shorter
        # than the window they overlap, and consecutive frames are successive
        # views of ONE forecast rather than separate events. The dashboard has
        # to say so, so it needs the numbers rather than a hardcoded guess.
        "product_window_hours": clean(meta.get("window_hours")),
        "product_step_hours": clean(meta.get("step_hours")),
        "windows_overlap": bool(meta.get("windows_overlap", False)),
        # A hindcast is a measurement of past performance, not a forecast.
        # The dashboard has to be able to tell them apart without inspecting
        # dates, so the kind travels explicitly rather than being inferred.
        "product_kind": str(meta.get("product_kind", "forecast")),
        "hazard_basis": clean(meta.get("hazard_basis")),
        "has_observed": bool(meta.get("has_observed", False)),
        "verification": clean(meta.get("verification")),
        "display_frame_count": 1,
        "county_data_format": "w2g-columnar-v1",
        "training_data_cutoff_utc": meta.get("training_data_cutoff_utc"),
    }


def _series_key(summary: dict[str, Any]) -> str:
    """Identify windows that belong to the same forecast initialization series.

    Hindcasts are keyed per storm, not pooled with the live forecast series.
    Sharing a series would let a newer forecast evict a hindcast through
    retention, or a hindcast claim to be the latest initialization of a live
    product -- both wrong, and the second is dangerous.
    """
    if str(summary.get("product_kind", "forecast")) == "hindcast":
        return f"hindcast:{summary.get('event_id', '')}"
    return str(summary.get("hazard_source", ""))


def _issued_value(summary: dict[str, Any]) -> float:
    try:
        issued = pd.Timestamp(summary.get("issued_utc"))
        return issued.timestamp() if not pd.isna(issued) else float("-inf")
    except (TypeError, ValueError):
        return float("-inf")


# How many forecast initializations per hazard source stay on the public site.
# The newest is "latest"; the rest are the history the archive picker offers.
#
# This is a size decision, not a taste one. One extended WeatherNext
# initialization is 25 windows of ~0.9 MB of county JSON, so ~22 MB on disk and
# ~5.6 MB of git objects once packed. Four initializations is roughly 88 MB in
# the working tree - well inside a GitHub Pages site, and the browser still
# fetches exactly one cycle at a time, so page weight does not change at all.
# What does grow without bound is git HISTORY, which retention cannot shrink:
# every publish adds its blobs forever. See docs/OPERATIONS.md.
DEFAULT_KEEP_INITIALIZATIONS = 4


def _apply_retention(summaries: list[dict[str, Any]], staging: Path,
                     keep: int) -> list[dict[str, Any]]:
    """Keep the newest `keep` initializations per series and drop older ones.

    Every window of a retained initialization is kept, because a forecast whose
    frames were partly evicted would animate with silent gaps. Each summary is
    tagged `is_latest_initialization`, which is what lets the dashboard show an
    archived run without presenting it as current guidance.
    """
    keep = max(1, int(keep))
    by_series: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries:
        by_series.setdefault(_series_key(summary), []).append(summary)

    kept: list[dict[str, Any]] = []
    for items in by_series.values():
        initializations = sorted({_issued_value(item) for item in items},
                                 reverse=True)
        retained = set(initializations[:keep])
        newest = initializations[0] if initializations else None
        for summary in items:
            value = _issued_value(summary)
            # "Latest initialization" is a statement about a live forecast
            # series. A hindcast is a fixed measurement of a past storm and is
            # never the current run of anything, so it never claims the flag --
            # otherwise a 2016 storm would arrive in the dashboard labelled
            # latest simply because it is the only run of its own series.
            summary["is_latest_initialization"] = (
                value == newest
                and summary.get("product_kind", "forecast") != "hindcast")
            if value in retained:
                kept.append(summary)
            else:
                shutil.rmtree(staging / "cycles" / str(summary["cycle_id"]),
                              ignore_errors=True)
    return kept


def initialization_index(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per retained initialization, newest first, for the picker."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for summary in summaries:
        key = (_series_key(summary), str(summary.get("issued_utc", "")))
        entry = grouped.setdefault(key, {
            "hazard_source": _series_key(summary),
            "forecast_provider": summary.get("forecast_provider"),
            "issued_utc": summary.get("issued_utc"),
            "is_latest_initialization": bool(
                summary.get("is_latest_initialization", False)),
            "cycles": 0,
            "horizon_hours": None,
            "product_window_hours": summary.get("product_window_hours"),
            "product_step_hours": summary.get("product_step_hours"),
            "windows_overlap": bool(summary.get("windows_overlap", False)),
        })
        entry["cycles"] += 1
        horizon = summary.get("forecast_horizon_hours")
        if isinstance(horizon, (int, float)):
            entry["horizon_hours"] = max(entry["horizon_hours"] or 0, horizon)
    return sorted(grouped.values(),
                  key=lambda entry: str(entry["issued_utc"]), reverse=True)


def _discard_unreferenced_geometries(
        summaries: list[dict[str, Any]], staging: Path) -> None:
    used = {str(summary.get("geometry_path")) for summary in summaries}
    geometry_root = staging / "geometries"
    if not geometry_root.exists():
        return
    for geometry in geometry_root.glob("*.geojson"):
        relative = geometry.relative_to(staging).as_posix()
        if relative not in used:
            geometry.unlink()


def banner(any_synthetic: bool, any_ungated: bool) -> dict[str, str]:
    if any_synthetic:
        return {
            "level": "critical",
            "title": "SYNTHETIC DATA — NOT A FORECAST",
            "detail": "Every number on this page was generated for development. It describes no real storm and no real grid.",
        }
    if any_ungated:
        return {
            "level": "warning",
            "title": "REAL-TIME FORECAST — EXPERIMENTAL",
            "detail": "The model is running on real-time weather data but has not passed its release gate; do not use it as operational guidance.",
        }
    return {
        "level": "info",
        "title": "Supplemental guidance",
        "detail": "Not an official forecast or warning. Official watches and warnings come from the NHC and NWS.",
    }


# Coordinate and value precision published to the browser.
#
# COORD_DECIMALS=4 is ~11 m at CONUS latitudes. The dashboard's most zoomed-in
# pixel is roughly 600 m across, so this is about sixty times finer than
# anything a viewer can resolve - but it is what makes the content hash of two
# byte-different exports of the *same* county boundaries agree. Before this,
# one archive wrote full float64 coordinates and another wrote six decimals, so
# `publish_geometry` hashed them differently and shipped two ~3-5 MB copies of
# an identical CONUS county layer; a user switching forecast source downloaded
# the second one for nothing.
#
# VALUE_SIGFIGS=6 applies the same reasoning to county metrics. The UI formats
# them to at most one decimal place, so seventeen significant digits of a
# float64 repr are pure transfer cost.
COORD_DECIMALS = 4
VALUE_SIGFIGS = 6


def round_sig(value: float, digits: int = VALUE_SIGFIGS) -> float:
    """Round to a fixed number of significant digits, keeping tiny quantiles."""
    if value == 0 or not math.isfinite(value):
        return value
    return round(value, digits - 1 - math.floor(math.log10(abs(value))))


def quantize_value(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    rounded = round_sig(value)
    # 33.0 costs three bytes less than 33.3360141700736 and reads the same.
    return int(rounded) if rounded == int(rounded) and abs(rounded) < 1e15 else rounded


def quantize_coordinates(coordinates: Any) -> Any:
    """Round a GeoJSON coordinate tree to COORD_DECIMALS in place of float64."""
    if coordinates and isinstance(coordinates[0], (int, float)):
        return [round(float(coordinates[0]), COORD_DECIMALS),
                round(float(coordinates[1]), COORD_DECIMALS)]
    return [quantize_coordinates(item) for item in coordinates]


def quantize_geojson(geometry: dict[str, Any]) -> dict[str, Any]:
    for feature in geometry.get("features", []):
        shape = feature.get("geometry") or {}
        if shape.get("coordinates") is not None:
            shape["coordinates"] = quantize_coordinates(shape["coordinates"])
    return geometry


def records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    frame = frame.copy()
    if "county_fips" in frame:
        frame["county_fips"] = frame["county_fips"].astype(str).str.zfill(5)
    return [{key: clean(value) for key, value in row.items()} for row in frame.to_dict("records")]

def columnar_counties(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Encode county rows without repeating every field name for every county."""
    columns = [column for column in DASHBOARD_COLUMNS if any(column in row for row in rows)]
    return {
        "format": "w2g-columnar-v1",
        "columns": columns,
        "rows": [[quantize_value(clean(row.get(column))) for column in columns]
                 for row in rows],
    }


# Two archives can carry the same county layer at different float precision -
# one from the shapefile at full float64, one already rounded. Their bytes
# never match, so a pure content hash publishes both, and the browser downloads
# a second multi-megabyte copy of boundaries it has already drawn. Anything
# that agrees to within the precision we publish renders identically, so it is
# the same layer for this dashboard's purposes.
# The tolerance is one and a half published grid steps (~16 m): a vertex that
# straddles a rounding boundary lands exactly one step away, and comparing
# floats at exactly one step is a coin flip on the last bit.
MATCH_TOLERANCE_DEG = 1.5 * 10 ** -COORD_DECIMALS


def _feature_rings(feature: dict[str, Any]) -> list[list[list[float]]]:
    shape = feature.get("geometry") or {}
    coordinates = shape.get("coordinates") or []
    polygons = [coordinates] if shape.get("type") == "Polygon" else coordinates
    return [ring for polygon in polygons for ring in polygon]


def _feature_key(feature: dict[str, Any]) -> str:
    properties = feature.get("properties") or {}
    return str(properties.get("county_fips") or properties.get("state") or feature.get("id") or "")


def same_layer(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when two GeoJSON layers agree to within published precision."""
    a = {_feature_key(f): f for f in left.get("features", [])}
    b = {_feature_key(f): f for f in right.get("features", [])}
    if a.keys() != b.keys():
        return False
    for key, feature in a.items():
        rings_a, rings_b = _feature_rings(feature), _feature_rings(b[key])
        if [len(r) for r in rings_a] != [len(r) for r in rings_b]:
            return False
        for ring_a, ring_b in zip(rings_a, rings_b):
            for point_a, point_b in zip(ring_a, ring_b):
                if (abs(point_a[0] - point_b[0]) > MATCH_TOLERANCE_DEG
                        or abs(point_a[1] - point_b[1]) > MATCH_TOLERANCE_DEG):
                    return False
    return True


def publish_geometry(source: Path, staging: Path) -> str:
    """Write one compact content-addressed copy of repeated county geometry."""
    geometry = quantize_geojson(json.loads(source.read_bytes()))
    serialized = json.dumps(
        geometry, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    relative = Path("geometries") / f"{digest}.geojson"
    target = staging / relative
    if target.exists():
        return relative.as_posix()
    for published in sorted((staging / "geometries").glob("*.geojson")):
        if same_layer(json.loads(published.read_bytes()), geometry):
            return published.relative_to(staging).as_posix()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(serialized, encoding="utf-8")
    return relative.as_posix()


def _relocate_archived_cycles(summaries: list[dict[str, Any]], staging: Path,
                              archive_output: Path, archive_base_url: str,
                              include_latest: bool = False) -> int:
    """Move every non-latest cycle's payload out of the published site tree.

    The main repository then carries exactly one initialization no matter how
    long the archive gets, and its git history stops growing with the number of
    runs kept. Older payloads go to a second checkout that is published to
    Pages under the same account, so they are SAME-ORIGIN with the site
    (scheme, host and port all match; only the path differs) and need no CORS
    configuration at all.

    Only `cycles/<id>/` moves. `cycles.json`, `initializations.json`,
    `status.json` and the content-addressed `geometries/` stay in the main
    repository: the index is small, and county geometry is deduplicated across
    every run, so keeping one copy beside the site is cheaper than copying it
    into the archive.

    With `include_latest`, the CURRENT run is offloaded too. That is the
    difference between a site repository that still grows by one run per
    publish and one that grows by a few hundred kilobytes of index -- flat, in
    practice. The cost is that the site then has no forecast data of its own:
    if the archive is unreachable, nothing renders, where otherwise the current
    run would still work. Off by default for that reason.
    """
    base = archive_base_url.rstrip("/")
    destination = archive_output / "cycles"
    destination.mkdir(parents=True, exist_ok=True)
    moved = 0
    retained: set[str] = set()
    for summary in summaries:
        cycle_id = str(summary["cycle_id"])
        if summary.get("is_latest_initialization") and not include_latest:
            continue
        retained.add(cycle_id)
        summary["data_base"] = base
        source = staging / "cycles" / cycle_id
        target = destination / cycle_id
        if not source.is_dir():
            # Already in the archive from an earlier publish and not rebuilt
            # this time. Nothing to move, but it must still be there.
            if not target.is_dir():
                raise SystemExit(
                    f"{cycle_id} is archived but its payload is in neither "
                    f"{source} nor {target}")
            continue
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(source), str(target))
        moved += 1

    # Retention already dropped these from the index; drop their payloads too,
    # or the archive grows forever while nothing links to them.
    if destination.is_dir():
        for existing in sorted(destination.iterdir()):
            if existing.is_dir() and existing.name not in retained:
                shutil.rmtree(existing, ignore_errors=True)
    return moved


def _build_snapshot(archive: Path, cycle_paths: list[Path],
                    staging: Path, output: Path,
                    merge: bool = False,
                    keep_initializations: int = DEFAULT_KEEP_INITIALIZATIONS,
                    archive_output: Path | None = None,
                    archive_base_url: str = "",
                    offload_current: bool = False,
                    ) -> list[dict[str, Any]]:
    """Validate and completely build one not-yet-public snapshot."""
    now = datetime.now(timezone.utc)
    summaries: list[dict[str, Any]] = []
    processed_cycles: set[str] = set()
    for risk_path in cycle_paths:
        source = risk_path.parent
        processed_cycles.add(source.name)
        meta_path = source / "cycle.json"
        if not meta_path.exists():
            raise SystemExit(f"Missing product metadata: {meta_path}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.setdefault("cycle_id", source.name)
        if str(meta["cycle_id"]) != source.name:
            raise SystemExit(f"Cycle id {meta['cycle_id']!r} does not match directory {source.name!r}")

        frame = pd.read_parquet(risk_path)
        required = {
            "county_fips", "county_name", "state", "expected_customers_out",
            "p90_customers_out", "prob_outage_fraction_gt_05", "peak_gust_ms",
        }
        missing = sorted(required - set(frame.columns))
        if missing:
            raise SystemExit(f"{source.name}: missing dashboard fields {missing}")

        summary = cycle_summary(meta, now)
        summaries.append(summary)
        target = staging / "cycles" / source.name
        fields = {
            "required": sorted(required),
            "optional_present": sorted(set(frame.columns) - required),
            "optional_absent": [],
            "cdf_quantiles": sorted(column for column in frame if column.startswith("q") and column.endswith("_outage_fraction")),
        }
        dashboard_columns = [column for column in DASHBOARD_COLUMNS if column in frame]
        county_rows = records(frame[dashboard_columns])
        write_compact_json(target / "counties.json", columnar_counties(county_rows))

        geometry = source / "counties.geojson"
        if not geometry.exists():
            geometry = archive / "counties.geojson"
        summary["geometry_path"] = publish_geometry(geometry, staging)

        track = source / "track.json"
        if not track.exists():
            track = archive / "track.json"
        if track.exists():
            try:
                track_data = json.loads(track.read_text(encoding="utf-8"))
                summary["track_available"] = bool(
                    track_data.get("available", True) is not False
                    and track_data.get("points")
                )
            except (OSError, json.JSONDecodeError):
                summary["track_available"] = False
            shutil.copyfile(track, target / "track.json")
        else:
            summary["track_available"] = False
            write_json(target / "track.json", {"available": False, "reason": "no track in this product"})
        write_json(target / "cycle.json", {**summary, "fields": fields, "meta": meta})

    if merge and (output / "cycles").exists():
        for existing_dir in sorted((output / "cycles").iterdir()):
            if not existing_dir.is_dir() or existing_dir.name in processed_cycles:
                continue
            dest_dir = staging / "cycles" / existing_dir.name
            shutil.copytree(existing_dir, dest_dir)
            meta_file = dest_dir / "cycle.json"
            if meta_file.exists():
                existing_cycle_data = json.loads(meta_file.read_text(encoding="utf-8"))
                meta = existing_cycle_data.get("meta", existing_cycle_data)
                summary = cycle_summary(meta, now)
                counties_file = dest_dir / "counties.json"
                county_payload = json.loads(counties_file.read_text(encoding="utf-8"))
                if isinstance(county_payload, list):
                    write_compact_json(
                        counties_file, columnar_counties(county_payload))
                geometry_file = dest_dir / "counties.geojson"
                if not geometry_file.exists() and existing_cycle_data.get("geometry_path"):
                    geometry_file = output / str(existing_cycle_data["geometry_path"])
                if geometry_file.exists():
                    summary["geometry_path"] = publish_geometry(geometry_file, staging)
                    cycle_geometry = dest_dir / "counties.geojson"
                    if cycle_geometry.exists():
                        cycle_geometry.unlink()
                track_file = dest_dir / "track.json"
                if track_file.exists():
                    try:
                        track_data = json.loads(track_file.read_text(encoding="utf-8"))
                        summary["track_available"] = bool(
                            track_data.get("available", True) is not False
                            and track_data.get("points")
                        )
                    except (OSError, json.JSONDecodeError):
                        summary["track_available"] = False
                else:
                    summary["track_available"] = False
                existing_cycle_data.update(summary)
                write_json(meta_file, existing_cycle_data)
                summaries.append(summary)

    summaries = _apply_retention(summaries, staging, keep_initializations)
    if archive_output is not None:
        _relocate_archived_cycles(summaries, staging, archive_output,
                                  archive_base_url,
                                  include_latest=offload_current)
    # Runs after relocation, and over ALL summaries: an archived cycle still
    # references its geometry, which stays in the main repository.
    _discard_unreferenced_geometries(summaries, staging)
    # Newest initialization first, and within one initialization the nearest
    # window first. The old cycle_id sort put the FARTHEST window at index 0,
    # which made status.json's "latest" the seven-day frame; harmless while the
    # site re-sorted anyway, wrong now that "latest" also has to mean "not one
    # of the archived runs".
    summaries.sort(key=lambda item: (-_issued_value(item),
                                     str(item.get("valid_start_utc") or ""),
                                     str(item["cycle_id"])))
    initializations = initialization_index(summaries)
    latest_only = [item for item in summaries
                   if item.get("is_latest_initialization")
                   and item.get("product_kind", "forecast") != "hindcast"]
    # Banner state describes the CURRENT forecast. An archived run that was
    # synthetic must not make today's real product claim synthetic, and an
    # archived run cannot make an ungated product look gated either.
    any_synthetic = any(item["synthetic"] for item in latest_only)
    any_ungated = any(not item["release_gate_passed"] for item in latest_only)
    write_json(staging / "cycles.json", summaries)
    write_json(staging / "initializations.json", initializations)
    write_json(
        staging / "status.json",
        {
            "version": "0.1.0",
            "generated_at_utc": now.isoformat(),
            "cycles": len(summaries),
            "cycles_latest_initialization": len(latest_only),
            "initializations": len(initializations),
            "keep_initializations": max(1, int(keep_initializations)),
            "archive_base_url": archive_base_url or None,
            "operational": not (any_synthetic or any_ungated),
            "shadow_mode": bool(any_ungated and not any_synthetic),
            "any_synthetic": any_synthetic,
            "any_ungated": any_ungated,
            "banner": banner(any_synthetic, any_ungated),
            "latest": (latest_only or summaries)[0],
        },
    )

    # Official active tropical-cyclone tracks are deliberately outside every
    # StormGrid county-risk cycle: an NHC advisory is a forecaster-issued
    # storm-map product, not a county-outage prediction.  Copy it as a
    # separate static layer when the StormGrid NHC fetch command has refreshed
    # the archive.  Keeping this publish-time avoids browser CORS/rate-limit
    # failures and makes the displayed advisory timestamp auditable.
    nhc_tracks = archive / "nhc-active-tracks.json"
    existing_nhc = output / "nhc-active-tracks.json"
    if nhc_tracks.exists():
        shutil.copyfile(nhc_tracks, staging / "nhc-active-tracks.json")
    elif existing_nhc.exists():
        shutil.copyfile(existing_nhc, staging / "nhc-active-tracks.json")
    else:
        write_json(staging / "nhc-active-tracks.json", {
            "available": False,
            "source": "NOAA NHC Tropical Weather Summary MapServer",
            "reason": "The archive has not been refreshed with fetch-nhc-tracks.",
            "tracks": [],
        })

    # PowerOutage.us credentials and licensing must stay behind a server-side
    # integration.  A public GitHub Pages bundle must never contain an API
    # key, nor claim observed outages when no licensed feed is configured.
    write_json(staging / "live-outage-status.json", {
        "available": False,
        "provider": "PowerOutage.us",
        "reason": "Live observed outage data is disabled until a licensed server-side API integration is configured.",
        "forecast_data_remains_separate": True,
    })

    # basemap.geojson is static site chrome (coastlines/state outlines) that
    # a StormGrid product archive has never been responsible for producing -
    # nothing in stormgrid writes one. Only fall back to an empty
    # FeatureCollection when there is truly nothing to keep; otherwise an
    # archive that (correctly) omits it would silently blank out the map's
    # base layer on every export.
    basemap = archive / "basemap.geojson"
    existing_basemap = output / "basemap.geojson"
    if basemap.exists():
        write_compact_json(staging / "basemap.geojson",
                           quantize_geojson(json.loads(basemap.read_bytes())))
    elif existing_basemap.exists():
        write_compact_json(staging / "basemap.geojson",
                           quantize_geojson(json.loads(existing_basemap.read_bytes())))
    else:
        write_json(staging / "basemap.geojson", {"type": "FeatureCollection", "features": []})
    return summaries


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def export_archive(archive: Path, output: Path, merge: bool = False,
                   keep_initializations: int = DEFAULT_KEEP_INITIALIZATIONS,
                   archive_output: Path | None = None,
                   archive_base_url: str = "",
                   offload_current: bool = False,
                   ) -> list[dict[str, Any]]:
    archive = archive.resolve()
    output = output.resolve()
    if archive_output is not None:
        archive_output = Path(archive_output).resolve()
        if not archive_base_url.strip():
            raise SystemExit(
                "--archive-output needs --archive-base-url: without the URL the "
                "site would link archived runs to a path that does not exist "
                "on it, and every archived cycle would 404 in the browser.")
        if not archive_base_url.startswith(("http://", "https://", "/")):
            raise SystemExit(
                f"--archive-base-url must be absolute (https://... or /...), "
                f"got {archive_base_url!r}. A relative value would resolve "
                "against whatever page the viewer happens to be on.")
    cycle_paths = sorted(archive.glob("*/risk.parquet"))
    if not cycle_paths:
        raise SystemExit(f"No */risk.parquet products found under {archive}")
    if not (archive / "counties.geojson").exists():
        raise SystemExit(f"Missing shared county geometry: {archive / 'counties.geojson'}")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    backup = output.with_name(f".{output.name}.backup-{os.getpid()}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    try:
        summaries = _build_snapshot(
            archive, cycle_paths, staging, output, merge=merge,
            keep_initializations=keep_initializations,
            archive_output=archive_output, archive_base_url=archive_base_url,
            offload_current=offload_current)
        if output.exists():
            os.replace(output, backup)
        os.replace(staging, output)
    except Exception:
        if backup.exists() and not output.exists():
            os.replace(backup, output)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    return summaries


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="StormGrid product archive")
    parser.add_argument("--output", type=Path, default=root / "site" / "data", help="Static data destination")
    parser.add_argument("--merge", action="store_true", help="Merge with existing exported cycles in output")
    parser.add_argument(
        "--keep-initializations", type=int, default=DEFAULT_KEEP_INITIALIZATIONS,
        help="Forecast initializations kept per hazard source. The newest is "
             "shown by default; the rest are offered in the archive picker. "
             f"Default {DEFAULT_KEEP_INITIALIZATIONS}.")
    parser.add_argument(
        "--archive-output", type=Path, default=None,
        help="Write older initializations' payloads here instead of into the "
             "published site, so the site repository never carries more than "
             "one run. Point it at a second Pages checkout.")
    parser.add_argument(
        "--archive-base-url", default="",
        help="Public URL the --archive-output directory is served from, e.g. "
             "https://<user>.github.io/weather2grid-archive. Required with "
             "--archive-output.")
    parser.add_argument(
        "--offload-current", action="store_true",
        help="Also move the CURRENT run's payload to --archive-output, leaving "
             "the site repository with only code, indexes and geometry. Its "
             "history then stops growing with publishes almost entirely. The "
             "site has no forecast data of its own after this, so nothing "
             "renders if the archive is unreachable.")
    args = parser.parse_args()
    summaries = export_archive(args.archive, args.output, merge=args.merge,
                               keep_initializations=args.keep_initializations,
                               archive_output=args.archive_output,
                               archive_base_url=args.archive_base_url,
                               offload_current=args.offload_current)
    output = args.output.resolve()
    index = initialization_index(summaries)
    latest = [entry for entry in index if entry["is_latest_initialization"]]
    print(f"Exported {len(summaries)} forecast cycles across {len(index)} "
          f"initializations to {output}")
    for entry in index:
        marker = "latest " if entry["is_latest_initialization"] else "archive"
        print(f"  {marker}  {entry['issued_utc']}  {entry['cycles']:>3} cycles  "
              f"{entry['hazard_source']}")
    size = _tree_bytes(output)
    print(f"site/data is now {size / 1e6:.1f} MB "
          f"({len(latest)} live series, --keep-initializations "
          f"{args.keep_initializations})")
    if args.archive_output is not None:
        archived = _tree_bytes(args.archive_output)
        print(f"archive is {archived / 1e6:.1f} MB at {args.archive_output} "
              f"-> {args.archive_base_url}")
        print("The site repository now carries one initialization regardless "
              "of how many are kept, so its history stops growing with the "
              "archive. Reset the archive repository's history when it gets "
              "large; nothing links to its old commits.")
        return
    # A static Pages site can carry this; the repository's git history is what
    # grows without bound, because every publish adds blobs that retention
    # cannot remove. Say so at the point where it becomes visible.
    if size > 250e6:
        print("WARNING: site/data is over 250 MB. Lower --keep-initializations, "
              "or move publishing to a force-rebuilt orphan branch so history "
              "stops accumulating.")


if __name__ == "__main__":
    main()
