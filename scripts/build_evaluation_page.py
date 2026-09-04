#!/usr/bin/env python3
"""Build site/evaluation.html from a StormGrid hindcast evaluation bundle.

The white paper is GENERATED, not hand-written, so every number on the public
page comes from the evaluation artifacts and can be traced back to their
content hashes. Charts are inline SVG computed here: the page carries no
plotting library, no fetch, and renders identically offline and in print.

The narrative prose lives in this file. Numbers interpolate, but a claim like
"the published model is the least calibrated of the three" is an INTERPRETATION
of one particular result set -- re-read the prose against the numbers when you
regenerate the page from a different evaluation run.

    python3 scripts/build_evaluation_page.py \
        --evaluation ../weather2grid-archive/evaluation/prism-37922961 \
        --output site/evaluation.html
"""
from __future__ import annotations

import argparse
import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ARCHIVE_URL = "https://afahadabdullah.github.io/weather2grid-archive/"

# Validated categorical palette, in this order, for both themes: OKLCH
# lightness inside each mode's band, chroma above the floor, worst adjacent
# CVD deltaE 23.4 and normal-vision deltaE 30.6, all three above 3:1 on the
# light (#ffffff) and dark (#101c27) chart surfaces. Re-run the check before
# reordering or substituting a hue.
SERIES = {
    "baseline_a": "#0284c7",
    "baseline_b": "#d97706",
    "baseline_bplus": "#7c3aed",
}
MODEL_LABEL = {
    "baseline_a": "baseline A",
    "baseline_b": "baseline B",
    "baseline_bplus": "baseline B+",
}
STORM_LABEL = {
    "MATTHEW_2016": "Matthew 2016", "HARVEY_2017": "Harvey 2017",
    "IRMA_2017": "Irma 2017", "FLORENCE_2018": "Florence 2018",
    "MICHAEL_2018": "Michael 2018", "ISAIAS_2020": "Isaias 2020",
    "LAURA_2020": "Laura 2020", "IDA_2021": "Ida 2021",
    "IAN_2022": "Ian 2022", "IDALIA_2023": "Idalia 2023",
}


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def f(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.{digits}f}"


def pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------
# SVG primitives
# --------------------------------------------------------------------------

def bar(x: float, y0: float, y1: float, width: float, radius: float = 4.0) -> str:
    """A column with a rounded data-end and a square baseline end."""
    height = abs(y1 - y0)
    r = min(radius, width / 2, height)
    if height <= 0.5:
        return ""
    if y1 < y0:  # grows upward
        return (f"M{x:.2f},{y0:.2f} V{y1 + r:.2f} Q{x:.2f},{y1:.2f} {x + r:.2f},{y1:.2f} "
                f"H{x + width - r:.2f} Q{x + width:.2f},{y1:.2f} {x + width:.2f},{y1 + r:.2f} "
                f"V{y0:.2f} Z")
    return (f"M{x:.2f},{y0:.2f} V{y1 - r:.2f} Q{x:.2f},{y1:.2f} {x + r:.2f},{y1:.2f} "
            f"H{x + width - r:.2f} Q{x + width:.2f},{y1:.2f} {x + width:.2f},{y1 - r:.2f} "
            f"V{y0:.2f} Z")


def legend(models: list[str]) -> str:
    items = "".join(
        f'<span class="wp-key"><i style="background:{SERIES[m]}"></i>{e(MODEL_LABEL[m])}</span>'
        for m in models)
    return f'<div class="wp-legend">{items}</div>'


