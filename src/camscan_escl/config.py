"""Configuration loading for camscan-escl.

TOML at ~/.config/camscan-escl/config.toml, per spec §9. Every value has a
default so the daemon starts on a host with no config file at all; the
defaults are the ones written in the spec.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "camscan-escl"

DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"

# Settings the GUI writes, kept apart from the hand-authored TOML above.
# JSON because the standard library can write it: rewriting a user's
# commented config file with a generated one would lose the comments, and
# those comments are where the reasoning lives.
ADJUSTMENTS_PATH = CONFIG_DIR / "adjustments.json"


@dataclass(frozen=True)
class ServerConfig:
    port: int = 8090
    bind: str = "127.0.0.1"


@dataclass(frozen=True)
class ScannerConfig:
    make_and_model: str = "camscan-escl (Logitech C920)"
    serial: str = "camscan-0001"
    resolution_dpi: int = 150
    jpeg_quality: int = 88


@dataclass(frozen=True)
class FocusConfig:
    """Autofocus by default. Pin it only if you have a reason.

    The spec pins focus on the grounds that autofocus hunts between pages.
    That is a real failure mode, but it is conditional, and a wrong fixed
    value is unconditionally bad -- the shipped guess of 40 was measurably
    softer than what the camera's own autofocus chooses for this rig, and
    nothing had ever checked. Autofocus is right by default and pinning is
    the informed exception, not the other way round.

    If you do pin it, get the number from `--focus-sweep`, and sanity-check
    it against what autofocus picks: enable autofocus, capture, then read
    `v4l2-ctl -d /dev/video0 -C focus_absolute`.
    """

    device: str = "/dev/video0"
    absolute: int = 0
    disable_autofocus: bool = False


@dataclass(frozen=True)
class ExposureConfig:
    """Pin exposure and white balance, for the same reason focus is pinned.

    A rig photographing paper wants every scan to come out the same. Left on
    auto, the sensor re-decides per capture and a bright scene can return a
    page washed to near-white -- observed on this rig: a scan with 89% of its
    pixels at 250+ luma, from a frame that was correctly exposed minutes
    later. Off by default because the right values are rig-specific; see the
    calibration recipe in README.
    """

    device: str = "/dev/video0"
    lock: bool = False
    # None leaves that control alone, so exposure can be pinned without
    # touching white balance or the other way round.
    time_absolute: int | None = None
    white_balance_temperature: int | None = None


@dataclass(frozen=True)
class CaptureConfig:
    # ffmpeg rather than the spec's fswebcam: 2304x1536 is YUYV-only on the
    # C920, and fswebcam negotiates MJPG and silently delivers 1920x1080.
    # Measured on this host; see config.example.toml.
    command: str = (
        "ffmpeg -loglevel error -f v4l2 -pix_fmt yuyv422 "
        "-video_size 2304x1536 -i /dev/video0 -frames:v 3 -update 1 -y %f"
    )
    native_width: int = 2304
    native_height: int = 1536
    timeout_s: int = 30
    # Degrees counter-clockwise to rotate the captured frame before it is
    # treated as the platen. The rig mounts the camera portrait (spec §6), so
    # the sensor's landscape frame needs turning before rig.coverage_mm means
    # anything. 0 = camera and page share an orientation.
    rotate_deg: int = 0
    focus: FocusConfig = field(default_factory=FocusConfig)
    exposure: ExposureConfig = field(default_factory=ExposureConfig)


@dataclass(frozen=True)
class DiscoveryConfig:
    # Advertise over DNS-SD so front-ends find the device without Manual IP.
    # Requires the optional `zeroconf` dependency; without it the daemon runs
    # and logs a warning.
    enable: bool = True
    # The DNS-SD instance name, kept separate from scanner.make_and_model.
    # The obvious instance name -- "camscan-escl (Logitech C920)
    # [camscan-0001]" -- carries spaces, parentheses and brackets, all legal
    # in DNS-SD and all a plausible thing for a client's own mDNS parser to
    # mishandle. The display name a front-end shows comes from the TXT `ty`
    # record and from MakeAndModel, so keeping this plain costs nothing.
    name: str = "camscan"


@dataclass(frozen=True)
class PreviewConfig:
    """A live preview for positioning, with crop marks.

    The size must be a 16:9 mode. Measured on the C920: 16:9 modes are the
    still's full width with a centred vertical crop, so preview pixels map
    onto still pixels exactly; 4:3 modes are zoomed to a narrower horizontal
    field and cannot be mapped by cropping. See preview.py.
    """

    enable: bool = True
    width: int = 1280
    height: int = 720
    fps: int = 15
    # A v4l2loopback device to publish the marked-up video on, so any normal
    # webcam app -- Kamoso, Cheese, a browser -- shows the preview WITH the
    # crop marks burned in. Empty disables it and the preview is web-only.
    # Needs the module: see README.
    loopback_device: str = ""
    # Everything outside the union of the paper sizes is dead space that no
    # scan can ever reach, so it is dimmed rather than left looking usable.
    outside_colour: str = "gray@0.55"
    # Paper sizes to draw, as name = [width_mm, height_mm].
    papers: tuple[tuple[str, float, float], ...] = (
        ("A4", 210.0, 297.0),
        ("A5", 148.0, 210.0),
        ("Letter", 215.9, 279.4),
    )


@dataclass(frozen=True)
class RigConfig:
    # Physical area the frame covers at rig height, in mm, [width, height],
    # measured in the same orientation the frame ends up after rotate_deg.
    coverage_mm: tuple[float, float] = (210.0, 297.0)


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    rig: RigConfig = field(default_factory=RigConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    source_path: Path | None = None


def _subset(cls, data: dict, **overrides):
    """Build a dataclass from the keys it declares, ignoring strangers."""
    known = {f.name for f in cls.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in data.items() if k in known and k not in overrides}
    return cls(**kwargs, **overrides)


def _preview(raw: dict) -> PreviewConfig:
    """PreviewConfig, converting a [preview.papers] table into a tuple."""
    data = dict(raw)
    papers = data.pop("papers", None)
    if papers is None:
        return _subset(PreviewConfig, data)
    return _subset(
        PreviewConfig,
        data,
        papers=tuple(
            (name, float(dims[0]), float(dims[1])) for name, dims in papers.items()
        ),
    )


def load(path: Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()

    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    capture_raw = dict(raw.get("capture", {}))
    focus_raw = capture_raw.pop("focus", {})
    exposure_raw = capture_raw.pop("exposure", {})
    rig_raw = dict(raw.get("rig", {}))
    coverage = rig_raw.pop("coverage_mm", None)

    cfg = Config(
        server=_subset(ServerConfig, raw.get("server", {})),
        scanner=_subset(ScannerConfig, raw.get("scanner", {})),
        capture=_subset(
            CaptureConfig,
            capture_raw,
            focus=_subset(FocusConfig, focus_raw),
            exposure=_subset(ExposureConfig, exposure_raw),
        ),
        discovery=_subset(DiscoveryConfig, raw.get("discovery", {})),
        preview=_preview(raw.get("preview", {})),
        rig=RigConfig(
            coverage_mm=tuple(float(v) for v in coverage)
            if coverage
            else RigConfig().coverage_mm
        ),
        source_path=path,
    )
    validate(cfg)
    return cfg


def load_adjustments(cfg: Config, path: Path | None = None) -> Config:
    """Apply the GUI's saved settings on top of a loaded config.

    Called by the entry point rather than by `load`, so that `load` stays a
    pure function of the file it is given. Folding it in meant anything
    calling `load` -- tests included -- silently inherited whichever
    calibration this particular machine happened to have saved.
    """
    path = Path(path) if path else ADJUSTMENTS_PATH
    if not path.exists():
        return cfg
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # Never let a corrupt adjustments file stop the scanner starting.
        log.warning("ignoring unreadable %s: %s", path, exc)
        return cfg
    return apply_adjustments(cfg, data)


def apply_adjustments(cfg: Config, data: dict) -> Config:
    """Overlay a settings dict onto a Config, ignoring anything unrecognised."""
    coverage = data.get("coverage_mm")
    papers = data.get("papers")
    if coverage and len(coverage) == 2:
        cfg = replace(cfg, rig=RigConfig(coverage_mm=(float(coverage[0]),
                                                      float(coverage[1]))))
    if papers is not None:
        cfg = replace(cfg, preview=replace(
            cfg.preview,
            papers=tuple((str(n), float(w), float(h)) for n, w, h in papers),
        ))
    return cfg


def save_adjustments(cfg: Config, path: Path | None = None) -> Path:
    """Persist the adjustable settings. Written whole, so it stays readable."""
    path = Path(path) if path else ADJUSTMENTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": "Written by the camscan-escl settings GUI. Delete this "
                    "file to fall back to config.toml.",
        "coverage_mm": list(cfg.rig.coverage_mm),
        "papers": [list(p) for p in cfg.preview.papers],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)  # atomic: a half-written file must never be loaded
    return path


def validate(cfg: Config) -> None:
    if "%f" not in cfg.capture.command:
        raise ValueError("capture.command must contain %f, the output path")
    if len(cfg.rig.coverage_mm) != 2 or min(cfg.rig.coverage_mm) <= 0:
        raise ValueError("rig.coverage_mm must be two positive numbers")
    if cfg.capture.rotate_deg % 90:
        raise ValueError("capture.rotate_deg must be a multiple of 90")
    if not 1 <= cfg.scanner.jpeg_quality <= 100:
        raise ValueError("scanner.jpeg_quality must be 1..100")
    if cfg.scanner.resolution_dpi <= 0:
        raise ValueError("scanner.resolution_dpi must be positive")
    # 16:9 only, and not for tidiness: 4:3 modes on this camera are zoomed to
    # a narrower horizontal field, so preview pixels cannot be mapped onto
    # still pixels by cropping and every crop mark would be wrong. Measured;
    # see preview.py.
    if cfg.preview.enable:
        ratio = cfg.preview.width / cfg.preview.height
        if abs(ratio - 16 / 9) > 0.01:
            raise ValueError(
                f"preview.width/height must be 16:9, got "
                f"{cfg.preview.width}x{cfg.preview.height}. A 4:3 preview has a "
                f"different field of view than the still and its crop marks "
                f"would be wrong."
            )
