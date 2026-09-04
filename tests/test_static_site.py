import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


def test_site_uses_relative_assets_and_static_data() -> None:
    index = (SITE / "index.html").read_text()
    app = (SITE / "assets" / "app.js").read_text()

    assert 'href="assets/app.css"' in index
    assert 'src="assets/app.js"' in index
    assert "Weather2Grid" in index
    assert "/api/" not in app
    assert "data/status.json" in app
    assert "data/cycles.json" in app
    assert "weather2grid-archive/" in index
    assert "Live forecast" in index
    assert "Archive" in index
    assert 'id="run-picker"' in index
    assert "Archived runs" in index
    assert "function drawRunPicker" in app
    assert "function selectAvailableRun" in app


def test_committed_demo_has_required_entrypoints() -> None:
    assert (SITE / "data" / "status.json").exists()
    assert (SITE / "data" / "cycles.json").exists()
    assert (SITE / "data" / "basemap.geojson").exists()


def test_committed_static_payload_stays_within_pages_budget() -> None:
    total = sum(path.stat().st_size for path in (SITE / "data").rglob("*") if path.is_file())
    assert total < 50_000_000
    for counties in (SITE / "data" / "cycles").glob("*/counties.json"):
        assert counties.stat().st_size < 2_000_000
    summaries = json.loads((SITE / "data" / "cycles.json").read_text())
    referenced = {summary["geometry_path"] for summary in summaries}
    stored = {
        path.relative_to(SITE / "data").as_posix()
        for path in (SITE / "data" / "geometries").glob("*.geojson")
    }
    assert stored == referenced

def test_evaluation_white_paper_is_published_and_linked() -> None:
    index = (SITE / "index.html").read_text()
    paper = (SITE / "evaluation.html").read_text()

    assert 'href="evaluation.html"' in index
    assert "Evaluation" in index
    assert "Hindcast evaluation" in paper
    assert 'href="assets/app.css"' in paper
    # The white paper is a static document generated from the evaluation
    # bundle: it must not depend on a plotting library or fetch anything at
    # read time, or the numbers on a public page could change under it.
    assert "cdn" not in paper.lower()
    assert "fetch(" not in paper
    assert "<svg" in paper
