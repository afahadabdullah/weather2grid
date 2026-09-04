from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.export_products import export_archive, publish_geometry


def test_geometry_hash_uses_compact_served_content(tmp_path: Path) -> None:
    first = tmp_path / "first.geojson"
    second = tmp_path / "second.geojson"
    first.write_text('{"type": "FeatureCollection", "features": []}')
    second.write_text('{\n  "type":"FeatureCollection",\n  "features":[]\n}')
    staging = tmp_path / "site-data"

    first_path = publish_geometry(first, staging)
    second_path = publish_geometry(second, staging)

    assert first_path == second_path
    assert len(list((staging / "geometries").glob("*.geojson"))) == 1


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
    assert counties["format"] == "w2g-columnar-v1"
    assert summaries[0]["county_data_format"] == "w2g-columnar-v1"
    fips_index = counties["columns"].index("county_fips")
    assert counties["rows"][0][fips_index] == "01001"
    summary = json.loads((output / "cycles.json").read_text())[0]
    assert (output / summary["geometry_path"]).exists()
    assert not (output / "cycles" / cycle.name / "counties.geojson").exists()
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


def test_export_preserves_independently_refreshed_weathernext_tracks(tmp_path: Path) -> None:
    archive = _init_archive(tmp_path / "products", [0])
    output = tmp_path / "site-data"
    output.mkdir()
    tracks = '{"available":true,"tracks":[{"name":"Test"}]}\n'
    (output / "weathernext-active-tracks.json").write_text(tracks)

    export_archive(archive, output)

    assert (output / "weathernext-active-tracks.json").read_text() == tracks


def test_export_preserves_an_enriched_cycle_track(tmp_path: Path) -> None:
    archive = _init_archive(tmp_path / "products", [0])
    output = tmp_path / "site-data"
    summaries = export_archive(archive, output)
    cycle_id = summaries[0]["cycle_id"]
    track_path = output / "cycles" / cycle_id / "track.json"
    enriched = {"available": True, "points": [{"lat": 30, "lon": -80}]}
    track_path.write_text(json.dumps(enriched))

    export_archive(archive, output)

    assert json.loads(track_path.read_text()) == enriched


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


def test_export_archive_merge_retains_the_older_initialization(tmp_path: Path) -> None:
    """Older initializations become the archive the picker offers, instead of
    being deleted. Only the newest is flagged as latest."""
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

    assert [s["cycle_id"] for s in summaries] == ["20260101T0600Z", "20260101T0000Z"]
    latest = {s["cycle_id"]: s["is_latest_initialization"] for s in summaries}
    assert latest == {"20260101T0600Z": True, "20260101T0000Z": False}
    status = json.loads((output / "status.json").read_text())
    assert status["cycles"] == 2
    assert status["cycles_latest_initialization"] == 1
    assert status["initializations"] == 2
    assert status["latest"]["cycle_id"] == "20260101T0600Z"
    # Both are still fetchable; the older one is history, not a deletion.
    assert (output / "cycles" / "20260101T0000Z" / "counties.json").exists()
    assert (output / "cycles" / "20260101T0600Z" / "counties.json").exists()
    index = json.loads((output / "initializations.json").read_text())
    assert [entry["issued_utc"] for entry in index] == [
        "2026-01-01T06:00:00+00:00", "2026-01-01T00:00:00+00:00"]
    assert [entry["is_latest_initialization"] for entry in index] == [True, False]
    assert [entry["cycles"] for entry in index] == [1, 1]


