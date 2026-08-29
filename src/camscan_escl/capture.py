"""Capture a frame by shelling out to a configurable command (spec §8)."""

from __future__ import annotations

import logging
import shlex
import statistics
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter

from . import devices
from .config import (
    CaptureConfig,
    exposure_device_spec,
    focus_device_spec,
    image_device_spec,
)

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


def camera_path(cfg: CaptureConfig) -> str:
    """The camera's device node right now, resolved from capture.device."""
    return devices.resolve(cfg.device, "capture")


def _resolve_quietly(spec: str, what: str) -> str | None:
    """Resolve a control device, or warn and return None.

    Control setting is best-effort throughout this module -- a camera that
    will not take a focus hint still takes photographs -- so an unresolvable
    device is a warning, not an exception.
    """
    try:
        return devices.resolve(spec, "capture")
    except devices.DeviceError as exc:
        log.warning("cannot apply %s settings: %s", what, exc)
        return None


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

    device = _resolve_quietly(exposure_device_spec(cfg), "exposure")
    if device is None:
        return
    available = _v4l2_controls(device, cfg.timeout_s)
    steps: list[str] = []

    if exposure.time_absolute is not None:
        auto = next((c for c in AUTO_EXPOSURE_CONTROLS if c in available), None)
        absolute = next((c for c in EXPOSURE_ABSOLUTE_CONTROLS if c in available), None)
        if auto:
            steps.append(f"{auto}={MANUAL_EXPOSURE}")
        if absolute:
            steps.append(f"{absolute}={exposure.time_absolute}")
        elif available:
            log.warning("no exposure control found on %s", device)

    if exposure.white_balance_temperature is not None:
        auto_wb = next((c for c in AUTO_WB_CONTROLS if c in available), None)
        temp = next((c for c in WB_TEMPERATURE_CONTROLS if c in available), None)
        if auto_wb:
            steps.append(f"{auto_wb}=0")
        if temp:
            steps.append(f"{temp}={exposure.white_balance_temperature}")

    _set_controls(device, steps, cfg.timeout_s, "exposure")


def apply_focus(cfg: CaptureConfig) -> None:
    """Pin focus before capture. Best-effort: a failure here is not fatal."""
    focus = cfg.focus
    device = _resolve_quietly(focus_device_spec(cfg), "focus")
    if device is None:
        return
    available = _v4l2_controls(device, cfg.timeout_s)

    auto = next((c for c in AUTOFOCUS_CONTROLS if c in available), None)
    absolute = next((c for c in FOCUS_ABSOLUTE_CONTROLS if c in available), None)
    if auto is None and available:
        log.warning("no autofocus control found on %s; leaving focus alone", device)

    steps = []
    if not focus.disable_autofocus:
        # Turn autofocus back ON, rather than returning and leaving whatever
        # the control happened to hold. V4L2 settings persist on the device
        # across processes, so anything that once pinned focus -- an earlier
        # config, a sweep, a stray v4l2-ctl -- would otherwise leave the
        # camera stuck at that value with nothing in the config to explain it.
        if auto:
            steps.append(f"{auto}=1")
    else:
        if auto:
            steps.append(f"{auto}=0")
        if absolute:
            steps.append(f"{absolute}={focus.absolute}")

    _set_controls(device, steps, cfg.timeout_s, "focus")


# The controls the settings window drives. Named rather than open-ended: a
# GUI that offered every control the driver happens to expose would be a
# different feature, and most of them are not about photographing paper.
IMAGE_CONTROLS = ("brightness", "contrast")


def control_ranges(device: str, timeout_s: int = 10,
                   names: tuple[str, ...] = IMAGE_CONTROLS) -> dict[str, dict]:
    """min/max/default/current for each control, read off the device.

    Read rather than assumed. 0..255 with a default of 128 is the C920's
    range, not a V4L2 guarantee, and a slider hard-coded to it would silently
    misrepresent any other camera.
    """
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-ctrls"],
            capture_output=True, text=True, timeout=timeout_s,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not read controls on %s: %s", device, exc)
        return {}

    found: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0] not in names:
            continue
        entry = {"name": parts[0]}
        for token in parts:
            for key in ("min", "max", "step", "default", "value"):
                prefix = f"{key}="
                if token.startswith(prefix):
                    try:
                        entry[key] = int(token[len(prefix):])
                    except ValueError:
                        pass
        if "min" in entry and "max" in entry:
            found[parts[0]] = entry
    return found


# Digital pan, tilt and zoom. The daemon never sets these, but the C920 has
# them and V4L2 control state lives on the DEVICE, surviving every process
# that touches it. A non-default value moves or crops what the streaming mode
# shows relative to the still -- which silently invalidates the centred-crop
# model every crop mark is placed by, with nothing in the pipeline to notice.
# Reported so a wrong picture has somewhere to be traced to.
GEOMETRY_CONTROLS = ("pan_absolute", "tilt_absolute", "zoom_absolute")