def grouped_columns(rows: list[str], series: list[str],
                    values: dict[tuple[str, str], float],
                    lo: float, hi: float, ticks: list[float],
                    tick_fmt, reference: tuple[float, str] | None,
                    title: str, y_label: str, value_fmt) -> str:
    """One column per (row, series), grouped by row, on a shared scale."""
    width, height = 880.0, 330.0
    left, right, top, bottom = 58.0, 18.0, 18.0, 74.0
    plot_w = width - left - right
    plot_h = height - top - bottom
    band = plot_w / len(rows)
    bar_w = min(18.0, (band - 22.0) / len(series))
    gap = 2.0
    group_w = bar_w * len(series) + gap * (len(series) - 1)

    def y_of(value: float) -> float:
        return top + plot_h - (value - lo) / (hi - lo) * plot_h

    parts = [f'<svg class="chart" viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
             f'aria-label="{e(title)}"><title>{e(title)}</title>']
    for tick in ticks:
        y = y_of(tick)
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.2f}" x2="{left + plot_w:.2f}" y2="{y:.2f}"/>')
        parts.append(f'<text class="tick" x="{left - 10}" y="{y + 3.5:.2f}" text-anchor="end">{e(tick_fmt(tick))}</text>')
    baseline = y_of(max(lo, min(hi, 0.0)))
    parts.append(f'<line class="axis" x1="{left}" y1="{baseline:.2f}" x2="{left + plot_w:.2f}" y2="{baseline:.2f}"/>')
    if reference is not None:
        value, label = reference
        y = y_of(value)
        parts.append(f'<line class="reference" x1="{left}" y1="{y:.2f}" x2="{left + plot_w:.2f}" y2="{y:.2f}"/>')
        parts.append(f'<text class="reference-label" x="{left + plot_w:.2f}" y="{y - 7:.2f}" text-anchor="end">{e(label)}</text>')

    for index, row in enumerate(rows):
        cx = left + band * (index + 0.5)
        for slot, model in enumerate(series):
            value = values.get((row, model))
            if value is None:
                continue
            x = cx - group_w / 2 + slot * (bar_w + gap)
            path = bar(x, baseline, y_of(max(lo, min(hi, value))), bar_w)
            if path:
                parts.append(
                    f'<path d="{path}" fill="{SERIES[model]}"><title>'
                    f'{e(STORM_LABEL.get(row, row))} · {e(MODEL_LABEL[model])}: '
                    f'{e(value_fmt(value))}</title></path>')
        label = STORM_LABEL.get(row, row)
        name, _, year = label.rpartition(" ")
        parts.append(f'<text class="tick" x="{cx:.2f}" y="{top + plot_h + 20:.2f}" text-anchor="middle">{e(name)}</text>')
        parts.append(f'<text class="tick muted" x="{cx:.2f}" y="{top + plot_h + 34:.2f}" text-anchor="middle">{e(year)}</text>')

    parts.append(f'<text class="axis-label" x="{left - 44}" y="{top + plot_h / 2:.2f}" '
                 f'transform="rotate(-90 {left - 44} {top + plot_h / 2:.2f})" text-anchor="middle">{e(y_label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def interval_chart(models: list[str], summary: dict[str, dict[str, Any]],
                   climatology: float) -> str:
    """Primary CRPS per model with its storm-bootstrap 95% interval."""
    width, height = 880.0, 182.0
    left, right, top, bottom = 108.0, 24.0, 30.0, 46.0
    plot_w = width - left - right
    lo, hi = 0.10, 0.24

    def x_of(value: float) -> float:
        return left + (value - lo) / (hi - lo) * plot_w

    parts = [f'<svg class="chart" viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
             'aria-label="Primary CRPS by model with 95% bootstrap interval">'
             '<title>Primary CRPS by model with 95% bootstrap interval</title>']
    for tick in [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24]:
        x = x_of(tick)
        parts.append(f'<line class="grid" x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + 96:.2f}"/>')
        parts.append(f'<text class="tick" x="{x:.2f}" y="{top + 116:.2f}" text-anchor="middle">{f(tick, 2)}</text>')
    cx = x_of(climatology)
    parts.append(f'<line class="reference" x1="{cx:.2f}" y1="{top - 6:.2f}" x2="{cx:.2f}" y2="{top + 96:.2f}"/>')
    parts.append(f'<text class="reference-label" x="{cx - 8:.2f}" y="{top - 12:.2f}" text-anchor="end">climatology {f(climatology)}</text>')

    for index, model in enumerate(models):
        row = summary[model]
        y = top + 22 + index * 30
        x_lo, x_hi = x_of(float(row["primary_crps_ci_lo"])), x_of(float(row["primary_crps_ci_hi"]))
        x_mid = x_of(float(row["primary_crps"]))
        colour = SERIES[model]
        parts.append(f'<text class="row-label" x="{left - 14}" y="{y + 4:.2f}" text-anchor="end">{e(MODEL_LABEL[model])}</text>')
        parts.append(f'<line x1="{x_lo:.2f}" y1="{y:.2f}" x2="{x_hi:.2f}" y2="{y:.2f}" stroke="{colour}" stroke-width="2" stroke-linecap="round" opacity="0.45"/>')
        for x in (x_lo, x_hi):
            parts.append(f'<line x1="{x:.2f}" y1="{y - 5:.2f}" x2="{x:.2f}" y2="{y + 5:.2f}" stroke="{colour}" stroke-width="2" opacity="0.45"/>')
        parts.append(
            f'<circle cx="{x_mid:.2f}" cy="{y:.2f}" r="5.5" fill="{colour}" class="ring">'
            f'<title>{e(MODEL_LABEL[model])}: CRPS {f(row["primary_crps"])} '
            f'(95% CI {f(row["primary_crps_ci_lo"])}–{f(row["primary_crps_ci_hi"])})</title></circle>')
        parts.append(f'<text class="value" x="{x_mid:.2f}" y="{y - 12:.2f}" text-anchor="middle">{f(row["primary_crps"])}</text>')
    parts.append(f'<text class="axis-label" x="{left + plot_w / 2:.2f}" y="{height - 8:.2f}" text-anchor="middle">CRPS (outage fraction) — lower is better</text>')
    parts.append("</svg>")
    return "".join(parts)


def reliability_chart(models: list[str], curves: dict[str, list[dict[str, Any]]]) -> str:
    width, height = 560.0, 440.0
    left, right, top, bottom = 62.0, 22.0, 22.0, 58.0
    plot = min(width - left - right, height - top - bottom)

    def x_of(v: float) -> float:
        return left + v * plot

    def y_of(v: float) -> float:
        return top + plot - v * plot

    parts = [f'<svg class="chart" viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
             'aria-label="Reliability of the probability that a county loses more than 5% of customers">'
             '<title>Reliability diagram</title>']
    for tick in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
        parts.append(f'<line class="grid" x1="{left}" y1="{y_of(tick):.2f}" x2="{left + plot:.2f}" y2="{y_of(tick):.2f}"/>')
        parts.append(f'<text class="tick" x="{left - 10}" y="{y_of(tick) + 3.5:.2f}" text-anchor="end">{f(tick, 1)}</text>')
        parts.append(f'<text class="tick" x="{x_of(tick):.2f}" y="{top + plot + 20:.2f}" text-anchor="middle">{f(tick, 1)}</text>')
    parts.append(f'<line class="reference dashed" x1="{x_of(0):.2f}" y1="{y_of(0):.2f}" x2="{x_of(1):.2f}" y2="{y_of(1):.2f}"/>')
    parts.append(f'<text class="reference-label" x="{x_of(0.60):.2f}" y="{y_of(0.47):.2f}" text-anchor="start">perfect reliability</text>')
    for model in models:
        points = curves[model]
        colour = SERIES[model]
        path = " ".join(f'{x_of(p["mean_forecast"]):.2f},{y_of(p["observed_frequency"]):.2f}' for p in points)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{colour}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        for point in points:
            parts.append(
                f'<circle cx="{x_of(point["mean_forecast"]):.2f}" cy="{y_of(point["observed_frequency"]):.2f}" '
                f'r="4.5" fill="{colour}" class="ring"><title>{e(MODEL_LABEL[model])}: forecast '
                f'{pct(point["mean_forecast"])}, observed {pct(point["observed_frequency"])} '
                f'({point["n"]} counties)</title></circle>')
    parts.append(f'<text class="axis-label" x="{left + plot / 2:.2f}" y="{height - 16:.2f}" text-anchor="middle">Forecast probability</text>')
    parts.append(f'<text class="axis-label" x="{left - 46}" y="{top + plot / 2:.2f}" transform="rotate(-90 {left - 46} {top + plot / 2:.2f})" text-anchor="middle">Observed frequency</text>')
    parts.append("</svg>")
    return "".join(parts)


