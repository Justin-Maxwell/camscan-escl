"""The anchored edge, and the strip of sensor it removes.

The rig registers a sheet by pushing it against a rail. The crop mark for
that sheet is drawn flush against the scannable area's edge, so the rail and
that edge have to be the same line -- otherwise the mark is drawn somewhere
the sheet cannot be put, and there is nothing to watch it land against.

Take the whole still as the scannable area and they are not the same line. A
16:9 stream of a 3:2 still is the still's full width with a centred vertical
crop, so 120 rows at each end are scannable and never streamed, and the
scannable area's edge sits 120 rows outside the picture. The scannable area
therefore drops the strip on the anchored edge.

Which strip goes is read off `rig.anchor` and nothing else. There is no
setting: an anchor names an edge, and there is no rig on which an anchor
names an edge and the strip outside it should be kept.
"""

import pytest
from dataclasses import replace
from PIL import Image

from camscan_escl import imaging, preview
from camscan_escl.config import Config, RigConfig
from camscan_escl.escl import ScanRegion, ScanSettings

STRIP = 120          # sensor rows outside the streamed band, per edge
BAND = 1296          # streamed rows of the still's 1536


def rig(anchor="top-left", rotate=90, **kw):
    """A config with the camera turned portrait and a given anchor."""
    return replace(
        Config(),
        capture=replace(Config().capture, rotate_deg=rotate),
        rig=replace(RigConfig(), anchor=anchor, **kw),
    )


# -- the equality the whole thing exists for ---------------------------------


@pytest.mark.parametrize("anchor,rotate,axis", [
    ("top-left", 90, 0),     # turned: strips are at the sides
    ("left", 90, 0),
    ("bottom-left", 90, 0),
    ("top-left", 0, 1),      # upright: strips are top and bottom
    ("top", 0, 1),
    ("top-right", 0, 1),
])
def test_the_anchored_edge_of_the_scan_is_the_edge_of_the_picture(
        anchor, rotate, axis):
    cfg = rig(anchor=anchor, rotate=rotate)
    assert preview.fence_edge(cfg) == "low"
    # The band now starts at zero on that axis: the scannable area's edge and
    # the live picture's edge are the same line.
    assert preview.visible_still_region(cfg)[axis] == pytest.approx(0.0)
    assert preview.stream_origin(cfg)[axis] == 0


@pytest.mark.parametrize("anchor,rotate,axis", [
    ("top-right", 90, 0),
    ("right", 90, 0),
    ("bottom-left", 0, 1),
    ("bottom", 0, 1),
])
def test_an_anchor_on_the_far_edge_trims_the_other_end(anchor, rotate, axis):
    cfg = rig(anchor=anchor, rotate=rotate)
    assert preview.fence_edge(cfg) == "high"
    span = preview.upright_still(cfg)[axis]
    # Flush against the high edge instead, so the band ends where the
    # scannable area ends.
    assert preview.visible_still_region(cfg)[axis + 2] == pytest.approx(span)


def test_the_registration_edge_of_the_anchored_mark_is_on_screen():
    """The mark's anchored edge lands at zero, not 120 px off the picture."""
    cfg = rig(anchor="top-left", coverage_mm=(210.0, 341.7))
    a4 = next(m for m in preview.marks(cfg) if m.name == "A4")
    assert (a4.x, a4.y) == (0, 0)
    assert not a4.clipped_top
    # And it is inside the live stream, which is the point: the stream starts
    # at zero too, so the edge is being watched rather than inferred.
    assert preview.stream_origin(cfg)[0] == 0


def test_every_mark_shares_the_anchored_edge_and_all_are_on_picture():
    """Several papers anchored left all register against the same visible
    line -- which is the arrangement the rig actually uses."""
    cfg = rig(anchor="left", coverage_mm=(210.0, 341.7))
    marked = preview.marks(cfg)
    assert len(marked) > 1
    assert {m.x for m in marked} == {0}
    assert preview.stream_origin(cfg)[0] == 0


# -- what it costs -----------------------------------------------------------


def test_it_costs_one_strip_and_keeps_the_far_one():
    cfg = rig(anchor="left")
    assert preview.raw_upright_still(cfg) == (1536, 2304)
    assert preview.upright_still(cfg) == (1536 - STRIP, 2304)
    # The far strip survives: still scannable, still not streamed.
    x0, _y0, x1, _y1 = preview.visible_still_region(cfg)
    assert (x1 - x0) == pytest.approx(BAND)
    assert preview.upright_still(cfg)[0] - x1 == pytest.approx(STRIP)


def test_sensor_scannable_is_orientation_independent():
    """The strip always comes off the sensor's row axis, whichever end."""
    for anchor in ("top-left", "top-right"):
        for rotate in (0, 90, 180, 270):
            cfg = rig(anchor=anchor, rotate=rotate)
            if preview.fence_edge(cfg) is None:
                continue
            assert preview.sensor_scannable(cfg) == (2304, 1536 - STRIP)


# -- a centred anchor registers against nothing, and keeps both strips -------


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_a_centred_anchor_leaves_the_geometry_untouched(rotate):
    cfg = rig(anchor="center", rotate=rotate)
    assert preview.fence_edge(cfg) is None
    assert preview.upright_still(cfg) == preview.raw_upright_still(cfg)
    assert (preview.visible_still_region(cfg)
            == preview.raw_visible_still_region(cfg))


def test_the_shipped_default_is_unchanged():
    """`center` is the default anchor, so an untouched install sees exactly
    the geometry it saw before any of this."""
    cfg = Config()
    assert cfg.rig.anchor == "center"
    assert preview.upright_still(cfg) == (2304, 1536)
    assert preview.stream_origin(cfg) == (0, 67)


