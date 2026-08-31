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
  - 4:3 modes are a NARROWER HORIZONTAL FIELD. Which is the same thing on the
    other axis: full height, centred crop of columns.

Generalised, a mode's field of view is the largest centred rectangle of its
shape that fits the sensor. Wider than 3:2 crops rows, taller than 3:2 crops
columns, and the slack is on whichever axis the shape leaves it. So a 16:9
preview shows the middle 1296 of the still's 1536 rows and a 4:3 preview the
middle 2048 of its 2304 columns. Either way the scanner sees more than you
do, and crop marks must show that or they lie about what will be captured.

Only the wide arm is cross-correlated. The tall arm is the same mechanism
predicted onto the other axis -- 4:3 was established to be a narrower
horizontal field, which this explains, but the extent was never measured. See
docs/ISSUES.md for how to check it with a ruler.

THE ANCHORED EDGE. A rig registers a sheet by pushing it against a rail, and
the crop mark for that sheet is drawn flush against the same edge of the
scannable area. Those two edges have to be the same line or the mark is drawn
somewhere the sheet cannot be. Take the whole still as the scannable area and
they are not: its edge sits 120 rows outside the picture, so the registration
edge is in the ghost, where it cannot be watched.

So the scannable area drops the strip on the anchored edge, and the two edges
become one line. Which strip goes is read off `rig.anchor` and nothing else --
there is no configuration in which an anchor names an edge and the strip
outside it should be kept, so this is not a setting. A centred anchor
registers against nothing and keeps both strips.

It costs those 120 rows, about 8% of the sensor, and it is not a function of
camera height: no particular height is needed to make the marks true.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import subprocess
import time
import threading
from dataclasses import dataclass
from pathlib import Path

from . import devices
from .config import Config
from .imaging import ANCHORS, anchor_offset_mm

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
    # True when the region runs past the canvas, which is the scannable area.
    # That part of the sheet is not captured at all.
    #
    # clipped_left carries a default only so the three-flag constructor call
    # in the tests still builds. Every constructor in this module sets all
    # four; an anchor on the right or the bottom overflows the low edges, so
    # leaving one out would report a sheet as fitting when it does not.
    clipped_top: bool
    clipped_bottom: bool
    clipped_right: bool
    clipped_left: bool = False


def visible_still_rows(still: tuple[int, int], preview: tuple[int, int]) -> tuple[float, float]:
    """Which rows of the still the preview actually shows, as (top, bottom).

    Derived from the measured relationship: full width, centred vertical crop.
    """
    scale = preview[0] / still[0]
    visible_rows = preview[1] / scale
    top = (still[1] - visible_rows) / 2
    return (top, top + visible_rows)


# rotate_deg counts counter-clockwise, as PIL's rotate does. ffmpeg's
# transpose calls the same turn "cclock".
TRANSPOSE = {90: "transpose=cclock", 180: "transpose=clock,transpose=clock",
             270: "transpose=clock"}


def is_mappable(width: int, height: int) -> bool:
    """Can a preview of this size be mapped onto the still by cropping?

    THE definition, and `validate` calls this one rather than repeating the
    test. A chooser that hid a size `validate` accepts would be the daemon
    holding two opinions about the same mode, and the stricter of the two
    would be wherever someone happened to look last.

    Any positive size, now that `raw_visible_still_region` crops on whichever
    axis has the slack. This used to be 16:9 within a tolerance, which was the
    measured arm mistaken for the whole rule: a 4:3 mode is a narrower
    horizontal field, and a narrower horizontal field IS a crop, just of
    columns rather than rows. What the tolerance actually protected was the
    one-axis arithmetic behind it, and that is gone.
    """
    return width > 0 and height > 0


def available_modes(cfg: Config) -> list[devices.StreamMode]:
    """The camera's mappable streaming modes, largest first.

    Asked of the camera, so the choice is whatever this camera has rather
    than a list of one model's. Every derived quantity -- the canvas, the
    band, the scale, the marks -- is computed from the size, so changing it
    needs nothing else changed.
    """
    try:
        path = devices.resolve(cfg.capture.device, "capture")
    except devices.DeviceError as exc:
        log.warning("cannot offer preview modes: %s", exc)
        return []
    return [m for m in reversed(devices.stream_modes(path))
            if is_mappable(m.width, m.height)]


def sensor_preview_size(cfg: Config) -> tuple[int, int]:
    """The preview frame as it comes off the sensor, before any transpose."""
    return (cfg.preview.width, cfg.preview.height)


def stream_size(cfg: Config) -> tuple[int, int]:
    """The live camera frame, after the transpose. Smaller than the canvas.

    A turned camera is turned back at the head of the pipeline, so what the
    stream shows is the upright frame the scanner works from.
    """
    w, h = sensor_preview_size(cfg)
    return (h, w) if cfg.capture.rotate_deg % 180 == 90 else (w, h)


def raw_upright_still(cfg: Config) -> tuple[int, int]:
    """The whole still's dimensions, after the transpose and before the fence."""
    sw, sh = cfg.capture.native_width, cfg.capture.native_height
    return (sh, sw) if cfg.capture.rotate_deg % 180 == 90 else (sw, sh)


def ghost_axis(cfg: Config) -> int:
    """Which upright axis carries the two unstreamed strips. 0 across, 1 down.

    Measured off the region rather than assumed from the rotation, because the
    strips are not always on the same axis: a mode wider than the sensor's 3:2
    leaves slack in rows, a mode taller than it leaves slack in columns, and
    the transpose then moves whichever it is. Deriving it means a 4:3 preview
    on a turned camera correctly puts the strips at the top and bottom.
    """
    sw, sh = raw_upright_still(cfg)
    x0, y0, x1, y1 = raw_visible_still_region(cfg)
    return 0 if (sw - (x1 - x0)) >= (sh - (y1 - y0)) else 1


def fence_edge(cfg: Config) -> str | None:
    """Which upright edge the anchor registers against: "low", "high", or None.

    Read off `rig.anchor`, and off nothing else. An anchored edge is the line a
    sheet is registered against, so it has to be a line that can be seen; there
    is no configuration in which an anchor names an edge and the strip outside
    it should be kept. None only when the anchor names no edge on the axis the
    strips lie on, which is a centred anchor -- and then there is nothing to
    remove, because nothing is being registered against anything.
    """
    fraction = ANCHORS.get(cfg.rig.anchor, ANCHORS["top-left"])[ghost_axis(cfg)]
    if fraction == 0.0:
        return "low"
    if fraction == 1.0:
        return "high"
    return None


