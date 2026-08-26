"""Capture a frame by shelling out to a configurable command (spec §8)."""

from __future__ import annotations

import logging
import shlex
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from .config import CaptureConfig

log = logging.getLogger(__name__)

# The control is focus_automatic_continuous on current kernels and focus_auto
# on older ones. Detect, never assume (spec §8).
AUTOFOCUS_CONTROLS = ("focus_automatic_continuous", "focus_auto")
FOCUS_ABSOLUTE_CONTROLS = ("focus_absolute",)


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

    # Two invocations, not one. focus_absolute is read-only while continuous
    # autofocus is enabled, and a single v4l2-ctl call applies its -c options
    # in an order that loses the race: "focus_absolute: Permission denied".
    steps = []
    if auto:
        steps.append(f"{auto}=0")
    if absolute:
        steps.append(f"{absolute}={focus.absolute}")

    for step in steps:
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", focus.device, "-c", step],
                capture_output=True,
                text=True,
                timeout=cfg.timeout_s,
            )
            if result.returncode != 0 or "denied" in result.stderr.lower():
                log.warning("focus setup (%s) failed: %s", step, result.stderr.strip())
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("focus setup (%s) failed: %s", step, exc)


def grab(cfg: CaptureConfig) -> Image.Image:
    """Run the configured capture command and load the frame it wrote."""
    apply_focus(cfg)

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
