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


def render(
    frame: Image.Image,
    settings: ScanSettings,
    coverage_mm: tuple[float, float],
    jpeg_quality: int,
) -> bytes:
    """Map the requested ScanRegion onto the frame and encode it as JPEG."""
    frame = frame.convert("RGB")
    src_w, src_h = frame.size
    cov_w_mm, cov_h_mm = coverage_mm

    px_per_mm_x = src_w / cov_w_mm
    px_per_mm_y = src_h / cov_h_mm

    x_mm, y_mm, w_mm, h_mm = settings.region.mm

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
