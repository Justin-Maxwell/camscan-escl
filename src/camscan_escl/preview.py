"""A live low-resolution preview, for positioning the page under the camera.

The daemon owns the camera for the preview and hands it back for a scan.
V4L2 streaming access is exclusive -- a second process gets EBUSY, measured
on this rig -- so there is no arrangement where a preview app and the
scanner both hold the device. One owner, and it releases on demand.

GEOMETRY, which is the whole point of the preview (spec §5 is the contract
this helps you satisfy):

The still is 2304x1536, 3:2, and on the C920 that is the *only* 3:2 mode and
exists only in YUYV. Every mode fast enough to stream is 4:3 or 16:9, so the
preview cannot share the still's framing exactly. Measured on this rig:

  - 16:9 modes are the still's FULL WIDTH with a CENTRED VERTICAL CROP.
    Cross-correlating a 1280x720 frame against the still scaled to 1280 wide
    put the best match at row 67 where a centred crop predicts 66, RMS 6.3;
    the "whole frame squashed" hypothesis scored 32.9, five times worse.
  - 4:3 modes are zoomed in -- a narrower horizontal field -- so they cannot
    be mapped by cropping alone. Do not preview in 4:3.

So a 16:9 preview shows the middle 1296 of the still's 1536 rows: the
scanner sees 120 rows MORE at the top and bottom than you can see. Crop
marks must show that, or they lie about what will be captured.
"""

from __future__ import annotations

import contextlib
import logging
import shlex
import subprocess
import threading
from dataclasses import dataclass

from .config import Config

log = logging.getLogger(__name__)

# JPEG frame delimiters. MJPG comes off the camera already encoded, so the
# preview never re-encodes: frames are sliced out of the stream and served.
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

# Guard against a desynchronised stream eating memory without bound.
MAX_FRAME_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Mark:
    """One paper size, as a rectangle in preview pixels."""

    name: str
    x: int
    y: int
    width: int
    height: int
    # True when the region runs past what the preview can show. The scan
    # still captures it; you just cannot see it here.
    clipped_top: bool
    clipped_bottom: bool
    clipped_right: bool


def visible_still_rows(still: tuple[int, int], preview: tuple[int, int]) -> tuple[float, float]:
    """Which rows of the still the preview actually shows, as (top, bottom).

    Derived from the measured relationship: full width, centred vertical crop.
    """
    scale = preview[0] / still[0]
    visible_rows = preview[1] / scale
    top = (still[1] - visible_rows) / 2
    return (top, top + visible_rows)


def marks(cfg: Config) -> list[Mark]:
    """Where each paper size lands in the preview, given the rig calibration.

    A scan region starts at the origin of the coverage area, so a paper of
    w x h mm occupies that fraction of the frame from the top left. If
    rig.coverage_mm is wrong, these marks are wrong in exactly the same way
    the scans are -- which is what makes them useful for calibrating it.
    """
    still = (cfg.capture.native_width, cfg.capture.native_height)
    preview = (cfg.preview.width, cfg.preview.height)
    cov_w, cov_h = cfg.rig.coverage_mm
    scale = preview[0] / still[0]
    top, bottom = visible_still_rows(still, preview)

    out = []
    for name, mm_w, mm_h in cfg.preview.papers:
        # Paper -> still pixels, then still -> preview pixels.
        sx = mm_w / cov_w * still[0]
        sy = mm_h / cov_h * still[1]
        px = 0
        py = (0 - top) * scale
        pw = sx * scale
        ph = sy * scale
        out.append(
            Mark(
                name=name,
                x=int(round(px)),
                y=int(round(py)),
                width=int(round(pw)),
                height=int(round(ph)),
                clipped_top=top > 0,
                clipped_bottom=sy > bottom,
                clipped_right=sx > still[0],
            )
        )
    return out


