from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts.export_products import export_archive


def test_export_archive_builds_static_contract(tmp_path: Path) -> None:
    archive = tmp_path / "products"
    cycle = archive / "20260101T0000Z"
    cycle.mkdir(parents=True)
    (archive / "counties.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    (archive / "basemap.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    (cycle / "cycle.json").write_text(json.dumps({
        "cycle_id": cycle.name,
        "event_id": "event-1",
        "event_name": "Synthetic test",
        "forecast_init_time_utc": "2026-01-01T00:00:00+00:00",
        "synthetic": True,
        "release_gate_passed": False,
    }))
    pd.DataFrame([{
        "county_fips": "1001",
        "county_name": "Autauga",
        "state": "AL",
        "expected_customers_out": 10.0,
        "p90_customers_out": 20.0,
        "prob_outage_fraction_gt_05": 0.4,
        "peak_gust_ms": 30.0,
        "q50_outage_fraction": 0.08,
    }]).to_parquet(cycle / "risk.parquet")

    output = tmp_path / "site-data"
    summaries = export_archive(archive, output)

    assert [summary["cycle_id"] for summary in summaries] == [cycle.name]
    assert json.loads((output / "status.json").read_text())["any_synthetic"] is True
    counties = json.loads((output / "cycles" / cycle.name / "counties.json").read_text())
    assert counties[0]["county_fips"] == "01001"
    assert (output / "cycles" / cycle.name / "counties.geojson").exists()
    assert json.loads((output / "cycles" / cycle.name / "track.json").read_text())["available"] is False