CSS = """
:root { --paper-max: 900px; }
body.paper-body { min-height: 100vh; background: var(--bg-app); }
.paper-body .mast { position: sticky; top: 0; z-index: 20; }
main.paper {
  max-width: var(--paper-max);
  margin: 0 auto;
  padding: 40px clamp(18px, 4vw, 32px) 72px;
  font-size: 15px;
  line-height: 1.62;
  color: var(--ink-primary);
}
.paper p { margin: 0 0 16px; color: var(--ink-primary); }
.paper p.lede { font-size: 17px; line-height: 1.6; color: var(--ink-primary); }
.paper h1 { font-size: clamp(26px, 3.4vw, 34px); line-height: 1.2; margin: 6px 0 14px; }
.paper h2 {
  font-size: 19px; font-weight: 700; letter-spacing: -0.01em;
  margin: 44px 0 12px; padding-top: 18px;
  border-top: 1px solid var(--border-subtle);
}
.paper h3 {
  font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--ink-muted); margin: 28px 0 8px;
}
.paper a { color: var(--color-cyan); text-decoration: none; border-bottom: 1px solid var(--border-strong); }
.paper a:hover { border-bottom-color: var(--color-cyan); }
.paper strong { font-weight: 650; }
.paper code, .mono { font-family: var(--font-mono); font-size: 0.88em; }
.paper code { background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 1px 5px; }
.doc-meta {
  display: flex; flex-wrap: wrap; gap: 6px 18px;
  font-family: var(--font-mono); font-size: 11.5px; color: var(--ink-muted);
  margin: 0 0 30px; padding-bottom: 20px; border-bottom: 1px solid var(--border-subtle);
}
.wp-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 26px 0 34px; }
.wp-kpi { background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 14px 16px; }
.wp-kpi .wp-kpi-value { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.15; }
.wp-kpi .wp-kpi-label { display: block; margin-top: 6px; font-size: 11px; color: var(--ink-muted); letter-spacing: 0.04em; text-transform: uppercase; }
.wp-kpi .wp-kpi-note { display: block; margin-top: 4px; font-size: 12px; color: var(--ink-secondary); }
figure { margin: 26px 0 30px; background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: 18px 18px 14px; }
figure.split { display: grid; grid-template-columns: minmax(320px, 1.05fr) minmax(240px, 1fr); gap: 20px; align-items: start; }
figure .fig-body { min-width: 0; }
figcaption { margin-top: 12px; font-size: 12.5px; line-height: 1.55; color: var(--ink-secondary); }
figcaption b { color: var(--ink-primary); font-weight: 650; }
svg.chart { display: block; width: 100%; height: auto; overflow: visible; }
svg.chart .grid { stroke: var(--border-subtle); stroke-width: 1; }
svg.chart .axis { stroke: var(--border-strong); stroke-width: 1; }
svg.chart .reference { stroke: var(--ink-muted); stroke-width: 1; opacity: 0.75; }
svg.chart .reference.dashed { stroke-dasharray: 5 4; }
svg.chart .reference-label, svg.chart .tick, svg.chart .axis-label, svg.chart .row-label, svg.chart .value {
  font-family: var(--font-sans); fill: var(--ink-muted);
}
svg.chart .tick { font-size: 11px; }
svg.chart .tick.muted { font-size: 10px; opacity: 0.7; }
svg.chart .reference-label { font-size: 10.5px; letter-spacing: 0.04em; }
svg.chart .axis-label { font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }
svg.chart .row-label { font-size: 12.5px; fill: var(--ink-secondary); }
svg.chart .value { font-size: 11.5px; fill: var(--ink-secondary); font-family: var(--font-mono); }
svg.chart .ring { stroke: var(--bg-card); stroke-width: 2; }
.wp-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 2px 0 10px; font-size: 12px; color: var(--ink-secondary); }
.wp-legend .wp-key { display: inline-flex; align-items: center; gap: 7px; }
.wp-legend .wp-key i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.table-wrap { overflow-x: auto; margin: 20px 0 26px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
caption { text-align: left; padding: 12px 14px 10px; font-size: 12.5px; color: var(--ink-secondary); border-bottom: 1px solid var(--border-subtle); }
caption b { color: var(--ink-primary); font-weight: 650; }
th, td { padding: 8px 12px; text-align: right; border-bottom: 1px solid var(--border-subtle); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
thead th { font-size: 10.5px; letter-spacing: 0.07em; text-transform: uppercase; color: var(--ink-muted); font-weight: 700; background: var(--bg-subtle); }
tbody tr:last-child td { border-bottom: none; }
tbody tr.is-published { background: var(--bg-card-hover); }
td.num, th.num { font-family: var(--font-mono); }
td .swatch { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 8px; vertical-align: baseline; }
.callout { border: 1px solid var(--border-subtle); border-left: 3px solid var(--color-amber); background: var(--bg-card); border-radius: var(--radius-md); padding: 14px 18px; margin: 24px 0; }
.callout p:last-child { margin-bottom: 0; }
.callout .callout-title { display: block; font-size: 10.5px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--color-amber); font-weight: 750; margin-bottom: 6px; }
.paper ul, .paper ol { margin: 0 0 18px; padding-left: 22px; }
.paper li { margin-bottom: 9px; }
.paper li::marker { color: var(--ink-muted); }
dl.facts { display: grid; grid-template-columns: minmax(150px, auto) 1fr; gap: 8px 18px; margin: 0 0 20px; font-size: 13px; }
dl.facts dt { color: var(--ink-muted); }
dl.facts dd { margin: 0; font-family: var(--font-mono); font-size: 12px; word-break: break-all; color: var(--ink-secondary); }
.foot.paper-foot { max-width: var(--paper-max); margin: 0 auto; }
@media (max-width: 760px) {
  figure.split { grid-template-columns: 1fr; }
  main.paper { font-size: 14.5px; }
}
@media print {
  .mast, .paper-foot { display: none; }
  main.paper { max-width: none; padding: 0; }
  figure, .table-wrap, .wp-kpi, .callout { break-inside: avoid; }
}
"""