def scannable_box(cfg: Config) -> tuple[int, int, int, int]:
    """The scannable area within the raw upright still, in still pixels.

    The still minus the strip the live stream never reaches on the anchored
    edge: that edge of the scannable area and that edge of the live picture
    become the same line, so a sheet pushed against the rail is registered
    where it can be watched. Costs the 120 sensor pixels the strip holds.

    The strip on the far edge is kept, and is scanned but not streamed. So is
    both strips on a centred anchor, which registers against nothing.
    """
    sw, sh = raw_upright_still(cfg)
    edge = fence_edge(cfg)
    if edge is None:
        return (0, 0, sw, sh)
    x0, y0, x1, y1 = raw_visible_still_region(cfg)
    if ghost_axis(cfg) == 0:
        return (int(round(x0)), 0, sw, sh) if edge == "low" \
            else (0, 0, int(round(x1)), sh)
    return (0, int(round(y0)), sw, sh) if edge == "low" \
        else (0, 0, sw, int(round(y1)))


def upright_still(cfg: Config) -> tuple[int, int]:
    """The scannable area's dimensions, which is the space everything
    downstream works in. `rig.coverage_mm` measures exactly this rectangle."""
    x0, y0, x1, y1 = scannable_box(cfg)
    return (x1 - x0, y1 - y0)


def sensor_scannable(cfg: Config) -> tuple[int, int]:
    """The scannable area in the sensor's own orientation, before the transpose."""
    w, h = upright_still(cfg)
    return (h, w) if cfg.capture.rotate_deg % 180 == 90 else (w, h)


def to_scannable(cfg: Config, frame):
    """Turn a raw capture upright and trim it to the scannable area.

    The box is scaled to the frame in hand rather than used as given. It is
    derived from `capture.native_*`, and not every frame that comes through
    here is that size: `save_scan_still` downsamples the stored still to keep
    the file small, so the ghost is rebuilt from a 1600x1067 copy of a
    2304x1536 capture. Cropping that with a full-size box does not fail --
    PIL extends past the edge with black -- so the ghost came out two thirds
    black, in the shape of the difference. Nothing numeric caught it: every
    test fed this a full-size frame. It was found by looking at the picture.
    """
    from .imaging import orient

    frame = orient(frame, cfg.capture.rotate_deg)
    x0, y0, x1, y1 = scannable_box(cfg)
    sw, sh = raw_upright_still(cfg)
    fw, fh = frame.size
    if (fw, fh) != (sw, sh):
        x0, x1 = round(x0 * fw / sw), round(x1 * fw / sw)
        y0, y1 = round(y0 * fh / sh), round(y1 * fh / sh)
    if (x0, y0, x1, y1) == (0, 0, fw, fh):
        return frame
    return frame.crop((x0, y0, x1, y1))


def still_scale(cfg: Config) -> float:
    """Still pixels to published pixels, set so the stream is never resampled."""
    x0, _y0, x1, _y1 = visible_still_region(cfg)
    return stream_size(cfg)[0] / (x1 - x0)


def preview_size(cfg: Config) -> tuple[int, int]:
    """The published frame: the WHOLE scannable area, at stream resolution.

    Not the camera's streaming frame. The still reaches further than any
    streaming mode can -- a 16:9 stream of a 3:2 still is the full width with
    a centred crop -- so a frame the size of the stream can only ever show
    part of what a scan will capture, and the rest has to be conjured as
    padding whose size depends on whatever the crop marks happen to be doing.

    Sized to the still instead, this is a fixed frame: 853x1280 here against a
    720x1280 stream. The scannable area never changes shape, so neither does
    the border, whatever the paper sizes do. The stream is overlaid inside it
    at its own resolution, unscaled, and the last scan fills the rest.
    """
    sw, sh = upright_still(cfg)
    k = still_scale(cfg)
    return (int(round(sw * k)), int(round(sh * k)))


def stream_origin(cfg: Config) -> tuple[int, int]:
    """Where the live stream sits inside the published frame."""
    x0, y0, _x1, _y1 = visible_still_region(cfg)
    k = still_scale(cfg)
    return (int(round(x0 * k)), int(round(y0 * k)))


def turn_mark(mark: Mark, rotate_deg: int, sensor: tuple[int, int]) -> Mark:
    """Carry a mark from sensor coordinates into transposed ones.

    Marks are computed against the sensor's own framing, because that is what
    the measured 16:9-crop relationship describes. The video is then turned
    upright, so the rectangles have to make the same journey.
    """
    deg = rotate_deg % 360
    if deg == 0:
        return mark
    sw, sh = sensor
    x, y, w, h = mark.x, mark.y, mark.width, mark.height
    if deg == 90:      # counter-clockwise
        x, y, w, h = y, sw - x - w, h, w
    elif deg == 180:
        x, y = sw - x - w, sh - y - h
    else:              # 270, clockwise
        x, y, w, h = sh - y - h, x, h, w
    pw, ph = (sh, sw) if deg % 180 == 90 else (sw, sh)
    return Mark(
        name=mark.name, x=x, y=y, width=w, height=h,
        clipped_top=y < 0,
        clipped_bottom=y + h > ph,
        clipped_right=x + w > pw,
        clipped_left=x < 0,
    )


