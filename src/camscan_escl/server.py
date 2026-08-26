"""The eSCL HTTP surface (spec §4). Plain HTTP, base path /eSCL."""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import capture, escl, imaging
from .config import Config
from .jobs import JobStore

log = logging.getLogger(__name__)

BASE = "/eSCL"


class ESCLHandler(BaseHTTPRequestHandler):
    server_version = "camscan-escl/1.0"
    protocol_version = "HTTP/1.1"

    # Injected by serve().
    config: Config
    jobs: JobStore

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes = b"", content_type: str | None = None,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _path_parts(self) -> list[str] | None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if not path.startswith(BASE):
            return None
        return [p for p in path[len(BASE):].split("/") if p]

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    # -- verbs -----------------------------------------------------------

    def do_GET(self) -> None:
        parts = self._path_parts()
        if parts is None:
            return self._send(404)

        if parts == ["ScannerCapabilities"]:
            cfg = self.config.scanner
            return self._send(
                200,
                escl.capabilities_xml(cfg.make_and_model, cfg.serial, cfg.resolution_dpi),
                "text/xml; charset=utf-8",
            )

        if parts == ["ScannerStatus"]:
            state = "Processing" if self.jobs.scanning else "Idle"
            return self._send(200, escl.status_xml(state), "text/xml; charset=utf-8")

        if len(parts) == 3 and parts[0] == "ScanJobs" and parts[2] == "NextDocument":
            return self._next_document(parts[1])

        self._send(404)

    def do_POST(self) -> None:
        parts = self._path_parts()
        if parts != ["ScanJobs"]:
            return self._send(404)

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        try:
            settings = escl.parse_scan_settings(body, self.config.scanner.resolution_dpi)
        except Exception as exc:  # malformed XML from a client
            log.warning("could not parse ScanSettings: %s", exc)
            return self._send(400)

        job = self.jobs.create(settings)
        if job is None:
            # One job at a time (spec §3).
            return self._send(503, headers={"Retry-After": "5"})

        host = self.headers.get("Host") or f"{self.config.server.bind}:{self.config.server.port}"
        location = f"http://{host}{BASE}/ScanJobs/{job.id}"
        log.info(
            "job %s: region=%s res=%dx%d mode=%s expect=%sx%s",
            job.id, settings.region, settings.x_resolution, settings.y_resolution,
            settings.color_mode, *settings.expected_size,
        )
        self._send(201, headers={"Location": location})

    def do_DELETE(self) -> None:
        parts = self._path_parts()
        if len(parts or []) == 2 and parts[0] == "ScanJobs":
            self.jobs.delete(parts[1])
            return self._send(200)
        self._send(404)

    # -- the scan itself -------------------------------------------------

    def _next_document(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.delivered:
            # Not an error: this is how the client learns the job is done (§4).
            return self._send(404)

        self.jobs.scanning = True
        try:
            frame = capture.grab(self.config.capture)
            frame = imaging.orient(frame, self.config.capture.rotate_deg)
            jpeg = imaging.render(
                frame,
                job.settings,
                self.config.rig.coverage_mm,
                self.config.scanner.jpeg_quality,
            )
        except Exception as exc:
            # Fail the job, stay Idle, stay selectable (§7, acceptance 7).
            log.error("job %s failed: %s", job_id, exc)
            self.jobs.finish(job_id, failed=True)
            return self._send(500)
        finally:
            self.jobs.scanning = False

        self.jobs.finish(job_id)
        log.info("job %s: delivered %d bytes", job_id, len(jpeg))
        self._send(200, jpeg, "image/jpeg")


def serve(config: Config) -> ThreadingHTTPServer:
    handler = type("BoundESCLHandler", (ESCLHandler,), {
        "config": config,
        "jobs": JobStore(),
    })
    httpd = ThreadingHTTPServer((config.server.bind, config.server.port), handler)
    return httpd
