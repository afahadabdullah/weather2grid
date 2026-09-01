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


def test_committed_demo_has_required_entrypoints() -> None:
    assert (SITE / "data" / "status.json").exists()
    assert (SITE / "data" / "cycles.json").exists()
    assert (SITE / "data" / "basemap.geojson").exists()