def raw_visible_still_region(cfg: Config) -> tuple[float, float, float, float]:
    """Which part of the WHOLE upright still the preview can show, in pixels.

    The largest centred rectangle of the mode's shape that fits the sensor.
    A mode WIDER than the sensor's 3:2 keeps the full width and crops rows; a
    mode TALLER than it keeps the full height and crops columns. Either way
    the field of view is a centred crop, never a rescale, so preview pixels
    map onto still pixels.

    The wide arm is measured: cross-correlating a 1280x720 frame against the
    still put the best match at row 67 where a centred crop predicts 66, RMS
    6.3, against 32.9 for "the whole frame squashed". The tall arm is the same
    mechanism on the other axis and is NOT separately measured -- 4:3 was only
    ever established to be a narrower horizontal field, which this predicts
    but does not confirm the extent of. See docs/ISSUES.md.

    Turn the camera and the crop turns with it, because the whole frame has.
    """
    sw, sh = cfg.capture.native_width, cfg.capture.native_height
    pw, ph = cfg.preview.width, cfg.preview.height
    if pw * sh >= ph * sw:          # mode is wider: full width, crop rows
        visible_w, visible_h = float(sw), sw * ph / pw
    else:                           # mode is taller: full height, crop columns
        visible_w, visible_h = sh * pw / ph, float(sh)
    x0, y0 = (sw - visible_w) / 2, (sh - visible_h) / 2
    if cfg.capture.rotate_deg % 180 == 90:
        # Sensor rows become upright columns, and columns become rows.
        return (y0, x0, y0 + visible_h, x0 + visible_w)
    return (x0, y0, x0 + visible_w, y0 + visible_h)


def visible_still_region(cfg: Config) -> tuple[float, float, float, float]:
    """The same band, measured from the scannable area's own origin.

    Which is where everything downstream measures from. Unfenced the two
    functions agree; fenced, the band's coordinates shift by the strip the
    fence removed, and on the fenced edge the band starts at zero.
    """
    bx0, by0, _bx1, _by1 = scannable_box(cfg)
    x0, y0, x1, y1 = raw_visible_still_region(cfg)
    return (x0 - bx0, y0 - by0, x1 - bx0, y1 - by0)


def streamed_mm(cfg: Config) -> tuple[float, float]:
    """How much of the coverage the LIVE stream shows, in mm.

    Smaller than the coverage, and reported so the difference between what
    moves and what is a still is stated rather than left to be noticed.
    """
    sw, sh = upright_still(cfg)
    cov_w, cov_h = cfg.rig.coverage_mm
    x0, y0, x1, y1 = visible_still_region(cfg)
    return ((x1 - x0) / sw * cov_w, (y1 - y0) / sh * cov_h)


def does_not_fit(cfg: Config) -> list[dict]:
    """Papers larger than the SCANNABLE area, with the numbers.

    Measured against the coverage, not against the streamed band: a sheet
    bigger than the stream but smaller than the still is captured perfectly
    well, it just cannot be watched live. Only a sheet bigger than the
    coverage is genuinely beyond the camera, and that is what needs saying.
    """
    usable_w, usable_h = cfg.rig.coverage_mm
    out = []
    for name, mm_w, mm_h in cfg.preview.papers:
        if cfg.preview.landscape:
            mm_w, mm_h = mm_h, mm_w
        # Half a millimetre of slack: a paper that fits exactly should not be
        # reported as overflowing on a rounding error.
        if mm_w > usable_w + 0.5 or mm_h > usable_h + 0.5:
            out.append({
                "name": name,
                "needs": [round(mm_w, 1), round(mm_h, 1)],
                "available": [round(usable_w, 1), round(usable_h, 1)],
            })
    return out


def marks(cfg: Config) -> list[Mark]:
    """Where each paper size lands in the published preview.

    Everything here is in UPRIGHT coordinates -- the space after
    capture.rotate_deg, which is the space imaging.render works in and the
    space the transposed video is published in. Computing marks in the
    sensor's own space instead meant the anchor grid pointed at a different
    corner than the scan used, once the camera was turned.

    If rig.coverage_mm is wrong, these marks are wrong in exactly the same way
    the scans are -- which is what makes them useful for calibrating it.
    """
    sw, sh = upright_still(cfg)
    cov_w, cov_h = cfg.rig.coverage_mm
    pw, ph = preview_size(cfg)
    # The canvas IS the still, so this is a straight still-to-canvas scale
    # with no origin to subtract. The clamp that used to anchor inside the
    # streamed band is gone with it: the coverage is the scannable area and
    # the frame now shows all of it, so anchoring against the coverage puts a
    # mark on an edge that is actually on screen.
    scale = pw / sw

    out = []
    for name, mm_w, mm_h in cfg.preview.papers:
        # Landscape turns the page, not the frame: the camera sees the same
        # physical area whichever way a sheet is lying on it.
        if cfg.preview.landscape:
            mm_w, mm_h = mm_h, mm_w
        off_x, off_y = anchor_offset_mm(cfg.rig.anchor, (cov_w, cov_h),
                                        (mm_w, mm_h))
        px = off_x / cov_w * sw * scale
        py = off_y / cov_h * sh * scale
        pwid = mm_w / cov_w * sw * scale
        phei = mm_h / cov_h * sh * scale
        out.append(
            Mark(
                name=name,
                x=int(round(px)), y=int(round(py)),
                width=int(round(pwid)), height=int(round(phei)),
                # Half a pixel of slack. A mark that fills the canvas exactly
                # -- which is what coverage_mm == the paper size means -- lands
                # on 853.0000001 against 853 and was reported as clipped.
                clipped_top=py < -0.5,
                clipped_bottom=py + phei > ph + 0.5,
                clipped_right=px + pwid > pw + 0.5,
                clipped_left=px < -0.5,
            )
        )
    return out


# Distinct hues that survive being drawn over paper and over a dark desk.
MARK_COLOURS = ("red", "lime", "cyan", "yellow", "magenta")

# Crop-mark line width, in published stream pixels, for every mark.
#
# One number, not a function of the mark's rank. Widths used to run from 6
# down to 2 so that sizes sharing an edge drew concentrically, which meant
# ticking a fourth paper size rethickened the first three, and a stream whose
# size follows the video mode drew the same rank at a different width on
# screen. Coincident edges only happen at the anchor, and two colours meeting
# there costs less than a line whose width means nothing.
MARK_THICKNESS = 3

# The union-of-papers box. Thinner than a mark, because it is a summary of
# them rather than one more of them.
UNION_THICKNESS = 2


def union_rect(marked: list[Mark]) -> tuple[int, int, int, int]:
    """The bounding box of every configured paper size, in preview pixels.

    This is what the scanner can actually be asked for, so it is the frame
    that matters: anything outside it will never appear in any scan, whatever
    paper is chosen.
    """
    if not marked:
        return (0, 0, 0, 0)
    left = min(m.x for m in marked)
    top = min(m.y for m in marked)
    right = max(m.x + m.width for m in marked)
    bottom = max(m.y + m.height for m in marked)
    return (left, top, right - left, bottom - top)


