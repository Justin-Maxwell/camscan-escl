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
    `v4l2-ctl -d "$(camscan-escl --print-camera)" -C focus_absolute`.
    """

    # Empty means "whatever capture.device resolves to", which is almost
    # always right: focus controls belong to the camera being captured from.
    # Set it only to drive controls on a different node than the capture.
    device: str = ""
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

    device: str = ""          # empty: follow capture.device, as with focus
    lock: bool = False
    # None leaves that control alone, so exposure can be pinned without
    # touching white balance or the other way round.
    time_absolute: int | None = None
    white_balance_temperature: int | None = None


@dataclass(frozen=True)
class ImageConfig:
    """Brightness and contrast, as V4L2 controls on the camera itself.

    Set on the device rather than as an ffmpeg filter, so the scan gets the
    same picture the preview showed. A filter would brighten the preview and
    leave every scan exactly as dark as before, which is worse than useless
    on a rig whose whole job is photographing paper.

    None leaves a control alone, so brightness can be pinned without touching
    contrast. The C920 takes 0..255 for both, defaulting to 128; the daemon
    reads the real range off the device rather than assuming that.
    """

    device: str = ""          # empty: follow capture.device
    brightness: int | None = None
    contrast: int | None = None


@dataclass(frozen=True)
class CaptureConfig:
    # How to find the camera. NOT a /dev/videoN number by default: those move
    # between boots and a stale one made the daemon read from its own loopback
    # for a whole session. See devices.py. Use "card:HD Pro Webcam C920" to
    # name a specific camera; "auto" takes the first node that can capture.
    device: str = "auto"
    # ffmpeg rather than the spec's fswebcam: 2304x1536 is YUYV-only on the
    # C920, and fswebcam negotiates MJPG and silently delivers 1920x1080.
    # Measured on this host; see config.example.toml.
    # %d is the resolved capture device, %f the output path. A command with no
    # %d is run verbatim, so an existing hand-written command still works --
    # it just keeps whatever device number is baked into it.
    command: str = (
        "ffmpeg -loglevel error -f v4l2 -pix_fmt yuyv422 "
        "-video_size 2304x1536 -i %d -frames:v 3 -update 1 -y %f"
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
    image: ImageConfig = field(default_factory=ImageConfig)


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
    # Takes the same forms as capture.device, and for the same reason: the
    # loopback's number moves between boots too. "card:OBS Virtual Camera"
    # names it by the card_label its modprobe options set.
    loopback_device: str = ""
    # Dead space, dimmed rather than left looking usable. Two kinds of it,
    # and the dimming covers both: what the camera sees but no scan can reach
    # (outside the union of the paper sizes), and what the camera cannot see
    # at all (the border, when an oversized mark has zoomed the picture out).
    #
    # It is applied to the intersection of those two regions, not to the
    # union alone. An oversized mark spans the border, so against the union
    # the border counted as wanted and went undimmed -- leaving a strip with
    # no picture in it looking like part of the scene.
    outside_colour: str = "gray@0.55"
    # Draw the crop marks across the frame. Purely about the marks: eSCL
    # carries orientation in the requested region's own dimensions -- a
    # landscape scan is one wider than it is tall -- so a client asking for
    # a landscape page needs no rotation from us, and up stays up.
    landscape: bool = False
    # Shrink the picture when a crop mark would fall entirely outside it.
    # A mark bigger than the frame otherwise draws nothing, exactly when it
    # most needs to be seen.
    # Shrink the picture to show the parts of a mark that run off the frame.
    #
    # The padding goes only on the sides that actually overflow. The anchor
    # clamps marks to the edge it names, so that edge never overflows and
    # never gets a border: the video stays flush against exactly the edge you
    # are lining paper up against. A border appears opposite, where a paper
    # too big for the frame is spilling, and its width is the size of the
    # spill.
    fit_marks: bool = True
    # The most of the frame that may become padding, as a fraction. Past this
    # the overflow clips instead, because a border wide enough to swallow the
    # picture tells you less about where the paper goes than a clipped mark.
    max_pad: float = 0.35
    # Washed-out paper, not the near-black this used to be. The padding is
    # where the camera has no picture, and a solid black strip beside the
    # video read as a fault rather than as "outside the field of view" --
    # and swallowed the crop marks drawn across it. A pale desaturated tone
    # keeps those marks legible and says "notional paper area".
    #
    # Genuinely translucent is not available: the pipeline publishes yuv420p
    # and MJPEG, neither of which carries an alpha channel, so a flat light
    # tone is the honest approximation.
    pad_colour: str = "#d7d2c8"
    # Show the last scan, faintly, in the border.
    #
    # The scan captures more than the preview can show -- the preview is a
    # 16:9 crop of a 3:2 still -- so the border is not empty space, it is
    # space the SCANNER can reach and the live view cannot. Painting the last
    # captured frame there, washed back into the padding, makes the border
    # say what is actually out there instead of being a blank margin.
    #
    # Redrawn after every scan, and it is a still: it does not move with the
    # page, which is the point of showing it faintly rather than as picture.
    scan_ghost: bool = True
    # How much of the scan shows through, 0 invisible to 1 full strength.
    # Faint on purpose -- it must never be mistaken for the live view.
    scan_ghost_opacity: float = 0.38
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
    # Where the scan region sits within that coverage. Clients always send an
    # origin of 0,0, so without this every scan is pinned to the top left of
    # what the camera sees. A rig usually wants the page in the middle.
    # An edge anchor also decides the size of the scannable area: the strip of
    # sensor the live picture never reaches is dropped on the anchored edge,
    # so the edge a sheet registers against is one you can watch. See
    # preview.py's module docstring.
    anchor: str = "center"


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    rig: RigConfig = field(default_factory=RigConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    source_path: Path | None = None
    # Where generated state goes: the stored scan still and the composited
    # border overlay. Separate from the config file, and settable, because a
    # hard-coded CONFIG_DIR meant every test that ran a scan wrote into the
    # real one -- the suite quietly replaced a live preview's border with the
    # flat blue rectangle its fake camera paints, and then the daemon showed
    # that to the user until the next real scan.
    state_dir: Path | None = None


def state_dir(cfg: Config) -> Path:
    """Where generated state lives for this configuration."""
    return Path(cfg.state_dir) if cfg.state_dir else CONFIG_DIR


def focus_device_spec(cfg: CaptureConfig) -> str:
    """Which device the focus controls act on. Follows the camera by default."""
    return cfg.focus.device or cfg.device


def exposure_device_spec(cfg: CaptureConfig) -> str:
    """Which device the exposure controls act on. Follows the camera too."""
    return cfg.exposure.device or cfg.device


def image_device_spec(cfg: CaptureConfig) -> str:
    """Which device brightness and contrast act on. Follows the camera too."""
    return cfg.image.device or cfg.device


def device_report(cfg: Config) -> list[dict]:
    """Resolve every configured device, for logging and the diagnostic panel.

    Never raises and never fails the daemon. An unplugged camera is a thing to
    report, not a reason to refuse to start -- the whole point of the redesign
    is that the failure shows up somewhere a human is looking.
    """
    from . import devices as devices_mod

    present = devices_mod.enumerate_devices()
    wanted = [("camera", cfg.capture.device, "capture")]
    focus_spec = focus_device_spec(cfg.capture)
    if focus_spec != cfg.capture.device:
        wanted.append(("focus", focus_spec, "capture"))
    exposure_spec = exposure_device_spec(cfg.capture)
    if exposure_spec != cfg.capture.device:
        wanted.append(("exposure", exposure_spec, "capture"))
    if cfg.preview.loopback_device:
        wanted.append(("loopback", cfg.preview.loopback_device, "output"))

    out = []
    for name, spec, role in wanted:
        entry = devices_mod.describe(spec, role, present)
        entry["name"] = name
        out.append(entry)
    return out


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
    image_raw = capture_raw.pop("image", {})
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
            image=_subset(ImageConfig, image_raw),
        ),
        discovery=_subset(DiscoveryConfig, raw.get("discovery", {})),
        preview=_preview(raw.get("preview", {})),
        rig=RigConfig(
            coverage_mm=tuple(float(v) for v in coverage)
            if coverage
            else RigConfig().coverage_mm,
            anchor=rig_raw.get("anchor", RigConfig().anchor),
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
    anchor = data.get("anchor")
    landscape = data.get("landscape")
    if landscape is not None:
        cfg = replace(cfg, preview=replace(cfg.preview,
                                           landscape=bool(landscape)))
    rotate = data.get("rotate_deg")
    if rotate is not None:
        cfg = replace(cfg, capture=replace(cfg.capture,
                                           rotate_deg=int(rotate) % 360))
    # The streaming mode. Everything the preview derives -- the canvas, the
    # band, the scale, where the marks land -- is computed from this pair, so
    # setting it is the whole change.
    mode = data.get("preview_mode")
    if mode and len(mode) == 2:
        cfg = replace(cfg, preview=replace(cfg.preview,
                                           width=int(mode[0]),
                                           height=int(mode[1])))
    # Camera controls. Absent leaves the config's value; explicit null clears
    # the pin and hands the control back to the camera's own default.
    image_keys = [k for k in ("brightness", "contrast") if k in data]
    if image_keys:
        cfg = replace(cfg, capture=replace(cfg.capture, image=replace(
            cfg.capture.image,
            **{k: (None if data[k] is None else int(data[k]))
               for k in image_keys},
        )))
    if coverage and len(coverage) == 2:
        cfg = replace(cfg, rig=replace(cfg.rig,
                                       coverage_mm=(float(coverage[0]),
                                                    float(coverage[1]))))
    if anchor:
        cfg = replace(cfg, rig=replace(cfg.rig, anchor=str(anchor)))
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
        "anchor": cfg.rig.anchor,
        "preview_mode": [cfg.preview.width, cfg.preview.height],
        "rotate_deg": cfg.capture.rotate_deg,
        "landscape": cfg.preview.landscape,
        "papers": [list(p) for p in cfg.preview.papers],
        "brightness": cfg.capture.image.brightness,
        "contrast": cfg.capture.image.contrast,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)  # atomic: a half-written file must never be loaded
    return path


def warn_about_geometry(cfg: Config) -> None:
    """Complain if the coverage is a different shape to the frame.

    Not fatal -- a tilted camera is a legitimate reason -- but loud, because
    the symptom is silent: every scan comes out stretched by this ratio and
    the dimensions still satisfy the units contract, so nothing else
    complains. Measured on this rig at 2.05x before anyone noticed.

    Deliberately NOT part of `validate`, which runs inside `load` -- that is
    before the GUI's saved adjustments are applied, so it judged a rotation
    and a coverage the daemon was never going to use. It announced "scans
    will be stretched 2.12x" on every start of a correctly calibrated rig,
    which is the precise way to teach someone to ignore a warning.
    """
    from .preview import upright_still

    # The scannable area, which is what rig.coverage_mm measures and which an
    # edge anchor has already trimmed. Measuring the whole still here would
    # report a skew of exactly the strip's size on a correctly calibrated
    # edge-anchored rig.
    fw, fh = upright_still(cfg)
    frame_aspect = fw / fh
    cov_aspect = cfg.rig.coverage_mm[0] / cfg.rig.coverage_mm[1]
    skew = max(frame_aspect, cov_aspect) / min(frame_aspect, cov_aspect)
    if skew > 1.02:
        log.warning(
            "rig.coverage_mm is %.3f wide-to-tall but the frame is %.3f: "
            "scans will be stretched %.2fx. With the camera square-on these "
            "must match -- a millimetre is the same number of pixels in both "
            "directions. Suggested height for this width: %.1f mm",
            cov_aspect, frame_aspect, skew,
            cfg.rig.coverage_mm[0] / frame_aspect,
        )


def validate(cfg: Config) -> None:
    if "%f" not in cfg.capture.command:
        raise ValueError("capture.command must contain %f, the output path")
    if len(cfg.rig.coverage_mm) != 2 or min(cfg.rig.coverage_mm) <= 0:
        raise ValueError("rig.coverage_mm must be two positive numbers")
    from .imaging import ANCHORS
    if cfg.rig.anchor not in ANCHORS:
        raise ValueError(
            f"rig.anchor must be one of {', '.join(sorted(ANCHORS))}"
        )
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
        from .preview import is_mappable

        ratio = cfg.preview.width / cfg.preview.height
        if not is_mappable(cfg.preview.width, cfg.preview.height):
            raise ValueError(
                f"preview.width/height must be 16:9, got "
                f"{cfg.preview.width}x{cfg.preview.height}. A 4:3 preview has a "
                f"different field of view than the still and its crop marks "
                f"would be wrong."
            )
