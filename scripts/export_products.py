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
        "display_frame_count": 1,
        "county_data_format": "w2g-columnar-v1",
        "training_data_cutoff_utc": meta.get("training_data_cutoff_utc"),
    }


def _series_key(summary: dict[str, Any]) -> str:
    """Identify windows that belong to the same forecast initialization series."""
    return str(summary.get("hazard_source", ""))


def _discard_older_initializations(summaries: list[dict[str, Any]], staging: Path) -> list[dict[str, Any]]:
    """Keep every window from the newest initialization, and remove older runs."""
    newest: dict[str, float] = {}
    issued_values: dict[str, float] = {}
    for summary in summaries:
        try:
            issued = pd.Timestamp(summary.get("issued_utc"))
            value = issued.timestamp() if not pd.isna(issued) else float("-inf")
        except (TypeError, ValueError):
            value = float("-inf")
        issued_values[str(summary["cycle_id"])] = value
        key = _series_key(summary)
        newest[key] = max(newest.get(key, float("-inf")), value)

    kept: list[dict[str, Any]] = []
    for summary in summaries:
        if issued_values[str(summary["cycle_id"])] == newest[_series_key(summary)]:
            kept.append(summary)
            continue
        shutil.rmtree(staging / "cycles" / str(summary["cycle_id"]), ignore_errors=True)
    return kept


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
        "rows": [[clean(row.get(column)) for column in columns] for row in rows],
    }


def publish_geometry(source: Path, staging: Path) -> str:
    """Write one compact content-addressed copy of repeated county geometry."""
    geometry = json.loads(source.read_bytes())
    serialized = json.dumps(
        geometry, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    relative = Path("geometries") / f"{digest}.geojson"
    target = staging / relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(serialized, encoding="utf-8")
    return relative.as_posix()


def _build_snapshot(archive: Path, cycle_paths: list[Path],
                    staging: Path, output: Path,
                    merge: bool = False) -> list[dict[str, Any]]:
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

    summaries = _discard_older_initializations(summaries, staging)
    _discard_unreferenced_geometries(summaries, staging)
    summaries.sort(key=lambda item: item["cycle_id"], reverse=True)
    any_synthetic = any(item["synthetic"] for item in summaries)
    any_ungated = any(not item["release_gate_passed"] for item in summaries)
    write_json(staging / "cycles.json", summaries)
    write_json(
        staging / "status.json",
        {
            "version": "0.1.0",
            "generated_at_utc": now.isoformat(),
            "cycles": len(summaries),
            "operational": not (any_synthetic or any_ungated),
            "shadow_mode": bool(any_ungated and not any_synthetic),
            "any_synthetic": any_synthetic,
            "any_ungated": any_ungated,
            "banner": banner(any_synthetic, any_ungated),
            "latest": summaries[0],
        },
    )

    # basemap.geojson is static site chrome (coastlines/state outlines) that
    # a StormGrid product archive has never been responsible for producing -
    # nothing in stormgrid writes one. Only fall back to an empty
    # FeatureCollection when there is truly nothing to keep; otherwise an
    # archive that (correctly) omits it would silently blank out the map's
    # base layer on every export.
    basemap = archive / "basemap.geojson"
    existing_basemap = output / "basemap.geojson"
    if basemap.exists():
        shutil.copyfile(basemap, staging / "basemap.geojson")
    elif existing_basemap.exists():
        shutil.copyfile(existing_basemap, staging / "basemap.geojson")
    else:
        write_json(staging / "basemap.geojson", {"type": "FeatureCollection", "features": []})
    return summaries


def export_archive(archive: Path, output: Path, merge: bool = False) -> list[dict[str, Any]]:
    archive = archive.resolve()
    output = output.resolve()
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
        summaries = _build_snapshot(archive, cycle_paths, staging, output, merge=merge)
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
    args = parser.parse_args()
    summaries = export_archive(args.archive, args.output, merge=args.merge)
    print(f"Exported {len(summaries)} forecast cycles to {args.output.resolve()}")


if __name__ == "__main__":
    main()
