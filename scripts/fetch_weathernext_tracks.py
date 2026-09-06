#!/usr/bin/env python3
"""Ingest and generate Google WeatherNext Cyclones tracks for Weather2Grid.

Supports:
1. Parsing standard ATCF (.dat) cyclone track files from WeatherNext / Weather Lab.
2. Generating a fixed DEMONSTRATION track (invented, not forecast) for
   exercising the dashboard's storm overlay. Requires --allow-synthetic.
   Real live tracks come from `stormgrid fetch-weathernext3-track`, which
   detects a surface low in the same model run the outage forecast came from.
3. Exporting to site/data/weathernext-active-tracks.json and per-cycle track.json.

Every track this script writes is tagged with the forecast initialization it
belongs to (``forecast_init_time_utc``).  The dashboard pairs a cyclone track
to a county-outage cycle by requiring that tag to equal the cycle's
``issued_utc`` exactly, so a storm from one init can never be drawn over an
outage forecast from another.  Cycles whose stored track no longer matches
their own init are reset to an "unavailable" placeholder on every run.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def parse_atcf_lat(val: str) -> float:
    val = val.strip()
    hem = val[-1].upper()
    deg = float(val[:-1]) / 10.0
    return deg if hem == "N" else -deg


def parse_atcf_lon(val: str) -> float:
    val = val.strip()
    hem = val[-1].upper()
    deg = float(val[:-1]) / 10.0
    return -deg if hem == "W" else deg


def hazard_source_for(version: int) -> str:
    """StormGrid hazard_source string for a WeatherNext model version."""
    return f"weathernext{version}_100m_wind_proxy"


def forecast_provider_for(version: int) -> str:
    return f"Google DeepMind WeatherNext {version} via BigQuery"


def stamp_pairing(track: dict[str, Any], init_dt: datetime, version: int) -> dict[str, Any]:
    """Tag a track with the initialization the dashboard must pair it to.

    ``forecast_init_time_utc`` is the single pairing key: the dashboard only
    draws this track over a cycle whose ``issued_utc`` matches it.  The other
    fields make the pairing auditable in the published JSON.
    """
    track["init_time_utc"] = init_dt.isoformat()
    track["forecast_init_time_utc"] = init_dt.isoformat()
    track["advisory_issued_utc"] = init_dt.isoformat()
    track["hazard_source"] = hazard_source_for(version)
    track["forecast_provider"] = forecast_provider_for(version)
    track["model_version"] = version
    track["pairing_key"] = "forecast_init_time_utc"
    return track


def parse_atcf_file(path: Path) -> dict[str, Any]:
    """Parse standard ATCF (.dat) track lines."""
    tracks_by_id: dict[str, dict[str, Any]] = {}
    lines = path.read_text(encoding="utf-8").splitlines()

    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 11:
            continue
        basin = parts[0].upper()
        cy = parts[1]
        storm_id = f"{basin.lower()}{int(cy):02d}{parts[2][:4]}"
        init_str = parts[2]
        model = parts[4].upper()
        tau = int(parts[5])
        lat = parse_atcf_lat(parts[6])
        lon = parse_atcf_lon(parts[7])
        vmax = float(parts[8]) if parts[8].isdigit() else 0.0
        pmin = float(parts[9]) if parts[9].isdigit() else 9999.0
        stage = parts[10]

        if storm_id not in tracks_by_id:
            tracks_by_id[storm_id] = {
                "available": True,
                "source": "Google DeepMind WeatherNext Cyclones",
                "classification": "AI Tropical Cyclone Forecast (WeatherNext)",
                "storm_id": storm_id,
                "name": f"Cyclone {storm_id.upper()} (WeatherNext)",
                "basin": basin,
                "model": model,
                "init_time_utc": f"{init_str[:4]}-{init_str[4:6]}-{init_str[6:8]}T{init_str[8:10]}:00:00+00:00",
                "current_index": 0,
                "points": [],
            }

        tracks_by_id[storm_id]["points"].append({
            "lead_hours": tau,
            "lat": lat,
            "lon": lon,
            "vmax_kt": vmax,
            "pmin_mb": pmin,
            "stage": stage,
        })

    return {"available": bool(tracks_by_id), "tracks": list(tracks_by_id.values())}


def parse_iso_or_date(val: str) -> datetime:
    val = val.strip()
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    if "T" in val or ":" in val:
        dt = datetime.fromisoformat(val)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    # Plain date: YYYY-MM-DD or YYYYMMDD
    clean_date = val.replace("-", "")
    if len(clean_date) == 8 and clean_date.isdigit():
        return datetime(int(clean_date[:4]), int(clean_date[4:6]), int(clean_date[6:8]), tzinfo=timezone.utc)
    dt = datetime.fromisoformat(val)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_weathernext_init(init_arg: str, site_data_dir: Path, version: int = 3) -> tuple[datetime, str]:
    """Resolve WeatherNext initialization datetime and cycle matching pattern."""
    tag = f"_wn{version}"
    source_prefix = f"weathernext{version}"
    if init_arg.strip().lower() == "latest":
        # 1. Check initializations.json
        inits_file = site_data_dir / "initializations.json"
        if inits_file.is_file():
            try:
                items = json.loads(inits_file.read_text(encoding="utf-8"))
                wn_items = [item for item in items if str(item.get("hazard_source", "")).startswith(source_prefix)]
                if not wn_items and version == 2:
                    wn_items = [item for item in items if str(item.get("hazard_source", "")).startswith("weathernext")]
                if wn_items:
                    latest_item = next((item for item in wn_items if item.get("is_latest_initialization")), wn_items[0])
                    issued = latest_item.get("issued_utc")
                    if issued:
                        dt = parse_iso_or_date(issued)
                        return dt, dt.strftime("%Y%m%dT%H%MZ") + tag
            except Exception:
                pass

        # 2. Check cycles.json
        cycles_file = site_data_dir / "cycles.json"
        if cycles_file.is_file():
            try:
                cycles = json.loads(cycles_file.read_text(encoding="utf-8"))
                wn_cycles = [c for c in cycles if str(c.get("hazard_source", "")).startswith(source_prefix)]
                if not wn_cycles and version == 2:
                    wn_cycles = [c for c in cycles if str(c.get("hazard_source", "")).startswith("weathernext")]
                if wn_cycles:
                    issued = wn_cycles[0].get("issued_utc")
                    if issued:
                        dt = parse_iso_or_date(issued)
                        return dt, dt.strftime("%Y%m%dT%H%MZ") + tag
            except Exception:
                pass

        # 3. Check cycles directory
        cycles_dir = site_data_dir / "cycles"
        if cycles_dir.is_dir():
            wn_dirs = sorted(cycles_dir.glob(f"*{tag}*"), reverse=True)
            if wn_dirs:
                prefix = wn_dirs[0].name.split("_")[0]
                try:
                    dt = datetime.strptime(prefix, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
                    return dt, prefix + tag
                except Exception:
                    pass

        # Fallback
        if version == 3:
            dt = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        else:
            dt = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
        return dt, dt.strftime("%Y%m%dT%H%MZ") + tag

    # Explicit init string provided
    dt = parse_iso_or_date(init_arg)
    has_time = "T" in init_arg or ":" in init_arg
    if has_time:
        prefix = dt.strftime("%Y%m%dT%H%MZ") + tag
    else:
        cycles_dir = site_data_dir / "cycles"
        date_str = dt.strftime("%Y%m%d")
        matching = sorted(cycles_dir.glob(f"{date_str}T*{tag}*")) if cycles_dir.is_dir() else []
        if matching:
            cycle_prefix = matching[0].name.split("_")[0]
            try:
                dt = datetime.strptime(cycle_prefix, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
            except Exception:
                pass
            prefix = cycle_prefix + tag
        else:
            prefix = dt.strftime("%Y%m%dT0000Z") + tag
    return dt, prefix


def generate_weathernext_marie_track(init_dt: datetime | None = None, version: int = 2) -> dict[str, Any]:
    """Generate the WeatherNext ensemble track for Hurricane Marie (EP132026).
    
    Initialized with 6-hourly fixes matching the 25 rolling WeatherNext forecast windows (leads 6h to 168h).
    """
    raw_fixes = [
        # (lead_h, lat, lon, vmax_kt, pmin_hpa, r34_nm, r50_nm, r64_nm, stage)
        (6,   19.2, -114.9, 82.0, 974.0, 60.0, 35.0, 25.0, "HU"),
        (12,  19.4, -115.5, 85.0, 971.0, 65.0, 38.0, 26.0, "HU"),
        (18,  19.6, -116.1, 87.0, 969.0, 70.0, 40.0, 28.0, "HU"),
        (24,  19.8, -116.8, 89.0, 967.0, 72.0, 42.0, 30.0, "HU"),
        (30,  20.1, -117.5, 90.0, 966.0, 75.0, 44.0, 32.0, "HU"),
        (36,  20.4, -118.2, 91.0, 965.0, 75.0, 45.0, 32.0, "HU"),
        (42,  20.8, -118.9, 90.0, 966.0, 75.0, 44.0, 31.0, "HU"),
        (48,  21.1, -119.5, 88.0, 968.0, 70.0, 42.0, 29.0, "HU"),
        (54,  21.5, -120.1, 86.0, 970.0, 68.0, 40.0, 27.0, "HU"),
        (60,  21.9, -120.7, 83.0, 973.0, 65.0, 38.0, 25.0, "HU"),
        (66,  22.3, -121.3, 80.0, 976.0, 60.0, 35.0, 22.0, "HU"),
        (72,  22.5, -121.9, 77.0, 979.0, 58.0, 32.0, 18.0, "HU"),
        (78,  22.8, -122.5, 74.0, 982.0, 55.0, 28.0, 15.0, "HU"),
        (84,  23.0, -123.2, 70.0, 986.0, 50.0, 25.0, 10.0, "HU"),
        (90,  23.2, -123.9, 67.0, 990.0, 48.0, 22.0,  0.0, "HU"),
        (96,  23.4, -124.6, 63.0, 994.0, 45.0, 18.0,  0.0, "TS"),
        (102, 23.6, -125.4, 59.0, 998.0, 42.0, 15.0,  0.0, "TS"),
        (108, 23.7, -126.1, 55.0, 1001.0, 38.0,  0.0,  0.0, "TS"),
        (114, 23.8, -126.9, 50.0, 1004.0, 35.0,  0.0,  0.0, "TS"),
        (120, 24.0, -127.7, 46.0, 1007.0, 30.0,  0.0,  0.0, "TS"),
        (126, 24.1, -128.5, 42.0, 1009.0, 25.0,  0.0,  0.0, "TS"),
        (132, 24.2, -129.3, 38.0, 1011.0, 20.0,  0.0,  0.0, "TS"),
        (138, 24.3, -130.1, 35.0, 1012.0, 15.0,  0.0,  0.0, "TS"),
        (144, 24.4, -130.8, 30.0, 1014.0,  0.0,  0.0,  0.0, "LOW"),
        (150, 24.5, -131.5, 27.0, 1015.0,  0.0,  0.0,  0.0, "LOW"),
        (156, 24.6, -132.1, 25.0, 1016.0,  0.0,  0.0,  0.0, "LOW"),
        (162, 24.7, -132.7, 22.0, 1017.0,  0.0,  0.0,  0.0, "LOW"),
        (168, 24.8, -133.2, 20.0, 1018.0,  0.0,  0.0,  0.0, "LOW"),
    ]

    if init_dt is None:
        init_dt = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
    points = []
    for tau, lat, lon, vmax, pmin, r34, r50, r64, stage in raw_fixes:
        valid_dt = init_dt + timedelta(hours=tau)
        points.append({
            "lead_hours": tau,
            "valid_utc": valid_dt.strftime("%d/%H%M"),
            "valid_iso": valid_dt.isoformat(),
            "lat": round(lat, 2),
            "lon": round(lon, 2),
            "vmax_kt": vmax,
            "pmin_mb": pmin,
            "r34_nm": r34,
            "r50_nm": r50,
            "r64_nm": r64,
            "stage": stage,
        })

    model_label = f"WeatherNext {version} / Cyclones"
    source_label = f"Google DeepMind WeatherNext {version} Cyclones (AI Ensemble)"
    classification = f"AI Tropical Cyclone Track Forecast ({'0.1°' if version == 3 else '0.25°'})"

    return stamp_pairing({
        "available": True,
        "source": source_label,
        "classification": classification,
        "storm_id": "ep132026",
        "name": "Hurricane Marie (WeatherNext AI)",
        "basin": "EP",
        "model": model_label,
        "current_index": 0,
        "points": points,
    }, init_dt, version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=int, choices=[2, 3], default=3,
                        help="WeatherNext model version (2 or 3, default: 3)")
    parser.add_argument("--init", default="latest",
                        help="WeatherNext initialization (default: 'latest', or e.g. '2026-08-31', '2026-08-31T12:00:00Z')")
    parser.add_argument("--atcf", type=Path, help="Optional ATCF format track file")
    parser.add_argument("--output", type=Path, default=Path("site/data/weathernext-active-tracks.json"))
    parser.add_argument("--populate-cycles", action="store_true", default=True,
                        help="Populate individual cycle track.json files")
    parser.add_argument("--no-prune-stale", dest="prune_stale", action="store_false", default=True,
                        help="Keep per-cycle tracks whose init no longer matches their cycle "
                             "(default: reset them, so no cycle ever shows another init's storm)")
    parser.add_argument("--allow-unpaired", action="store_true",
                        help="Exit 0 even when the resolved init matches no published cycle")
    parser.add_argument("--allow-synthetic", action="store_true",
                        help="Permit the built-in demonstration track. Required "
                             "whenever --atcf is not given, because the built-in "
                             "track is invented, not forecast.")
    args = parser.parse_args()

    # The generator below produces a fixed, invented Hurricane Marie track. It
    # exists to exercise the dashboard's storm overlay, and it is fine for
    # that. It is not fine on a public site presenting real forecasts: a
    # fabricated hurricane, published beside genuine outage guidance and
    # labelled with a real initialization, is indistinguishable from a real
    # forecast to anyone reading the map. The live pipelines never call this
    # script - they use `stormgrid fetch-weathernext3-track`, which detects a
    # low from the same model fields the outage forecast came from.
    if not args.atcf and not args.allow_synthetic:
        raise SystemExit(
            "ERROR: without --atcf this script emits an INVENTED demonstration "
            "track (Hurricane Marie), not a forecast.\n"
            "  For a real WeatherNext track, run the modelling pipeline:\n"
            "    stormgrid fetch-weathernext3-track --init <init> --data-root <root>\n"
            "    (or scripts/run_weathernext3_live.sh, which does it for you)\n"
            "  To parse a real ATCF file:  --atcf <file.dat>\n"
            "  To publish the demo track anyway:  --allow-synthetic")

    site_data_dir = args.output.parent
    init_dt, cycle_prefix = resolve_weathernext_init(args.init, site_data_dir, version=args.version)
    print(f"Targeting WeatherNext {args.version} initialization: {init_dt.isoformat()} (pattern: {cycle_prefix}*)")

    if args.atcf and args.atcf.exists():
        data = parse_atcf_file(args.atcf)
        # An ATCF file carries its own init.  Trust the file, but refuse to
        # silently publish it against a different initialization than the one
        # the cycles were built from - that is exactly the pairing this script
        # exists to guarantee.
        kept = []
        for track in data.get("tracks", []):
            track_init = track.get("init_time_utc")
            if track_init and parse_iso_or_date(track_init) != init_dt:
                print(f"  ! skipping {track.get('storm_id')}: ATCF init {track_init} "
                      f"does not match target init {init_dt.isoformat()}")
                continue
            kept.append(stamp_pairing(track, init_dt, args.version))
        data["tracks"] = kept
        data["available"] = bool(kept)
    else:
        track = generate_weathernext_marie_track(init_dt, version=args.version)
        track["synthetic"] = True
        track["name"] = f"{track['name']} [DEMONSTRATION TRACK - NOT A FORECAST]"
        data = {"available": True, "synthetic": True, "tracks": [track]}
        print("WARNING: publishing the invented demonstration track "
              "(--allow-synthetic).")

    # Initialization provenance on the index itself, so a consumer can pair
    # without opening each track.
    data["source"] = f"Google DeepMind WeatherNext {args.version} Cyclones (AI Ensemble)"
    data["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
    data["forecast_init_time_utc"] = init_dt.isoformat()
    data["hazard_source"] = hazard_source_for(args.version)
    data["forecast_provider"] = forecast_provider_for(args.version)
    data["model_version"] = args.version
    data["pairing_key"] = "forecast_init_time_utc"

    paired_cycle_ids: list[str] = []
    cycles_dir = site_data_dir / "cycles"

    # ------------------------------------------------------------------
    # Pair the track into every cycle sharing this exact initialization.
    # ------------------------------------------------------------------
    if args.populate_cycles and data.get("tracks") and cycles_dir.exists():
        marie_track = data["tracks"][0]
        for cycle_dir in sorted(cycles_dir.glob(f"{cycle_prefix}*")):
            cycle_json_path = cycle_dir / "cycle.json"
            if not cycle_json_path.exists():
                continue
            try:
                cdata = json.loads(cycle_json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Could not read {cycle_dir}: {exc}")
                continue

            issued = cdata.get("issued_utc") or (cdata.get("meta") or {}).get("forecast_init_time_utc")
            if not issued or parse_iso_or_date(str(issued)) != init_dt:
                print(f"  ! {cycle_dir.name}: issued {issued} != track init "
                      f"{init_dt.isoformat()} - not pairing")
                continue

            try:
                lead_h = int(cdata.get("forecast_horizon_hours") or cdata.get("lead_hours") or 24)
                pts = marie_track["points"]
                best_idx = min(range(len(pts)), key=lambda i: abs(pts[i]["lead_hours"] - lead_h))

                cycle_track = dict(marie_track)
                cycle_track["current_index"] = best_idx
                cycle_track["cycle_id"] = cdata.get("cycle_id", cycle_dir.name)
                cycle_track["paired_cycle_issued_utc"] = str(issued)
                (cycle_dir / "track.json").write_text(
                    json.dumps(cycle_track, indent=2) + "\n", encoding="utf-8")

                cdata["track_available"] = True
                cdata["track_init_time_utc"] = init_dt.isoformat()
                cdata["track_init_matches_cycle"] = True
                cdata.pop("track_init_mismatch", None)
                cycle_json_path.write_text(json.dumps(cdata, indent=2) + "\n", encoding="utf-8")
                paired_cycle_ids.append(cdata.get("cycle_id", cycle_dir.name))
            except Exception as exc:
                print(f"Could not update {cycle_dir}: {exc}")

        print(f"Paired track into {len(paired_cycle_ids)} cycle(s) for init {init_dt.isoformat()}")

    # ------------------------------------------------------------------
    # Reset any cycle still holding a track from a different init.  Without
    # this a cycle keeps whichever storm it was last given, which is how an
    # outage forecast ends up under a storm from another run.
    # ------------------------------------------------------------------
    if args.prune_stale and cycles_dir.exists():
        pruned = 0
        for cycle_dir in sorted(cycles_dir.iterdir()):
            track_path = cycle_dir / "track.json"
            cycle_json_path = cycle_dir / "cycle.json"
            if not track_path.exists() or not cycle_json_path.exists():
                continue
            try:
                tdata = json.loads(track_path.read_text(encoding="utf-8"))
                cdata = json.loads(cycle_json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if tdata.get("available") is False:
                continue

            # Only clear tracks this script is responsible for. A cycle can
            # ship its own track inside its product bundle (the HRRR surface-low
            # detection, for example); that one belongs to its cycle by
            # construction and is not ours to second-guess.
            provenance = " ".join(str(tdata.get(k, "")) for k in
                                  ("source", "model", "hazard_source", "classification"))
            if "weathernext" not in provenance.lower():
                continue

            track_init = tdata.get("forecast_init_time_utc") or tdata.get("init_time_utc")
            issued = cdata.get("issued_utc")
            matched = False
            if track_init and issued:
                try:
                    matched = parse_iso_or_date(str(track_init)) == parse_iso_or_date(str(issued))
                except Exception:
                    matched = False
            if matched:
                continue

            write_unavailable_track(
                track_path,
                reason=(f"Track init {track_init or 'unknown'} does not match this cycle's "
                        f"initialization {issued or 'unknown'}; a storm forecast is only shown "
                        f"alongside the outage forecast from the same init."),
                cycle_id=cdata.get("cycle_id", cycle_dir.name),
                cycle_issued_utc=str(issued) if issued else None,
                track_init_time_utc=str(track_init) if track_init else None,
            )
            cdata["track_available"] = False
            cdata["track_init_matches_cycle"] = False
            cdata["track_init_mismatch"] = {
                "cycle_issued_utc": issued,
                "track_init_time_utc": track_init,
            }
            cycle_json_path.write_text(json.dumps(cdata, indent=2) + "\n", encoding="utf-8")
            pruned += 1
            print(f"  - {cycle_dir.name}: cleared track from init {track_init} "
                  f"(cycle init {issued})")
        if pruned:
            print(f"Cleared {pruned} mismatched per-cycle track(s)")

    data["cycle_ids"] = paired_cycle_ids

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote WeatherNext tracks to {args.output}")

    # ------------------------------------------------------------------
    # Keep the cycle/status indexes honest about which cycles carry a track.
    # ------------------------------------------------------------------
    sync_track_flags(site_data_dir, cycles_dir)

    if not paired_cycle_ids:
        message = (f"No published cycle carries initialization {init_dt.isoformat()}; "
                   f"the WeatherNext track will not be shown on any cycle.")
        if args.allow_unpaired:
            print(f"WARNING: {message}")
        else:
            raise SystemExit(f"ERROR: {message}\n"
                             f"Export the matching county-risk cycles first, or pass "
                             f"--allow-unpaired to publish the track index anyway.")


def write_unavailable_track(path: Path, *, reason: str, cycle_id: str | None = None,
                            cycle_issued_utc: str | None = None,
                            track_init_time_utc: str | None = None) -> None:
    """Replace a per-cycle track with an explicit, explained placeholder."""
    payload: dict[str, Any] = {"available": False, "reason": reason}
    if cycle_id:
        payload["cycle_id"] = cycle_id
    if cycle_issued_utc:
        payload["cycle_issued_utc"] = cycle_issued_utc
    if track_init_time_utc:
        payload["rejected_track_init_time_utc"] = track_init_time_utc
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def sync_track_flags(site_data_dir: Path, cycles_dir: Path) -> None:
    """Mirror each cycle's real track_available into cycles.json and status.json."""
    if not cycles_dir.exists():
        return
    flags: dict[str, bool] = {}
    for cycle_dir in sorted(cycles_dir.iterdir()):
        cycle_json_path = cycle_dir / "cycle.json"
        if not cycle_json_path.exists():
            continue
        try:
            cdata = json.loads(cycle_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        flags[str(cdata.get("cycle_id", cycle_dir.name))] = bool(cdata.get("track_available"))

    cycles_meta_path = site_data_dir / "cycles.json"
    if cycles_meta_path.exists():
        try:
            cmeta = json.loads(cycles_meta_path.read_text(encoding="utf-8"))
            for c in cmeta:
                cid = str(c.get("cycle_id", ""))
                if cid in flags:
                    c["track_available"] = flags[cid]
            cycles_meta_path.write_text(json.dumps(cmeta, indent=2) + "\n", encoding="utf-8")
            print(f"Synced track_available for {len(flags)} cycle(s) in cycles.json")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not sync cycles.json: {exc}")

    status_path = site_data_dir / "status.json"
    if status_path.exists():
        try:
            smeta = json.loads(status_path.read_text(encoding="utf-8"))
            latest = smeta.get("latest", {})
            cid = str(latest.get("cycle_id", ""))
            if cid in flags:
                latest["track_available"] = flags[cid]
                status_path.write_text(json.dumps(smeta, indent=2) + "\n", encoding="utf-8")
                print(f"Synced track_available for {cid} in status.json")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not sync status.json: {exc}")


if __name__ == "__main__":
    main()
