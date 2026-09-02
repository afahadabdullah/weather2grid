#!/usr/bin/env python3
"""Fetch active NHC advisory tracks for the static GitHub Pages overlay.

The script has no third-party dependencies so a scheduled GitHub Actions run
can refresh the map independently of the StormGrid forecast-product export.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = ("https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/"
        "NHC_tropical_weather_summary/MapServer")


def attrs(feature: dict[str, Any]) -> dict[str, Any]:
    return {str(key).lower(): value for key, value in
            (feature.get("properties") or {}).items()}


def number(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (float, int)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat()
    return str(value)


def storm_key(properties: dict[str, Any]) -> str:
    return str(properties.get("idp_subset") or properties.get("stormid") or
               properties.get("atcfid") or properties.get("idp_source") or ":".join(str(properties.get(key, ""))
               for key in ("basin", "stormnum", "advnum", "advisnum")))


def mean_radius(properties: dict[str, Any], threshold: int) -> float:
    if number(properties.get("radii")) == threshold:
        values = [number(properties.get(quadrant))
                  for quadrant in ("ne", "se", "sw", "nw")]
    else:
        values = [number(properties.get(f"{quadrant}{threshold}"))
                  for quadrant in ("ne", "se", "sw", "nw")]
    values = [value for value in values if value is not None and value > 0]
    return round(sum(values) / len(values), 1) if values else 0.0


def normalize(points: dict[str, Any], radii: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    metadata: dict[str, dict[str, Any]] = {}
    for feature in points.get("features", []):
        properties = attrs(feature)
        coordinates = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coordinates) < 2:
            continue
        key = storm_key(properties)
        lead = number(properties.get("tau"))
        grouped[key].append({
            "lat": round(float(coordinates[1]), 4), "lon": round(float(coordinates[0]), 4),
            "lead_hours": int(lead if lead is not None else number(properties.get("fcstprd")) or 0),
            "valid_utc": timestamp(properties.get("validtime")),
            "vmax_kt": number(properties.get("maxwind")),
            "pmin_mb": number(properties.get("mslp")),
            "stage": properties.get("stormtype") or properties.get("dvlbl"),
        })
        metadata.setdefault(key, properties)

    radius_features: dict[str, list[dict[str, Any]]] = defaultdict(list)
    radii_by_lead: dict[str, dict[int, dict[str, float]]] = defaultdict(dict)
    for feature in radii.get("features", []):
        properties = attrs(feature)
        key = storm_key(properties)
        lead = int(number(properties.get("tau")) or 0)
        radius_features[key].append(feature)
        radii_by_lead[key][lead] = {"r34_nm": mean_radius(properties, 34),
                                    "r50_nm": mean_radius(properties, 50),
                                    "r64_nm": mean_radius(properties, 64)}

    tracks = []
    for key, forecast_points in grouped.items():
        forecast_points.sort(key=lambda point: point["lead_hours"])
        for point in forecast_points:
            point.update(radii_by_lead[key].get(point["lead_hours"],
                                                 {"r34_nm": 0.0, "r50_nm": 0.0, "r64_nm": 0.0}))
        properties = metadata[key]
        tracks.append({
            "available": True, "source": "NOAA NHC Tropical Weather Summary MapServer",
            "source_layers": {"forecast_points": 5, "forecast_wind_radii": 15},
            "classification": "Official NHC tropical cyclone advisory",
            "storm_id": key, "name": properties.get("stormname") or key,
            "basin": properties.get("basin"),
            "advisory_number": properties.get("advisnum") or properties.get("advnum"),
            "advisory_issued_utc": timestamp(properties.get("advdate")),
            "current_index": next((i for i, point in enumerate(forecast_points)
                                   if point["lead_hours"] == 0), 0),
            "points": forecast_points,
            "wind_radii_geojson": {"type": "FeatureCollection", "features": radius_features[key]},
        })
    return {"available": bool(tracks), "source": "NOAA NHC Tropical Weather Summary MapServer",
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(), "tracks": tracks}


def fetch_layer(layer: int) -> dict[str, Any]:
    query = urlencode({"where": "1=1", "outFields": "*", "returnGeometry": "true", "f": "geojson"})
    request = Request(f"{BASE}/{layer}/query?{query}", headers={"User-Agent": "Weather2Grid/0.1"})
    with urlopen(request, timeout=30) as response:  # nosec B310 - fixed NOAA endpoint
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = normalize(fetch_layer(5), fetch_layer(15))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"NHC active tracks: {len(payload['tracks'])} -> {args.output}")


if __name__ == "__main__":
    main()