def _init_archive(root: Path, hours: list[int], windows: int = 1) -> Path:
    """Build an archive holding `windows` cycles for each initialization hour."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "counties.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}')
    for hour in hours:
        for window in range(windows):
            name = f"202601{1 + hour // 24:02d}T{hour % 24:02d}00Z_wn2x-w{window}"
            cycle = root / name
            cycle.mkdir()
            (cycle / "cycle.json").write_text(json.dumps({
                "cycle_id": name,
                "event_id": f"wn2x-w{window}",
                "forecast_init_time_utc":
                    f"2026-01-{1 + hour // 24:02d}T{hour % 24:02d}:00:00+00:00",
                "valid_start_utc":
                    f"2026-01-{1 + hour // 24:02d}T{hour % 24:02d}:00:00+00:00",
                "hazard_source": "weathernext2_100m_wind_proxy",
                "synthetic": False,
                "release_gate_passed": False,
            }))
            pd.DataFrame([{
                "county_fips": "01001", "county_name": "Autauga", "state": "AL",
                "expected_customers_out": 10.0, "p90_customers_out": 20.0,
                "prob_outage_fraction_gt_05": 0.4, "peak_gust_ms": 30.0,
            }]).to_parquet(cycle / "risk.parquet")
    return root


def test_retention_evicts_only_beyond_the_keep_limit(tmp_path: Path) -> None:
    archive = _init_archive(tmp_path / "products", [0, 6, 12, 18, 24])
    output = tmp_path / "site-data"

    summaries = export_archive(archive, output, keep_initializations=3)

    kept = [s["cycle_id"] for s in summaries]
    assert len(kept) == 3
    # Newest three initializations survive; the two oldest are evicted from
    # the published site entirely, directory and all.
    assert kept[0].startswith("20260102T0000Z")
    assert not (output / "cycles" / "20260101T0000Z_wn2x-w0").exists()
    assert not (output / "cycles" / "20260101T0600Z_wn2x-w0").exists()
    assert (output / "cycles" / "20260101T1200Z_wn2x-w0").exists()
    assert sum(s["is_latest_initialization"] for s in summaries) == 1


def test_every_window_of_a_retained_initialization_survives(tmp_path: Path) -> None:
    """A partly-evicted initialization would animate with silent gaps, so
    retention is per initialization and never per cycle."""
    archive = _init_archive(tmp_path / "products", [0, 6], windows=4)
    summaries = export_archive(archive, tmp_path / "site-data",
                               keep_initializations=2)
    assert len(summaries) == 8
    index = json.loads(
        (tmp_path / "site-data" / "initializations.json").read_text())
    assert [entry["cycles"] for entry in index] == [4, 4]


def test_summaries_lead_with_the_nearest_window_of_the_newest_run(tmp_path: Path) -> None:
    """status.json 'latest' must be the nearest frame of the current run, not
    the seven-day frame the old cycle_id sort happened to put first."""
    archive = _init_archive(tmp_path / "products", [0], windows=4)
    summaries = export_archive(archive, tmp_path / "site-data")
    status = json.loads((tmp_path / "site-data" / "status.json").read_text())
    assert status["latest"]["cycle_id"] == summaries[0]["cycle_id"]
    assert status["latest"]["is_latest_initialization"] is True


def test_an_archived_synthetic_run_cannot_flip_the_live_banner(tmp_path: Path) -> None:
    """Banner state describes the CURRENT forecast. A retained synthetic run
    from last week must not make today's real product claim synthetic."""
    archive = _init_archive(tmp_path / "products", [0, 6])
    old_meta = archive / "20260101T0000Z_wn2x-w0" / "cycle.json"
    payload = json.loads(old_meta.read_text())
    payload["synthetic"] = True
    old_meta.write_text(json.dumps(payload))

    export_archive(archive, tmp_path / "site-data")
    status = json.loads((tmp_path / "site-data" / "status.json").read_text())

    assert status["any_synthetic"] is False
    assert status["banner"]["title"] == "REAL-TIME FORECAST — EXPERIMENTAL"