def geometry_controls(device: str, timeout_s: int = 10) -> dict:
    """pan/tilt/zoom and whether each is still at the camera's default."""
    found = control_ranges(device, timeout_s, GEOMETRY_CONTROLS)
    return {
        name: {
            "value": entry.get("value"),
            "default": entry.get("default"),
            "at_default": entry.get("value") == entry.get("default"),
        }
        for name, entry in found.items()
    }


def apply_image(cfg: CaptureConfig, device: str | None = None) -> None:
    """Push the configured brightness and contrast onto the camera.

    Takes effect on the live preview immediately, with no pipeline restart:
    these are device-side controls, so ffmpeg keeps streaming and the next
    frame simply looks different. That is what makes a slider usable.
    """
    settings = {name: getattr(cfg.image, name) for name in IMAGE_CONTROLS}
    steps = [f"{name}={value}" for name, value in settings.items()
             if value is not None]
    if not steps:
        return
    if device is None:
        device = _resolve_quietly(image_device_spec(cfg), "image")
        if device is None:
            return
    _set_controls(device, steps, cfg.timeout_s, "image")


def _centre_luma(image: Image.Image) -> list[int]:
    """Sorted luma of the middle of a frame.

    Centre only, and for a specific reason: the published preview has the
    crop marks and the dimmed dead zone burned into it, so metering the whole
    frame would be measuring `preview.outside_colour` as much as the paper.
    The middle is inside the marks and undimmed.
    """
    grey = image.convert("L")
    w, h = grey.size
    grey = grey.crop((w // 4, h // 4, w * 3 // 4, h * 3 // 4))
    return sorted(grey.get_flattened_data())


def _percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    return float(values[min(len(values) - 1, int(len(values) * q))])


def meter(image: Image.Image) -> float:
    """The 95th-percentile luma of the centre of a frame, 0..255.

    A high percentile rather than the mean, because the subject is paper --
    the brightest large area in the frame -- and the mean moves with how much
    desk happens to be showing around it.
    """
    return _percentile(_centre_luma(image), 0.95)


def spread(image: Image.Image) -> float:
    """How much tonal range the centre of the frame is using, 0..255."""
    values = _centre_luma(image)
    return _percentile(values, 0.95) - _percentile(values, 0.05)


# Paper bright but off the ceiling. 255 would mean the highlights are clipped
# and the texture in white paper is gone for good -- no amount of later
# processing brings back a value that was recorded as "maximum".
TARGET_LUMA = 232
# Enough separation between paper and print to stay legible. Not the full
# range: pushing contrast to its limit crushes the mid-tones a scan needs.
TARGET_SPREAD = 150


# Above this the paper's highlights are being clipped, and the texture in
# white paper is gone for good: no later processing recovers a value that was
# recorded as "maximum".
CLIPPING_LUMA = 248


def _stats(grab_frame) -> dict | None:
    """Level and tonal range from ONE frame.

    One frame, not two, because every measurement costs a couple of frame
    intervals and the loop takes a lot of them.
    """
    frame = grab_frame()
    if frame is None:
        return None
    values = _centre_luma(frame)
    high, low = _percentile(values, 0.95), _percentile(values, 0.05)
    return {"luma": high, "spread": high - low}


def _bisect_brightness(entry: dict, set_control, grab_frame,
                       rounds: int) -> tuple[int | None, float | None]:
    """Find the brightness whose 95th-percentile luma lands nearest the target.

    Bisection is sound here: brightness is a pure offset, so the measured
    level rises with it and stops rising only once it has clipped, which is
    past the target anyway. The loop keeps the best value it actually saw
    rather than trusting the last step.
    """
    low, high = entry["min"], entry["max"]
    best_value = best_error = best_measured = None
    for _ in range(rounds):
        if low > high:
            break
        middle = (low + high) // 2
        set_control("brightness", middle)
        stats = _stats(grab_frame)
        if stats is None:
            break
        error = abs(stats["luma"] - TARGET_LUMA)
        if best_error is None or error < best_error:
            best_value, best_error, best_measured = middle, error, stats["luma"]
        if stats["luma"] < TARGET_LUMA:
            low = middle + 1
        elif stats["luma"] > TARGET_LUMA:
            high = middle - 1
        else:
            break
    return best_value, best_measured


def _scan_contrast(entry: dict, set_control, grab_frame,
                   samples: int) -> tuple[int | None, float | None]:
    """Pick the contrast giving the most tonal range without clipping.

    A scan rather than a bisection, because spread is NOT monotonic in
    contrast: raising it pushes the shadows down onto zero, and once they
    clamp there the spread stops growing and starts shrinking again. A
    bisection assumes it can walk downhill towards a target and would stride
    straight past the peak -- measured on a simulated dark scene, it drove
    contrast to its maximum and crushed the ink to 0, leaving the picture
    flatter than when it started.
    """
    low, high = entry["min"], entry["max"]
    if samples < 2 or high <= low:
        return (None, None)
    best_value = best_spread = None
    for index in range(samples):
        candidate = low + round((high - low) * index / (samples - 1))
        set_control("contrast", candidate)
        stats = _stats(grab_frame)
        if stats is None:
            break
        if stats["luma"] >= CLIPPING_LUMA:
            continue          # gains "range" by destroying the highlights
        # Never overshoot the target either: past it, extra separation is
        # bought by crushing mid-tones a scan needs.
        score = min(stats["spread"], TARGET_SPREAD)
        if best_spread is None or score > best_spread:
            best_value, best_spread = candidate, stats["spread"]
    return best_value, best_spread


def auto_balance(grab_frame, set_control, ranges: dict[str, dict],
                 rounds: int = 6, samples: int = 5) -> dict:
    """Meter the live picture and choose brightness and contrast.

    Level first, then range, then level again. Contrast shifts the overall
    level as well as the spread, so the brightness found before it has to be
    re-trimmed afterwards or the result does not stick.

    Takes callables rather than a device path so the loop can be tested
    against a simulated camera, which is the only way to test it at all: the
    real one needs a rig, a page, and the light in the room.
    """
    result: dict = {"applied": {}, "measured": {}}

    if "brightness" in ranges:
        value, measured = _bisect_brightness(
            ranges["brightness"], set_control, grab_frame, rounds)
        if value is not None:
            set_control("brightness", value)
            result["applied"]["brightness"] = value
            result["measured"]["luma"] = measured

    # What brightness alone achieved, kept so the contrast pass can be judged
    # against it rather than assumed to be an improvement.
    level_only = result["measured"].get("luma")
    entry = ranges.get("contrast")
    prior_contrast = (entry or {}).get("value", (entry or {}).get("default", 128))

    if entry is not None:
        value, measured = _scan_contrast(entry, set_control, grab_frame, samples)
        if value is None:
            # Every candidate clipped. Leave the control where the camera had
            # it rather than picking the least-bad way to ruin the highlights.
            set_control("contrast", prior_contrast)
        else:
            set_control("contrast", value)
            result["applied"]["contrast"] = value
            result["measured"]["spread"] = measured

    # Contrast moved the level, so settle it again.
    if "brightness" in ranges and "contrast" in result["applied"]:
        value, measured = _bisect_brightness(
            ranges["brightness"], set_control, grab_frame, rounds)
        if value is not None:
            set_control("brightness", value)
            result["applied"]["brightness"] = value
            result["measured"]["luma"] = measured

        # Level beats range, for a rig photographing paper. More contrast
        # always looks like more information, but on a scene too dark to
        # reach the target it buys separation by pushing the whole picture
        # further down -- and brightness has already run out of room to lift
        # it back. Measured on a simulated dark scene: brightness alone
        # reached 197, and adding the "better" contrast left it at 139.
        final = result["measured"].get("luma")
        if (level_only is not None and final is not None
                and abs(final - TARGET_LUMA) > abs(level_only - TARGET_LUMA) + 2):
            set_control("contrast", prior_contrast)
            result["applied"]["contrast"] = prior_contrast
            result["measured"].pop("spread", None)
            result["reverted_contrast"] = True
            value, measured = _bisect_brightness(
                ranges["brightness"], set_control, grab_frame, rounds)
            if value is not None:
                set_control("brightness", value)
                result["applied"]["brightness"] = value
                result["measured"]["luma"] = measured

    return result


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
    the answer is only true for the distance it was measured at.

    Treat the numbers with suspicion. Variance of the Laplacian rewards noise
    as much as detail, so a grainier frame can outscore a sharper one, and a
    nearly flat spread across the range means the differences are not about
    focus at all. Look at the frames before believing the ranking, and check
    it against what the camera's own autofocus settles on -- on the rig this
    was written for, autofocus chose the value the sweep ranked first.
    """
    scored = []
    device = _resolve_quietly(focus_device_spec(cfg), "focus sweep")
    if device is None:
        return []
    for value in values:
        _set_controls(
            device,
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
        # So a scan gets the picture the preview was showing, rather than
        # whatever the camera happened to be left set to.
        apply_image(cfg)

    with tempfile.TemporaryDirectory(prefix="camscan-") as tmp:
        out = Path(tmp) / "frame.jpg"
        argv = shlex.split(cfg.command)
        if any("%d" in tok for tok in argv):
            # Resolved here rather than at config load, so a camera replugged
            # onto a different node is picked up by the next scan instead of
            # needing a restart.
            try:
                device = camera_path(cfg)
            except devices.DeviceError as exc:
                raise CaptureError(f"no camera to capture from: {exc}") from exc
            argv = [tok.replace("%d", device) for tok in argv]
        argv = [tok.replace("%f", out.as_posix()) for tok in argv]

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
