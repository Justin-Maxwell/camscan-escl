"""Frame -> page: crop, pad, scale, colour-convert, encode (spec §5).

The one invariant this module exists to hold: the returned JPEG's pixel
dimensions equal `region x resolution / 300`, exactly, always.
"""

from __future__ import annotations

import io
import logging

from PIL import Image

from .escl import ScanSettings

log = logging.getLogger(__name__)

WHITE_RGB = (255, 255, 255)


# Where a scan region sits inside the frame's coverage. eSCL regions are
# expressed from an origin, and the client always sends 0,0 -- so without
# this every scan is pinned to the top left of what the camera sees, and the
# page has to be put there. A rig usually wants the page in the middle.
ANCHORS = {
    "top-left": (0.0, 0.0), "top": (0.5, 0.0), "top-right": (1.0, 0.0),
    "left": (0.0, 0.5), "center": (0.5, 0.5), "right": (1.0, 0.5),
    "bottom-left": (0.0, 1.0), "bottom": (0.5, 1.0), "bottom-right": (1.0, 1.0),
}


def anchor_offset_mm(
    anchor: str,
    coverage_mm: tuple[float, float],
    region_mm: tuple[float, float],
) -> tuple[float, float]:
    """How far to shift a region of this size to sit at `anchor`, in mm.

    Against the whole coverage, which is the whole scannable area. There was
    briefly a clamp here that anchored inside the streamed band instead: the
    preview frame used to be the size of the camera's streaming mode, which
    is smaller than the still, so an edge-anchored mark landed off-screen and
    the clamp dragged it back into view -- at the cost of an edge-anchored
    scan using only the part of the sensor the stream covered.

    The preview frame is now sized to the scannable area and shows all of it,
    so the mark is on screen where it always belonged and the clamp is gone.

    Negative when the region is larger than the coverage, which keeps an
    oversized request centred on what the camera can see rather than dumping
    the overflow on one side.
    """
    fx, fy = ANCHORS.get(anchor, ANCHORS["top-left"])
    return ((coverage_mm[0] - region_mm[0]) * fx,
            (coverage_mm[1] - region_mm[1]) * fy)


def render(
    frame: Image.Image,
    settings: ScanSettings,
    coverage_mm: tuple[float, float],
    jpeg_quality: int,
    anchor: str = "top-left",
) -> bytes:
    """Map the requested ScanRegion onto the frame and encode it as JPEG."""
    frame = frame.convert("RGB")
    src_w, src_h = frame.size
    cov_w_mm, cov_h_mm = coverage_mm

    px_per_mm_x = src_w / cov_w_mm
    px_per_mm_y = src_h / cov_h_mm

    x_mm, y_mm, w_mm, h_mm = settings.region.mm
    off_x, off_y = anchor_offset_mm(anchor, coverage_mm, (w_mm, h_mm))
    x_mm += off_x
    y_mm += off_y

    # The requested region as a rectangle in source pixels. It may fall wholly
    # or partly outside the frame -- the page genuinely was not in shot there,
    # so those pixels become white rather than stretched content.
    left = x_mm * px_per_mm_x
    top = y_mm * px_per_mm_y
    right = (x_mm + w_mm) * px_per_mm_x
    bottom = (y_mm + h_mm) * px_per_mm_y

    canvas_w = max(1, round(right - left))
    canvas_h = max(1, round(bottom - top))
    canvas = Image.new("RGB", (canvas_w, canvas_h), WHITE_RGB)

    # Intersection of the requested rectangle with the real frame.
    ix0, iy0 = max(0, int(left)), max(0, int(top))
    ix1, iy1 = min(src_w, int(round(right))), min(src_h, int(round(bottom)))
    if ix1 > ix0 and iy1 > iy0:
        canvas.paste(frame.crop((ix0, iy0, ix1, iy1)), (ix0 - int(left), iy0 - int(top)))
    else:
        log.warning(
            "requested region falls entirely outside the camera coverage; "
            "returning a blank page"
        )

    target = settings.expected_size
    if canvas.size != target:
        canvas = canvas.resize(target, Image.LANCZOS)

    if settings.color_mode == "Grayscale8":
        canvas = canvas.convert("L")

    assert canvas.size == target, "dimension contract violated"

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    return buf.getvalue()


def orient(frame: Image.Image, rotate_deg: int) -> Image.Image:
    """Rotate the raw frame into the orientation rig.coverage_mm describes."""
    if rotate_deg % 360 == 0:
        return frame
    return frame.rotate(rotate_deg % 360, expand=True)
