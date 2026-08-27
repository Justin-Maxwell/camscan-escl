"""Capture a frame by shelling out to a configurable command (spec §8)."""

from __future__ import annotations

import logging
import shlex
import statistics
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter

from .config import CaptureConfig

log = logging.getLogger(__name__)

# The control is focus_automatic_continuous on current kernels and focus_auto
# on older ones. Detect, never assume (spec §8).
AUTOFOCUS_CONTROLS = ("focus_automatic_continuous", "focus_auto")
FOCUS_ABSOLUTE_CONTROLS = ("focus_absolute",)

# Same rename-across-kernels story as focus. auto_exposure is a menu where 1
# is Manual Mode and 3 is Aperture Priority; the older exposure_auto uses the
# same numbering, so one value serves both.
AUTO_EXPOSURE_CONTROLS = ("auto_exposure", "exposure_auto")
EXPOSURE_ABSOLUTE_CONTROLS = ("exposure_time_absolute", "exposure_absolute")
MANUAL_EXPOSURE = 1
AUTO_WB_CONTROLS = ("white_balance_automatic", "white_balance_temperature_auto")
WB_TEMPERATURE_CONTROLS = ("white_balance_temperature",)


class CaptureError(RuntimeError):
    """The camera did not produce a usable frame."""


def _v4l2_controls(device: str, timeout_s: int) -> set[str]:
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-ctrls"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not list v4l2 controls on %s: %s", device, exc)
        return set()
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def _set_controls(device: str, steps: list[str], timeout_s: int, what: str) -> None:
    """Apply v4l2 controls one invocation at a time. Best-effort, never fatal.

    One call per control, not one call with many -c options: a manual control
    is read-only while its automatic counterpart is enabled, and v4l2-ctl
    applies the options of a single call in an order that loses that race --
    "focus_absolute: Permission denied", observed on this rig.
    """
    for step in steps:
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", device, "-c", step],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if result.returncode != 0 or "denied" in result.stderr.lower():
                log.warning("%s setup (%s) failed: %s", what, step, result.stderr.strip())
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("%s setup (%s) failed: %s", what, step, exc)


def apply_exposure(cfg: CaptureConfig) -> None:
    """Pin exposure and white balance so scans are repeatable, not ambient."""
    exposure = cfg.exposure
    if not exposure.lock:
        return

    available = _v4l2_controls(exposure.device, cfg.timeout_s)
    steps: list[str] = []

    if exposure.time_absolute is not None:
        auto = next((c for c in AUTO_EXPOSURE_CONTROLS if c in available), None)
        absolute = next((c for c in EXPOSURE_ABSOLUTE_CONTROLS if c in available), None)
        if auto:
            steps.append(f"{auto}={MANUAL_EXPOSURE}")
        if absolute:
            steps.append(f"{absolute}={exposure.time_absolute}")
        elif available:
            log.warning("no exposure control found on %s", exposure.device)

    if exposure.white_balance_temperature is not None:
        auto_wb = next((c for c in AUTO_WB_CONTROLS if c in available), None)
        temp = next((c for c in WB_TEMPERATURE_CONTROLS if c in available), None)
        if auto_wb:
            steps.append(f"{auto_wb}=0")
        if temp:
            steps.append(f"{temp}={exposure.white_balance_temperature}")

    _set_controls(exposure.device, steps, cfg.timeout_s, "exposure")


def apply_focus(cfg: CaptureConfig) -> None:
    """Pin focus before capture. Best-effort: a failure here is not fatal."""
    focus = cfg.focus
    if not focus.disable_autofocus:
        return

    available = _v4l2_controls(focus.device, cfg.timeout_s)

    auto = next((c for c in AUTOFOCUS_CONTROLS if c in available), None)
    absolute = next((c for c in FOCUS_ABSOLUTE_CONTROLS if c in available), None)
    if auto is None and available:
        log.warning("no autofocus control found on %s; leaving focus alone", focus.device)

    steps = []
    if auto:
        steps.append(f"{auto}=0")
    if absolute:
        steps.append(f"{absolute}={focus.absolute}")

    _set_controls(focus.device, steps, cfg.timeout_s, "focus")


def sharpness(image: Image.Image) -> float:
    """Variance of the Laplacian: the standard focus metric, higher is sharper.

    Scored on the centre half only. The edges of the frame carry whatever is
    on the desk, and clutter reads as detail.
    """
    grey = image.convert("L")
    w, h = grey.size
    grey = grey.crop((w // 4, h // 4, w * 3 // 4, h * 3 // 4))
    lap = grey.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1))
    return statistics.pvariance(list(lap.get_flattened_data()))


def focus_sweep(cfg: CaptureConfig, values: list[int]) -> list[tuple[int, float]]:
    """Score each focus setting on a real capture, sharpest first.

    Run this with the rig in its final position and a real page underneath:
    the answer is only true for the distance it was measured at. `absolute=0`
    is infinity on this camera and 255 is closest, so a winner at 0 means the
    subject is beyond the near-focus range -- move the camera, do not just
    take the number.
    """
    scored = []
    for value in values:
        _set_controls(
            cfg.focus.device,
            [f"{c}=0" for c in AUTOFOCUS_CONTROLS[:1]] + [f"focus_absolute={value}"],
            cfg.timeout_s,
            "focus sweep",
        )
        try:
            frame = grab(cfg, apply_settings=False)
        except CaptureError as exc:
            log.warning("focus %d: capture failed: %s", value, exc)
            continue
        score = sharpness(frame)
        log.info("focus %3d: sharpness %9.1f", value, score)
        scored.append((value, score))
    return sorted(scored, key=lambda pair: pair[1], reverse=True)


def grab(cfg: CaptureConfig, apply_settings: bool = True) -> Image.Image:
    """Run the configured capture command and load the frame it wrote."""
    if apply_settings:
        apply_focus(cfg)
        apply_exposure(cfg)

    with tempfile.TemporaryDirectory(prefix="camscan-") as tmp:
        out = Path(tmp) / "frame.jpg"
        argv = [tok.replace("%f", out.as_posix()) for tok in shlex.split(cfg.command)]

        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=cfg.timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raise CaptureError(f"capture command timed out after {cfg.timeout_s}s") from exc
        except OSError as exc:
            raise CaptureError(f"capture command could not be run: {exc}") from exc

        if result.returncode != 0:
            raise CaptureError(
                f"capture command exited {result.returncode}: {result.stderr.strip()}"
            )
        if not out.exists() or out.stat().st_size == 0:
            raise CaptureError("capture command wrote no image")

        try:
            with Image.open(out) as img:
                frame = img.convert("RGB")
        except OSError as exc:
            raise CaptureError(f"capture output is not a readable image: {exc}") from exc

    expected = (cfg.native_width, cfg.native_height)
    if frame.size != expected:
        # fswebcam may negotiate MJPG and silently land at 1920x1080, because
        # 2304x1536 exists only in YUYV on this device. Say so loudly (§8).
        log.error(
            "CAPTURE SIZE MISMATCH: got %sx%s, configured native is %sx%s. "
            "The command probably negotiated a different pixel format; see "
            "the ffmpeg fallback in the spec.",
            frame.size[0], frame.size[1], *expected,
        )

    return frame