def test_an_anchor_centred_only_on_the_other_axis_still_trims():
    """`top` names an edge on the sensor's row axis, which is where the
    strips are when the camera is upright. Turned, it does not."""
    assert preview.fence_edge(rig(anchor="top", rotate=0)) == "low"
    assert preview.fence_edge(rig(anchor="top", rotate=90)) is None


# -- the pixels ---------------------------------------------------------------


def test_to_scannable_trims_the_frame_to_the_scannable_area():
    cfg = rig(anchor="left")
    raw = Image.new("RGB", (2304, 1536))
    assert preview.to_scannable(cfg, raw).size == preview.upright_still(cfg)


def test_to_scannable_keeps_the_anchored_edge_of_the_picture():
    """The trim comes off the anchored side, so what survives starts at the
    band's first column -- not at the sensor's."""
    cfg = rig(anchor="left")
    raw = Image.new("RGB", (2304, 1536), "black")
    # Paint the sensor rows the trim discards. After the transpose they are
    # the leftmost columns, and after the trim they are gone.
    raw.paste(Image.new("RGB", (2304, STRIP), "red"), (0, 0))
    out = preview.to_scannable(cfg, raw)
    assert out.getpixel((0, 0)) != (255, 0, 0)
    assert out.size == (1536 - STRIP, 2304)


def test_the_units_contract_survives_the_trim():
    """A trimmed frame is still mapped by coverage_mm, so the JPEG's pixel
    dimensions still equal region x resolution / 300, exactly."""
    cfg = rig(anchor="top-left", coverage_mm=(210.0, 341.7))
    frame = preview.to_scannable(cfg, Image.new("RGB", (2304, 1536), "white"))
    settings = ScanSettings(
        region=ScanRegion(x=0, y=0, width=2480, height=3507),
        x_resolution=150, y_resolution=150, color_mode="RGB24",
    )
    jpeg = imaging.render(frame, settings, cfg.rig.coverage_mm, 85,
                          cfg.rig.anchor)
    with Image.open(__import__("io").BytesIO(jpeg)) as out:
        assert out.size == settings.expected_size


def test_a_sheet_pushed_into_the_anchored_corner_scans_whole():
    """Painted in SENSOR coordinates, so this exercises the whole chain:
    transpose, trim, then the eSCL region mapping."""
    cfg = rig(anchor="top-left", coverage_mm=(210.0, 341.7))
    raw = Image.new("RGB", (2304, 1536), "black")
    length = round(297.0 / 341.7 * 2304)        # A4's long side, in sensor px
    raw.paste(Image.new("RGB", (length, 1536 - STRIP), "white"),
              (2304 - length, STRIP))
    settings = ScanSettings(
        region=ScanRegion(x=0, y=0, width=2480, height=3507),
        x_resolution=150, y_resolution=150, color_mode="RGB24",
    )
    jpeg = imaging.render(preview.to_scannable(cfg, raw), settings,
                          cfg.rig.coverage_mm, 92, cfg.rig.anchor)
    with Image.open(__import__("io").BytesIO(jpeg)) as out:
        luma = out.convert("L")
        w, h = luma.size
        corners = [luma.getpixel(p)
                   for p in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3))]
    assert min(corners) > 240, corners


def test_the_trim_scales_to_a_downsampled_frame():
    """Not every frame through here is `capture.native_*` sized.

    `save_scan_still` downsamples the stored still, so the ghost is rebuilt
    from a smaller copy. Cropping that with a full-size box does not fail --
    PIL extends past the edge with black -- so the ghost came out part black,
    in the shape of the difference. This is the case no numeric test covered.
    """
    cfg = rig(anchor="left")
    small = Image.new("RGB", (1600, 1067), "white")
    out = preview.to_scannable(cfg, small)

    # Same proportions as the full-size trim, against the frame in hand.
    assert out.size == (round(1067 * 1416 / 1536), 1600)
    # And every pixel is real, not black padding conjured past the edge.
    assert out.getpixel((out.size[0] - 1, out.size[1] - 1)) == (255, 255, 255)
    assert out.convert("L").getextrema() == (255, 255)


def test_the_rebuilt_ghost_carries_the_scan_everywhere(tmp_path):
    """End to end, through the stored still the daemon actually writes."""
    cfg = replace(rig(anchor="left"), state_dir=tmp_path)
    preview.save_scan_still(cfg, Image.new("RGB", (2304, 1536), (150, 60, 30)))
    path = preview.rebuild_ghost(cfg)
    assert path is not None

    ghost = Image.open(path).convert("RGBA")
    assert ghost.size == preview.preview_size(cfg)
    transform = preview.fit_transform(cfg, preview.marks(cfg))
    vx, vy, vw, vh = preview.video_rect(cfg, *transform)
    gx, gy, gw, gh = preview.ghost_rect(cfg, *transform)

    # Inside the ghost rect and outside the video hole is where the still
    # belongs, and every pixel of it must carry the still. One flat scan means
    # one flat colour; a second colour is the still cropped past its own edge,
    # which arrives as black. Checked against the ghost rect rather than the
    # whole canvas because an oversized paper zooms the picture out and the
    # padding around it is legitimately pad-coloured.
    seen = {ghost.getpixel((x, y))
            for x in range(gx, gx + gw, 7)
            for y in range(gy, gy + gh, 7)
            if not (vx <= x < vx + vw and vy <= y < vy + vh)}
    assert seen, "no border to check"
    assert all(p[3] == 255 for p in seen)
    assert len(seen) == 1, f"the still did not fill the border: {sorted(seen)}"
    assert min(seen.pop()[:3]) > 60, "the border is black, not a washed scan"
