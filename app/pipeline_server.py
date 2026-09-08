#!/usr/bin/env python3
"""
StormGrid & Weather2Grid Control Center Server
Zero-dependency HTTP server with real-time SSE streaming for the desktop dashboard.
"""

import os
import sys
import json
import time
import queue
from pathlib import Path
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# Import runner
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
from pipeline_runner import runner, W2G_ROOT, W2G_ARCHIVE_ROOT

PORT = int(os.environ.get("PORT", 8765))
HOST = "127.0.0.1"


class PipelineRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            index_file = APP_DIR / "index.html"
            self.wfile.write(index_file.read_bytes())
            return

        if self.path == "/api/status":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            status = runner.get_status()
            self.wfile.write(json.dumps(status).encode("utf-8"))
            return

        if self.path == "/api/latest":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = {"cycles": [], "archived_count": 0, "status": {}}
            try:
                cycles_file = W2G_ROOT / "site" / "data" / "cycles.json"
                if cycles_file.exists():
                    data["cycles"] = json.loads(cycles_file.read_text())
                status_file = W2G_ROOT / "site" / "data" / "status.json"
                if status_file.exists():
                    data["status"] = json.loads(status_file.read_text())
                arch_file = W2G_ARCHIVE_ROOT / "data" / "cycles.json"
                if arch_file.exists():
                    data["archived_count"] = len(json.loads(arch_file.read_text()))
            except Exception as e:
                data["error"] = str(e)
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        if self.path == "/api/logs":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            log_q = queue.Queue(maxsize=1000)

            def log_callback(msg: str):
                try:
                    log_q.put_nowait(msg)
                except queue.Full:
                    pass

            runner.subscribe_logs(log_callback)
            try:
                while True:
                    try:
                        msg = log_q.get(timeout=1.0)
                        payload = f"data: {json.dumps({'message': msg})}\n\n"
                        self.wfile.write(payload.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        # Keep-alive heartbeat
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                runner.unsubscribe_logs(log_callback)
            return

        # Fallback to standard file server
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/run":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
            try:
                options = json.loads(body)
            except Exception:
                options = {}

            started = runner.start(options)
            self.send_response(HTTPStatus.OK if started else HTTPStatus.CONFLICT)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": started, "status": runner.get_status()}).encode("utf-8"))
            return

        if self.path == "/api/stop":
            runner.cancel()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy GET /api/logs ping logging to keep stdout clean
        if "api/logs" in format or "api/status" in format:
            return
        super().log_message(format, *args)


class ReusableThreadingServer(ThreadingHTTPServer):
    allow_reuse_address = True


def run_server(port: int = PORT):
    server = ReusableThreadingServer((HOST, port), PipelineRequestHandler)
    print(f"Weather2Grid Pipeline Control Center running at http://{HOST}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()


if __name__ == "__main__":
    run_server()