def test_rolling_window_geometry_reaches_the_dashboard_contract(tmp_path: Path) -> None:
    """A 24 h window stepped every 6 h must arrive at the browser labelled as
    overlapping. Without these three fields the dashboard falls back to a
    hardcoded window length and silently presents successive views of one
    forecast as if they were separate events."""
    archive = tmp_path / "products"
    (archive).mkdir(parents=True)
    (archive / "counties.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    for end_hour in (24, 30):
        cycle = archive / f"20260101T0000Z_wn2x-conus-h{end_hour:03d}"
        cycle.mkdir()
        (cycle / "cycle.json").write_text(json.dumps({
            "cycle_id": cycle.name,
            "event_id": f"wn2x-conus-h{end_hour:03d}",
            "event_name": f"CONUS wind outlook — 24 h to +{end_hour} h (rolling)",
            "forecast_init_time_utc": "2026-01-01T00:00:00+00:00",
            "valid_start_utc": f"2026-01-01T{end_hour - 18:02d}:00:00+00:00",
            "valid_end_utc": f"2026-01-0{1 + end_hour // 24}T{end_hour % 24:02d}:00:00+00:00",
            "forecast_provider": "Google DeepMind WeatherNext 2 via BigQuery",
            "hazard_source": "weathernext2_100m_wind_proxy",
            "input_lead_hours": list(range(end_hour - 18, end_hour + 1, 6)),
            "lead_step_hours": 6.0,
            "window_hours": 24.0,
            "step_hours": 6.0,
            "windows_overlap": True,
            "synthetic": False,
            "release_gate_passed": False,
            "degraded_mode": True,
        }))
        pd.DataFrame([{
            "county_fips": "01001", "county_name": "Autauga", "state": "AL",
            "expected_customers_out": 10.0, "p90_customers_out": 20.0,
            "prob_outage_fraction_gt_05": 0.4, "peak_gust_ms": 30.0,
        }]).to_parquet(cycle / "risk.parquet")

    summaries = export_archive(archive, tmp_path / "site-data")

    # Both windows share one initialization, so neither is discarded as older.
    assert len(summaries) == 2
    for summary in summaries:
        assert summary["product_window_hours"] == 24.0
        assert summary["product_step_hours"] == 6.0
        assert summary["windows_overlap"] is True


def test_a_product_without_window_geometry_keeps_the_old_contract(tmp_path: Path) -> None:
    """HRRR cycles carry no window fields; they must export as before rather
    than acquiring a fabricated window shape."""
    archive = tmp_path / "products"
    cycle = archive / "20260101T0000Z_hrrr"
    cycle.mkdir(parents=True)
    (archive / "counties.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    (cycle / "cycle.json").write_text(json.dumps({
        "cycle_id": cycle.name,
        "event_id": "hrrr-conus",
        "forecast_init_time_utc": "2026-01-01T00:00:00+00:00",
        "valid_start_utc": "2026-01-01T00:00:00+00:00",
        "valid_end_utc": "2026-01-01T18:00:00+00:00",
        "hazard_source": "hrrr_forecast_calibrated_county_centroid",
        "release_gate_passed": False,
    }))
    pd.DataFrame([{
        "county_fips": "01001", "county_name": "Autauga", "state": "AL",
        "expected_customers_out": 10.0, "p90_customers_out": 20.0,
        "prob_outage_fraction_gt_05": 0.4, "peak_gust_ms": 30.0,
    }]).to_parquet(cycle / "risk.parquet")

    summaries = export_archive(archive, tmp_path / "site-data")
    assert summaries[0]["product_window_hours"] is None
    assert summaries[0]["product_step_hours"] is None
    assert summaries[0]["windows_overlap"] is False


def test_archived_payloads_move_out_of_the_site_repository(tmp_path: Path) -> None:
    """The whole point of the split: the site tree carries exactly one
    initialization no matter how many are retained."""
    archive = _init_archive(tmp_path / "products", [0, 6, 12], windows=2)
    output = tmp_path / "site-data"
    store = tmp_path / "archive-repo"

    summaries = export_archive(
        archive, output, keep_initializations=3, archive_output=store,
        archive_base_url="https://example.github.io/w2g-archive/")

    assert len(summaries) == 6
    latest = [s for s in summaries if s["is_latest_initialization"]]
    older = [s for s in summaries if not s["is_latest_initialization"]]
    assert len(latest) == 2 and len(older) == 4

    # Only the current run ships inside the site.
    on_site = sorted(p.name for p in (output / "cycles").iterdir())
    assert on_site == sorted(s["cycle_id"] for s in latest)
    in_store = sorted(p.name for p in (store / "cycles").iterdir())
    assert in_store == sorted(s["cycle_id"] for s in older)

    # The live index lists only current runs. The archive has its own index and
    # full dashboard shell, and archived entries say where to fetch payloads.
    assert all(s.get("data_base") is None for s in latest)
    assert {s["data_base"] for s in older} == {"https://example.github.io/w2g-archive"}
    assert json.loads((output / "cycles.json").read_text()) == latest
    archived_index = json.loads((store / "data" / "cycles.json").read_text())
    assert {s["cycle_id"] for s in archived_index} == {
        s["cycle_id"] for s in older}
    assert all(not s["is_latest_initialization"] for s in archived_index)
    assert json.loads((store / "data" / "status.json").read_text())["archive_view"] is True
    assert (store / "index.html").is_file()
    assert (store / "assets" / "app.js").is_file()
    for summary in older:
        assert (store / "cycles" / summary["cycle_id"] / "counties.json").is_file()

    # Each dashboard carries only the geometry needed by its own index.
    assert (output / "geometries").is_dir()
    for summary in latest:
        assert (output / summary["geometry_path"]).is_file()
    for summary in older:
        assert (store / "data" / summary["geometry_path"]).is_file()

    status = json.loads((output / "status.json").read_text())
    assert status["archive_base_url"] == "https://example.github.io/w2g-archive/"


