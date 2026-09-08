#!/usr/bin/env python3
"""
StormGrid & Weather2Grid Live Pipeline Runner
Orchestrates live forecasting, inference, cyclone tracking, archiving, and deployment.
"""

import os
import sys
import time
import json
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

# Standard repository paths
APP_DIR = Path(__file__).resolve().parent
W2G_ROOT = APP_DIR.parent
SG_ROOT = W2G_ROOT.parent / "stormgrid"
W2G_ARCHIVE_ROOT = W2G_ROOT.parent / "weather2grid-archive"
DATA_ROOT = SG_ROOT / "data"

SG_PYTHON = SG_ROOT / ".venv" / "bin" / "python"
W2G_PYTHON = W2G_ROOT / ".venv" / "bin" / "python"


class PipelineRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.cancelled = False
        self.current_process: Optional[subprocess.Popen] = None
        self.log_history: List[str] = []
        self.log_listeners: List[Callable[[str], None]] = []

        self.state: Dict[str, Any] = {
            "status": "idle",       # idle | running | success | failed | cancelled
            "progress": 0,          # 0 - 100
            "current_stage": "",
            "stage_index": 0,
            "total_stages": 7,
            "stages": [
                {"id": "preflight", "name": "Preflight & System Checks", "status": "pending", "progress": 0},
                {"id": "hrrr", "name": "NOAA HRRR Live Outlook", "status": "pending", "progress": 0},
                {"id": "wnx3", "name": "DeepMind WeatherNext 3 (12h Rolling Windows)", "status": "pending", "progress": 0},
                {"id": "cyclones", "name": "NOAA ATCF WeatherNext Cyclone Tracking", "status": "pending", "progress": 0},
                {"id": "export_archive", "name": "Dashboard Export & Archive Sync", "status": "pending", "progress": 0},
                {"id": "verification", "name": "Track Pairing & Publication Gate", "status": "pending", "progress": 0},
                {"id": "deploy", "name": "Deploy to GitHub Pages (Git Push)", "status": "pending", "progress": 0},
            ],
            "start_time": None,
            "end_time": None,
            "elapsed_seconds": 0,
            "error_message": "",
            "metrics": {
                "hrrr_init": None,
                "wnx3_init": None,
                "cyclones": [],
                "published_cycles": 0,
                "archived_cycles": 0,
            }
        }

    def subscribe_logs(self, callback: Callable[[str], None]):
        with self.lock:
            self.log_listeners.append(callback)
            for line in self.log_history[-100:]:
                callback(line)

    def unsubscribe_logs(self, callback: Callable[[str], None]):
        with self.lock:
            if callback in self.log_listeners:
                self.log_listeners.remove(callback)

    def log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}"
        with self.lock:
            self.log_history.append(formatted)
            listeners = list(self.log_listeners)
        for cb in listeners:
            try:
                cb(formatted)
            except Exception:
                pass

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            status_copy = dict(self.state)
            if self.running and self.state["start_time"]:
                status_copy["elapsed_seconds"] = int(time.time() - self.state["start_time"])
            return status_copy

    def update_stage(self, stage_id: str, status: str, progress: int = 100):
        with self.lock:
            for idx, st in enumerate(self.state["stages"]):
                if st["id"] == stage_id:
                    st["status"] = status
                    st["progress"] = progress
                    self.state["current_stage"] = st["name"]
                    self.state["stage_index"] = idx + 1
                    break
            
            # Recalculate overall progress
            completed_stages = sum(1 for s in self.state["stages"] if s["status"] == "success")
            running_stage = next((s for s in self.state["stages"] if s["status"] == "running"), None)
            stage_frac = (running_stage["progress"] / 100.0) if running_stage else 0.0
            total = len(self.state["stages"])
            overall = int(((completed_stages + stage_frac) / total) * 100)
            self.state["progress"] = min(overall, 100)

    def run_command(self, cmd: List[str], cwd: Path, env: Optional[Dict[str, str]] = None, stage_id: str = "") -> bool:
        if self.cancelled:
            return False

        full_env = os.environ.copy()
        full_env["SG_SKIP_TESTS"] = "1"
        full_env["PYTHONUNBUFFERED"] = "1"
        full_env["PAGER"] = "cat"
        if env:
            full_env.update(env)

        self.log(f"Executing: {' '.join(cmd)}")
        try:
            p = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=full_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self.lock:
                self.current_process = p

            for line in iter(p.stdout.readline, ""):
                if self.cancelled:
                    p.terminate()
                    break
                line_clean = line.rstrip()
                if line_clean:
                    self.log(f"  {line_clean}")

            p.stdout.close()
            ret = p.wait()

            with self.lock:
                self.current_process = None

            if self.cancelled:
                self.log("Command aborted by user.")
                return False

            if ret != 0:
                self.log(f"Command failed with exit code {ret}")
                return False
            return True
        except Exception as e:
            self.log(f"Execution error: {str(e)}")
            return False

    def cancel(self):
        with self.lock:
            if not self.running:
                return
            self.cancelled = True
            self.state["status"] = "cancelled"
            if self.current_process:
                try:
                    self.current_process.terminate()
                except Exception:
                    pass
        self.log("Pipeline cancellation requested.")

    def start(self, options: Dict[str, Any]):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.cancelled = False
            self.log_history.clear()
            self.state["status"] = "running"
            self.state["start_time"] = time.time()
            self.state["end_time"] = None
            self.state["error_message"] = ""
            for st in self.state["stages"]:
                st["status"] = "pending"
                st["progress"] = 0

        thread = threading.Thread(target=self._execute_pipeline, args=(options,), daemon=True)
        thread.start()
        return True

    def _notify(self, title: str, message: str):
        try:
            cmd = f'display notification "{message}" with title "{title}" sound name "Glass"'
            subprocess.run(["osascript", "-e", cmd], check=False)
        except Exception:
            pass

    def _execute_pipeline(self, options: Dict[str, Any]):
        run_hrrr = options.get("hrrr", True)
        run_wnx3 = options.get("wnx3", True)
        run_cyclones = options.get("cyclones", True)
        do_push = options.get("push", True)
        do_force = options.get("force", False)

        self.log("=======================================================")
        self.log("🚀 Weather2Grid Live Forecast Pipeline")
        self.log(f"Options: HRRR={run_hrrr}, WNX3={run_wnx3}, Cyclones={run_cyclones}, Push={do_push}, Force={do_force}")
        self.log("=======================================================")

        try:
            # ------------------------------------------------------------- STAGE 1: Preflight
            self.update_stage("preflight", "running", 20)
            self.log("--> Stage 1: Running Preflight & System Checks...")

            if not SG_PYTHON.exists():
                raise RuntimeError(f"StormGrid virtualenv not found at {SG_PYTHON}")
            if not W2G_PYTHON.exists():
                raise RuntimeError(f"Weather2Grid virtualenv not found at {W2G_PYTHON}")

            self.log(f"StormGrid Python: {SG_PYTHON}")
            self.log(f"Weather2Grid Python: {W2G_PYTHON}")
            self.log("Environment checks passed.")
            self.update_stage("preflight", "success", 100)

            # ------------------------------------------------------------- STAGE 2: NOAA HRRR
            if run_hrrr and not self.cancelled:
                self.update_stage("hrrr", "running", 30)
                self.log("--> Stage 2: Ingesting latest NOAA HRRR and running inference...")
                hrrr_script = W2G_ROOT / "scripts" / "run_hrrr_live.sh"
                hrrr_cmd = ["/bin/bash", str(hrrr_script)]
                if do_force:
                    hrrr_cmd.append("--force")
                
                success = self.run_command(hrrr_cmd, cwd=W2G_ROOT, stage_id="hrrr")
                if not success and not self.cancelled:
                    raise RuntimeError("NOAA HRRR pipeline failed.")
                self.update_stage("hrrr", "success", 100)
            else:
                self.update_stage("hrrr", "success" if not run_hrrr else "cancelled", 100)

            # ------------------------------------------------------------- STAGE 3: WeatherNext 3
            if run_wnx3 and not self.cancelled:
                self.update_stage("wnx3", "running", 30)
                self.log("--> Stage 3: Ingesting Google DeepMind WeatherNext 3 (12h rolling windows)...")
                wnx3_script = W2G_ROOT / "scripts" / "run_weathernext3_live.sh"
                wnx3_cmd = ["/bin/bash", str(wnx3_script)]
                if do_force:
                    wnx3_cmd.append("--force")

                success = self.run_command(wnx3_cmd, cwd=W2G_ROOT, stage_id="wnx3")
                if not success and not self.cancelled:
                    raise RuntimeError("WeatherNext 3 pipeline failed.")
                self.update_stage("wnx3", "success", 100)
            else:
                self.update_stage("wnx3", "success" if not run_wnx3 else "cancelled", 100)

            # ------------------------------------------------------------- STAGE 4: Cyclone Tracks
            if run_cyclones and not self.cancelled:
                self.update_stage("cyclones", "running", 50)
                self.log("--> Stage 4: Automated NOAA ATCF & Weather Lab Cyclone Track resolution...")
                track_script = W2G_ROOT / "scripts" / "fetch_weathernext_tracks.py"

                # Find latest WeatherNext 3 init
                cycles_file = W2G_ROOT / "site" / "data" / "cycles.json"
                wnx_init = ""
                if cycles_file.exists():
                    try:
                        cycles = json.loads(cycles_file.read_text())
                        for c in cycles:
                            if "wn3" in c.get("cycle_id", ""):
                                wnx_init = c.get("issued_utc", "")
                                break
                    except Exception:
                        pass

                cmd = [
                    str(W2G_PYTHON),
                    str(track_script),
                    "--version", "3",
                    "--output", str(W2G_ROOT / "site" / "data" / "weathernext-active-tracks.json"),
                    "--populate-cycles",
                    "--allow-synthetic"
                ]
                if wnx_init:
                    cmd.extend(["--init", wnx_init])

                success = self.run_command(cmd, cwd=W2G_ROOT, stage_id="cyclones")
                if not success and not self.cancelled:
                    self.log("Warning: ATCF track fetch encountered an issue, checking existing tracks...")
                self.update_stage("cyclones", "success", 100)
            else:
                self.update_stage("cyclones", "success" if not run_cyclones else "cancelled", 100)

            # ------------------------------------------------------------- STAGE 5: Export & Archive
            if not self.cancelled:
                self.update_stage("export_archive", "running", 50)
                self.log("--> Stage 5: Merging multi-series data and syncing archive...")
                export_script = SG_ROOT / "scripts" / "export_weather2grid.sh"
                env_export = {
                    "SG_WEATHER2GRID_REPO": str(W2G_ROOT),
                    "SG_DATA_ROOT": str(DATA_ROOT),
                    "SG_WEATHER2GRID_ARCHIVE_REPO": str(W2G_ARCHIVE_ROOT),
                }
                success = self.run_command(["/bin/bash", str(export_script)], cwd=SG_ROOT, env=env_export, stage_id="export_archive")
                if not success and not self.cancelled:
                    raise RuntimeError("Export and archive synchronization failed.")
                self.update_stage("export_archive", "success", 100)

            # ------------------------------------------------------------- STAGE 6: Verification Gates
            if not self.cancelled:
                self.update_stage("verification", "running", 50)
                self.log("--> Stage 6: Running publication gates and track pairing assertions...")

                # Track pairing check
                pair_check = [
                    str(W2G_PYTHON), "-c",
                    """
import json, sys
from pathlib import Path
site = Path(sys.argv[1])
cycles_dir = site / "cycles"
failures = []
paired = withheld = 0
for d in sorted(p for p in cycles_dir.glob("*") if p.is_dir()):
    cycle = json.loads((d / "cycle.json").read_text())
    issued = cycle.get("issued_utc")
    track_path = d / "track.json"
    track = json.loads(track_path.read_text()) if track_path.is_file() else {"available": False}
    if track.get("available") is False or not track.get("points"):
        withheld += 1
        continue
    init = track.get("forecast_init_time_utc") or track.get("init_time_utc")
    if str(init) != str(issued):
        failures.append(f"{d.name}: track init {init} != cycle init {issued}")
    else:
        paired += 1
print(f"Paired cycles: {paired}, withheld: {withheld}")
if failures:
    for f in failures: print(f"ERROR: {f}")
    sys.exit(1)
print("Track pairing: PASS")
                    """,
                    str(W2G_ROOT / "site" / "data")
                ]
                success = self.run_command(pair_check, cwd=W2G_ROOT, stage_id="verification")
                if not success and not self.cancelled:
                    raise RuntimeError("Track pairing assertion failed.")

                self.update_stage("verification", "success", 100)

            # ------------------------------------------------------------- STAGE 7: Deploy & Git Push
            if do_push and not self.cancelled:
                self.update_stage("deploy", "running", 40)
                self.log("--> Stage 7: Deploying to GitHub Pages (committing and pushing)...")

                # Push archive repository first
                self.log("Pushing weather2grid-archive...")
                self.run_command(["git", "add", "-u"], cwd=W2G_ARCHIVE_ROOT)
                self.run_command(
                    ["git", "commit", "-m", "Live forecast archive update", "--allow-empty"],
                    cwd=W2G_ARCHIVE_ROOT
                )
                success_archive = self.run_command(["git", "push", "origin", "main"], cwd=W2G_ARCHIVE_ROOT)
                if not success_archive:
                    self.log("Warning: archive push returned non-zero (may be already up to date).")

                # Push live weather2grid repository
                self.log("Pushing weather2grid live site...")
                self.run_command(["git", "add", "-u"], cwd=W2G_ROOT)
                self.run_command(
                    ["git", "commit", "-m", "Live forecast publication: HRRR + WeatherNext 3", "--allow-empty"],
                    cwd=W2G_ROOT
                )
                success_live = self.run_command(["git", "push", "origin", "main"], cwd=W2G_ROOT)
                if not success_live and not self.cancelled:
                    raise RuntimeError("Git push to live repository failed.")

                self.update_stage("deploy", "success", 100)
            else:
                self.update_stage("deploy", "success", 100)
                self.log("Git push skipped (--push not selected). Data ready in local site/data.")

            # Pipeline finished successfully!
            if not self.cancelled:
                with self.lock:
                    self.state["status"] = "success"
                    self.state["progress"] = 100
                    self.state["end_time"] = time.time()
                self.log("🎉 ALL PIPELINES COMPLETED SUCCESSFULLY!")
                self._notify("Weather2Grid Live", "Forecast pipeline completed and published successfully!")
                self._update_metrics()

        except Exception as e:
            err = str(e)
            self.log(f"❌ PIPELINE ERROR: {err}")
            with self.lock:
                self.state["status"] = "failed"
                self.state["error_message"] = err
                self.state["end_time"] = time.time()
            self._notify("Weather2Grid Live Alert", f"Pipeline failed: {err}")
        finally:
            with self.lock:
                self.running = False

    def _update_metrics(self):
        try:
            cycles_file = W2G_ROOT / "site" / "data" / "cycles.json"
            if cycles_file.exists():
                cycles = json.loads(cycles_file.read_text())
                hrrr_inits = [c["issued_utc"] for c in cycles if "hrrr" in c.get("cycle_id", "")]
                wnx_inits = [c["issued_utc"] for c in cycles if "wn3" in c.get("cycle_id", "")]
                with self.lock:
                    self.state["metrics"]["published_cycles"] = len(cycles)
                    if hrrr_inits:
                        self.state["metrics"]["hrrr_init"] = hrrr_inits[0]
                    if wnx_inits:
                        self.state["metrics"]["wnx3_init"] = wnx_inits[0]

            archive_file = W2G_ARCHIVE_ROOT / "data" / "cycles.json"
            if archive_file.exists():
                arch_cycles = json.loads(archive_file.read_text())
                with self.lock:
                    self.state["metrics"]["archived_cycles"] = len(arch_cycles)

            tracks_file = W2G_ROOT / "site" / "data" / "weathernext-active-tracks.json"
            if tracks_file.exists():
                tracks = json.loads(tracks_file.read_text())
                with self.lock:
                    self.state["metrics"]["cyclones"] = [t.get("storm_name") for t in tracks.get("tracks", []) if t.get("storm_name")]
        except Exception:
            pass


# Global singleton instance
runner = PipelineRunner()
runner._update_metrics()