def video_rect(cfg: Config, scale: float, off_x: int, off_y: int
               ) -> tuple[int, int, int, int]:
    """Where the LIVE stream sits in the published frame.

    A sub-rectangle of the canvas, not the whole of it: the canvas is the
    scannable area and the stream only covers the middle of it. At scale 1
    the stream is placed at its own resolution and never resampled.
    """
    sx, sy = stream_origin(cfg)
    stw, sth = stream_size(cfg)
    return (int(round(sx * scale + off_x)), int(round(sy * scale + off_y)),
            int(round(stw * scale)), int(round(sth * scale)))


def _intersect(a: tuple[int, int, int, int],
               b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x, y = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    return (x, y, max(0, right - x), max(0, bottom - y))


def _outside_bands(cfg: Config, rect: tuple[int, int, int, int], colour: str,
                   clip: tuple[int, int, int, int] | None = None) -> list[str]:
    """Filled boxes covering everything outside `rect`, to dim the dead zone.

    Four bands rather than one shape with a hole, because drawbox has no
    concept of a hole. Bands that fall entirely off-screen are skipped, which
    is the normal case when the capture area is larger than the preview.
    """
    x, y, w, h = rect
    pw, ph = preview_size(cfg)
    candidates = [
        (0, 0, pw, y),                              # above
        (0, y + h, pw, ph - (y + h)),               # below
        (0, max(y, 0), x, min(h, ph)),              # left
        (x + w, max(y, 0), pw - (x + w), min(h, ph)),  # right
    ]
    if clip is not None:
        candidates = [_intersect(band, clip) for band in candidates]
    return [
        f"drawbox=x={bx}:y={by}:w={bw}:h={bh}:color={colour}:t=fill"
        for bx, by, bw, bh in candidates
        if bw > 0 and bh > 0 and bx < pw and by < ph
    ]


def outside_of(rect: tuple[int, int, int, int],
               container: tuple[int, int, int, int]
               ) -> list[tuple[int, int, int, int]]:
    """The parts of `rect` that lie outside `container`.

    Up to four bands, and they never overlap: the left and right bands take
    the mark's full height, the top and bottom bands only the width the two
    rectangles share. Overlapping bands would blend the tint twice and paint
    the corners darker than the edges, which reads as a fifth region rather
    than as one warning.
    """
    rx, ry, rw, rh = rect
    cx, cy, cw, ch = container
    cright, cbottom = cx + cw, cy + ch
    xa, xb = max(rx, cx), min(rx + rw, cright)
    bands = [
        (rx, ry, min(cx, rx + rw) - rx, rh),                       # left
        (max(cright, rx), ry, rx + rw - max(cright, rx), rh),      # right
        (xa, ry, xb - xa, min(cy, ry + rh) - ry),                  # above
        (xa, max(cbottom, ry), xb - xa,
         ry + rh - max(cbottom, ry)),                              # below
    ]
    return [b for b in bands if b[2] > 0 and b[3] > 0]


def fit_transform(cfg: Config, marked: list[Mark]) -> tuple[float, int, int]:
    """Make room for paper that runs past the SCANNABLE area entirely.

    A third zone, beyond the two the canvas already carries. The canvas is
    the scannable area and the stream sits inside it; this extends the
    picture further still, for a sheet larger than the camera can capture at
    all -- so its mark can be seen rather than clipped at the frame edge.

    Only on the sides it actually runs off. The anchor holds a mark flush
    against the edge it names, so that side never overflows and never gets an
    extension; the extension appears opposite, where the sheet is spilling.

    Nothing here depends on the streamed band any more. That is what stops
    the border changing size as paper sizes change: the scannable area is a
    fixed property of the camera, and only genuinely-oversized paper moves
    anything.

    `preview.max_pad` caps the extension as a fraction of the frame, so a
    wildly oversized sheet cannot shrink the picture to a postage stamp; past
    the cap the mark clips and the sidebar says so in millimetres.

    Returns (scale, x offset, y offset); scale is never above 1, since there
    is no reason to enlarge a picture that already fits.
    """
    pw, ph = preview_size(cfg)
    if not marked or not cfg.preview.fit_marks:
        return (1.0, 0, 0)

    # How far the marks run off each side, in unzoomed preview pixels.
    left = max(0, -min(m.x for m in marked))
    top = max(0, -min(m.y for m in marked))
    right = max(0, max(m.x + m.width for m in marked) - pw)
    bottom = max(0, max(m.y + m.height for m in marked) - ph)
    if not (left or top or right or bottom):
        return (1.0, 0, 0)

    scale = min(pw / (pw + left + right), ph / (ph + top + bottom), 1.0)
    # Never shrink past the cap: a border wide enough to swallow the picture
    # tells you less about where the paper goes than a clipped mark does.
    scale = max(scale, 1.0 / (1.0 + max(0.0, cfg.preview.max_pad)))
    if scale >= 0.999:
        return (1.0, 0, 0)

    # Padding apportioned to the sides that overflow -- NOT simply
    # `overflow * scale`. One axis usually sets the scale and leaves the other
    # with slack it never asked for, and giving that slack entirely to one
    # side put 7 pixels above the picture and 269 below it on a mark whose
    # vertical overflow was centred.
    #
    # Centre the picture, then shift it by half the imbalance in the overflow.
    # On the axis that set the scale, slack == before + after and this returns
    # exactly `before`, which is the whole overflow going where it is needed.
    # On the slack axis it stays near the middle. Apportioning by the RATIO
    # instead multiplied that axis's rounding error by slack/(before + after):
    # an overflow of 9 against 10, which is one pixel of rounding on a
    # symmetric mark, moved 183 pixels of slack to 87 against 96.
    def share(before: int, after: int, extent: int) -> int:
        slack = extent - int(round(extent * scale))
        if before + after <= 0:
            return slack // 2       # no overflow this way: no preference
        return max(0, min(slack, int(round((slack + before - after) / 2))))

    return (scale, share(left, right, pw), share(top, bottom, ph))


def place(mark: Mark, scale: float, off_x: int, off_y: int) -> Mark:
    """Move a mark into the zoomed-out frame alongside the picture."""
    if scale == 1.0 and not off_x and not off_y:
        return mark
    return Mark(
        name=mark.name,
        x=int(round(mark.x * scale + off_x)),
        y=int(round(mark.y * scale + off_y)),
        width=int(round(mark.width * scale)),
        height=int(round(mark.height * scale)),
        clipped_top=mark.clipped_top,
        clipped_bottom=mark.clipped_bottom,
        clipped_right=mark.clipped_right,
        clipped_left=mark.clipped_left,
    )


def geometry_chain(cfg: Config) -> str:
    """Place the live stream inside the canvas. Draw nothing on it.

    Split from the marks so the last scan can be composited in between: the
    ghost belongs above the placed stream and below the crop marks, or the
    marks end up underneath it.

    The pad is unconditional now. The canvas is the scannable area and the
    stream only covers the middle of it, so there is always a border to place
    the stream within -- which is the point, since that border is where the
    scan reaches and the live view does not.
    """
    scale, off_x, off_y = fit_transform(cfg, marks(cfg))
    cw, ch = preview_size(cfg)
    stw, sth = stream_size(cfg)
    vx, vy, vw, vh = video_rect(cfg, scale, off_x, off_y)

    parts = []
    # Turn the picture upright first, so everything after this -- the marks,
    # the loopback, the web page -- is in one coordinate space: the one the
    # scanner works in. Compensating for a turned camera downstream instead
    # is what produced three separate orientation bugs.
    turn = TRANSPOSE.get(cfg.capture.rotate_deg % 360)
    if turn:
        parts.append(turn)

    # Only when an oversized mark has forced the whole thing smaller. With no
    # such mark the stream is placed at its own resolution and never resampled.
    if (vw, vh) != (stw, sth):
        parts.append(f"scale=w={vw}:h={vh}")
    parts.append(
        f"pad=w={cw}:h={ch}:x={vx}:y={vy}:color={cfg.preview.pad_colour}")
    return ",".join(parts)


def overlay_chain(cfg: Config) -> str:
    """Everything drawn ON the picture: the dead zone, the marks, the labels."""
    raw = marks(cfg)
    scale, off_x, off_y = fit_transform(cfg, raw)
    marked = [place(m, scale, off_x, off_y) for m in raw]
    pw, ph = preview_size(cfg)

    parts: list[str] = []
    # Dead zone next, so the marks and labels draw on top of it.
    #
    # Clipped to the picture. The dimming means "the camera sees this but no
    # scan can reach it", which is a statement about live video; the border
    # outside the picture is a different thing entirely and carries the
    # faint last scan, so dimming it would grey out the very content that
    # makes it worth having.
    live = video_rect(cfg, scale, off_x, off_y)
    rect = union_rect(marked)
    parts += _outside_bands(cfg, rect, cfg.preview.outside_colour, clip=live)
    ux, uy, uw, uh = rect
    parts.append(
        f"drawbox=x={ux}:y={uy}:w={uw}:h={uh}"
        f":color=white@0.85:t={UNION_THICKNESS}"
    )

    # The canvas is the scannable area, so anything of a mark outside it is
    # sheet the scan will not reach. Tinted before the marks are drawn, so a
    # mark's own line stays on top of its warning and the colour still says
    # which paper size is overflowing.
    canvas = ghost_rect(cfg, scale, off_x, off_y)
    for m in marked:
        for bx, by, bw, bh in outside_of(
                (m.x, m.y, m.width, m.height), canvas):
            parts.append(
                f"drawbox=x={bx}:y={by}:w={bw}:h={bh}"
                f":color={cfg.preview.overflow_colour}:t=fill"
            )

    for i, m in enumerate(marked):
        colour = MARK_COLOURS[i % len(MARK_COLOURS)]
        parts.append(
            f"drawbox=x={m.x}:y={m.y}:w={m.width}:h={m.height}"
            f":color={colour}@0.9:t={MARK_THICKNESS}"
        )
        # Keep the label on screen when the box starts above the frame, and
        # stagger them: marks that share a top edge would otherwise stack
        # their labels in one illegible pile.
        label_y = max(m.y + 6, 6) + i * 34
        parts.append(
            f"drawtext=text='{m.name}':x={m.x + 12}:y={label_y}"
            f":fontsize=26:fontcolor={colour}:box=1:boxcolor=black@0.5:boxborderw=4"
        )
    return ",".join(parts)


def filter_chain(cfg: Config) -> str:
    """The whole chain, for the no-ghost case and for inspection in tests."""
    return ",".join(p for p in (geometry_chain(cfg), overlay_chain(cfg)) if p)


def ghost_rect(cfg: Config, scale: float, off_x: int, off_y: int
               ) -> tuple[int, int, int, int]:
    """Where the WHOLE still lands: the canvas itself.

    The published frame is sized to the scannable area, so the still fills it
    exactly -- no arithmetic, and nothing left over. That is what makes the
    ghost always mappable: the camera has not moved, so the still is the
    canvas, whatever the paper sizes are doing.
    """
    cw, ch = preview_size(cfg)
    return (off_x, off_y,
            max(1, int(round(cw * scale))), max(1, int(round(ch * scale))))


def ghost_path(cfg: Config) -> Path:
    """Where the composited border image lives, ready for ffmpeg to overlay."""
    from . import config as config_mod

    return config_mod.state_dir(cfg) / "preview-ghost.png"


def scan_still_path(cfg: Config) -> Path:
    """Where the last captured still is kept, in the camera's own orientation.

    Kept as well as the composited overlay, and this is the one that matters:
    the overlay is baked against one geometry, but the STILL is a photograph
    of the desk and stays true whatever the settings do. Tick a paper size and
    only the placement moves -- the picture is as valid as it was a second
    ago, so it is re-composited rather than thrown away and waited for.

    Stored unrotated so a change to `capture.rotate_deg` can be honoured by
    orienting it again, rather than invalidating it.
    """
    from . import config as config_mod

    return config_mod.state_dir(cfg) / "preview-scan.jpg"


# The ghost is never drawn much wider than the published frame -- the still
# reaches about 1/0.844 of it -- so keeping a full 2304x1536 costs scan time
# and disk for detail no one will see.
STILL_STORE_MAX = 1600


def save_scan_still(cfg: Config, raw) -> Path | None:
    """Keep the captured still so the border can be redrawn without a rescan."""
    if not cfg.preview.scan_ghost:
        return None
    path = scan_still_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = raw.convert("RGB")
    if max(image.size) > STILL_STORE_MAX:
        image = image.copy()
        image.thumbnail((STILL_STORE_MAX, STILL_STORE_MAX))
    tmp = path.with_suffix(".jpg.tmp")
    image.save(tmp, "JPEG", quality=85)
    tmp.replace(path)
    return path


def rebuild_ghost(cfg: Config) -> Path | None:
    """Re-composite the border from the stored still, at the current geometry.

    Called after a scan and after any settings change. A settings change moves
    where the still belongs; it does not make the still wrong.
    """
    from PIL import Image

    still = scan_still_path(cfg)
    if not cfg.preview.scan_ghost or not still.exists():
        clear_ghost(cfg)
        return None
    try:
        with Image.open(still) as stored:
            frame = to_scannable(cfg, stored.convert("RGB"))
    except OSError as exc:
        log.warning("could not read the stored scan still: %s", exc)
        clear_ghost(cfg)
        return None
    return write_ghost(cfg, frame)


def _rgb(colour: str) -> tuple[int, int, int]:
    """Parse an ffmpeg colour well enough for the padding. Hex, or give up."""
    text = colour.split("@")[0].strip().lstrip("#")
    if len(text) == 6:
        try:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
        except ValueError:
            pass
    return (215, 210, 200)


def write_ghost(cfg: Config, frame) -> Path | None:
    """Composite the last scan into the border, and leave a hole for the video.

    A hole rather than compositing the video in: the overlay is drawn ON TOP
    of the live picture by ffmpeg, so anything opaque here would cover the
    thing being previewed. Transparent over `video_rect` means the ghost
    paints only where there is no live picture to cover.

    Returns None when there is nothing to draw -- no border, or the feature
    turned off -- so the caller knows to run without the extra input.
    """
    if not cfg.preview.scan_ghost:
        clear_ghost(cfg)
        return None
    scale, off_x, off_y = fit_transform(cfg, marks(cfg))
    # NOT conditional on the zoom. The region outside the live stream is a
    # permanent fact of the camera -- the still always reaches further than
    # any streaming mode -- so there is always somewhere for the ghost to go,
    # whatever the paper sizes happen to be doing. Tying this to the zoom
    # meant the ghost was deleted the moment nothing overflowed, and a
    # perfectly good photograph of the desk was thrown away.
    if video_rect(cfg, scale, off_x, off_y) == ghost_rect(cfg, scale, off_x, off_y):
        clear_ghost(cfg)            # the stream covers everything: nothing to add
        return None

    from PIL import Image

    pw, ph = preview_size(cfg)
    pad = _rgb(cfg.preview.pad_colour)
    canvas = Image.new("RGBA", (pw, ph), (*pad, 255))

    gx, gy, gw, gh = ghost_rect(cfg, scale, off_x, off_y)
    shrunk = frame.convert("RGB").resize((gw, gh), Image.LANCZOS)
    # Washed back towards the padding rather than merely made transparent, so
    # it reads as a note about what is out there and never as live video.
    opacity = min(1.0, max(0.0, cfg.preview.scan_ghost_opacity))
    washed = Image.blend(Image.new("RGB", (gw, gh), pad), shrunk, opacity)
    canvas.paste(washed, (gx, gy))

    vx, vy, vw, vh = video_rect(cfg, scale, off_x, off_y)
    canvas.paste((0, 0, 0, 0), (vx, vy, vx + vw, vy + vh))

    path = ghost_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".png.tmp")
    canvas.save(tmp, "PNG")
    tmp.replace(path)               # atomic: ffmpeg must never read a half file
    log.info("scan ghost written to %s (%dx%d at %d,%d)", path, gw, gh, gx, gy)
    return path