def test_the_archive_prunes_runs_that_retention_dropped(tmp_path: Path) -> None:
    """An archive nobody links to would otherwise grow forever."""
    store = tmp_path / "archive-repo"
    output = tmp_path / "site-data"
    url = "https://example.github.io/w2g-archive"

    first = _init_archive(tmp_path / "p1", [0, 6, 12])
    export_archive(first, output, keep_initializations=3,
                   archive_output=store, archive_base_url=url)
    assert len(list((store / "cycles").iterdir())) == 2

    # A newer run arrives and the limit pushes the oldest out of the index.
    second = _init_archive(tmp_path / "p2", [0, 6, 12, 18])
    summaries = export_archive(second, output, keep_initializations=3,
                               archive_output=store, archive_base_url=url)

    kept = {s["cycle_id"] for s in summaries}
    stored = {p.name for p in (store / "cycles").iterdir()}
    assert stored <= kept, "archive holds a cycle the index no longer lists"
    assert stored == {s["cycle_id"] for s in summaries
                      if not s["is_latest_initialization"]}
    assert not (store / "cycles" / "20260101T0000Z_wn2x-w0").exists()


def test_archive_output_without_a_url_is_refused(tmp_path: Path) -> None:
    """A missing or relative base URL would 404 every archived run in the
    browser, so it fails at export instead of at a viewer's screen."""
    archive = _init_archive(tmp_path / "products", [0, 6])
    with pytest.raises(SystemExit, match="needs --archive-base-url"):
        export_archive(archive, tmp_path / "a", archive_output=tmp_path / "s")
    with pytest.raises(SystemExit, match="must be absolute"):
        export_archive(archive, tmp_path / "b", archive_output=tmp_path / "s",
                       archive_base_url="../w2g-archive")


def test_without_a_split_everything_still_ships_in_the_site(tmp_path: Path) -> None:
    """The split is opt-in; an operator with no archive repository is
    unaffected."""
    archive = _init_archive(tmp_path / "products", [0, 6])
    output = tmp_path / "site-data"
    summaries = export_archive(archive, output, keep_initializations=2)
    assert all(s.get("data_base") is None for s in summaries)
    assert len(list((output / "cycles").iterdir())) == 2
    assert json.loads((output / "status.json").read_text())["archive_base_url"] is None


def test_offload_current_leaves_the_site_with_no_cycle_payloads(tmp_path: Path) -> None:
    """The site repository then holds only code, indexes and geometry, so its
    history stops growing with each publish."""
    archive = _init_archive(tmp_path / "products", [0, 6], windows=2)
    output = tmp_path / "site-data"
    store = tmp_path / "archive-repo"
    url = "https://example.github.io/w2g-archive"

    summaries = export_archive(archive, output, keep_initializations=2,
                               archive_output=store, archive_base_url=url,
                               offload_current=True)

    assert not any((output / "cycles").iterdir())
    assert len(list((store / "cycles").iterdir())) == 4
    assert all(s["data_base"] == url for s in summaries)
    # The index and the shared geometry still ship with the site.
    assert (output / "cycles.json").is_file()
    assert (output / "initializations.json").is_file()
    assert (output / "geometries").is_dir()


def test_offload_current_still_prunes_dropped_runs(tmp_path: Path) -> None:
    store = tmp_path / "archive-repo"
    output = tmp_path / "site-data"
    url = "https://example.github.io/w2g-archive"
    export_archive(_init_archive(tmp_path / "p1", [0, 6]), output,
                   keep_initializations=2, archive_output=store,
                   archive_base_url=url, offload_current=True)
    summaries = export_archive(_init_archive(tmp_path / "p2", [0, 6, 12]), output,
                               keep_initializations=2, archive_output=store,
                               archive_base_url=url, offload_current=True)
    stored = {p.name for p in (store / "cycles").iterdir()}
    assert stored == {s["cycle_id"] for s in summaries}
    assert not (store / "cycles" / "20260101T0000Z_wn2x-w0").exists()


