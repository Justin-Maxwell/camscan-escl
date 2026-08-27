"""The eSCL HTTP surface (spec §4). Plain HTTP, base path /eSCL."""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import capture, escl, imaging
from . import config as config_mod
from . import preview as preview_mod
from .config import Config
from .jobs import JobStore
from .previewpage import page_html

log = logging.getLogger(__name__)

BASE = "/eSCL"
PREVIEW = "/preview"


class ESCLHandler(BaseHTTPRequestHandler):
    server_version = "camscan-escl/1.0"
    protocol_version = "HTTP/1.1"

    # Injected by serve().
    config: Config
    jobs: JobStore
    preview: preview_mod.PreviewStream

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
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in (PREVIEW, PREVIEW + "/index.html", "/"):
            return self._preview_page()
        if path == PREVIEW + "/stream":
            return self._preview_stream()
        if path == PREVIEW + "/frame":
            return self._preview_frame()
        if path == PREVIEW + "/settings":
            return self._settings_get()

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
        if self.path.split("?", 1)[0].rstrip("/") == PREVIEW + "/settings":
            return self._settings_post()

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

    # -- preview ---------------------------------------------------------

    def _settings_get(self) -> None:
        cfg = type(self).config
        body = json.dumps({
            "coverage_mm": list(cfg.rig.coverage_mm),
            "anchor": cfg.rig.anchor,
            "papers": [list(p) for p in cfg.preview.papers],
            "preview": {"width": cfg.preview.width, "height": cfg.preview.height},
            "still": [cfg.capture.native_width, cfg.capture.native_height],
        }).encode()
        self._send(200, body, "application/json")

    def _settings_post(self) -> None:
        """Apply new settings and restart the pipeline so they take effect.

        The marks are baked into ffmpeg's filter chain when it starts, so a
        change means a new pipeline. That is a second or so, and it is why
        the GUI drives this rather than the user editing TOML and running
        systemctl.
        """
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            return self._send(400, f"bad JSON: {exc}".encode(), "text/plain")

        try:
            cfg = config_mod.apply_adjustments(type(self).config, data)
            config_mod.validate(cfg)
        except (ValueError, TypeError) as exc:
            return self._send(400, str(exc).encode(), "text/plain")

        # Every handler instance reads these off the class, so rebind there.
        type(self).config = cfg
        self.preview._cfg = cfg
        try:
            saved = config_mod.save_adjustments(cfg)
        except OSError as exc:
            log.warning("could not save adjustments: %s", exc)
            saved = None

        with self.preview.released():
            pass  # released() stops and restarts, which reloads the filters

        log.info("settings updated: coverage_mm=%s papers=%d%s",
                 list(cfg.rig.coverage_mm), len(cfg.preview.papers),
                 f", saved to {saved}" if saved else " (not saved)")
        self._settings_get()

    def _preview_page(self) -> None:
        html = page_html(self.config, preview_mod.marks(self.config))
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def _preview_frame(self) -> None:
        frame = self.preview.latest() if self.preview.running else None
        if frame is None:
            return self._send(503, b"preview not running", "text/plain")
        self._send(200, frame, "image/jpeg")

    def _preview_stream(self) -> None:
        """multipart/x-mixed-replace, which every browser renders natively."""
        if not self.preview.running:
            return self._send(503, b"preview not running", "text/plain")

        boundary = "camscanframe"
        self.send_response(200)
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={boundary}"
        )
        # Frames are pushed until the client goes away, so the length is not
        # known and keep-alive framing does not apply.
        self.send_header("Cache-Control", "no-store")
        self.protocol_version = "HTTP/1.0"
        self.end_headers()

        try:
            for frame in self.preview.frames():
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n".encode()
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tab was closed; entirely normal

    # -- the scan itself -------------------------------------------------

    def _next_document(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.delivered:
            # Not an error: this is how the client learns the job is done (§4).
            return self._send(404)

        self.jobs.scanning = True
        try:
            # The preview owns the camera between scans, and V4L2 streaming
            # access is exclusive: without handing it back, this grab gets
            # EBUSY. `released` restores the stream afterwards either way.
            t0 = time.monotonic()
            with self.preview.released():
                t1 = time.monotonic()
                frame = capture.grab(self.config.capture)
                t2 = time.monotonic()
            t3 = time.monotonic()
            log.info("timing: release %.2fs, capture %.2fs, resume %.2fs",
                     t1 - t0, t2 - t1, t3 - t2)
            frame = imaging.orient(frame, self.config.capture.rotate_deg)

            jpeg = imaging.render(
                frame,
                job.settings,
                self.config.rig.coverage_mm,
                self.config.scanner.jpeg_quality,
                self.config.rig.anchor,
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


def serve(config: Config, stream: preview_mod.PreviewStream | None = None) -> ThreadingHTTPServer:
    handler = type("BoundESCLHandler", (ESCLHandler,), {
        "config": config,
        "jobs": JobStore(),
        "preview": stream if stream is not None else preview_mod.PreviewStream(config),
    })
    httpd = ThreadingHTTPServer((config.server.bind, config.server.port), handler)
    return httpd