HEAD_SCRIPT = """
(function () {
  try {
    var stored = localStorage.getItem('w2g_theme');
    document.documentElement.setAttribute('data-theme', stored === 'dark' ? 'dark' : 'light');
  } catch (error) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
"""

FOOT_SCRIPT = """
(function () {
  var button = document.getElementById('theme-toggle');
  if (!button) return;
  button.addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('w2g_theme', next); } catch (error) { /* private mode */ }
  });
})();
"""


def masthead(subtitle: str) -> str:
    return f"""<header class="mast">
  <div class="mast-left">
    <a class="brand" href="https://afahadabdullah.github.io/weather2grid/" aria-label="Weather2Grid home">
      <span class="mark" aria-hidden="true"><i></i></span>
      <div class="brand-text">
        <strong>Weather2Grid</strong>
        <small>{e(subtitle)}</small>
      </div>
    </a>
  </div>
  <div class="mast-actions">
    <nav class="view-switch" aria-label="Site sections">
      <a class="view-link" href="https://afahadabdullah.github.io/weather2grid/">
        <span class="view-dot" aria-hidden="true"></span>
        Live forecast
      </a>
      <a class="view-link" href="{ARCHIVE_URL}">
        <span aria-hidden="true">&#9727;</span>
        Archive
      </a>
      <a class="view-link" href="evaluation.html" aria-current="page">
        <span aria-hidden="true">&#9636;</span>
        Evaluation
      </a>
    </nav>
    <button type="button" id="theme-toggle" class="theme-toggle" aria-label="Toggle display theme"
            title="Switch between Command Center Dark and Corporate Office Light mode">
      <span class="theme-icon" aria-hidden="true">◐</span>
      <span class="theme-text">Theme</span>
    </button>
  </div>
</header>"""


