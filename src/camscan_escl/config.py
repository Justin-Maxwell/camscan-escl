"""Configuration loading for camscan-escl.

TOML at ~/.config/camscan-escl/config.toml, per spec §9. Every value has a
default so the daemon starts on a host with no config file at all; the
defaults are the ones written in the spec.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "camscan-escl" / "config.toml"


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
    device: str = "/dev/video0"
    absolute: int = 40
    disable_autofocus: bool = True


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
    source_path: Path | None = None


def _subset(cls, data: dict, **overrides):
    """Build a dataclass from the keys it declares, ignoring strangers."""
    known = {f.name for f in cls.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in data.items() if k in known and k not in overrides}
    return cls(**kwargs, **overrides)


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
        rig=RigConfig(
            coverage_mm=tuple(float(v) for v in coverage)
            if coverage
            else RigConfig().coverage_mm
        ),
        source_path=path,
    )
    validate(cfg)
    return cfg


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