def clear_ghost(cfg: Config) -> None:
    """Throw the ghost away, because the geometry it was composited for has gone.

    It is a flat picture baked against one coverage, rotation and zoom. Change
    any of those and every pixel of it is in the wrong place -- and being
    faint, it would sit there misaligned looking plausible rather than
    obviously broken. The next scan draws a correct one.
    """
    try:
        ghost_path(cfg).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove the stale scan ghost: %s", exc)


def usable_ghost(cfg: Config) -> str | None:
    """The ghost file, if one exists. Never gated on the zoom.

    There is always a region outside the live stream for it to occupy, so the
    only question is whether a scan has happened yet.
    """
    if not cfg.preview.scan_ghost:
        return None
    path = ghost_path(cfg)
    return str(path) if path.exists() else None


def build_command(cfg: Config, camera: str | None = None,
                  loopback: str | None = None,
                  ghost: str | None = None) -> list[str]:
    """The capture pipeline: camera in, marked-up video out.

    Up to two outputs from one camera read -- the loopback device that
    ordinary webcam apps open, and the MJPEG pipe this process serves over
    HTTP. Reading the camera twice is not an option: access is exclusive.

    `camera` and `loopback` are RESOLVED device paths. They are arguments
    rather than looked up here so this stays a pure function of its inputs --
    testable without hardware, and with one place (`_start_locked`) that
    touches the device tree. Passing neither falls back to the raw config
    specs, which is only useful for inspecting the shape of the command.
    """
    # Requested from the camera in its own orientation; the transpose in the
    # filter chain is what turns the published frame upright.
    size = f"{cfg.preview.width}x{cfg.preview.height}"
    argv = [
        "ffmpeg", "-loglevel", "error",
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", size, "-framerate", str(cfg.preview.fps),
        "-i", camera or cfg.capture.device,
    ]
    sink = cfg.preview.loopback_device if loopback is None else loopback

    if not ghost:
        vf = filter_chain(cfg)
        if sink:
            argv += ["-vf", vf, "-pix_fmt", "yuv420p", "-f", "v4l2", sink]
        argv += ["-vf", vf, "-c:v", "mjpeg", "-q:v", "6", "-f", "mjpeg", "pipe:1"]
        return argv

    # The ghost is a still, looped forever. The camera is the OVERLAY BASE, so
    # output timing follows the camera and not this image -- an image input
    # used as the base drives the whole graph at its own rate.
    argv += ["-loop", "1", "-framerate", str(cfg.preview.fps), "-i", ghost]

    geometry = geometry_chain(cfg)
    drawn = overlay_chain(cfg)
    graph = f"[0:v]{geometry}[live];" if geometry else "[0:v]null[live];"
    graph += "[1:v]format=rgba[ghost];[live][ghost]overlay=0:0:format=auto"
    if drawn:
        graph += f",{drawn}"
    labels = ["[out0]", "[out1]"] if sink else ["[out0]"]
    graph += f",split={len(labels)}" + "".join(labels)
    argv += ["-filter_complex", graph]

    if sink:
        argv += ["-map", labels[0], "-pix_fmt", "yuv420p", "-f", "v4l2", sink]
    argv += ["-map", labels[-1], "-c:v", "mjpeg", "-q:v", "6",
             "-f", "mjpeg", "pipe:1"]
    return argv