class PreviewStream:
    """Owns the camera between scans, and gets out of the way for one."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._latest: bytes | None = None
        self._seq = 0
        self._cond = threading.Condition()
        self._running = False
        # True while the camera is lent out for a capture. Distinguishes "back
        # shortly" from "stopped", which is the difference between an open
        # MJPEG response waiting and an open MJPEG response ending.
        self._paused = False
        # Held for the whole of a capture, so two scans cannot race each
        # other into restarting the stream underneath one another.
        self._camera = threading.RLock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> bool:
        if not self._cfg.preview.enable:
            return False
        with self._camera:
            return self._start_locked()

    def _start_locked(self) -> bool:
        if self._running:
            return True
        cmd = self._cfg.preview.command.replace("%d", self._cfg.capture.focus.device)
        cmd = cmd.replace("%s", f"{self._cfg.preview.width}x{self._cfg.preview.height}")
        cmd = cmd.replace("%r", str(self._cfg.preview.fps))
        try:
            self._proc = subprocess.Popen(
                shlex.split(cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("preview could not start: %s", exc)
            self._proc = None
            return False

        self._running = True
        self._thread = threading.Thread(target=self._read_frames, daemon=True)
        self._thread.start()
        log.info(
            "preview streaming %dx%d at %d fps",
            self._cfg.preview.width, self._cfg.preview.height, self._cfg.preview.fps,
        )
        return True

    def _stop_locked(self) -> None:
        if not self._running:
            return
        self._running = False
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()
            try:
                # Wait for the process to actually go, not merely be asked.
                # The device is not free until it has, and the capture that
                # follows would get EBUSY.
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._cond:
            self._latest = None
            self._cond.notify_all()

    def stop(self) -> None:
        with self._camera:
            self._stop_locked()

    @contextlib.contextmanager
    def released(self):
        """Hand the camera back for the duration of a capture, then resume.

        Configuration changes work the same way, per the design: stop the
        stream, do the thing, start it again. Nothing is reconfigured live.
        """
        with self._camera:
            was_running = self._running
            with self._cond:
                self._paused = was_running
                self._cond.notify_all()
            self._stop_locked()
            try:
                yield
            finally:
                try:
                    if was_running:
                        self._start_locked()
                finally:
                    with self._cond:
                        self._paused = False
                        self._cond.notify_all()

    # -- frames ---------------------------------------------------------

    def _read_frames(self) -> None:
        """Slice JPEGs out of the MJPEG stream as they arrive."""
        buf = bytearray()
        stdout = self._proc.stdout if self._proc else None
        if stdout is None:
            return
        try:
            while self._running:
                chunk = stdout.read(16384)
                if not chunk:
                    break
                buf.extend(chunk)

                while True:
                    start = buf.find(SOI)
                    if start < 0:
                        # No frame started yet; do not let junk accumulate.
                        if len(buf) > MAX_FRAME_BYTES:
                            del buf[:-2]
                        break
                    end = buf.find(EOI, start + 2)
                    if end < 0:
                        if start > 0:
                            del buf[:start]
                        if len(buf) > MAX_FRAME_BYTES:
                            log.warning("preview frame exceeded %d bytes, resyncing",
                                        MAX_FRAME_BYTES)
                            buf.clear()
                        break
                    frame = bytes(buf[start:end + 2])
                    del buf[:end + 2]
                    with self._cond:
                        self._latest = frame
                        self._seq += 1
                        self._cond.notify_all()
        except (OSError, ValueError) as exc:
            if self._running:
                log.warning("preview stream ended: %s", exc)
        finally:
            with self._cond:
                self._cond.notify_all()

    def latest(self, timeout: float = 2.0) -> bytes | None:
        with self._cond:
            if self._latest is None:
                self._cond.wait(timeout)
            return self._latest

    def frames(self, stall_timeout: float = 60.0):
        """Yield each new frame as it arrives, for the MJPEG endpoint.

        Survives a scan. The camera is handed over for several seconds during
        a capture, and an MJPEG response that ends there is not resumed by the
        browser -- the preview simply goes dead until the page is reloaded,
        which is what it used to do. So while the stream is paused this waits
        rather than returning, and only gives up once the stream has genuinely
        stopped, or nothing has arrived for `stall_timeout`.
        """
        last = -1
        waited = 0.0
        while True:
            with self._cond:
                while self._seq == last or self._latest is None:
                    if not (self._running or self._paused):
                        return
                    if not self._cond.wait(1.0):
                        waited += 1.0
                        if waited >= stall_timeout:
                            return
                    else:
                        waited = 0.0
                waited = 0.0
                last = self._seq
                frame = self._latest
            yield frame

    @property
    def running(self) -> bool:
        return self._running
