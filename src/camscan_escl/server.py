"""The eSCL HTTP surface (spec §4). Plain HTTP, base path /eSCL."""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import capture, devices, escl, imaging
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
        if path == PREVIEW + "/status":
            return self._preview_status()
        if path == PREVIEW + "/controls":
            return self._controls_get()

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
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == PREVIEW + "/settings":
            return self._settings_post()
        if path == PREVIEW + "/start":
            return self._preview_start()
        if path == PREVIEW + "/stop":
            return self._preview_stop()
        if path == PREVIEW + "/restart":
            return self._preview_restart()
        if path == PREVIEW + "/controls":
            return self._controls_post()
        if path == PREVIEW + "/controls/auto":
            return self._controls_auto()

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
            "rotate_deg": cfg.capture.rotate_deg,
            "landscape": cfg.preview.landscape,
            "papers": [list(p) for p in cfg.preview.papers],
            "preview": dict(zip(("width", "height"),
                                preview_mod.preview_size(cfg))),
            "still": [cfg.capture.native_width, cfg.capture.native_height],
            # The area the camera can actually see, which is smaller than the
            # coverage and is what the anchor is applied inside.
            "streamed_mm": [round(v, 1) for v in preview_mod.streamed_mm(cfg)],
            "does_not_fit": preview_mod.does_not_fit(cfg),
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
            config_mod.warn_about_geometry(cfg)
        except (ValueError, TypeError) as exc:
            return self._send(400, str(exc).encode(), "text/plain")

        # Every handler instance reads these off the class, so rebind there.
        type(self).config = cfg
        self.preview._cfg = cfg
        # REDRAWN, not discarded. The composited overlay is baked against one
        # geometry and is now misaligned, but the still it was made from is a
        # photograph of the desk and is as true as it was a second ago. Ticking
        # a paper size moves where the picture belongs; it does not make the
        # picture wrong, and throwing it away would blank the border until
        # someone happened to scan again.
        preview_mod.rebuild_ghost(cfg)
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

    # -- preview lifecycle and diagnostics -------------------------------
    #
    # These exist because the daemon spent a session insisting it was
    # streaming while its ffmpeg was a zombie. `running` was a lie, the error
    # went to DEVNULL, and the only visible symptom was a viewer reconnecting
    # every 31 seconds. Anything that can fail this quietly needs a way to be
    # asked what it is doing, and a way to be told to try again.

    def _preview_status(self) -> None:
        self._send(200, json.dumps(self.preview.status()).encode(),
                   "application/json")

    def _preview_start(self) -> None:
        # Waits for a real frame rather than a successful fork: the whole
        # point is that those are not the same thing.
        ok = self.preview.start(wait=6.0)
        status = self.preview.status()
        log.info("preview start requested: %s", status["summary"])
        self._send(200 if ok else 503,
                   json.dumps(status).encode(), "application/json")

    def _preview_stop(self) -> None:
        self.preview.stop()
        status = self.preview.status()
        log.info("preview stop requested")
        self._send(200, json.dumps(status).encode(), "application/json")

    def _preview_restart(self) -> None:
        """Stop and start, which is also how a device change is picked up.

        Device numbers are resolved at start, so a camera replugged onto a
        different node needs exactly this and not a daemon restart.
        """
        self.preview.stop()
        ok = self.preview.start(wait=6.0)
        status = self.preview.status()
        log.info("preview restart requested: %s", status["summary"])
        self._send(200 if ok else 503,
                   json.dumps(status).encode(), "application/json")

    # -- camera controls -------------------------------------------------
    #
    # Separate from /preview/settings on purpose. These are device-side V4L2
    # controls, so they take effect on the frame after next with no pipeline
    # restart -- which is what makes a slider draggable. Routing them through
    # the settings endpoint would rebuild the ffmpeg filter chain on every
    # pixel of travel.

    def _camera_device(self) -> str | None:
        try:
            return capture.camera_path(type(self).config.capture)
        except devices.DeviceError as exc:
            log.warning("camera controls unavailable: %s", exc)
            return None

    def _controls_payload(self, device: str | None, extra: dict | None = None) -> bytes:
        cfg = type(self).config
        ranges = (capture.control_ranges(device, cfg.capture.timeout_s)
                  if device else {})
        body = {
            "device": device,
            "ranges": ranges,
            "values": {
                "brightness": cfg.capture.image.brightness,
                "contrast": cfg.capture.image.contrast,
            },
            # What the camera is actually set to, which is not always what the
            # config asked for: a control can be refused, or left over from
            # another process, since V4L2 settings persist on the device.
            "actual": {name: entry.get("value")
                       for name, entry in ranges.items()},
        }
        body.update(extra or {})
        return json.dumps(body).encode()

    def _controls_get(self) -> None:
        device = self._camera_device()
        self._send(200, self._controls_payload(device), "application/json")

    def _store_controls(self, values: dict) -> None:
        """Apply to the camera and persist, without touching the pipeline."""
        cfg = config_mod.apply_adjustments(type(self).config, values)
        type(self).config = cfg
        self.preview._cfg = cfg
        capture.apply_image(cfg.capture)
        try:
            config_mod.save_adjustments(cfg)
        except OSError as exc:
            log.warning("could not save adjustments: %s", exc)

    def _controls_post(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            return self._send(400, f"bad JSON: {exc}".encode(), "text/plain")

        device = self._camera_device()
        if device is None:
            return self._unavailable()
        ranges = capture.control_ranges(device, type(self).config.capture.timeout_s)

        wanted = {}
        for name in capture.IMAGE_CONTROLS:
            if name not in data:
                continue
            if data[name] is None:
                wanted[name] = None
                continue
            try:
                value = int(data[name])
            except (TypeError, ValueError):
                return self._send(400, f"{name} must be a number".encode(),
                                  "text/plain")
            entry = ranges.get(name)
            if entry:
                # Clamped rather than refused. A slider cannot generate an
                # out-of-range value, so one arriving here means a script, and
                # the useful response is the nearest thing the camera can do.
                value = max(entry["min"], min(entry["max"], value))
            wanted[name] = value

        self._store_controls(wanted)
        log.info("camera controls set: %s", wanted)
        self._send(200, self._controls_payload(device), "application/json")

    def _controls_auto(self) -> None:
        """Meter the live preview and choose brightness and contrast."""
        device = self._camera_device()
        if device is None:
            return self._unavailable()
        if not self.preview.healthy:
            return self._send(
                503,
                b"the preview must be running to meter the picture",
                "text/plain")

        cfg = type(self).config
        ranges = capture.control_ranges(device, cfg.capture.timeout_s)
        if not ranges:
            return self._send(503, b"this camera exposes no image controls",
                              "text/plain")

        applied: dict = {}

        def set_control(name, value):
            applied[name] = value
            capture._set_controls(device, [f"{name}={value}"],
                                  cfg.capture.timeout_s, "auto")

        result = capture.auto_balance(self._fresh_frame, set_control, ranges)
        self._store_controls(result["applied"])
        log.info("auto image balance: %s measured %s",
                 result["applied"], result["measured"])
        self._send(200, self._controls_payload(device, {"auto": result}),
                   "application/json")

    def _fresh_frame(self):
        """A preview frame taken AFTER the control just set had time to land.

        The camera does not apply a control to the frame already in flight,
        and the reader hands out the most recent one it has. Waiting for the
        sequence number to move twice is what stops the loop metering the
        picture it was trying to change.
        """
        import io

        from PIL import Image

        frame = self.preview.next_frame(skip=2, timeout=2.0)
        if frame is None:
            return None
        try:
            return Image.open(io.BytesIO(frame))
        except OSError:
            return None

    def _preview_page(self) -> None:
        cfg = self.config
        raw = preview_mod.marks(cfg)
        scale, ox, oy = preview_mod.fit_transform(cfg, raw)
        html = page_html(cfg, [preview_mod.place(m, scale, ox, oy) for m in raw])
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def _unavailable(self) -> None:
        """503 that says WHY, not just that.

        "preview not running" was true and useless. The reason is known --
        which device could not be resolved, what ffmpeg said before it died --
        so it goes in the body where a client can show it.
        """
        summary = self.preview.status()["summary"]
        self._send(503, summary.encode(), "text/plain; charset=utf-8")

    def _preview_frame(self) -> None:
        frame = self.preview.latest() if self.preview.running else None
        if frame is None:
            return self._unavailable()
        self._send(200, frame, "image/jpeg")

    def _preview_stream(self) -> None:
        """multipart/x-mixed-replace, which every browser renders natively."""
        # `running` is now cleared when the pipeline dies, so this guard finally
        # means something. It used to pass with a dead ffmpeg behind it, send
        # 200, and then block until the stall timeout with nothing to send.
        if not self.preview.running:
            return self._unavailable()

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
                # Kept and composited INSIDE the released block, so the
                # pipeline that restarts on the way out already has this scan
                # to show. The still is stored unrotated and the border is
                # then drawn from the stored copy, not from the frame in hand:
                # that way what you see now is pixel-for-pixel what you will
                # see after a settings change redraws it.
                try:
                    preview_mod.save_scan_still(self.config, frame)
                    preview_mod.rebuild_ghost(self.config)
                except Exception as exc:  # noqa: BLE001 - never fail a scan
                    log.warning("could not write the scan ghost: %s", exc)
                frame = imaging.orient(frame, self.config.capture.rotate_deg)
            t3 = time.monotonic()
            log.info("timing: release %.2fs, capture %.2fs, resume %.2fs",
                     t1 - t0, t2 - t1, t3 - t2)

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