class PreviewStream:
    """Owns the camera between scans, and gets out of the way for one."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._latest: bytes | None = None
        self._seq = 0
        self._cond = threading.Condition()
        self._running = False
        # Why the preview is not showing anything, in a sentence a human can
        # act on. The whole reason this class grew a diagnostic surface: a
        # dead ffmpeg used to leave `_running` True, stderr went to DEVNULL,
        # and the daemon logged "preview streaming 1280x720" either way.
        self._last_error: str | None = None
        self._stderr_tail: list[str] = []
        self._exit_code: int | None = None
        self._resolved: dict[str, str] = {}
        self._frames_seen = 0
        self._last_frame_at: float | None = None
        self._started_at: float | None = None
        # True while the camera is lent out for a capture. Distinguishes "back
        # shortly" from "stopped", which is the difference between an open
        # MJPEG response waiting and an open MJPEG response ending.
        self._paused = False
        # Held for the whole of a capture, so two scans cannot race each
        # other into restarting the stream underneath one another.
        self._camera = threading.RLock()

    # -- lifecycle ------------------------------------------------------

    def start(self, wait: float = 0.0) -> bool:
        """Start the pipeline. With `wait`, block until it is proven up or down.

        `wait` is 0 on the hot path -- `released()` restarts the stream at the
        end of every scan, and the measured resume there is 0.00s, which is
        not worth spending to learn something the reader thread reports a
        moment later anyway. Startup and the GUI's Start button pass a real
        timeout, because both have someone waiting for a yes or a no.
        """
        if not self._cfg.preview.enable:
            self._last_error = "preview is disabled in the config"
            return False
        with self._camera:
            started = self._start_locked()
        if started and wait > 0:
            return self.wait_ready(wait)
        return started

    def _resolve_devices(self) -> tuple[str, str] | None:
        """Look the device numbers up fresh. None means say why and give up."""
        try:
            camera = devices.resolve(self._cfg.capture.device, "capture")
        except devices.DeviceError as exc:
            self._last_error = f"no camera: {exc}"
            log.error("preview cannot start: %s", self._last_error)
            return None

        loopback = ""
        if self._cfg.preview.loopback_device:
            try:
                loopback = devices.resolve(
                    self._cfg.preview.loopback_device, "output")
            except devices.DeviceError as exc:
                # Not fatal. The loopback is a convenience for Kamoso and the
                # like; the web preview and every scan work without it, so a
                # missing one degrades rather than stops.
                log.warning("preview loopback unavailable, continuing "
                            "web-only: %s", exc)
                self._last_error = f"loopback unavailable: {exc}"
        self._resolved = {"camera": camera, "loopback": loopback}
        return camera, loopback

    def _start_locked(self) -> bool:
        if self._running:
            return True
        self._exit_code = None
        self._stderr_tail = []
        self._last_error = None
        self._frames_seen = 0
        self._last_frame_at = None

        resolved = self._resolve_devices()
        if resolved is None:
            return False
        camera, loopback = resolved

        # Picked up here rather than held in memory, so the last scan survives
        # a daemon restart and the border is not blank until the next one.
        argv = build_command(self._cfg, camera, loopback, usable_ghost(self._cfg))
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                # NOT DEVNULL. This is the only channel that ever says what
                # went wrong -- "Not a video capture device" spent a whole
                # session being written to /dev/null while the daemon
                # cheerfully logged that it was streaming.
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self._last_error = f"could not run ffmpeg: {exc}"
            log.error("preview could not start: %s", exc)
            self._proc = None
            return False

        self._running = True
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._read_frames, daemon=True)
        self._thread.start()
        # Drained continuously, not read at exit: a full pipe buffer would
        # block ffmpeg itself, and the interesting lines arrive at startup.
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, args=(self._proc,), daemon=True)
        self._stderr_thread.start()
        log.info(
            "preview starting %dx%d at %d fps from %s%s",
            self._cfg.preview.width, self._cfg.preview.height,
            self._cfg.preview.fps, camera,
            f", published to {loopback}" if loopback else " (web only)",
        )
        return True

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Block until a first frame arrives, or the pipeline dies. True = up.

        A started process is not a working preview: the failure that prompted
        all this was ffmpeg exiting 237 a few milliseconds after a perfectly
        successful `Popen`. The honest test of "is the preview up" is whether
        a frame came out of it.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._latest is None and self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if self._latest is not None:
                return True
        if self._running:
            self._last_error = (
                f"no frame within {timeout:.0f}s of starting, though ffmpeg "
                "is still alive"
            )
        return False

    def _stop_locked(self) -> None:
        if not self._running:
            return
        self._running = False
        proc, self._proc = self._proc, None
        started = time.monotonic()
        if proc is not None:
            # SIGKILL outright. There is nothing to flush -- both outputs are
            # live streams, not files being finalised -- and asking politely
            # cost real time: SIGTERM left ffmpeg running until the timeout
            # expired, measured at 5.26s of dead air on every single scan,
            # and SIGINT was no better at 1.72s. That was most of the delay
            # between pressing scan and seeing a page.
            proc.kill()
            try:
                # Wait for it to actually go, not merely be asked. The device
                # is not free until it has, and the capture that follows
                # would get EBUSY.
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                log.warning("preview process would not die")
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None
        with self._cond:
            self._latest = None
            self._started_at = None
            # A stop asked for is not a fault. Clearing this is what keeps the
            # GUI from showing the last crash forever after a clean restart.
            self._last_error = None
            self._cond.notify_all()
        log.debug("preview stopped in %.2fs", time.monotonic() - started)

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

    # Enough to carry an ffmpeg failure, not enough to grow without bound on
    # a pipeline that warns once a frame.
    STDERR_LINES = 40

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        """Keep the tail of ffmpeg's stderr, so a failure can be reported."""
        pipe = proc.stderr
        if pipe is None:
            return
        try:
            for raw in pipe:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                with self._cond:
                    self._stderr_tail.append(line)
                    del self._stderr_tail[:-self.STDERR_LINES]
        except (OSError, ValueError):
            pass  # pipe closed under us; the exit code carries the story

    def _note_exit(self) -> None:
        """Record why the pipeline stopped, once the process has gone.

        Called from the reader thread when stdout reaches EOF. `_running` must
        go False here: leaving it True is what made `/preview/stream` answer
        200 and then block for its full stall timeout, and made the GUI retry
        every 31 seconds without ever reporting a reason.
        """
        proc = self._proc
        if proc is None:
            return
        try:
            code = proc.wait(timeout=2)   # also reaps; there was a zombie here
        except subprocess.TimeoutExpired:
            return
        with self._cond:
            self._exit_code = code
            if self._running:
                self._running = False
                tail = "; ".join(self._stderr_tail[-3:])
                self._last_error = (
                    f"ffmpeg exited {code}" + (f": {tail}" if tail else "")
                )
                log.error("preview pipeline died: %s", self._last_error)
            self._cond.notify_all()

    def _read_frames(self) -> None:
        """Slice JPEGs out of the MJPEG stream as they arrive."""
        buf = bytearray()
        stdout = self._proc.stdout if self._proc else None
        if stdout is None:
            return
        try:
            while self._running:
                # read1, not read. `read` on a BufferedReader blocks until it
                # has the full 16384 bytes or the pipe closes, so a first
                # frame is reported only once that much has piled up behind
                # it. At 15fps of 70KB frames that is invisible, but it makes
                # "has a frame arrived yet" depend on volume rather than on
                # arrival -- and that is exactly the question `wait_ready`
                # asks to decide whether the preview is up.
                chunk = stdout.read1(16384)
                if not chunk:
                    # EOF. Either a deliberate stop, or ffmpeg fell over --
                    # _note_exit tells the two apart and clears _running.
                    self._note_exit()
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
                        self._frames_seen += 1
                        self._last_frame_at = time.monotonic()
                        self._cond.notify_all()
        except (OSError, ValueError) as exc:
            if self._running:
                log.warning("preview stream ended: %s", exc)
        finally:
            with self._cond:
                self._cond.notify_all()

    def next_frame(self, skip: int = 1, timeout: float = 2.0) -> bytes | None:
        """Wait for a frame that STARTED after this call, and return it.

        `latest` hands back whatever is already buffered, which is the wrong
        frame for a metering loop: it was captured before the control being
        measured was set, so the loop reads the picture it was trying to
        change and converges on nothing. Skipping ahead by sequence number
        costs a couple of frame intervals -- about 130ms at 15fps -- and
        makes each measurement correspond to the setting that produced it.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            target = self._seq + max(1, skip)
            while self._seq < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not (self._running or self._paused):
                    break
                self._cond.wait(remaining)
            return self._latest

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

    @property
    def healthy(self) -> bool:
        """Running AND actually producing pictures.

        `running` only ever meant "we started a process", and that was the
        whole bug: it stayed True with a dead ffmpeg behind it. Callers that
        want to know whether a viewer will see anything want this.
        """
        return self._running and self._latest is not None

    def status(self) -> dict:
        """Everything needed to say why the preview is or is not working."""
        from . import config as config_mod

        now = time.monotonic()
        with self._cond:
            proc = self._proc
            state = {
                "enabled": self._cfg.preview.enable,
                "running": self._running,
                "healthy": self._running and self._latest is not None,
                "pid": proc.pid if proc is not None else None,
                "exit_code": self._exit_code,
                "error": self._last_error,
                "stderr_tail": list(self._stderr_tail[-8:]),
                "frames_seen": self._frames_seen,
                "resolved": dict(self._resolved),
                "uptime_s": (round(now - self._started_at, 1)
                             if self._started_at else None),
                "last_frame_age_s": (round(now - self._last_frame_at, 1)
                                     if self._last_frame_at else None),
                "size": dict(zip(("width", "height"), preview_size(self._cfg))),
                "fps": self._cfg.preview.fps,
            }

        # Outside the lock: these shell out to the device tree and there is no
        # reason to hold a frame-delivery lock while they do.
        state["devices"] = config_mod.device_report(self._cfg)
        state["inventory"] = devices.inventory()
        camera = state["resolved"].get("camera")
        if camera:
            from . import capture as capture_mod

            state["geometry_controls"] = capture_mod.geometry_controls(
                camera, self._cfg.capture.timeout_s)
        state["summary"] = self._summary(state)
        return state

    @staticmethod
    def _summary(state: dict) -> str:
        """One line, written to be read by someone who is not a programmer."""
        if not state["enabled"]:
            return "Preview is disabled in the configuration."
        broken = [d for d in state["devices"] if not d["ok"]]
        # Loud even when everything else is fine: a panned or zoomed camera
        # produces a picture that looks perfectly healthy and crop marks that
        # are all in the wrong place.
        moved = [name for name, c in (state.get("geometry_controls") or {}).items()
                 if not c["at_default"]]
        if moved and state["healthy"]:
            return (f"Streaming, but {', '.join(moved)} is not at its default: "
                    "every crop mark will be misplaced until it is reset.")
        if state["healthy"]:
            note = f"Streaming from {state['resolved'].get('camera', '?')}"
            sink = state["resolved"].get("loopback")
            note += f", published to {sink}." if sink else " (web only)."
            if broken:
                note += f" {len(broken)} configured device still unresolved."
            return note
        if state["error"]:
            return state["error"]
        if broken:
            return broken[0]["detail"] or "A configured device is missing."
        if state["running"]:
            return "Started, but no frame has arrived yet."
        return "Preview is not running."