def table(caption_html: str, headers: list[tuple[str, bool]],
          rows: list[list[str]], highlight: int | None = None) -> str:
    head = "".join(f'<th class="{"num" if numeric else ""}" scope="col">{label}</th>'
                   for label, numeric in headers)
    body = []
    for index, row in enumerate(rows):
        cells = "".join(
            f'<td class="{"num" if headers[column][1] else ""}">{value}</td>'
            for column, value in enumerate(row))
        klass = ' class="is-published"' if highlight is not None and index == highlight else ""
        body.append(f"<tr{klass}>{cells}</tr>")
    return (f'<div class="table-wrap"><table><caption>{caption_html}</caption>'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>")


def build(run: Path) -> str:
    manifest = json.loads((run / "evaluation_manifest.json").read_text(encoding="utf-8"))
    summary_rows = {row["model_kind"]: row for row in read_csv(run / "model_summary.csv")}
    matrix = read_csv(run / "evaluation_matrix.csv")
    identity = read_csv(run / "event_identity_map.csv")
    scorecards = {
        model: json.loads((run / "models" / model / "scorecard.json").read_text(encoding="utf-8"))
        for model in manifest["models"]
    }
    cycles = []
    staging = run / str(manifest.get("dashboard", {}).get("staging_root", "dashboard"))
    for path in sorted(staging.glob("*/cycle.json")):
        cycles.append(json.loads(path.read_text(encoding="utf-8")))

    models = list(manifest["models"])
    published = str(manifest["dashboard_model"])
    storms = list(manifest["requested_storms"])
    cell = {(row["event_id"], row["model_kind"]): row for row in matrix}
    pooled = {model: scorecards[model]["pooled"] for model in models}
    climatology = float(summary_rows[published]["primary_crps_climatology"])
    counties_scored = int(pooled[published]["counties"])
    fitset = manifest["fitset"]
    generated = str(manifest["generated_utc"])[:19].replace("T", " ")
    best = min(models, key=lambda m: float(summary_rows[m]["primary_crps"]))
    artifacts = f"{ARCHIVE_URL}evaluation/{run.name}/"

    # ---- figures ----------------------------------------------------------
    crpss_values = {(storm, model): float(cell[(storm, model)]["crpss"])
                    for storm in storms for model in models}
    fig_crpss = grouped_columns(
        storms, models, crpss_values, -0.20, 0.50,
        [-0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5], lambda t: f(t, 1), None,
        "Continuous ranked probability skill score by storm and model",
        "CRPSS vs climatology", lambda v: f(v, 3))

    coverage_values = {(storm, model): float(cell[(storm, model)]["p90_coverage"])
                       for storm in storms for model in models}
    fig_coverage = grouped_columns(
        storms, models, coverage_values, 0.0, 1.0,
        [0.0, 0.25, 0.5, 0.75, 1.0], lambda t: f(t, 2),
        (0.9, "nominal 90%"),
        "Share of counties whose observed outage fell at or below the 90th-percentile forecast",
        "90% interval coverage", lambda v: pct(v))

    fig_interval = interval_chart(models, summary_rows, climatology)
    fig_reliability = reliability_chart(
        models, {model: scorecards[model]["reliability"] for model in models})

    # ---- tables -----------------------------------------------------------
    model_table = table(
        "<b>Table 1.</b> Model comparison. The primary score is the county CRPS averaged "
        "within a storm and then across storms, so a 318-county storm and a 66-county storm "
        "count the same. The interval is a 2,000-draw bootstrap over storms. PIT&#8211;KS and "
        "coverage are pooled county diagnostics.",
        [("Model", False), ("CRPS", True), ("95% interval", True), ("CRPSS", True),
         ("PIT&#8211;KS", True), ("90% coverage", True), ("Bias", True),
         ("MAE", True), ("RMSE", True)],
        [[f'<span class="swatch" style="background:{SERIES[model]}"></span>{e(MODEL_LABEL[model])}'
          + (" <small>(published)</small>" if model == published else ""),
          f(summary_rows[model]["primary_crps"]),
          f'{f(summary_rows[model]["primary_crps_ci_lo"])}&#8211;{f(summary_rows[model]["primary_crps_ci_hi"])}',
          f(summary_rows[model]["primary_crpss"]),
          f(pooled[model]["pit_ks"]),
          pct(pooled[model]["p90_coverage"]),
          f(summary_rows[model]["mean_storm_bias_outage_fraction"]),
          f(summary_rows[model]["mean_storm_mae_outage_fraction"]),
          f(summary_rows[model]["mean_storm_rmse_outage_fraction"])]
         for model in models],
        highlight=models.index(published))

    storm_rows = []
    for storm in storms:
        row = cell[(storm, published)]
        ranked = min(models, key=lambda m: float(cell[(storm, m)]["crps"]))
        storm_rows.append([
            e(STORM_LABEL.get(storm, storm)),
            f'{int(row["counties"]):,}',
            f(row["crps_climatology"]),
            *[f(cell[(storm, model)]["crps"]) for model in models],
            f'<span class="swatch" style="background:{SERIES[ranked]}"></span>{e(MODEL_LABEL[ranked])}',
        ])
    crps_table = table(
        "<b>Table 2.</b> County CRPS by storm, one column per model, against the "
        "climatological reference built from the same event pool. Lower is better; the "
        "climatology column is the number each model has to beat for that storm.",
        [("Storm", False), ("Counties", True), ("Climatology", True)]
        + [(e(MODEL_LABEL[model]), True) for model in models] + [("Lowest CRPS", False)],
        storm_rows)

    detail_rows = []
    for storm in storms:
        row = cell[(storm, published)]
        detail_rows.append([
            e(STORM_LABEL.get(storm, storm)),
            f(row["crpss"]),
            f(row["pit_ks"]),
            pct(row["p90_coverage"]),
            f(row["observed_mean_outage_fraction"]),
            f(row["predicted_mean_outage_fraction"]),
            f(row["mean_bias_outage_fraction"]),
            f(row["mae_outage_fraction"]),
            f(row["rmse_outage_fraction"]),
        ])
    detail_table = table(
        f"<b>Table 3.</b> Per-storm detail for <b>{e(MODEL_LABEL[published])}</b>, the model "
        "whose hindcasts are published to the archive dashboard. Outage fractions are "
        "customer-weighted county means; bias is predicted minus observed.",
        [("Storm", False), ("CRPSS", True), ("PIT&#8211;KS", True), ("90% coverage", True),
         ("Observed", True), ("Predicted", True), ("Bias", True), ("MAE", True), ("RMSE", True)],
        detail_rows)

    identity_table = table(
        "<b>Table 4.</b> Event identity. Every scored storm was matched to a HURDAT2 best "
        "track before scoring, so a storm's outage events, its hazard field and its name all "
        "refer to the same system. Distance is the closest approach between the outage event "
        "and the best track; the radius is the match tolerance that event was allowed.",
        [("Storm", False), ("HURDAT2", False), ("Status", False), ("Outage events", True),
         ("Track distance", True), ("Match radius", True)],
        [[e(STORM_LABEL.get(row["event_id"], row["event_id"])),
          f'<span class="mono">{e(row["hurdat_storm_id"])}</span>',
          e(row["status"]),
          e(row["source_events"]),
          f'{float(row["minimum_track_distance_km"]):.1f} km',
          f'{float(row["match_radius_km"]):.0f} km']
         for row in sorted(identity, key=lambda r: storms.index(r["event_id"]))])

    cycle_rows = []
    for cycle in sorted(cycles, key=lambda c: str(c["cycle_id"])):
        verification = cycle.get("verification") or {}
        cycle_rows.append([
            e(str(cycle.get("event_name", "")).replace(" — hindcast", "")),
            f'<span class="mono">{e(cycle["cycle_id"])}</span>',
            f'{int(verification.get("counties", 0)):,}',
            e(str(cycle.get("valid_start_utc", ""))[:10]),
            e(str(cycle.get("valid_end_utc", ""))[:10]),
            f(verification.get("crpss")),
        ])
    cycle_table = table(
        "<b>Table 5.</b> The ten hindcast cycles published to the archive dashboard. Each "
        "carries the full predictive distribution per county plus the observed outcome, so "
        "the dashboard can draw the observed and error layers a live forecast cannot have.",
        [("Storm", False), ("Cycle id", False), ("Counties", True),
         ("Window start", False), ("Window end", False), ("CRPSS", True)],
        cycle_rows)

    kpis = f"""<div class="wp-kpis">
  <div class="wp-kpi"><div class="wp-kpi-value">{len(storms)}</div><span class="wp-kpi-label">Storms scored</span><span class="wp-kpi-note">2016&#8211;2023, all matched to HURDAT2</span></div>
  <div class="wp-kpi"><div class="wp-kpi-value">{counties_scored:,}</div><span class="wp-kpi-label">County&#8211;events</span><span class="wp-kpi-note">each with an observed outage fraction</span></div>
  <div class="wp-kpi"><div class="wp-kpi-value">{f(summary_rows[published]["primary_crpss"], 2)}</div><span class="wp-kpi-label">CRPSS, published model</span><span class="wp-kpi-note">skill over climatology, 0 = no better</span></div>
  <div class="wp-kpi"><div class="wp-kpi-value">{pct(pooled[published]["p90_coverage"], 0)}</div><span class="wp-kpi-label">90% interval coverage</span><span class="wp-kpi-note">nominal is 90% &#8212; intervals are too narrow</span></div>
</div>"""

    wins = sum(1 for storm in storms
               if min(models, key=lambda m: float(cell[(storm, m)]["crps"])) == published)
    lasts = sum(1 for storm in storms
                if max(models, key=lambda m: float(cell[(storm, m)]["crps"])) == published)
    worst_coverage = sum(
        1 for storm in storms
        if min(models, key=lambda m: float(cell[(storm, m)]["p90_coverage"])) == published)
    worst_coverage_text = ("every one of the ten" if worst_coverage == len(storms)
                           else f"{worst_coverage} of the ten")
    negative = [storm for storm in storms if float(cell[(storm, published)]["crpss"]) < 0]
    worst_bias = min(storms, key=lambda s: float(cell[(s, published)]["mean_bias_outage_fraction"]))
    worst_bias_row = cell[(worst_bias, published)]
    calibrated = min(models, key=lambda m: float(pooled[m]["pit_ks"]))
    published_label = MODEL_LABEL[published]
    negative_text = ", ".join(STORM_LABEL.get(s, s) for s in negative) or "none"

    body = f"""<main class="paper">
<span class="eyebrow">Weather2Grid technical note</span>
<h1>Hindcast evaluation of the county outage-risk model</h1>
<div class="doc-meta">
  <span>Evaluation run <b class="mono">{e(run.name)}</b></span>
  <span>Generated {e(generated)} UTC</span>
  <span>Code version {e(manifest["code_version"])}</span>
  <span>Hazard basis: analysed</span>
</div>

<p class="lede">Ten United States landfalling tropical cyclones between 2016 and 2023 were
replayed through three candidate impact models, each storm withheld from the training pool
before its own outages were predicted. Every model beats climatology by roughly a quarter of
a CRPS, none of the three is distinguishable from the others on ten storms, and all three
under-predict how many customers actually lose power. The model whose hindcasts are published
to the dashboard, <b>{e(published_label)}</b>, produces the sharpest point predictions in the
set and the worst-calibrated intervals.</p>

{kpis}

<h2>1. What this measures &#8212; and what it deliberately does not</h2>
<p>The scores below were computed on the <b>analysed hazard</b>: each county's wind field comes
from the reanalysed record of what the storm actually did, not from a forecast of it. The
evaluation therefore isolates the impact model &#8212; the step that turns gust and duration
into a distribution over the fraction of customers out &#8212; and excludes every source of
forecast error ahead of it. A live Weather2Grid forecast inherits both, so the skill reported
here is an <em>upper bound</em> on end-to-end forecast skill, never a substitute for it.</p>
<p>Each storm was scored under leave-one-event-out replay: the storm being predicted was
removed from the fitting pool, so a model never sees the event it is asked to forecast. That
is why every scorecard row shows a training pool of about {int(float(cell[(storms[0], published)]["train_events"])):,}
events out of {fitset["evaluation_events"]:,} rather than the whole set.</p>

<div class="callout">
  <span class="callout-title">Status of the underlying product</span>
  <p>These hindcasts run in verification mode with the release gate not passed and the
  degraded-mode flag set, the same status the live dashboard reports. The evaluation is
  research output about a research product; nothing here makes it operational guidance.</p>
</div>

<h2>2. Data and event identity</h2>
<p>The fitting set holds {fitset["source_rows"]:,} county&#8211;event rows over
{fitset["source_events"]:,} outage events; {fitset["evaluation_rows"]:,} rows and
{fitset["evaluation_events"]:,} events survive the evaluation filter. Scoring covers
{counties_scored:,} county&#8211;events across the ten storms, each with an observed outage
fraction to score against.</p>
<p>Storm identity is resolved before any scoring: an outage event carries its own opaque
identifier, so it has to be matched to a named best track or the results cannot be attributed
to a storm at all. Every requested storm matched a HURDAT2 track, four of them across two
outage events that belong to the same system. The closest approach between event and track
ranged from {min(float(r["minimum_track_distance_km"]) for r in identity):.1f} km to
{max(float(r["minimum_track_distance_km"]) for r in identity):.1f} km, comfortably inside each
event's match radius.</p>
{identity_table}

<h2>3. Scoring protocol</h2>
<p>The primary metric is the continuous ranked probability score of the predicted outage
fraction, averaged over the counties of a storm and then over storms with equal weight. Equal
storm weight is the point: pooling counties instead would let Michael's 318 counties outvote
Harvey's 66 and turn the headline number into a statement about geography rather than about
the model. Pooled county diagnostics are reported alongside, and treated as diagnostics.</p>
<ul>
  <li><b>Reference.</b> CRPSS compares each model against a climatological distribution built
  from the same event pool. Zero means no better than climatology; negative means worse.</li>
  <li><b>Uncertainty.</b> A 2,000-draw bootstrap resamples <em>storms</em>, not counties.
  Counties within a storm share a weather field and are nowhere near independent, so a county
  bootstrap would report an interval several times too narrow.</li>
  <li><b>Calibration.</b> The PIT&#8211;KS statistic measures how far the probability integral
  transform of the observations departs from uniform; 90% interval coverage is the share of
  counties whose observed outage fell at or below the 90th-percentile forecast. A
  well-calibrated model returns about 0.90.</li>
</ul>

<h2>4. Skill: all three models beat climatology, none beats the others</h2>
<figure>
  {legend(models)}
  {fig_interval}
  <figcaption><b>Figure 1.</b> Primary CRPS with its 95% bootstrap interval over storms.
  The three intervals overlap almost completely: with ten storms, a separation of
  {abs(float(summary_rows[best]["primary_crps"]) - float(summary_rows[published]["primary_crps"])):.4f}
  CRPS between the best and the published model is far inside the noise. Choosing between these
  models on the strength of the headline number is not supported by this evidence.</figcaption>
</figure>
{model_table}
<p>Skill against climatology is consistent across the set: {e(MODEL_LABEL[best])} scores
{f(summary_rows[best]["primary_crpss"])} and {e(published_label)} scores
{f(summary_rows[published]["primary_crpss"])}, both meaningful improvements over a
climatological forecast, neither distinguishable from the other. Per storm the ordering
churns &#8212; {e(published_label)} takes the lowest CRPS in {wins} of the ten storms and the
highest in the other {lasts}, which is what a set of models with no real separation looks
like.</p>
<figure>
  {legend(models)}
  {fig_crpss}
  <figcaption><b>Figure 2.</b> Skill over climatology by storm. Bars above zero beat the
  climatological reference for that storm. {e(STORM_LABEL.get(negative[0], negative[0])) if negative else "No storm"}
  is the exception: every model scores worse than climatology there. Its observed outages were
  the mildest in the set (mean fraction
  {f(cell[(negative[0], published)]["observed_mean_outage_fraction"]) if negative else "&#8212;"}),
  and the climatological distribution was already close to right.</figcaption>
</figure>
{crps_table}

<h2>5. Calibration: sharp, and overconfident</h2>
<p>Skill and calibration part company here. {e(published_label)} carries the narrowest
predictive distributions in the set &#8212; the lowest mean absolute error
({f(summary_rows[published]["mean_storm_mae_outage_fraction"])}) and the lowest RMSE
({f(summary_rows[published]["mean_storm_rmse_outage_fraction"])}) of the three &#8212; but its
intervals do not cover the outcome. Only {pct(pooled[published]["p90_coverage"], 0)} of counties
fall at or below their 90th-percentile forecast, against a nominal 90%, and its PIT&#8211;KS
statistic ({f(pooled[published]["pit_ks"])}) is more than double
{e(MODEL_LABEL[calibrated])}'s ({f(pooled[calibrated]["pit_ks"])}). The intervals it publishes
are, in plain terms, too narrow to be believed.</p>
<figure class="split">
  <div class="fig-body">
    {legend(models)}
    {fig_reliability}
  </div>
  <figcaption><b>Figure 3.</b> Reliability of the forecast probability that a county loses
  more than 5% of its customers. A perfectly reliable model sits on the diagonal. All three
  models sit above it &#8212; events happen more often than they are forecast &#8212; and
  {e(published_label)}'s curve departs furthest, especially in the low-probability bins where
  it assigns near-zero probability to counties that lost power roughly a third of the
  time.</figcaption>
</figure>
<figure>
  {legend(models)}
  {fig_coverage}
  <figcaption><b>Figure 4.</b> 90% interval coverage by storm against the nominal 90% line.
  Every storm falls short for every model, and {e(published_label)} falls furthest short in
  {worst_coverage_text}. Coverage this far below nominal means an operator reading the p90 column
  as a reasonable worst case is reading something considerably milder than that.</figcaption>
</figure>

<h2>6. Bias: the models under-predict, consistently</h2>
<p>Averaged over storms, every model predicts a smaller outage fraction than was observed:
{f(summary_rows[published]["mean_storm_bias_outage_fraction"])} for {e(published_label)},
{f(summary_rows[calibrated]["mean_storm_bias_outage_fraction"])} for
{e(MODEL_LABEL[calibrated])}. Pooled over counties the gap is starker still &#8212; a mean
observed outage fraction of {f(pooled[published]["mean_observed_outage_fraction"])} against a
mean prediction of {f(pooled[published]["mean_predicted_outage_fraction"])}. The single worst
case is {e(STORM_LABEL.get(worst_bias, worst_bias))}, where the published model predicted a
mean outage fraction of {f(worst_bias_row["predicted_mean_outage_fraction"])} against
{f(worst_bias_row["observed_mean_outage_fraction"])} observed.</p>
<p>Under-prediction and narrow intervals compound rather than cancel: a distribution centred
too low <em>and</em> too tight is wrong in the direction that matters most for staging crews.
That is the first thing to fix, and it is measurable &#8212; both diagnostics on this page are
the test that a fix has to move.</p>
{detail_table}

<h2>7. What this evaluation does not establish</h2>
<ul>
  <li><b>It is not forecast skill.</b> Scoring against the analysed hazard removes weather
  forecast error entirely. The corresponding forecast-basis evaluation has not been run.</li>
  <li><b>Ten storms is a small sample.</b> The bootstrap interval on the headline score spans
  roughly {f(float(summary_rows[published]["primary_crps_ci_hi"]) - float(summary_rows[published]["primary_crps_ci_lo"]), 2)}
  CRPS. Differences between these models cannot be resolved at this sample size, and neither
  can a modest real improvement.</li>
  <li><b>All ten are tropical cyclones.</b> Nothing here speaks to winter storms, derechos or
  ice, which the live product also runs on.</li>
  <li><b>Coverage is county-level, not customer-level.</b> A county is one unit whatever its
  customer base, which is the right call for scoring the model and the wrong one for reading
  off expected system-wide impact.</li>
  <li><b>The observed outage record is a data product too</b>, with its own reporting gaps.
  Counties absent from the record are absent from the score.</li>
</ul>

<h2>8. The published hindcasts</h2>
<p>All ten {e(published_label)} hindcasts are published to the
<a href="{ARCHIVE_URL}">Weather2Grid archive dashboard</a>. Open the run picker and choose a
storm from the <em>Hindcast verification</em> group: each one carries the full predictive
distribution per county alongside the observed outcome, so the observed and error map layers
&#8212; which a live forecast cannot have, because its outcome has not happened yet &#8212;
are available for every county in the storm.</p>
{cycle_table}

<h2>9. Reproducibility</h2>
<p>Every number on this page is generated from the evaluation bundle named below by
<code>scripts/build_evaluation_page.py</code>; the hindcast payloads on the archive dashboard
are written from the same bundle by <code>scripts/publish_hindcast_evaluation.py</code>.
Neither page is edited by hand. The bundle itself is published alongside the hindcasts, so
every table here can be recomputed from the artifacts it links.</p>
<dl class="facts">
  <dt>Evaluation run</dt><dd>{e(run.name)}</dd>
  <dt>Generated (UTC)</dt><dd>{e(manifest["generated_utc"])}</dd>
  <dt>Code version</dt><dd>{e(manifest["code_version"])}</dd>
  <dt>Fitset</dt><dd>{e(fitset["path"])}</dd>
  <dt>Fitset SHA-256</dt><dd>{e(fitset["sha256"])}</dd>
  <dt>Evaluation content hash</dt><dd>{e(fitset["evaluation_content_hash"])}</dd>
  <dt>Best-track source</dt><dd>{e(Path(manifest["event_identity"]["hurdat"]["path"]).name)}</dd>
  <dt>Best-track SHA-256</dt><dd>{e(manifest["event_identity"]["hurdat"]["sha256"])}</dd>
  <dt>Manifest</dt><dd><a href="{artifacts}evaluation_manifest.json">evaluation_manifest.json</a></dd>
  <dt>Scorecards</dt><dd>{" &#183; ".join(f'<a href="{artifacts}models/{model}/scorecard.json">{model}</a>' for model in models)}</dd>
  <dt>Score matrix</dt><dd><a href="{artifacts}evaluation_matrix.csv">evaluation_matrix.csv</a> &#183; <a href="{artifacts}model_summary.csv">model_summary.csv</a> &#183; <a href="{artifacts}event_identity_map.csv">event_identity_map.csv</a></dd>
</dl>
</main>"""

    return f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Hindcast evaluation of the Weather2Grid county outage-risk model: ten landfalling tropical cyclones, three candidate impact models, leave-one-event-out replay.">
<title>Weather2Grid &#8212; Hindcast evaluation of the county outage-risk model</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='9' fill='%231d4e6f'/%3E%3Ccircle cx='16' cy='16' r='5' fill='%23fff'/%3E%3C/svg%3E">
<link rel="stylesheet" href="assets/app.css">
<style>{CSS}</style>
<script>{HEAD_SCRIPT}</script>
</head>
<body class="paper-body">
{masthead("Hindcast evaluation")}
{body}
<footer class="foot paper-foot">
  <span>Weather2Grid &#183; Probabilistic Weather-to-Power-Grid Decision Intelligence</span>
  <span>Research evaluation &#183; not operational guidance. Verify with official National Weather Service / NHC directives for life safety.</span>
</footer>
<script>{FOOT_SCRIPT}</script>
</body>
</html>
"""


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evaluation", type=Path, required=True,
                        help="Evaluation run directory (holds evaluation_manifest.json)")
    parser.add_argument("--output", type=Path, default=root / "site" / "evaluation.html",
                        help="Where to write the white paper")
    args = parser.parse_args()

    page = build(args.evaluation.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(f"Wrote {args.output} ({len(page.encode('utf-8')):,} bytes) at "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
