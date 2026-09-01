#!/usr/bin/env python3
"""Export a StormGrid product archive as static Weather2Grid JSON.

The public dashboard never imports StormGrid and never reads model inputs. It
consumes only the versioned product contract written by the modeling pipeline.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def clean(value: Any) -> Any:
    """Convert pandas/numpy values to strict JSON values."""
    if value is None:
        return None
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
        "training_data_cutoff_utc": meta.get("training_data_cutoff_utc"),
    }


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
            "title": "SHADOW MODE — NOT FOR OPERATIONAL USE",
            "detail": "The model artifact behind this product has not passed its release gate. Guidance only.",
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


def export_archive(archive: Path, output: Path) -> list[dict[str, Any]]:
    archive = archive.resolve()
    output = output.resolve()
    cycle_paths = sorted(archive.glob("*/risk.parquet"))
    if not cycle_paths:
        raise SystemExit(f"No */risk.parquet products found under {archive}")
    if not (archive / "counties.geojson").exists():
        raise SystemExit(f"Missing shared county geometry: {archive / 'counties.geojson'}")

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    summaries: list[dict[str, Any]] = []
    for risk_path in cycle_paths:
        source = risk_path.parent
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
        target = output / "cycles" / source.name
        fields = {
            "required": sorted(required),
            "optional_present": sorted(set(frame.columns) - required),
            "optional_absent": [],
            "cdf_quantiles": sorted(column for column in frame if column.startswith("q") and column.endswith("_outage_fraction")),
        }
        write_json(target / "cycle.json", {**summary, "fields": fields, "meta": meta})
        write_json(target / "counties.json", records(frame))

        geometry = source / "counties.geojson"
        if not geometry.exists():
            geometry = archive / "counties.geojson"
        shutil.copyfile(geometry, target / "counties.geojson")

        track = source / "track.json"
        if not track.exists():
            track = archive / "track.json"
        if track.exists():
            shutil.copyfile(track, target / "track.json")
        else:
            write_json(target / "track.json", {"available": False, "reason": "no track in this product"})

    summaries.sort(key=lambda item: item["cycle_id"], reverse=True)
    any_synthetic = any(item["synthetic"] for item in summaries)
    any_ungated = any(not item["release_gate_passed"] for item in summaries)
    write_json(output / "cycles.json", summaries)
    write_json(
        output / "status.json",
        {
            "version": "0.1.0",
            "generated_at_utc": now.isoformat(),
            "cycles": len(summaries),
            "operational": not (any_synthetic or any_ungated),
            "shadow_mode": True,
            "any_synthetic": any_synthetic,
            "any_ungated": any_ungated,
            "banner": banner(any_synthetic, any_ungated),
            "latest": summaries[0],
        },
    )

    basemap = archive / "basemap.geojson"
    if basemap.exists():
        shutil.copyfile(basemap, output / "basemap.geojson")
    else:
        write_json(output / "basemap.geojson", {"type": "FeatureCollection", "features": []})
    return summaries


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="StormGrid product archive")
    parser.add_argument("--output", type=Path, default=root / "site" / "data", help="Static data destination")
    args = parser.parse_args()
    summaries = export_archive(args.archive, args.output)
    print(f"Exported {len(summaries)} forecast cycles to {args.output.resolve()}")


if __name__ == "__main__":
    main()
