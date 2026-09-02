from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

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
        "valid_start_utc": "2026-01-01T06:00:00+00:00",
        "valid_end_utc": "2026-01-01T18:00:00+00:00",
        "forecast_provider": "Test forecast provider",
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
        "training_event_exclusions": ["held-out-event"],
    }]).to_parquet(cycle / "risk.parquet")

    output = tmp_path / "site-data"
    summaries = export_archive(archive, output)

    assert [summary["cycle_id"] for summary in summaries] == [cycle.name]
    assert summaries[0]["forecast_provider"] == "Test forecast provider"
    assert summaries[0]["forecast_window_hours"] == 12.0
    assert summaries[0]["forecast_horizon_hours"] == 18.0
    assert summaries[0]["track_available"] is False
    status = json.loads((output / "status.json").read_text())
    assert status["any_synthetic"] is True
    counties = json.loads((output / "cycles" / cycle.name / "counties.json").read_text())
    assert counties[0]["training_event_exclusions"] == ["held-out-event"]
    assert counties[0]["county_fips"] == "01001"
    assert (output / "cycles" / cycle.name / "counties.geojson").exists()
    assert json.loads((output / "cycles" / cycle.name / "track.json").read_text())["available"] is False


def test_ungated_real_product_uses_experimental_banner() -> None:
    from scripts.export_products import banner

    value = banner(any_synthetic=False, any_ungated=True)
    assert value["title"] == "REAL-TIME FORECAST — EXPERIMENTAL"
    assert "not passed its release gate" in value["detail"]


def test_archive_without_basemap_keeps_existing_site_basemap(tmp_path: Path) -> None:
    # No stormgrid archive publishes basemap.geojson today - it is static
    # site chrome, not part of the product contract. A real export must not
    # blank out an already-good basemap just because the archive lacks one.
    archive = tmp_path / "products"
    cycle = archive / "20260101T0000Z"
    cycle.mkdir(parents=True)
    (archive / "counties.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    (cycle / "cycle.json").write_text(json.dumps({
        "cycle_id": cycle.name,
        "event_id": "event-1",
        "event_name": "Live test",
        "forecast_init_time_utc": "2026-01-01T00:00:00+00:00",
        "synthetic": False,
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
    output.mkdir()
    rich_basemap = '{"type":"FeatureCollection","features":[{"type":"Feature"}]}'
    (output / "basemap.geojson").write_text(rich_basemap)

    export_archive(archive, output)

    assert (output / "basemap.geojson").read_text() == rich_basemap


def test_failed_export_preserves_previous_snapshot(tmp_path: Path) -> None:
    archive = tmp_path / "bad-products"
    cycle = archive / "20260101T0000Z"
    cycle.mkdir(parents=True)
    (archive / "counties.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}')
    (cycle / "cycle.json").write_text(json.dumps({"cycle_id": cycle.name}))
    pd.DataFrame([{"county_fips": "01001"}]).to_parquet(
        cycle / "risk.parquet")
    output = tmp_path / "site-data"
    output.mkdir()
    (output / "last-good.txt").write_text("keep me")

    with pytest.raises(SystemExit, match="missing dashboard fields"):
        export_archive(archive, output)

    assert (output / "last-good.txt").read_text() == "keep me"
    assert not list(tmp_path.glob(".site-data.tmp-*"))


def test_export_archive_merge_preserves_existing_cycles(tmp_path: Path) -> None:
    # First export cycle 1
    archive1 = tmp_path / "archive1"
    cycle1 = archive1 / "20260101T0000Z"
    cycle1.mkdir(parents=True)
    (archive1 / "counties.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    (cycle1 / "cycle.json").write_text(json.dumps({
        "cycle_id": cycle1.name,
        "event_id": "event-1",
        "forecast_init_time_utc": "2026-01-01T00:00:00+00:00",
        "synthetic": False,
        "release_gate_passed": False,
    }))
    pd.DataFrame([{
        "county_fips": "01001",
        "county_name": "Autauga",
        "state": "AL",
        "expected_customers_out": 10.0,
        "p90_customers_out": 20.0,
        "prob_outage_fraction_gt_05": 0.4,
        "peak_gust_ms": 30.0,
    }]).to_parquet(cycle1 / "risk.parquet")

    output = tmp_path / "site-data"
    export_archive(archive1, output)

    # Now export cycle 2 with merge=True
    archive2 = tmp_path / "archive2"
    cycle2 = archive2 / "20260101T0600Z"
    cycle2.mkdir(parents=True)
    (archive2 / "counties.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    (cycle2 / "cycle.json").write_text(json.dumps({
        "cycle_id": cycle2.name,
        "event_id": "event-1",
        "forecast_init_time_utc": "2026-01-01T06:00:00+00:00",
        "synthetic": False,
        "release_gate_passed": False,
    }))
    pd.DataFrame([{
        "county_fips": "01001",
        "county_name": "Autauga",
        "state": "AL",
        "expected_customers_out": 15.0,
        "p90_customers_out": 25.0,
        "prob_outage_fraction_gt_05": 0.5,
        "peak_gust_ms": 35.0,
    }]).to_parquet(cycle2 / "risk.parquet")

    summaries = export_archive(archive2, output, merge=True)

    assert len(summaries) == 2
    assert [s["cycle_id"] for s in summaries] == ["20260101T0600Z", "20260101T0000Z"]
    status = json.loads((output / "status.json").read_text())
    assert status["cycles"] == 2
    assert status["latest"]["cycle_id"] == "20260101T0600Z"
    assert (output / "cycles" / "20260101T0000Z" / "counties.json").exists()
    assert (output / "cycles" / "20260101T0600Z" / "counties.json").exists()