def _hindcast_archive(root: Path, storms: list[tuple[str, str]]) -> Path:
    """storms: (event_id, ISO event start)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "counties.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}')
    for event_id, start in storms:
        stamp = pd.Timestamp(start).strftime("%Y%m%dT%H%M")
        name = f"{stamp}Z_hindcast-{event_id}"
        cycle = root / name
        cycle.mkdir()
        (cycle / "cycle.json").write_text(json.dumps({
            "cycle_id": name,
            "event_id": f"hindcast-{event_id}",
            "event_name": f"{event_id} — hindcast",
            "forecast_init_time_utc": start,
            "valid_start_utc": start,
            "valid_end_utc": start,
            "hazard_source": "hindcast_analysed_hazard_impact_model_only",
            "forecast_provider": "Hindcast — analysed hazard, no forecast",
            "product_kind": "hindcast",
            "hazard_basis": "analysed_hazard_impact_model_only",
            "has_observed": True,
            "verification": {"storm": event_id, "crps": 0.03, "crpss": 0.4},
            "degraded_mode": True,
            "release_gate_passed": False,
            "synthetic": False,
        }))
        pd.DataFrame([{
            "county_fips": "01001", "county_name": "Autauga", "state": "AL",
            "customers_total": 100000.0,
            "expected_customers_out": 10.0, "p90_customers_out": 20.0,
            "prob_outage_fraction_gt_05": 0.4, "peak_gust_ms": 30.0,
            "observed_outage_fraction": 0.02,
            "observed_customers_out": 2000.0,
            "residual_outage_fraction": -0.01,
            "residual_customers_out": -1000.0,
        }]).to_parquet(cycle / "risk.parquet")
    return root


def test_a_hindcast_never_claims_to_be_a_latest_initialization(tmp_path: Path) -> None:
    """Each hindcast is the only run of its own series, so the naive rule would
    mark a 2016 storm 'latest' and the picker would offer it as current."""
    archive = _hindcast_archive(tmp_path / "products", [
        ("IDA_2021", "2021-08-29T00:00:00+00:00"),
        ("IAN_2022", "2022-09-28T00:00:00+00:00")])
    summaries = export_archive(archive, tmp_path / "site-data")
    assert len(summaries) == 2
    assert all(not s["is_latest_initialization"] for s in summaries)
    assert all(s["product_kind"] == "hindcast" for s in summaries)
    assert all(s["has_observed"] for s in summaries)


def test_hindcasts_and_forecasts_do_not_evict_each_other(tmp_path: Path) -> None:
    """They are different series. A new forecast must not push a hindcast out
    through retention, and a hindcast must not displace a forecast."""
    archive = _init_archive(tmp_path / "products", [0, 6, 12])
    _hindcast_archive(archive, [("IDA_2021", "2021-08-29T00:00:00+00:00")])

    summaries = export_archive(archive, tmp_path / "site-data",
                               keep_initializations=1)

    kinds = {s["cycle_id"]: s["product_kind"] for s in summaries}
    hindcasts = [c for c, k in kinds.items() if k == "hindcast"]
    forecasts = [c for c, k in kinds.items() if k == "forecast"]
    # keep_initializations=1 prunes the forecast series to its newest run only,
    # and leaves the hindcast alone.
    assert len(hindcasts) == 1
    assert len(forecasts) == 1
    assert forecasts[0].startswith("20260101T1200Z")


def test_an_archived_hindcast_cannot_set_the_live_banner(tmp_path: Path) -> None:
    """Banner state describes current guidance. A verification product is not
    guidance and must not participate in it."""
    archive = _init_archive(tmp_path / "products", [0])
    _hindcast_archive(archive, [("IDA_2021", "2021-08-29T00:00:00+00:00")])
    export_archive(archive, tmp_path / "site-data")
    status = json.loads((tmp_path / "site-data" / "status.json").read_text())
    assert status["latest"]["product_kind"] == "forecast"


def test_observed_columns_reach_the_browser_payload(tmp_path: Path) -> None:
    archive = _hindcast_archive(tmp_path / "products",
                                [("IDA_2021", "2021-08-29T00:00:00+00:00")])
    summaries = export_archive(archive, tmp_path / "site-data")
    payload = json.loads((tmp_path / "site-data" / "cycles"
                          / summaries[0]["cycle_id"] / "counties.json").read_text())
    assert {"observed_outage_fraction", "residual_outage_fraction",
            "observed_customers_out"} <= set(payload["columns"])
